"""cocite_subdoc.py -- explain one sub-cluster: a PDF and an Obsidian note (tribonian cocite subdoc).

For one sub-cluster in one slice (2020-24:2.3): its label and description, where it sits (cluster,
lineage, area), the lineage's timeline, with the cases that joined or left it on either side of
this slice and the sub-clusters they came from or went to, and then every member case, most
central first: how often it is cited (in the slice and in all), its good-law standing, the other
clusters it ties to, the members it travels with most, and how later courts describe it
(CourtListener's parenthetical groups). Everything is read from the view, the bulk database and
the cached case profiles. No language model runs here; the label and description were written
once from script-built evidence (cocite_labels.json) and are marked as such.

    tribonian cocite subdoc 2020-24:2.3                  # md-cocite/Sub-clusters/2020-24/<...>.md + pdf/Co-citation/2020-24/<...>.pdf
    tribonian cocite subdoc 2020-24:2.3 --goodlaw        # first the quick good-law check (newest 100 citers) on members never checked
    tribonian cocite subdoc 2020-24:2.3 --goodlaw full   # the full check instead (slow; gives the year it changed)
    tribonian cocite subdoc 2020-24:2.3 --json           # the data on stdout; writes nothing
    tribonian cocite subdoc 2020-24:2.3 --no-pdf         # the note and the .tex only
    tribonian cocite subdoc 2020:7                       # an unsplit cluster is its own sub-cluster
    tribonian cocite subdoc 2020-24:2.3 --view appellate # another view (default: the case-note view)

Good law per case: the cached result (cases/citedby/<ref>/profile.json, from a build or --goodlaw;
"(quick)" when only the newest 100 citing opinions were read), else "not calculated". Grey "dormant": never cited, or no citing decision in the last
5 full years before the bulk data's date; from the profile's citations per year, else
CourtListener's citing count when it is 0, else a live look-up for cases cited at most 300 times.
"""
import json, os, re, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

from tribonian import cocite as C
from tribonian import case2md

LIVE_DORMANT_MAX = 300           # the live dormancy look-up runs only for cases cited at most this often
PARTNERS_SHOWN = 3               # members it travels with, per case
EVIDENCE_SHOWN = 3               # good-law warnings per red/yellow case
ABOUT_COLOR = "violet!8"         # the cocite boxes' colour: derived, not part of the record


def add_parser(sub, view):
    p = sub.add_parser("subdoc", parents=[view], help="explain one sub-cluster: every case, its citations, "
                                                      "parentheticals and good law -> note + PDF (cocite_subdoc.py)")
    p.add_argument("which", help="slice:sub-cluster, e.g. 2020-24:2.3 (or 2020:7 for an unsplit cluster)")
    p.add_argument("--view", help="results file stem after cocite-, e.g. appellate-sentence-m0.2 (default, the "
                                  "case-note view), appellate, or all; overrides --courts/--weight/--mix")
    p.add_argument("--goodlaw", nargs="?", const="quick", choices=["quick", "full"],
                   help="first run the good-law check on members with no current result: quick (the default: the "
                   "newest 100 citing opinions, status now) or full (every one from a court that could overrule it: "
                   "slow, gives the year it changed)")
    p.add_argument("--json", action="store_true", help="print the data as JSON; write nothing")
    p.add_argument("--no-pdf", action="store_true", help="skip pdflatex")
    p.add_argument("--font", default="XCharter", help="LaTeX font package (default XCharter)")
    p.set_defaults(func=cmd_subdoc)


# ================================================================ data

def open_db(args):
    """-> (connection, view name). --view, else the --courts/--weight/--mix file, else the note view."""
    if args.view or not (getattr(args, "courts", None) or getattr(args, "weight", None)):
        v = args.view or C.NOTE_VIEW
        db = C.open_view(v)
        if db is None:
            sys.exit("no view %s (bulk/cocite-%s.sqlite); tribonian cocite build makes it" % (v, v))
        return db, v
    stem = C.COCITE_DB.stem
    return C.need_db(), (stem[len("cocite-"):] if stem.startswith("cocite-") else "all")


def resolve(db, which):
    """'2020-24:2.3' -> (slice, sub code). An unsplit cluster's number names its only sub-cluster."""
    try:
        s, k, j = C.parse_label(which)
    except ValueError:
        sys.exit("can't read %r: write slice:cluster.sub, e.g. 2020-24:2.3" % which)
    s -= s % C.WIDTH
    if k is None:
        sys.exit("name a sub-cluster, e.g. %s:2.3 (tribonian cocite tree %d lists them)" % (C.label(s), s))
    if j is None:
        subs = [r[0] for r in db.execute("SELECT sub FROM subs WHERE slice=? AND sub/1000=? ORDER BY sub", (s, k))]
        if not subs:
            sys.exit("no cluster %s:%d (tribonian cocite tree %d lists them)" % (C.label(s), k, s))
        if subs != [k * 1000]:
            sys.exit("cluster %s:%d is split; name one of its sub-clusters: %s" % (
                C.label(s), k, ", ".join("%s:%s" % (C.label(s), C.sub_label(x)) for x in subs)))
        j = k * 1000
    if not db.execute("SELECT 1 FROM subs WHERE slice=? AND sub=?", (s, j)).fetchone():
        sys.exit("no sub-cluster %s:%s (tribonian cocite cluster %s:%d lists them)" % (C.label(s), C.sub_label(j), C.label(s), k))
    return s, j


def chunks(xs, n=500):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def main_info(con, refs):
    """ref -> name, date, CourtListener's citing count, precedential status, court; from the main database."""
    out = {}
    if con is None:
        return out
    try:
        for part in chunks(refs):
            q = ",".join("?" * len(part))
            for r in con.execute(
                    "SELECT cl.id, cl.case_name, cl.case_name_short, cl.date_filed, cl.citation_count, cl.precedential_status, "
                    "ct.citation_string, ct.short_name, ct.id FROM clusters cl LEFT JOIN dockets d ON d.id = cl.docket_id "
                    "LEFT JOIN courts ct ON ct.id = d.court_id WHERE cl.id IN (%s)" % q, part):
                out[r[0]] = {"name": r[1] or r[2], "date": r[3], "cited": r[4], "status": r[5],
                             "court": r[6] or r[7] or r[8] or ""}
    except sqlite3.Error as e:
        print("  note: no main-database details (%s)" % e, file=sys.stderr)
    return out


def read_profile(ref):
    p = case2md.CASES / "citedby" / str(ref) / "profile.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def data_end(con, db):
    """Last full year of the bulk data (its date's year less one): the dormancy window ends there."""
    stamp = None
    if con is not None:
        try:
            r = con.execute("SELECT file FROM loaded WHERE name='citation-map'").fetchone()
            m = r and re.search(r"\d{4}-\d\d-\d\d", r[0])
            stamp = m.group(0) if m else None
        except sqlite3.Error:
            pass
    if not stamp:
        m = re.search(r"\d{4}-\d\d-\d\d", C.view_stamp(db) or "")
        stamp = m.group(0) if m else None
    return (int(stamp[:4]) - 1 if stamp else time.localtime().tm_year - 1), stamp


def live_last_year(con, ref):
    op = C.casedb.op_table(con)
    r = con.execute("SELECT max(cl.date_filed) FROM edges e JOIN %s o ON o.id = e.citing "
                    "JOIN clusters cl ON cl.id = o.cluster_id WHERE e.cited IN "
                    "(SELECT id FROM %s WHERE cluster_id = ?) AND cl.id != ?" % (op, op), (ref, ref)).fetchone()
    return int(r[0][:4]) if r and r[0] and r[0][:4].isdigit() else None


def dormancy(prof, info, con, ref, end):
    """{'dormant', 'last', 'source'} or None (unknown). Recent = the last RECENT full years, or later."""
    start = end - case2md.RECENT + 1
    if prof and isinstance(prof.get("years"), dict):
        ys = [int(y) for y, n in prof["years"].items() if n and str(y).isdigit()]
        last, src = (max(ys) if ys else None), "citations per year in the cached case profile"
    elif info and info.get("cited") == 0:
        last, src = None, "CourtListener's citing count is 0"
    elif info and info.get("cited") is not None and info["cited"] <= LIVE_DORMANT_MAX and con is not None:
        try:
            last = live_last_year(con, ref)
        except sqlite3.Error:
            return None
        if last is None:
            return None
        src = "newest citing decision in the local database"
    else:
        return None
    return {"dormant": last is None or last < start, "last": last, "window": [start, end], "source": src}


def unit_ref(db, t, code, cache):
    """(slice, sub) -> {'code', 'name', 'lineage'}; cached."""
    key = (t, code)
    if key not in cache:
        nm, lin, _ = C.unit_name(db, t, code, "sub")
        cache[key] = {"slice": t, "code": "%s:%s" % (C.label(t), C.sub_label(code)), "name": nm, "lineage": lin}
    return cache[key]


def subs_at(db, t, refs):
    """ref -> its sub-cluster code in slice t (absent: not a node there)."""
    out = {}
    for part in chunks(refs):
        q = ",".join("?" * len(part))
        out.update(db.execute("SELECT ref, sub FROM nodes WHERE slice=? AND ref IN (%s)" % q, [t] + part).fetchall())
    return out


def moves(db, refs, t, cache, names):
    """Group cases by where they sit in slice t: [{'unit': {...} or None, 'cases': [(ref, name)]}], biggest first."""
    where = subs_at(db, t, refs)
    groups = defaultdict(list)
    for r in refs:
        code = where.get(r)
        groups[code if code is None or code > 0 else -1].append(r)      # sub <= 0: a node in no cluster
    out = []
    for code, rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0] is None, kv[0] or 0)):
        unit = (unit_ref(db, t, code, cache) if code and code > 0 else
                {"slice": t, "code": None, "name": None, "lineage": None, "node": code == -1})
        out.append({"unit": unit, "cases": [{"ref": r, "name": names.get(r) or C.name(db, r)} for r in rs]})
    return out


def lineage_data(db, s, j, lin, members, names):
    """Timeline cells, merges/splits, and the cases that joined or left on either side of slice s."""
    if not lin:
        return None
    cache = {}
    cells = db.execute("SELECT slice, sub, size FROM subs WHERE lineage=? ORDER BY slice", (lin,)).fetchall()
    slices = {r[0] for r in db.execute("SELECT slice FROM slices")}
    cutoff = C.newest_full_slice(db)
    here = set(members)
    out = {"lineage": lin, "cells": [{"slice": t, "label": C.label(t), "code": "%s:%s" % (C.label(t), C.sub_label(c)),
                                      "size": z, "here": t == s} for t, c, z in cells], "links": [], "sides": []}
    for lv, sf, cf, st, ct, shared, jac in db.execute(
            "SELECT level, slice_from, cl_from, slice_to, cl_to, shared, jaccard FROM links WHERE level='sub' AND "
            "((slice_to=? AND cl_to=?) OR (slice_from=? AND cl_from=?)) ORDER BY jaccard DESC", (s, j, s, j)):
        into = st == s and ct == j
        other = unit_ref(db, sf, cf, cache) if into else unit_ref(db, st, ct, cache)
        out["links"].append({"dir": "in" if into else "out", "other": other, "shared": shared, "jaccard": jac,
                             "same": other["lineage"] == lin})
    by_slice = {t: c for t, c, _ in cells}

    def cell_members(t):
        return {r for (r,) in db.execute("SELECT ref FROM nodes WHERE slice=? AND sub=?", (t, by_slice[t]))}

    for t, word in ((s - C.WIDTH, "before"), (s + C.WIDTH, "after")):
        if t not in slices:
            continue
        side = {"slice": t, "label": C.label(t), "side": word, "cell": None, "partial": t + C.WIDTH - 1 >= cutoff}
        if t in by_slice:
            other = cell_members(t)
            side["cell"] = "%s:%s" % (C.label(t), C.sub_label(by_slice[t]))
            side["kept"] = len(here & other)
        else:
            other = set()
        if word == "before":     # joined: here, not in the lineage before -> where they were; left: the reverse
            side["joined"] = moves(db, sorted(here - other, key=members.index), t, cache, names)
            side["left"] = moves(db, sorted(other - here), s, cache, names)
        else:                    # leaving: here, not in the lineage after -> where they go; joining after: from where
            side["left"] = moves(db, sorted(here - other, key=members.index), t, cache, names)
            side["joined"] = moves(db, sorted(other - here), s, cache, names)
        out["sides"].append(side)
    return out


def split_by_sub(db, s, refs, view):
    """How each case's co-citations in slice s divide among the slice's sub-clusters: ref -> Counter(sub ->
    weight), weight = decisions citing both, summed over the partners in that sub-cluster (sub -1: nodes in
    no sub-cluster). Read from cocite-cache/ (decision -> cited case) with the slice's own filters: the
    view's courts, decisions filed in the slice, decisions citing more than max_cites of its nodes left
    out, pairs under min_support dropped. Binary counts: a sentence view's proximity weights aren't in
    the cache. -> (splits, None) or (None, why not)."""
    import numpy as np
    cache = C.CACHE
    if not (cache / "by_cited.npy").exists() or not (cache / "level.npy").exists():
        return None, "no co-citation cache (tribonian cocite prep)"
    stamp = C.view_stamp(db)
    if stamp and (cache / "stamp.txt").read_text() != stamp:
        return None, "the co-citation cache is newer than this view (rebuild the view)"
    r = db.execute("SELECT params FROM slices WHERE slice=?", (s,)).fetchone()
    params = json.loads(r[0]) if r and r[0] else {}
    max_cites, min_support = params.get("max_cites", 150), params.get("min_support", 5)
    codes, _ = C.parse_courts(view)
    # Read whole, not memory-mapped: the lookups below jump all over these arrays (binary searches, every
    # citing decision's row), and on the HDD each jump is a seek. Sequential reads of ~1 GB beat that by far.
    t0 = time.time()
    print("  loading the co-citation cache (%s, about 1 GB)..." % cache, file=sys.stderr, flush=True)
    ci = np.load(cache / "citing.npy")
    cd = np.load(cache / "cited.npy")
    y = np.load(cache / "year.npy")
    lvl = np.load(cache / "level.npy")
    order = np.load(cache / "by_cited.npy", mmap_mode="r")      # read in contiguous slices only
    cds = np.load(cache / "cited_sorted.npy")
    print("  loaded in %.0f s; splitting each case's co-citations by sub-cluster" % (time.time() - t0),
          file=sys.stderr, flush=True)
    node_sub = {ref: (sub if sub and sub > 0 else -1) for ref, sub in db.execute("SELECT ref, sub FROM nodes WHERE slice=?", (s,))}
    cited_nodes = {}                          # decision -> the slice's nodes it cites (None: over max_cites)
    out = {}
    for k, ref in enumerate(refs, 1):
        if k % 50 == 0:
            print("  split %d of %d cases" % (k, len(refs)), file=sys.stderr, flush=True)
        lo, hi = int(np.searchsorted(cds, ref)), int(np.searchsorted(cds, ref, side="right"))
        rows = np.asarray(order[lo:hi]).astype(np.int64)
        if not len(rows):
            out[ref] = {}
            continue
        D, Y = np.asarray(ci[rows]).astype(np.int64), np.asarray(y[rows])
        keep = (Y >= s) & (Y < s + C.WIDTH)
        if codes:
            keep &= np.isin(np.asarray(lvl[D]), codes)
        per = defaultdict(int)
        for d in np.unique(D[keep]).tolist():
            lst = cited_nodes.get(d, 0)
            if lst == 0:
                a, b = int(np.searchsorted(ci, d)), int(np.searchsorted(ci, d, side="right"))
                lst = [x for x in np.asarray(cd[a:b]).tolist() if x in node_sub]
                lst = None if len(lst) > max_cites else lst
                cited_nodes[d] = lst
            for x in lst or ():
                if x != ref:
                    per[x] += 1
        split = defaultdict(int)
        for x, n in per.items():
            if n >= min_support:
                split[node_sub[x]] += n
        out[ref] = dict(split)
    return out, None


def fit_of(split, here, db, s, cache):
    """-> {'share': this sub-cluster's part of the case's co-citations, 'best': True when no other
    sub-cluster holds more, 'top': [(unit, share)] best three, 'total'}."""
    total = sum(split.values())
    if not total:
        return {"share": None, "best": True, "top": [], "total": 0}
    ranked = sorted(((k, v) for k, v in split.items() if k > 0), key=lambda kv: -kv[1])
    top = [{"unit": unit_ref(db, s, k, cache), "share": v / total, "here": k == here} for k, v in ranked[:3]]
    share = split.get(here, 0) / total
    return {"share": share, "best": bool(ranked) and ranked[0][0] == here, "top": top, "total": total,
            "none": split.get(-1, 0) / total}


def ensure_goodlaw(refs, names, mode="quick"):
    """The good-law check (casemap.goodlaw via case2md.profile_data) for members with no current result.
    quick: any current result will do; full: a quick result is redone."""
    from tribonian import casemap

    def current(r):
        g = (read_profile(r) or {}).get("goodlaw") or {}
        return g.get("version") == casemap.GOODLAW_VERSION and (mode == "quick" or g.get("mode") != "quick")
    todo = [r for r in refs if not current(r)]
    print("good law (%s): %d of %d members need the check" % (mode, len(todo), len(refs)), file=sys.stderr)
    for i, r in enumerate(todo, 1):
        t0 = time.time()
        print("  good law %d/%d: %s" % (i, len(todo), names.get(r, r)), file=sys.stderr, flush=True)
        case2md.profile_data(r, scan=mode == "full", quick=casemap.GOODLAW_QUICK if mode == "quick" else None)
        print("    %.0f s" % (time.time() - t0), file=sys.stderr, flush=True)


def gather(db, view, s, j, goodlaw=False):
    con = case2md.local_db()
    size, anchor, internal, lin = db.execute("SELECT size, anchor, internal, lineage FROM subs WHERE slice=? AND sub=?",
                                             (s, j)).fetchone()
    rows = db.execute("SELECT ref, n, subw, cl, weight FROM nodes WHERE slice=? AND sub=? ORDER BY subw DESC, n DESC",
                      (s, j)).fetchall()
    members = [r[0] for r in rows]
    info = main_info(con, members)
    short = {r: C.short(db, r) for r in members}
    full = {r: (info.get(r) or {}).get("name") or short[r] for r in members}
    if goodlaw:
        ensure_goodlaw(members, full, goodlaw)
    lab = C.label_of(db, lin)
    try:
        st = db.execute("SELECT status FROM label_status WHERE lineage=?", (lin,)).fetchone()
    except sqlite3.OperationalError:
        st = None
    cl = j // 1000
    cl_name, cl_lin, _ = C.unit_name(db, s, cl, "cluster")
    cl_size = db.execute("SELECT size FROM clusters WHERE slice=? AND cl=?", (s, cl)).fetchone()
    area = C.area_of(db, lin)
    span = db.execute("SELECT min(slice), max(slice) FROM subs WHERE lineage=?", (lin,)).fetchone() if lin else (None, None)
    end, stamp = data_end(con, db)
    cutoff = C.newest_full_slice(db)
    srow = db.execute("SELECT citing FROM slices WHERE slice=?", (s,)).fetchone()
    index = case2md.load_index()
    vlin, varea = C.vault_links(db)
    memberset = set(members)
    ucache = {}
    splits, why_no_split = split_by_sub(db, s, members, view)
    cases = []
    for rank, (ref, n, subw, home_cl, wtot) in enumerate(rows, 1):
        if rank % 100 == 0:
            print("  read %d of %d cases" % (rank, len(rows)), file=sys.stderr, flush=True)
        prof = read_profile(ref)
        i = info.get(ref) or {}
        nm = db.execute("SELECT name, cite, year FROM names WHERE ref=?", (ref,)).fetchone() or (None, "", None)
        years = {int(y): c for y, c in ((prof or {}).get("years") or {}).items() if str(y).isdigit()}
        total = sum(years.values()) if years else i.get("cited")
        recent = sum(c for y, c in years.items() if y >= end - case2md.RECENT + 1) if years else None
        also = []
        for c2, share in db.execute("SELECT cl, share FROM members WHERE slice=? AND ref=? AND home=0 ORDER BY share DESC",
                                    (s, ref)):
            nmc, _, _ = C.unit_name(db, s, c2, "cluster")
            also.append({"code": "%s:%d" % (C.label(s), c2), "name": nmc, "share": share})
        partners = []
        for p, cos, both, lift in db.execute("SELECT partner, cosine, binary, lift FROM partners WHERE slice=? AND ref=? "
                                             "ORDER BY cosine DESC", (s, ref)):
            if p in memberset and p != ref:
                partners.append({"ref": p, "name": short[p], "cosine": cos, "both": both, "lift": lift})
                if len(partners) >= PARTNERS_SHOWN:
                    break
        parens = (prof or {}).get("parens")
        if parens is None and con is not None:
            parens = case2md.parens_data(con, ref)
        gl = (prof or {}).get("goodlaw")
        ent = index.get(str(ref)) or {}
        cases.append({
            "rank": rank, "ref": ref, "name": full[ref], "short": short[ref], "cite": nm[1] or "", "year": nm[2],
            "date": i.get("date"), "court": i.get("court", ""), "precedential": i.get("status"),
            "n_slice": n, "weight": subw, "total": total, "total_source": "profile" if years else ("courtlistener" if total is not None else None),
            "recent": recent, "goodlaw": None if not gl else {
                "status": gl.get("status"), "yellow_year": gl.get("yellow_year"), "red_year": gl.get("red_year"),
                "read": gl.get("read"), "of": gl.get("of"), "mode": gl.get("mode") or "full", "window": gl.get("window"),
                "n_evidence": gl.get("n_evidence", len(gl.get("evidence") or [])),
                "evidence": (gl.get("evidence") or [])[:EVIDENCE_SHOWN] if gl.get("status") in ("red", "yellow") else []},
            "dormant": dormancy(prof, i, con, ref, end),
            "successor": (prof or {}).get("successor"),
            "also": also, "partners": partners, "parens": parens,
            "note": ent.get("slug"), "built": bool(ent.get("full")),
            "fit": fit_of(splits[ref], j, db, s, ucache) if splits is not None else
                   {"share": (subw or 0) / wtot if wtot else None, "best": True, "top": [], "total": None, "view_weight": True},
        })
    # order: best fit here first, then the rest; each by this sub-cluster's share of the case's co-citations
    cases.sort(key=lambda c: (not c["fit"]["best"], -(c["fit"]["share"] or 0), -c["weight"]))
    for i, c in enumerate(cases, 1):
        c["rank"] = i
    return {
        "schema": "1", "view": view, "slice": s, "slice_label": C.label(s), "partial": s + C.WIDTH - 1 >= cutoff,
        "sub": j, "code": "%s:%s" % (C.label(s), C.sub_label(j)), "split": j % 1000 != 0,
        "size": size, "internal": internal, "anchor": anchor, "anchor_name": short.get(anchor) or C.short(db, anchor),
        "citing_in_slice": srow[0] if srow else None,
        "label": {"name": lab[0], "description": lab[1], "status": st[0] if st else None} if lab else None,
        "name": lab[0] if lab else "around %s" % (short.get(anchor) or C.short(db, anchor)),
        "cluster": {"code": "%s:%d" % (C.label(s), cl), "name": cl_name, "size": cl_size[0] if cl_size else None,
                    "lineage": cl_lin},
        "lineage_id": lin, "lineage_span": [C.label(span[0]), C.label(span[1])] if span and span[0] else None,
        "lineage_note": vlin.get(lin),
        "area": {"id": area, "name": C.area_name(db, area), "note": varea.get(area)} if area else None,
        "data_date": stamp, "recent_window": [end - case2md.RECENT + 1, end],
        "lineage": lineage_data(db, s, j, lin, members, short),
        "fit_source": "cache" if splits is not None else why_no_split,
        "cases": cases,
        "generated": time.strftime("%Y-%m-%d"),
    }


# ================================================================ shared wording

def fmt(n):
    return "–" if n is None else format(n, ",")


def gl_word(c):
    g, d = c["goodlaw"], c["dormant"]
    parts = []
    if g and g.get("status"):
        y = g.get("red_year") if g["status"] == "red" else g.get("yellow_year") if g["status"] == "yellow" else None
        parts.append(g["status"] + ("?" if g.get("mode") == "quick" else "") + (" %s" % y if y else ""))
    else:
        parts.append("not calculated")
    if d and d["dormant"]:
        parts.append("dormant" + (" (last cited %s)" % d["last"] if d["last"] else " (never cited)"))
    return ", ".join(parts)


def gl_short(c):
    """The summary table's cell: 'red 2024', 'green · dormant', 'not calc.'."""
    g, d = c["goodlaw"], c["dormant"]
    if g and g.get("status"):
        y = g.get("red_year") if g["status"] == "red" else g.get("yellow_year") if g["status"] == "yellow" else None
        w = g["status"] + ("?" if g.get("mode") == "quick" else "") + (" %s" % y if y else "")
    else:
        w = "not calc."
    return w + (" · dormant" if d and d["dormant"] else "")


def gl_colour(c):
    g, d = c["goodlaw"], c["dormant"]
    if g and g.get("status") in ("red", "yellow"):
        return g["status"]
    if d and d["dormant"]:
        return "grey"
    return g["status"] if g and g.get("status") else None


def cited_line(c, d):
    t = "In %s: cited by %s decisions%s" % (d["slice_label"], fmt(c["n_slice"]), " (appellate)" if "appellate" in d["view"] else "")
    t += " · in all: %s" % fmt(c["total"])
    if c["recent"] is not None:
        t += " · %d–%d: %s" % (d["recent_window"][0], d["recent_window"][1], fmt(c["recent"]))
    t += " · weight in the sub-cluster %s" % fmt(round(c["weight"] or 0))
    return t


def pct(x):
    return "–" if x is None else "%.0f%%" % (100 * x)


def fit_line(c, d):
    f = c["fit"]
    if f.get("view_weight"):
        return "Fit: %s of its pair weight in %s is with this sub-cluster's members (best fit elsewhere unknown: %s)" % (
            pct(f["share"]), d["slice_label"], d["fit_source"])
    if f["share"] is None:
        return "Fit: no co-citations in %s that pass the slice's filters" % d["slice_label"]
    t = "Fit: %s of its co-citations in %s are with this sub-cluster's members" % (pct(f["share"]), d["slice_label"])
    if f["best"]:
        t += "; this is its best-fitting sub-cluster"
        if len(f["top"]) > 1:
            t += " (next: %s %s, %s)" % (f["top"][1]["unit"]["code"], f["top"][1]["unit"]["name"], pct(f["top"][1]["share"]))
    elif f["top"]:
        b = f["top"][0]
        t += "; fits best in %s %s (%s)" % (b["unit"]["code"], b["unit"]["name"], pct(b["share"]))
    return t


def case_groups(d):
    return [("Best fit here", [c for c in d["cases"] if c["fit"]["best"]]),
            ("Fit better in another sub-cluster", [c for c in d["cases"] if not c["fit"]["best"]])]


def cases_note(d):
    t = ("Ordered by fit: the share of each case's co-citations in %s that are with this sub-cluster's members "
         "(decisions citing both, from the co-citation cache; binary counts, so a sentence view's own weights can "
         "differ a little). Cases whose best-fitting sub-cluster is this one come first. " % d["slice_label"])
    if d.get("fit_source") != "cache":
        t = ("Ordered by the share of each case's pair weight inside this sub-cluster; the best fit elsewhere is unknown (%s). "
             % d.get("fit_source"))
    return t + ("In slice: decisions in %s citing it%s. In all: every citing case in the bulk data (cached profile) or "
                "CourtListener's count. Good law: red, yellow or green from the cached check; ? = quick check (the newest "
                "100 citing opinions: status now, no year); dormant = no citing decision %d or later." % (
                    d["slice_label"], " (appellate courts)" if "appellate" in d["view"] else "", d["recent_window"][0]))


def best_elsewhere(c):
    f = c["fit"]
    return None if f["best"] or not f["top"] else f["top"][0]


def court_year(c):
    return ", ".join(x for x in (c["court"], (c["date"] or "")[:4] or str(c["year"] or "")) if x)


def successor_text(c):
    sc = c.get("successor")
    if not sc:
        return None
    return "Role changed %s: likely successor %s (%.0f%% of the %s decisions citing this case in %s also cite it)" % (
        sc["slice"], sc["name"], 100 * sc["share"], fmt(sc["citing"]), sc["slice"])


def move_text(g):
    u = g["unit"]
    if u["code"]:
        return "%s %s" % (u["code"], u["name"])
    return ("in no cluster in %s (cited, but too loosely tied to any)" % C.label(u["slice"]) if u["node"] else
            "not a node in %s (cited too rarely then, or not yet decided)" % C.label(u["slice"]))


def side_heads(side, d):
    """(joined heading, left heading) for one side of the slice."""
    if side["side"] == "after" and not side["cell"]:
        p = " (a partial slice, where lineages are still forming)" if side["partial"] else ""
        return ("", "The lineage does not continue into %s%s; where these cases are there" % (side["label"], p))
    if side["side"] == "before" and not side["cell"]:
        return ("The lineage starts here; where its cases were in %s" % side["label"], "")
    if side["side"] == "before":
        return ("Joined since %s (not in the lineage's %s sub-cluster; where they were then)" % (side["label"], side["label"]),
                "Left since %s (in the lineage in %s, not here; where they are in %s)" % (side["label"], side["label"], d["slice_label"]))
    return ("Joining in %s (in the lineage's %s sub-cluster, not here; where they are in %s)" % (side["label"], side["label"], d["slice_label"]),
            "Leaving by %s (here, not in the lineage in %s; where they go)%s" % (
                side["label"], side["label"], " (a partial slice)" if side["partial"] else ""))


# ================================================================ Markdown

def render_md(d):
    e = lambda s: case2md.esc(str(s), "md")
    title = "Sub-cluster %s · %s" % (d["code"], d["name"])
    L = case2md.yaml_head([("title", title), ("aliases", [d["code"]]), ("slice", d["slice_label"]),
                           ("sub_cluster", d["code"]), ("lineage", d["lineage_id"]), ("view", d["view"]),
                           ("source", "cocite.py subdoc"), ("retrieved", case2md.Raw(d["generated"])),
                           ("synthetic", False)])
    L += ["", "# " + e(title), ""]
    lab = d["label"]
    if lab:
        L += ["> [!abstract] %s%s" % (e(lab["name"]), " · label %s" % lab["status"] if lab["status"] and lab["status"] != "stable" else ""),
              "> %s" % e(lab["description"]),
              "> *Label written once by a language model from script-built evidence (cocite_labels.json); not part of the record.*", ""]
    else:
        L += ["*No label: named after its anchor case.*", ""]
    where = ["- **Size:** %d cases%s; %s citing decisions in %s%s" % (
        d["size"], "" if d["split"] else " (an unsplit cluster)", fmt(d["citing_in_slice"]), d["slice_label"],
        " (partial slice)" if d["partial"] else "")]
    if d["split"]:
        where.append("- **Within cluster:** %s %s (%s cases)" % (d["cluster"]["code"], e(d["cluster"]["name"]), fmt(d["cluster"]["size"])))
    if d["lineage_id"]:
        ln = "[[%s]]" % d["lineage_note"] if d["lineage_note"] else "lineage %d" % d["lineage_id"]
        where.append("- **Lineage:** %s, %s – %s" % (ln, *(d["lineage_span"] or ["?", "?"])))
    if d["area"]:
        an = "[[%s]]" % d["area"]["note"] if d["area"]["note"] else e(d["area"]["name"])
        where.append("- **Area:** %s" % an)
    where.append("- **Anchor case:** %s" % e(d["anchor_name"]))
    where.append("- **View:** %s · CourtListener bulk data of %s · dormant = no citing decision %d–%d or later" % (
        d["view"], d["data_date"] or "?", *d["recent_window"]))
    L += where + [""]

    L += ["## Cases", "", "*%s*" % e(cases_note(d)), ""]
    for head, group in case_groups(d):
        if not group:
            continue
        L += ["### %s (%d)" % (head, len(group)), "", "| # | Case | Cite | Court, year | In slice | In all | Fit here | Best fit | Good law |",
              "|---|---|---|---|---|---|---|---|---|"]
        for c in group:
            nm = "[[%s\\|%s]]" % (c["note"], e(c["name"][:80])) if c["note"] else e(c["name"][:80])
            b = best_elsewhere(c)
            L.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                c["rank"], nm, e(c["cite"]), e(court_year(c)), fmt(c["n_slice"]), fmt(c["total"]), pct(c["fit"]["share"]),
                "%s %s (%s)" % (b["unit"]["code"], e(b["unit"]["name"]), pct(b["share"])) if b else "here", gl_word(c)))
        L.append("")

    ld = d["lineage"]
    if ld:
        L += ["## Lineage", "", "| Slice | Sub-cluster | Cases |", "|---|---|---|"]
        for cell in ld["cells"]:
            b = "**" if cell["here"] else ""
            L.append("| %s%s%s | %s | %d |" % (b, cell["label"], b, cell["code"], cell["size"]))
        L.append("")
        if ld["links"]:
            L += ["### Merges and splits", ""]
            for k in ld["links"]:
                L.append("- %s %s %s · %s shared, Jaccard %.2f%s" % (
                    "from" if k["dir"] == "in" else "to", k["other"]["code"], e(k["other"]["name"]), fmt(k["shared"]),
                    k["jaccard"], " (this lineage)" if k["same"] else ""))
            L.append("")
        for side in ld["sides"]:
            jh, lh = side_heads(side, d)
            for head, groups in ((jh, side["joined"]), (lh, side["left"])):
                n = sum(len(g["cases"]) for g in groups)
                if not n:
                    continue
                L += ["### %s: %d" % (head, n), ""]
                for g in groups:
                    L.append("- **%s** (%d): %s" % (e(move_text(g)), len(g["cases"]),
                                                    "; ".join(e(x["name"]) for x in g["cases"])))
                L.append("")

    L += ["## Case details", ""]
    prev = None
    for c in d["cases"]:
        if c["fit"]["best"] != prev and any(not x["fit"]["best"] for x in d["cases"]):
            prev = c["fit"]["best"]
            L += ["**%s**" % ("Best fit here" if prev else "Fit better in another sub-cluster"), ""]
        L += ["### %d. %s" % (c["rank"], e(c["name"])), ""]
        head = ", ".join(x for x in (e(c["cite"]), e(court_year(c))) if x)
        link = "[[%s]]" % c["note"] if c["note"] else "[CourtListener](%s/opinion/%d/x/)" % (case2md.CL, c["ref"])
        L.append("%s · %s%s" % (head, link, " · %s" % e(c["precedential"]) if c["precedential"] and c["precedential"] != "Published" else ""))
        L += ["", "- %s" % cited_line(c, d), "- %s" % e(fit_line(c, d)), "- **Good law:** %s%s" % (gl_word(c), gl_detail(c))]
        for ev in (c["goodlaw"] or {}).get("evidence") or []:
            L.append("  - %s (%s): %s “%s”%s — %s · %s" % (ev.get("year"), e(ev.get("court", "")), ev.get("kind"), e(ev.get("word", "")),
                                                        " · could overrule" if ev.get("above") else "", e(ev.get("name", "")[:60]),
                                                        e(ev.get("snippet", ""))))
        if successor_text(c):
            L.append("- %s. *Co-citation, not the text.*" % e(successor_text(c)))
        if c["also"]:
            L.append("- **Also ties to:** " + "; ".join("%s %s (%.0f%%)" % (a["code"], e(a["name"]), 100 * a["share"]) for a in c["also"]))
        if c["partners"]:
            L.append("- **Travels with:** " + "; ".join("%s (cosine %.2f, %s decisions citing both)" % (
                e(p["name"]), p["cosine"] or 0, fmt(p["both"])) for p in c["partners"]))
        pd = c["parens"]
        if pd and pd.get("groups"):
            L.append("- **How later courts describe it** (%s parentheticals; groups of similar wording, largest first; other courts' words):" % fmt(pd["total"]))
            for g in pd["groups"]:
                src = ", ".join(x for x in (e(g.get("case") or ""), g.get("court") or "", g.get("year") or "") if x)
                L.append("  1. **%d** · (%s)%s" % (g["n"], e(g["text"][:500]), " — *%s*" % src if src else ""))
        else:
            L.append("- *No parentheticals.*")
        L.append("")
    return "\n".join(L) + "\n"


def gl_detail(c, tex=False):
    g = c["goodlaw"]
    e = (lambda x: case2md.esc(x, "tex")) if tex else (lambda x: x)
    if g and g.get("mode") == "quick":
        return e(" (read the newest %s of %s citing opinions, %s: status now, not the year it changed)" % (
            fmt(g["read"]), fmt(g["of"]), case2md.quick_window(g)))
    if g and g.get("read") is not None:
        return e(" (read %s of %s citing opinions)" % (fmt(g["read"]), fmt(g["of"])))
    if not g:
        return " (Build, or subdoc %s, runs the check)" % ("\\texttt{-{}-goodlaw}" if tex else "`--goodlaw`")
    return ""


# ================================================================ TeX

TEX_COL = {"green": "green!55!black", "yellow": "yellow!85!orange", "red": "red!75!black", "grey": "black!40"}


def render_tex(d, font):
    from tribonian.render import PREAMBLE, RULES
    t = lambda s: case2md.esc(str(s), "tex")
    title = "Sub-cluster %s" % d["code"]
    src = "view %s \\textbullet\\ CourtListener bulk data of %s \\textbullet\\ built %s" % (
        t(d["view"]), t(d["data_date"] or "?"), t(d["generated"]))
    pre = (PREAMBLE.replace("__FONTPKG__", font).replace("__RULES__", RULES["boxes"])
           .replace("__DOCTITLE__", t(title)).replace("__DOCHEAD__", t(d["name"])).replace("__SRCLINE__", src))
    pre = pre.replace("\\begin{document}", case2md.TOC_DEFS + "\\begin{document}", 1)
    B = []

    def sq(c):                       # no colour squares: the words carry it
        return ""

    # about
    lab = d["label"]
    about = []
    if lab:
        about.append("\\textbf{%s}%s\\par %s\\par{\\scriptsize\\itshape Label written once by a language model from "
                     "script-built evidence (cocite\\_labels.json); not part of the record.\\par}\\vspace{3pt}" % (
                         t(lab["name"]), " {\\scriptsize(label %s)}" % t(lab["status"]) if lab["status"] and lab["status"] != "stable" else "",
                         t(lab["description"])))
    else:
        about.append("{\\itshape No label: named after its anchor case.}\\par\\vspace{3pt}")
    about.append("\\textbf{Size:} %d cases%s; %s citing decisions in %s%s.\\par" % (
        d["size"], "" if d["split"] else " (an unsplit cluster)", fmt(d["citing_in_slice"]), d["slice_label"],
        " (partial slice)" if d["partial"] else ""))
    if d["split"]:
        about.append("\\textbf{Within cluster:} %s %s (%s cases).\\par" % (d["cluster"]["code"], t(d["cluster"]["name"]), fmt(d["cluster"]["size"])))
    if d["lineage_id"]:
        about.append("\\textbf{Lineage:} %d, %s -- %s.\\par" % (d["lineage_id"], *(d["lineage_span"] or ["?", "?"])))
    if d["area"]:
        about.append("\\textbf{Area:} %s.\\par" % t(d["area"]["name"]))
    about.append("\\textbf{Anchor case:} %s.\\par" % t(d["anchor_name"]))
    about.append("{\\scriptsize Good law: red, yellow or green from the cached check; ? = quick check (the newest 100 citing "
                 "opinions: status now, no year); dormant = no citing decision %d or later; not calc. = never checked "
                 "(Build, or subdoc \\texttt{-{}-goodlaw}).\\par}" % d["recent_window"][0])
    B.append("\\pdfbookmark[1]{About}{about}\\recordpath{About}\n"
             "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=violet!40!black, boxrule=0.4pt, arc=1.5pt,\n"
             "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n" % (
                 ABOUT_COLOR, "\n".join(about)))

    # summary table = the contents
    B.append("\\hypertarget{contents}{}\\pdfbookmark[1]{Cases}{contents}\\recordpath{Cases}\n"
             "{\\large\\bfseries Cases}\\par{\\scriptsize %s Tap a name for its details; the \\ensuremath{\\uparrow} "
             "in the margin there comes back here.\\par}\\vspace{2pt}\n" % t(cases_note(d)))
    for gi, (head, group) in enumerate(case_groups(d)):
        if not group:
            continue
        rows = []
        for c in group:
            b = best_elsewhere(c)
            name = "\\hyperlink{c%d}{%s}" % (c["ref"], t(C.trim(c["name"], 70)))
            if b:
                name += "\\newline{\\scriptsize\\itshape best: %s %s (%s)}" % (b["unit"]["code"], t(C.trim(b["unit"]["name"], 60)), t(pct(b["share"])))
            rows.append("%d & %s & %s & %s & %s & %s & %s \\\\" % (
                c["rank"], name, t(court_year(c)), fmt(c["n_slice"]), fmt(c["total"]), t(pct(c["fit"]["share"])), t(gl_short(c))))
        B.append("\\pdfbookmark[2]{%s}{grp%d}\\vspace{3pt}{\\bfseries %s (%d)}\\par\n"
                 "{\\footnotesize\\setlength{\\tabcolsep}{3pt}\n"
                 "\\begin{longtable}{@{}r p{200pt} p{66pt} r r r p{58pt}@{}}\n"
                 "\\# & Case & Court, year & In slice & In all & Fit & Good law \\\\ \\hline\\endhead\n%s\n"
                 "\\end{longtable}}\n" % (t(head), gi, t(head), len(group), "\n".join(rows)))

    # lineage
    ld = d["lineage"]
    if ld:
        L = ["\\clearpage\\pdfbookmark[1]{Lineage}{lineage}\\recordpath{Lineage}{\\large\\bfseries Lineage %d}\\par\\vspace{2pt}" % ld["lineage"],
             "{\\small " + " \\textbullet\\ ".join(("\\textbf{%s %d}" if cell["here"] else "%s %d") % (cell["code"], cell["size"])
                                                for cell in ld["cells"]) + "\\par}\\vspace{4pt}"]
        if ld["links"]:
            L.append("{\\small\\textbf{Merges and splits}\\par}")
            L += ["{\\footnotesize %s %s %s \\textbullet\\ %s shared, Jaccard %.2f%s\\par}" % (
                "from" if k["dir"] == "in" else "to", k["other"]["code"], t(k["other"]["name"]), fmt(k["shared"]), k["jaccard"],
                " (this lineage)" if k["same"] else "") for k in ld["links"]]
            L.append("\\vspace{4pt}")
        for side in ld["sides"]:
            jh, lh = side_heads(side, d)
            for head, groups in ((jh, side["joined"]), (lh, side["left"])):
                n = sum(len(g["cases"]) for g in groups)
                if not n:
                    continue
                L.append("{\\small\\textbf{%s: %d}\\par}" % (t(head), n))
                for g in groups:
                    names = "; ".join(("\\hyperlink{c%d}{%s}" % (x["ref"], t(x["name"])) if x["ref"] in d["_members"] else t(x["name"]))
                                      for x in g["cases"])
                    L.append("{\\footnotesize\\hangindent=1em\\textit{%s} (%d): %s\\par}" % (t(move_text(g)), len(g["cases"]), names))
                L.append("\\vspace{3pt}")
        B.append("\n".join(L) + "\n")

    # case details
    B.append("\\clearpage\\pdfbookmark[1]{Case details}{details}\\recordpath{Case details}{\\large\\bfseries Case details}\\par\\vspace{3pt}\n")
    prev = None
    for c in d["cases"]:
        if c["fit"]["best"] != prev and any(not x["fit"]["best"] for x in d["cases"]):
            prev = c["fit"]["best"]
            head = "Best fit here" if prev else "Fit better in another sub-cluster"
            B.append("\\pdfbookmark[1]{%s}{det%d}\\vspace{6pt}{\\bfseries %s}\\par\n" % (head, int(prev), head))
        x = ["\\hypertarget{c%d}{}\\pdfbookmark[2]{%d. %s}{d%d}\\recordpath{%d. %s}\\tocback%s\\textbf{%d. %s}\\par" % (
            c["ref"], c["rank"], t(C.trim(c["short"], 60)), c["ref"], c["rank"], t(C.trim(c["short"], 60)), sq(c), c["rank"], t(c["name"])),
            "{\\footnotesize %s%s%s\\par}" % (t(", ".join(y for y in (c["cite"], court_year(c)) if y)),
                                             " \\textbullet\\ note built" if c["built"] else "",
                                             " \\textbullet\\ %s" % t(c["precedential"]) if c["precedential"] and c["precedential"] != "Published" else ""),
            "{\\footnotesize %s\\par}" % t(cited_line(c, d)),
            "{\\footnotesize %s\\par}" % t(fit_line(c, d)),
            "{\\footnotesize\\textbf{Good law:} %s%s\\par}" % (t(gl_word(c)), gl_detail(c, tex=True))]
        for ev in (c["goodlaw"] or {}).get("evidence") or []:
            x.append("{\\scriptsize\\hangindent=1em %s (%s): %s ``%s''%s --- \\emph{%s}: %s\\par}" % (
                t(ev.get("year", "")), t(ev.get("court", "")), t(ev.get("kind", "")), t(ev.get("word", "")),
                ", could overrule" if ev.get("above") else "", t(ev.get("name", "")[:60]), t(C.trim(ev.get("snippet", ""), 300).replace("⟦", "[").replace("⟧", "]"))))
        if successor_text(c):
            x.append("{\\footnotesize %s. {\\scriptsize(co-citation, not the text)}\\par}" % t(successor_text(c)))
        if c["also"]:
            x.append("{\\footnotesize\\textbf{Also ties to:} %s\\par}" % "; ".join(
                "%s %s (%.0f\\%%)" % (a["code"], t(a["name"]), 100 * a["share"]) for a in c["also"]))
        if c["partners"]:
            x.append("{\\footnotesize\\textbf{Travels with:} %s\\par}" % "; ".join(
                "\\hyperlink{c%d}{%s} (cosine %.2f, %s both)" % (p["ref"], t(p["name"]), p["cosine"] or 0, fmt(p["both"])) for p in c["partners"]))
        pd = c["parens"]
        if pd and pd.get("groups"):
            items = "".join("\\item[\\textbf{%d}] (%s)%s\n" % (
                g["n"], t(g["text"][:500]),
                " {\\scriptsize --- \\emph{%s}}" % t(", ".join(y for y in (g.get("case") or "", g.get("court") or "", g.get("year") or "") if y))
                if g.get("case") else "") for g in pd["groups"])
            x.append("{\\footnotesize\\textbf{How later courts describe it} {\\scriptsize(%s parentheticals; groups of similar "
                     "wording, largest first; other courts' words)}\\par}\n"
                     "{\\footnotesize\\begin{list}{}{\\leftmargin=2.2em\\labelwidth=1.8em\\labelsep=0.4em\\itemsep=1pt\\parsep=0pt\\topsep=1pt}\n%s"
                     "\\end{list}}" % (fmt(pd["total"]), items))
        else:
            x.append("{\\footnotesize\\itshape No parentheticals.\\par}")
        B.append("\\begin{tcolorbox}[enhanced, breakable, colback=white, colframe=black!35, boxrule=0.3pt, arc=0pt,\n"
                 "  left=4pt, right=4pt, top=3pt, bottom=3pt, before skip=4pt, after skip=0pt]\n%s\n\\end{tcolorbox}\n" % "\n".join(x))
    return pre + "".join(B) + "\n\\end{document}\n"


# ================================================================ driver

def out_stem(d):
    return C.BAD_CHARS.sub("-", "%s %s - %s" % (d["slice_label"], C.sub_label(d["sub"]), C.trim(d["name"], 80))).strip()


def out_dirs(d):
    rel = ([] if d["view"] == C.NOTE_VIEW else [d["view"]]) + [d["slice_label"]]
    return (case2md.ROOT / "md-cocite" / "Sub-clusters").joinpath(*rel), (case2md.ROOT / "pdf" / "Co-citation").joinpath(*rel)


def cmd_subdoc(args):
    db, view = open_db(args)
    s, j = resolve(db, args.which)
    t0 = time.time()
    d = gather(db, view, s, j, goodlaw=args.goodlaw)
    if args.json:
        json.dump(d, sys.stdout, indent=1, ensure_ascii=False)
        print()
        return
    d["_members"] = {c["ref"] for c in d["cases"]}
    md_dir, pdf_dir = out_dirs(d)
    stem = out_stem(d)
    for old in list(md_dir.glob("%s %s - *.md" % (d["slice_label"], C.sub_label(j)))):   # label renamed: one note per unit
        if old.stem != stem:
            print("  note: an older note for this sub-cluster is %s (remove it by hand)" % old.name)
    md_dir.mkdir(parents=True, exist_ok=True)
    md_path = md_dir / (stem + ".md")
    md_path.write_text(render_md(d), encoding="utf-8")
    print("wrote %s" % md_path.relative_to(case2md.ROOT))
    build = case2md.ROOT / "build"
    build.mkdir(exist_ok=True)
    tex_path = build / ("subdoc-%s-%d-%d.tex" % (view, s, j))       # no spaces: pdflatex jobname
    tex_path.write_text(render_tex(d, args.font), encoding="utf-8")
    print("wrote %s" % tex_path.relative_to(case2md.ROOT))
    n_gl = sum(1 for c in d["cases"] if c["goodlaw"])
    n_dorm = sum(1 for c in d["cases"] if c["dormant"] and c["dormant"]["dormant"])
    print("  %d cases · good law cached for %d · dormant %d · parentheticals for %d" % (
        len(d["cases"]), n_gl, n_dorm, sum(1 for c in d["cases"] if c["parens"] and c["parens"].get("groups"))))
    if not args.no_pdf:
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf = case2md.run_pdflatex(tex_path, pdf_dir)
        if pdf:
            final = pdf_dir / (stem + ".pdf")
            os.replace(pdf, final)
            print("wrote %s" % final.relative_to(case2md.ROOT))
    case2md.unmapped_note()
    print("  (%.1f s)" % (time.time() - t0))
