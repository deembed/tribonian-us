"""Round-3 (appellate view) evidence for one work item, compact.
usage: python show_round3.py SHARD INDEX     (INDEX 1-based in build/cocite/rule-labels/round3/shards/shard-SHARD.txt)"""
import json, sys
from pathlib import Path
W = Path(__file__).resolve().parent.parent
D = W / "build" / "cocite" / "rule-labels" / "round3"
keys = [k.strip() for k in (D / "shards" / ("shard-%s.txt" % sys.argv[1])).read_text().splitlines() if k.strip()]
key = keys[int(sys.argv[2]) - 1]
it = {i["key"]: i for i in json.loads((D / "items.json").read_text())}[key]
ev = next(json.loads(l) for l in (D / "evidence.jsonl").open(encoding="utf-8") if json.loads(l)["lineage"] == it["lineage"])
labs = {x["key"]: x for x in json.loads((W / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8"))}
c = lambda x: x.split(",")[0][:50] + " " + x.rsplit("(", 1)[-1].rstrip(")")
TASK = {"new": "NEW LINEAGE in the appellate view, unlabelled: write a label for it",
        "drifted": "A shared label (below) attaches here only loosely and does not fit: write an appellate-only label for this lineage",
        "narrowed": "A shared label (below) describes slices this lineage no longer covers: write an appellate-only label for this lineage"}
print("KEY %s  level %s  appellate-view lineage %s  span %s" % (key, ev["level"], ev["lineage"], ev["span"]))
print("TASK:", TASK[it["kind"]])
if ev.get("parent_cluster") and ev["parent_cluster"].get("label"):
    print("PARENT CLUSTER:", ev["parent_cluster"]["label"])
if it.get("shared_key"):
    x = labs[it["shared_key"]]
    print("SHARED LABEL NAME:", x["name"]); print("SHARED LABEL DESCRIPTION:", x["description"]); print("SHARED LABEL HISTORY:", x.get("history", ""))
print("HEAVIEST:", "; ".join(c(x) for x in ev["heaviest"][:12]))
print("TIMELINE (slice size anchor | leading | +entered | merged from / split to):")
for t in ev["timeline"]:
    print(" %s %s %s | %s%s" % (t["unit"], t["size"], c(t["anchor"]), "; ".join(c(x) for x in t["leading"][:5]),
                                ("  +in: " + "; ".join(c(x) for x in t["entered"][:4])) if t.get("entered") else ""))
    for k in ("merged_in_from", "split_off_to"):
        if t.get(k):
            print("    %s: %s" % (k, "; ".join("%s %s" % (y["unit"], y["label"] or "") for y in t[k][:3])))
print("HOLDINGS:")
for case, ps in list(ev["holdings"].items())[:20]:
    print("  %s | %s" % (c(case), ps[0][:200]))
print("CHANGE PARENTHETICALS:")
for case, ps in list(ev["change_parentheticals"].items())[:12]:
    print("  %s | %s" % (c(case), ps[0][:200]))
