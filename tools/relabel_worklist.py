"""Which labels and lineages need (re)writing after a rebuild, with no language model.

For one view:
  narrowed   labels whose lineage now spans fewer slices than the rule-history evidence they were written from
             (the lineage split; the label may describe a phase now in another lineage)
  drifted    labels attached with fingerprint overlap below 0.7 (cocite labels check: drifted)
  lost       labels no longer attached here (displaced or orphaned) that were attached when written
  new        unlabelled lineages with a unit of MIN+ cases, largest first, marking those born from a split
             of a labelled lineage (their predecessor unit belonged to a labelled lineage)

usage: python relabel_worklist.py VIEW.sqlite OUT.json [--min 100]
"""
import json, sqlite3, sys, collections
from pathlib import Path

view, out = Path(sys.argv[1]), Path(sys.argv[2])
MIN = int(sys.argv[sys.argv.index("--min") + 1]) if "--min" in sys.argv else 100
WORK = Path(__file__).resolve().parent.parent
D = WORK / "build" / "cocite" / "rule-labels"
db = sqlite3.connect("file:%s?mode=ro" % view, uri=True)
labels = {x["key"]: x for x in json.loads((WORK / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8"))}
ev = {}
for f in D.glob("rule-evidence-*.jsonl"):
    for line in f.open(encoding="utf-8"):
        r = json.loads(line)
        ev[r["key"]] = r
vname = view.stem


def slab(s):
    return "%d-%02d" % (s, (s + 4) % 100)


spans = {}
for tbl in ("subs", "clusters"):
    for lin, a, b, n, mx in db.execute("SELECT lineage, min(slice), max(slice), count(*), max(size) FROM %s GROUP BY lineage" % tbl):
        spans[lin] = (a, b, n, mx)
level = dict(db.execute("SELECT lineage, level FROM lineages"))
status = {k: (s, l, j) for k, s, l, j in db.execute("SELECT key, status, lineage, jaccard FROM label_status")}
lmap = dict(db.execute("SELECT lineage, key FROM label_map"))
key_lin = {k: l for l, k in lmap.items()}

narrowed, drifted, lost = [], [], []
for k, e in ev.items():
    st = status.get(k, ("none", None, 0))
    if e["view"] != vname and k not in key_lin:
        continue                                   # written from the other view and not attached here
    if k not in key_lin:
        lost.append({"key": k, "name": labels[k]["name"], "status": st[0], "overlap": round(st[2] or 0, 2)})
        continue
    lin = key_lin[k]
    a, b, n, mx = spans[lin]
    was = [t["slice"] for t in e["timeline"]]
    if e["view"] == vname and n < len(was):
        narrowed.append({"key": k, "name": labels[k]["name"], "lineage": lin, "was": "%s to %s (%d slices)" % (was[0], was[-1], len(was)),
                         "now": "%s to %s (%d slices)" % (slab(a), slab(b), n)})
    if st[0] == "drifted":
        drifted.append({"key": k, "name": labels[k]["name"], "lineage": lin, "overlap": round(st[2], 2)})

# unlabelled big lineages; born from a split if a links row ties their first unit to a labelled lineage's unit
unit_lin = {}
for tbl, col in (("subs", "sub"), ("clusters", "cl")):
    for s, k, l in db.execute("SELECT slice, %s, lineage FROM %s" % (col, tbl)):
        unit_lin[(tbl, s, k)] = l
first = {}
for tbl, col in (("subs", "sub"), ("clusters", "cl")):
    for l, s, k in db.execute("SELECT lineage, slice, %s FROM %s ORDER BY slice DESC" % (col, tbl)):
        first[l] = (tbl, s, k)
parent = collections.defaultdict(list)
for lv, sf, kf, st_, kt, m, j in db.execute("SELECT level, slice_from, cl_from, slice_to, cl_to, shared, jaccard FROM links"):
    tbl = "subs" if lv == "sub" else "clusters"
    child = unit_lin.get((tbl, st_, kt))
    if child is not None and first.get(child) == (tbl, st_, kt):
        p = unit_lin.get((tbl, sf, kf))
        if p in lmap and p != child:
            parent[child].append((j, labels[lmap[p]]["name"], p))
new = []
for lin, (a, b, n, mx) in spans.items():
    if lin in lmap or mx < MIN or level.get(lin) not in ("sub", "cluster"):
        continue
    par = max(parent.get(lin, []), default=None)
    new.append({"lineage": lin, "level": level[lin], "span": "%s to %s" % (slab(a), slab(b)), "slices": n, "max_size": mx,
                "split_from": {"lineage": par[2], "label": par[1], "jaccard": round(par[0], 2)} if par else None})
new.sort(key=lambda x: (-(x["split_from"] is not None), -x["max_size"]))

res = {"view": vname, "narrowed": narrowed, "drifted": drifted, "lost": lost, "new": new}
out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
print("%s: %d narrowed by a split, %d drifted, %d no longer attached; %d unlabelled lineages of %d+ cases "
      "(%d born from a split of a labelled lineage)" % (vname, len(narrowed), len(drifted), len(lost), len(new), MIN,
                                                        sum(1 for x in new if x["split_from"])))
