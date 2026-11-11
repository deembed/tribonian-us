"""Lay the round-2 differentiation output over import-round2.jsonl -> import-final.jsonl, and check it.

Checks: every group label written; names unique within each group (and reported across the whole set);
distinct_from points only at keys in the same group. Text fields replace the round-2 ones; the pre-differentiation
name is kept as `before_differentiation`.

usage: python merge_diff.py WORK_DIR [--check]
"""
import collections, json, sys
from pathlib import Path

work = Path(sys.argv[1])
D = work / "build" / "cocite" / "rule-labels"
labs = {}
for line in (D / "import-round2.jsonl").open(encoding="utf-8"):
    x = json.loads(line)
    labs[x["key"]] = x
groups = json.loads((D / "round2" / "diff-groups.json").read_text())
group_of = {k: g["group"] for g in groups for k in g["keys"]}
got = {}
for f in sorted((D / "round2" / "diff" / "out").glob("*.jsonl")):
    for line in f.open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            got[r["key"]] = r
missing = sorted(set(group_of) - set(got))
problems = []
for g in groups:
    names = collections.Counter((got.get(k) or labs[k])["name"] for k in g["keys"])
    for n, c in names.items():
        if c > 1:
            problems.append("group %d: name used %d times: %s" % (g["group"], c, n))
for k, r in got.items():
    for d in r.get("distinct_from", []):
        if group_of.get(d["key"]) != group_of.get(k):
            problems.append("%s: distinct_from names %s, not in its group" % (k, d["key"]))
print("differentiation: %d of %d labels written; %d missing; %d problems" % (len(got), len(group_of), len(missing), len(problems)))
for p in problems[:40]:
    print("  ", p)
if "--check" in sys.argv:
    sys.exit(0)
import re
# evidence per label, as show_diff_group2.py reads it (round-2 labels by note-view lineage, others by key; the gap
# labels of shard 9 by their gap evidence), to re-mark changes naming a case outside it: the save tool keeps only
# the five standard fields of a change, so the round-2 outside_evidence marks were dropped on the way
NOTE = "appellate-sentence-m0.2"
by_lin, by_key = {}, {}
for line in (D / ("round2-evidence-%s.jsonl" % NOTE)).open(encoding="utf-8"):
    e = json.loads(line)
    by_lin[e["lineage"]] = e
for f in (D / "round2" / "gap-evidence.jsonl", D / ("rule-evidence-%s.jsonl" % NOTE), D / "rule-evidence-appellate.jsonl"):
    for line in f.open(encoding="utf-8"):
        e = json.loads(line)
        by_key.setdefault(e["key"], e)
GENERIC = set("v vs United States State People Commonwealth Com Ex Parte In re Re of the and City County Board Inc Co "
              "Corp Department Dept Court Appeals Supreme Matter Estate Interest et al Ass'n Association Company Doe".split())


def etext(e):
    parts = e["heaviest"] + list(e["holdings"]) + list(e["change_parentheticals"])
    for t in e["timeline"]:
        parts += t["leading"] + t.get("entered", []) + t.get("left", []) + t.get("new_decisions", []) + [t["anchor"]]
    for ps in list(e["holdings"].values()) + list(e["change_parentheticals"].values()):
        parts += ps
    return " ".join(parts)


n_out = 0
for k, r in got.items():
    x = labs[k]
    x["before_differentiation"] = {f: x[f] for f in ("name", "description") if f in x}
    for f in ("name", "description", "history", "rule", "changes", "phases", "distinct_from"):
        if f in r:
            x[f] = r[f]
    x["differentiated"] = "2026-11-10"
    e = by_key.get(k) if k in by_key and not (x.get("round") == 2 or x.get("round2_kind")) else by_lin.get(x.get("evidence_lineage"))
    if e is None:
        e = by_key.get(k)
    if e is None:
        continue
    et = etext(e)
    for c in x["changes"]:
        words = [w for case in c["case"].split(";") for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", case) if w not in GENERIC]
        if words and not any(w in et for w in words):
            c["outside_evidence"] = True
            n_out += 1
            if c["basis"] == "evidence":
                c["basis"] = "knowledge"
print("%d changes in differentiated labels marked outside_evidence" % n_out)
dups = [n for n, c in collections.Counter(x["name"] for x in labs.values() if x["level"] != "area").items() if c > 1]
out = D / "import-final.jsonl"
with out.open("w", encoding="utf-8") as f:
    for k in sorted(labs):
        f.write(json.dumps(labs[k], ensure_ascii=False) + "\n")
print("wrote %s: %d labels; %d names still used by more than one label across the whole set" % (out, len(labs), len(dups)))
for n in dups[:20]:
    print("   dup:", n)
