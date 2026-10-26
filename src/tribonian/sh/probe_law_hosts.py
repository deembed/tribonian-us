#!/usr/bin/env python3
"""probe_law_hosts.py -- before writing the C.F.R. and state-law fetchers (scope-scotus-law.md §3):
can this machine reach each source, and does its robots.txt allow the pages a fetcher would take?

    python3 src/tribonian/sh/probe_law_hosts.py            # every source below
    python3 src/tribonian/sh/probe_law_hosts.py NH WI      # only these

One line per sample URL: HTTP status (or the error), and robots.txt's answer for our user agent.
Standard library only; one request per URL, a second apart per host. Writes nothing.
The sample URLs are the patterns in src/tribonian/data/state_law_sources.toml.
"""
import sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser

UA = "tribonian-us/0.1 (personal legal formatter; one request per second)"

SAMPLES = {
    "CFR": ["https://www.ecfr.gov/api/versioner/v1/titles.json",
            "https://www.ecfr.gov/api/versioner/v1/full/2026-10-01/title-34.xml?part=104"],
    "CONST": ["https://constitution.congress.gov/constitution/",
              "https://www.govinfo.gov/content/pkg/CDOC-110hdoc50/pdf/CDOC-110hdoc50.pdf"],
    "CO": ["https://leg.colorado.gov/colorado-revised-statutes",
           "https://www.sos.state.co.us/CCR/Welcome.do"],
    "NH": ["https://gc.nh.gov/rsa/html/nhtoc.htm",
           "https://gc.nh.gov/rsa/html/I/21-P/21-P-mrg.htm",
           "https://gc.nh.gov/rules/state_agencies/env-wq1300.html"],
    "WI": ["https://docs.legis.wisconsin.gov/statutes/prefaces/toc",
           "https://docs.legis.wisconsin.gov/document/statutes/35.19",
           "https://docs.legis.wisconsin.gov/statutes/statutes/889.pdf",
           "https://docs.legis.wisconsin.gov/code/admin_code",
           "https://docs.legis.wisconsin.gov/constitution/wi/000055",
           "https://docs.legis.wisconsin.gov/constitution/wi.pdf"],
    "WV": ["https://code.wvlegislature.gov/",
           "https://code.wvlegislature.gov/west-virginia-constitution",
           "https://apps.sos.wv.gov/adlaw/csr/"],
    "NE": ["https://nebraskalegislature.gov/laws/display-chapters.php?chapter=1",
           "https://nebraskalegislature.gov/laws/display-fullconst.php?print=false",
           "https://rules.nebraska.gov/"],
    "IL": ["https://ilga.gov/legislation/ilcs/ilcs4",
           "https://www.ilga.gov/commission/jcar/admincode/",
           "https://www.ilga.gov/ftp/JCAR/AdminCode/"],
    "MI": ["https://legislature.mi.gov/Laws/MCLSearch",
           "https://www.legislature.mi.gov/Publications/MIConstitution.pdf",
           "https://ars.apps.lara.state.mi.us/AdminCode/AdminCode"],
    "CT": ["https://cga.ct.gov/Current/pub/titles.htm",
           "https://cga.ct.gov/Current/pub/title_01.htm",
           "https://eregulations.ct.gov/eRegsPortal/Browse/RCSA"],
}

_robots, _last = {}, {}


def robots_says(url):
    host = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if host not in _robots:
        rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
        try:
            req = urllib.request.Request(host + "/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
            _robots[host] = rp
        except urllib.error.HTTPError as e:
            _robots[host] = "none (%d)" % e.code          # 404: no robots.txt, everything allowed
        except Exception as e:
            _robots[host] = "unreadable (%s)" % type(e).__name__
    rp = _robots[host]
    if isinstance(rp, str):
        return rp
    return "allowed" if rp.can_fetch(UA, url) else "DISALLOWED"


def status(url):
    host = urllib.parse.urlsplit(url).netloc
    wait = 1.0 - (time.time() - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(200000)
            return "%d %s %dkB" % (r.status, r.headers.get_content_type(), len(body) // 1024)
    except urllib.error.HTTPError as e:
        return "%d %s" % (e.code, e.reason)
    except Exception as e:
        return "error: %s" % (getattr(e, "reason", None) or type(e).__name__)


def main():
    want = [a.upper() for a in sys.argv[1:]] or list(SAMPLES)
    print("%-6s %-26s %-12s %s" % ("source", "fetch", "robots.txt", "url"))
    for key in want:
        for url in SAMPLES.get(key, []):
            print("%-6s %-26s %-12s %s" % (key, status(url)[:26], robots_says(url)[:12], url), flush=True)


if __name__ == "__main__":
    main()
