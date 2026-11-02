"""Case history: one federal case across its courts, as a timeline that jumps between dockets.

    tribonian history --court cadc --number 21-5166       # the graph; time runs down, one lane per docket
    tribonian history --court scotus --number 22-451 --json
    tribonian history --docket 62982521 --all             # every filing, not only the key events

Start from any docket in the chain. The walk follows, in both directions:
  field   a docket's originating-court fields (appeals -> district, Supreme Court -> appeals)
  entry   the docket's own text ("USCA Case Number 21-5166", "placed on the docket ... as No. 22-451",
          "MANDATE ISSUED", "vacated and the case remanded")
Links are not guessed from names or dates. In the graph, field links are solid and entry links dashed.

Federal only: a link to a state court or an agency is noted, not followed. A filing missing from RECAP is
missing here too; district-court gaps in the entry numbers are counted and shown.

Downloads are kept in cases/history/_raw/ (--refresh reads them again); the chain is saved in
cases/history/<court>-<number>.json.
"""
import argparse, json, re, sys, urllib.parse

from tribonian import case2md as c
from tribonian import scotus, tconfig

OUT = c.CASES / "history"
RAW = OUT / "_raw"
PAGE_SIZE = 100
SCOTUS = "scotus"

_DISTRICTS = {
    "ca1": "med mad nhd rid prd", "ca2": "ctd nyed nynd nysd nywd vtd", "ca3": "ded njd paed pamd pawd vid",
    "ca4": "mdd nced ncmd ncwd scd vaed vawd wvnd wvsd", "ca5": "laed lamd lawd msnd mssd txed txnd txsd txwd",
    "ca6": "kyed kywd mied miwd ohnd ohsd tned tnmd tnwd", "ca7": "ilcd ilnd ilsd innd insd wied wiwd",
    "ca8": "ared arwd iand iasd mnd moed mowd ned ndd sdd",
    "ca9": "akd azd cacd caed cand casd hid idd mtd nvd ord waed wawd gud nmid",
    "ca10": "cod ksd nmd oked oknd okwd utd wyd", "ca11": "almd alnd alsd flmd flnd flsd gamd gand gasd",
    "cadc": "dcd"}
CIRCUIT_OF = {d: ca for ca, ds in _DISTRICTS.items() for d in ds.split()}
APPEALS = set(_DISTRICTS) | {"cafc"}

# -------------------------------------------------------------- reading a docket entry

LATER = re.compile(r"^\s*(?:\[\s*VACATED[^\]]*\]\s*-*|VACATED PURSUANT[^.]*\.+|RECALLED PURSUANT[^.]*\.+)\s*", re.I)
NOT_THE_COURT = re.compile(r"^(?:\w+ )?(?:unopposed |consent |joint |emergency |amended )*(?:motion|response|reply|opposition|"
                           r"brief|memorandum in|appellants? |appellees? |amicus|notice of (?:withdrawal|appearance|intention)|"
                           r"statement|letter \[?\d*\]? ?sent|form |mandate returned)", re.I)
KINDS = [
    ("recalled", re.compile(r"^(?:clerk's )?order\b.*\brecall|^mandate recalled", re.I)),
    ("appeal", re.compile(r"^(?:amended )?notice of (?:interlocutory )?appeal\b(?! due)", re.I)),
    ("docketed", re.compile(r"^USCA (?:Case )?(?:Number|No\.?|#)\s*\d", re.I)),
    ("cert_petition", re.compile(r"(?:petition for (?:a )?writ of certiorari|certiorari petition).*\b(?:filed|docketed)", re.I)),
    ("cert_granted", re.compile(r"(?:certiorari\b.*\bgranted|^petition granted)", re.I)),
    ("cert_denied", re.compile(r"(?:certiorari\b.*\bdenied|^petition denied)", re.I)),
    ("remand", re.compile(r"\b(?:vacated|reversed|case|cause)\b.*\bremanded\b|^(?:order|opinion|judgment)\b.*\bremand(?:ed|ing)\b", re.I)),
    ("mandate", re.compile(r"^mandate|^usca mandate|^judgment issued", re.I)),
    ("judgment", re.compile(r"^(?:final |per curiam )?judgment\b", re.I)),
    ("opinion", re.compile(r"^(?:memorandum |per curiam )?opinion\b", re.I)),
    ("argued", re.compile(r"^argued\b|^oral argument (?:held|heard)", re.I)),
    ("abeyance", re.compile(r"^(?:\w+ ){0,3}order\b.*\b(?:case|cases|appeal|action|matter|proceedings?)\b[^.;]*\bin abeyance\b", re.I)),
    ("dismissed", re.compile(r"^(?:clerk's |per curiam )?order\b.{0,40}\bdismiss(?:ed|ing)\b|^(?:case |appeal )?dismissed\b", re.I)),
]
USCA_RE = re.compile(r"USCA (?:Case )?(?:Number|No\.?|#)\s*(\d{2}-\d+)", re.I)
SCOTUS_NO_RE = re.compile(r"\bNo\.\s*(\d{2}(?:-|A)\d+)", re.I)
HIGH_COURT_RE = re.compile(r"Supreme Court|certiorari", re.I)
FROM_HIGH_COURT_RE = re.compile(r"Clerk of the Supreme Court|received from the Supreme Court", re.I)
USCA_WORD_RE = re.compile(r"\bUSCA\b")
REOPENS = {"argued", "opinion", "judgment", "abeyance", "remand"}
FED_CIRCUIT_RE = re.compile(r"federal circuit|\bCAFC\b", re.I)


def classify(text):
    """A docket entry's text -> (kind or None, later vacated or recalled?). Filings by the parties aren't key events;
    only a notice of appeal is, since it's the jump."""
    later = bool(LATER.match(text or ""))
    t = LATER.sub("", text or "").strip()
    if NOT_THE_COURT.match(t):
        return None, later
    for kind, rx in KINDS:
        if rx.search(t):
            return kind, later
    return None, later


def court_label(court, cites=None):
    if court == SCOTUS:
        return "U.S."
    if court == "cadc":
        return "D.C. Cir."
    if court == "cafc":
        return "Fed. Cir."
    m = re.fullmatch(r"ca(\d+)", court)
    if m:
        n = int(m.group(1))
        return "%d%s Cir." % (n, {1: "st", 2: "d", 3: "d"}.get(n, "th"))
    return (cites or {}).get(court) or court


def clean_number(number):
    """The court's own number without a judge suffix: 1:20-cv-00466-EGS -> 1:20-cv-00466."""
    number = (number or "").strip().strip("()").strip()
    m = re.match(r"(\d+:\d{2}-[a-zA-Z]+-\d+)", number)
    return m.group(1) if m else number


def lower_refs(raw):
    """The docket below as [(court, number)], from the originating-court fields. Empty when below isn't a federal
    court (raw["origin"] then names it)."""
    court = raw.get("appeal_from")
    if court not in CIRCUIT_OF and court not in APPEALS:
        return []
    return [(court, clean_number(n)) for n in (raw.get("lower_numbers") or []) if clean_number(n)]


def read_docket(raw):
    """One docket's raw record -> (events, links). Pure: no requests. A link is
    {from, to, kind, date, how}, each end being (court, number)."""
    me = (raw["court"], raw["number"])
    events, links = [], []

    def link(a, b, kind, date, how):
        links.append({"from": list(a), "to": list(b), "kind": kind, "date": date, "how": how})

    for r in lower_refs(raw):
        link(r, me, "cert" if raw["court"] == SCOTUS else "appeal", raw.get("noa_date") or raw.get("date_filed") or "", "field")

    if raw.get("date_filed"):
        events.append({"date": raw["date_filed"], "kind": "filed", "text": "Docket opened", "key": True, "vacated": False})
    if raw.get("date_terminated"):
        events.append({"date": raw["date_terminated"], "kind": "terminated", "text": "Docket terminated", "key": True, "vacated": False})

    circuit = "cafc" if raw["court"] in CIRCUIT_OF and any(FED_CIRCUIT_RE.search(e.get("text") or "") for e in raw["entries"]) \
        else CIRCUIT_OF.get(raw["court"])
    below = lower_refs(raw)
    for e in sorted(raw["entries"], key=lambda e: e.get("date") or ""):
        text = " ".join((e.get("text") or "").split())
        if not e.get("date") or not text:
            continue
        kind, later = classify(text)
        ev = {"date": e["date"], "kind": kind or "other", "text": text, "key": kind is not None,
              "vacated": later, "number": e.get("number")}
        # another court's action, copied onto this docket: walk() hides it when that court's lane has its own entries
        if kind and raw["court"] in APPEALS and FROM_HIGH_COURT_RE.search(LATER.sub("", text)):
            ev["reports"] = SCOTUS
        elif kind and kind != "appeal" and raw["court"] in CIRCUIT_OF and circuit and USCA_WORD_RE.search(text):
            ev["reports"] = circuit
        if kind == "appeal" and raw["court"] in APPEALS:       # the arrow already says it
            ev["key"] = False
        events.append(ev)
        if raw["court"] in CIRCUIT_OF and circuit:             # district -> appeals
            for n in USCA_RE.findall(text):
                link(me, (circuit, n), "appeal", e["date"], "entry")
        elif raw["court"] in APPEALS and HIGH_COURT_RE.search(text):     # appeals <-> Supreme Court
            for n in SCOTUS_NO_RE.findall(text):
                link(me, (SCOTUS, n), "cert", e["date"], "entry")
                if kind == "remand":
                    link((SCOTUS, n), me, "remand", e["date"], "entry")
        if kind in ("mandate", "remand") and not HIGH_COURT_RE.search(text):      # back down
            for r in below:
                link(me, r, "mandate" if kind == "mandate" else "remand", e["date"], "entry")
        if kind == "remand" and raw["court"] == SCOTUS:
            for r in below:
                link(me, r, "remand", e["date"], "entry")
    filings = [e for e in events if e["kind"] not in ("filed", "terminated")]
    if filings and not filings[-1]["key"]:                     # where the docket stands now
        filings[-1].update(key=True, latest=True)
    for t in events:
        if t["kind"] == "terminated":
            t["reopened"] = any(e["kind"] in REOPENS and e["date"] > t["date"] and not e.get("reports") for e in filings)
    return events, links


def missing_numbers(entries):
    """District entries are numbered in order, so a skipped number is a filing RECAP doesn't have."""
    seen = sorted({int(e["number"]) for e in entries if str(e.get("number") or "").isdigit()})
    return seen[-1] - len(seen) if seen and seen[-1] < 100000 else 0


# -------------------------------------------------------------- fetching one docket

def raw_path(court, number):
    return RAW / ("%s-%s.json" % (court, c.slugify(number)))


def _appeal_court(rec):
    m = re.search(r"/courts/([^/]+)/", rec.get("appeal_from") or "")
    return m.group(1) if m else None


def _entries_api(did, tok, max_pages):
    """A docket's entries. Falls back to RECAP's document search if the token may not read docket entries."""
    q = urllib.parse.urlencode({"docket": did, "page_size": PAGE_SIZE,   # unsorted: walk() sorts, and sorting here can stall the server
                                "fields": "date_filed,entry_number,description"})
    url, out, pages, src = "%s/docket-entries/?%s" % (c.API, q), [], 0, "docket-entries"
    while url and pages < max_pages:
        page = c.call(url, tok, soft=(403, 404))
        if page is None:
            if pages == 0:
                return _entries_search(did, tok, max_pages)
            break
        out += [{"date": e.get("date_filed"), "number": e.get("entry_number"), "text": e.get("description") or ""}
                for e in page.get("results") or []]
        url, pages = page.get("next"), pages + 1
    return out, src, bool(url)


def _entries_search(did, tok, max_pages):
    q = urllib.parse.urlencode({"type": "rd", "q": "docket_id:%d" % did, "order_by": "entry_date_filed asc"})
    url, seen, pages = "%s/search/?%s" % (c.API, q), {}, 0
    while url and pages < max_pages:
        page = c.call(url, tok, soft=(403, 404)) or {}
        for r in page.get("results") or []:
            seen.setdefault((r.get("entry_date_filed"), r.get("description")), {
                "date": r.get("entry_date_filed"), "number": r.get("entry_number") or r.get("document_number"),
                "text": r.get("description") or r.get("short_description") or ""})
        url, pages = page.get("next"), pages + 1
    return list(seen.values()), "documents only", bool(url)


def fetch_scotus_feed(number):
    """The Court's own docket, which CourtListener's Supreme Court records don't carry."""
    if not tconfig.online():
        return None
    try:
        raw = scotus.get(scotus.FEED % number)
        return json.loads(raw.decode("utf-8-sig")) if raw else None
    except ValueError:
        return None


def fetch_docket(court, number, tok, max_pages):
    """-> the raw record of (court, number), merging CourtListener's duplicates. found False when there is none."""
    raw = {"court": court, "number": number, "found": False, "ids": [], "entries": [], "lower_numbers": [],
           "case_name": "", "date_filed": "", "date_terminated": "", "noa_date": "", "appeal_from": None, "origin": "",
           "source": "", "truncated": False, "url": ""}
    fields = "id,case_name,docket_number,date_filed,date_terminated,appeal_from,appeal_from_str,original_court_info,absolute_url"
    q = urllib.parse.urlencode({"court": court, "docket_number": number, "fields": fields})
    recs = (c.call("%s/dockets/?%s" % (c.API, q), tok, soft=(403, 404)) or {}).get("results") or []
    seen = set()
    for rec in recs:
        raw["found"] = True
        raw["ids"].append(rec["id"])
        raw["url"] = raw["url"] or ("https://www.courtlistener.com" + rec.get("absolute_url", ""))
        raw["case_name"] = raw["case_name"] or rec.get("case_name") or ""
        for k in ("date_filed", "date_terminated"):
            raw[k] = raw[k] or rec.get(k) or ""
        raw["appeal_from"] = raw["appeal_from"] or _appeal_court(rec)
        raw["origin"] = raw["origin"] or rec.get("appeal_from_str") or ""
        oi = rec.get("original_court_info") or {}
        raw["noa_date"] = raw["noa_date"] or oi.get("date_filed_noa") or ""
        for n in re.split(r"[,;]", oi.get("docket_number") or ""):
            if n.strip() and n.strip() not in raw["lower_numbers"]:
                raw["lower_numbers"].append(n.strip())
        if court != SCOTUS:
            entries, raw["source"], more = _entries_api(rec["id"], tok, max_pages)
            raw["truncated"] = raw["truncated"] or more
            for e in entries:
                key = (e.get("date"), e.get("text"))
                if key not in seen:
                    seen.add(key)
                    raw["entries"].append(e)
    if court == SCOTUS:
        feed = fetch_scotus_feed(number)
        if feed:
            raw["found"], raw["source"] = True, "supremecourt.gov"
            raw["case_name"] = raw["case_name"] or " v. ".join(filter(None, (feed.get("PetitionerTitle"), feed.get("RespondentTitle"))))
            raw["date_filed"] = raw["date_filed"] or scotus.when(feed.get("DocketedDate"))
            raw["entries"] = [{"date": scotus.when(p.get("Date")), "text": p.get("Text") or ""}
                              for p in feed.get("ProceedingsandOrder") or []]
            if not raw["lower_numbers"]:
                court_below, n = scotus.below_ref(feed)
                if n:
                    raw["lower_numbers"] = [n]
                    raw["appeal_from"] = raw["appeal_from"] or court_below
                raw["origin"] = raw["origin"] or feed.get("LowerCourt") or ""
    return raw


def load_docket(court, number, tok, refresh, max_pages):
    p = raw_path(court, number)
    if p.exists() and not refresh:
        return json.loads(p.read_text(encoding="utf-8"))
    print("reading %s %s ..." % (court, number), file=sys.stderr, flush=True)
    raw = fetch_docket(court, number, tok, max_pages)
    if tconfig.online():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    return raw


# -------------------------------------------------------------- the chain

def walk(seed, get, max_dockets=12):
    """Follow links from seed (court, number). get(court, number) -> raw record. -> the chain as plain data."""
    todo, raws, links, events, notes = [tuple(seed)], {}, [], [], []
    while todo:
        key = todo.pop(0)
        if key in raws:
            continue
        if len(raws) >= max_dockets:
            notes.append("Stopped at %d dockets; %s %s and any others not read (--max-dockets)." % (max_dockets, key[0], key[1]))
            break
        raw = get(*key)
        raws[key] = raw
        evs, lks = read_docket(raw)
        events += [dict(e, docket=list(key)) for e in evs]
        for l in lks:
            links.append(l)
            todo += [tuple(l["from"]), tuple(l["to"])]
    seen, unique = set(), []
    for l in sorted(links, key=lambda l: (l["date"], l["how"] != "field")):
        up = l["kind"] in ("appeal", "cert")                   # one appeal per pair; each trip back down is its own
        k = (tuple(l["from"]), tuple(l["to"])) + (("up",) if up else (l["kind"], l["date"]))
        if k not in seen:
            seen.add(k)
            unique.append(l)
    has_lane = {r["court"] for r in raws.values() if r.get("entries")}
    for e in events:
        if e.get("reports") in has_lane:
            e["key"] = False
    # a recall undoes the latest mandate before it, on the arrow and on the row
    for r in [e for e in events if e["kind"] == "recalled"]:
        back = [l for l in unique if l["kind"] == "mandate" and l["date"] < r["date"] and r["docket"] in (l["from"], l["to"])]
        if back:
            m = max(back, key=lambda l: l["date"])
            m["recalled"] = r["date"]
            for e in events:
                if e["kind"] == "mandate" and e["date"] == m["date"] and e["docket"] in (m["from"], m["to"]):
                    e["vacated"] = True
    dockets = []
    for key, r in raws.items():
        d = {k: r.get(k) for k in ("court", "number", "found", "case_name", "date_filed", "date_terminated", "url", "ids", "source", "truncated", "origin")}
        d["entries"] = len(r.get("entries") or [])
        d["missing_entries"] = missing_numbers(r.get("entries") or []) if r["court"] in CIRCUIT_OF else 0
        dockets.append(d)
        if not r.get("found"):
            notes.append("%s %s: named by another docket, but not on CourtListener." % key)
        if r.get("truncated"):
            notes.append("%s %s: long docket, stopped reading at the page limit (--max-pages)." % key)
        if r.get("source") == "documents only":
            notes.append("%s %s: only entries with a document in RECAP were readable." % key)
        if d["missing_entries"]:
            notes.append("%s %s: %d numbered entries aren't in RECAP." % (key[0], key[1], d["missing_entries"]))
        if r.get("found") and not lower_refs(r) and r["court"] not in CIRCUIT_OF and r.get("origin"):
            notes.append("%s %s: comes from %s, not followed." % (key[0], key[1], r["origin"]))
    events.sort(key=lambda e: e["date"])
    return {"seed": list(seed), "dockets": dockets, "events": events, "links": unique, "notes": notes}


# -------------------------------------------------------------- the graph

KIND_LABEL = {"appeal": "appeal", "cert": "to the Supreme Court", "mandate": "mandate", "remand": "remand"}


def _rank(court):
    return 2 if court == SCOTUS else 1 if court in APPEALS else 0


def lanes_of(chain):
    ds = sorted(chain["dockets"], key=lambda d: (_rank(d["court"]), d.get("date_filed") or "9999", d["number"]))
    return [(d["court"], d["number"]) for d in ds]


def render(chain, all_events=False, cites=None, width=110):
    """Text graph. One lane per docket (a new number in the same court is a new lane), time running down."""
    lanes = lanes_of(chain)
    idx = {k: i for i, k in enumerate(lanes)}
    cell = 3
    lines = []
    name = next((d["case_name"] for d in chain["dockets"] if d.get("case_name")), "")
    if name:
        lines += [name, ""]
    for i, (court, number) in enumerate(lanes):
        lines.append(" " * 12 + "│  " * i + "┌─ %s %s" % (court_label(court, cites), number))
    shown, rows = set(), []
    for e in chain["events"]:
        k = (e["date"], tuple(e["docket"]), e["kind"])
        if all_events or (e.get("key") and (e.get("latest") or k not in shown)):
            shown.add(k)
            rows.append((e["date"], 1, "event", e))
    rows += [(l["date"], 0 if l["kind"] in ("appeal", "cert") else 2, "link", l) for l in chain["links"] if l["date"]]
    rows.sort(key=lambda r: (r[0], r[1]))
    span = {}
    for date, _, typ, x in rows:
        for k in ([x["docket"]] if typ == "event" else [x["from"], x["to"]]):
            k = tuple(k)
            a, b = span.get(k, (date, date))
            span[k] = (min(a, date), max(b, date))

    def bar(i, date):
        a_b = span.get(lanes[i])
        return "│" if a_b and a_b[0] <= date <= a_b[1] else " "

    for date, _, typ, x in rows:
        if typ == "event":
            at = idx[tuple(x["docket"])]
            g = "".join(("●" if i == at else bar(i, date)) + "  " for i in range(len(lanes)))
            text = KIND_TEXT(x)
        else:
            a, b = idx[tuple(x["from"])], idx[tuple(x["to"])]
            lo, hi = min(a, b), max(a, b)
            dash = "─" if x["how"] == "field" else "┄"
            g = ""
            for i in range(len(lanes)):
                if i == a:
                    g += "├" if a < b else "┤"
                elif i == b:
                    g += "▶" if a < b else "◀"
                elif lo < i < hi:
                    g += dash
                else:
                    g += bar(i, date)
                g += (dash * (cell - 1)) if lo <= i < hi else " " * (cell - 1)
            text = "%s -> %s %s" % (KIND_LABEL[x["kind"]], court_label(x["to"][0], cites), x["to"][1])
            if x.get("recalled"):
                text += " (recalled %s)" % x["recalled"]
        room = max(20, width - 12 - len(g))
        lines.append("%-10s  %s%s" % (date, g, text[:room]))
    lines.append("")
    lines.append("─ from the docket's fields   ┄ from an entry's text   ● filing")
    for n in chain["notes"]:
        lines.append("note: " + n)
    return "\n".join(lines)


SHORT = {"filed": "docket opened", "terminated": "docket terminated", "docketed": "appeal docketed", "cert_petition": "cert petition",
         "cert_granted": "cert granted", "cert_denied": "cert denied", "remand": "remanded", "mandate": "mandate",
         "recalled": "mandate recalled", "judgment": "judgment", "opinion": "opinion", "appeal": "notice of appeal",
         "argued": "argued", "abeyance": "held in abeyance", "dismissed": "dismissed"}


def KIND_TEXT(e):
    if e.get("latest"):
        return "latest: " + e["text"]
    if e.get("reopened"):
        return "docket terminated (later reopened)"
    short = SHORT.get(e["kind"])
    if not short:
        return e["text"]
    return short + (" (later vacated or recalled)" if e.get("vacated") else "")


# -------------------------------------------------------------- command line

def seed_from_id(did, tok):
    d = c.call("%s/dockets/%d/?fields=court_id,docket_number" % (c.API, did), tok)
    return d["court_id"], clean_number(d["docket_number"])


def main():
    ap = argparse.ArgumentParser(prog="tribonian history", description=__doc__.split("\n")[0])
    ap.add_argument("--court", help="CourtListener court id, e.g. scotus, cadc, ca4, mdd")
    ap.add_argument("--number", help="with --court: the court's case number, e.g. 21-5166 or 1:20-cv-00466")
    ap.add_argument("--docket", help="or a CourtListener docket id or URL")
    ap.add_argument("--json", action="store_true", help="print the chain as JSON instead of the graph")
    ap.add_argument("--all", action="store_true", help="every filing in the graph, not only the key events")
    ap.add_argument("--refresh", action="store_true", help="read the dockets again instead of using cases/history/_raw/")
    ap.add_argument("--max-dockets", type=int, default=12, help="stop after this many dockets (default 12)")
    ap.add_argument("--max-pages", type=int, default=20, help="pages of %d entries to read per docket (default 20)" % PAGE_SIZE)
    args = ap.parse_args()
    tok = c.token()
    if args.docket:
        m = re.search(r"(\d+)", args.docket)
        if not m:
            sys.exit("--docket takes a CourtListener docket id or URL, e.g. 62982521")
        seed = seed_from_id(int(m.group(1)), tok)
    elif args.court and args.number:
        seed = (args.court.lower(), clean_number(args.number))
    else:
        sys.exit("Name the docket: --court ID --number N (e.g. --court cadc --number 21-5166), or --docket ID")
    chain = walk(seed, lambda court, number: load_docket(court, number, tok, args.refresh, args.max_pages), args.max_dockets)
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / ("%s-%s.json" % (seed[0], c.slugify(seed[1])))
    out.write_text(json.dumps(chain, indent=1, ensure_ascii=False), encoding="utf-8")
    if args.json:
        print(json.dumps(chain, indent=1, ensure_ascii=False))
    else:
        from tribonian import dockets
        print(render(chain, args.all, dockets.court_cites()))
        print("saved %s" % out)


if __name__ == "__main__":
    main()
