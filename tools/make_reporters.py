#!/usr/bin/env python3
"""Regenerate src/tribonian/data/reporters.json from reporters-db (Free Law Project, BSD-2-Clause).

    pip install reporters-db && python3 tools/make_reporters.py

The patterns engine (citefind.py) builds its reporter pattern from this file, so cite finding needs no
dependency at run time. Each spelling maps to the edition(s) CourtListener's citation table uses
("U. S." -> "U.S.", "NY2d" -> "N.Y.2d"); several editions when the spelling is ambiguous ("Ohio").
Left out: neutral cites ("2019-Ohio-123": another shape) and spellings with no period that are plain
words ("Hill", "Day"), which would match prose; all-capital ones ("US", "NY2d") and WORDS stay."""
import json, re, sys
from pathlib import Path
import reporters_db as R

# reporters spelled without a period, too well known to leave out: early Supreme Court reporters ("1 Cranch 137",
# "2 Black 635") and states' own reports named for the state ("138 Idaho 348", "89 Ohio St.3d 62" has its period)
WORDS = {"Cranch", "Black", "Idaho", "Iowa", "Utah", "Ohio", "Haw", "Hawaii", "Guam",
         "Dallas", "Wheaton", "Peters", "Howard", "Wallace", "Woods"}


def keep(v):
    """A spelling with a period, a digit, all capitals, in WORDS, or several short words with no period (New York's
    "251 App Div 132", "566 F Supp 555", "S Ct"; not titles like "Puerto Rico" or "Ridgely's Notebook")."""
    words = re.sub(r"[()\[\]]", " ", v).split()
    return ("." in v or any(ch.isdigit() for ch in v) or (v.isupper() and len(v) >= 2) or v in WORDS
            or (len(words) >= 2 and all(len(w) <= 5 for w in words)))

out = {}
for key, rs in R.REPORTERS.items():
    for r in rs:
        if r.get("cite_type") == "neutral":
            continue
        for ed in r["editions"]:
            if keep(ed):
                out.setdefault(ed, set()).add(ed)
        for var, ed in (r.get("variations") or {}).items():
            if keep(var):
                out.setdefault(var, set()).add(ed)
out.setdefault("WL", set()).add("WL")                      # Westlaw numbers: not a reporter in reporters-db
# the Supreme Court's early reporters by name ("7 Howard, 624", "8 Wallace, 168"), first: "Howard" is also Mississippi's
ALIASES = {"Howard": ["How.", "Howard"], "Wallace": ["Wall."]}
order = {}
for sp, eds in ALIASES.items():
    out.setdefault(sp, set()).update(eds)
    order[sp] = eds
version = getattr(R, "__version__", None)
try:
    import importlib.metadata as m
    version = m.version("reporters-db")
except Exception:
    pass
data = {"source": "reporters-db %s (Free Law Project, BSD-2-Clause; https://github.com/freelawproject/reporters-db)" % version,
        "spellings": {k: order.get(k) or sorted(v) for k, v in sorted(out.items())}}
p = Path(__file__).resolve().parent.parent / "src" / "tribonian" / "data" / "reporters.json"
p.write_text(json.dumps(data, indent=0, ensure_ascii=False) + "\n", encoding="utf-8")
print("%d spellings -> %d editions: %s" % (len(out), len({e for v in out.values() for e in v}), p))
