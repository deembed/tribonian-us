#!/usr/bin/env python3
"""casedb.py -- load CourtListener's quarterly bulk files into one local SQLite database.

    ./casedb.py load                  # every bulk file found, smallest first; finished tables are skipped
    ./casedb.py load citation-map     # just one (names: courts citations citation-map parentheticals
                                      #            opinion-clusters dockets opinions)
    ./casedb.py load --force courts   # reload a table that is already done
    ./casedb.py status
    ./casedb.py index                 # full-text index of case names and parentheticals, for case2md.py find
    ./casedb.py case cases/463-u-s-29_*.json    # smoke test: who cites this case, how deeply, and for what

Where things live (both default to ./bulk next to this script):
    CL_BULK = folder holding the *.csv.bz2 downloads
    CL_DB   = the database file (default $CL_BULK/courtlistener.sqlite)

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
import argparse, csv, io, json, os, re, shutil, sqlite3, subprocess, sys, time, zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BULK = Path(os.environ.get("CL_BULK") or HERE / "bulk")
DB = Path(os.environ.get("CL_DB") or BULK / "courtlistener.sqlite")
# The big one: compressed opinion text, about 110 GB. It is written once, in order, as fast as the
# disk allows, so it goes on the fast disk: ./db/ next to this script if that folder exists
# (CL_OPS_DB overrides), else beside the main database.
OPS_DB = Path(os.environ.get("CL_OPS_DB") or (HERE / "db" / "opinions.sqlite" if (HERE / "db").is_dir()
                                              else DB.with_name("opinions.sqlite")))
csv.field_size_limit(1 << 31 - 1)

TEXT_FIELDS = ("html_with_citations", "xml_harvard", "html_columbia", "html_lawbox",
               "html_anon_2020", "html", "plain_text")          # first non-empty one is kept


def integer(v):
    return int(v) if v not in ("", None) else None


def keep_opinion(r):
    for f in TEXT_FIELDS:
        if r.get(f):
            return (integer(r["id"]), integer(r["cluster_id"]), r.get("type"), r.get("author_str"),
                    f, zlib.compress(r[f].encode("utf-8"), 6))
    return (integer(r["id"]), integer(r["cluster_id"]), r.get("type"), r.get("author_str"), None, None)


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
                         "precedential_status TEXT, scdb_id TEXT, other_dates TEXT, summary TEXT, syllabus TEXT",
                         lambda r: (integer(r["id"]), r["case_name"], r["case_name_short"], r["case_name_full"], r["date_filed"],
                                    integer(r["citation_count"]), integer(r["docket_id"]), r["slug"], r["judges"],
                                    r["precedential_status"], r["scdb_id"], r["other_dates"], r["summary"] or None, r["syllabus"] or None),
                         ["CREATE UNIQUE INDEX clusters_by_id ON clusters(id)", "CREATE INDEX clusters_by_docket ON clusters(docket_id)"]),
    # nature_of_suit / cause sort case notes into folders (case2md.py FOLDERS); the rest lets
    # recap fetch skip its docket request
    "dockets": ("dockets", "id INTEGER, court_id TEXT, docket_number TEXT, case_name TEXT, case_name_short TEXT, "
                "nature_of_suit TEXT, cause TEXT, date_filed TEXT, date_terminated TEXT, assigned_to_str TEXT, "
                "referred_to_str TEXT, jurisdiction_type TEXT, pacer_case_id TEXT, slug TEXT",
                lambda r: (integer(r["id"]), r["court_id"], r.get("docket_number")) + tuple(
                    r.get(k) or None for k in ("case_name", "case_name_short", "nature_of_suit", "cause", "date_filed",
                                               "date_terminated", "assigned_to_str", "referred_to_str",
                                               "jurisdiction_type", "pacer_case_id", "slug")),
                ["CREATE UNIQUE INDEX dockets_by_id ON dockets(id)"]),
    "opinions": ("opinions", "id INTEGER, cluster_id INTEGER, type TEXT, author_str TEXT, field TEXT, text BLOB",
                 keep_opinion, ["CREATE UNIQUE INDEX opinions_by_id ON opinions(id)",
                                "CREATE INDEX opinions_by_cluster ON opinions(cluster_id)"]),
}
ORDER = ["courts", "citations", "parentheticals", "citation-map", "opinion-clusters", "dockets", "opinions"]


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


def rows_of(path):
    """Stream a .csv.bz2 as dicts. lbzip2/pbzip2 use every core; bzip2 one; Python's own module is the fallback."""
    tool = next((t for t in ("lbzip2", "pbzip2", "bzip2") if shutil.which(t)), None)
    if tool:
        proc = subprocess.Popen([tool, "-dc", str(path)], stdout=subprocess.PIPE, bufsize=1 << 20)
        raw = proc.stdout
    else:
        import bz2
        proc, raw = None, bz2.open(path, "rb")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    reader = csv.reader(text, doublequote=False, escapechar="\\")
    header = next(reader)
    n = len(header)
    for row in reader:
        if len(row) == n:
            yield dict(zip(header, row))
        else:
            yield None                                  # counted as a bad row by the caller
    if proc:
        proc.stdout.close()
        proc.wait()


def load(name, con, force=False, limit=None):
    table, body, fn, after = TABLES[name]
    path = newest(name)
    if path is None:
        print("%-17s no file in %s" % (name, BULK))
        return
    done = con.execute("SELECT file, rows FROM loaded WHERE name=?", (name,)).fetchone()
    if done and done[0] == path.name and not force:
        print("%-17s already loaded (%s rows from %s)" % (name, format(done[1], ","), done[0]))
        return
    print("%-17s loading %s (%.1f GB compressed)" % (name, path.name, path.stat().st_size / 1e9))
    con.execute("DELETE FROM loaded WHERE name=?", (name,))
    con.commit()
    main = con
    if name == "opinions":
        main.execute("DROP TABLE IF EXISTS opinions")          # left over from the first version, if any
        if OPS_DB.exists() and not limit:
            OPS_DB.unlink()                                     # start the big file clean rather than DROP 90 GB
        con = connect_ops()
    con.execute("DROP TABLE IF EXISTS %s" % table)
    con.execute("CREATE TABLE %s (%s)" % (table, body))
    ncol = body.count(",") + 1 - body.count("PRIMARY KEY,") * 0
    ncol = len([c for c in re.split(r",\s*(?![^()]*\))", body)])
    sql = "INSERT INTO %s VALUES (%s)" % (table, ",".join("?" * ncol))
    t0, n, bad, batch = time.time(), 0, 0, []
    t_mark = t0
    for r in rows_of(path):
        if r is None:
            bad += 1
            continue
        try:
            batch.append(fn(r))
        except (KeyError, ValueError) as e:
            if bad == 0:
                print("  first unreadable row: %r" % (e,))
            bad += 1
            continue
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
    main.commit()
    if con is not main:
        con.close()
    print("  done: %s rows in %.1f min%s" % (format(n, ","), (time.time() - t0) / 60,
                                            ", %d unreadable rows skipped" % bad if bad else ""))


# ---------------------------------------------------------------- search index (case2md.py find)
# Case names and the parentheticals other courts wrote about each case, in SQLite full-text (FTS5)
# tables in their own file. A nickname like "Glomar" is in no case name, but courts write it in
# their parentheticals about Phillippi v. CIA, so both are searched.
SEARCH_DB = Path(os.environ.get("CL_SEARCH_DB") or DB.with_name("search.sqlite"))


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


def main():
    sys.stdout.reconfigure(line_buffering=True)      # so progress shows up at once in a log file (nohup, > file)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("names", nargs="*", choices=ORDER + [[]], help="default: everything downloaded")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, help="testing: stop after this many rows (table is not marked done)")
    sub.add_parser("status")
    sub.add_parser("index", help="(re)build the full-text index case2md.py find uses")
    p = sub.add_parser("case")
    p.add_argument("json")
    args = ap.parse_args()
    if args.cmd == "load":
        con = connect()
        lock = Path(str(DB) + ".loading")          # casemap.py refuses to read while this exists
        lock.write_text("casedb.py load, pid %d\n" % os.getpid())
        try:
            for name in (args.names or ORDER):
                load(name, con, args.force, args.limit)
        except KeyboardInterrupt:
            print("\ninterrupted: the table being loaded is incomplete and not marked done; rerun the same command to redo it.")
        finally:
            lock.unlink(missing_ok=True)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "index":
        if Path(str(DB) + ".loading").exists():
            sys.exit("casedb.py load is running; a long read now can make it fail with 'database is locked'. Index after it finishes.")
        build_search(sqlite3.connect("file:%s?mode=ro" % DB, uri=True))
    else:
        cmd_case(args)


if __name__ == "__main__":
    main()
