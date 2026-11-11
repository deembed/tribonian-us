"""One label for the review pass: every field, then its lineage's current evidence (the view it is shown in).
usage: python show_review.py SHARD INDEX     (INDEX 1-based in build/cocite/rule-labels/review/shards/shard-SHARD.txt)"""
import json, sys
from pathlib import Path
W = Path(__file__).resolve().parent.parent
D = W / "build" / "cocite" / "rule-labels" / "review"
keys = [k.strip() for k in (D / "shards" / ("shard-%s.txt" % sys.argv[1])).read_text().splitlines() if k.strip()]
key = keys[int(sys.argv[2]) - 1]
lab = next(x for x in json.loads((W / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8")) if x["key"] == key)
ev = next((json.loads(l) for l in (D / "evidence.jsonl").open(encoding="utf-8") if json.loads(l)["key"] == key), None)
c = lambda x: x.split(",")[0][:50] + " " + x.rsplit("(", 1)[-1].rstrip(")")
print("KEY %s (%d of %d in shard %s)  level %s  shown in %s" % (key, int(sys.argv[2]), len(keys), sys.argv[1], lab["level"],
                                                           ev["view"] if ev else "?"))
for f in ("name", "description", "history", "rule"):
    print("%s: %s" % (f.upper(), lab.get(f, "")))
if lab.get("phases"):
    print("PHASES:", json.dumps(lab["phases"], ensure_ascii=False))
print("CHANGES:")
for x in lab.get("changes", []):
    print("  %s | %s | %s | %s | basis %s%s" % (x["when"], x["case"], x["change"], x["what"], x["basis"],
                                              " | outside the evidence" if x.get("outside_evidence") else ""))
if lab.get("distinct_from"):
    print("DISTINCT FROM:", "; ".join("%s: %s" % (d["key"], d["how"]) for d in lab["distinct_from"]))
if not ev:
    print("(no evidence: review the text only)"); sys.exit(0)
print("--- EVIDENCE: span", ev["span"])
print("HEAVIEST:", "; ".join(c(x) for x in ev["heaviest"][:12]))
for t in ev["timeline"]:
    print(" %s %s %s | %s%s" % (t["unit"], t["size"], c(t["anchor"]), "; ".join(c(x) for x in t["leading"][:5]),
                                ("  +in: " + "; ".join(c(x) for x in t["entered"][:4])) if t.get("entered") else ""))
print("HOLDINGS (parentheticals; some are known to be attached to the wrong case):")
for case, ps in list(ev["holdings"].items())[:18]:
    print("  %s | %s" % (c(case), ps[0][:180]))
print("CHANGE PARENTHETICALS:")
for case, ps in list(ev["change_parentheticals"].items())[:10]:
    print("  %s | %s" % (c(case), ps[0][:180]))
