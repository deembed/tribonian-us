"""Calibrate a core-continuity rule for co-citation lineages, in memory (reads a view, writes nothing to it).

Today a unit in slice s continues the lineage of a unit in slice s-5 when they are each other's best match
by whole-membership Jaccard >= CARRY (0.2). A large cluster can pass that on its periphery while its core has
moved to another doctrine. Candidate rules add a core test on top:

  retain:K:x   at least x of the earlier unit's top-K cases are still members of the later unit
  corej:K:x    the two units' top-K cores have Jaccard >= x

For each rule: lineages, how many of today's lineages it splits, the remaining core-break rate (top-15 Jaccard
< 0.1 between adjacent slices of one lineage), and two reference sets from the labels:
  mixed   lineages a rewrite agent reported as drifting between doctrines (should split)
  stable  long labelled lineages with no core break today (should mostly not split)

usage: python lineage_calibrate.py VIEW.sqlite OUT.tsv [rule ...]   (default: a grid)
"""
import collections, json, sqlite3, sys, time
from pathlib import Path

CARRY, WIDTH, TOPK_BREAK, BREAK = 0.2, 5, 15, 0.1
MIXED = """sub-1b7697697d cluster-48a1459dfb sub-66b34259ef sub-89d927312f cluster-3750c97e1b cluster-3a26aeb9eb
cluster-d4ab733260 sub-f28ed8605a cluster-991bf23317 cluster-bc0d955297 sub-dee1b1d80f sub-6d6822e7b0 sub-74ff8a34ab
sub-826f4ac2b3 cluster-632d1c1ccf sub-4c7a94687f sub-8a89fa1a78 sub-7d942f3585 cluster-49dfa61fb4 sub-56828eb39e
sub-544248333d sub-98f4b5e2e6 cluster-1c257d3a52 sub-1b7697697d sub-da978d68c4 sub-27199c045f sub-ec23b44cf4
sub-7cffc04ac4 sub-d9b2b08101 sub-473fa3d4ea sub-f40bfc9624 sub-71e769c33c sub-13e69aa18e""".split()

view, out_path = Path(sys.argv[1]), Path(sys.argv[2])
rules = sys.argv[3:] or ["none"] + ["retain:%d:%s" % (k, x) for k in (10, 15, 20, 30) for x in ("0.2", "0.3", "0.4", "0.5")] + \
    ["corej:%d:%s" % (k, x) for k in (15, 30) for x in ("0.05", "0.1", "0.15")]
t0 = time.time()
db = sqlite3.connect("file:%s?mode=ro" % view, uri=True)
built = [r[0] for r in db.execute("SELECT slice FROM slices ORDER BY slice")]

# per level: unit -> members (set) and ranked core list; ref -> unit per slice
LEV = {"cluster": ("cl", "weight"), "sub": ("sub", "subw")}
mem = {lv: collections.defaultdict(set) for lv in LEV}
rank = {lv: collections.defaultdict(list) for lv in LEV}
where = {lv: collections.defaultdict(dict) for lv in LEV}
for s, ref, cl, w, sub, sw in db.execute("SELECT slice, ref, cl, weight, sub, subw FROM nodes"):
    if cl and cl > 0:
        mem["cluster"][(s, cl)].add(ref)
        rank["cluster"][(s, cl)].append((w or 0, ref))
        where["cluster"][s][ref] = cl
    if sub and sub > 0:
        mem["sub"][(s, sub)].add(ref)
        rank["sub"][(s, sub)].append((sw or 0, ref))
        where["sub"][s][ref] = sub
import os
if os.environ.get("CLUSTER_RANK", "members") == "members":      # within-cluster weight, as the label fingerprints use
    rank["cluster"] = collections.defaultdict(list)
    for s, cl, ref, w in db.execute("SELECT slice, cl, ref, weight FROM members WHERE home = 1"):
        rank["cluster"][(s, cl)].append((w or 0, ref))
for lv in LEV:
    for u, xs in rank[lv].items():
        xs.sort(reverse=True)
        rank[lv][u] = [r for _, r in xs]
units = {lv: collections.defaultdict(list) for lv in LEV}
for lv, (col, _) in LEV.items():
    for (s, k) in mem[lv]:
        units[lv][s].append(k)
print("loaded %s: %d slices  (%.1f min)" % (view.name, len(built), (time.time() - t0) / 60), flush=True)

# candidate links: mutual best match by membership similarity >= threshold, as build_lineage does.
# LINK=jaccard:0.2 (today) or LINK=overlap:x, the overlap coefficient |A&B| / min(|A|,|B|) (Szymkiewicz-Simpson)
import os
_lk = os.environ.get("LINK", "jaccard:%s" % CARRY).split(":")
LINK_METRIC, LINK_MIN = _lk[0], float(_lk[1])
JFLOOR = float(_lk[2]) if len(_lk) > 2 else 0.0      # hybrid:x:floor  mutual best by Jaccard, accepted when the
                                                     # overlap coefficient >= x and the Jaccard >= floor
print("link: %s >= %s" % (LINK_METRIC, LINK_MIN), flush=True)


def sim(m, a, b, metric):
    return m / (a + b - m) if metric == "jaccard" else m / max(1, min(a, b))


links = {lv: {} for lv in LEV}                       # (s, ks) -> (t, kt)
for lv in LEV:
    for idx, s in enumerate(built):
        t = built[idx - 1] if idx and built[idx - 1] == s - WIDTH else None
        if t is None:
            continue
        here, before = where[lv][s], where[lv][t]
        size_t, size_s, shared = collections.Counter(), collections.Counter(), collections.Counter()
        for ref in here.keys() & before.keys():
            kt, ks = before[ref], here[ref]
            size_t[kt] += 1
            size_s[ks] += 1
            shared[(kt, ks)] += 1
        best_f, best_b, ov = {}, {}, {}
        for (kt, ks), m in shared.items():
            j = sim(m, size_t[kt], size_s[ks], "jaccard" if LINK_METRIC == "hybrid" else LINK_METRIC)
            ov[(kt, ks)] = (sim(m, size_t[kt], size_s[ks], "overlap"), j)
            if j > best_f.get(kt, (None, -1))[1]:
                best_f[kt] = (ks, j)
            if j > best_b.get(ks, (None, -1))[1]:
                best_b[ks] = (kt, j)
        for ks, (kt, j) in best_b.items():
            ok = (ov[(kt, ks)][0] >= LINK_MIN and j >= JFLOOR) if LINK_METRIC == "hybrid" else j >= LINK_MIN
            if ok and best_f[kt][0] == ks:
                links[lv][(s, ks)] = (t, kt)


def core_j(lv, a, b, K):
    sa, sb = set(rank[lv][a][:K]), set(rank[lv][b][:K])
    return len(sa & sb) / max(1, len(sa | sb))


def core_ok(lv, t, kt, s, ks, rule, back=None):
    """back: the lineage's unit two slices before s (for corej2), or None. rule may be per level:
    'cluster=corej:15:0.05,sub=corej:15:0.1'."""
    if "=" in rule:
        rule = dict(x.split("=") for x in rule.split(","))[lv]
    if rule == "none":
        return True
    if rule.startswith("corej2:"):
        _, K, x, y = rule.split(":")
        K, x, y = int(K), float(x), float(y)
        if core_j(lv, (t, kt), (s, ks), K) < x:
            return False
        return back is None or core_j(lv, back, (s, ks), K) >= y
    kind, K, x = rule.split(":")
    K, x = int(K), float(x)
    a, b = rank[lv][(t, kt)][:K], rank[lv][(s, ks)][:K]
    if kind == "retain":
        return sum(1 for r in a if r in mem[lv][(s, ks)]) / max(1, len(a)) >= x
    sa, sb = set(a), set(b)
    if kind == "coreo":                               # overlap coefficient of the cores
        return len(sa & sb) / max(1, min(len(sa), len(sb))) >= x
    return len(sa & sb) / max(1, len(sa | sb)) >= x


def lineages(rule):
    lin = {lv: {} for lv in LEV}
    prev = {lv: {} for lv in LEV}                    # unit -> the unit before it in its lineage
    nxt = 0
    for lv in LEV:
        for s in built:
            for ks in sorted(units[lv][s]):
                lk = links[lv].get((s, ks))
                back = prev[lv].get(lk) if lk else None
                if back is not None and back[0] != s - 2 * WIDTH:
                    back = None
                if lk and lk in lin[lv] and core_ok(lv, lk[0], lk[1], s, ks, rule, back):
                    lin[lv][(s, ks)] = lin[lv][lk]
                    prev[lv][(s, ks)] = lk
                else:
                    nxt += 1
                    lin[lv][(s, ks)] = nxt
    return lin


def breaks(lv, us):
    out = []
    for (t, kt), (s, ks) in zip(us, us[1:]):
        if s - t == WIDTH:
            a, b = set(rank[lv][(t, kt)][:TOPK_BREAK]), set(rank[lv][(s, ks)][:TOPK_BREAK])
            if len(a & b) / max(1, len(a | b)) < BREAK:
                out.append(s)
    return out


# reference sets: today's lineages (rule none) holding each labelled lineage
base = lineages("none")
label_lin = dict(db.execute("SELECT key, lineage FROM label_map JOIN lineages USING(lineage)").fetchall()) \
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='label_map'").fetchone() else {}
lin_units = {}
for lv, (col, _) in LEV.items():
    tbl = "subs" if lv == "sub" else "clusters"
    for l, s, k in db.execute("SELECT lineage, slice, %s FROM %s ORDER BY slice" % (col, tbl)):
        lin_units.setdefault(l, (lv, []))[1].append((s, k))
mixed = {k: lin_units[l] for k, l in label_lin.items() if k in MIXED and l in lin_units}
stable = {k: lin_units[l] for k, l in label_lin.items() if k not in MIXED and l in lin_units
          and len(lin_units[l][1]) >= 6 and not breaks(*lin_units[l])}
print("reference: %d mixed lineages attached here (of %d listed), %d stable long lineages" % (
    len(mixed), len(set(MIXED)), len(stable)), flush=True)

rows = []
for rule in rules:
    lin = lineages(rule)
    n_lin, n_multi, n_brk, split_base = 0, 0, 0, 0
    for lv in LEV:
        by = collections.defaultdict(list)
        for u, l in lin[lv].items():
            by[l].append(u)
        for l, us in by.items():
            us.sort()
            n_lin += 1
            if len(us) > 1 and max(len(mem[lv][u]) for u in us) >= 30:
                n_multi += 1
                n_brk += bool(breaks(lv, us))
        bb = collections.defaultdict(set)
        for u, l in base[lv].items():
            bb[l].add(lin[lv][u])
        split_base += sum(1 for v in bb.values() if len(v) > 1)

    def pieces(ref):
        out = {}
        for k, (lv, us) in ref.items():
            out[k] = len({lin[lv][u] for u in us if u in lin[lv]})
        return out
    pm, ps = pieces(mixed), pieces(stable)
    inner = set(built[1:-1])
    for lv in LEV:
        nl = collections.Counter(lin[lv].values())
        us = [u for u in lin[lv] if u[0] in inner and u[0] >= 1970 and len(mem[lv][u]) >= 100]
        single = sum(1 for u in us if nl[lin[lv][u]] == 1)
        ms = sum(1 for k, v in pm.items() if v > 1 and mixed[k][0] == lv), sum(1 for k in pm if mixed[k][0] == lv)
        ss = sum(1 for k, v in ps.items() if v > 1 and stable[k][0] == lv), sum(1 for k in ps if stable[k][0] == lv)
        print("    %-7s single-slice units(100+, since 1970) %5.1f%%   mixed split %2d/%2d   stable split %3d/%3d" % (
            lv, 100.0 * single / max(1, len(us)), ms[0], ms[1], ss[0], ss[1]), flush=True)
    rows.append((rule, n_lin, split_base, n_multi, n_brk, 100.0 * n_brk / max(1, n_multi),
                 sum(1 for v in pm.values() if v > 1), len(pm), sum(1 for v in ps.values() if v > 1), len(ps)))
    print("%-14s lineages %6d  split %5d  multi30 %5d  breaks %4d (%4.1f%%)  mixed split %2d/%2d  stable split %3d/%3d" % rows[-1],
          flush=True)

with out_path.open("w") as f:
    f.write("rule\tlineages\tsplit_vs_today\tmulti_slice_30plus\twith_break\tbreak_pct\tmixed_split\tmixed_n\tstable_split\tstable_n\n")
    for r in rows:
        f.write("\t".join(str(round(x, 2)) if isinstance(x, float) else str(x) for x in r) + "\n")
print("wrote %s  (%.1f min)" % (out_path, (time.time() - t0) / 60))
