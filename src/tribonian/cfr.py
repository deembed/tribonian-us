"""cfr.py -- the Code of Federal Regulations, one title at a time, from eCFR (scope-scotus-law.md §3).

    tribonian cfr fetch 34                 title 34, unless it is already local
    tribonian cfr fetch 34 40 --since 2026-10-01
                                           ... or the local copy is older than that date
    tribonian cfr fetch 34 --force         download again whatever is local
    tribonian cfr notes 34                 (re)write title 34's Obsidian notes -> md-cfr/t34/pt<part>/ (fetch does it)
    tribonian cfr list                     the titles stored here, with their dates

Source: the eCFR versioner API (www.ecfr.gov/api/versioner/v1). titles.json gives each title's
up_to_date_as_of date; full/<date>/title-<n>.xml is the whole title as of that date (eCFR's XML:
DIV1 title ... DIV5 part ... DIV8 section). eCFR is the editorially current text; the official
annual edition is govinfo's. Files go to <law_dir>/cfr/ (law_dir: config.toml or TRIBONIAN_LAW,
default <work>/law; some titles run to hundreds of MB):

    title-34.xml    the title           title-34.json   {date, fetched, url, bytes}

Standard library only. robots.txt is obeyed (scope §3): a disallowed URL is reported, not fetched.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.request, urllib.robotparser, zlib
from datetime import date
from pathlib import Path

from tribonian import tconfig

API = "https://www.ecfr.gov/api/versioner/v1"
UA = "tribonian-us/0.1 (personal legal formatter)"
_robots = None


def law_dir():
    return Path(tconfig.get("law_dir") or tconfig.WORK / "law")


def cfr_dir():
    return law_dir() / "cfr"


def local(title):
    """The local copy's metadata ({date, fetched, url, bytes}) or None."""
    meta, xml = cfr_dir() / ("title-%s.json" % title), cfr_dir() / ("title-%s.xml" % title)
    if not (meta.exists() and xml.exists()):
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return None


def tls_context():
    """The system's trusted roots, plus the intermediate certificates in data/certs/*.pem: some publishers'
    servers (Connecticut's General Assembly) send their certificate without the intermediate, which browsers
    fetch on their own and Python does not. An intermediate added here only helps build the chain; it must
    still end at a root the system trusts. Put only CA intermediates in that folder, never a root."""
    global _TLS
    if _TLS is None:
        import ssl
        _TLS = ssl.create_default_context()
        for pem in sorted((tconfig.PKG / "data" / "certs").glob("*.pem")):
            _TLS.load_verify_locations(cafile=str(pem))
    return _TLS


_TLS = None


def urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout, context=tls_context())


def read_robots(host, ua):
    """host's robots.txt, read with our own user agent (RobotFileParser.read() sends Python's default agent,
    which some servers, the Colorado Secretary of State's, answer with 403). As RFC 9309: a 4xx answer means
    no rules; a 5xx answer or no answer at all (network, TLS) means the whole site is off limits until it can
    be read -- an unreadable robots.txt is never taken as permission."""
    rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
    try:
        req = urllib.request.Request(host + "/robots.txt", headers={"User-Agent": ua})
        with urlopen(req, timeout=30) as r:
            rp.parse(r.read().decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            rp.parse([])
        else:
            rp.disallow_all = True
            rp.reason = "robots.txt answered HTTP %d" % e.code
    except Exception as e:
        rp.disallow_all = True
        rp.reason = "robots.txt unreadable: %s" % getattr(e, "reason", e)
    return rp


def allowed(url):
    global _robots
    if _robots is None:
        _robots = read_robots("https://www.ecfr.gov", UA)
    return _robots.can_fetch(UA, url)


def get(url, accept="application/json"):
    if not allowed(url):
        sys.exit("robots.txt on www.ecfr.gov disallows %s; not fetched" % url)
    # the full-title endpoint refuses an uncompressed response (HTTP 406): ask for gzip, inflate in fetch_one
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip"})
    try:
        return urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        body = e.read(500).decode("utf-8", "replace").strip()
        sys.exit("%s: HTTP %d %s%s" % (url, e.code, e.reason, "\n  " + body if body else ""))
    except urllib.error.URLError as e:
        sys.exit("%s: %s" % (url, e.reason))


def titles():
    """{title number (str): eCFR's entry} and whether eCFR says an import is running."""
    with get(API + "/titles.json") as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
        d = json.loads(raw)
    return {str(t["number"]): t for t in d.get("titles", [])}, bool((d.get("meta") or {}).get("import_in_progress"))


def fetch_one(title, entry, force=False, since=None):
    have = local(title)
    want = entry.get("up_to_date_as_of") or entry.get("latest_issue_date")
    if entry.get("reserved"):
        print("title %s: reserved (no text)" % title)
        return
    if have and not force and (since is None or have["date"] >= since):
        print("title %s: local, as of %s (fetched %s); --force or --since to replace" % (title, have["date"], have["fetched"]))
        return
    url = "%s/full/%s/title-%s.xml" % (API, want, title)
    d = cfr_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ("title-%s.xml.part" % title)
    print("title %s: %s, as of %s ..." % (title, entry.get("name", ""), want), flush=True)
    t0, n = time.time(), 0
    with get(url, "application/xml") as r, open(tmp, "wb") as out:
        enc = r.headers.get("Content-Encoding", "")
        z = zlib.decompressobj(16 + zlib.MAX_WBITS if enc == "gzip" else zlib.MAX_WBITS) if enc in ("gzip", "deflate") else None
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            if z:
                chunk = z.decompress(chunk)
            out.write(chunk)
            n += len(chunk)
        if z:
            tail = z.flush()
            out.write(tail)
            n += len(tail)
    os.replace(tmp, d / ("title-%s.xml" % title))
    (d / ("title-%s.json" % title)).write_text(json.dumps(
        {"date": want, "fetched": date.today().isoformat(), "url": url, "bytes": n}, indent=1), encoding="utf-8")
    print("  wrote %s (%.1f MB, %.0f s)" % (d / ("title-%s.xml" % title), n / 1e6, time.time() - t0))


# ---------------------------------------------------------------- notes (Obsidian), like the US Code's

def notes_dir():
    return tconfig.WORK / "md-cfr"


def _md_inline(el):
    """eCFR inline markup -> Markdown: <I>/<E> italic, the rest plain, escaped."""
    from tribonian import case2md as c
    out = [c.esc(el.text or "", "md")]
    for k in el:
        inner = _md_inline(k)
        out.append("*%s*" % inner.strip() if k.tag in ("I", "E") and inner.strip() else inner)
        out.append(c.esc(k.tail or "", "md"))
    return " ".join("".join(out).split())


def _link(text, known):
    """Statute and regulation cites in a note's paragraph -> [[note|cite]] where that note exists."""
    from tribonian import lawcites
    for start, end, key in sorted(lawcites.spans_in(text), reverse=True):
        note = lawcites.note_name(key)
        if note and note in known:
            text = text[:start] + "[[%s|%s]]" % (note, text[start:end]) + text[end:]
    return text


def write_notes(title, quiet=False):
    """md-cfr/t<title>/pt<part>/: one note per section (<t>-cfr-<sec>.md) and per part (<t>-cfr-pt-<part>.md),
    from the local title XML. Notes of the title that no longer exist are left (sync-vault mirrors md-cfr/)."""
    from lxml import etree
    from tribonian import case2md as c
    meta = local(title)
    if meta is None:
        sys.exit("title %s is not local: tribonian cfr fetch %s" % (title, title))
    root = etree.parse(str(cfr_dir() / ("title-%s.xml" % title)), etree.XMLParser(huge_tree=True, remove_comments=True)).getroot()
    known = c.statute_notes()
    parts = list(root.iter("DIV5"))
    known |= {"%s-cfr-pt-%s" % (title, p.get("N")) for p in parts}
    known |= {"%s-cfr-%s" % (title, s_.get("N")) for p in parts for s_ in p.iter("DIV8")}
    head = lambda el: " ".join((el.findtext("HEAD") or "").split())
    common = [("source", "ecfr.gov (versioner API)"), ("current_as_of", c.Raw(meta["date"])),
              ("retrieved", c.Raw(meta["fetched"])), ("synthetic", False)]
    n = 0
    for part in parts:
        pn = part.get("N")
        d = notes_dir() / ("t%s" % title) / ("pt%s" % pn)
        d.mkdir(parents=True, exist_ok=True)
        pcite = "%s C.F.R. pt. %s" % (title, pn)
        ptitle = re.sub(r"^PARTS?\s+\S+\s*[—-]\s*", "", head(part))
        L = c.yaml_head([("title", pcite), ("heading", c.titlecase(ptitle)), ("citation", pcite),
                         ("identifier", "/us/cfr/t%s/pt%s" % (title, pn))] + common + [("aliases", [pcite, "%s CFR %s" % (title, pn)])])
        L += ["", "# %s — %s" % (pcite, c.esc(c.titlecase(ptitle), "md")), ""]
        for tag in ("AUTH", "SOURCE"):
            el = part.find(tag)
            if el is not None:
                L += ["*%s*" % c.esc(" ".join(" ".join(el.itertext()).split()), "md"), ""]      # "Authority: 20 U.S.C. ..."
        for el in part.iter("DIV6", "DIV8"):
            if el.tag == "DIV6":
                L += ["", "## %s" % c.esc(head(el), "md"), ""]
                continue
            sn = el.get("N")
            h = re.sub(r"^§\s*\S+\s*", "", head(el))
            L.append("- [[%s-cfr-%s|§ %s]] %s" % (title, sn, sn, c.esc(h, "md")))
            # the section's own note
            cite = "%s C.F.R. § %s" % (title, sn)
            S = c.yaml_head([("title", cite), ("heading", h.rstrip(".")), ("citation", cite),
                             ("identifier", "/us/cfr/t%s/s%s" % (title, sn))] + common +
                            [("aliases", [cite, "%s CFR %s" % (title, sn)])])
            S += ["", "# %s — %s" % (cite, c.esc(h.rstrip("."), "md")), "",
                  "Part: [[%s-cfr-pt-%s|%s]]" % (title, pn, pcite), ""]
            for k in el:
                if k.tag in ("P", "FP"):
                    t = _md_inline(k)
                    m = re.match(r"((?:\([a-zA-Z0-9]{1,4}\)\s*)+)(.*)", t)
                    t = "**%s** %s" % (m.group(1).strip(), m.group(2)) if m else t
                    S += [_link(t, known), ""]
                elif k.tag in ("HD1", "HD2", "HD3"):
                    S += ["**%s**" % _md_inline(k), ""]
                elif k.tag == "EXTRACT":
                    S += ["> " + _link(_md_inline(p), known) for p in k.iter("P", "FP")] + [""]
                elif k.tag in ("GPOTABLE", "TABLE", "DIV") or k.find(".//TABLE") is not None:
                    S += ["*[A table: see this section on ecfr.gov.]*", ""]
                elif k.tag in ("NOTE", "EDNOTE"):
                    S += ["> *%s*" % _md_inline(p) for p in k.iter("P")] + [""]
                elif k.tag == "CITA":
                    S += ["*%s*" % _md_inline(k), ""]
            (d / ("%s-cfr-%s.md" % (title, sn))).write_text("\n".join(S).rstrip() + "\n", encoding="utf-8")
            n += 1
        (d / ("%s-cfr-pt-%s.md" % (title, pn))).write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")
    if not quiet:
        print("title %s: %d section notes, %d part notes -> %s" % (title, n, len(parts), notes_dir() / ("t%s" % title)))


def cmd_notes(args):
    for t in args.titles:
        write_notes(str(int(t)))


def cmd_fetch(args):
    known, busy = titles()
    if busy:
        print("note: eCFR says an import is in progress; the newest day may be incomplete")
    for t in args.titles:
        t = str(int(t))
        if t not in known:
            sys.exit("no title %s in eCFR's list (1-50)" % t)
        fetch_one(t, known[t], args.force, args.since)
        if local(t):
            write_notes(t)
        time.sleep(1)


def cmd_list(args):
    d = cfr_dir()
    rows = sorted((int(p.stem.split("-")[1]), local(p.stem.split("-")[1])) for p in d.glob("title-*.json")) if d.is_dir() else []
    if not rows:
        print("no C.F.R. titles in %s (tribonian cfr fetch <title>)" % d)
        return
    for t, m in rows:
        if m:
            print("title %-3d as of %s  fetched %s  %7.1f MB" % (t, m["date"], m["fetched"], m["bytes"] / 1e6))
    print("in %s" % d)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download titles from eCFR unless they are already local")
    f.add_argument("titles", nargs="+", help="title numbers, e.g. 34")
    f.add_argument("--since", metavar="YYYY-MM-DD", help="also replace a local copy older than this date")
    f.add_argument("--force", action="store_true", help="download again whatever is local")
    f.set_defaults(func=cmd_fetch)
    nt = sub.add_parser("notes", help="(re)write the Obsidian notes of local titles -> md-cfr/ (fetch does it too)")
    nt.add_argument("titles", nargs="+")
    nt.set_defaults(func=cmd_notes)
    l = sub.add_parser("list", help="the titles stored here, with their dates")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
