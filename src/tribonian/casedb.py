"""casedb.py -- load CourtListener's quarterly bulk files into one local SQLite database.

    tribonian db load                  # every bulk file found, smallest first; finished tables are skipped
    tribonian db load citation-map     # just one (names: courts citations citation-map parentheticals
                                      #            opinion-clusters dockets opinions)
    tribonian db load --force courts   # reload a table that is already done
    tribonian db status
    tribonian db columns [opinion-clusters ...]   # per bulk file: columns kept, columns left out
    tribonian db index                 # full-text index of case names and parentheticals, for case2md.py find
    tribonian db courtindex            # dockets by court and number, cases by date: a court's opinions, cites found
                                      #   by name or docket number (load builds them too)
    tribonian db opmap [--force]       # opinion id -> case table in the main database (load builds it after opinions)
    tribonian db case cases/463-u-s-29_*.json    # smoke test: who cites this case, how deeply, and for what

Where things live (both default to ./bulk next to this script); each is a variable or a key in
~/.config/tribonian-us/config.toml (tconfig.py):
    CL_BULK / bulk_dir = folder holding the *.csv.bz2 downloads
    CL_DB   / main_db  = the database file (default <bulk_dir>/courtlistener.sqlite)

Loading is append-only: rows go in in file order and every index is built after the
last row. (The first version made id the table's key. The dumps are not sorted by id,
so each insert landed on a random page; on a hard disk that fell from 1,987 rows/s to
under 200 within three million opinions.) Opinion text goes in its own file,
opinions.sqlite (in ./db/ when that folder exists), so the graph database stays small
and the 110 GB of text can sit on whichever disk writes fastest.

The files are PostgreSQL CSV dumps: every value quoted, NULL = nothing between the
commas, a quote inside a value written as \\" (backslash), newlines allowed inside
values. Checked against the 2026-06-30 files; Python's default CSV dialect
(doubled quotes) misreads about 5% of rows.
"""
import argparse, collections, contextlib, csv, io, json, os, re, shutil, sqlite3, subprocess, sys, time, zlib
from pathlib import Path

from tribonian import tconfig, textcodec          # each location: CL_* variable, else the config file, else the default here

HERE = tconfig.WORK                 # the work folder
BULK = tconfig.get("bulk_dir", HERE / "bulk")
DB = tconfig.get("main_db", BULK / "courtlistener.sqlite")
# The big one: compressed opinion text, about 110 GB. It is written once, in order, as fast as the
# disk allows, so it goes on the fast disk: <work>/db/ if that folder exists
# (CL_OPS_DB overrides), else beside the main database.
OPS_DB = tconfig.get("opinions_db", HERE / "db" / "opinions.sqlite" if (HERE / "db").is_dir()
                     else DB.with_name("opinions.sqlite"))
csv.field_size_limit(1 << 31 - 1)

TEXT_FIELDS = ("html_with_citations", "xml_harvard", "html_columbia", "html_lawbox",
               "html_anon_2020", "html", "plain_text")          # first non-empty one is kept


def integer(v):
    return int(v) if v not in ("", None) else None


def keep_opinion(r):
    """One opinions row from the CSV's dict: the first non-empty text field, compressed (textcodec: zstd)."""
    for f in TEXT_FIELDS:
        if r.get(f):
            return (integer(r["id"]), integer(r["cluster_id"]), r.get("type"), r.get("author_str"),
                    f, textcodec.pack(r[f].encode("utf-8")))
    return (integer(r["id"]), integer(r["cluster_id"]), r.get("type"), r.get("author_str"), None, None)


# A court's dockets (View Dockets lists a court's opinions newest first: dockets by court, then their clusters).
COURT_INDEX = "CREATE INDEX IF NOT EXISTS dockets_by_court ON dockets(court_id)"
# Cases by date: a cite the citations table lacks (most since 2018) is found by name, court and date (scotus.resolve_name).
DATE_INDEX = "CREATE INDEX IF NOT EXISTS clusters_by_date ON clusters(date_filed)"
# Dockets by number: a Westlaw cite ("No. C 07-02299 JF (PR), 2009 WL 773499") is found by its court and docket number.
NUMBER_INDEX = "CREATE INDEX IF NOT EXISTS dockets_by_number ON dockets(court_id, docket_number)"

# name -> (table, CREATE body, row function(dict) -> tuple, statements to run after the load)
TABLES = {
    "courts": ("courts", "id TEXT PRIMARY KEY, citation_string TEXT, short_name TEXT, full_name TEXT, jurisdiction TEXT",
               lambda r: (r["id"], r["citation_string"], r["short_name"], r["full_name"], r["jurisdiction"]), []),
    "citations": ("cites", "cluster_id INTEGER, volume TEXT, reporter TEXT, page TEXT, type INTEGER",
                  lambda r: (integer(r["cluster_id"]), r["volume"], r["reporter"], r["page"], integer(r["type"])),
                  ["CREATE INDEX cites_by_cite ON cites(volume, reporter, page)",
                   "CREATE INDEX cites_by_cluster ON cites(cluster_id)"]),
    "citation-map": ("edges", "citing INTEGER, cited INTEGER, depth INTEGER",
                     lambda r: (integer(r["citing_opinion_id"]), integer(r["cited_opinion_id"]), integer(r["depth"])),
                     ["CREATE INDEX edges_out ON edges(citing, cited, depth)",
                      "CREATE INDEX edges_in ON edges(cited, citing, depth)"]),
    "parentheticals": ("parens", "id INTEGER, described INTEGER, describing INTEGER, score REAL, group_id INTEGER, text TEXT",
                       lambda r: (integer(r["id"]), integer(r["described_opinion_id"]), integer(r["describing_opinion_id"]),
                                  float(r["score"] or 0), integer(r["group_id"]), r["text"]),
                       ["CREATE INDEX parens_by_described ON parens(described)"]),
    "opinion-clusters": ("clusters", "id INTEGER, case_name TEXT, case_name_short TEXT, case_name_full TEXT, "
                         "date_filed TEXT, citation_count INTEGER, docket_id INTEGER, slug TEXT, judges TEXT, "
                         "precedential_status TEXT, scdb_id TEXT, other_dates TEXT, summary TEXT, syllabus TEXT, headmatter BLOB",
                         lambda r: (integer(r["id"]), r["case_name"], r["case_name_short"], r["case_name_full"], r["date_filed"],
                                    integer(r["citation_count"]), integer(r["docket_id"]), r["slug"], r["judges"],
                                    r["precedential_status"], r["scdb_id"], r["other_dates"], r["summary"] or None, r["syllabus"] or None,
                                    zlib.compress(r["headmatter"].encode("utf-8"), 6) if r["headmatter"] else None),   # zlib (the GUI reads it); opinion text is zstd (textcodec)
                         ["CREATE UNIQUE INDEX clusters_by_id ON clusters(id)", "CREATE INDEX clusters_by_docket ON clusters(docket_id)",
                          DATE_INDEX]),
    # nature_of_suit / cause sort case notes into folders (case2md.py FOLDERS); the rest lets
    # recap fetch skip its docket request
    "dockets": ("dockets", "id INTEGER, court_id TEXT, docket_number TEXT, case_name TEXT, case_name_short TEXT, "
                "nature_of_suit TEXT, cause TEXT, date_filed TEXT, date_terminated TEXT, assigned_to_str TEXT, "
                "referred_to_str TEXT, jurisdiction_type TEXT, pacer_case_id TEXT, slug TEXT",
                lambda r: (integer(r["id"]), r["court_id"], r.get("docket_number")) + tuple(
                    r.get(k) or None for k in ("case_name", "case_name_short", "nature_of_suit", "cause", "date_filed",
                                               "date_terminated", "assigned_to_str", "referred_to_str",
                                               "jurisdiction_type", "pacer_case_id", "slug")),
                ["CREATE UNIQUE INDEX dockets_by_id ON dockets(id)", COURT_INDEX, NUMBER_INDEX]),
    "opinions": ("opinions", "id INTEGER, cluster_id INTEGER, type TEXT, author_str TEXT, field TEXT, text BLOB",
                 keep_opinion, ["CREATE UNIQUE INDEX opinions_by_id ON opinions(id)",
                                "CREATE INDEX opinions_by_cluster ON opinions(cluster_id)"]),
}
ORDER = ["courts", "citations", "parentheticals", "citation-map", "opinion-clusters", "dockets", "opinions"]


def has_court_index(con):
    try:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='dockets_by_court'").fetchone())
    except sqlite3.Error:
        return False


PRAGMAS = ("PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF; PRAGMA cache_size=-400000; PRAGMA temp_store=FILE; "
           "PRAGMA page_size=16384;")     # page_size only takes effect on a file that is still empty


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=600)
    con.executescript(PRAGMAS + "CREATE TABLE IF NOT EXISTS loaded(name TEXT PRIMARY KEY, file TEXT, rows INTEGER, seconds REAL, finished TEXT);")
    return con


def connect_ops():
    ops = sqlite3.connect(OPS_DB, timeout=600)
    ops.executescript(PRAGMAS)
    return ops


def attach_ops(con):
    """Make the opinions table visible through a connection to the main database."""
    if OPS_DB.exists():
        con.execute("ATTACH DATABASE ? AS ops", (str(OPS_DB),))
    return con


def newest(name):
    """bulk/<name>-YYYY-MM-DD.csv.bz2, latest date; 'opinions' must not match 'opinion-clusters'."""
    hits = sorted(p for p in BULK.glob(name + "-*.csv.bz2") if re.fullmatch(re.escape(name) + r"-\d{4}-\d\d-\d\d\.csv\.bz2", p.name))
    return hits[-1] if hits else None


CORRUPT = "%s is corrupt (%s): delete it and download it again (the Sync page, or tribonian setup download)"


@contextlib.contextmanager
def open_stream(path):
    """The decompressed bytes of a .csv.bz2, as a binary file: lbzip2/pbzip2/bzip2 when one is on PATH (its own process,
    every core for lbzip2), else indexed_bzip2 (every core, in this process: Windows), else Python's bz2 (one core).
    Closed on the way out, so a load that stops part way leaves no decompression threads running."""
    tool = next((t for t in ("lbzip2", "pbzip2", "bzip2") if shutil.which(t)), None)
    if tool:
        proc = subprocess.Popen([tool, "-dc", str(path)], stdout=subprocess.PIPE, bufsize=1 << 20)
        try:
            yield proc.stdout
        finally:
            proc.stdout.close()
            if proc.poll() is None:                 # stopped before the end: don't wait for the rest of the file
                proc.kill()
                proc.wait()
            elif proc.returncode:                   # truncated or corrupt download: never load it silently
                raise RuntimeError(CORRUPT % (path.name, "%s exited %d" % (tool, proc.returncode)))
        return
    try:
        import indexed_bzip2
        raw = indexed_bzip2.open(str(path), parallelization=os.cpu_count() or 1)
    except ImportError:
        import bz2
        raw = bz2.open(path, "rb")
    try:
        yield raw
    finally:
        raw.close()


def rows_of(path):
    """Stream a .csv.bz2 as dicts (None for a row with the wrong number of fields). A corrupt or cut file stops the
    load with a message naming it (never loaded silently)."""
    with open_stream(path) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.reader(text, doublequote=False, escapechar="\\")
        try:
            header = next(reader)
            n = len(header)
            for row in reader:
                if len(row) == n:
                    yield dict(zip(header, row))
                else:
                    yield None                          # counted as a bad row by the caller
        except (OSError, EOFError, ValueError) as e:    # Python's bz2 or indexed_bzip2: a bad stream or a cut file
            raise RuntimeError(CORRUPT % (path.name, e))
        text.detach()                                   # the stream is closed by open_stream, not by the wrapper


BAD = object()          # a row with the wrong number of fields
CHUNK = 256             # rows per task on the worker threads


def convert_chunk(fn, rows):
    """fn over rows: each result is the row tuple, BAD, or the KeyError/ValueError fn raised."""
    out = []
    for r in rows:
        if r is None:
            out.append(BAD)
            continue
        try:
            out.append(fn(r))
        except (KeyError, ValueError) as e:
            out.append(e)
    return out


def converted(rows, fn, workers):
    """fn over rows in file order. With workers > 1 the rows go to worker threads in chunks: compression releases Python's
    lock, so the opinions' compression (43% of the load's time, profiled) runs beside the CSV parsing. At most
    4 chunks per worker are in flight, so memory stays bounded."""
    if workers <= 1:
        for r in rows:
            yield from convert_chunk(fn, (r,))
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as pool:
        waiting, chunk = collections.deque(), []
        for r in rows:
            chunk.append(r)
            if len(chunk) == CHUNK:
                waiting.append(pool.submit(convert_chunk, fn, chunk))
                chunk = []
                if len(waiting) >= 4 * workers:
                    yield from waiting.popleft().result()
        if chunk:
            waiting.append(pool.submit(convert_chunk, fn, chunk))
        while waiting:
            yield from waiting.popleft().result()


# ---------------------------------------------------------------- opinions through pyarrow's CSV reader
# Measured on bblinux (2026-11-06), parsing alone with lbzip2 feeding it: Python's csv module 4,238 rows/s, pyarrow
# 11,880, a Rust csv-crate test 10,271; lbzip2 alone ~512 MB/s. pyarrow reads several blocks at once in C++, and only
# the columns the opinions table keeps. Every value is read as raw bytes (no UTF-8 check that would reject a row) and
# decoded the way Python's reader does (bad bytes replaced), so both paths store the same rows.
# TRIBONIAN_CSV=python forces the Python reader (tests compare the two).
OPINION_COLUMNS = ["id", "cluster_id", "type", "author_str"] + list(TEXT_FIELDS)


def arrow_available():
    if os.environ.get("TRIBONIAN_CSV") == "python":
        return False
    try:
        import pyarrow.csv  # noqa: F401
        return True
    except ImportError:
        return False


def _text(b):
    return "" if b is None else b.decode("utf-8", "replace")


def _utf8(b):
    """Stored text bytes as Python's reader would give them: bad UTF-8 replaced (U+FFFD), good bytes as they are."""
    return b if b.isascii() else b.decode("utf-8", "replace").encode("utf-8")


def arrow_chunk(part):
    """keep_opinion over a slice of a pyarrow batch: the same tuples, BAD never (pyarrow skips wrong-length rows)."""
    cols = {c: part.column(c).to_pylist() for c in OPINION_COLUMNS}
    out = []
    for i in range(part.num_rows):
        try:
            head = (integer(_text(cols["id"][i])), integer(_text(cols["cluster_id"][i])),
                    _text(cols["type"][i]), _text(cols["author_str"][i]))
        except ValueError as e:
            out.append(e)
            continue
        for f in TEXT_FIELDS:
            v = cols[f][i]
            if v:
                out.append(head + (f, textcodec.pack(_utf8(v))))
                break
        else:
            out.append(head + (None, None))
    return out


def arrow_opinions(path, workers):
    """The opinions rows in file order, like converted(rows_of(path), keep_opinion, workers): pyarrow parses, the
    worker threads turn slices into rows and compress, BAD is yielded once per wrong-length row pyarrow skipped."""
    import pyarrow as pa, pyarrow.csv as pc
    from concurrent.futures import ThreadPoolExecutor
    skipped = [0]

    def wrong_length(row):
        skipped[0] += 1
        return "skip"

    with open_stream(path) as raw:
        rd = pc.open_csv(raw, read_options=pc.ReadOptions(block_size=32 << 20, use_threads=True),
                         parse_options=pc.ParseOptions(double_quote=False, escape_char="\\", newlines_in_values=True,
                                                       invalid_row_handler=wrong_length),
                         convert_options=pc.ConvertOptions(include_columns=OPINION_COLUMNS, include_missing_columns=True,
                                                           column_types={c: pa.binary() for c in OPINION_COLUMNS},
                                                           strings_can_be_null=False))
        try:
            with ThreadPoolExecutor(workers) as pool:
                waiting = collections.deque()
                for batch in rd:
                    for at in range(0, batch.num_rows, CHUNK):
                        waiting.append(pool.submit(arrow_chunk, batch.slice(at, CHUNK)))
                        if len(waiting) >= 4 * workers:
                            yield from waiting.popleft().result()
                while waiting:
                    yield from waiting.popleft().result()
        except (OSError, EOFError, pa.ArrowInvalid) as e:
            raise RuntimeError(CORRUPT % (path.name, e))
        finally:
            rd.close()
    for _ in range(skipped[0]):
        yield BAD


def table_rows(name, path, fn, workers):
    """The rows a load inserts, in file order: pyarrow for the opinions when it is installed, else Python's reader."""
    if name == "opinions" and arrow_available():
        print("  reading with pyarrow, %d worker threads" % workers)
        return arrow_opinions(path, workers)
    if workers > 1:
        print("  %d worker threads" % workers)
    return converted(rows_of(path), fn, workers)


def workers_for(name):
    """Worker threads for a table's rows: only the opinions' (compression) are worth it; leave a core for parsing."""
    return max(1, (os.cpu_count() or 2) - 1) if name == "opinions" else 1


def load(name, con, force=False, limit=None):
    table, body, fn, after = TABLES[name]
    path = newest(name)
    if path is None:
        print("%-17s no file in %s" % (name, BULK))
        return
    done = con.execute("SELECT file, rows FROM loaded WHERE name=?", (name,)).fetchone() if con else None
    if done and done[0] == path.name and not force:
        print("%-17s already loaded (%s rows from %s)" % (name, format(done[1], ","), done[0]))
        return
    print("%-17s loading %s (%.1f GB compressed)" % (name, path.name, path.stat().st_size / 1e9))
    main = con                                                  # None with --limit: the main database is left alone
    if not limit:
        con.execute("DELETE FROM loaded WHERE name=?", (name,))
        con.commit()
    if limit:                                                   # testing: never touch the production tables
        sample = OPS_DB if name == "opinions" else DB
        sample = sample.with_name(sample.stem + "-sample.sqlite")
        con = sqlite3.connect(sample, timeout=600)
        con.executescript(PRAGMAS)
        print("  --limit: loading into %s (production tables untouched)" % sample)
    elif name == "opinions":
        main.execute("DROP TABLE IF EXISTS opinions")          # left over from the first version, if any
        if OPS_DB.exists():
            OPS_DB.unlink()                                     # start the big file clean rather than DROP 90 GB
        con = connect_ops()
    con.execute("DROP TABLE IF EXISTS %s" % table)
    con.execute("CREATE TABLE %s (%s)" % (table, body))
    ncol = body.count(",") + 1 - body.count("PRIMARY KEY,") * 0
    ncol = len([c for c in re.split(r",\s*(?![^()]*\))", body)])
    sql = "INSERT INTO %s VALUES (%s)" % (table, ",".join("?" * ncol))
    t0, n, bad, batch = time.time(), 0, 0, []
    t_mark = t0
    for x in table_rows(name, path, fn, workers_for(name)):
        if x is BAD:
            bad += 1
            continue
        if isinstance(x, Exception):
            if bad == 0:
                print("  first unreadable row: %r" % (x,))
            bad += 1
            continue
        batch.append(x)
        n += 1
        if len(batch) >= 50000:
            con.executemany(sql, batch)
            batch = []
            if n % 200000 == 0:
                con.commit()
            if n % 1000000 == 0:
                now = time.time()
                print("  %s rows, %.0f rows/s overall, %.0f rows/s for the last million" % (
                    format(n, ","), n / (now - t0), 1000000 / (now - t_mark)), flush=True)
                t_mark = now
        if limit and n >= limit:
            break
    con.executemany(sql, batch)
    con.commit()
    for stmt in after:
        t1 = time.time()
        con.execute(stmt)
        con.commit()
        print("  %s: %.1f min" % (stmt.split(" ON ")[0].lower(), (time.time() - t1) / 60))
    if not limit:
        main.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?,?,datetime('now'))", (name, path.name, n, time.time() - t0))
    con.commit()
    if main is not None:
        main.commit()
    if con is not main:
        con.close()
    print("  done: %s rows in %.1f min%s" % (format(n, ","), (time.time() - t0) / 60,
                                            ", %d unreadable rows skipped" % bad if bad else ""))


# ---------------------------------------------------------------- search index (case2md.py find)
# Case names and the parentheticals other courts wrote about each case, in SQLite full-text (FTS5)
# tables in their own file. A nickname like "Glomar" is in no case name, but courts write it in
# their parentheticals about Phillippi v. CIA, so both are searched.
SEARCH_DB = tconfig.get("search_db", DB.with_name("search.sqlite"))


def search_stamp(con):
    """What the index was built from: the finish times of the two source tables."""
    return "|".join(str(r) for r in con.execute(
        "SELECT name, finished FROM loaded WHERE name IN ('opinion-clusters', 'parentheticals') ORDER BY name"))


def build_search(con):
    t0 = time.time()
    tmp = SEARCH_DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    s = sqlite3.connect(tmp)
    s.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                    "CREATE VIRTUAL TABLE names USING fts5(cluster_id UNINDEXED, name, tokenize='unicode61 remove_diacritics 2');"
                    "CREATE VIRTUAL TABLE parens USING fts5(described UNINDEXED, text, tokenize='unicode61 remove_diacritics 2');"
                    "CREATE TABLE meta(stamp TEXT);")
    for table, sql, ins in (
            ("names", "SELECT id, coalesce(case_name, '') || ' ' || coalesce(case_name_short, '') FROM clusters",
             "INSERT INTO names VALUES (?, ?)"),
            ("parens", "SELECT described, text FROM parens WHERE text IS NOT NULL", "INSERT INTO parens VALUES (?, ?)")):
        n, cur = 0, con.execute(sql)
        while True:
            rows = cur.fetchmany(100000)
            if not rows:
                break
            s.executemany(ins, rows)
            n += len(rows)
            if n % 1000000 == 0:
                print("  %s: %s rows" % (table, format(n, ",")), flush=True)
        s.commit()
        print("  %s: %s rows indexed" % (table, format(n, ",")))
    s.execute("INSERT INTO meta VALUES (?)", (search_stamp(con),))
    s.execute("INSERT INTO names(names) VALUES ('optimize')")
    s.execute("INSERT INTO parens(parens) VALUES ('optimize')")
    s.commit()
    s.close()
    tmp.replace(SEARCH_DB)
    print("search index: %s (%.1f GB) in %.1f min" % (SEARCH_DB, SEARCH_DB.stat().st_size / 1e9, (time.time() - t0) / 60))


def have(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


# ---------------------------------------------------------------- opmap: opinion id -> case, off the text file
# Finding a case's citers needs each citing opinion's case (cluster) id. Read from opinions.sqlite that
# is one random read of the text file per citer (about 12 ms each on the hard disk: 26 s for 2,189
# citers, 2026-10-24). opmap holds just (id, cluster_id) in the main database. It is built from the
# text file's two indexes, so the 90 GB of text is never read, and rebuilt whenever the opinions are.
def build_opmap(con, force=False):
    """(Re)build opmap from opinions.sqlite's indexes. Marked current in `loaded` under the opinions file's name."""
    of = con.execute("SELECT file FROM loaded WHERE name='opinions'").fetchone()
    if not of or not OPS_DB.exists():
        print("opmap             skipped: opinions not loaded")
        return
    cur = con.execute("SELECT file FROM loaded WHERE name='opmap'").fetchone()
    if cur and cur[0] == of[0] and not force:
        print("opmap             already built (from %s)" % of[0])
        return
    print("opmap             building from the indexes of %s" % OPS_DB)
    t0 = time.time()
    con.execute("DELETE FROM loaded WHERE name='opmap'")      # readers fall back to ops.opinions meanwhile
    con.commit()
    if not any(r[1] == "ops" for r in con.execute("PRAGMA database_list")):
        attach_ops(con)
    # rowid -> id and rowid -> cluster_id, each a scan of one index (both carry the rowid), joined on rowid
    con.executescript("""
        DROP TABLE IF EXISTS temp.op_r_id; DROP TABLE IF EXISTS temp.op_r_cl;
        CREATE TEMP TABLE op_r_id(r INTEGER PRIMARY KEY, id INTEGER);
        CREATE TEMP TABLE op_r_cl(r INTEGER PRIMARY KEY, cluster_id INTEGER);
        INSERT INTO op_r_id SELECT rowid, id FROM ops.opinions INDEXED BY opinions_by_id ORDER BY rowid;
        INSERT INTO op_r_cl SELECT rowid, cluster_id FROM ops.opinions INDEXED BY opinions_by_cluster ORDER BY rowid;""")
    n_id, n_cl = (con.execute("SELECT count(*) FROM temp.%s" % t).fetchone()[0] for t in ("op_r_id", "op_r_cl"))
    if n_id != n_cl:
        sys.exit("opmap: the two opinion indexes disagree (%d vs %d rows); is opinions.sqlite complete?" % (n_id, n_cl))
    print("  indexes read: %s opinions, %.1f min" % (format(n_id, ","), (time.time() - t0) / 60))
    con.executescript("""
        DROP TABLE IF EXISTS main.opmap;
        CREATE TABLE main.opmap(id INTEGER PRIMARY KEY, cluster_id INTEGER);
        INSERT INTO main.opmap SELECT a.id, b.cluster_id FROM temp.op_r_id a JOIN temp.op_r_cl b ON b.r = a.r
            WHERE a.id IS NOT NULL ORDER BY a.id;
        CREATE INDEX main.opmap_by_cluster ON opmap(cluster_id);
        DROP TABLE temp.op_r_id; DROP TABLE temp.op_r_cl;""")
    n = con.execute("SELECT count(*) FROM main.opmap").fetchone()[0]
    con.execute("INSERT OR REPLACE INTO loaded VALUES ('opmap', ?, ?, ?, datetime('now'))", (of[0], n, time.time() - t0))
    con.commit()
    print("  done: %s rows in %.1f min" % (format(n, ","), (time.time() - t0) / 60))


def op_table(con):
    """The table to read opinion id -> cluster_id from: 'opmap' when it was built from the opinions now
    loaded, else 'ops.opinions' (the text file). Both have columns id and cluster_id."""
    try:
        ok = con.execute("SELECT (SELECT file FROM loaded WHERE name='opmap') = "
                         "(SELECT file FROM loaded WHERE name='opinions')").fetchone()[0]
    except sqlite3.Error:
        return "ops.opinions"
    return "opmap" if ok else "ops.opinions"


def cmd_status(args):
    print("bulk files: %s\ndatabase:   %s%s" % (BULK, DB, "  (%.1f GB)" % (DB.stat().st_size / 1e9) if DB.exists() else "  (not created yet)"))
    if OPS_DB.exists():
        print("opinions:   %s  (%.1f GB)" % (OPS_DB, OPS_DB.stat().st_size / 1e9))
    con = connect()
    done = {r[0]: r for r in con.execute("SELECT name, file, rows, seconds FROM loaded")}
    for name in ORDER:
        p = newest(name)
        d = done.get(name)
        print("  %-17s %-42s %s" % (name, p.name if p else "(not downloaded)",
                                  "%s rows, loaded in %.1f min" % (format(d[2], ","), d[3] / 60) if d else "not loaded"))
    print("  %-17s %s" % ("opmap", "current (%s rows)" % format(done["opmap"][2], ",") if op_table(con) == "opmap"
                          else "not built or stale: tribonian db opmap"))


def cmd_case(args):
    con = connect()
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    ids = [o["id"] for o in data["opinions"]]
    q = ",".join("?" * len(ids))
    print("%s  (cluster %s; opinion ids %s)" % (data["cluster"].get("case_name"), data["cluster"].get("id"), ids))
    if have(con, "edges"):
        n_in = con.execute("SELECT count(DISTINCT citing) FROM edges WHERE cited IN (%s)" % q, ids).fetchone()[0]
        n_out = con.execute("SELECT count(DISTINCT cited) FROM edges WHERE citing IN (%s)" % q, ids).fetchone()[0]
        print("cited by %s opinions; cites %s opinions.   CourtListener's own count: %s" % (
            format(n_in, ","), n_out, data["cluster"].get("citation_count")))
        print("times cited per citing opinion (exact, every citing opinion):")
        for label, lo, hi in (("1 (passing)", 1, 1), ("2-3", 2, 3), ("4-9", 4, 9), ("10+ (heavy)", 10, 10**9)):
            k = con.execute("SELECT count(*) FROM (SELECT citing, sum(depth) d FROM edges WHERE cited IN (%s) GROUP BY citing) "
                            "WHERE d BETWEEN ? AND ?" % q, ids + [lo, hi]).fetchone()[0]
            print("  %-12s %7s  %5.1f%%" % (label, format(k, ","), 100 * k / max(n_in, 1)))
    if have(con, "parens"):
        n = con.execute("SELECT count(*) FROM parens WHERE described IN (%s)" % q, ids).fetchone()[0]
        print("%s parentheticals describe it. Highest scored:" % format(n, ","))
        for score, text in con.execute("SELECT score, text FROM parens WHERE described IN (%s) ORDER BY score DESC LIMIT 8" % q, ids):
            print("  %.2f  %s" % (score, text[:230].replace("\n", " ")))


# ================================================================ columns

def header_of(path):
    """The CSV header of a .csv.bz2: only the first block is decompressed."""
    import bz2
    with bz2.open(path, "rt", encoding="utf-8", errors="replace", newline="") as f:
        return next(csv.reader(f, doublequote=False, escapechar="\\"))


class _Seen(dict):
    """A stand-in row that records which columns a TABLES row function reads."""
    def __init__(self):
        super().__init__()
        self.used = []

    def __getitem__(self, k):
        self.used.append(k)
        return "1"

    def get(self, k, default=None):
        self.used.append(k)
        return "1"


def kept_columns(name):
    row = _Seen()
    TABLES[name][2](row)
    if name == "opinions":                    # any one text field is kept, whichever comes first
        row.used += list(TEXT_FIELDS)
    return set(row.used)


def cmd_columns(args):
    """Per bulk file: the columns loaded into the database and the ones left out."""
    stems = sorted({re.sub(r"-\d{4}-\d\d-\d\d\.csv\.bz2$", "", p.name) for p in BULK.glob("*-????-??-??.csv.bz2")})
    names = args.names or [n for n in ORDER if n in stems] + [n for n in stems if n not in ORDER]
    if not names:
        sys.exit("No bulk files in %s" % BULK)
    for name in names:
        path = newest(name)
        if path is None:
            print("%s: no file in %s" % (name, BULK))
            continue
        cols = header_of(path)
        if name not in TABLES:
            print("\n%s  (%s, not loaded): %d columns\n  %s" % (name, path.name, len(cols), ", ".join(cols)))
            continue
        kept = kept_columns(name)
        print("\n%s  (%s -> table %s)" % (name, path.name, TABLES[name][0]))
        print("  kept    (%2d): %s" % (sum(c in kept for c in cols), ", ".join(c for c in cols if c in kept)))
        print("  dropped (%2d): %s" % (sum(c not in kept for c in cols), ", ".join(c for c in cols if c not in kept) or "-"))
        gone = sorted(kept - set(cols) - set(TEXT_FIELDS))
        if gone:
            print("  expected but not in this file: %s" % ", ".join(gone))


def main():
    sys.stdout.reconfigure(line_buffering=True)      # so progress shows up at once in a log file (nohup, > file)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("names", nargs="*", choices=ORDER + [[]], help="default: everything downloaded")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, help="testing: stop after this many rows, loaded into a separate *-sample.sqlite "
                                              "file; the production tables are not touched")
    sub.add_parser("status")
    sub.add_parser("index", help="(re)build the full-text index case2md.py find uses")
    sub.add_parser("courtindex", help="index dockets by court (a court's opinions in View Dockets) and cases by date "
                                      "(cites found by name), dockets by number (Westlaw cites); load builds them too")
    p = sub.add_parser("opmap", help="(re)build opinion id -> case in the main database (load does it after opinions)")
    p.add_argument("--force", action="store_true", help="rebuild even if current")
    p = sub.add_parser("case")
    p.add_argument("json")
    p = sub.add_parser("columns", help="per bulk file: columns loaded and columns left out")
    p.add_argument("names", nargs="*", help="bulk file names, e.g. opinion-clusters (default: every file in bulk/)")
    args = ap.parse_args()
    if args.cmd == "load":
        # --limit loads a sample into *-sample.sqlite and leaves the main database alone: no lock, no bookkeeping
        con = None if args.limit else connect()
        lock = Path(str(DB) + ".loading")          # casemap.py refuses to read while this exists
        if not args.limit:
            lock.write_text("casedb.py load, pid %d\n" % os.getpid())
        try:
            for name in (args.names or ORDER):
                load(name, con, args.force, args.limit)
            if not args.limit:
                build_opmap(con)                   # no-op unless the opinions changed
        except KeyboardInterrupt:
            print("\ninterrupted: the table being loaded is incomplete and not marked done; rerun the same command to redo it.")
        except RuntimeError as e:                  # a corrupt download (rows_of): say which, without a traceback
            sys.exit("load stopped: %s" % e)
        finally:
            if not args.limit:
                lock.unlink(missing_ok=True)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "courtindex":
        if Path(str(DB) + ".loading").exists():
            sys.exit("casedb.py load is running; index after it finishes.")
        con = sqlite3.connect(DB, timeout=600)
        for name, sql, what in (("dockets_by_court", COURT_INDEX, "dockets by court (one pass over the dockets table)"),
                                ("clusters_by_date", DATE_INDEX, "cases by date (one pass over the clusters table)"),
                                ("dockets_by_number", NUMBER_INDEX, "dockets by court and number (one pass over the dockets table)")):
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone():
                print("%s is already there" % name)
                continue
            t = time.time()
            print("indexing %s ..." % what)
            con.execute(sql)
            con.commit()
            print("done in %.0f s" % (time.time() - t))
    elif args.cmd == "index":
        if Path(str(DB) + ".loading").exists():
            sys.exit("casedb.py load is running; a long read now can make it fail with 'database is locked'. Index after it finishes.")
        build_search(sqlite3.connect("file:%s?mode=ro" % DB, uri=True))
    elif args.cmd == "opmap":
        con = connect()
        lock = Path(str(DB) + ".loading")          # readers wait while the main database is written
        lock.write_text("casedb.py opmap, pid %d\n" % os.getpid())
        try:
            build_opmap(con, args.force)
        finally:
            lock.unlink(missing_ok=True)
    elif args.cmd == "columns":
        cmd_columns(args)
    else:
        cmd_case(args)


if __name__ == "__main__":
    main()
