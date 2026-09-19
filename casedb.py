#!/usr/bin/env python3
"""casedb.py -- load CourtListener's quarterly bulk files into one local SQLite database.

    ./casedb.py load                  # every bulk file found, smallest first; finished tables are skipped
    ./casedb.py load citation-map     # just one (names: courts citations citation-map parentheticals
                                      #            opinion-clusters dockets opinions)
    ./casedb.py load --force courts   # reload a table that is already done
    ./casedb.py status
    ./casedb.py case cases/463-u-s-29_*.json    # smoke test: who cites this case, how deeply, and for what

Where things live (both default to ./bulk next to this script):
    CL_BULK = folder holding the *.csv.bz2 downloads
    CL_DB   = the database file (default $CL_BULK/courtlistener.sqlite)

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
    "parentheticals": ("parens", "id INTEGER PRIMARY KEY, described INTEGER, describing INTEGER, score REAL, group_id INTEGER, text TEXT",
                       lambda r: (integer(r["id"]), integer(r["described_opinion_id"]), integer(r["describing_opinion_id"]),
                                  float(r["score"] or 0), integer(r["group_id"]), r["text"]),
                       ["CREATE INDEX parens_by_described ON parens(described)"]),
    "opinion-clusters": ("clusters", "id INTEGER PRIMARY KEY, case_name TEXT, case_name_short TEXT, case_name_full TEXT, "
                         "date_filed TEXT, citation_count INTEGER, docket_id INTEGER, slug TEXT, judges TEXT, "
                         "precedential_status TEXT, scdb_id TEXT, other_dates TEXT",
                         lambda r: (integer(r["id"]), r["case_name"], r["case_name_short"], r["case_name_full"], r["date_filed"],
                                    integer(r["citation_count"]), integer(r["docket_id"]), r["slug"], r["judges"],
                                    r["precedential_status"], r["scdb_id"], r["other_dates"]), []),
    "dockets": ("dockets", "id INTEGER PRIMARY KEY, court_id TEXT, docket_number TEXT",
                lambda r: (integer(r["id"]), r["court_id"], r.get("docket_number")), []),
    "opinions": ("opinions", "id INTEGER PRIMARY KEY, cluster_id INTEGER, type TEXT, author_str TEXT, field TEXT, text BLOB",
                 keep_opinion, ["CREATE INDEX opinions_by_cluster ON opinions(cluster_id)"]),
}
ORDER = ["courts", "citations", "parentheticals", "citation-map", "opinion-clusters", "dockets", "opinions"]


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript("PRAGMA journal_mode=MEMORY; PRAGMA synchronous=OFF; PRAGMA cache_size=-1000000; PRAGMA temp_store=MEMORY;"
                      "CREATE TABLE IF NOT EXISTS loaded(name TEXT PRIMARY KEY, file TEXT, rows INTEGER, seconds REAL, finished TEXT);")
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
    con.execute("DROP TABLE IF EXISTS %s" % table)
    con.execute("CREATE TABLE %s (%s)" % (table, body))
    ncol = body.count(",") + 1 - body.count("PRIMARY KEY,") * 0
    ncol = len([c for c in re.split(r",\s*(?![^()]*\))", body)])
    sql = "INSERT OR REPLACE INTO %s VALUES (%s)" % (table, ",".join("?" * ncol))
    t0, n, bad, batch = time.time(), 0, 0, []
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
            if n % 1000000 == 0:
                con.commit()
                print("  %s rows, %.0f rows/s" % (format(n, ","), n / (time.time() - t0)), flush=True)
        if limit and n >= limit:
            break
    con.executemany(sql, batch)
    con.commit()
    for stmt in after:
        print("  " + stmt.split(" ON ")[0].lower())
        con.execute(stmt)
    if not limit:
        con.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?,?,datetime('now'))", (name, path.name, n, time.time() - t0))
    con.commit()
    print("  done: %s rows in %.1f min%s" % (format(n, ","), (time.time() - t0) / 60,
                                            ", %d unreadable rows skipped" % bad if bad else ""))


def have(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def cmd_status(args):
    print("bulk files: %s\ndatabase:   %s%s" % (BULK, DB, "  (%.1f GB)" % (DB.stat().st_size / 1e9) if DB.exists() else "  (not created yet)"))
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
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("load")
    p.add_argument("names", nargs="*", choices=ORDER + [[]], help="default: everything downloaded")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, help="testing: stop after this many rows (table is not marked done)")
    sub.add_parser("status")
    p = sub.add_parser("case")
    p.add_argument("json")
    args = ap.parse_args()
    if args.cmd == "load":
        con = connect()
        for name in (args.names or ORDER):
            load(name, con, args.force, args.limit)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        cmd_case(args)


if __name__ == "__main__":
    main()
