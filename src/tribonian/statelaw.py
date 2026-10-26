"""statelaw.py -- state law, fetched per state from its own publisher (scope-scotus-law.md §3).
One reader per state, written when a docket needs that state; the registry of sources is
data/state_law_sources.toml. Colorado so far:

    tribonian state fetch CO crs 26.5 00      C.R.S. titles (00 = the Colorado Constitution)
    tribonian state fetch CO crs 26.5 --force download again
    tribonian state fetch CO ccr 8-1404-1     a rule of the Code of Colorado Regulations (8 CCR 1404-1)
    tribonian state fetch US const            the U.S. Constitution (govinfo, H. Doc. 110-50)
    tribonian state list                      what is stored here

C.R.S.: the Office of Legislative Legal Services' per-title HTML (olls.info/crs/crs<edition>-title-NN.htm),
the edition current through the latest regular session. Personal use; republishing needs a statement to
the Committee on Legal Services (C.R.S. 2-5-118). Files: <law_dir>/co/crs/title-26.5.htm + .json.
robots.txt is obeyed: a disallowed URL is reported, not fetched.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from datetime import date

from tribonian import cfr, tconfig

UA = cfr.UA
CRS_EDITION = 2026                    # the OLLS set current through the 2026 regular session
CRS_URL = "https://olls.info/crs/crs{edition}-title-{title}.htm"
_robots = {}


def state_dir(st):
    return cfr.law_dir() / st.lower()


def allowed(url):
    host = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if host not in _robots:
        _robots[host] = cfr.read_robots(host, UA)
    return _robots[host].can_fetch(UA, url)


def download(url, path):
    """url -> path (via a .part file). Returns bytes written, or exits with the reason."""
    if not allowed(url):
        sys.exit("robots.txt disallows %s; not fetched" % url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        sys.exit("%s: HTTP %d %s" % (url, e.code, e.reason))
    except urllib.error.URLError as e:
        sys.exit("%s: %s" % (url, e.reason))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return len(body)


def crs_title_name(t):
    """'0' -> '00', '1' -> '01', '26.5' -> '26.5' (the OLLS file names)."""
    whole, _, frac = t.partition(".")
    return "%02d" % int(whole) + ("." + frac if frac else "")


def local_meta(path):
    meta = path.with_suffix(".json")
    if not (path.exists() and meta.exists()):
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return None


def fetch_co_crs(titles, force=False, edition=CRS_EDITION):
    for t in titles:
        name = crs_title_name(t)
        path = state_dir("CO") / "crs" / ("title-%s.htm" % name)
        have = local_meta(path)
        if have and have.get("edition") == edition and not force:
            print("C.R.S. title %s: local, %s edition (fetched %s); --force to replace" % (name, edition, have["fetched"]))
            continue
        url = CRS_URL.format(edition=edition, title=name)
        print("C.R.S. title %s (%d edition) ..." % (name, edition), flush=True)
        n = download(url, path)
        path.with_suffix(".json").write_text(json.dumps(
            {"edition": edition, "fetched": date.today().isoformat(), "url": url, "bytes": n}, indent=1), encoding="utf-8")
        print("  wrote %s (%.1f MB)" % (path, n / 1e6))
        time.sleep(1)


SOS = "https://www.sos.state.co.us/CCR/"


def get_text(url):
    """A page's HTML (robots.txt obeyed, a second between requests)."""
    if not allowed(url):
        sys.exit("robots.txt disallows %s; not fetched" % url)
    time.sleep(1)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        sys.exit("%s: %s" % (url, e))


def sos_url(path):
    return urllib.parse.quote(urllib.parse.urljoin(SOS, path.replace("&amp;", "&")), safe=":/?&=%#;,+")


def fetch_co_ccr(series, force=False):
    """8 CCR 1404-1: the Secretary of State's department list -> the agency's rule list -> the rule's page ->
    its current version, as the Word document (the PDF is the official one; the DOCX is the same text,
    easier to read). Files: <law_dir>/co/ccr/8-1404-1.docx + .json {effective, version_id, url, ...}."""
    import html as _html, re
    for ser in series:
        m = re.fullmatch(r"(\d+)-(\d{3,4})-(\d+)", ser)
        if not m:
            sys.exit("give the rule as volume-agency-number, e.g. 8-1404-1 for 8 CCR 1404-1")
        vol, agency, num = m.groups()
        name = "%s CCR %s-%s" % (vol, agency, num)
        path = state_dir("CO") / "ccr" / ("%s.docx" % ser)
        have = local_meta(path)
        if have and not force:
            print("%s: local, effective %s (fetched %s); --force to check for a newer version" % (name, have["effective"], have["fetched"]))
            continue
        page = get_text(SOS + "NumericalDeptList.do")
        link = re.search(r"""href=["']([^"']*agencyName=%s[ %%][^"']*)["']""" % agency, page)
        if not link:
            sys.exit("%s: no agency %s in the Secretary of State's department list" % (name, agency))
        page = get_text(sos_url(link.group(1)))
        link = re.search(r"""href=["']([^"']*DisplayRule\.do[^"']*seriesNum=%s[^"']*)["']""" % re.escape(name).replace("\\ ", "(?: |%20|\\+)"), page)
        if not link:
            sys.exit("%s: not in the agency's rule list" % name)
        page = get_text(sos_url(_html.unescape(link.group(1))))
        cur = page                          # the current version's row comes first, before the archived ones
        word = re.search(r"OpenRuleWordVersion\('(\d+)'", cur)
        eff = re.search(r"OpenRuleWindow\('\d+'[^>]*>\s*(\d{2})/(\d{2})/(\d{4})", cur)
        if not word:
            sys.exit("%s: no current Word version on the rule's page" % name)
        url = sos_url("GenerateRulePdf.do?type=word&ruleVersionId=%s&fileName=%s" % (word.group(1), name))
        print("%s, current version %s ..." % (name, word.group(1)), flush=True)
        n = download(url, path)
        effective = "%s-%s-%s" % (eff.group(3), eff.group(1), eff.group(2)) if eff else ""
        path.with_suffix(".json").write_text(json.dumps(
            {"effective": effective, "version_id": word.group(1), "fetched": date.today().isoformat(), "url": url, "bytes": n},
            indent=1), encoding="utf-8")
        print("  wrote %s (effective %s, %.1f MB)" % (path, effective or "?", n / 1e6))


# The U.S. Constitution: House Document 110-50 on govinfo ("The Constitution of the United States of America,
# as amended", with the amendments' ratification notes). Its HTML rendition if govinfo has one, else the PDF.
US_CONST = ["https://www.govinfo.gov/content/pkg/CDOC-110hdoc50/html/CDOC-110hdoc50.htm",
            "https://www.govinfo.gov/content/pkg/CDOC-110hdoc50/pdf/CDOC-110hdoc50.pdf"]


def fetch_us_const(items=None, force=False):
    d = state_dir("US") / "const"
    have = [p for p in (d / "constitution.htm", d / "constitution.pdf") if local_meta(p)]
    if have and not force:
        print("U.S. Constitution: local (%s, fetched %s); --force to replace" % (have[0].name, local_meta(have[0])["fetched"]))
        return
    for url in US_CONST:
        path = d / ("constitution" + (".htm" if url.endswith(".htm") else ".pdf"))
        print("U.S. Constitution (%s) ..." % url.rsplit("/", 2)[-2], flush=True)
        try:
            n = download(url, path)
        except SystemExit as e:                # no HTML rendition: try the PDF
            print("  %s" % e.code)
            continue
        path.with_suffix(".json").write_text(json.dumps(
            {"edition": "H. Doc. 110-50", "fetched": date.today().isoformat(), "url": url, "bytes": n}, indent=1), encoding="utf-8")
        print("  wrote %s (%.1f MB)" % (path, n / 1e6))
        return
    sys.exit("U.S. Constitution: neither govinfo rendition could be fetched")


FETCHERS = {("CO", "crs"): fetch_co_crs, ("CO", "ccr"): fetch_co_ccr, ("US", "const"): fetch_us_const}


def cmd_fetch(args):
    key = (args.state.upper(), args.code.lower())
    if key not in FETCHERS:
        sys.exit("no reader for %s %s yet (have: %s)" % (key[0], key[1], ", ".join("%s %s" % k for k in FETCHERS)))
    FETCHERS[key](args.items, args.force)


def cmd_list(args):
    base = cfr.law_dir()
    rows = sorted(p for p in base.glob("*/*/*.json") if p.parts[-3] != "cfr") if base.is_dir() else []
    if not rows:
        print("no state law in %s (tribonian state fetch CO crs <title>)" % base)
        return
    for p in rows:
        m = json.loads(p.read_text(encoding="utf-8"))
        print("%-3s %-4s %-14s %s  fetched %s  %6.1f MB" % (p.parts[-3].upper(), p.parts[-2], p.stem,
              "%s edition" % m["edition"] if "edition" in m else "effective %s" % m.get("effective", "?"), m["fetched"], m["bytes"] / 1e6))
    print("in %s" % base)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download a state's code, title by title, unless already local")
    f.add_argument("state", help="postal code, e.g. CO")
    f.add_argument("code", help="crs (Colorado Revised Statutes; title 00 is the constitution) or ccr (Code of Colorado Regulations)")
    f.add_argument("items", nargs="*", help="crs: titles, e.g. 26.5 00; ccr: rules, e.g. 8-1404-1; US const: none")
    f.add_argument("--force", action="store_true", help="download again whatever is local")
    f.set_defaults(func=cmd_fetch)
    l = sub.add_parser("list", help="what is stored here")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
