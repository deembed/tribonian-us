"""Merge rule-centred label rewrites into a file for `tribonian cocite labels import`.

Reads build/cocite/rule-labels/out/*.jsonl (one rewrite per line: key, name, description, history, rule,
changes), checks each against the current labels, and writes build/cocite/rule-labels/import.jsonl:
the existing label with the new fields, the old name and description kept under `previous`.

usage: python merge_rule_labels.py WORK_DIR [--check]     (--check: report problems, write nothing)
"""
import json, sys
from pathlib import Path

work = Path(sys.argv[1])
check_only = "--check" in sys.argv
D = work / "build" / "cocite" / "rule-labels"
labels = {x["key"]: x for x in json.loads((work / "src/tribonian/data/cocite_labels.json").read_text(encoding="utf-8"))}
views = {}
for f in D.glob("rule-evidence-*.jsonl"):
    for line in f.open(encoding="utf-8"):
        r = json.loads(line)
        views[r["key"]] = r["view"]

FIELDS = ("key", "name", "description", "history", "rule", "changes")
CHANGE_FIELDS = {"when", "case", "change", "what", "basis"}
got, problems = {}, []
for f in sorted((D / "out").glob("*.jsonl")):
    for n, line in enumerate(f.open(encoding="utf-8"), 1):
        if not line.strip():
            continue
        where = "%s:%d" % (f.name, n)
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append("%s: bad JSON (%s)" % (where, e))
            continue
        miss = [k for k in FIELDS if k not in r]
        if miss:
            problems.append("%s: missing %s" % (where, ", ".join(miss)))
            continue
        if r["key"] not in labels:
            problems.append("%s: unknown key %s" % (where, r["key"]))
            continue
        if r["key"] in got:
            problems.append("%s: %s written twice (the later one is kept)" % (where, r["key"]))
        if len(r["name"]) > 90:
            problems.append("%s: %s name is %d characters" % (where, r["key"], len(r["name"])))
        if len(r["description"]) > 520:
            problems.append("%s: %s description is %d characters" % (where, r["key"], len(r["description"])))
        for c in r["changes"] if isinstance(r["changes"], list) else [None]:
            if not isinstance(c, dict) or not CHANGE_FIELDS <= set(c) or c.get("basis") not in ("evidence", "knowledge"):
                problems.append("%s: %s has a malformed change: %s" % (where, r["key"], json.dumps(c)[:120]))
                break
        got[r["key"]] = r

want = set(views)
print("%d rewrites for %d labels with evidence; %d missing" % (len(got), len(want), len(want - set(got))))
for k in sorted(want - set(got))[:40]:
    print("  missing:", k, labels[k]["name"])
for p in problems:
    print("  problem:", p)
if check_only:
    sys.exit(0)

# a change marked basis "evidence" must name a case found in that lineage's evidence; otherwise it rests on
# the writer's knowledge, and is relabelled so (the list: check-evidence-cases.json)
import re
GENERIC = set("v vs United States State People Commonwealth Com Ex Parte In re Re of the and City County Board Inc Co "
              "Corp Department Dept Court Appeals Supreme Matter Estate Interest et al Ass'n Association Company Doe".split())
etext = {}
for f in D.glob("rule-evidence-*.jsonl"):
    for line in f.open(encoding="utf-8"):
        e = json.loads(line)
        parts = e["heaviest"] + list(e["holdings"]) + list(e["change_parentheticals"])
        for t in e["timeline"]:
            parts += t["leading"] + t.get("entered", []) + t.get("left", []) + t.get("new_decisions", []) + [t["anchor"]]
        for ps in list(e["holdings"].values()) + list(e["change_parentheticals"].values()):
            parts += ps
        etext[e["key"]] = " ".join(parts)
moved = []
for k, r in got.items():
    for c in r["changes"]:
        if c["basis"] != "evidence":
            continue
        for case in re.split(r";|\band\b(?= [A-Z])", c["case"]):
            words = [w for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", case) if w not in GENERIC]
            if words and not any(w in etext.get(k, "") for w in words):
                c["basis"] = "knowledge"
                moved.append({"key": k, "when": c["when"], "case": c["case"]})
                break
(D / "check-evidence-cases.json").write_text(json.dumps(moved, indent=1, ensure_ascii=False), encoding="utf-8")
print("%d changes relabelled from basis evidence to knowledge (case not in the lineage's evidence)" % len(moved))

# decided 2026-11-10: changes naming a case outside the lineage's evidence (later overrulings and the like) are
# kept, marked outside_evidence; years are decision years, and every year that differs from the evidence's
# year for the same case is listed for review (check-years.json)
years = {}
for f in D.glob("rule-evidence-*.jsonl"):
    for line in f.open(encoding="utf-8"):
        e = json.loads(line)
        ys = {}
        for x in e["heaviest"] + [y for t in e["timeline"] for y in t["leading"] + t.get("entered", []) + [t["anchor"]]]:
            nm, _, rest = x.partition(",")
            yr = re.search(r"\((\d{4})\)\s*$", x)
            if yr:
                for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", nm):
                    if w not in GENERIC:
                        ys.setdefault(w, set()).add(int(yr.group(1)))
        years[e["key"]] = ys
n_out, ydiff = 0, []
for k, r in got.items():
    for c in r["changes"]:
        words = [w for case in re.split(r";", c["case"]) for w in re.findall(r"[A-Z][A-Za-z'\-]{2,}", case) if w not in GENERIC]
        if words and not any(w in etext.get(k, "") for w in words):
            c["outside_evidence"] = True
            n_out += 1
        when = [int(y) for y in re.findall(r"\b(1[6-9]\d\d|20\d\d)\b", str(c["when"]))]
        if len(when) == 1 and ";" not in c["case"]:
            ev_years = set().union(*(years.get(k, {}).get(w, set()) for w in words)) if words else set()
            if ev_years and when[0] not in ev_years and all(abs(when[0] - y) <= 2 for y in ev_years):
                ydiff.append({"key": k, "case": c["case"], "label_year": when[0], "evidence_years": sorted(ev_years)})
(D / "check-years.json").write_text(json.dumps(ydiff, indent=1, ensure_ascii=False), encoding="utf-8")
print("%d changes marked outside_evidence; %d years differ from the evidence's year for the case (check-years.json)" % (
    n_out, len(ydiff)))

out = D / "import.jsonl"
with out.open("w", encoding="utf-8") as f:
    for k, r in sorted(got.items()):
        lab = dict(labels[k])
        lab["previous"] = {x: lab[x] for x in ("name", "description", "written_by", "written") if x in lab}
        for x in ("name", "description", "history", "rule", "changes"):
            lab[x] = r[x]
        lab["written_by"] = "claude (Claude Code subagents, rule-centred pass from rule-history evidence)"
        lab["written"] = "2026-11-10"
        lab["rule_evidence_view"] = views.get(k)
        f.write(json.dumps(lab, ensure_ascii=False) + "\n")
print("wrote %s (%d labels)" % (out, len(got)))
