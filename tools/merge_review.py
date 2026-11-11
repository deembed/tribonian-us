"""Apply the review pass (build/cocite/rule-labels/review/out/*.jsonl) to the labels -> import-review.jsonl.

"ok" verdicts stamp the label as reviewed. "revised" ones replace its text fields (name, description, history, rule,
changes, phases, distinct_from) and keep the structure (key, fingerprint, views, earlier versions); the previous
text is kept under `before_review`, the reviewer's note under `review_note`. Changes naming a case outside the
label's review evidence are re-marked outside_evidence (the save tool keeps only the five standard fields).
Checks: every review key exists; names stay unique among labels shown in the same view.

usage: python merge_review.py WORK_DIR [--check]
"""
import collections, json, re, sqlite3, sys
from pathlib import Path

work = Path(sys.argv[1])
D = work / "build" / "cocite" / "rule-labels"
R = D / "review"
labs = {x["key"]: x for x in json.loads((work / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8"))}
ev = {}
for line in (R / "evidence.jsonl").open(encoding="utf-8"):
    e = json.loads(line)
    ev[e["key"]] = e
want = {k.strip() for f in (R / "shards").glob("shard-*.txt") for k in f.read_text().split()}
got = {}
for f in sorted((R / "out").glob("*.jsonl")):
    for line in f.open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            got[r["key"]] = r
c = collections.Counter(r.get("verdict") for r in got.values())
print("review: %d of %d done; %s; %d unknown keys" % (len(got), len(want), dict(c), len(set(got) - set(labs))))
if "--check" in sys.argv:
    sys.exit(0)
GENERIC = set("v vs United States State People Commonwealth Com Ex Parte In re Re of the and City County Board Inc Co "
              "Corp Department Dept Court Appeals Supreme Matter Estate Interest et al Ass'n Association Company Doe".split())


def etext(e):
    parts = e["heaviest"] + list(e["holdings"]) + list(e["change_parentheticals"])
    for t in e["timeline"]:
        parts += t["leading"] + t.get("entered", []) + t.get("left", []) + t.get("new_decisions", []) + [t["anchor"]]
    for ps in list(e["holdings"].values()) + list(e["change_parentheticals"].values()):
        parts += ps
    return " ".join(parts)


n_rev = n_out = 0
out = {}
already = {k for k in got if labs[k].get("reviewed")}
print("%d already applied (skipped)" % len(already))
for k, r in got.items():
    if k in already:                     # applied by an earlier run: merging again would overwrite before_review
        continue
    x = dict(labs[k])
    x["reviewed"] = {"by": "claude-fable-5-2", "date": "2026-11-10", "verdict": r["verdict"]}
    if r["verdict"] == "revised":
        x["before_review"] = {f: x[f] for f in ("name", "description", "history", "rule", "changes", "phases") if f in x}
        for f in ("name", "description", "history", "rule", "changes", "phases", "distinct_from"):
            if f in r:
                x[f] = r[f]
        x["reviewed"]["note"] = r.get("review_note", "")
        n_rev += 1
        et = etext(ev[k]) if k in ev else ""
        for ch in x["changes"]:
            words = [w for case in ch["case"].split(";") for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", case) if w not in GENERIC]
            if et and words and not any(w in et for w in words):
                ch["outside_evidence"] = True
                n_out += 1
                if ch["basis"] == "evidence":
                    ch["basis"] = "knowledge"
    out[k] = x
# names unique among labels shown in one view
merged = dict(labs)
merged.update(out)
for v in ("appellate-sentence-m0.2", "appellate"):
    db = sqlite3.connect("file:%s?mode=ro" % (work / "db" / ("cocite-%s.sqlite" % v)), uri=True)
    shown = [k for (k,) in db.execute("SELECT key FROM label_map")]
    dup = [n for n, m in collections.Counter(merged[k]["name"] for k in shown).items() if m > 1]
    print("%s: %d shown, %d duplicate names%s" % (v, len(shown), len(dup), (": " + "; ".join(dup[:5])) if dup else ""))
path = D / "import-review.jsonl"
with path.open("w", encoding="utf-8") as f:
    for k in sorted(out):
        f.write(json.dumps(out[k], ensure_ascii=False) + "\n")
print("wrote %s: %d reviewed, %d revised; %d changes in revised labels outside the evidence" % (path, len(out), n_rev, n_out))
