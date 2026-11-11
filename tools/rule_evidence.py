"""Rule-history evidence for every labelled lineage in one co-citation view.

For each lineage whose label is attached (label_map): the current label, the parent cluster's label,
and per slice the size, anchor, leading cases, cases that entered or left, merges and splits; plus the
holdings of the leading cases and entrants (parentheticals, highest score first) and parentheticals
that mark a rule being made, changed or read differently (overruling, abrogating, adopting, ...).

usage: python rule_evidence.py WORK_DIR OUT.jsonl [--view appellate-sentence-m0.2] [--keys k1,k2]
"""
import json, re, sqlite3, sys
from pathlib import Path

work = Path(sys.argv[1])
out = Path(sys.argv[2])
args = sys.argv[3:]
view = args[args.index("--view") + 1] if "--view" in args else "appellate-sentence-m0.2"
only = set(args[args.index("--keys") + 1].split(",")) if "--keys" in args else None

db = sqlite3.connect("file:%s?mode=ro" % (work / "db" / ("cocite-%s.sqlite" % view)), uri=True)
cl = sqlite3.connect("file:%s?mode=ro" % (work / "db" / "courtlistener.sqlite"), uri=True)
ops = sqlite3.connect("file:%s?mode=ro" % (work / "db" / "opinions.sqlite"), uri=True)
labels = {x["key"]: x for x in json.loads((work / "src/tribonian/data/cocite_labels.json").read_text())}

WIDTH = 5
CHANGE = re.compile(r"\b(overrul\w*|abrogat\w*|abandon\w*|supersed\w*|disavow\w*|disapprov\w*|repudiat\w*|"
                    r"retreat\w*|replac\w*|adopt\w*|announc\w*|establish\w*|clarif\w*|modif\w*|refin\w*|"
                    r"extend\w*|expand\w*|narrow\w*|limit\w*|reaffirm\w*|reject\w*|recogniz\w* (?:a|an|the) new|"
                    r"first time|no longer|departing|reconsider\w*|recast\w*|reformulat\w*|superseded by statute|"
                    r"amend\w*|codif\w*)\b", re.I)


def slab(s):
    return "%d-%02d" % (s, (s + WIDTH - 1) % 100)


def sublab(code):
    return "%d.%d" % divmod(code, 1000) if code % 1000 else "%d" % (code // 1000)


names = {}


def nm(ref):
    if ref not in names:
        r = db.execute("SELECT name, cite, year FROM names WHERE ref=?", (ref,)).fetchone()
        names[ref] = {"name": r[0], "cite": r[1], "year": r[2]} if r else {"name": "cluster %d" % ref, "cite": None, "year": None}
    return names[ref]


def nstr(ref):
    x = nm(ref)
    return "%s, %s (%s)" % (x["name"], x["cite"], x["year"])


opcache = {}


def opinion_ids(ref):
    if ref not in opcache:
        opcache[ref] = [r[0] for r in ops.execute("SELECT id FROM opinions WHERE cluster_id=?", (ref,))]
    return opcache[ref]


def parens(ref, k=3, change=False):
    ids = opinion_ids(ref)
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    rows = cl.execute("SELECT text, score FROM parens WHERE described IN (%s) AND text IS NOT NULL "
                      "ORDER BY score DESC LIMIT 400" % q, ids).fetchall()
    seen, outp = set(), []
    for t, sc in rows:
        t = " ".join(t.split())
        key = re.sub(r"\W+", "", t.lower())[:80]
        if key in seen or len(t) < 25:
            continue
        if change and not CHANGE.search(t):
            continue
        seen.add(key)
        outp.append(t[:300])
        if len(outp) >= k:
            break
    return outp


label_map = dict(db.execute("SELECT lineage, key FROM label_map"))
level_of = dict(db.execute("SELECT lineage, level FROM lineages"))
unit_lin = {("sub", s, k): l for s, k, l in db.execute("SELECT slice, sub, lineage FROM subs")}
unit_lin.update({("cluster", s, k): l for s, k, l in db.execute("SELECT slice, cl, lineage FROM clusters")})


def lab_name(lin):
    k = label_map.get(lin)
    return labels[k]["name"] if k in labels else None


# parent cluster lineage for each sub lineage (most slices)
votes = {}
for sl, pl, n in db.execute("SELECT s.lineage, c.lineage, count(*) FROM subs s JOIN clusters c "
                            "ON c.slice = s.slice AND c.cl = s.sub / 1000 WHERE s.sub % 1000 > 0 GROUP BY 1, 2"):
    if n > votes.get(sl, (None, 0))[1]:
        votes[sl] = (pl, n)

todo = [(lin, key) for lin, key in label_map.items() if key in labels and (only is None or key in only)]
if "--lineages" in args:                 # unlabelled lineages too: a JSON list of lineage ids; key "new-<view>-<lineage>"
    want = json.loads(Path(args[args.index("--lineages") + 1]).read_text())
    todo = [(l, label_map.get(l) or "new-%s-%d" % (view, l)) for l in want]
todo.sort(key=lambda x: (level_of[x[0]] != "cluster", x[0]))
print("%d lineages" % len(todo), file=sys.stderr)

with out.open("w", encoding="utf-8") as f:
    for i, (lin, key) in enumerate(todo, 1):
        level = level_of[lin]
        lab = labels.get(key) or {"name": None, "description": None}
        if level == "sub":
            units = db.execute("SELECT slice, sub, size, anchor FROM subs WHERE lineage=? ORDER BY slice", (lin,)).fetchall()
            mem_sql = "SELECT ref, subw FROM nodes WHERE slice=? AND sub=? ORDER BY subw DESC"
        else:
            units = db.execute("SELECT slice, cl, size, anchor FROM clusters WHERE lineage=? ORDER BY slice", (lin,)).fetchall()
            mem_sql = "SELECT ref, weight FROM members WHERE slice=? AND cl=? AND home=1 ORDER BY weight DESC"
        timeline, prev, total = [], None, {}
        entrants_all = []
        for s, k, size, anchor in units:
            mem = db.execute(mem_sql, (s, k)).fetchall()
            rank = [r for r, _ in mem]
            for r, w in mem:
                total[r] = total.get(r, 0) + (w or 0)
            setm = set(rank)
            row = {"slice": slab(s), "unit": "%s:%s" % (slab(s), sublab(k) if level == "sub" else k), "size": size,
                   "anchor": nstr(anchor), "leading": [nstr(r) for r in rank[:8]]}
            if prev is not None:
                ps, pset, prank = prev
                if ps == s - WIDTH:
                    ent = [r for r in rank[:60] if r not in pset][:8]
                    ext = [r for r in prank[:30] if r not in setm][:5]
                    # where the leavers went / entrants came from
                    row["entered"] = [nstr(r) for r in ent]
                    row["left"] = [nstr(r) for r in ext]
                    entrants_all += [(r, s) for r in ent]
                else:
                    row["gap_before"] = True
            # newly decided leading cases (decided in this slice or the one before) carry new rules
            row["new_decisions"] = [nstr(r) for r in rank[:80] if (nm(r)["year"] or 0) >= s - WIDTH][:6]
            entrants_all += [(r, s) for r in rank[:80] if (nm(r)["year"] or 0) >= s - WIDTH][:6]
            tbl = "subs" if level == "sub" else "clusters"
            ins = db.execute("SELECT slice_from, cl_from, jaccard FROM links WHERE level=? AND slice_to=? AND cl_to=?",
                             (level, s, k)).fetchall()
            outs = db.execute("SELECT slice_to, cl_to, jaccard FROM links WHERE level=? AND slice_from=? AND cl_from=?",
                              (level, s, k)).fetchall()
            def desc(ss, kk, j):
                l2 = unit_lin.get((level, ss, kk))
                return {"unit": "%s:%s" % (slab(ss), sublab(kk) if level == "sub" else kk), "jaccard": round(j, 2),
                        "same_lineage": l2 == lin, "label": lab_name(l2) if l2 != lin else None}
            ins = [desc(*x) for x in ins if unit_lin.get((level, x[0], x[1])) != lin]
            outs = [desc(*x) for x in outs if unit_lin.get((level, x[0], x[1])) != lin]
            if ins:
                row["merged_in_from"] = ins[:4]
            if outs:
                row["split_off_to"] = outs[:4]
            timeline.append(row)
            prev = (s, setm, rank)
        heavy = [r for r, _ in sorted(total.items(), key=lambda x: -x[1])[:20]]
        focus = list(dict.fromkeys(heavy[:14] + [r for r, _ in entrants_all]))[:30]
        holdings = {}
        changes = {}
        for r in focus:
            p = parens(r, k=2)
            if p:
                holdings[nstr(r)] = p
            c = parens(r, k=2, change=True)
            c = [t for t in c if t not in p]
            if c:
                changes[nstr(r)] = c
        rec = {"key": key, "level": level, "lineage": lin, "view": "cocite-" + view,
               "current": {"name": lab["name"], "description": lab["description"]},
               "span": "%s to %s" % (timeline[0]["slice"], timeline[-1]["slice"]) if timeline else None,
               "heaviest": [nstr(r) for r in heavy],
               "timeline": timeline, "holdings": holdings, "change_parentheticals": changes}
        if level == "sub":
            pl = votes.get(lin, (None,))[0]
            rec["parent_cluster"] = {"lineage": pl, "label": lab_name(pl)}
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if i % 20 == 0:
            print("  %d of %d" % (i, len(todo)), file=sys.stderr, flush=True)
