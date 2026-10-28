"""statelaw.py -- state law, fetched per state from its own publisher (scope-scotus-law.md §3).
One reader per state, written when a docket needs that state; the registry of sources is
data/state_law_sources.toml. Colorado so far:

    tribonian state fetch CO crs 26.5 00      C.R.S. titles (00 = the Colorado Constitution)
    tribonian state fetch CO crs 26.5 --force download again
    tribonian state fetch CO ccr 8-1404-1     a rule of the Code of Colorado Regulations (8 CCR 1404-1)
    tribonian state fetch US const            the U.S. Constitution (govinfo, H. Doc. 110-50)
    tribonian state fetch CO const            the Colorado Constitution (C.R.S. title 00)
    tribonian state build CO crs 26.5         source file -> USLM XML + verify report (law/co/uslm/crs/)
    tribonian state build CO crs --all        every local title of that code
    tribonian state notes                     (re)write the Obsidian notes -> md-state/ (fetch does it too)
    tribonian state list                      what is stored here
    tribonian state status                    the 51-jurisdiction tracker (docs/state-tracker.toml): who is on what
    tribonian state cites [CO TX ...]         check the [cite] tables against their examples

Every state code has a formula (data/state_formulas.toml, data/formulas/XX.toml): fetched by the generic
downloader and converted by a shared parser family (uslm/), statutes, constitutions and regulations alike
(8 CCR: kind = "regs", fetch = "resolve"). Each formula's [cite] table says how courts cite it (statecites.py);
`tribonian state cites` checks the tables. Only the U.S. Constitution keeps its own reader below.

C.R.S.: the Office of Legislative Legal Services' per-title HTML (olls.info/crs/crs<edition>-title-NN.htm),
the edition current through the latest regular session. Personal use; republishing needs a statement to
the Committee on Legal Services (C.R.S. 2-5-118). Files: <law_dir>/co/crs/title-26.5.htm + .json.
robots.txt is obeyed: a disallowed URL is reported, not fetched.
"""
import argparse, json, os, pathlib, re, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from datetime import date

from tribonian import cfr, tconfig

UA = cfr.UA
_robots = {}


def state_dir(st):
    return cfr.law_dir() / st.lower()


DELAY = 0.5            # seconds between the starts of two requests to one host; a formula's `delay` overrides,
_last = {}             # and a robots.txt Crawl-delay is always honoured when it is longer


def pace(url):
    """Wait until `DELAY` (or the host's robots.txt Crawl-delay, if longer) has passed since the last request to the
    same host. Measured from that request's start, so the download time itself counts toward the gap."""
    host = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    wait = DELAY
    rp = _robots.get(host)
    try:
        cd = rp.crawl_delay(UA) if rp is not None else None
    except Exception:
        cd = None
    if cd:
        wait = max(wait, float(cd))
    gap = time.monotonic() - _last.get(host, -1e9)
    if gap < wait:
        time.sleep(wait - gap)
    _last[host] = time.monotonic()


def allowed(url):
    host = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if host not in _robots:
        _robots[host] = cfr.read_robots(host, UA)
    return _robots[host].can_fetch(UA, url)


def _env_fill(url):
    def val(m):
        v = os.environ.get(m.group(1))
        if not v:
            sys.exit("%s is not set in the environment (needed for %s)" % (m.group(1), url))
        return urllib.parse.quote(v, safe="")
    return re.sub(r"%7Benv:(\w+)%7D|\{env:(\w+)\}", lambda m: val(type("M", (), {"group": lambda self, i: m.group(1) or m.group(2)})()), url)


_REJECT = None          # formula `reject = regex`: set by fetch_formula for its run


def download(url, path):
    """url -> path (via a .part file). Returns bytes written, or exits with the reason."""
    url = urllib.parse.quote(url, safe=":/?&=%#+,;@~!$'()*[]")    # raw spaces in listed links (Illinois' "Ch 0005/")
    shown = url                    # {env:NAME} in a URL: a key read from the environment at request time, never
    url = _env_fill(url)           # written to file names, .meta files or messages (New York's API key)
    if not allowed(url):
        rp = _robots["{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))]
        sys.exit("%s; not fetched: %s" % (getattr(rp, "reason", "robots.txt disallows it"), shown))
    pace(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with cfr.urlopen(req, timeout=120) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        sys.exit("%s: HTTP %d %s" % (shown, e.code, e.reason))
    except urllib.error.URLError as e:
        sys.exit("%s: %s" % (shown, e.reason))
    if _REJECT is not None and not body.startswith(b"PK\x03\x04") and _REJECT.search(body[:200000].decode("utf-8", "replace")):
        # an app answers every unknown path with 200 and its page shell (Texas): never save that as law
        sys.exit("%s: the answer (%d bytes: %r) matches the formula's reject pattern (not a law page)"
                 % (shown, len(body), body[:60]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return len(body)


_PB_OPENER = None


def _pb_open(url, data=None):
    """One request in a postback session (cookies kept, TLS as download()); robots.txt obeyed; 1 s apart."""
    global _PB_OPENER
    import http.cookiejar
    if _PB_OPENER is None:
        _PB_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                                                 urllib.request.HTTPSHandler(context=cfr.tls_context()))
    if not allowed(url):
        sys.exit("robots.txt disallows it; not fetched: %s" % url)
    pace(url)
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    try:
        with _PB_OPENER.open(req, timeout=120) as r:
            body, final = r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        sys.exit("%s: HTTP %d %s" % (url, e.code, e.reason))
    except urllib.error.URLError as e:
        sys.exit("%s: %s" % (url, e.reason))
    pass  # pacing: pace() before each request
    return body.decode("utf-8", "replace"), final


def postback_page(url, steps):
    """ASP.NET WebForms tables of contents (Louisiana): links are javascript:__doPostBack('target','arg'). From the
    page at url, each step is a regex on a link's text: the form's hidden fields are POSTed back with that link's
    __EVENTTARGET / __EVENTARGUMENT, and the answer is the next page. A step {each = regex} posts EVERY matching link
    of the current page and joins the answers (a title page that lists chapters, each a postback). -> html text."""
    import html as _h
    page, cur = _pb_open(url)

    def posts(page, cur, rx, every):
        form = re.search(r'<form[^>]*action="([^"]*)"', page)
        action = urllib.parse.urljoin(cur, _h.unescape(form.group(1))) if form else cur
        hidden = dict((_h.unescape(n), _h.unescape(v)) for n, v in re.findall(
            r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page))
        links = [(_h.unescape(t), _h.unescape(a), _h.unescape(b)) for a, b, t in re.findall(
            r"""href="javascript:__doPostBack\(&#39;([^&]*)&#39;,&#39;([^&]*)&#39;\)"[^>]*>\s*([^<]*?)\s*<""", page)]
        hits = [(t, a, b) for t, a, b in links if re.search(rx, t)]
        if not hits:
            sys.exit("postback: no link matching %r on %s" % (rx, cur))
        out = []
        for t, a, b in (hits if every else hits[:1]):
            data = dict(hidden, __EVENTTARGET=a, __EVENTARGUMENT=b)
            out.append(_pb_open(action, urllib.parse.urlencode(data).encode()))
        return out

    for step in steps:
        every = isinstance(step, dict)
        rx = step["each"] if every else step
        got = posts(page, cur, rx, every)
        if every:
            page = "\n".join(p for p, _ in got)
            cur = got[-1][1]
        else:
            page, cur = got[0]
    return page


def crs_title_name(t):
    """'0' -> '00', '1' -> '01', '26.5' -> '26.5' (the OLLS file names)."""
    whole, _, frac = t.partition(".")
    return "%02d" % int(whole) + ("." + frac if frac else "")


def meta_path(path):
    """Where a downloaded file's metadata goes: x.htm -> x.json; a JSON download keeps its own name, so
    _list.json -> _list.meta.json."""
    return path.with_name(path.stem + ".meta.json") if path.suffix == ".json" else path.with_suffix(".json")


def local_meta(path):
    meta = meta_path(path)
    if not (path.exists() and meta.exists()):
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return None


SOS = "https://www.sos.state.co.us/CCR/"


def get_text(url):
    """A page's HTML (robots.txt obeyed, a second between requests)."""
    if not allowed(url):
        sys.exit("robots.txt disallows %s; not fetched" % url)
    pace(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with cfr.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        sys.exit("%s: %s" % (url, e))


def sos_url(path):
    return urllib.parse.quote(urllib.parse.urljoin(SOS, path.replace("&amp;", "&")), safe=":/?&=%#;,+")


def resolve_co_sos(f, ser):
    """8 CCR 1404-1 ('8-1404-1'): the Secretary of State's department list -> the agency's rule list -> the rule's
    page -> its current version, as the Word document (the PDF is the official one; the DOCX is the same text,
    easier to read). -> (url, {"edition": effective date, "effective": ..., "version_id": ...})."""
    import html as _html
    m = re.fullmatch(r"(\d+)-(\d{3,4})-(\d+)", ser)
    if not m:
        sys.exit("give the rule as volume-agency-number, e.g. 8-1404-1 for 8 CCR 1404-1")
    vol, agency, num = m.groups()
    name = "%s CCR %s-%s" % (vol, agency, num)
    page = get_text(SOS + "NumericalDeptList.do")
    link = re.search(r"""href=["']([^"']*agencyName=%s[ %%][^"']*)["']""" % agency, page)
    if not link:
        sys.exit("%s: no agency %s in the Secretary of State's department list" % (name, agency))
    page = get_text(sos_url(link.group(1)))
    link = re.search(r"""href=["']([^"']*DisplayRule\.do[^"']*seriesNum=%s[^"']*)["']""" % re.escape(name).replace("\\ ", "(?: |%20|\\+)"), page)
    if not link:
        sys.exit("%s: not in the agency's rule list" % name)
    cur = get_text(sos_url(_html.unescape(link.group(1))))   # the current version's row comes first, before the archived ones
    word = re.search(r"OpenRuleWordVersion\('(\d+)'", cur)
    eff = re.search(r"OpenRuleWindow\('\d+'[^>]*>\s*(\d{2})/(\d{2})/(\d{4})", cur)
    if not word:
        sys.exit("%s: no current Word version on the rule's page" % name)
    effective = "%s-%s-%s" % (eff.group(3), eff.group(1), eff.group(2)) if eff else ""
    return (sos_url("GenerateRulePdf.do?type=word&ruleVersionId=%s&fileName=%s" % (word.group(1), name)),
            {"edition": effective or "current", "effective": effective, "version_id": word.group(1)})


# fetch = "resolve": a formula names one of these (resolver = "co-sos"); it finds an item's current file through
# the publisher's pages (a rule's latest version has no fixed URL). Regulations codes mostly need one.
RESOLVERS = {"co-sos": resolve_co_sos}


def _fetch_resolve(st, code, f, d, name, force):
    path = d / f["file"].format(item=name)
    have = local_meta(path)
    if have and not force:
        print("%s %s %s: local, %s (fetched %s); --force to check for a newer version" % (st, code, name, have.get("edition"), have.get("fetched")))
        return
    fn = RESOLVERS.get(f.get("resolver"))
    if fn is None:
        sys.exit("%s %s: unknown resolver %r (known: %s)" % (st, code, f.get("resolver"), ", ".join(RESOLVERS)))
    url, extra = fn(f, name)
    print("%s %s %s (%s) ..." % (st, code, name, extra.get("edition", "")), flush=True)
    n = download(url, path)
    meta_path(path).write_text(json.dumps(dict(extra, fetched=date.today().isoformat(), url=url, bytes=n), indent=1), encoding="utf-8")
    print("  wrote %s (%.2f MB)" % (path, n / 1e6))


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


FETCHERS = {("US", "const"): fetch_us_const}


# ---------------------------------------------------------------- formulas (data/state_formulas.toml)

_FORMULAS = None


def formulas():
    """{(STATE, code): formula}. Needs Python 3.11+ (tomllib) or the tomli package."""
    global _FORMULAS
    if _FORMULAS is None:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        # data/state_formulas.toml, plus one file per state in data/formulas/ (XX.toml), so agents working on
        # different states never edit the same file. A state's tables live in one place or the other.
        flat = {}
        for path in [tconfig.PKG / "data" / "state_formulas.toml"] + sorted((tconfig.PKG / "data" / "formulas").glob("*.toml")):
            try:
                with open(path, "rb") as fh:
                    raw = tomllib.load(fh)
            except tomllib.TOMLDecodeError as e:
                if path.parent.name != "formulas":
                    raise
                print("formulas/%s skipped: %s" % (path.name, e), file=sys.stderr)   # one state's broken file
                continue                                                              # must not stop the rest
            bad = [k for k, v in raw.items() if not isinstance(v, dict) or not all(isinstance(x, dict) for x in v.values())]
            if bad and path.parent.name == "formulas":     # e.g. keys written above the first [ST.code] header
                print("formulas/%s skipped: %s is not a table of [ST.code] formulas" % (path.name, ", ".join(bad)),
                      file=sys.stderr)
                continue
            for st, codes in raw.items():
                for code, f in codes.items():
                    if (st, code) in flat:
                        sys.exit("formula %s.%s is defined twice (second in %s)" % (st, code, path.name))
                    flat[(st, code)] = f
        # a table with no `fetch` is cite-only (statecites.py): a code we recognize in citations but have no
        # source for (Purdon's Pennsylvania Statutes, a LexisNexis-only state); it says why in `source_error`
        global _CITE_ONLY
        _CITE_ONLY = {k: f for k, f in flat.items() if "fetch" not in f and "inherit" not in f}
        _FORMULAS = {k: _inherit(flat, k, f) for k, f in flat.items() if k not in _CITE_ONLY}
    return _FORMULAS


_CITE_ONLY = {}


def cite_only():
    """{(STATE, code): table} for codes with a [cite] table and no source (no `fetch`)."""
    formulas()
    return _CITE_ONLY


def uslm_file(st, code, item):
    """Where `build` writes one item's USLM: law/<st>/uslm/<code>/<name>.xml, named as each strategy names it
    (template, zip, scroll: the source file's stem; index: title-<item>; list: <item>). None if the item
    has not been built."""
    import pathlib
    f = formulas().get((st, code))
    if f is None or item is None:
        return None
    name = item_name(f.get("item"), item)
    d = state_dir(st) / "uslm" / code
    cands = []
    if f.get("file"):
        cands.append(pathlib.Path(f["file"].format(item=name) if "{item}" in f["file"] else f["file"]).stem)
    cands += ["title-%s" % name, name, str(item)]
    for c in cands:
        p = d / (c + ".xml")
        if p.exists():
            return p
    if d.is_dir():                                   # a build named in another case (Michigan's CHAP26.xml)
        low = {x.lower() for x in cands}
        for p in d.glob("*.xml"):
            if p.stem.lower() in low:
                return p
    return None


def fetch_and_build(st, code, item, force=False):
    """One item, fetched (unless its source is local) and built to USLM. Raises SystemExit with the reason."""
    fetch_formula(st, code, [item] if item else [], force)
    if build_formula(st, code, [item] if item else []):
        print("  %s %s %s: built, but verify reports problems (the text is still used)" % (st, code, item))


NOT_INHERITED = ("cite", "catalog")    # how a code is cited, and what items it has, are its own (a constitution
                                       # inheriting a statute formula must not read statute cites)


def _inherit(flat, key, f, depth=0):
    """inherit = "MD.code": start from that formula, override with this one's keys (tables merged one level;
    replace = ["containers", ...] names tables taken whole from this formula instead of merged)."""
    if "inherit" not in f or depth > 5:
        return f
    st, _, code = f["inherit"].partition(".")
    base = _inherit(flat, (st, code), flat[(st, code)], depth + 1)
    out = {k: v for k, v in base.items() if k not in NOT_INHERITED}
    for k, v in f.items():
        merge = isinstance(v, dict) and isinstance(base.get(k), dict) and k not in f.get("replace", [])
        out[k] = dict(base[k], **v) if merge else v
    out.pop("inherit", None)
    out.pop("replace", None)
    return out


def item_name(fmt, item):
    """'%02d' applied to the leading whole number: '1' -> '01', '26.5' -> '26.5', '4a' -> '04a', '00' -> '00'."""
    m = re.match(r"(\d+)(.*)", str(item))
    return fmt % int(m.group(1)) + m.group(2) if fmt and m else str(item)


def formula_dir(st, code, f):
    return state_dir(st) / f.get("dir", code)


def _get_one(url, path, edition, force, label, missing_ok=False):
    """One file, unless the same edition is already local. -> True when the file is local afterwards."""
    have = local_meta(path)
    if have and have.get("url") and have["url"] != url:
        # two pages with one local name (Arizona's ars/1/00101.htm and ars/19/00101.htm under one folder): never
        # let one silently stand in for the other
        raise SystemExit("%s: local file %s came from %s, not %s -- page names collide; set save_as = \"url\""
                         % (label, path.name, have["url"], url))
    if have and str(have.get("edition")) == str(edition) and not force:
        print("%s: local, %s (fetched %s); --force to replace" % (label, edition, have["fetched"]))
        return True
    print("%s (%s) ..." % (label, edition), flush=True)
    try:
        n = download(url, path)
    except SystemExit as e:
        if missing_ok and "HTTP 404" in str(e.code):
            print("  none (404): %s" % url)
            return False
        raise
    meta_path(path).write_text(json.dumps(
        {"edition": edition, "fetched": date.today().isoformat(), "url": url, "bytes": n}, indent=1), encoding="utf-8")
    print("  wrote %s (%.2f MB)" % (path, n / 1e6))
    pass  # pacing: pace() before each request
    return True


class _EnvField:
    """{env:NAME} survives str.format as itself, so download() fills it at request time (a key never stored)."""
    def __format__(self, spec):
        return "{env:%s}" % spec


def url_fields(f, name):
    """Fields a template URL may use: {item} as named, {edition}, and for a numbered item {n} (its leading
    number, format it as {n:04d}), {id} (from the formula's ids table), {lo}/{hi} (its hundred-range: 768 -> 700, 799; Florida's
    0700-0799/0768/0768.html is "{lo:04d}-{hi:04d}/{n:04d}/{n:04d}.html") and {suffix} (4a -> a)."""
    m = re.match(r"0*(\d+)(.*)", name)
    n = int(m.group(1)) if m else 0
    return {"env": _EnvField(), "item": name, "edition": f.get("edition", ""), "n": n, "lo": n // 100 * 100, "hi": n // 100 * 100 + 99,
            "suffix": m.group(2) if m else "",
            "id": str(f.get("ids", {}).get(name, "")),   # [ids] table: a chapter's label -> the site's id (New Mexico)
            **_item_groups(f, name)}


def _item_groups(f, name):
    """item_match = regex with named groups: more URL fields from the item ("6A-2" -> {title} 6A, {chapter} 2:
    Rhode Island's TITLE6A/6A-2/ needs the title of a chapter item). A name that does not match adds nothing."""
    m = re.fullmatch(f["item_match"], name) if f.get("item_match") else None
    return {k: v or "" for k, v in m.groupdict().items()} if m else {}


def page_name(f, href):
    """Local file name for a linked page: its last path part (Connecticut's chap_001.htm), or with
    save_as = "path" the whole link made file-safe, for sites whose pages share names across folders."""
    if f.get("save_as") in ("path", "url"):
        return re.sub(r"[^A-Za-z0-9._-]+", "_", href).strip("._")   # "../title1/c001/index.html" must not be a dot file
    return href.rsplit("/", 1)[-1]


OUTLINE = "#outline:"


def html_unescape(t):
    import html as _h
    return _h.unescape(t)


def outline_page(f, name):
    """The synthetic page for an outline heading: `outline_html` with {text} (default a bare <p>), so the formula's
    container rules (any = true) open the chapter / article it names before the sections linked under it."""
    import html as _h
    tpl = f.get("outline_html", '<p class="outline">{text}</p>')
    return ("<html><body>" + tpl.format(text=_h.escape(name[len(OUTLINE):])) + "</body></html>").encode("utf-8")


def walk_index(f, idx, url, ld, fetch=False, force=False, label="", item=""):
    """The pages an index leads to, in reading order: [(file name, url)]. `links` is one regex (index -> pages)
    or a list, one per level (title -> chapters -> sections): every level but the last is a list page, kept in
    <layer>/_idx/. A list page with no links of the next level is itself a text page (mixed depth: Delaware's
    small chapters are text, big ones list subchapters) and is returned as "_idx/<name>". With fetch, missing
    list pages are downloaded on the way; without (build), the walk uses what is local and skips the rest."""
    levels = [re.compile(x.replace("{item}", re.escape(item)))      # {item} in a link regex: one shared index
              for x in (f["links"] if isinstance(f["links"], list) else [f["links"]])]   # page serves every item (Utah)
    enc = f.get("encoding", "utf-8")
    out = []

    def hrefs(path, i):
        seen = []
        for h in levels[i].findall(path.read_bytes().decode(enc, errors="replace")):
            h = h if isinstance(h, str) else h[0]
            if h not in seen:
                seen.append(h)
        return seen

    def name_of(href, u):
        if f.get("save_as") == "url":
            sp = urllib.parse.urlsplit(u)
            return page_name(f, sp.path + ("?" + sp.query if sp.query else ""))
        return page_name(f, href)

    outline = [re.compile(x) for x in (f.get("outline") or [])]

    def natural(h):                       # leaf_sort = "natural": K1 K2 K10, not K1 K10 K2 (folder listings)
        h = re.sub(r"\.s?html?$", "", h, flags=re.I)        # K1 before K1.01 ("." < ".html" otherwise)
        return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", h)]

    def events(path, i):
        """The last level's links, with the outline headings between them when `outline` is set:
        [(href, None)] or [(None, heading text)], in page order."""
        if i != len(levels) - 1 or not outline:
            hs = hrefs(path, i)
            if i == len(levels) - 1 and f.get("leaf_sort") == "natural":
                hs = sorted(hs, key=natural)
            return [(h, None) for h in hs]
        raw = path.read_bytes().decode(enc, errors="replace")
        ev, seen = [], set()
        for m in levels[i].finditer(raw):
            h = m.group(1) if m.re.groups else m.group(0)
            if h not in seen:
                seen.add(h)
                ev.append((m.start(), h, None))
        for rx in outline:
            for m in rx.finditer(raw):
                t = m.group("text") if "text" in rx.groupindex else (m.group(1) if rx.groups else m.group(0))
                t = " ".join(html_unescape(re.sub(r"<[^>]+>", " ", t)).split())
                if t:
                    ev.append((m.start(), None, t))
        return [(h, t) for _, h, t in sorted(ev, key=lambda e: e[0])]

    def visit(path, base, i):
        for href, head in events(path, i):
            if head is not None:          # a heading from the index outline: a synthetic page (build_index)
                out.append((OUTLINE + head, None))
                continue
            u = urllib.parse.urljoin(base, href)
            if i == len(levels) - 1:
                if f.get("rewrite"):      # rewrite = [regex, replacement]: a listed page -> the page to fetch
                    u = re.sub(f["rewrite"][0], f["rewrite"][1], u)     # (Vermont: chapter -> fullchapter)
                    sp = urllib.parse.urlsplit(u)       # the query names the page too (default.aspx?cite=1.04)
                    out.append((page_name(f, sp.path + ("?" + sp.query if sp.query else "")), u))
                else:
                    out.append((name_of(href, u), u))
                continue
            name = name_of(href, u)
            p = ld / "_idx" / name
            if fetch:
                _get_one(u, p, label, force, "  %s list %s" % (label, name))
            if not p.exists():            # a list page not local: listed as a page, so the build counts it
                out.append(("_idx/" + name, u))     # missing and fails, instead of silently losing its sections
                continue
            nxt = next((j for j in range(i + 1, len(levels)) if hrefs(p, j)), None)
            if nxt is not None:           # uneven depth: a chapter page may list parts or, skipping that level,
                visit(p, u, nxt)          # the sections themselves (Idaho)
            else:
                out.append(("_idx/" + name, u))

    if idx.exists():
        visit(idx, url, 0)
        if not out and f.get("empty_index") == "page":   # a title that links no chapters is its own text
            out.append((idx.name, url))                  # (New Hampshire's wholly repealed title IV)
    return list(dict.fromkeys(out))


def fetch_formula(st, code, items, force=False):
    """The generic downloader; robots.txt obeyed, a second between requests. Strategies:
        template  url with {edition} and {item} (and {n} {lo} {hi} {suffix}: url_fields): one file per item
        index     per layer, an index page per item, then every page it links to that matches `links`
                  (Connecticut: title_01.htm lists chap_001.htm ...); `links` as a list walks several
                  levels (title page -> chapter pages -> section pages). Layers stack editions: the
                  revision, then a supplement whose sections replace the revision's (build does that).
    """
    global _REJECT, DELAY
    f = formulas()[(st, code)]
    DELAY = float(f.get("delay", DELAY))      # per state: `delay = 1.0` for a slow or touchy server
    _REJECT = re.compile(f["reject"]) if f.get("reject") else None
    items = items or f.get("items") or []
    if not items:
        sys.exit("%s %s: name what to fetch (e.g. a title number)" % (st, code))
    d = formula_dir(st, code, f)
    failures = []
    for it in items:
        try:
            _fetch_item(st, code, f, d, it, force)
        except SystemExit as e:          # one missing title (a 404, an unassigned number) must not stop the rest
            print("  %s %s %s: %s" % (st, code, it, e.code))
            failures.append(str(it))
    if failures:
        sys.exit("%s %s: %d of %d items not fetched: %s" % (st, code, len(failures), len(items), " ".join(failures[:20])))


def _fetch_item(st, code, f, d, it, force):
    if True:
        name = item_name(f.get("item"), it)
        if f["fetch"] == "template":
            _get_one(f["url"].format(**url_fields(f, name)), d / f["file"].format(item=name),
                     f.get("edition", ""), force, "%s %s %s" % (st, code, name))
        elif f["fetch"] == "list":
            # the section list (JSON) for the item, then one page per entry; the list is saved beside the pages
            d_item = d / name
            listing = d_item / "_list.json"
            _get_one(f["list_url"].format(item=name), listing, date.today().isoformat() if force else "list", force,
                     "%s %s %s section list" % (st, code, name))
            entries = [e[f["list_field"]] for e in json.loads(listing.read_text(encoding="utf-8"))]
            print("  %d entries" % len(entries))
            if f.get("titles_url"):
                _get_one(f["titles_url"], d / "_titles.json", "titles", False, "%s %s article names" % (st, code))
            for e in entries:
                _get_one(f["url"].format(item=name, entry=urllib.parse.quote(e)), d_item / f["file"].format(entry=e),
                         f.get("edition", "current"), force, "  %s %s" % (name, e))
        elif f["fetch"] == "resolve":
            _fetch_resolve(st, code, f, d, name, force)
        elif f["fetch"] == "zip":
            _fetch_zip(st, code, f, d, name, force)
        elif f["fetch"] == "scroll":
            _fetch_scroll(st, code, f, d, name, force)
        elif f["fetch"] == "index":
            for layer in f["layers"]:
                url = layer["url"].format(**url_fields(f, name))   # {item} {id} {n} ... as template URLs
                ld = d / layer["name"]
                idx = ld / f["index_file"].format(item=name)
                steps = layer.get("postback") or f.get("postback")
                if steps:                                # the index is reached by form posts, not a GET
                    fields = url_fields(f, name)
                    steps = [({"each": x["each"].format(**fields)} if isinstance(x, dict) else x.format(**fields))
                             for x in steps]
                    have = local_meta(idx)
                    if have and str(have.get("edition")) == str(layer["name"]) and not force:
                        print("%s %s %s index: local" % (st, code, name))
                    else:
                        print("%s %s %s index (postback %s) ..." % (st, code, name, steps), flush=True)
                        page = postback_page(url, steps)
                        idx.parent.mkdir(parents=True, exist_ok=True)
                        idx.write_text(page, encoding="utf-8")
                        meta_path(idx).write_text(json.dumps({"edition": layer["name"], "fetched": date.today().isoformat(),
                                                              "url": url, "postback": steps}, indent=1), encoding="utf-8")
                elif not _get_one(url, idx, layer["name"], force, "%s %s %s %s index" % (st, code, layer["name"], name),
                                missing_ok=layer.get("optional", False)):
                    continue
                leaves = walk_index(f, idx, url, ld, fetch=True, force=force, label=layer["name"], item=name)
                if not leaves:
                    print("  no pages matching %s under %s" % (f["links"], idx.name))
                bad = []
                for page, page_url in leaves:
                    if page_url is None:                 # an outline heading, not a page
                        continue
                    try:
                        _get_one(page_url, ld / page, layer["name"], force, "  %s %s" % (layer["name"], page))
                    except SystemExit as e:              # one page missing must not stop the title; the
                        print("    %s" % e.code)          # build will report it and fail until it is fetched
                        bad.append(page)
                if bad:
                    sys.exit("%d of %d pages not fetched (%s ...)" % (len(bad), len(leaves), ", ".join(bad[:3])))
        else:
            sys.exit("%s %s: fetch strategy %r not written yet" % (st, code, f["fetch"]))


def _fetch_zip(st, code, f, d, name, force):
    """fetch = "zip": a bulk archive (Indiana's whole code, New Jersey's statutes, a GitHub snapshot), downloaded once
    to <dir>/_bulk/ and reused for every item; then the item's member -- `member`, a regex with the URL fields
    ({item} ...) that must match exactly one name -- is copied out to `file`. The build reads that file as a template
    fetch's file (with `transform` when it is not HTML). url may hold {item} too (one archive per code)."""
    import zipfile
    fields = url_fields(f, name)
    url = f["url"].format(**fields)
    bulk = d / "_bulk" / urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
    if f.get("remote_members") and not bulk.exists():
        # only part of a huge archive is law (California: ~100 MB of its 1.29 GB): copy the members matching
        # remote_members into a local zip by HTTP Range requests, and use that as the archive
        import zipfile as _zf
        rx_m = re.compile(f["remote_members"])
        rf = RemoteFile(url)
        src = _zf.ZipFile(rf)
        bulk.parent.mkdir(parents=True, exist_ok=True)
        tmp = bulk.with_name(bulk.name + ".part")
        n = 0
        with _zf.ZipFile(tmp, "w", _zf.ZIP_DEFLATED) as dst:
            for i in src.infolist():
                if rx_m.search(i.filename) and not i.is_dir():
                    dst.writestr(i.filename, src.read(i))
                    n += 1
                    if n % 5000 == 0:
                        print("  %d members (%d range requests)" % (n, rf.requests), flush=True)
        os.replace(tmp, bulk)
        meta_path(bulk).write_text(json.dumps({"edition": f.get("edition", "current"), "url": url, "members": n,
            "remote_members": f["remote_members"], "fetched": date.today().isoformat()}, indent=1), encoding="utf-8")
        print("  %s: %d members copied from %s (%d range requests)" % (bulk.name, n, url, rf.requests))
    if f.get("manual"):
        # the publisher's server refuses our client (Indiana answers its app shell): the user downloads the archive
        # in a browser and puts it here; we never work around the refusal ourselves
        if not bulk.exists():
            sys.exit("%s %s: download %s in a browser and save it as %s" % (st, code, url, bulk))
        if not local_meta(bulk):
            meta_path(bulk).write_text(json.dumps({"edition": f.get("edition", "current"), "url": url, "manual": True,
                "fetched": date.fromtimestamp(bulk.stat().st_mtime).isoformat()}, indent=1), encoding="utf-8")
    elif not f.get("remote_members"):
        _get_one(url, bulk, f.get("edition", "current"), force, "%s %s bulk %s" % (st, code, bulk.name))
    out = d / f["file"].format(item=name)
    have = local_meta(out)
    bulk_meta = local_meta(bulk) or {}
    if have and have.get("bulk_fetched") == bulk_meta.get("fetched") and not force:
        print("%s %s %s: local (from %s)" % (st, code, name, bulk.name))
        return
    rx = re.compile(f["member"].replace("{item}", re.escape(name)).format(**{k: re.escape(str(v)) for k, v in fields.items() if k != "item"}))
    with zipfile.ZipFile(bulk) as z:
        hits = [n for n in z.namelist() if rx.fullmatch(n)]
        if len(hits) != 1:
            sys.exit("%s %s %s: %d members match %s in %s" % (st, code, name, len(hits), f["member"], bulk.name))
        if f.get("assemble"):
            data = _assemble(f["assemble"], z, hits[0], d, name)
        else:
            data = z.read(hits[0])
    if f.get("split"):                 # one big text file for the whole code (New Jersey's STATUTES.TXT): the item is
        data = _split(f, data, name)   # the run of lines from its own `split` line to the next one
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    meta_path(out).write_text(json.dumps({"edition": f.get("edition", "current"), "fetched": date.today().isoformat(),
                                          "url": url, "member": hits[0], "bulk_fetched": bulk_meta.get("fetched"),
                                          "bytes": len(data)}, indent=1), encoding="utf-8")
    print("  %s %s %s: %s (%.2f MB)" % (st, code, name, hits[0], len(data) / 1e6))


def _split(f, data, name):
    """split = regex with a named group `item`, matched per line: a line that matches starts that item's part."""
    rx = re.compile(f["split"])
    enc = f.get("text_encoding", "cp1252")
    out, cur, found = [], None, False
    for line in data.decode(enc).splitlines(keepends=True):
        m = rx.match(line)
        if m:
            cur = m.group("item")
            found = found or cur == name
        if cur == name:
            out.append(line)
    if not found:
        sys.exit("split: no line for item %s matches %s" % (name, f["split"]))
    return "".join(out).encode(enc)


def _assemble(kind, z, member, d, name):
    """assemble = "<kind>": an item made of many archive members, joined into one HTML page (checked lossless).
    oll-xml: an Open Law Library title (DC): `member` is the title's index.xml; its folder's members (the
    xi:included section files) are unpacked to <dir>/_work/<item>/ and joined in the index's order."""
    import shutil, importlib
    if kind == "ca-tables":            # California's database tables -> one code's HTML (transforms/capublic.py)
        return importlib.import_module("tribonian.uslm.transforms.capublic").assemble(z, name)
    if kind != "oll-xml":
        sys.exit("unknown assemble %r" % kind)
    from tribonian.uslm.transforms import oll_xml
    folder = member.rsplit("/", 1)[0] + "/"
    work = d / "_work" / name          # files are overwritten in place (no delete: the index decides what is read)
    work.mkdir(parents=True, exist_ok=True)
    for n in z.namelist():
        if n.startswith(folder) and not n.endswith("/"):
            dest = work / n[len(folder):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(n))
    out = work / "_page.html"
    oll_xml.main(str(work / member.rsplit("/", 1)[1]), str(work / "sections"), str(out))
    return out.read_bytes()


def _fetch_scroll(st, code, f, d, name, force):
    """fetch = "scroll": a document served in windows (Wisconsin's docs.legis: every page shows ~30-90 lines and
    the rest loads as you scroll). url has {start} (and the template fields): the first window; next = regex whose
    group 1 is the next window's link; join_key = the attribute that names a block (data-path). Windows are fetched
    until one has no block (past the end) or brings nothing new (max_windows guards), and their blocks are joined in
    order, each key once, into one file: <html><body><div id="document">...</div></body></html> (join_root)."""
    from lxml import html as lhtml
    out = d / f["file"].format(item=name)
    edition = f.get("edition", "current")
    have = local_meta(out)
    if have and str(have.get("edition")) == str(edition) and not force:
        print("%s %s %s: local, %s (fetched %s); --force to replace" % (st, code, name, edition, have["fetched"]))
        return
    key = f.get("join_key", "data-path")
    nxt_rx = re.compile(f["next"])
    url = f["url"].format(start=f.get("start", 0), **url_fields(f, name))
    first = url
    seen, blocks, windows = set(), [], 0
    tmp = d / "_scroll" / (name + ".window")
    print("%s %s %s (%s): windows from %s" % (st, code, name, edition, url), flush=True)
    while url and windows < f.get("max_windows", 2000):
        download(url, tmp)
        windows += 1
        pass  # pacing: pace() before each request
        raw = tmp.read_bytes().decode(f.get("encoding", "utf-8"), errors="replace")
        if ("%s=" % key) not in raw:
            break                                          # past the end: no blocks
        doc = lhtml.fromstring(raw) if "<html" in raw[:2000].lower() else lhtml.fragment_fromstring(raw, create_parent="div")
        new = 0
        for el in doc.iter():
            k = el.get(key) if isinstance(el.tag, str) else None
            if k is None or k in seen:
                continue
            if any(a.get(key) is not None for a in el.iterancestors()):
                continue                                   # only top-level blocks
            seen.add(k)
            new += 1
            el.tail = None
            blocks.append(lhtml.tostring(el, encoding="unicode"))
        if not new:
            break
        m = nxt_rx.search(raw)
        url = urllib.parse.urljoin(first, m.group(1)) if m else None
    root = f.get("join_root", '<div id="document">')
    tag = re.match(r"<(\w+)", root).group(1)
    body = "<html><body>%s%s</%s></body></html>" % (root, "".join(blocks), tag)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    if tmp.exists():
        tmp.unlink()
    meta_path(out).write_text(json.dumps({"edition": edition, "fetched": date.today().isoformat(), "url": first,
                                          "windows": windows, "blocks": len(blocks), "bytes": len(body)}, indent=1),
                              encoding="utf-8")
    print("  wrote %s: %d blocks from %d windows" % (out, len(blocks), windows))


def uslm_path(st, code, src):
    return state_dir(st) / "uslm" / code / (src.stem + ".xml")


def _title_ident(f, name):
    if f.get("title_id"):                   # [regex, replacement] on the item's file name: Montana "0010" -> "t1"
        return "%s/%s" % (f["doc"], re.sub(f["title_id"][0], f["title_id"][1], name))
    m = re.match(r"0*(\d+)(.*)", name)
    return "%s/t%s" % (f["doc"], (m.group(1) + m.group(2)) if m else name)


def build_index(st, code, f, items, everything=False):
    """Index codes (Connecticut): per title, every chapter page of every layer -> one USLM title."""
    from lxml import html as lhtml
    from tribonian.uslm import build, verify
    d = formula_dir(st, code, f)
    base = d / f["layers"][0]["name"]
    if everything:
        rx = re.compile("^" + re.escape(f["index_file"]).replace(re.escape("{item}"), "(.+)") + "$")
        names = sorted(m.group(1) for m in (rx.match(p.name) for p in base.glob("*")) if m) if base.is_dir() else []
    else:
        names = [item_name(f.get("item"), i) for i in items]
    if not names:
        sys.exit("%s %s: nothing to build (fetch first, or name the titles)" % (st, code))
    failed = 0
    for name in names:
        layers, metas, all_missing = [], [], []
        for L in f["layers"]:
            idx = d / L["name"] / f["index_file"].format(item=name)
            if not idx.exists():
                if L is f["layers"][0]:
                    print("%s %s title %s: not local (tribonian state fetch %s %s %s)" % (st, code, name, st, code, name))
                continue
            url = L["url"].format(**url_fields(f, name))
            pages = [p for p, _ in walk_index(f, idx, url, d / L["name"], item=name)]
            missing = [p for p in pages if not p.startswith(OUTLINE) and not (d / L["name"] / p).exists()]
            all_missing += ["%s/%s" % (L["name"], p) for p in missing]
            if missing:
                print("%s %s title %s: %s pages not local (%s ...); fetch again" % (st, code, name, L["name"], ", ".join(missing[:3])))
            layers.append((L["name"], [(p, outline_page(f, p) if p.startswith(OUTLINE) else (d / L["name"] / p).read_bytes())
                                       for p in pages if p not in missing]))
            metas.append("%s fetched %s" % (L["name"], (local_meta(idx) or {}).get("fetched", "?")))
        if not layers:
            failed += 1
            continue
        doc = lhtml.fromstring((base / f["index_file"].format(item=name)).read_bytes())
        t = f.get("title", {})
        from tribonian.uslm.families.html_paragraphs import selector as _sel
        for br in doc.iter("br"):                 # "TITLE IV<br>ELECTIONS" reads "TITLE IV ELECTIONS"
            br.tail = " " + (br.tail or "")
        txt = lambda sel: next((" ".join(e.text_content().split()) for e in doc.iter() if _sel(sel)(e)), "") if sel else ""
        ident = _title_ident(f, name)
        incomplete = [L for L, pages in layers if not pages] if layers else ["(all)"]
        if not layers or not layers[0][1]:
            print("%s %s title %s: FAIL -- no pages of the base layer are local (fetch again)" % (st, code, name))
            failed += 1
            continue
        tree, report = build.convert_layers(layers, f, ident, txt(t.get("number")), txt(t.get("name")),
                                            {"edition": " + ".join(L[0] for L in layers), "retrieved": "; ".join(metas),
                                             "formula": "%s.%s" % (st, code)})
        report["missing_pages"] = all_missing
        if all_missing:                   # a build from a partial download must not pass
            report["ok"] = False
        out = state_dir(st) / "uslm" / code / ("title-%s.xml" % name)
        build.write(tree, report, out)
        print(verify.summary("%s %s title %s" % (st, code, name), report))
        for layer, log in list(report["layers"].items())[1:]:
            print("  %-13s replaced %d sections, added %d, new chapters %d" % (layer, len(log["replaced"]), len(log["added"]), len(log["chapters_added"])))
        print("  -> %s" % out)
        failed += not report["ok"]
    return failed


def build_list(st, code, f, items, everything=False):
    """List codes (Maryland): per item (an article), its saved section list in order -> one USLM file."""
    from tribonian.uslm import build, verify
    d = formula_dir(st, code, f)
    if everything:
        names = sorted(p.parent.name for p in d.glob("*/_list.json")) if d.is_dir() else []
        if f.get("items_match"):
            names = [n for n in names if re.fullmatch(f["items_match"], n)]
    else:
        names = [item_name(f.get("item"), i) for i in items]
    if not names:
        sys.exit("%s %s: nothing to build (fetch first, or name the items)" % (st, code))
    titles = {}
    tf = d / "_titles.json"
    if tf.exists():
        for t in json.loads(tf.read_text(encoding="utf-8")):
            titles[t["Value"]] = re.sub(r"\s*-\s*\([^)]*\)\s*$", "", t["DisplayText"])
    failed = 0
    for name in names:
        listing = d / name / "_list.json"
        if not listing.exists():
            print("%s %s %s: not local (tribonian state fetch %s %s %s)" % (st, code, name, st, code, name))
            failed += 1
            continue
        entries = [e[f["list_field"]] for e in json.loads(listing.read_text(encoding="utf-8"))]
        pages, missing = [], []
        for e in entries:
            p = d / name / f["file"].format(entry=e)
            (pages.append((e, p.read_bytes())) if p.exists() else missing.append(e))
        if missing:
            print("%s %s %s: %d of %d pages not local (%s ...); fetch again" % (st, code, name, len(missing), len(entries), ", ".join(missing[:3])))
        ident = "%s/%s" % (f["doc"], f.get("title_id", "{item}").format(item=name))
        meta = local_meta(listing) or {}
        tree, report = build.convert_layers([("current", pages)], f, ident, "", titles.get(name, ""),
                                            {"edition": "current", "retrieved": meta.get("fetched"), "formula": "%s.%s" % (st, code)},
                                            title_num=name)
        report["missing_pages"] = missing
        out = state_dir(st) / "uslm" / code / ("%s.xml" % name)
        build.write(tree, report, out)
        print(verify.summary("%s %s %s (%s)" % (st, code, name, titles.get(name, "?")), report))
        print("  -> %s" % out)
        failed += not report["ok"] or bool(missing)
    return failed


def build_formula(st, code, items, everything=False):
    """Local source files -> USLM + <name>.verify.json beside it. Returns the number that failed."""
    from tribonian.uslm import build, verify
    f = formulas()[(st, code)]
    if f["fetch"] == "index":
        return build_index(st, code, f, items, everything)
    if f["fetch"] == "list":
        return build_list(st, code, f, items, everything)
    d = formula_dir(st, code, f)
    if everything:
        pat = f["file"].format(item="*")
        srcs = sorted(p for p in d.glob(pat)) if d.is_dir() else []
        names = lambda xs: {f["file"].format(item=item_name(f.get("item"), i)) for i in xs}
        if f.get("items"):
            srcs = [p for p in srcs if p.name in names(f["items"])]
        srcs = [p for p in srcs if p.name not in names(f.get("skip", []))]   # C.R.S. title 00 is the constitution
    else:
        items = items or f.get("items") or []
        srcs = [d / f["file"].format(item=item_name(f.get("item"), i)) for i in items]
    if not srcs:
        sys.exit("%s %s: nothing to build (fetch first, or name the items)" % (st, code))
    failed = 0
    for src in srcs:
        if not src.exists():
            print("%s: not local (tribonian state fetch %s %s ...)" % (src.name, st, code))
            failed += 1
            continue
        meta = local_meta(src) or {}
        m = re.fullmatch(re.escape(f["file"]).replace(re.escape("{item}"), "(.+)"), src.name)
        tree, report = build.convert(src.read_bytes(), f, m.group(1) if m and m.re.groups else src.stem,
                                     {"edition": meta.get("edition"), "retrieved": meta.get("fetched"), "url": meta.get("url"),
                                      "formula": "%s.%s" % (st, code)})
        out = uslm_path(st, code, src)
        build.write(tree, report, out)
        print(verify.summary("%s %s %s" % (st, code, src.stem), report))
        print("  -> %s" % out)
        failed += not report["ok"]
    return failed


def cmd_probe(args):
    """Save pages for working out a new state's formula: robots.txt verdict, the page, its links and the
    URLs its scripts mention (AJAX endpoints of a selector form). Files go to build/probe/<host>/."""
    out = tconfig.WORK / "build" / "probe"
    urls = list(args.urls)
    if args.file:
        urls += [l.split("#")[0].strip() for l in open(args.file, encoding="utf-8") if l.split("#")[0].strip()]
    for url in urls:
        u = urllib.parse.urlsplit(url)
        host = "%s://%s" % (u.scheme, u.netloc)
        try:
            ok = allowed(url)
        except Exception as e:
            print("%s\n  robots.txt: %s" % (url, e))
            continue
        rp = _robots[host]
        print("%s\n  robots.txt: %s" % (url, "allowed" if ok else getattr(rp, "reason", "disallowed")))
        if not ok:
            continue
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", (u.path + ("?" + u.query if u.query else "")).strip("/")) or "index"
        path = out / u.netloc / (name[:150] + ".html")
        try:
            n = download(url, path)
        except SystemExit as e:              # one bad URL must not stop a batch
            print("  failed: %s" % e.code)
            continue
        raw = path.read_bytes().decode("utf-8", "replace")
        links = sorted(set(re.findall(r"""(?:href|src)=["']([^"'#]+)""", raw, re.I)))   # pages, scripts, frames
        links += sorted(set(re.findall(r"""https?://[A-Za-z0-9.-]+(?:/[A-Za-z0-9_./?=&%+-]*)?""", raw)) - set(links))  # absolute
        #                                                  URLs anywhere (an app's API host inside its .js bundle)
        js = sorted(set(re.findall(r"""["'](/[A-Za-z0-9_/.-]*(?:api|Api|Get|get|Statute|statute|Law|law)[A-Za-z0-9_/.?=&-]*)["']""", raw)))
        (path.with_suffix(".links.txt")).write_text("\n".join(links + ["", "# script URLs"] + js) + "\n", encoding="utf-8")
        print("  saved %s (%.1f KB), %d links, %d script URLs -> %s" % (path, n / 1e3, len(links), len(js), path.with_suffix(".links.txt").name))
        pass  # pacing: pace() before each request


def cmd_status(args):
    """The tally from docs/state-tracker.toml: states by status, shared pieces, what is claimed, what can start."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    path = (tconfig.REPO or tconfig.WORK) / "docs" / "state-tracker.toml"
    if not path.exists():
        sys.exit("no %s" % path)
    with open(path, "rb") as fh:
        t = tomllib.load(fh)
    pieces = t.pop("pieces", {})
    order = ["working", "fits", "unclear", "bulk", "pdf", "vendor"]
    by = {}
    for st, v in sorted(t.items()):
        by.setdefault(v.get("status", "?"), []).append(st)
    print("%d jurisdictions" % len(t))
    for s in order + sorted(set(by) - set(order)):
        if s in by:
            print("  %-8s %2d  %s" % (s, len(by[s]), " ".join(by[s])))
    print("shared pieces")
    for k, p in pieces.items():
        print("  %-11s %-8s %s%s" % (k, p.get("status", "?"), p.get("what", ""), ("  [claimed: %s]" % p["claimed"]) if p.get("claimed") else ""))
    done = {k for k, p in pieces.items() if p.get("status") == "done"}
    claimed = ["%s (%s%s)" % (st, v["claimed"], ", " + v["branch"] if v.get("branch") else "") for st, v in sorted(t.items()) if v.get("claimed")]
    ready = [st for st, v in sorted(t.items()) if v.get("status") in ("fits", "unclear") and not v.get("claimed")
             and set(v.get("needs", [])) <= done]
    print("claimed       %s" % (", ".join(claimed) or "none"))
    print("ready to take %s" % (" ".join(ready) or "none"))


def cmd_build(args):
    key = (args.state.upper(), args.code.lower())
    if key not in formulas():
        sys.exit("no formula for %s %s in data/state_formulas.toml (have: %s)" % (key + (", ".join("%s %s" % k for k in formulas()),)))
    if build_formula(*key, args.items, args.all):
        sys.exit(1)


# lawtext.run_fetch looks readers up here: formula codes go through the generic downloader
try:
    for _st, _code in list(formulas()):
        FETCHERS.setdefault((_st, _code), lambda items, force=False, _k=(_st, _code): fetch_formula(*_k, items, force))
except ImportError:                    # Python 3.10 without tomli: the hand-written readers still work
    pass


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


NOTE_STATES = ("CO",)       # whose built law gets notes; the rest wait for the vault work (it would be every section)


def write_notes(quiet=False, states=NOTE_STATES):
    """Notes from the built USLM of each code with a [cite] table: a statute section each (co-crs-26.5-4-205), a
    constitution an article each (co-const-art-9), a rule each (co-ccr-8-1404-1); the U.S. Constitution an article
    or amendment each (us-const-amend-1). Names match lawcites.note_name, so cites in the case and docket notes
    link to them. Codes whose sections repeat across sub-codes (Texas's codes) are left for later."""
    import re
    from tribonian import case2md as c, lawcites, lawtext, statecites
    known = c.statute_notes()
    plans = []                                     # (path, cite, heading, fields, rows)
    for tb in statecites.tables():
        if tb.st not in states or tb.source_error or tb.subs:
            continue
        d = state_dir(tb.st) / "uslm" / tb.code
        for f in sorted(d.glob("*.xml")) if d.is_dir() else []:
            root = lawtext._uslm(f)
            props = {p.get("role"): (p.text or "").strip() for p in root.iter("property")}
            fields = [("source", props.get("url", "")), ("edition", c.Raw(props.get("edition", ""))),
                      ("retrieved", c.Raw(props.get("retrieved", "")))]
            secs = list(root.iter("section"))
            if tb.kind == "const":
                arts = list(dict.fromkeys(lawtext._article_of(x) for x in secs))
                for art in arts:
                    if not art:
                        continue
                    head, rows = lawtext.state(tb.key({"art": art}), "md") or ("", [])
                    body = []
                    for _, label, heading, _ in rows:
                        got = lawtext.state(tb.key({"art": art, "sec": label.replace("§ ", "")}), "md")
                        body += [(0, label, heading, "")] + (got[1] if got else [])
                    key = tb.key({"art": art})
                    plans.append((notes_dir() / tb.st.lower() / tb.code / (lawcites.note_name(key) + ".md"), lawcites.label(key),
                                  lawtext.article_heading(tb, art), fields, body))
            elif tb.kind == "regs":
                vals = tb.parse("%s:%s" % (tb.prefix, f.stem)) or {}
                key = tb.key(vals) if vals else None
                got = lawtext.state(key, "md") if key else None
                if got:
                    plans.append((notes_dir() / tb.st.lower() / tb.code / (lawcites.note_name(key) + ".md"), lawcites.label(key),
                                  re.sub(r"\s*\{.*$", "", got[0]), fields, got[1]))
            else:
                for x in secs:
                    key = tb.key({"sec": lawtext._num(x)})
                    got = lawtext.state(key, "md") if key else None
                    if got:
                        plans.append((notes_dir() / tb.st.lower() / tb.code / ("t" + f.stem.replace("title-", "")) / (lawcites.note_name(key) + ".md"),
                                      lawcites.label(key), got[0].rstrip("."), fields, got[1]))
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
    if key in formulas():
        fetch_formula(*key, args.items, args.force)
    elif key in FETCHERS:
        FETCHERS[key](args.items, args.force)
    else:
        have = sorted(set(formulas()) | set(FETCHERS))
        sys.exit("no reader for %s %s yet (have: %s)" % (key[0], key[1], ", ".join("%s %s" % k for k in have)))
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


class RemoteFile:
    """A file on a web server read by HTTP Range requests, seekable like a local file, so zipfile can list a huge archive
    and take single members without downloading the rest (California's 1.29 GB weekly database dump: we need only its
    LAW_* tables). Blocks of `block` bytes are fetched once each; robots.txt obeyed; a second between requests."""

    def __init__(self, url, block=1 << 20):
        if not allowed(url):
            sys.exit("robots.txt disallows it; not fetched: %s" % url)
        self.url, self.block, self.pos, self.cache, self.requests = url, block, 0, {}, 0
        req = urllib.request.Request(_env_fill(url), method="HEAD", headers={"User-Agent": UA})
        with cfr.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])
            if r.headers.get("Accept-Ranges", "").lower() != "bytes":
                print("  (server does not advertise Accept-Ranges: bytes; trying anyway)")

    def _get(self, i):
        if i not in self.cache:
            lo, hi = i * self.block, min(self.size, (i + 1) * self.block) - 1
            pace(self.url)
            req = urllib.request.Request(_env_fill(self.url), headers={"User-Agent": UA, "Range": "bytes=%d-%d" % (lo, hi)})
            with cfr.urlopen(req, timeout=120) as r:
                if r.status != 206:
                    sys.exit("%s: the server ignored the Range request (HTTP %d); a range fetch is not possible" % (self.url, r.status))
                self.cache[i] = r.read()
            self.requests += 1
            if len(self.cache) > 64:             # keep memory bounded
                self.cache.pop(next(iter(self.cache)))
            pass  # pacing: pace() before each request
        return self.cache[i]

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while n:
            i, o = divmod(self.pos, self.block)
            chunk = self._get(i)[o:o + n]
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def cmd_zip(args):
    """List a remote zip's members (name, size, compressed size) without downloading it, or with --get REGEX copy the
    matching members to --out (default build/probe/<host>/<zip name>/). Uses HTTP Range requests (RemoteFile)."""
    import zipfile
    rf = RemoteFile(args.url)
    z = zipfile.ZipFile(rf)
    infos = z.infolist()
    if not args.get:
        tot = sum(i.compress_size for i in infos)
        for i in infos:
            print("%12d %12d  %s" % (i.file_size, i.compress_size, i.filename))
        print("%d members, %.1f MB compressed of %.1f MB file; %d range requests" % (len(infos), tot / 1e6, rf.size / 1e6, rf.requests))
        return
    rx = re.compile(args.get)
    u = urllib.parse.urlsplit(args.url)
    out = pathlib.Path(args.out) if args.out else tconfig.WORK / "build" / "probe" / u.netloc / u.path.rsplit("/", 1)[-1]
    got = 0
    for i in infos:
        if rx.search(i.filename) and not i.is_dir():
            if args.max and got >= args.max:
                break
            dest = out / i.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(i))
            got += 1
            print("  %s (%d bytes)" % (dest, i.file_size))
    print("%d members copied; %d range requests" % (got, rf.requests))


def cmd_cites(args):
    """Check the [cite] tables: every `examples` pair (cite text -> key) must read back as that key, its item must be
    one the formula knows (catalog/items), and when the item is built here its text must be found. --fetch gets the
    items first. Exits 1 on any failure."""
    from tribonian import lawcites, lawtext, statecites
    want = {s.upper() for s in args.states}
    bad = 0
    tbs = [t for t in statecites.tables() if not want or t.st in want]
    if not tbs:
        sys.exit("no [cite] tables%s" % (" for " + " ".join(sorted(want)) if want else ""))
    for tb in tbs:
        exs = tb.c.get("examples") or []
        if not exs and not tb.source_error:
            print("%s.%s: no examples" % (tb.st, tb.code))
            bad += 1
        statecites.CONTEXT["states"] = [tb.st]          # bare forms are tried as if the case were from this state
        for ex in exs:
            text, key = ex[0], ex[1]
            got = lawcites.find(text)
            keys = [k for k in got["cites"]]
            ok = key in keys
            line = "%-4s %-8s %-48s -> %-34s" % (tb.st, tb.code, text[:48], key)
            if not ok:
                print(line + " FAIL read %s" % (keys or "nothing"))
                bad += 1
                continue
            if tb.source_error:
                print(line + " ok (source error: %s)" % tb.source_error[:40])
                continue
            spec = lawtext.fetch_spec(key)
            item = spec[3] if spec else None
            f = formulas().get((tb.st, tb.code), {})
            known = [str(x) for x in (f.get("catalog") or f.get("items") or [])]
            low = {k.lower() for k in known} | {item_name(f.get("item"), k).lower() for k in known}
            name_ok = item is not None and (not known or item.lower() in low or item_name(f.get("item"), item).lower() in low)
            if args.fetch and spec and lawtext.text_of(key) is None:
                okf, why = lawtext.run_fetch(spec)
                if not okf:
                    print("  fetch %s: %s" % (" ".join(spec[1:]), why))
            txt = lawtext.text_of(key)
            if not name_ok:
                print(line + " FAIL item %r not in the formula's catalog/items" % item)
                bad += 1
            elif txt is None:
                print(line + " ok, item %s (%s)" % (item, lawtext.fetch_hint(key)))
            else:
                print(line + " ok, item %s, text: %s" % (item, re.sub(r"\s*\{.*$", "", txt[0])[:40]))
            if len(ex) > 2 and lawcites.label(key) != ex[2]:
                print("     label %r, expected %r" % (lawcites.label(key), ex[2]))
                bad += 1
    statecites.CONTEXT["states"] = []
    print("%d problem%s" % (bad, "" if bad == 1 else "s"))
    if bad:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download a state's code, title by title, unless already local")
    f.add_argument("state", help="postal code, e.g. CO")
    f.add_argument("code", help="the formula's code: crs (Colorado Revised Statutes), const, ccr (Code of Colorado Regulations), ...")
    f.add_argument("items", nargs="*", help="crs: titles, e.g. 26.5 00; ccr: rules, e.g. 8-1404-1; US const: none")
    f.add_argument("--force", action="store_true", help="download again whatever is local")
    f.set_defaults(func=cmd_fetch)
    b = sub.add_parser("build", help="local source files -> USLM XML, each with a verify report (conservation, schema, numbering)")
    b.add_argument("state", help="postal code, e.g. CO")
    b.add_argument("code", help="a code with a formula in data/state_formulas.toml: CO crs, CO const, CT cgs")
    b.add_argument("items", nargs="*", help="titles, e.g. 26.5 (const: none)")
    b.add_argument("--all", action="store_true", help="every local file of that code")
    b.set_defaults(func=cmd_build)
    ss = sub.add_parser("status", help="the tally from docs/state-tracker.toml: by status, shared pieces, claimed, ready")
    ss.set_defaults(func=cmd_status)
    pr = sub.add_parser("probe", help="save a page (robots.txt obeyed) and its links, to work out a new state's formula")
    pr.add_argument("urls", nargs="*")
    pr.add_argument("--file", help="a file of URLs, one per line (# comments)")
    pr.set_defaults(func=cmd_probe)
    nt = sub.add_parser("notes", help="(re)write the Obsidian notes of everything stored here -> md-state/ (fetch does it too)")
    nt.set_defaults(func=cmd_notes)
    zp = sub.add_parser("zip", help="list a remote zip's members, or --get REGEX copy some, by HTTP Range requests")
    zp.add_argument("url")
    zp.add_argument("--get", help="regex on member names to copy")
    zp.add_argument("--out", help="folder (default build/probe/<host>/<zip name>/)")
    zp.add_argument("--max", type=int, default=0, help="copy at most this many matching members")
    zp.set_defaults(func=cmd_zip)
    ci = sub.add_parser("cites", help="check the [cite] tables against their examples (and the built text, when local)")
    ci.add_argument("states", nargs="*", help="postal codes (default: every table)")
    ci.add_argument("--fetch", action="store_true", help="fetch and build each example's item first (network)")
    ci.set_defaults(func=cmd_cites)
    l = sub.add_parser("list", help="what is stored here")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
