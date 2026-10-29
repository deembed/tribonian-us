"""Dockets: the court tree and the index of fetched dockets that the GUI's View Dockets screen reads.

    tribonian dockets courts      # the court tree -> cases/dockets/_courts.json
    tribonian dockets index       # what is fetched and built -> cases/recap/_index.json (fetch and build refresh it)
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


def index(args=None, quiet=False):
    """One summary per fetched docket (no document text), for the GUI."""
    notes, pdfs = built_files()
    rel = lambda p: str(p.relative_to(c.ROOT)) if p else ""
    out = []
    for p in sorted(c.RECAP.glob("*.json")) if c.RECAP.is_dir() else []:
        if p.name.startswith("_"):
            continue
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


def api_dockets(query, court=None, max_age=24):
    """CourtListener's docket search, cached a day under cases/api/dockets/. -> (count, hits, cached)."""
    params = {"type": "d", "order_by": "score desc"}
    params["docket_number" if NUMBER_RE.match(query.strip()) else "case_name"] = query.strip()
    if court:
        params["court"] = court
    path = c.CASES / "api" / "dockets" / (hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16] + ".json")
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - out.get("time", 0) <= max_age * 3600:
            return out["count"], out["hits"], True
    except (OSError, ValueError, KeyError):
        pass
    res = c.call("%s/search/?%s" % (c.API, urllib.parse.urlencode(params)), c.token())
    hits = [{"id": r.get("docket_id"), "court": r.get("court_id") or "", "number": r.get("docketNumber") or "",
             "name": r.get("caseName") or "", "filed": r.get("dateFiled") or "", "terminated": r.get("dateTerminated") or "",
             "courtlistener": c.CL + (r.get("docket_absolute_url") or "") if r.get("docket_absolute_url") else ""}
            for r in res.get("results") or []]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"params": params, "time": time.time(), "count": res.get("count"), "hits": hits},
                               ensure_ascii=False), encoding="utf-8")
    return res.get("count"), hits, False


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
            out["count"], hits, out["cached"] = api_dockets(q, court)
            out["api"] = [dict(h, fetched=h["id"] in have) for h in hits]
        except SystemExit as e:                     # no token, the daily limit ...: the local half still answers
            out["error"] = str(e)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print("Fetched (%d):" % len(mine))
    for d in mine:
        print("  %-6s %-16s %s  %s" % (d["court"], d["number"], d["name"][:60], "open" if not d["terminated"] else "closed"))
    if out["api"] is None:
        print("CourtListener: %s" % (out["error"] or "not searched (offline mode)"))
        return
    print("CourtListener (%s of %s%s):" % (len(out["api"]), out["count"], ", cached" if out["cached"] else ""))
    for h in out["api"]:
        print("  %-6s %-16s %s  %s%s" % (h["court"], h["number"], h["name"][:60], h["filed"], "  [fetched]" if h["fetched"] else ""))
    print("fetch one:  tribonian case recap fetch --docket <id>")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("courts", help="the court tree -> cases/dockets/_courts.json").set_defaults(func=courts)
    sub.add_parser("index", help="fetched dockets and what is built -> cases/recap/_index.json").set_defaults(func=index)
    se = sub.add_parser("search", help="fetched dockets, then CourtListener's (not in offline mode)")
    se.add_argument("words", nargs="+", help="party or case name words, or a case number (26-2230)")
    se.add_argument("--court", help="only this court, e.g. ca4")
    se.add_argument("--json", action="store_true", help="print JSON (for the GUI)")
    se.set_defaults(func=search)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
