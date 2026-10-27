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

Codes with a formula in data/state_formulas.toml (C.R.S., the Colorado Constitution) are fetched by the
generic downloader and converted by a shared parser family (uslm/); the rest (8 CCR, the U.S. Constitution)
still have their own readers below.

C.R.S.: the Office of Legislative Legal Services' per-title HTML (olls.info/crs/crs<edition>-title-NN.htm),
the edition current through the latest regular session. Personal use; republishing needs a statement to
the Committee on Legal Services (C.R.S. 2-5-118). Files: <law_dir>/co/crs/title-26.5.htm + .json.
robots.txt is obeyed: a disallowed URL is reported, not fetched.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from datetime import date

from tribonian import cfr, tconfig

UA = cfr.UA
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
        rp = _robots["{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))]
        sys.exit("%s; not fetched: %s" % (getattr(rp, "reason", "robots.txt disallows it"), url))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with cfr.urlopen(req, timeout=120) as r:
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
    time.sleep(1)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with cfr.urlopen(req, timeout=60) as r:
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


FETCHERS = {("CO", "ccr"): fetch_co_ccr, ("US", "const"): fetch_us_const}


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
            for st, codes in raw.items():
                for code, f in codes.items():
                    if (st, code) in flat:
                        sys.exit("formula %s.%s is defined twice (second in %s)" % (st, code, path.name))
                    flat[(st, code)] = f
        _FORMULAS = {k: _inherit(flat, k, f) for k, f in flat.items()}
    return _FORMULAS


def _inherit(flat, key, f, depth=0):
    """inherit = "MD.code": start from that formula, override with this one's keys (tables merged one level;
    replace = ["containers", ...] names tables taken whole from this formula instead of merged)."""
    if "inherit" not in f or depth > 5:
        return f
    st, _, code = f["inherit"].partition(".")
    base = _inherit(flat, (st, code), flat[(st, code)], depth + 1)
    out = dict(base)
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
    time.sleep(1)
    return True


def url_fields(f, name):
    """Fields a template URL may use: {item} as named, {edition}, and for a numbered item {n} (its leading
    number, format it as {n:04d}), {id} (from the formula's ids table), {lo}/{hi} (its hundred-range: 768 -> 700, 799; Florida's
    0700-0799/0768/0768.html is "{lo:04d}-{hi:04d}/{n:04d}/{n:04d}.html") and {suffix} (4a -> a)."""
    m = re.match(r"0*(\d+)(.*)", name)
    n = int(m.group(1)) if m else 0
    return {"item": name, "edition": f.get("edition", ""), "n": n, "lo": n // 100 * 100, "hi": n // 100 * 100 + 99,
            "suffix": m.group(2) if m else "",
            "id": str(f.get("ids", {}).get(name, ""))}   # [ids] table: a chapter's label -> the site's id (New Mexico)


def page_name(f, href):
    """Local file name for a linked page: its last path part (Connecticut's chap_001.htm), or with
    save_as = "path" the whole link made file-safe, for sites whose pages share names across folders."""
    if f.get("save_as") == "path":
        return re.sub(r"[^A-Za-z0-9._-]+", "_", href).strip("._")   # "../title1/c001/index.html" must not be a dot file
    return href.rsplit("/", 1)[-1]


def walk_index(f, idx, url, ld, fetch=False, force=False, label=""):
    """The pages an index leads to, in reading order: [(file name, url)]. `links` is one regex (index -> pages)
    or a list, one per level (title -> chapters -> sections): every level but the last is a list page, kept in
    <layer>/_idx/. A list page with no links of the next level is itself a text page (mixed depth: Delaware's
    small chapters are text, big ones list subchapters) and is returned as "_idx/<name>". With fetch, missing
    list pages are downloaded on the way; without (build), the walk uses what is local and skips the rest."""
    levels = [re.compile(x) for x in (f["links"] if isinstance(f["links"], list) else [f["links"]])]
    enc = f.get("encoding", "utf-8")
    out = []

    def hrefs(path, i):
        seen = []
        for h in levels[i].findall(path.read_bytes().decode(enc, errors="replace")):
            h = h if isinstance(h, str) else h[0]
            if h not in seen:
                seen.append(h)
        return seen

    def visit(path, base, i):
        for href in hrefs(path, i):
            u = urllib.parse.urljoin(base, href)
            if i == len(levels) - 1:
                if f.get("rewrite"):      # rewrite = [regex, replacement]: a listed page -> the page to fetch
                    u = re.sub(f["rewrite"][0], f["rewrite"][1], u)     # (Vermont: chapter -> fullchapter)
                    sp = urllib.parse.urlsplit(u)       # the query names the page too (default.aspx?cite=1.04)
                    out.append((page_name(f, sp.path + ("?" + sp.query if sp.query else "")), u))
                else:
                    out.append((page_name(f, href), u))
                continue
            name = page_name(f, href)
            p = ld / "_idx" / name
            if fetch:
                _get_one(u, p, label, force, "  %s list %s" % (label, name))
            if not p.exists():
                continue
            if hrefs(p, i + 1):
                visit(p, u, i + 1)
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
    f = formulas()[(st, code)]
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
        elif f["fetch"] == "index":
            for layer in f["layers"]:
                url = layer["url"].format(item=name)
                ld = d / layer["name"]
                idx = ld / f["index_file"].format(item=name)
                if not _get_one(url, idx, layer["name"], force, "%s %s %s %s index" % (st, code, layer["name"], name),
                                missing_ok=layer.get("optional", False)):
                    continue
                leaves = walk_index(f, idx, url, ld, fetch=True, force=force, label=layer["name"])
                if not leaves:
                    print("  no pages matching %s under %s" % (f["links"], idx.name))
                bad = []
                for page, page_url in leaves:
                    try:
                        _get_one(page_url, ld / page, layer["name"], force, "  %s %s" % (layer["name"], page))
                    except SystemExit as e:              # one page missing must not stop the title; the
                        print("    %s" % e.code)          # build will report it and fail until it is fetched
                        bad.append(page)
                if bad:
                    sys.exit("%d of %d pages not fetched (%s ...)" % (len(bad), len(leaves), ", ".join(bad[:3])))
        else:
            sys.exit("%s %s: fetch strategy %r not written yet" % (st, code, f["fetch"]))


def uslm_path(st, code, src):
    return state_dir(st) / "uslm" / code / (src.stem + ".xml")


def _title_ident(f, name):
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
            url = L["url"].format(item=name)
            pages = [p for p, _ in walk_index(f, idx, url, d / L["name"])]
            missing = [p for p in pages if not (d / L["name"] / p).exists()]
            all_missing += ["%s/%s" % (L["name"], p) for p in missing]
            if missing:
                print("%s %s title %s: %s pages not local (%s ...); fetch again" % (st, code, name, L["name"], ", ".join(missing[:3])))
            layers.append((L["name"], [(p, (d / L["name"] / p).read_bytes()) for p in pages if p not in missing]))
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
        links = sorted(set(re.findall(r"""href=["']([^"'#]+)""", raw)))
        js = sorted(set(re.findall(r"""["'](/[A-Za-z0-9_/.-]*(?:api|Api|Get|get|Statute|statute|Law|law)[A-Za-z0-9_/.?=&-]*)["']""", raw)))
        (path.with_suffix(".links.txt")).write_text("\n".join(links + ["", "# script URLs"] + js) + "\n", encoding="utf-8")
        print("  saved %s (%.1f KB), %d links, %d script URLs -> %s" % (path, n / 1e3, len(links), len(js), path.with_suffix(".links.txt").name))
        time.sleep(1)


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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download a state's code, title by title, unless already local")
    f.add_argument("state", help="postal code, e.g. CO")
    f.add_argument("code", help="crs (Colorado Revised Statutes), const (Colorado Constitution), ccr (Code of Colorado Regulations)")
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
    l = sub.add_parser("list", help="what is stored here")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
