"""Merge round-2 label rewrites (after the lineages were re-linked) onto the round-1 import, for `cocite labels import`.

Reads build/cocite/rule-labels/import.jsonl (round 1, all 609 labels), round2/out/*.jsonl (rewrites and new labels)
and round2/auto-keep-<view>.json (drifted labels whose text still fits: fingerprint refresh only). Each round-2 label
gets its lineage's current fingerprint (the FINGER heaviest cases in the view), so it attaches as stable. New lineages
("new-<view>-<lineage>") get a key from their fingerprint, as `labels import` makes one. Changes naming a case outside
the lineage's round-2 evidence are marked outside_evidence (and moved to basis knowledge if marked evidence).

usage: python merge_round2.py WORK_DIR [--view appellate-sentence-m0.2] [--check]
"""
import hashlib, json, re, sqlite3, sys
from pathlib import Path

work = Path(sys.argv[1])
args = sys.argv[2:]
view = args[args.index("--view") + 1] if "--view" in args else "appellate-sentence-m0.2"
check_only = "--check" in args
sys.path.insert(0, str(work / "src"))
from tribonian import cocite as C

D = work / "build" / "cocite" / "rule-labels"
R = D / "round2"
db = sqlite3.connect("file:%s?mode=ro" % (work / "db" / ("cocite-%s.sqlite" % view)), uri=True)
level = dict(db.execute("SELECT lineage, level FROM lineages"))
base = {}
for line in (D / "import.jsonl").open(encoding="utf-8"):
    x = json.loads(line)
    base[x["key"]] = x
items = {i["key"]: i for i in json.loads((R / ("items-%s.json" % view)).read_text())}
ev = {}
for line in (D / ("round2-evidence-%s.jsonl" % view)).open(encoding="utf-8"):
    e = json.loads(line)
    ev[e["key"]] = e
got, problems = {}, []
for f in sorted((R / "out").glob("*.jsonl")):
    for n, line in enumerate(f.open(encoding="utf-8"), 1):
        if line.strip():
            r = json.loads(line)
            if r["key"] not in items:
                problems.append("%s:%d unknown key %s" % (f.name, n, r["key"]))
                continue
            got[r["key"]] = r
missing = sorted(set(items) - set(got))
print("round 2: %d of %d items written; %d missing" % (len(got), len(items), len(missing)))
for p in problems:
    print("  problem:", p)
if check_only:
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


out, n_new, n_rew, n_auto, n_out = dict(base), 0, 0, 0, 0
today = "2026-11-10"
for k, r in got.items():
    it, e = items[k], ev[k]
    fp = C.lineage_members(db, it["lineage"], level[it["lineage"]])
    et = etext(e)
    for c in r["changes"]:
        words = [w for case in c["case"].split(";") for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", case) if w not in GENERIC]
        if words and not any(w in et for w in words):
            c["outside_evidence"] = True
            n_out += 1
            if c["basis"] == "evidence":
                c["basis"] = "knowledge"
    if k.startswith("new-"):
        key = "%s-%s" % (it["level"], hashlib.sha1(",".join(map(str, sorted(fp))).encode()).hexdigest()[:10])
        rec = {"level": it["level"], "key": key, "fingerprint": fp, "evidence_view": "cocite-" + view,
               "evidence_lineage": it["lineage"], "round": 2, "round2_kind": it["kind"]}
        n_new += 1
    else:
        rec = dict(base[k])
        rec["round1"] = {x: rec[x] for x in ("name", "description", "history", "rule", "changes") if x in rec}
        rec["fingerprint"] = fp
        rec["round"] = 2
        rec["round2_kind"] = it["kind"]
        rec["evidence_lineage"] = it["lineage"]
        key = k
        n_rew += 1
    for x in ("name", "description", "history", "rule", "changes", "phases"):
        if x in r:
            rec[x] = r[x]
    rec["written_by"] = "claude (Claude Code subagents, round 2 after the lineage re-link)"
    rec["written"] = today
    out[key] = rec
for a in json.loads((R / ("auto-keep-%s.json" % view)).read_text()):
    rec = dict(base[a["key"]])
    rec["fingerprint"] = C.lineage_members(db, a["lineage"], level[a["lineage"]])
    rec["evidence_lineage"] = a["lineage"]
    rec["round2_kind"] = "drifted-auto-kept"
    out[a["key"]] = rec
    n_auto += 1
path = D / "import-round2.jsonl"
with path.open("w", encoding="utf-8") as f:
    for k in sorted(out):
        f.write(json.dumps(out[k], ensure_ascii=False) + "\n")
print("wrote %s: %d labels (%d rewritten, %d new, %d fingerprints refreshed); %d changes outside the evidence" % (
    path, len(out), n_rew, n_new, n_auto, n_out))
