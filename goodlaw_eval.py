#!/usr/bin/env python3
"""goodlaw_eval.py -- test the good-law check (casemap.goodlaw) against cases whose fate is known.

    ./goodlaw_eval.py collect            # read each test case's citing opinions once (slow: the disk)
    ./goodlaw_eval.py score              # score every ruleset on set A and set B (fast: the cache)
    ./goodlaw_eval.py show "410 U.S. 113" R3   # one case under one ruleset, with its evidence

Collect caches, per case, the text around every citation of it and every sentence naming it,
for a superset of citing opinions: every one from a court that could overrule it (up to 1,500),
the 500 newest, and up to 25 per decade before that. A ruleset chooses what it reads from that.
Cache: build/goodlaw-eval/<cluster>.json. No language model anywhere.
"""
import json, re, sys, time, zlib
from collections import Counter
from pathlib import Path

import casemap as cm

OUT = Path(__file__).resolve().parent / "build" / "goodlaw-eval"

# (citation, year it was overruled or superseded) -- US Supreme Court overrulings
SET_A = [("163 U.S. 537", 1954), ("478 U.S. 186", 2003), ("410 U.S. 113", 2022), ("505 U.S. 833", 2022),
         ("467 U.S. 837", 2024), ("310 U.S. 586", 1943), ("261 U.S. 525", 1937), ("316 U.S. 455", 1963),
         ("338 U.S. 25", 1961), ("41 U.S. 1", 1938), ("494 U.S. 652", 2010), ("431 U.S. 209", 2018),
         ("426 U.S. 833", 1985), ("504 U.S. 298", 2018), ("492 U.S. 361", 2005), ("277 U.S. 438", 1967),
         ("448 U.S. 56", 2004), ("482 U.S. 496", 1991), ("473 U.S. 172", 2019), ("406 U.S. 404", 2020)]
CONTROL_A = ["5 U.S. 137", "347 U.S. 483", "384 U.S. 436", "372 U.S. 335", "392 U.S. 1", "463 U.S. 29",
             "466 U.S. 668", "477 U.S. 317", "389 U.S. 347", "424 U.S. 319"]
SET_B = [("440 U.S. 410", 2019), ("491 U.S. 1", 1996), ("473 U.S. 402", 1997), ("390 U.S. 145", 1997),
         ("220 U.S. 373", 2007), ("475 U.S. 625", 2009), ("536 U.S. 545", 2013), ("550 U.S. 192", 2015),
         ("495 U.S. 508", 1993), ("490 U.S. 805", 1991), ("468 U.S. 447", 2016), ("533 U.S. 194", 2009),
         ("497 U.S. 639", 2002), ("380 U.S. 202", 1986), ("247 U.S. 251", 1941), ("302 U.S. 277", 1966),
         ("106 U.S. 583", 1964), ("316 U.S. 584", 1943), ("490 U.S. 638", 2016), ("279 U.S. 263", 1995)]
CONTROL_B = ["17 U.S. 316", "367 U.S. 643", "388 U.S. 1", "381 U.S. 479", "476 U.S. 79", "550 U.S. 544",
             "556 U.S. 662", "477 U.S. 242", "554 U.S. 570", "576 U.S. 644"]
STRAT_PER_DECADE = 25
FIRST_PARTY = False        # second parties ("Arizona", "Wainwright") match the wrong things; the short name is enough


def lookup(con, cite):
    """Cluster for a citation; when CourtListener has two records of one case, the more-cited one."""
    m = re.match(r"\s*(\d+)\s+(.+?)\s+(\d+)\s*$", cite)
    squash = lambda t: re.sub(r"[\s.]", "", t).lower()
    ids = sorted({r[0] for r in con.execute("SELECT cluster_id, reporter FROM cites WHERE volume=? AND page=?", (m.group(1), m.group(3)))
                  if squash(r[1]) == squash(m.group(2))})
    if not ids:
        raise SystemExit("no case with citation %s in the database" % cite)
    return max(ids, key=lambda i: con.execute("SELECT coalesce(citation_count, 0) FROM clusters WHERE id=?", (i,)).fetchone()[0])


def score_names(case):
    """Names used at score time: the cached short name (first entry), minus states and stop words."""
    ns = case["names"][:1]
    return [n for n in ns if n.lower() not in cm.STATES and n.lower() not in cm.NAME_STOP]


def names_for(con, cid):
    """Short names a court would use for the case: 'Chevron', 'Plessy', 'Roe', 'Casey'."""
    short, full = con.execute("SELECT case_name_short, case_name FROM clusters WHERE id=?", (cid,)).fetchone()
    out = []
    first = (full or "").split(" v. ")[0] if " v. " in (full or "") else ""
    for n in [short, first] if FIRST_PARTY else [short]:
        n = re.sub(r",.*$|\bet al\.?$", "", (n or "")).strip()
        if n and len(n) >= 3 and n.lower() not in cm.NAME_STOP and n.lower() not in cm.STATES and len(n.split()) <= 3 and n not in out:
            out.append(n)
    return out[:3]


def collect_one(con, cite):
    cid = lookup(con, cite)
    path = OUT / ("%d.json" % cid)
    if path.exists():
        return cid, "cached"
    me = con.execute("SELECT cl.date_filed, d.court_id, ct.jurisdiction, cl.case_name FROM clusters cl LEFT JOIN dockets d ON d.id = cl.docket_id "
                     "LEFT JOIN courts ct ON ct.id = d.court_id WHERE cl.id = ?", (cid,)).fetchone()
    my_level = cm.level_of(me[1], me[2])
    cites = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id = ?", (cid,)).fetchall()
    names = names_for(con, cid)
    op = cm.casedb.op_table(con)
    rows = con.execute(
        "SELECT DISTINCT o.id, cl.id, cl.date_filed, d.court_id, ct.jurisdiction, ct.citation_string "
        "FROM edges e JOIN %s o ON o.id = e.citing JOIN clusters cl ON cl.id = o.cluster_id "
        "LEFT JOIN dockets d ON d.id = cl.docket_id LEFT JOIN courts ct ON ct.id = d.court_id "
        "WHERE e.cited IN (SELECT id FROM %s WHERE cluster_id = ?) AND cl.id != ?" % (op, op), (cid, cid)).fetchall()
    rank = lambda r: cm.RANK[cm.level_of(r[3], r[4])]
    above = lambda r: rank(r) >= cm.RANK[my_level] if my_level != "other" else cm.level_of(r[3], r[4]) == "supreme"
    newest = sorted(rows, key=lambda r: r[2] or "", reverse=True)
    pick = {}
    for r in [x for x in newest if above(x)][:cm.GOODLAW_MAX]:
        pick.setdefault(r[0], [r, set()])[1].add("above")
    for r in newest[:cm.GOODLAW_RECENT]:
        pick.setdefault(r[0], [r, set()])[1].add("newest")
    by_decade = {}
    for r in newest[cm.GOODLAW_RECENT:]:
        by_decade.setdefault((r[2] or "0000")[:3], []).append(r)
    for d, rs in by_decade.items():
        for r in rs[::max(1, len(rs) // STRAT_PER_DECADE)][:STRAT_PER_DECADE]:
            pick.setdefault(r[0], [r, set()])[1].add("strat")
    name_rx = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(n) for n in names)) if names else None
    ops = []
    for oid, (r, why) in pick.items():
        row = con.execute("SELECT field, text FROM ops.opinions WHERE id = ?", (oid,)).fetchone()
        if not row or not row[1]:
            continue
        raw = zlib.decompress(row[1]).decode("utf-8", "replace")
        text = cm.marked_text(raw, cid)
        if "\x01" not in text:
            text = cm._cite_marks(cm.marked_text(raw, -1), cites)
        wins = []
        for para in text.split("\n"):
            for m in re.finditer("\x01([^\x02]*)\x02", para):
                wins.append([para[max(0, m.start() - 300):m.start()].replace("\x01", "").replace("\x02", ""), m.group(1),
                             para[m.end():m.end() + 200].replace("\x01", "").replace("\x02", "")])
        plain = text.replace("\x01", "").replace("\x02", "")
        sents = []
        if name_rx:
            for m in name_rx.finditer(plain):
                sents.append(plain[max(0, m.start() - 160):m.end() + 160])
        ops.append({"oid": oid, "cluster": r[1], "year": (r[2] or "")[:4], "court": r[5] or r[3] or "?",
                    "level": cm.level_of(r[3], r[4]), "above": bool(above(r)), "why": sorted(why),
                    "wins": wins[:40], "sents": sents[:40]})
    data = {"cluster": cid, "cite": cite, "name": me[3], "level": my_level, "names": names, "of": len(rows), "ops": ops}
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return cid, "%d of %d citing opinions" % (len(ops), len(rows))


def collect():
    con, _ = cm.open_db()
    todo = [c for c, _ in SET_A] + CONTROL_A + [c for c, _ in SET_B] + CONTROL_B
    for k, cite in enumerate(todo, 1):
        t0 = time.time()
        try:
            cid, what = collect_one(con, cite)
            print("%2d/%d %-14s cluster %-8s %s (%.0f s)" % (k, len(todo), cite, cid, what, time.time() - t0), flush=True)
        except SystemExit as e:
            print("%2d/%d %-14s SKIP %s" % (k, len(todo), cite, e), flush=True)
        except Exception as e:
            print("%2d/%d %-14s ERROR %r" % (k, len(todo), cite, e), flush=True)


# ================================================================ rulesets
# the older, looser list of softer words (rulesets before R9)
WEAK_RE = re.compile(r"\b(questioned|criticized|called into (?:serious )?(?:question|doubt)|"
                     r"declin\w+ to (?:follow|extend)|no longer (?:good law|valid|followed)|"
                     r"limited (?:to its facts|by)|undermined|eroded)", re.I)


def classify(c, before, after):
    """casemap.classify, except that rulesets without weak_tight (R0-R8) take their softer words from WEAK_RE."""
    k, w = cm.classify(c, before, after)
    if c.get("weak_tight") or k in ("strong", "other grounds"):
        return k, w
    hit = WEAK_RE.search(" \x00 ".join(cm.sentence_window(before, after)))
    return ("weak", hit.group(1).lower()) if hit else (None, None)


def evaluate(case, rs):
    if rs.get("live"):                       # the code the build actually runs
        case = dict(case, names=cm.case_names(case["names"][0] if case["names"] else "", case["name"]))
        st, y, r, ev = cm.evaluate(case, rs)
        for e in ev:
            e["snip"] = e["snippet"]
        return st, y, r, ev
    c = rs["_c"]
    reads = [o for o in case["ops"] if set(o["why"]) & set(rs["read"])]
    parties = [x.strip().lower() for x in case["name"].split(" v. ")]
    names = score_names(case)
    nrx = cm.name_rxs(c, names) if rs.get("names") else []
    ev, seen = [], set()
    for o in reads:
        found = []
        for kind_rx, rx in nrx:
            for s in o["sents"]:
                for m in rx.finditer(s):
                    pre = s[max(0, m.start() - 60):m.start()]
                    if cm.other_case(s, parties, names, m):
                        continue
                    if kind_rx != "passive" and cm.HYPOTHETICAL_RE.search(pre):
                        continue
                    if rs.get("guard_all") and cm.HYPOTHETICAL_RE.search(pre):
                        continue
                    if c.get("sent_guard"):
                        sent = re.split(cm.SENTENCE_END, s[:m.start()])[-1] + s[m.start():m.end()]
                        if cm.SENT_HYPO_RE.search(sent):
                            continue
                    found.append(("strong", m.group(1).lower(), s))
                    break
        for b, anc, a in o["wins"]:
            k, w = classify(c, b, a)
            if k:
                found.append((k, w, b[-120:] + "⟦" + anc + "⟧" + a[:100]))
        for k, w, snip in found:
            if (o["cluster"], k) in seen:
                continue
            seen.add((o["cluster"], k))
            ev.append({"year": o["year"], "court": o["court"], "above": o["above"], "kind": k, "word": w, "snip": snip,
                       "cluster": o["cluster"]})
    strong = sorted((e for e in ev if e["kind"] == "strong"), key=lambda e: e["year"])
    red = None
    need = rs["red_lower"]
    for i, e in enumerate(strong):
        lower_so_far = sum(1 for x in strong[:i + 1] if not x["above"])
        above_so_far = sum(1 for x in strong[:i + 1] if x["above"])
        if above_so_far >= rs["red_above"] or lower_so_far >= need:
            red = e["year"]
            break
    doubts = sorted((e for e in ev if e["kind"] != "weak"), key=lambda e: e["year"])
    weak = sorted((e for e in ev if e["kind"] == "weak"), key=lambda e: e["year"])
    wa = [e for e in weak if e["above"]]
    cands = [e["year"] for e in doubts[:1]] + ([weak[2]["year"]] if len(weak) >= 3 else []) + ([wa[1]["year"]] if len(wa) >= 2 else [])
    yellow = min(cands) if cands else None
    if red and (not yellow or red < yellow):
        yellow = red
    status = "red" if red else "yellow" if yellow else "green"
    return status, yellow, red, ev


BASE = {"verbs": cm.V_PASSIVE, "names": True, "ing_name": False, "read": ["above", "newest"], "red_above": 1, "red_lower": 2}
R3 = dict(BASE, sent_guard=True, sep=True, verbs=cm.V_MORE, ing_name=True, rejected=True)
RULESETS = {
    "R0 current (v4)":                           dict(BASE),
    "R1 sentence guard (unless/will/urge...)":   dict(BASE, sent_guard=True),
    "R2 R1 + history needs , or ( before it":    dict(BASE, sent_guard=True, sep=True),
    "R3 R2 + more verbs, '(overruling X)'":      R3,
    "R6 R3 + 'X is overruled' only right after": dict(R3, tight_is=True),
    "R7 R6 + red: 2 above or 3 lower":           dict(R3, tight_is=True, red_above=2, red_lower=3),
    "R8 R6 + read older decades":                dict(R3, tight_is=True, read=["above", "newest", "strat"]),
    "R9 R6 + softer words: near, not negated":   dict(R3, tight_is=True, weak_tight=True),
    "R10 R9 + read older decades":               dict(R3, tight_is=True, weak_tight=True, read=["above", "newest", "strat"]),
}
RULESETS["casemap.py now (= R9)"] = dict(cm.RULES, live=True)
for rs in RULESETS.values():
    rs["_c"] = cm.compile_rules(rs)


def load_set(pairs, controls):
    con, _ = cm.open_db()
    out = []
    for cite, year in [(c, y) for c, y in pairs] + [(c, None) for c in controls]:
        try:
            cid = lookup(con, cite)
        except SystemExit:
            continue
        p = OUT / ("%d.json" % cid)
        if p.exists():
            out.append((json.loads(p.read_text(encoding="utf-8")), year))
    return out


def score(cases, rs, tol_before=1, tol_after=5):
    """hit: red within [year-1, year+5]; early: red before that; late: red after; miss: not red.
    control: green is right, yellow a soft miss, red a false alarm."""
    t = Counter()
    rows = []
    for case, year in cases:
        st, y, r, ev = evaluate(case, rs)
        if year:
            ry = int(r) if r else None
            k = ("miss" if ry is None else "early" if ry < year - tol_before else "late" if ry > year + tol_after else "hit")
        else:
            k = {"green": "ctl ok", "yellow": "ctl yellow", "red": "ctl RED"}[st]
        t[k] += 1
        rows.append((case["cite"], case["name"][:34], year, st, y, r, k))
    return t, rows


def cmd_score():
    sets = {"A": load_set(SET_A, CONTROL_A), "B": load_set(SET_B, CONTROL_B)}
    print("cases cached: A %d, B %d" % (len(sets["A"]), len(sets["B"])))
    for name, rs in RULESETS.items():
        line = [name.ljust(40)]
        for sname, cases in sets.items():
            t, _ = score(cases, rs)
            n = sum(t[k] for k in ("hit", "early", "late", "miss"))
            line.append("%s: hit %2d/%d early %d late %d miss %d | ctl ok %d yel %d RED %d" % (
                sname, t["hit"], n, t["early"], t["late"], t["miss"], t["ctl ok"], t["ctl yellow"], t["ctl RED"]))
        print("  ".join(line))
    if "-v" in sys.argv:
        for name, rs in RULESETS.items():
            print("\n" + name)
            for sname, cases in sets.items():
                for row in score(cases, rs)[1]:
                    if row[-1] not in ("hit", "ctl ok"):
                        print("  %s %-14s %-34s truth %-5s -> %-6s yellow %-5s red %-5s %s" % ((sname,) + row))


def cmd_show(key, rsname):
    rs = next(v for k, v in RULESETS.items() if k.startswith(rsname))
    con, _ = cm.open_db()
    cid = int(key) if key.isdigit() else lookup(con, key)
    case = json.loads((OUT / ("%d.json" % cid)).read_text(encoding="utf-8"))
    st, y, r, ev = evaluate(case, rs)
    print("%s %s names=%s  status %s yellow %s red %s" % (case["cite"], case["name"], case["names"], st, y, r))
    for e in sorted(ev, key=lambda e: e["year"])[:40]:
        print("  %s %-12s %-13s %-12s %s| %s" % (e["year"], e["court"][:12], e["kind"], e["word"], "above " if e["above"] else "",
                                                re.sub(r"\s+", " ", e["snip"])[:200]))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "score"
    if cmd == "collect":
        collect()
    elif cmd == "show":
        cmd_show(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "R0")
    else:
        cmd_score()
