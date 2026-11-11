"""One differentiation group (round 2): each label's current text, span, anchors, and the heaviest cases only it holds.
usage: python show_diff_group2.py SHARD INDEX   (INDEX 1-based over the groups listed in round2/diff/shards/groups-SHARD.txt)"""
import json, sys
from pathlib import Path
D = Path(__file__).resolve().parent.parent / "build" / "cocite" / "rule-labels"
gnos = [int(x) for x in (D / "round2" / "diff" / "shards" / ("groups-%s.txt" % sys.argv[1])).read_text().split()]
gno = gnos[int(sys.argv[2]) - 1]
g = next(x for x in json.loads((D / "round2" / "diff-groups.json").read_text()) if x["group"] == gno)
labs = {json.loads(l)["key"]: json.loads(l) for l in (D / "import-round2.jsonl").open(encoding="utf-8")}
# Evidence for a label: round-2 labels by their lineage in the note view (lineage ids are per view, so never mix
# views); other labels by key, the note-view file first (fixed 2026-11-10: a lookup by lineage alone across both
# views could return another view's lineage with the same number)
NOTE = "appellate-sentence-m0.2"
by_lin, by_key = {}, {}
for line in (D / ("round2-evidence-%s.jsonl" % NOTE)).open(encoding="utf-8"):
    r = json.loads(line)
    by_lin[r["lineage"]] = r
for f in ("rule-evidence-%s.jsonl" % NOTE, "rule-evidence-appellate.jsonl"):
    for line in (D / f).open(encoding="utf-8"):
        r = json.loads(line)
        by_key.setdefault(r["key"], r)
c = lambda x: x.split(",")[0][:45] + " " + x.rsplit("(", 1)[-1].rstrip(")")


def evid(k):
    x = labs[k]
    if x.get("round") == 2 or x.get("round2_kind"):
        return by_lin.get(x.get("evidence_lineage"))
    return by_key.get(k)


heavy = {k: [c(y) for y in (evid(k) or {}).get("heaviest", [])[:20]] for k in g["keys"]}
print("GROUP %d (%d of %d in your shard): %d labels" % (gno, int(sys.argv[2]), len(gnos), len(g["keys"])))
for a, b, j, nj, sib in g["pairs"]:
    print("  close: %s ~ %s  case overlap %.2f  name overlap %.2f%s" % (a, b, j, nj, "  siblings" if sib else ""))
for k in g["keys"]:
    x, e = labs[k], evid(k) or {}
    others = set().union(*(set(heavy[o]) for o in g["keys"] if o != k))
    print("=" * 80)
    print("KEY %s  level %s  span %s" % (k, x["level"], e.get("span", "?")))
    print("NAME:", x["name"]); print("DESCRIPTION:", x["description"]); print("HISTORY:", x["history"])
    if x.get("phases"): print("PHASES:", x["phases"])
    print("CHANGES:", "; ".join("%s %s (%s)" % (y["when"], y["case"], y["change"]) for y in x["changes"]))
    if e: print("ANCHORS:", "; ".join("%s %s" % (t["slice"], c(t["anchor"])) for t in e["timeline"]))
    print("HEAVIEST ONLY HERE:", "; ".join(h for h in heavy[k] if h not in others) or "none")
    print("HEAVIEST SHARED:", "; ".join(h for h in heavy[k] if h in others) or "none")
