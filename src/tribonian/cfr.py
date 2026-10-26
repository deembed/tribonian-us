"""cfr.py -- the Code of Federal Regulations, one title at a time, from eCFR (scope-scotus-law.md §3).

    tribonian cfr fetch 34                 title 34, unless it is already local
    tribonian cfr fetch 34 40 --since 2026-10-01
                                           ... or the local copy is older than that date
    tribonian cfr fetch 34 --force         download again whatever is local
    tribonian cfr list                     the titles stored here, with their dates

Source: the eCFR versioner API (www.ecfr.gov/api/versioner/v1). titles.json gives each title's
up_to_date_as_of date; full/<date>/title-<n>.xml is the whole title as of that date (eCFR's XML:
DIV1 title ... DIV5 part ... DIV8 section). eCFR is the editorially current text; the official
annual edition is govinfo's. Files go to <law_dir>/cfr/ (law_dir: config.toml or TRIBONIAN_LAW,
default <work>/law; some titles run to hundreds of MB):

    title-34.xml    the title           title-34.json   {date, fetched, url, bytes}

Standard library only. robots.txt is obeyed (scope §3): a disallowed URL is reported, not fetched.
"""
import argparse, json, os, sys, time, urllib.error, urllib.request, urllib.robotparser, zlib
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


def allowed(url):
    global _robots
    if _robots is None:
        _robots = urllib.robotparser.RobotFileParser("https://www.ecfr.gov/robots.txt")
        try:
            _robots.read()
        except Exception:            # unreadable robots.txt: treat as no rules, as robotparser does for 404
            _robots.allow_all = True
    return _robots.can_fetch(UA, url)


def get(url, accept="application/json"):
    if not allowed(url):
        sys.exit("robots.txt on www.ecfr.gov disallows %s; not fetched" % url)
    # the full-title endpoint refuses an uncompressed response (HTTP 406): ask for gzip, inflate in fetch_one
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Encoding": "gzip"})
    try:
        return urllib.request.urlopen(req, timeout=120)
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


def cmd_fetch(args):
    known, busy = titles()
    if busy:
        print("note: eCFR says an import is in progress; the newest day may be incomplete")
    for t in args.titles:
        t = str(int(t))
        if t not in known:
            sys.exit("no title %s in eCFR's list (1-50)" % t)
        fetch_one(t, known[t], args.force, args.since)
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
    l = sub.add_parser("list", help="the titles stored here, with their dates")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
