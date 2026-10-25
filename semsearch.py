#!/usr/bin/env python3
"""semsearch.py -- CourtListener semantic search, re-ranked and joined to the co-citation clusters.

    ./casemap.py semsearch "an agency that reverses a rule must explain why"
    ./casemap.py semsearch --file draft.md            one search per paragraph (blank-line separated)
    ./casemap.py semsearch "..." --window 1983-01-01:1983-12-31 --court scotus

What it does, per paragraph:
  1. one search per date window. Windows are the five-year co-citation slices (the view's slices
     table, so they line up with the clusters); by default only the newest slice, which costs
     --pages requests. --since YEAR widens it to every slice ending in or after YEAR (--since 1700:
     all of them); --window START:END sets windows by hand
  2. --pages pages per window (default 2, 20 results each), following CourtListener's `next` cursor
  3. everything re-ranked by meta.score.semantic. The server's own order follows the keyword (bm25)
     score with a recency boost, so it is ignored
  4. duplicate records of one decision collapsed (same name without "Revisions: ...", same date)
  5. each hit joined to its sub-cluster and area in the co-citation view (cocite.py), as of the
     newest complete slice the case appears in, the same position its case note shows
  6. writes cases/semsearch/<slug>/results.json (beside cases/citedby/<cluster>/)

API cost: windows x pages requests per paragraph, through case2md's shared throttle (5/min,
50/hour, 125/day free tier; CL_LIMITS for members). Every raw response is cached under
cases/semsearch/_cache/, keyed by (query, court, window, page), so a rerun costs nothing and a run
stopped at the daily limit resumes where it stopped.
"""
import argparse, hashlib, json, re, sqlite3, sys, time, urllib.parse
from datetime import date
from pathlib import Path

import case2md as c

OUT = c.CASES / "semsearch"
CACHE = OUT / "_cache"
MIN_CHARS = 60          # shorter paragraphs (headings, "ARGUMENT") are skipped, not searched


# ================================================================ windows

def slice_windows(view=None):
    """The co-citation slices as date windows [(after, before, label)], oldest first. None when the
    view is not built (then --since or --window is needed)."""
    try:
        import cocite
        db = cocite.open_view(view or cocite.NOTE_VIEW)
    except Exception:
        return None
    if db is None:
        return None
    w = cocite.WIDTH
    return [("%d-01-01" % s, "%d-12-31" % (s + w - 1), cocite.label(s))
            for (s,) in db.execute("SELECT slice FROM slices ORDER BY slice")]


def windows_for(args):
    if args.window:
        out = []
        for t in args.window:
            a, _, b = t.partition(":")
            if not (re.fullmatch(r"\d{4}-\d\d-\d\d", a) and re.fullmatch(r"\d{4}-\d\d-\d\d", b)):
                sys.exit("--window takes YYYY-MM-DD:YYYY-MM-DD, not %r" % t)
            out.append((a, b, "%s..%s" % (a, b)))
        return out
    wins = slice_windows(args.courts)
    if wins is None:                   # no view built: the same five-year grid, up to this year
        import cocite
        w, now = cocite.WIDTH, date.today().year
        s0 = (args.since or now) - (args.since or now) % w
        wins = [("%d-01-01" % s, "%d-12-31" % (s + w - 1), cocite.label(s)) for s in range(s0, now + 1, w)]
    if args.since:
        return [x for x in wins if int(x[1][:4]) >= args.since]
    return wins[-1:]                   # default: the newest slice only


def us_date(iso):
    """CourtListener's filed_after/filed_before take MM/DD/YYYY."""
    y, m, d = iso.split("-")
    return "%s/%s/%s" % (m, d, y)


# ================================================================ fetching (cached)

def cache_path(q, court, win, page):
    key = json.dumps([q, court or "", win[0], win[1], page])
    return CACHE / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def fetch_window(q, court, win, pages, tok, stats):
    """Raw results for one window, pages 1..pages. Page n's URL is the `next` cursor stored in page
    n-1's cached response, so the cache alone can replay a whole window."""
    hits, url = [], None
    for page in range(1, pages + 1):
        p = cache_path(q, court, win, page)
        if p.exists():
            res = json.loads(p.read_text())
            stats["cached"] += 1
        else:
            if page == 1:
                params = {"type": "o", "semantic": "true", "q": q,
                          "filed_after": us_date(win[0]), "filed_before": us_date(win[1])}
                if court:
                    params["court"] = court
                url = "%s/search/?%s" % (c.API, urllib.parse.urlencode(params))
            res = c.call(url, tok)
            CACHE.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"url": url, "fetched": time.strftime("%Y-%m-%d %H:%M"),
                                     "next": res.get("next"), "count": res.get("count"),
                                     "results": res.get("results") or []}))
            res = json.loads(p.read_text())
            stats["fetched"] += 1
        # _srank: where the server put it in this window (its bm25 order), kept to compare with ours
        hits += [dict(h, _window=win[2], _srank=len(hits) + j + 1) for j, h in enumerate(res.get("results") or [])]
        url = res.get("next")
        if not url:
            break
    return hits


# ================================================================ ranking

REVISIONS = re.compile(r"[\s.,;:-]*\(?\s*revisions?\s*:.*$", re.I)


def name_key(name):
    """'Loper Bright ... Raimondo ... Revisions: 6/27/24' and the plain record compare equal."""
    n = REVISIONS.sub("", name or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def semantic(h):
    s = (h.get("meta") or {}).get("score") or {}
    return s.get("semantic") if isinstance(s.get("semantic"), (int, float)) else float("-inf")


def rerank(raw):
    """Collapse duplicates, then sort by semantic score. Within a group of records of one decision the
    record with the most citations is kept (it is the one the citation map knows best); the group's
    score is its best record's score, and the other records' cluster ids are kept in 'also'."""
    groups = {}
    for h in raw:
        if not h.get("cluster_id"):
            continue
        k = (name_key(h.get("caseName")), h.get("dateFiled") or "")
        groups.setdefault(k, []).append(h)
    out = []
    for hs in groups.values():
        keep = max(hs, key=lambda h: (h.get("citeCount") or 0, semantic(h)))
        best = max(hs, key=semantic)
        ids = sorted({h["cluster_id"] for h in hs})
        out.append({"cluster_id": keep["cluster_id"], "case_name": REVISIONS.sub("", keep.get("caseName") or "").strip(),
                    "date": keep.get("dateFiled") or "", "court": keep.get("court_citation_string") or keep.get("court_id") or "",
                    "citation": (keep.get("citation") or [None])[0], "cite_count": keep.get("citeCount") or 0,
                    "semantic": round(semantic(best), 4), "bm25": (best.get("meta") or {}).get("score", {}).get("bm25"),
                    "window": best["_window"], "server_rank": best["_srank"],
                    "also": [i for i in ids if i != keep["cluster_id"]]})
    out.sort(key=lambda r: -r["semantic"])
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


# ================================================================ joining to the co-citation view

def cluster_join(ids_list, view=None):
    """cluster id -> {sub, sub_name, cluster, cluster_name, area, area_name, slice}, read from the
    co-citation view with cocite.py's own helpers (so the table layout lives in one place). Position =
    the newest complete slice the case is in, as in its case note. A case cited too rarely to be in
    any slice gets nothing. ids_list: per hit, all its record ids (a duplicate record may be the one
    the citation map uses)."""
    try:
        import cocite
        db = cocite.open_view(view or cocite.NOTE_VIEW)
    except Exception:
        db = None
    if db is None:
        return {}, "no co-citation view built: hits carry no sub-cluster or area"
    year = cocite.newest_full_slice(db)
    out = {}
    for ids in ids_list:
        for ref in ids:
            rows = db.execute("SELECT slice, cl, sub FROM nodes WHERE ref=? AND cl > 0 ORDER BY slice", (ref,)).fetchall()
            if not rows:
                continue
            full = [r for r in rows if r[0] + cocite.WIDTH - 1 < year] or rows
            s, cl, sub = full[-1]
            c_name, c_lin, _ = cocite.unit_name(db, s, cl)
            # slice_start / cl_code / sub_code: the raw numbers, for the GUI's links to those pages
            info = {"slice": cocite.label(s), "cluster": "%s:%d" % (cocite.label(s), cl), "cluster_name": c_name,
                    "sub": None, "sub_name": None, "sub_lineage": None, "area": None, "area_name": None,
                    "slice_start": s, "cl_code": cl, "sub_code": None}
            lin = c_lin
            if sub and sub % 1000:                     # j = 0: a cluster too small to split
                s_name, s_lin, _ = cocite.unit_name(db, s, sub, "sub")
                info.update(sub="%s:%s" % (cocite.label(s), cocite.sub_label(sub)), sub_name=s_name, sub_lineage=s_lin,
                            sub_code=sub)
                lin = s_lin or c_lin
            try:
                ar = cocite.area_of(db, lin)
                info.update(area=ar, area_name=cocite.area_name(db, ar))
            except sqlite3.OperationalError:          # areas not built yet
                pass
            out[ids[0]] = dict(info, ref=ref)
            break
    return out, None


def rollup(hits):
    """Hits grouped by sub-cluster (by its lineage, so the same sub-cluster in two slices is one
    group); a hit in an unsplit cluster groups under the cluster."""
    g = {}
    for h in hits:
        k = h.get("sub_lineage") or h.get("sub") or h.get("cluster")
        if not k:
            continue
        e = g.setdefault(k, {"sub": h.get("sub") or h.get("cluster"), "name": h.get("sub_name") or h.get("cluster_name"),
                             "area_name": h.get("area_name"), "slice_start": h.get("slice_start"),
                             "cl_code": h.get("cl_code"), "sub_code": h.get("sub_code"),
                             "hits": 0, "score_sum": 0.0, "best_rank": h["rank"], "cases": []})
        e["hits"] += 1
        e["score_sum"] = round(e["score_sum"] + h["semantic"], 4)
        e["best_rank"] = min(e["best_rank"], h["rank"])
        e["cases"].append(h["cluster_id"])
    return sorted(g.values(), key=lambda e: (-e["hits"], -e["score_sum"]))


# ================================================================ command

def paragraphs(args):
    """The texts to search: --file's paragraphs, or the query's (a query with blank lines in it is
    several paragraphs too, which is how the GUI passes a pasted draft). Paragraphs shorter than
    --min-chars are skipped only when there are several (they are headings); a lone short query runs."""
    if args.file:
        text, stem = Path(args.file).read_text(encoding="utf-8"), Path(args.file).stem
    elif args.query:
        text, stem = args.query, None
    else:
        sys.exit("give a query, or --file with paragraphs")
    paras = [p for p in (re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n\s*\n", text)) if p]
    if len(paras) > 1:
        keep = [p for p in paras if len(p) >= args.min_chars]
        if len(keep) < len(paras):
            print("skipping %d paragraph(s) under %d characters (headings)" % (len(paras) - len(keep), args.min_chars))
        paras = keep
    if not paras:
        sys.exit("nothing to search")
    return paras, stem or " ".join(paras[0].split()[:6])


def run(args):
    paras, stem = paragraphs(args)
    wins = windows_for(args)
    if not wins:
        sys.exit("no windows left to search")
    planned = sum(1 for q in paras for w in wins for p in range(1, args.pages + 1)
                  if not cache_path(q, args.court, w, p).exists())
    print("%d paragraph(s) x %d window(s) x up to %d page(s): at most %d API request(s) not cached (%d left today)" % (
        len(paras), len(wins), args.pages, planned, c.budget_left()))
    if planned > c.budget_left():
        print("  more than today's budget: it will stop at the daily limit; rerun the same command to resume")
    tok = c.token() if planned else None
    stats = {"fetched": 0, "cached": 0}
    results = []
    for i, q in enumerate(paras, 1):
        raw = []
        for w in wins:
            raw += fetch_window(q, args.court, w, args.pages, tok, stats)
        hits = rerank(raw)
        join, note = cluster_join([[h["cluster_id"]] + h["also"] for h in hits], args.courts)
        for h in hits:
            j = join.get(h["cluster_id"]) or {}
            h.update({k: j.get(k) for k in ("slice", "cluster", "cluster_name", "sub", "sub_name", "sub_lineage", "area",
                                            "area_name", "slice_start", "cl_code", "sub_code")})
        results.append({"paragraph": i, "text": q, "raw_hits": len(raw), "hits": hits, "by_subcluster": rollup(hits),
                        **({"note": note} if note else {})})
        print("\n[%d] %s" % (i, q[:100] + ("..." if len(q) > 100 else "")))
        for h in hits[:args.top]:
            print("  %2d  %.3f  %s  %-60s %s" % (h["rank"], h["semantic"], h["date"][:4], h["case_name"][:60],
                                               h["sub_name"] or ""))
    slug = c.slugify(stem, 40) + "-" + hashlib.sha256(json.dumps([paras, args.court, wins, args.pages]).encode()).hexdigest()[:8]
    out = (Path(args.out) if args.out else OUT / slug) / "results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    rec = {"searched": time.strftime("%Y-%m-%d %H:%M"), "source": args.file or None, "court": args.court,
           "pages": args.pages, "windows": [{"after": a, "before": b, "label": l} for a, b, l in wins],
           "requests": stats, "paragraphs": results}
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(out)
    print("\n%d request(s) made, %d from cache\nwrote %s" % (stats["fetched"], stats["cached"], out))
    return out


def add_args(p):
    p.add_argument("query", nargs="?", help="natural-language text to search for (blank lines separate paragraphs)")
    p.add_argument("--file", help="or a text/markdown file: one search per blank-line-separated paragraph")
    p.add_argument("--court", help="CourtListener court id(s), e.g. scotus, or 'scotus ca1' (default: all courts)")
    p.add_argument("--pages", type=int, default=2, help="pages of 20 per window (default 2)")
    p.add_argument("--window", action="append", metavar="START:END",
                   help="search this window (YYYY-MM-DD:YYYY-MM-DD) instead of the slices; repeatable")
    p.add_argument("--since", type=int, metavar="YEAR",
                   help="search every slice ending in or after YEAR (default: the newest slice only; 1700 = all)")
    p.add_argument("--courts", help="co-citation view for the windows and the join (default: the case notes' view)")
    p.add_argument("--out", metavar="DIR", help="write DIR/results.json (default cases/semsearch/<slug>/)")
    p.add_argument("--top", type=int, default=10, help="hits printed per paragraph (results.json has all)")
    p.add_argument("--min-chars", dest="min_chars", type=int, default=MIN_CHARS,
                   help="--file: skip paragraphs shorter than this (default %d)" % MIN_CHARS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_args(ap)
    run(ap.parse_args())
