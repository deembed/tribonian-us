"""Dockets: the court tree and the index of fetched dockets that the GUI's View Dockets screen reads.

    tribonian dockets courts      # the court tree -> cases/dockets/_courts.json
    tribonian dockets index       # what is fetched and built -> cases/recap/_index.json (fetch and build refresh it);
                                  # moves dockets fetched before the court folders into cases/recap/<court>/
    tribonian dockets opinions --court md [--pages N] [--json]
                                  # a court's opinions, newest first (local index, else CourtListener)
    tribonian dockets search brown fcc [--court ca4] [--json]
                                  # fetched dockets, then CourtListener's (not in offline mode)

The tree: Federal (Supreme Court, courts of appeals, district, bankruptcy, special courts) and every state
(highest, appellate, trial, special courts), from the local courts table (tribonian db load courts), else
CourtListener's courts API. Fetching dockets (RECAP) works for federal appeals and district courts; the rest
of the tree is there to be filled in.
"""
import argparse, hashlib, json, re, sys, time, urllib.parse
from datetime import date
from pathlib import Path

from tribonian import case2md as c
from tribonian import statecites, tconfig

COURTS_JSON = c.CASES / "dockets" / "_courts.json"
INDEX_JSON = c.RECAP / "_index.json"

# CourtListener's jurisdiction codes -> (federal or state, tier)
TIER = {"F": ("federal", "appeals"), "FD": ("federal", "district"), "FB": ("federal", "bankruptcy"),
        "FBP": ("federal", "bankruptcy"), "FS": ("federal", "special"),
        "S": ("state", "highest"), "SA": ("state", "appellate"), "ST": ("state", "trial"), "SS": ("state", "special")}
FED_TIERS = ["supreme", "appeals", "district", "bankruptcy", "special"]
STATE_TIERS = ["highest", "appellate", "trial", "special"]


def court_rows():
    """[(id, short name, full name, citation string, jurisdiction)], where they came from, and the open database
    (None from the API)."""
    con = c.local_db()
    if con is not None:
        try:
            rows = con.execute("SELECT id, short_name, full_name, citation_string, jurisdiction FROM courts").fetchall()
            if rows:
                return rows, "local courts table", con
        except Exception:
            pass
    tok, rows = c.token(), []
    url = "%s/courts/?fields=id,short_name,full_name,citation_string,jurisdiction,in_use" % c.API
    while url:
        page = c.call(url, tok)
        rows += [(r["id"], r.get("short_name"), r.get("full_name"), r.get("citation_string"), r.get("jurisdiction"))
                 for r in page.get("results") or [] if r.get("in_use", True)]
        url = page.get("next")
    return rows, "CourtListener courts API", None


def circuit_key(cid):
    """ca1 .. ca11, then cadc, cafc."""
    m = re.match(r"ca(\d+)$", cid)
    return (0, int(m.group(1))) if m else (1, cid)


def courts(args):
    rows, source, con = court_rows()
    fed = {t: [] for t in FED_TIERS}
    states = {code: {"name": name, **{t: [] for t in STATE_TIERS}} for code, (name, _) in statecites.STATES.items()}
    for cid, short, full, cite, jur in rows:
        if jur not in TIER:
            continue                                # attorneys general, committees, international, tribal, test
        side, tier = TIER[jur]
        court = {"id": cid, "name": full or short or cid, "short": short or "", "cite": cite or ""}
        if cid == "scotus":
            fed["supreme"].append(court)
        elif side == "federal":
            fed[tier if (tier != "appeals" or re.match(r"ca(\d+|dc|fc)$", cid)) else "special"].append(court)
        else:
            for st in statecites.court_states(cid, con) or []:
                if st in states:
                    states[st][tier].append(court)
    fed["appeals"].sort(key=lambda x: circuit_key(x["id"]))
    for lst in [fed[t] for t in FED_TIERS if t != "appeals"] + [s[t] for s in states.values() for t in STATE_TIERS]:
        lst.sort(key=lambda x: x["name"])
    out = {"made": date.today().isoformat(), "source": source, "federal": fed,
           "state": dict(sorted(states.items(), key=lambda kv: kv[1]["name"]))}
    COURTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    COURTS_JSON.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    n = sum(len(v) for v in fed.values()) + sum(len(s[t]) for s in states.values() for t in STATE_TIERS)
    print("%d courts from the %s -> %s" % (n, source, COURTS_JSON.relative_to(c.ROOT)))


def built_files():
    """note stem -> note path, PDF stem -> PDF path, under md-cases/ and pdf/ (relative to the work folder)."""
    notes = {p.stem: p for p in c.NOTES.rglob("*.md")} if c.NOTES.is_dir() else {}
    pdfroot = c.ROOT / "pdf"
    pdfs = {p.stem: p for p in pdfroot.rglob("*.pdf")} if pdfroot.is_dir() else {}
    return notes, pdfs


def pacer_url(court, r):
    """The document on PACER (a login, and charged per page). District and bankruptcy courts serve documents
    at /doc1/, the courts of appeals at /docs1/."""
    if not r.get("pacer_doc_id"):
        return ""
    path = "docs1" if re.match(r"ca(\d+|dc|fc)$", court or "") else "doc1"
    return "https://ecf.%s.uscourts.gov/%s/%s" % (court, path, r["pacer_doc_id"])


def move_to_court_folders(quiet=False):
    """Dockets fetched before the court folders sit at cases/recap/ itself: move each into cases/recap/<court>/."""
    moved = 0
    for p in sorted(c.RECAP.glob("*.json")) if c.RECAP.is_dir() else []:
        if p.name.startswith("_"):
            continue
        try:
            court = json.loads(p.read_text(encoding="utf-8"))["docket"].get("court_id") or "court"
        except (ValueError, KeyError, TypeError):
            continue
        dest = c.RECAP / court / p.name
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        p.replace(dest)
        moved += 1
        if not quiet:
            print("moved %s -> %s" % (p.relative_to(c.ROOT), dest.relative_to(c.ROOT)))
    return moved


def index(args=None, quiet=False):
    """One summary per fetched docket (no document text), for the GUI. Moves dockets fetched before the
    court folders into them first."""
    move_to_court_folders(quiet)
    notes, pdfs = built_files()
    rel = lambda p: str(p.relative_to(c.ROOT)) if p else ""
    out = []
    for p in c.recap_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            d = data["docket"]
        except (ValueError, KeyError, TypeError):
            continue
        dslug = c.docket_slug(d)
        docs = []
        for r in data.get("documents") or []:
            key = c.doc_key(r)
            text = bool((r.get("plain_text") or "").strip())
            docs.append({"key": key, "filed": r.get("entry_date_filed") or "", "pages": r.get("page_count"),
                         "description": c.clean(r.get("description") or r.get("short_description") or ""),
                         "status": "text" if text else ("recap" if r.get("is_available") else "pacer"),
                         "order": bool(c.ORDER_RE.match(r.get("description") or r.get("short_description") or "")),
                         "pacer": pacer_url(d.get("court_id"), r),
                         "courtlistener": c.CL + (r.get("absolute_url") or "") if r.get("absolute_url") else "",
                         "note": rel(notes.get(c.doc_slug(dslug, r)))})
        out.append({"file": rel(p), "id": d.get("id"), "court": d.get("court_id") or "",
                    "number": d.get("docket_number") or "", "name": d.get("case_name") or "",
                    "filed": d.get("date_filed") or "", "terminated": d.get("date_terminated") or "",
                    "nature": d.get("nature_of_suit") or d.get("cause") or "",
                    "courtlistener": c.CL + (d.get("absolute_url") or "") if d.get("absolute_url") else "",
                    "note": rel(notes.get(dslug)),
                    "pdfs": sorted(rel(v) for k, v in pdfs.items() if k == dslug or k.startswith(dslug + "-ecf-")),
                    "documents": docs})
    INDEX_JSON.parent.mkdir(parents=True, exist_ok=True)
    INDEX_JSON.write_text(json.dumps({"made": date.today().isoformat(), "dockets": out}, indent=1, ensure_ascii=False),
                          encoding="utf-8")
    if not quiet:
        print("%d dockets -> %s" % (len(out), INDEX_JSON.relative_to(c.ROOT)))


NUMBER_RE = re.compile(r"^(?:\d+:)?\d{2}-[\w-]*\d+$")          # 26-2230, 1:26-cv-00376


def words_match(q, *fields):
    """Every word of the query in one of the fields (case-insensitive)."""
    hay = " ".join(f or "" for f in fields).lower()
    return all(w in hay for w in re.findall(r"[\w.:-]+", q.lower()))


def api_pages(params, pages, kind, hit, max_age=24):
    """The first `pages` pages (20 results and 1 request each) of a CourtListener search, each page cached a day
    on its own under cases/api/<kind>/ with the link to the next, so one more page costs one request.
    hit: result -> dict. -> (count, hits, cached: no request made)."""
    key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    url, tok, hits, count, cached = "%s/search/?%s" % (c.API, urllib.parse.urlencode(params)), None, [], None, True
    for n in range(1, max(1, pages) + 1):
        path = c.CASES / "api" / kind / ("%s-p%d.json" % (key, n))
        page = None
        try:
            page = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - page.get("time", 0) > max_age * 3600 or (n > 1 and page.get("url") != url):
                page = None
        except (OSError, ValueError):
            pass
        if page is None:
            tok = tok or c.token()
            res = c.call(url, tok)
            page = {"url": url, "time": time.time(), "count": res.get("count"), "next": res.get("next"),
                    "hits": [hit(r) for r in res.get("results") or []]}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
            cached = False
        count = page["count"] if count is None else count
        hits += page["hits"]
        url = page.get("next")
        if not url:
            break
    return count, hits, cached


def api_dockets(query, court=None, pages=1):
    """CourtListener's docket search. -> (count, hits, cached)."""
    params = {"type": "d", "order_by": "score desc"}
    params["docket_number" if NUMBER_RE.match(query.strip()) else "case_name"] = query.strip()
    if court:
        params["court"] = court
    return api_pages(params, pages, "dockets", lambda r: {
        "id": r.get("docket_id"), "court": r.get("court_id") or "", "number": r.get("docketNumber") or "",
        "name": r.get("caseName") or "", "filed": r.get("dateFiled") or "", "terminated": r.get("dateTerminated") or "",
        "courtlistener": c.CL + (r.get("docket_absolute_url") or "") if r.get("docket_absolute_url") else ""})


# ------------------------------------------------------------------ a court's opinions

PER_PAGE = 20


def local_opinions(con, court, pages):
    """A court's opinions, newest first, from the local database (needs tribonian db courtindex).
    -> (hits, more) or None when the index isn't there."""
    from tribonian import casedb
    if con is None or not casedb.has_court_index(con):
        return None
    n = PER_PAGE * max(1, pages)
    rows = con.execute("SELECT c.id, c.case_name, c.date_filed, c.citation_count FROM dockets d "
                       "JOIN clusters c ON c.docket_id = d.id WHERE d.court_id=? "
                       "ORDER BY c.date_filed DESC LIMIT ?", (court, n + 1)).fetchall()
    hits = []
    for cid, name, filed, cited in rows[:n]:
        cite = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id=? ORDER BY type LIMIT 1", (cid,)).fetchone()
        hits.append({"cluster": cid, "name": name or "", "date": filed or "", "cited": cited or 0,
                     "cite": " ".join(cite) if cite else ""})
    return hits, len(rows) > n


def api_opinions(court, pages, after=None):
    """A court's opinions, newest first, from CourtListener's search (after: filed after this date only).
    -> (count, hits, cached)."""
    params = {"type": "o", "court": court, "order_by": "dateFiled desc"}
    if after:
        params["filed_after"] = after
    return api_pages(params, pages, "court-opinions", lambda r: {
        "cluster": r.get("cluster_id"), "name": r.get("caseName") or "", "date": r.get("dateFiled") or "",
        "cited": r.get("citeCount") or 0, "cite": ", ".join(r.get("citation") or [])})


def opinions(args):
    court, pages = args.court.lower(), max(1, args.pages)
    out = {"court": court, "source": "", "hits": [], "more": False, "count": None, "cached": False, "error": ""}
    con = c.local_db()
    got = local_opinions(con, court, pages)
    if got is not None:
        out["source"], (out["hits"], out["more"]) = "local database", got
        if tconfig.fill_gaps():                 # complete mode: what is newer than the bulk data, on top
            import datetime
            stamp = c.bulk_stamp(con)
            after = str(date.fromisoformat(stamp) - datetime.timedelta(days=c.API_LAG_DAYS)) if stamp else ""
            try:
                out["newer_count"], newer, _ = api_opinions(court, 1, after)
                have = {h["cluster"] for h in out["hits"]}
                out["hits"] = [dict(h, newer=True) for h in newer if h["cluster"] not in have] + out["hits"]
                out["newer_after"] = after
            except SystemExit as e:
                out["error"] = "newer than the bulk data: %s" % e
    elif tconfig.online():
        try:
            out["count"], out["hits"], out["cached"] = api_opinions(court, pages)
            out["source"], out["more"] = "CourtListener", (out["count"] or 0) > len(out["hits"])
        except SystemExit as e:
            out["error"] = str(e)
    else:
        out["error"] = "offline mode, and the local database has no index by court (tribonian db courtindex)"
    built = {int(k) for k in c.load_index()}
    for h in out["hits"]:
        h["built"] = h["cluster"] in built
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print("%s opinions, newest first, from %s%s:" % (court, out["source"] or "nowhere",
                                                   " and CourtListener (filed after %s)" % out["newer_after"] if out.get("newer_after") else ""))
    for h in out["hits"]:
        print("  %s  %-60s %s%s%s" % (h["date"], h["name"][:60], h["cite"], "  [built]" if h["built"] else "",
                                      "  [newer]" if h.get("newer") else ""))
    if out["error"]:
        print(out["error"])
    elif out["more"]:
        print("more: --pages %d" % (pages + 1))


def court_cites():
    """court id -> its citation form ("N.D. Ill.") from the court tree, where it has one."""
    try:
        t = json.loads(COURTS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    lists = list(t.get("federal", {}).values()) + [v for s in t.get("state", {}).values() for k, v in s.items() if k != "name"]
    return {x["id"]: x.get("cite") or "" for lst in lists for x in lst if x.get("cite")}


def search(args):
    q, court = " ".join(args.words), (args.court or "").lower() or None
    try:
        fetched = json.loads(INDEX_JSON.read_text(encoding="utf-8"))["dockets"]
    except (OSError, ValueError, KeyError):
        fetched = []
    mine = [{k: d.get(k, "") for k in ("id", "court", "number", "name", "filed", "terminated", "courtlistener")}
            for d in fetched if (not court or d.get("court") == court) and words_match(q, d.get("name"), d.get("number"), d.get("court"))]
    have = {d["id"] for d in fetched}
    out = {"query": q, "court": court or "", "mode": tconfig.data_mode(), "fetched": mine,
           "api": None, "count": None, "cached": False, "error": ""}
    if tconfig.online():
        try:
            out["count"], hits, out["cached"] = api_dockets(q, court, args.pages)
            out["api"] = [dict(h, fetched=h["id"] in have) for h in hits]
        except SystemExit as e:                     # no token, the daily limit ...: the local half still answers
            out["error"] = str(e)
    cites = court_cites()
    for h in out["fetched"] + (out["api"] or []):
        h["court_cite"] = cites.get(h["court"], "")
    out["pages"] = args.pages
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print("Fetched (%d):" % len(mine))
    for d in mine:
        print("  %-14s %-16s %s  %s" % (d["court_cite"] or d["court"], d["number"], d["name"][:60], "open" if not d["terminated"] else "closed"))
    if out["api"] is None:
        print("CourtListener: %s" % (out["error"] or "not searched (offline mode)"))
        return
    print("CourtListener (%s of %s%s):" % (len(out["api"]), out["count"], ", cached" if out["cached"] else ""))
    for h in out["api"]:
        print("  %-14s %-16s %s  %s%s" % (h["court_cite"] or h["court"], h["number"], h["name"][:60], h["filed"], "  [fetched]" if h["fetched"] else ""))
    print("fetch one:  tribonian case recap fetch --docket <id>")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("courts", help="the court tree -> cases/dockets/_courts.json").set_defaults(func=courts)
    sub.add_parser("index", help="fetched dockets and what is built -> cases/recap/_index.json").set_defaults(func=index)
    se = sub.add_parser("search", help="fetched dockets, then CourtListener's (not in offline mode)")
    se.add_argument("words", nargs="+", help="party or case name words, or a case number (26-2230)")
    se.add_argument("--court", help="only this court, e.g. ca4")
    se.add_argument("--pages", type=int, default=1, help="CourtListener result pages to read, 20 hits and 1 request each (default 1)")
    se.add_argument("--json", action="store_true", help="print JSON (for the GUI)")
    se.set_defaults(func=search)
    op = sub.add_parser("opinions", help="a court's opinions, newest first: the local database (tribonian db courtindex), "
                        "else CourtListener (not offline)")
    op.add_argument("--court", required=True, help="CourtListener court id, e.g. md (Supreme Court of Maryland)")
    op.add_argument("--pages", type=int, default=1, help="pages of %d to show (default 1)" % PER_PAGE)
    op.add_argument("--json", action="store_true", help="print JSON (for the GUI)")
    op.set_defaults(func=opinions)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
