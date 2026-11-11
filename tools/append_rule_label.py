"""Append one rule-centred label rewrite to a shard's output file, after checking it.

usage: python append_rule_label.py SHARD JSON_FILE [--replace]  (or - for stdin; --replace corrects a saved label)
       python append_rule_label.py SHARD --next          (print the next shard index and key still to do)

The label's key must be in build/cocite/rule-labels/shards/shard-SHARD.txt, and it must not already be written.
The output goes to build/cocite/rule-labels/out/shard-SHARD.jsonl.
"""
import json, sys
from pathlib import Path

D = Path(__file__).resolve().parent.parent / "build" / "cocite" / "rule-labels"
arg = sys.argv[1]
if arg.startswith("r2-"):                       # round 2: build/cocite/rule-labels/round2/
    D = D / "round2"
    arg = arg[3:]
elif arg.startswith("v-"):                      # review pass: build/cocite/rule-labels/review/
    D = D / "review"
    arg = arg[2:]
elif arg.startswith("r3-"):                     # round 3, appellate view: round3/
    D = D / "round3"
    arg = arg[3:]
elif arg.startswith("d-"):                      # differentiation pass on round 2: round2/diff/
    D = D / "round2" / "diff"
    arg = arg[2:]
shard = int(arg)
keys = [k.strip() for k in (D / "shards" / ("shard-%d.txt" % shard)).read_text().splitlines() if k.strip()]
out = D / "out" / ("shard-%d.jsonl" % shard)
done = set()
if out.exists():
    done = {json.loads(l)["key"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}

if sys.argv[2] == "--next":
    for i, k in enumerate(keys, 1):
        if k not in done:
            print("next: index %d of %d, key %s (%d done)" % (i, len(keys), k, len(done)))
            break
    else:
        print("shard %d complete: all %d keys written" % (shard, len(keys)))
    sys.exit(0)

text = sys.stdin.read() if sys.argv[2] == "-" else Path(sys.argv[2]).read_text(encoding="utf-8")
o = json.loads(text)
err = []
REVIEW = sys.argv[1].startswith("v-")
if REVIEW and o.get("verdict") == "ok" and set(o) <= {"key", "verdict", "review_note"}:
    # a review that needs no change: just the verdict
    if o.get("key") not in keys:
        print("REJECTED, nothing written:\n  key %s is not in shard %d" % (o.get("key"), shard)); sys.exit(1)
    if o["key"] in done and "--replace" not in sys.argv:
        print("REJECTED, nothing written:\n  key %s is already written (add --replace)" % o["key"]); sys.exit(1)
    o = {k: o[k] for k in ("key", "verdict", "review_note") if k in o}
    mode = "replace" if o["key"] in done else "append"
    if mode == "append":
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    else:
        lines = [l for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
        out.write_text("\n".join(json.dumps(o, ensure_ascii=False) if json.loads(l)["key"] == o["key"] else l for l in lines) + "\n", encoding="utf-8")
    print("ok %s (verdict ok)" % o["key"]); sys.exit(0)
if REVIEW and o.get("verdict") != "revised":
    err.append('a review is {"key", "verdict": "ok"} or the full label with "verdict": "revised" and a "review_note"')
if REVIEW and not o.get("review_note"):
    err.append('a revised label needs a "review_note" saying what was fixed')
for f in ("key", "name", "description", "history", "rule", "changes"):
    if f not in o:
        err.append("missing field %s" % f)
if not err:
    if o["key"] not in keys:
        err.append("key %s is not in shard %d; check which shard you are" % (o["key"], shard))
    if o["key"] in done and "--replace" not in sys.argv:
        err.append("key %s is already written in shard %d (add --replace to correct it)" % (o["key"], shard))
    if o["key"] not in done and "--replace" in sys.argv:
        err.append("key %s is not written yet; --replace only corrects a saved label" % o["key"])
    if len(o["name"]) > 85:
        err.append("name is %d characters; keep it under 80" % len(o["name"]))
    if len(o["description"]) > 470:
        err.append("description is %d characters; keep it under 450" % len(o["description"]))
    if not 400 <= len(o["history"]) <= 1300:
        err.append("history is %d characters; aim for 600-1,100" % len(o["history"]))
    if not isinstance(o["changes"], list):
        err.append("changes must be a list")
    else:
        for c in o["changes"]:
            if not isinstance(c, dict) or set(c) != {"when", "case", "change", "what", "basis"} or \
                    c["basis"] not in ("evidence", "knowledge"):
                err.append("malformed change: %s" % json.dumps(c, ensure_ascii=False)[:150])
    if "distinct_from" in o and (not isinstance(o["distinct_from"], list) or any(
            not isinstance(p, dict) or set(p) != {"key", "how"} for p in o["distinct_from"])):
        err.append('distinct_from must be a list of {"key", "how"}')
    if "phases" in o and (not isinstance(o["phases"], list) or any(
            not isinstance(p, dict) or set(p) != {"span", "rule"} for p in o["phases"])):
        err.append('phases must be a list of {"span", "rule"}')
if err:
    print("REJECTED, nothing written:\n  " + "\n  ".join(err))
    sys.exit(1)
o = {k: o[k] for k in ("key", "name", "description", "history", "rule", "changes", "phases", "distinct_from",
                       "verdict", "review_note") if k in o}
if "--replace" in sys.argv:                      # rewrite the file with this label in place of the saved one
    lines = [l for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = [json.dumps(o, ensure_ascii=False) if json.loads(l)["key"] == o["key"] else l for l in lines]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("replaced %s in shard %d" % (o["key"], shard))
    sys.exit(0)
with out.open("a", encoding="utf-8") as f:
    f.write(json.dumps(o, ensure_ascii=False) + "\n")
print("ok %s (%d of %d done in shard %d)" % (o["key"], len(done) + 1, len(keys), shard))
