"""Round 3 (appellate view) -> appellate-only labels to import, with the round-2 checks.

Each label from round3/out/*.jsonl gets its appellate lineage's fingerprint, a key from that fingerprint (as `labels
import` makes one), and views ["cocite-appellate"], so it is matched only in the appellate view and never displaces a
case-note-view label. Changes naming a case outside the lineage's evidence are marked outside_evidence (basis
knowledge). Names must be unique against every label already in the labels file.

usage: python merge_round3.py WORK_DIR [--check]     -> build/cocite/rule-labels/import-round3.jsonl
"""
import collections, hashlib, json, re, sqlite3, sys
from pathlib import Path

work = Path(sys.argv[1])
sys.path.insert(0, str(work / "src"))
from tribonian import cocite as C

D = work / "build" / "cocite" / "rule-labels"
R = D / "round3"
db = sqlite3.connect("file:%s?mode=ro" % (work / "db" / "cocite-appellate.sqlite"), uri=True)
level = dict(db.execute("SELECT lineage, level FROM lineages"))
items = {i["key"]: i for i in json.loads((R / "items.json").read_text())}
ev = {}
for line in (R / "evidence.jsonl").open(encoding="utf-8"):
    e = json.loads(line)
    ev[e["lineage"]] = e
existing = json.loads((work / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8"))
names = collections.Counter(x["name"] for x in existing if x["level"] != "area")
got, problems = {}, []
for f in sorted((R / "out").glob("*.jsonl")):
    for line in f.open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            if r["key"] not in items:
                problems.append("unknown key %s" % r["key"])
            else:
                got[r["key"]] = r
for n, c in collections.Counter(r["name"] for r in got.values()).items():
    if c > 1:
        problems.append("name used %d times in round 3: %s" % (c, n))
for r in got.values():
    if names.get(r["name"]):
        problems.append("%s: name already used by a label: %s" % (r["key"], r["name"]))
print("round 3: %d of %d written; %d problems" % (len(got), len(items), len(problems)))
for p in problems[:30]:
    print("  ", p)
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


n_out = 0
out = D / "import-round3.jsonl"
with out.open("w", encoding="utf-8") as f:
    for k, r in sorted(got.items()):
        it = items[k]
        lin = it["lineage"]
        fp = C.lineage_members(db, lin, level[lin])
        et = etext(ev[lin])
        for c in r["changes"]:
            words = [w for case in c["case"].split(";") for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", case) if w not in GENERIC]
            if words and not any(w in et for w in words):
                c["outside_evidence"] = True
                n_out += 1
                if c["basis"] == "evidence":
                    c["basis"] = "knowledge"
        rec = {"level": it["level"], "fingerprint": fp, "views": ["cocite-appellate"], "evidence_view": "cocite-appellate",
               "evidence_lineage": lin, "round": 3, "round3_kind": it["kind"], "shared_key": it.get("shared_key"),
               # own prefix: a fingerprint can equal a shared label's, and the key must not overwrite it
               "key": "app-%s-%s" % (it["level"], hashlib.sha1(",".join(map(str, sorted(fp))).encode()).hexdigest()[:10]),
               "written_by": "claude (Claude Code subagents, round 3: appellate view)", "written": "2026-11-10"}
        for x in ("name", "description", "history", "rule", "changes", "phases"):
            if x in r:
                rec[x] = r[x]
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print("wrote %s: %d appellate-only labels; %d changes outside the evidence" % (out, len(got), n_out))
