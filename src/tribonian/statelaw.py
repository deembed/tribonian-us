"""statelaw.py -- state law, fetched per state from its own publisher (scope-scotus-law.md §3).
One reader per state, written when a docket needs that state; the registry of sources is
data/state_law_sources.toml. Colorado so far:

    tribonian state fetch CO crs 26.5 00      C.R.S. titles (00 = the Colorado Constitution)
    tribonian state fetch CO crs 26.5 --force download again
    tribonian state fetch CO ccr 8-1404-1     a rule of the Code of Colorado Regulations (8 CCR 1404-1)
    tribonian state fetch US const            the U.S. Constitution (govinfo, H. Doc. 110-50)
    tribonian state notes                     (re)write the Obsidian notes -> md-state/ (fetch does it too)
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


# ---------------------------------------------------------------- notes (Obsidian): md-state/<state>/...

def notes_dir():
    return tconfig.WORK / "md-state"


def _md_rows(rows, known):
    """lawtext rows (fmt="md") -> Markdown paragraphs: leading labels bold, cites of existing notes linked."""
    import re
    out = []
    for depth, label, heading, text in rows:
        if heading and not text:
            out += ["", "## %s%s" % (label + " " if label else "", heading), ""]
            continue
        m = re.match(r"((?:\([A-Za-z0-9.]{1,6}\)\s*)+)(.*)", text)
        t = "**%s** %s" % (m.group(1).strip(), m.group(2)) if m else text
        out += ["%s%s" % ("**%s** " % label if label else "", cfr._link(t, known)), ""]
    return out


def _note(path, cite, heading, fields, body):
    from tribonian import case2md as c
    L = c.yaml_head([("title", cite), ("heading", heading or None), ("citation", cite)] + fields + [("synthetic", False), ("aliases", [cite])])
    L += ["", "# %s%s" % (c.esc(cite, "md"), " — " + c.esc(heading, "md") if heading else ""), ""] + body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")


def write_notes(quiet=False):
    """Notes for every state text stored here: a C.R.S. section each (co-crs-26.5-4-205), the Colorado and U.S.
    Constitutions an article or amendment each (co-const-art-9, us-const-amend-1), a Colorado rule each
    (co-ccr-8-1404-1). Names match lawcites.note_name, so cites in the case and docket notes link to them."""
    import re
    from tribonian import case2md as c, lawcites, lawtext
    known = c.statute_notes()
    plans = []                                     # (path, cite, heading, fields, rows)
    crs = state_dir("CO") / "crs"
    for f in sorted(crs.glob("title-*.htm")) if crs.is_dir() else []:
        meta, t = local_meta(f) or {}, f.stem.split("-", 1)[1]
        fields = [("source", "olls.info (Colorado Office of Legislative Legal Services)"), ("edition", c.Raw(meta.get("edition", ""))),
                  ("retrieved", c.Raw(meta.get("fetched", "")))]
        paras = lawtext._co_paras(t if t != "00" else "0")
        if t == "00":                              # the Colorado Constitution: one note per article
            for text, bold, centered in paras:
                m = re.fullmatch(r"ARTICLE ([IVXL]+)", text)
                if not (m and centered):
                    continue
                n = c.from_roman(m.group(1))
                head, rows = lawtext.co_const(str(n), None, "md") or ("", [])
                body = []
                for _, label, heading, _ in rows:
                    sec = label.replace("§ ", "")
                    got = lawtext.co_const(str(n), sec, "md")
                    body += [(0, label, heading, "")] + (got[1] if got else [])
                plans.append((notes_dir() / "co" / "const" / ("co-const-art-%d.md" % n), "Colo. Const. art. %s" % m.group(1),
                              re.sub(r"^Article [IVXL]+\.\s*", "", head), fields, body))
            continue
        for text, bold, centered in paras:
            m = lawtext.CRS_START.match(text)
            if not (bold and m):
                continue
            got = lawtext.co_crs(m.group(1), "md")
            if got:
                plans.append((notes_dir() / "co" / "crs" / ("t" + t) / ("co-crs-%s.md" % m.group(1)),
                              "Colo. Rev. Stat. § %s" % m.group(1), got[0].rstrip("."), fields, got[1]))
    ccr = state_dir("CO") / "ccr"
    for f in sorted(ccr.glob("*.docx")) if ccr.is_dir() else []:
        meta = local_meta(f) or {}
        got = lawtext.co_ccr(f.stem, None, "md")
        if got:
            vol, rule = f.stem.split("-", 1)
            plans.append((notes_dir() / "co" / "ccr" / ("co-ccr-%s.md" % f.stem), "%s Colo. Code Regs. %s" % (vol, rule), got[0],
                          [("source", "sos.state.co.us (Colorado Secretary of State)"), ("effective", c.Raw(meta.get("effective", ""))),
                           ("retrieved", c.Raw(meta.get("fetched", "")))], got[1]))
    us = state_dir("US") / "const" / "constitution.htm"
    if us.exists():
        meta = local_meta(us) or {}
        fields = [("source", "govinfo.gov, H. Doc. 110-50"), ("retrieved", c.Raw(meta.get("fetched", "")))]
        for kind, n in sorted(k for k in (lawtext._us_const() or {}) if k[0] != "preamble"):
            got = lawtext.us_const(kind, str(n), None, "md")
            if got:
                roman = {v: k_ for k_, v in lawcites.ROMAN.items()}[n]
                rows = [(0, label, "", text) for _, label, _, text in got[1]]
                plans.append((notes_dir() / "us" / "const" / ("us-const-%s-%d.md" % (kind, n)),
                              "U.S. Const. %s %s" % ("amend." if kind == "amend" else "art.", roman), "", fields, rows))
    known |= {p.stem for p, *_ in plans}
    for path, cite, heading, fields, rows in plans:
        _note(path, cite, heading, fields, _md_rows(rows, known))
    if not quiet:
        print("%d state-law notes -> %s" % (len(plans), notes_dir()))


def cmd_notes(args):
    write_notes()


def cmd_fetch(args):
    key = (args.state.upper(), args.code.lower())
    if key not in FETCHERS:
        sys.exit("no reader for %s %s yet (have: %s)" % (key[0], key[1], ", ".join("%s %s" % k for k in FETCHERS)))
    FETCHERS[key](args.items, args.force)
    write_notes()


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
    nt = sub.add_parser("notes", help="(re)write the Obsidian notes of everything stored here -> md-state/ (fetch does it too)")
    nt.set_defaults(func=cmd_notes)
    l = sub.add_parser("list", help="what is stored here")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
