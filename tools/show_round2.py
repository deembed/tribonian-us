"""Round-2 evidence for one work item, compact (about half the round-1 size).

usage: python show_round2.py VIEW SHARD INDEX     (INDEX 1-based within build/cocite/rule-labels/round2/shards/shard-SHARD.txt)
       python show_round2.py VIEW --key KEY
"""
import json, sys
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "build" / "cocite" / "rule-labels"
view = sys.argv[1]
if sys.argv[2] == "--key":
    key = sys.argv[3]
else:
    keys = [k.strip() for k in (D / "round2" / "shards" / ("shard-%s.txt" % sys.argv[2])).read_text().splitlines() if k.strip()]
    key = keys[int(sys.argv[3]) - 1]
items = {i["key"]: i for i in json.loads((D / "round2" / ("items-%s.json" % view)).read_text())}
ev = None
for line in (D / ("round2-evidence-%s.jsonl" % view)).open(encoding="utf-8"):
    r = json.loads(line)
    if r["key"] == key:
        ev = r
        break
r1 = {}
for line in (D / "import.jsonl").open(encoding="utf-8"):
    x = json.loads(line)
    r1[x["key"]] = x


def c(x):
    return x.split(",")[0][:50] + " " + x.rsplit("(", 1)[-1].rstrip(")")


it = items[key]
KIND = {"narrowed": "EXISTING LABEL, lineage NARROWED by a split: rewrite it for the slices it now covers",
        "drifted-check": "EXISTING LABEL, lineage DRIFTED: check it against this evidence and correct what no longer fits",
        "split-born": "NEW LINEAGE, split off from a labelled lineage: write a new label",
        "new": "NEW LINEAGE, unlabelled: write a new label"}
print("KEY %s  level %s  lineage %s  span %s" % (key, ev["level"], ev["lineage"], ev["span"]))
print("TASK:", KIND[it["kind"]])
if ev.get("parent_cluster") and ev["parent_cluster"].get("label"):
    print("PARENT CLUSTER:", ev["parent_cluster"]["label"])
if key in r1:
    x = r1[key]
    print("ROUND-1 NAME:", x["name"])
    print("ROUND-1 DESCRIPTION:", x["description"])
    print("ROUND-1 HISTORY:", x["history"])
    print("ROUND-1 CHANGES:", "; ".join("%s %s (%s)" % (y["when"], y["case"], y["change"]) for y in x["changes"]))
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
