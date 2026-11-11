"""Core turnover of co-citation lineages: how often a lineage's heaviest cases are replaced between adjacent
slices (a "break" = top-K Jaccard below BREAK). A lineage whose core turns over has drifted into another doctrine.

usage: python lineage_turnover.py VIEW.sqlite [VIEW2.sqlite ...] [--k 15] [--break 0.1] [--min-size 30]
"""
import sqlite3, sys, collections

args = sys.argv[1:]
K = int(args[args.index("--k") + 1]) if "--k" in args else 15
BREAK = float(args[args.index("--break") + 1]) if "--break" in args else 0.1
MIN = int(args[args.index("--min-size") + 1]) if "--min-size" in args else 30
views = [a for a in args if a.endswith(".sqlite")]


def measure(path):
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    level = dict(db.execute("SELECT lineage, level FROM lineages"))
    try:
        labelled = {r[0] for r in db.execute("SELECT lineage FROM label_map")}
    except sqlite3.OperationalError:
        labelled = set()
    core = collections.defaultdict(list)          # (level, slice, unit) -> top-K refs
    for s, k, ref in db.execute("SELECT slice, sub, ref FROM (SELECT slice, sub, ref, row_number() OVER "
                                "(PARTITION BY slice, sub ORDER BY subw DESC) rn FROM nodes WHERE sub > 0) WHERE rn <= ?", (K,)):
        core[("sub", s, k)].append(ref)
    for s, k, ref in db.execute("SELECT slice, cl, ref FROM (SELECT slice, cl, ref, row_number() OVER "
                                "(PARTITION BY slice, cl ORDER BY weight DESC) rn FROM members WHERE home = 1) WHERE rn <= ?", (K,)):
        core[("cluster", s, k)].append(ref)
    units = collections.defaultdict(list)
    for lv, tbl, col in (("sub", "subs", "sub"), ("cluster", "clusters", "cl")):
        for lin, s, k, size in db.execute("SELECT lineage, slice, %s, size FROM %s ORDER BY slice" % (col, tbl)):
            units[lin].append((s, k, size, lv))
    out = {}
    for lin, us in units.items():
        if len(us) < 2 or max(u[2] for u in us) < MIN:
            continue
        cs = [set(core[(u[3], u[0], u[1])]) for u in us]
        br = [us[i][0] for i in range(1, len(us)) if us[i][0] - us[i - 1][0] == 5 and
              len(cs[i] & cs[i - 1]) / max(1, len(cs[i] | cs[i - 1])) < BREAK]
        out[lin] = (level[lin], len(us), br, lin in labelled)
    return out


for v in views:
    m = measure(v)
    for scope, keep in (("all lineages of %d+ cases" % MIN, lambda x: True), ("labelled lineages", lambda x: x[3])):
        rows = [x for x in m.values() if keep(x)]
        if not rows:
            continue
        nb = sum(1 for x in rows if x[2])
        per = collections.Counter(x[0] for x in rows if x[2])
        tot = collections.Counter(x[0] for x in rows)
        print("%-45s %-28s %5d multi-slice, %4d with a core break (%.0f%%)  [cluster %d/%d, sub %d/%d]" % (
            v.split("/")[-1], scope, len(rows), nb, 100.0 * nb / len(rows), per["cluster"], tot["cluster"], per["sub"], tot["sub"]))
