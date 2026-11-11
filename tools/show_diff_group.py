"""Print one differentiation group: every label in it, rewritten (rule-centred pass), side by side with what sets
its lineage apart from the others (cases only it holds among its heaviest, its anchors by slice, its view).

usage: python show_diff_group.py WORK_DIR GROUP_NUMBER
"""
import json, sys
from pathlib import Path

work = Path(sys.argv[1])
gno = int(sys.argv[2])
D = work / "build" / "cocite" / "rule-labels"
groups = {g["group"]: g for g in json.loads((D / "diff-groups.json").read_text())}
g = groups[gno]
labels = {x["key"]: x for x in json.loads((work / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8"))}
ev = {}
for f in D.glob("rule-evidence-*.jsonl"):
    for line in f.open(encoding="utf-8"):
        r = json.loads(line)
        ev[r["key"]] = r
new = {}
for f in sorted((D / "out").glob("*.jsonl")):
    for line in f.open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            new[r["key"]] = r
all_pairs = []
for other in groups.values():
    all_pairs += other["pairs"]


def c(x):
    return x.split(",")[0][:50] + " " + x.rsplit("(", 1)[-1].rstrip(")")


heavy = {k: [c(x) for x in ev[k]["heaviest"][:20]] for k in g["keys"]}
print("GROUP %d: %d labels" % (gno, len(g["keys"])))
print("Close pairs inside the group (fingerprint overlap, old-name word overlap, siblings under one parent):")
for a, b, j, nj, sib in g["pairs"]:
    print("  %s ~ %s  overlap %.2f  name %.2f%s" % (a, b, j, nj, "  siblings" if sib else ""))
for k in g["keys"]:
    r, e = new.get(k), ev[k]
    others = set().union(*(set(heavy[o]) for o in g["keys"] if o != k))
    print("=" * 90)
    print("KEY %s  level %s  view %s  span %s" % (k, e["level"], e["view"], e["span"]))
    if e.get("parent_cluster"):
        print("PARENT CLUSTER:", e["parent_cluster"].get("label"))
    print("OLD NAME:", labels[k]["name"])
    if r:
        print("NEW NAME:", r["name"])
        print("NEW DESCRIPTION:", r["description"])
        print("NEW HISTORY:", r["history"])
        print("RULE:", r["rule"])
        print("CHANGES:", "; ".join("%s %s (%s, %s)" % (x["when"], x["case"], x["change"], x["basis"]) for x in r["changes"]))
    else:
        print("(no rule-centred rewrite yet)  OLD DESCRIPTION:", labels[k]["description"])
    print("ANCHORS BY SLICE:", "; ".join("%s %s (%s)" % (t["slice"], c(t["anchor"]), t["size"]) for t in e["timeline"]))
    print("HEAVIEST ONLY HERE (within group):", "; ".join(x for x in heavy[k] if x not in others) or "none")
    print("HEAVIEST SHARED:", "; ".join(x for x in heavy[k] if x in others) or "none")
    outside = [(b if a == k else a) for a, b, *_ in all_pairs if k in (a, b) and (b if a == k else a) not in g["keys"]]
    if outside:
        print("ALSO CLOSE, OUTSIDE THIS GROUP (context only, do not edit):",
              "; ".join("%s %s" % (o, (new.get(o) or labels[o])["name"]) for o in outside))
