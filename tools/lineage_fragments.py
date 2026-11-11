"""How fragmented the lineages of a view are: units of a given size in single-slice lineages (excluding the
first and the partial last slice, where every lineage starts or ends), and median lineage length, by level
and by unit size.  usage: python lineage_fragments.py VIEW.sqlite [VIEW2.sqlite ...]"""
import sqlite3, sys, statistics, collections
for v in sys.argv[1:]:
    db = sqlite3.connect("file:%s?mode=ro" % v, uri=True)
    built = [r[0] for r in db.execute("SELECT slice FROM slices ORDER BY slice")]
    inner = set(built[1:-1])
    for lv, tbl in (("cluster", "clusters"), ("sub", "subs")):
        n = dict(db.execute("SELECT lineage, count(*) FROM %s GROUP BY lineage" % tbl))
        rows = db.execute("SELECT slice, size, lineage FROM %s" % tbl).fetchall()
        for lo, hi in ((100, 500), (500, 2000), (2000, 10 ** 9)):
            us = [(s, z, l) for s, z, l in rows if lo <= z < hi and s in inner and s >= 1970]
            if not us:
                continue
            single = sum(1 for s, z, l in us if n[l] == 1)
            med = statistics.median(n[l] for s, z, l in us)
            print("%-40s %-7s units %5d-%-6s %5d units since 1970: %4d in single-slice lineages (%4.1f%%), median lineage %4.1f slices" % (
                v.split("/")[-2] + "/" + v.split("/")[-1], lv, lo, hi if hi < 10 ** 9 else "+", len(us), single, 100.0 * single / len(us), med))
    print()
