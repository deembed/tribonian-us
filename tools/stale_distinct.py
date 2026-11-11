"""Find distinct_from entries made stale by later revisions, and mark them.

An entry {"key", "how"} on label A describes how A differs from label B. After the review revised A or B, the "how"
text can name a period or a case that is no longer in either label. Such an entry is stale: its text becomes
"stale - update". An entry is checked only when A or B was revised (before_review present); it is stale when it names
a span ("1975-94", "1980-2004") or a case name that appears in neither label's current name, description or history.

usage: python stale_distinct.py WORK_DIR [--write]      (without --write: report only)
"""
import json, re, sys
from pathlib import Path

work = Path(sys.argv[1])
path = work / "src/tribonian/data/cocite_labels.json"
labs = json.loads(path.read_text(encoding="utf-8"))
by = {x["key"]: x for x in labs}
STALE = "stale - update"
SPAN = re.compile(r"\b(1[6-9]\d\d|20\d\d)\s*[-–]\s*(\d{2,4})\b")
NAME = re.compile(r"\b[A-Z][a-z][A-Za-z'\-]{2,}\b")
COMMON = set("""The This That These Both Each Their Its From With Without Under After Before Since During Through Where When
While Also Only Here There Other Others Unlike Like Same Later Earlier Federal State States Supreme Court Courts Circuit
Appeals Appellate Amendment Amendments Clause Act Rule Rules Code Constitution Constitutional Congress Commission Board
Department United Texas California Florida Georgia Illinois Ohio Pennsylvania Louisiana Mississippi Alabama Tennessee
Kentucky Missouri Michigan Washington Oregon Arizona Nevada Colorado Kansas Iowa Minnesota Wisconsin Indiana Virginia
Maryland Massachusetts Connecticut Jersey York Carolina Dakota Hampshire Idaho Montana Utah Mexico Oklahoma Arkansas
Hawaii Alaska Maine Vermont Nebraska Wyoming Delaware Rhode Island District Columbia Ninth Fifth Fourth Seventh Second
Third First Sixth Eighth Tenth Eleventh Fourteenth Sixth Dominant How Alongside Phase Phases Sub Cluster""".split())


def text(x):
    return " ".join(str(x.get(f, "")) for f in ("name", "description", "history", "rule"))


def norm_spans(t):
    out = set()
    for a, b in SPAN.findall(t):
        b = b if len(b) == 4 else a[:2] + b if len(b) == 2 else b
        out.add((a, b))
    return out


stale, checked = [], 0
for x in labs:
    for d in x.get("distinct_from") or []:
        y = by.get(d["key"])
        if d.get("how") == STALE or not (x.get("before_review") or (y and y.get("before_review"))):
            continue
        checked += 1
        both = text(x) + " " + (text(y) if y else "")
        years_now = set(re.findall(r"\b(?:1[6-9]|20)\d\d\b", both)) | {y for sp in norm_spans(both) for y in sp}
        bad_span = [sp for sp in norm_spans(d["how"]) if sp[0] not in years_now or sp[1] not in years_now]
        how = re.sub(r"(\w)['’]s\b", r"\1", d["how"])
        starts = {m.group(1) for m in re.finditer(r"(?:^|[.;:]\s+)([A-Z][A-Za-z'\-]+)", how)}
        lower = both.lower()
        def missing(n):        # a name absent from both labels; hyphenated compounds ("Alleyne-anchored") by part
            parts = [p for p in n.split("-") if p[:1].isupper()]
            return all(p not in COMMON and p not in both and re.search(r"\b%s\b" % re.escape(p.lower()), lower) is None
                       for p in parts) if parts else False
        bad_name = [n for n in set(NAME.findall(how)) if n not in starts and missing(n)]
        if bad_span or bad_name:
            stale.append((x["key"], d["key"], bad_span, bad_name, d["how"][:120]))
            if "--write" in sys.argv:
                d["how"] = STALE
print("distinct_from entries checked (label or neighbour revised): %d; stale: %d" % (checked, len(stale)))
for a, b, s, n, h in stale[:25]:
    print("  %s -> %s  %s%s | %s" % (a, b, ("spans %s " % s) if s else "", ("names %s" % n) if n else "", h))
if "--write" in sys.argv:
    path.write_text(json.dumps(sorted(labs, key=lambda r: r["key"]), indent=1, ensure_ascii=False), encoding="utf-8")
    print("wrote %s" % path)
