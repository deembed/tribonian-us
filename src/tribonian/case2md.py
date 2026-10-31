"""case2md.py -- court opinions (CourtListener) -> Obsidian markdown + LaTeX.

    tribonian case fetch --cite "463 U.S. 29"      # -> cases/<cite>_<name>.json
    tribonian case fetch --cluster 110991
    tribonian case build cases/463-u-s-29_*.json   # -> md-cases/, build/, pdf/
    tribonian case find glomar                     # search case names + courts' parentheticals
    tribonian case find "arbitrary and capricious" rulemaking   # quoted = a phrase; all terms must match
    tribonian case find glomar --get 3             # fetch result 3 from the local database and build its PDF
    tribonian case recap fetch --docket 4949363    # district and appeals court filings (RECAP) -> cases/recap/<court>/*.json
    tribonian case recap build cases/recap/laed/laed-09-cv-06732_*.json   # one PDF, a note per order
    tribonian case usage                           # API requests used (minute/hour/day) and history
The citation graph (stub notes, wikilinks, cited-by statistics) is casemap.py.

build reuses the LaTeX preamble (page size, boxes, running header/footer) and
the escaping table from render.py; everything opinion-specific lives here.
Text source: the Harvard Caselaw Access Project version of each opinion
(custom tags <author>, <page-number>, <footnotemark>, <footnote>), as served
by CourtListener in html_with_citations.

Token: environment variable COURTLISTENER_TOKEN, else the GNOME keyring via
    secret-tool lookup service courtlistener
"""
import argparse, atexit, json, os, re, shlex, shutil, sqlite3, subprocess, sys, tempfile, time, unicodedata
from contextlib import contextmanager
from collections import Counter
from datetime import date
from html.parser import HTMLParser
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

from tribonian import citemarks               # cite tints in the PDF and the closing "Cites not resolved" part
from tribonian import tconfig                  # settings: CL_* variable, else ~/.config/tribonian-us/config.toml, else default

API = "https://www.courtlistener.com/api/rest/v4"
# Everything is read and written under ROOT, the work folder (tconfig.WORK). TRIBONIAN_WORK overrides
# it, which is how the tests run without touching real output.
ROOT = tconfig.WORK
CASES = ROOT / "cases"
NOTES = ROOT / "md-cases"      # case notes; md/ belongs to the US Code build (tribonian sync-vault mirrors both)
# CourtListener API limits since May 2026, free tier: 5 a minute, 50 an hour,
# 125 a day. A member sets CL_LIMITS, e.g. CL_LIMITS=10,75,300 for the $10 tier.
LIMITS = [int(x) for x in str(tconfig.get("api_limits", "5,50,125")).split(",")]
WINDOWS = (60, 3600, 86400)
OFFLINE_NOTED = False   # the "offline mode: requests skipped" note is printed once per command
PAUSE = 0               # extra seconds between requests, on top of the limits
SIG_LINES = 8          # a signature block longer than this, or crossing a page, has ended
PDFLATEX_TIMEOUT = int(os.environ.get("PDFLATEX_TIMEOUT") or 1800)   # seconds per pass; a TeX loop must not hang a build worker


USED = 0               # API requests made by this process (reported at exit)


def _report_usage():
    if USED:
        print("CourtListener API: %d request%s by this command; %d of %d left today" % (
            USED, "" if USED == 1 else "s", budget_left(), LIMITS[-1]), file=sys.stderr)


atexit.register(_report_usage)


def throttle(url=""):
    """Wait until one more request fits in every window; if the DAY is used up,
    stop cleanly instead (everything is cached, so rerunning the same command
    later picks up where this stopped). Request times persist in cases/_api_log.json
    (last 24 h, for the limits); every request is also kept in cases/_api_history.tsv."""
    global USED
    CASES.mkdir(parents=True, exist_ok=True)
    logp = CASES / "_api_log.json"
    while True:
        with file_lock("_api.lock"):             # two processes must not both pass the check
            now = time.time()
            log = [t for t in (json.loads(logp.read_text()) if logp.exists() else []) if now - t < WINDOWS[-1]]
            waits = []
            for lim, win in zip(LIMITS, WINDOWS):
                recent = [t for t in log if now - t < win]
                if len(recent) >= lim:
                    waits.append((recent[-lim] + win - now + 1, win))
            if not waits:
                tmp = logp.with_suffix(".json.%d.tmp" % os.getpid())
                tmp.write_text(json.dumps(log + [now]))
                os.replace(tmp, logp)
        if not waits:
            USED += 1
            cmd = " ".join([Path(sys.argv[0]).name] + [a for a in sys.argv[1:] if not a.startswith("-")][:2])
            with open(CASES / "_api_history.tsv", "a", encoding="utf-8") as h:
                h.write("%s\t%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), cmd,
                                           urllib.parse.urlparse(url).path.replace("/api/rest/v4", "")))
            return
        wait, win = max(waits)
        if win == WINDOWS[-1]:
            sys.exit("Daily API limit reached (%d requests in 24 h). Rerun the same command after %s; "
                     "it resumes from the cache." % (LIMITS[-1], time.strftime("%a %H:%M", time.localtime(now + wait))))
        if wait > 90:
            print("  hourly API limit reached; sleeping %.0f min" % (wait / 60), file=sys.stderr)
        time.sleep(wait)


def budget_left():
    logp = CASES / "_api_log.json"
    log = json.loads(logp.read_text()) if logp.exists() else []
    return LIMITS[-1] - sum(1 for t in log if time.time() - t < WINDOWS[-1])


class Unsupported(Exception):
    """The case has text, but not in a format build can parse yet."""


def token():
    if not tconfig.online():
        return ""                                # offline: call() refuses before any request needs it
    t = tconfig.get("courtlistener_token")       # COURTLISTENER_TOKEN, or the config file
    if t:
        return str(t).strip()
    try:
        t = subprocess.run(["secret-tool", "lookup", "service", "courtlistener"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        t = ""
    if not t:
        sys.exit("No token: set COURTLISTENER_TOKEN, put courtlistener_token in %s, or store one with\n"
                 "  secret-tool store --label='CourtListener API' service courtlistener" % tconfig.config_path())
    return t


def call(url, tok, data=None, soft=False):
    """GET url (or POST when data is a dict); return parsed JSON.
    Paces itself, waits and retries when rate-limited. soft=True: a 404 returns None;
    soft=(403, 404): any of those codes does.
    Offline data mode (tribonian --mode offline, data_mode, the GUI's selector): no request is made; a soft call
    returns None (the caller goes on with what is local), any other stops the command with the reason."""
    global OFFLINE_NOTED
    if not tconfig.online():
        if soft:
            if not OFFLINE_NOTED:
                print("offline mode: CourtListener requests skipped; using what is local", file=sys.stderr)
                OFFLINE_NOTED = True
            return None
        sys.exit("Offline mode: this step needs a CourtListener request (%s). Run it with tribonian --mode full "
                 "(or lite / api), or pick another mode in the GUI's top bar." % urllib.parse.urlparse(url).path)
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(5):
        throttle(url)
        if PAUSE:
            time.sleep(PAUSE)
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Token {tok}",
            "Accept": "application/json",
            "User-Agent": "case2md/0.2 (personal legal formatter)",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if (e.code == 429 or e.code >= 500) and attempt < 4:
                try:                                   # Retry-After may be an HTTP-date, not seconds
                    delay = int(e.headers.get("Retry-After") or 0)
                except ValueError:
                    delay = 0
                delay = min(delay, 3600) or 30 * (attempt + 1)
                print(f"  HTTP {e.code}; waiting {delay}s then retrying", file=sys.stderr)
                time.sleep(delay)
                continue
            if soft and e.code in (soft if isinstance(soft, tuple) else (404,)):
                return None
            hint = {401: "token rejected", 403: "token rejected or not permitted",
                    404: "no such record", 429: "rate limited; wait and retry"}.get(e.code, "")
            sys.exit(f"HTTP {e.code} from {url}  {hint}\n{e.read(500).decode(errors='replace')}")
        except urllib.error.URLError as e:
            if attempt < 4:
                time.sleep(10)
                continue
            sys.exit(f"Network error reaching {url}: {e.reason}")


def cite_str(c):
    return f"{c.get('volume')} {c.get('reporter')} {c.get('page')}"


def best_cite(cluster):
    cites = cluster.get("citations") or []
    for c in cites:                       # prefer the official U.S. Reports cite
        if c.get("reporter") == "U.S.":
            return cite_str(c)
    return cite_str(cites[0]) if cites else f"cluster {cluster.get('id')}"


_SLUGS, _SLUG_DB = {}, []


def opinion_url(cid, name=None):
    """CourtListener's page for a cluster. The site needs the slug: /opinion/<id>/<slug>/ -- a bare
    /opinion/<id>/ does not resolve. The slug comes from the local database, else from the name."""
    cid = int(cid)
    if cid not in _SLUGS:
        if not _SLUG_DB:
            _SLUG_DB.append(local_db())
        con, slug = _SLUG_DB[0], None
        if con is not None:
            try:
                r = con.execute("SELECT slug, case_name FROM clusters WHERE id=?", (cid,)).fetchone()
                slug = r and (r[0] or (r[1] and slugify(r[1], 75)))
            except sqlite3.Error:
                pass
        _SLUGS[cid] = slug or (slugify(name, 75) if name else None) or "case"
    return "%s/opinion/%d/%s/" % (CL, cid, _SLUGS[cid])


# A first party that is a government or a place: the short form is then the other party (Bluebook rule 10.9).
_GOV_PARTY = re.compile(
    r"(?i)^(the )?(united states|u\. ?s\.|usa|america|state|states|people|commonwealth|territory|government|republic|"
    r"district of columbia|puerto rico|guam|(city|county|town|village|borough|parish|township) of\b|"
    r"in re\b|ex parte\b|matter of\b|in the matter\b|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    r"illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming)\b")


_GOV_BODY = re.compile(r"\b(Service|Department|Dep't|Commission|Board|Bureau|Agency|Administration|Authority|"
                       r"Secretary|Commissioner|Attorney General|Director|Office|Federal|National Labor|INS|NLRB|SEC|FCC|EPA)\b")


def case_short(short, full, limit=60):
    """The name to show for a case. CourtListener's case_name_short sometimes names the second party when the
    first is a private one ('Duran-Morales v. Blanche' -> 'Blanche', the Attorney General, a party to
    thousands of cases); the short form is the first party unless that one is a government or a place."""
    name = short or full or ""
    if short and full and " v. " in full:
        first, second = full.split(" v. ", 1)
        low = short.lower()
        if (low in second.lower() and low not in first.lower() and not _GOV_PARTY.match(first.strip())
                and not _GOV_BODY.search(first)):
            name = re.split(r",\s", first.strip(), 1)[0]
    if not short and full and len(full) > limit:
        name = full[:limit - 3] + "..."
    return name


def slugify(s, limit=60):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:limit].rstrip("-")


def lookup(cite, tok):
    """Citation -> cluster dict, via the citation-lookup endpoint."""
    hits = call(f"{API}/citation-lookup/", tok, data={"text": cite})
    clusters = [cl for h in hits for cl in (h.get("clusters") or [])]
    if not clusters:
        status = ", ".join(str(h.get("status")) for h in hits) or "no citation recognised"
        sys.exit(f"Nothing found for '{cite}' (lookup status: {status})")
    if len(clusters) > 1:
        print(f"'{cite}' matches {len(clusters)} clusters; rerun with --cluster ID:")
        for cl in clusters:
            print(f"  {cl.get('id'):>9}  {cl.get('case_name')}  ({cl.get('date_filed')})")
        sys.exit(1)
    return clusters[0]


def fetch_cluster(cluster, tok):
    """Download a cluster's opinions and docket; write cases/<cite>_<name>.json; return the path."""
    opinions = [call(u, tok) for u in cluster.get("sub_opinions") or []]
    docket = call(cluster["docket"], tok) if cluster.get("docket") else None
    cite = best_cite(cluster)
    name = cluster.get("case_name") or cluster.get("case_name_full") or "case"
    CASES.mkdir(parents=True, exist_ok=True)
    out = CASES / f"{slugify(cite)}_{slugify(name)}.json"
    out.write_text(json.dumps({"cluster": cluster, "docket": docket, "opinions": opinions},
                              indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def fetch(args):
    tok = token()
    cluster = (call(f"{API}/clusters/{args.cluster}/", tok) if args.cluster
               else lookup(args.cite, tok))
    out = fetch_cluster(cluster, tok)
    data = json.loads(out.read_text(encoding="utf-8"))
    index = load_index()
    register(data, index, out)
    save_index(index)

    print(f"{cluster.get('case_name')}, {best_cite(cluster)} ({cluster.get('date_filed')})  cluster {cluster.get('id')}")
    fields = ("html_with_citations", "xml_harvard", "html_lawbox", "html_columbia",
              "html", "plain_text")
    for o in data["opinions"]:
        have = ", ".join(f"{f}={len(o.get(f) or '')}" for f in fields if o.get(f))
        print(f"  opinion {o.get('id')}  type={o.get('type')}  "
              f"author={o.get('author_str') or o.get('author_id')}  [{have}]")
    print(f"saved cases/{out.name}")


# ---------------------------------------------------------------- index of known cases
# cases/_index.json: cluster id -> {slug, name, short, cite, year, court, full}
# "slug" is the note's filename, so it is what a [[wikilink]] points at; "full"
# is the fetched JSON (relative to cases/) when the opinion text is on disk,
# else the case is known only as a stub (cases/stubs/<id>.json: cluster + docket).

_INDEX_SNAP = {}        # id(index) -> deep copy of what load_index() returned, for save_index's merge


def _read_index():
    p = CASES / "_index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def load_index():
    index = _read_index()
    _INDEX_SNAP[id(index)] = json.loads(json.dumps(index))
    return index


def save_index(index):
    """Merge, then write atomically, under the lock. Callers may hold an index for
    minutes (casemap map, stubs) while parallel builds register other cases, so
    the on-disk file is re-read and only the entries THIS process changed since
    its load_index() are written over it; everything else keeps the disk value.
    Atomic (temp file, rename over): a parallel build never reads half a file."""
    CASES.mkdir(parents=True, exist_ok=True)
    snap = _INDEX_SNAP.get(id(index), {})
    with index_lock():
        disk = _read_index()
        for k, v in index.items():
            if k not in disk or v != snap.get(k):
                disk[k] = v
        tmp = CASES / ("_index.json.%d.tmp" % os.getpid())
        tmp.write_text(json.dumps(disk, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CASES / "_index.json")
    index.clear(); index.update(disk)                      # caller sees the merged result
    _INDEX_SNAP[id(index)] = json.loads(json.dumps(disk))


_LOCK_DEPTH = {}        # lock path -> nesting depth; flock is per open file, so re-entry must not reopen


@contextmanager
def file_lock(name):
    """Exclusive, re-entrant within a process. Hold while reading, changing and saving
    a shared file under cases/, so parallel builds (tribonian build) don't overwrite each
    other's entries."""
    CASES.mkdir(parents=True, exist_ok=True)
    path = CASES / name
    if _LOCK_DEPTH.get(path, 0):
        _LOCK_DEPTH[path] += 1
        try:
            yield
        finally:
            _LOCK_DEPTH[path] -= 1
        return
    with open(path, "w") as f:
        _flock(f)
        _LOCK_DEPTH[path] = 1
        try:
            yield
        finally:
            _LOCK_DEPTH[path] = 0
            _funlock(f)


def _flock(f):
    """Wait until this process holds an exclusive lock on f: flock on Linux and macOS, msvcrt on Windows
    (a lock on the file's first byte; msvcrt.LK_LOCK gives up after about 10 s, so keep asking)."""
    if os.name == "nt":
        import msvcrt
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue
    import fcntl
    fcntl.flock(f, fcntl.LOCK_EX)


def _funlock(f):
    """Release _flock's lock (closing the file releases it too; Windows says when only loosely, so do it now)."""
    if os.name == "nt":
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_UN)


def index_lock():
    return file_lock("_index.lock")


def register(data, index, full_path=None):
    """Record a case (full or stub) in the index; returns its entry."""
    meta = case_meta(data)
    cid = str(meta["cluster"])
    ent = index.get(cid, {})
    slug = ent.get("slug") or meta["slug"]
    if not ent and any(e["slug"] == slug for k, e in index.items() if k != cid):
        slug = "%s-%s" % (slug, cid)             # two records of one case: keep filenames distinct
    ent.update({"slug": slug, "name": meta["name"], "short": meta["short"], "cite": meta["cite"],
                "year": meta["year"], "court": meta["court"],
                "cited_by": data["cluster"].get("citation_count")})
    if full_path is not None:
        ent["full"] = Path(full_path).name
    index[cid] = ent
    return ent


def load_case(cid, index):
    """-> (data, is_full) for a cluster id already in the index."""
    ent = index[str(cid)]
    if ent.get("full") and (CASES / ent["full"]).exists():
        return json.loads((CASES / ent["full"]).read_text(encoding="utf-8")), True
    return json.loads((CASES / "stubs" / ("%s.json" % cid)).read_text(encoding="utf-8")), False


def usable_opinions(data):
    """The Harvard-format opinions when there are any (the 'combined' copy would
    double everything), else whatever there is."""
    ops = data.get("opinions") or []
    hv = [o for o in ops if "<opinion" in (o.get("html_with_citations") or o.get("xml_harvard") or "")]
    return hv or ops


def cited_clusters(data):
    """{cluster id: times cited} from the citation links CourtListener put in the text."""
    me = data["cluster"].get("id")
    if data.get("cites") is not None:            # from the local citation map (casemap.py): known even for a stub
        return {int(k): v for k, v in data["cites"].items() if int(k) != me}
    out = {}
    for o in usable_opinions(data):
        for m in re.finditer(r'href="/opinion/(\d+)/', o.get("html_with_citations") or ""):
            cid = int(m.group(1))
            if cid != me:
                out[cid] = out.get(cid, 0) + 1
    return out


# ================================================================ build
# ---------------------------------------------------------------- html -> tree

class El:
    def __init__(self, tag, attrs=None):
        self.tag, self.attrs, self.kids = tag, dict(attrs or {}), []

    def text(self):
        return "".join(k if isinstance(k, str) else k.text() for k in self.kids)

    def find(self, tag):
        for k in self.kids:
            if isinstance(k, El):
                if k.tag == tag:
                    return k
                r = k.find(tag)
                if r is not None:
                    return r
        return None


class Tree(HTMLParser):
    VOID = {"br", "hr", "img"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        e = El(tag, attrs)
        self.stack[-1].kids.append(e)
        if tag not in self.VOID:
            self.stack.append(e)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def parse_html(s):
    from tribonian.render import FOLD                  # ligatures and odd spaces -> plain text, once, at parse time
    t = Tree()
    t.feed(s.translate(FOLD))
    t.close()
    return t.root


# ---------------------------------------------------------------- inline text

CL = "https://www.courtlistener.com"
FN_NAMES = {"*": "star", "†": "dagger", "‡": "ddagger", "§": "sect", "**": "star2"}
MD_ESC = {"*": r"\*", "_": r"\_", "$": r"\$", "<": r"\<", "`": r"\`", "|": r"\|"}   # $ = Obsidian math; | would end a [[slug|label]]


def esc(s, mode):
    if mode == "tex":
        from tribonian.render import tex_chars
        return tex_chars(s).replace("•", r"\textbullet{}")
    return "".join(MD_ESC.get(c, c) for c in s)


_anchors = {}                                    # pdfbookmark names used so far in this PDF (render_tex resets it)


def anchor(s):
    """A hyperref destination for s: % # & § and spaces break pdflatex, so [A-Za-z0-9-] only,
    and a repeat (two parts with the same path) gets --2, --3, ... since duplicates only warn."""
    a = re.sub(r"[^A-Za-z0-9-]+", "-", s).strip("-") or "x"
    _anchors[a] = _anchors.get(a, 0) + 1
    return a if _anchors[a] == 1 else "%s--%d" % (a, _anchors[a])


class Raw(str):
    """A front-matter value written as is (dates, URLs, numbers-as-text, enums); other str values are quoted."""


def yaml_head(fields):
    r"""[(key, value)] -> front-matter lines, between the --- markers. str is a double-quoted YAML string
    (\ and " escaped), Raw is written bare, list -> block list of quoted strings, bool/int -> YAML scalars,
    None -> the key is left out. The sites pass fields in the order their notes already have, so nothing churns."""
    q = lambda v: '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')
    L = ["---"]
    for k, v in fields:
        if v is None:
            continue
        if isinstance(v, list):
            L += ["%s:" % k] + ["  - %s" % q(x) for x in v]
        elif isinstance(v, bool):
            L.append("%s: %s" % (k, "true" if v else "false"))
        elif isinstance(v, (Raw, int)):
            L.append("%s: %s" % (k, v))
        else:
            L.append("%s: %s" % (k, q(v)))
    return L + ["---"]


def wrap(inner, left, right):
    """left+inner+right with inner's edge whitespace kept outside the markers."""
    core = inner.strip()
    if not core:
        return inner
    a = inner[:len(inner) - len(inner.lstrip())]
    b = inner[len(inner.rstrip()):]
    return a + left + core + right + b


class Ctx:
    """Rendering state for one opinion: where we are, for the running header."""
    def __init__(self, reporter, who, oi):
        self.reporter, self.who, self.oi = reporter, who, oi   # "463 U.S.", "White, J.", 0
        self.page, self.part, self.record = "", "", True
        self.links = {}                          # cluster id -> note filename, for [[wikilinks]]
        self.statutes = set()                    # US Code note names that exist (md/**/5-usc-552a.md)
        self.lawtargets = {}                     # provision (lawcites section key) -> PDF anchor, where the build prints its text
        self.year = 0                            # decision year: what a US Code cite is judged against (usc_out)

    def path(self):
        bits = ["%s %s" % (self.reporter, self.page), self.who] + ([self.part] if self.part else [])
        return esc(" -- ".join(b for b in bits if b.strip()), "tex")

    def fnkey(self, label):
        key = FN_NAMES.get(label, re.sub(r"[^A-Za-z0-9]", "", label) or "x")
        return key if self.oi == 0 else "%d-%s" % (self.oi + 1, key)


def inline(node, mode, ctx, marks):
    mark_usc(node)
    if mode != "md":
        mark_cases(node)
        mark_quotes(node, ctx)
    out = []
    for k in node.kids:
        if isinstance(k, str):
            out.append(esc(k, mode))
            continue
        cls = k.attrs.get("class", "")
        if k.tag == "page-number" or (k.tag == "span" and "star-pagination" in cls):
            pg = k.attrs.get("label") or k.text().strip().lstrip("*")
            if mode == "md":
                out.append(" `*%s` " % pg)
            else:
                ctx.page = pg
                rec = "\\recordpath{%s}" % ctx.path() if ctx.record else ""
                if re.fullmatch(r"\d+a", pg) and getattr(ctx, "oi", None) is not None:   # an appendix page: a link target
                    rec += "\\hypertarget{app-%s-%s}{}" % (ctx.oi, pg)
                out.append(" %s{\\scriptsize\\bfseries[*%s]} " % (rec, pg))
        elif k.tag == "footnotemark":
            label = k.text().strip()
            marks.append(label)
            out.append("[^%s]" % ctx.fnkey(label) if mode == "md"
                       else "\\textsuperscript{%s}" % esc(label, "tex"))
        elif k.tag in ("em", "i"):
            out.append(wrap(inline(k, mode, ctx, marks), *(("*", "*") if mode == "md" else ("\\emph{", "}"))))
        elif k.tag in ("b", "strong"):
            out.append(wrap(inline(k, mode, ctx, marks), *(("**", "**") if mode == "md" else ("\\textbf{", "}"))))
        elif k.tag == "a" and mode != "md" and re.match(r"/opinion/\d+/", k.attrs.get("href", "")):
            out.append(citemarks.mark("ok", inline(k, mode, ctx, marks)))    # CourtListener linked it: the case is there
        elif k.tag == "quoted":                              # wording later opinions reuse (mark_quotes)
            out.append(quote_tex(inline(k, mode, ctx, marks), k.attrs, ctx))
        elif k.tag == "case" and mode != "md":            # a reporter cite found in the text (mark_cases)
            inner = inline(k, mode, ctx, marks)
            out.append(citemarks.mark("ok", inner) if k.attrs.get("ok") else
                       citemarks.mark(k.attrs.get("tint") or "red", inner, k.text(),
                                      k.attrs.get("why") or "no case found for this citation in the local database"))
        elif (k.tag == "span" and mode != "md" and "no-link" in cls and "citation" in cls
              and not any(k.find(t) is not None for t in ("law", "usc", "a"))):
            inner = inline(k, mode, ctx, marks)          # CourtListener could not link it: mark_cases looked again
            if k.attrs.get("cm_ok"):
                out.append(citemarks.mark("ok", inner))
            elif k.attrs.get("cm_done"):
                out.append(citemarks.mark(k.attrs.get("cm_tint") or "red", inner, k.text(),
                                          k.attrs.get("cm_why") or "no case found for this citation in the local database"))
            elif k.attrs.get("cm_plain") or not unlinked_is_case(k.text()):
                out.append(inner)                        # a statute, the Federal Register, a law review: not a case
            else:
                out.append(citemarks.mark("red", inner, k.text(), "CourtListener found no case for this citation"))
        elif k.tag == "a" and mode == "md" and k.attrs.get("href", "").startswith("/"):
            m = re.match(r"/opinion/(\d+)/", k.attrs["href"])
            if m and int(m.group(1)) in ctx.links:       # a case we have a note for
                out.append(wrap(inline(k, mode, ctx, marks), "[[%s|" % ctx.links[int(m.group(1))], "]]"))
            else:
                out.append(wrap(inline(k, mode, ctx, marks), "[", "](%s%s)" % (CL, k.attrs["href"])))
        elif k.tag == "span" and mode == "md" and "citation" in cls and statute_note(k.text()) in ctx.statutes:
            out.append(wrap(inline(k, mode, ctx, marks), "[[%s|" % statute_note(k.text()), "]]"))
        elif k.tag == "usc":
            out.append(usc_out(k, mode, ctx))
        elif k.tag == "law":                             # a statute or constitutional cite (lawcites.py)
            from tribonian import lawcites
            key, inner = k.attrs.get("key", ""), inline(k, mode, ctx, marks)
            note, target = lawcites.note_name(key), ctx.lawtargets.get(lawcites.section_of(key))
            if mode == "md" and note in ctx.statutes and not key.startswith("usc:"):   # U.S.C.: mark_usc's <usc> inside links it
                out.append(wrap(inner, "[[%s|" % note, "]]"))
            elif mode != "md":
                st, why = citemarks.law_status(key)
                tinted = citemarks.mark(st, inner, k.text(), why)
                out.append(wrap(tinted, "\\hyperlink{%s}{" % target, "}") if target else tinted)
            else:
                out.append(inner)
        elif k.tag == "leader":                          # toc entry: dots out to the page(s)
            pages = esc(k.attrs.get("pages", ""), mode)
            out.append(" … %s" % pages if mode == "md" else
                       "\\leaders\\hbox to .6em{\\hss.\\hss}\\hskip 1.5em plus 1fill\\nobreak\\mbox{\\,%s}" % pages)
        elif k.tag == "br":
            out.append(" ")
        else:
            out.append(inline(k, mode, ctx, marks))
    return "".join(out)


# ---------------------------------------------------------------- quote marks: where later opinions reuse the wording
# The sentences 2+ later parentheticals restate (quotelineage.py) are hatched in the case's PDF like a highlighter,
# rounded where the sentence starts and ends, darker with more, with "×N" at the end linking to the sentence's entry in
# Quote lineage. Cites inside the sentence keep their own tint (ulem marks do not nest), so a sentence can be marked in
# pieces. QUOTE_MARKS=0 turns it off; QUOTED_MIN sets the threshold.
QUOTE_MARKS = os.environ.get("QUOTE_MARKS", "1") != "0"
QUOTED_MIN = int(os.environ.get("QUOTED_MIN", "2"))
NOQUOTE = ("a", "case", "law", "usc", "quoted")          # never split: cites keep their tint


def _compact(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def mark_quotes(node, ctx):
    """Wrap the reused sentences in node's text in <quoted n first last> elements (block by block: once per node,
    and its descendants are left alone after)."""
    quotes = getattr(ctx, "quotes", None)
    if not quotes or getattr(node, "quotes_done", False) or node.tag in NOQUOTE + ("page-number", "footnotemark"):
        return
    leaves = []                                          # [parent, index, text, markable]

    def walk(n, ok):
        n.quotes_done = True
        for i, k in enumerate(n.kids):
            if isinstance(k, str):
                leaves.append([n, i, k, ok])
            elif k.tag not in ("page-number", "footnotemark"):
                walk(k, ok and k.tag not in NOQUOTE and not (k.tag == "span" and "citation" in k.attrs.get("class", "")))
    walk(node, True)
    full = "".join(l[2] for l in leaves)
    comp, at = [], []                                    # compact text and each compact char's place in full
    for i, ch in enumerate(full.lower()):
        if ch.isalnum() and ch.isascii():
            comp.append(ch)
            at.append(i)
    comp = "".join(comp)
    spans = []
    for n, sent, qk in quotes:
        head, tail = sent[:40], sent[-40:]
        a = comp.find(head)
        if a < 0:
            continue
        z = comp.find(tail, a + len(head) - 40)
        if z < 0 or z + len(tail) - a > len(sent) * 1.3 + 40:
            continue
        lo, hi = at[a], at[z + len(tail) - 1] + 1
        if not any(lo < h and l < hi for l, h, _, _ in spans):
            spans.append((lo, hi, n, qk))
    if not spans:
        return
    spans.sort()
    pieces, pos = {}, 0                                  # (parent id, index) -> new kids; span -> its elements
    made = {}
    for parent, i, text, ok in leaves:
        lo_leaf, hi_leaf = pos, pos + len(text)
        pos = hi_leaf
        if not ok:
            continue
        cuts = [(max(l, lo_leaf), min(h, hi_leaf), j) for j, (l, h, _, _) in enumerate(spans) if l < hi_leaf and lo_leaf < h]
        if not cuts:
            continue
        new, cur = [], lo_leaf
        for l, h, j in cuts:
            if l > cur:
                new.append(text[cur - lo_leaf:l - lo_leaf])
            e = El("quoted", {"n": str(spans[j][2]), "k": str(spans[j][3])})
            e.kids.append(text[l - lo_leaf:h - lo_leaf])
            e.quotes_done = True
            new.append(e)
            made.setdefault(j, []).append(e)
            cur = h
        if cur < hi_leaf:
            new.append(text[cur - lo_leaf:])
        pieces[(id(parent), i)] = (parent, new)
    for j, els in made.items():                          # caps where the marked text starts and ends
        els = [e for e in els if e.kids[0].strip()]
        if els:
            els[0].attrs["first"] = "1"
            els[-1].attrs["last"] = "1"
    for (_, i), (parent, new) in sorted(pieces.items(), key=lambda kv: -kv[0][1]):
        parent.kids[i:i + 1] = [k for k in new if k != ""]


def quote_tex(inner, attrs, ctx=None):
    """A marked piece: hatched, darker with more reuse; a rounded cap where the sentence starts (with the link target
    Quote lineage points back to) and ends (with ×N, linking to the sentence's entry there)."""
    from tribonian import citemarks
    if not inner.strip() or any(u in inner for u in citemarks.UNSAFE):
        return inner
    n = int(attrs.get("n") or 0)
    lvl = "A" if n < 10 else "B" if n < 50 else "C"
    lead, body, trail = re.match(r"(\s*)(.*?)(\s*)$", inner, re.S).groups()
    out = "\\quote%s{%s}" % (lvl, body)
    k = attrs.get("k") or "0"
    if attrs.get("first"):
        seen = getattr(ctx, "quote_seen", None)
        if seen is None and ctx is not None:
            seen = ctx.quote_seen = set()
        target = "" if seen is None or k in seen else "\\hypertarget{qs-%s}{}\\label{qs-%s}" % (k, k)
        if seen is not None:
            seen.add(k)
        out = target + "\\usebox\\ql%s\\kern-.3pt " % lvl + out
    if attrs.get("last"):
        out += "\\kern-.3pt\\quotecount{\\qr%s}{%d}{%s}" % (lvl, n, k)
    return lead + out + trail


def resolve_case_cite(con, c, before, after, doc):
    """One case cite (citefind.Cite) -> the attributes of its <case> mark: {"ok": "1"}, {"why": reason[, "tint": ...]},
    {} (red, no reason found), or None (leave it unmarked: an Id. or pin cite whose case is unknown). before/after: the
    text on either side, past page breaks. doc: the document's antecedents (Id., supra, pin), updated here."""
    from tribonian import scotus
    if c.kind == "id":                           # "Id., at 843": the cite just before it, when that one was found
        return {"ok": "1"} if doc.id() else None
    if c.kind == "supra":                        # "Smith, supra, at 890": the one earlier full cite of that name
        cid = doc.supra(c.name)
        doc.last = (cid, "")
        return {"ok": "1"} if cid else None
    if c.kind == "pin":                          # "494 U.S. at 890": marked only when the name finds the case
        rep = next((r for r in c.reps if scotus.resolve_pin(con, c.vol, r, c.page, before)), c.rep)
        cid = scotus.resolve_pin(con, c.vol, rep, c.page, before) or scotus.found_again(c.vol, c.rep, c.page, pin=True)
        start = None
        if not cid and doc.in_volume(c.vol, c.rep):     # "Smith, 494 U.S. at 850": the case cited in full here
            cid, start = doc.in_volume(c.vol, c.rep)
        doc.last = (cid, "")
        if not cid:
            return None
        why = scotus.pin_outside(con, cid, c.vol, rep, c.page, start=start)
        if why:                                  # the page is not inside that case: a likely miscite
            doc.last = None
            return {"why": why}
        return {"ok": "1"}
    vol, rep, page, why = c.vol, c.rep, c.page, ""
    own = getattr(doc, "own", ())
    if doc.own_id and any("%s %s %s" % (vol, r, page) in own for r in c.reps):   # the case citing itself
        doc.cite(doc.own_id, "", before, (vol, rep, page))
        return {"ok": "1"}
    rep = next((r for r in c.reps if scotus.resolve(con, vol, r, page, before)), rep)
    cid = scotus.resolve(con, vol, rep, page, before)
    if cid:                                      # found by its citation: does the text agree?
        pin = re.match(r",\s*(\d{1,5})(?:[-–]\d+)?(?!\d)(?!\s+[A-Z])", after)   # "494 U.S. 872, 890", not ", 110 S. Ct."
        why = scotus.name_mismatch(con, cid, vol, rep, page, before)
        if why:                                  # the name written is another case: say where that one is
            why += scotus.named_instead(con, vol, rep, page, before, after, cid)
        elif pin and page.isdigit():
            why = scotus.pin_outside(con, cid, vol, rep, pin.group(1), start=page)
        if why:
            cid = None
    if not cid and not why:
        cid = scotus.found_again(vol, rep, page)
    if not cid and not why:                      # no such citation in the data: try the name, court and date
        cid, why = scotus.resolve_name(con, vol, rep, page, before, after)
        if not cid and not why:                  # a RECAP docket by its number (Westlaw cites of district orders)
            d = scotus.resolve_docket(con, before, after)
            cid = ("docket", d[0]) if d else None
        if not cid and not why:                  # "the Tenth Circuit's opinion is reported at …": the case below
            cid = scotus.below_for(before)
        if cid:
            scotus.FOUND[(vol, rep, page)] = cid
    doc.cite(cid, why, before, (vol, rep, page))
    if cid:
        return {"ok": "1"}
    if why:
        return {"why": why}
    return {"why": "Westlaw only: CourtListener has no copy, by citation, name or docket number", "tint": "orange"} \
        if c.rep == "WL" else {}


CL_LINK_RE = re.compile(r"/opinion/(\d+)/")


def unsplit(node):
    """A cite cut by italics: "<em>Olmstead v. Zimring, 521</em> U.S. 581" -> the volume moves out of the italics, so
    "521 U.S. 581" is one piece of text the finder can read. The case name stays italic."""
    from tribonian import citefind
    for i, k in enumerate(node.kids[:-1]):
        nxt = node.kids[i + 1]
        if isinstance(k, El) and k.tag in ("em", "i") and k.kids and isinstance(k.kids[-1], str) and isinstance(nxt, str):
            m = re.search(r"(?<![\w.])(\d{1,4})\s*$", k.kids[-1])
            if m and citefind.CITE_RE.match(m.group(1) + " " + nxt.lstrip()):
                k.kids[-1] = k.kids[-1][:m.start()].rstrip() + " "
                node.kids[i + 1] = m.group(1) + " " + nxt.lstrip()


def unlinked_is_case(text):
    """Does an unlinked CourtListener citation span hold a case cite (not "90 Stat. 331" or "54 Mich. L. Rev. 1")?"""
    from tribonian import citefind
    return any(c.kind in ("full", "pin") for c in citefind.find(text))


def mark_cases(node):
    """Case cites in node's own text ("500 U.S. 1", "494 U.S. at 890", "Id.", "Smith, supra") -> <case ok=...>
    elements, resolved through the local database (resolve_case_cite), so the PDF can tint them. In HTML opinions,
    CourtListener's citation spans are read in the same pass: a linked one is the case for an Id. after it; an
    unlinked one is resolved like a cite in a brief (an Id., a case CourtListener could not link, or no case at all:
    a statute, the Federal Register, a law review, left untinted). Once per element; nothing without the database."""
    if getattr(node, "cases_done", False) or not citemarks.CITE_MARKS:
        return
    node.cases_done = True
    if node.tag in ("a", "law", "usc", "case", "page-number", "footnotemark", "leader") or \
            (node.tag == "span" and "citation" in node.attrs.get("class", "")):
        return
    con = shared_db()
    if con is None or not any(isinstance(k, str) or (k.tag == "span" and "citation" in k.attrs.get("class", ""))
                              for k in node.kids):
        return
    from tribonian import citefind
    doc = citefind.DOC                               # the cites before this point in the document (Id., supra)
    unsplit(node)
    out = []
    def flat(n):                                     # text without page markers or footnote marks
        return n if isinstance(n, str) else "" if n.tag in ("page-number", "footnotemark") else "".join(flat(x) for x in n.kids)
    for i, k in enumerate(node.kids):
        # the text on either side runs on past a page break ("(N.D. Cal. [*15] Mar. 23, 2009)")
        prev = "".join(flat(x) for x in node.kids[:i])[-400:]
        nxt = "".join(flat(x) for x in node.kids[i + 1:])[:160]
        if not isinstance(k, str):
            cls = k.attrs.get("class", "")
            if k.tag in ("law", "usc"):                  # a statute between: an Id. after it is the statute's
                doc.law()
            elif k.tag == "a" and CL_LINK_RE.match(k.attrs.get("href", "")):
                doc.cite(int(CL_LINK_RE.match(k.attrs["href"]).group(1)))
            elif k.tag == "span" and "citation" in cls:
                a = k.find("a")
                if a is not None and CL_LINK_RE.match(a.attrs.get("href", "")):
                    doc.cite(int(CL_LINK_RE.match(a.attrs["href"]).group(1)))   # CourtListener linked it
                elif "no-link" in cls and k.find("law") is None and k.find("usc") is None:
                    t = flat(k)
                    cs = citefind.find(t)
                    attrs = resolve_case_cite(con, cs[0], prev + t[:cs[0].start], t[cs[0].end:] + nxt, doc) if cs else None
                    if not cs:
                        doc.law()                        # a statute or regulation: an Id. after it is that law's
                    if attrs is None:
                        k.attrs["cm_plain"] = "1"        # not a case, or an Id. of an unknown case: no tint
                    else:
                        k.attrs.update({"cm_" + key: v for key, v in attrs.items()}, cm_done="1")
                elif "no-link" in cls:
                    doc.law()
            out.append(k)
            continue
        at = 0
        for c in citefind.find(k):
            attrs = resolve_case_cite(con, c, (prev + k[:c.start])[-400:], (k[c.end:] + nxt)[:160], doc)
            if attrs is None:
                continue
            e = El("case", attrs)
            e.kids.append(k[c.start:c.end])
            out += [k[at:c.start], e]
            at = c.end
        out.append(k[at:])
    node.kids = mark_dockets([k for k in out if k != ""], con, flat)


def mark_dockets(kids, con, flat):
    """Cases cited by docket number alone ("No. 25-233 (petition for cert. filed …)") -> <case> elements, after the
    reporter cites are marked (scotus.docket_only says how)."""
    from tribonian import scotus
    out = []
    for i, k in enumerate(kids):
        if not isinstance(k, str):
            out.append(k)
            continue
        nxt = "".join(flat(x) for x in kids[i + 1:])[:200]
        at = 0
        for m in scotus.ONLY_DOCKET_RE.finditer(k):
            ok, why, tint = scotus.docket_only(con, m.group(1), (k[m.end():] + nxt)[:200], ROOT / "cases" / "scotus")
            if tint is None:
                continue
            e = El("case", {"ok": "1"} if ok else dict({"why": why}, **({"tint": tint} if tint else {})))
            e.kids.append(m.group(0))
            out += [k[at:m.start()], e]
            at = m.end()
        out.append(k[at:])
    return [k for k in out if k != ""]


def plain(el, words=7):
    """First few words of a block, without page markers or footnote marks."""
    def walk(n):
        return "".join(k if isinstance(k, str) else
                       "" if k.tag in ("page-number", "footnotemark") else walk(k) for k in n.kids)
    w = clean(walk(el)).split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


# ---------------------------------------------------------------- US Code cites that changed (usc_status.py)
# A cite to a section that has since moved, gone, or been amended is highlighted yellow, struck through when the
# number moved or is gone, and followed by a short tag (mv rep om pre amd nf) and the current number. USC_MARKS=0
# turns it off (cites still link to their notes in Markdown).
USC_MARKS = os.environ.get("USC_MARKS", "1") != "0"


def mark_usc(node):
    """Code cites in node's own text -> <usc> elements. CourtListener's citation spans around them are unwrapped
    first, so a cite and its edition parenthetical ("(1976 ed., Supp. V)") are one string. Once per element."""
    if getattr(node, "usc_done", False):
        return
    node.usc_done = True
    from tribonian import usc_status as us
    kids = []
    for k in node.kids:
        if (isinstance(k, El) and k.tag == "span" and "citation" in k.attrs.get("class", "")
                and all(isinstance(x, str) for x in k.kids) and us.CITE_RE.search(k.text())):
            k = k.text()
        if isinstance(k, str) and kids and isinstance(kids[-1], str):
            kids[-1] += k
        else:
            kids.append(k)
    out = []
    for k in kids:
        if not isinstance(k, str):
            out.append(k)
            continue
        at = 0
        for m in us.CITE_RE.finditer(k):
            e = El("usc")
            e.kids.append(m.group(0))
            e.m = m
            out += [k[at:m.start()], e]
            at = m.end()
        out.append(k[at:])
    node.kids = [k for k in out if k != ""]


def usc_out(k, mode, ctx):
    from tribonian import usc_status as us
    m, txt = k.m, k.text()
    tag, moves, yr = us.mark(m, ctx.year) if USC_MARKS else ("ok", [], None)
    first = us.sections(m.group("secs"), m.group("sign"))
    note = us.note_name(us.ukey(m.group("title"), first[0])) if first else None
    if mode == "md":
        old = esc(txt, "md")
        if tag in ("ok", "amd", "§amd", "rd") and note in ctx.statutes:
            old = "[[%s|%s]]" % (note, old)
        if tag == "ok":
            return old
        new = ", ".join(("§ %s → " % o.split(":")[1] if len(moves) > 1 else "") +
                        ", ".join("[[%s|%s]]" % (us.note_name(n), us.label(n)) if us.note_name(n) in ctx.statutes
                                  else us.label(n) for n in ns) for o, ns in moves)
        flag = "**%s**%s%s" % (tag, " %d" % yr if yr else "", " " + new if new else "")
        if tag == "§amd":                    # Obsidian has one highlight colour: plain text, tag in italics
            return "%s *%s*" % (old, flag.replace("**", ""))
        return ("==%s==" if tag in ("amd", "rd") else "==~~%s~~==") % old + " ==%s==" % flag
    old = esc(txt, "tex")
    if tag == "ok":
        return old
    new = ", ".join(("§ %s → " % o.split(":")[1] if len(moves) > 1 else "") + ", ".join(us.label(n) for n in ns)
                    for o, ns in moves)
    flag = "\\textbf{%s}%s" % (esc(tag, "tex"), esc((" %d" % yr if yr else "") + (" " + new if new else ""), "tex"))
    hl = {"amd": "\\uscmark", "rd": "\\uscmark", "§amd": "\\usclight"}.get(tag, "\\uscstrike")
    return "%s{%s}{}\\,%s{%s}" % (hl, old, "\\usclight" if tag == "§amd" else "\\uscmark", flag)


def statute_note(text):
    """'15 U. S. C. § 1392' -> '15-usc-1392', the name usc2md.py gives that section's note."""
    m = re.match(r"\s*(\d+)\s*U\.\s?S\.\s?C\.\s*(?:A\.\s*)?§+\s*(\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?)", text or "")
    return "%s-usc-%s" % (m.group(1), m.group(2).lower()) if m else None


def statute_notes():
    """Names of the statute and regulation notes that exist: md/ (US Code), md-cfr/ (C.F.R., cfr.py notes)."""
    out = set()
    for d, pat in ((ROOT / "md", "*-usc-*.md"), (ROOT / "md-cfr", "*-cfr-*.md"), (ROOT / "md-state", "*.md")):
        if d.is_dir():
            out |= {p.stem for p in d.rglob(pat)}
    return out


def clean(s):
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*(•|\\textbullet\{\})$", "", s)     # scan speck read as a bullet at a paragraph's end


# ---------------------------------------------------------------- case profile
# Counted from the local database (casedb.py): how many later cases cite this one, per year.
# Cached in cases/citedby/<cluster>/profile.json and recounted when the bulk data changes.
PROFILE_COLOR = "cyan!12"
RECENT = 5                     # "recent" = the last 5 full years before the bulk data's date
_PROFILE_DB = []


def profile_data(cid, scan=False, quick=None):
    """-> {"stamp", "years": {year: citing cases}, "goodlaw": {...} when scan} or None (no database,
    nothing cached). scan=True also runs the good-law check (casemap.goodlaw): full cases only,
    since it reads citing opinions' text. quick=N (scan False) runs the quick check instead, reading
    the newest N citing opinions; a current full result satisfies it and is kept."""
    if cid is None:
        return None
    path = CASES / "citedby" / str(cid) / "profile.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if not _PROFILE_DB:
        _PROFILE_DB.append(local_db())         # one connection per process
    con = _PROFILE_DB[0]
    if con is None:
        return old
    try:
        row = con.execute("SELECT file FROM loaded WHERE name='citation-map'").fetchone()
        stamp = re.search(r"\d{4}-\d\d-\d\d", row[0]).group(0) if row else ""
        from tribonian import casemap
        ogl = (old or {}).get("goodlaw") or {}
        fresh = ogl.get("version") == casemap.GOODLAW_VERSION
        if scan and ogl.get("mode") == "quick":   # a full build redoes a quick result
            fresh = False
        rp = casemap.recent_path(cid)             # recent citers searched since the last check: read them
        if fresh and rp.exists() and rp.stat().st_mtime > path.stat().st_mtime:
            fresh = False
        if old and old.get("stamp") == stamp and (fresh or not (scan or quick)) and "parens" in old:
            if "successor" not in old:               # cached before the successor line existed: add it only
                old["successor"] = successor_data(cid)
                write_profile(path, old)
            return old
        op = casemap.casedb.op_table(con)
        years = {y: n for y, n in con.execute(
            "SELECT substr(cl.date_filed, 1, 4) AS y, count(DISTINCT cl.id) FROM edges e "
            "JOIN %s o ON o.id = e.citing JOIN clusters cl ON cl.id = o.cluster_id "
            "WHERE e.cited IN (SELECT id FROM %s WHERE cluster_id = ?) AND cl.id != ? "
            "GROUP BY y" % (op, op), (cid, cid)) if y and y.isdigit()}
    except sqlite3.Error as e:               # tables not loaded yet, or an older database
        print("  note: no cited-by counts (%s)" % e)
        return old
    new = {"stamp": stamp, "years": years, "parens": parens_data(con, cid), "successor": successor_data(cid)}
    if scan or quick:
        from tribonian import casemap
        try:
            new["goodlaw"] = casemap.goodlaw(con, cid, quick=None if scan else quick)
        except (sqlite3.Error, SystemExit) as e:   # casemap exits when the text database is missing
            print("  note: good-law check failed (%s)" % e)
    elif old and old.get("goodlaw"):        # a quick build keeps the last check rather than dropping it
        new["goodlaw"] = old["goodlaw"]
    write_profile(path, new)
    return new


def write_profile(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def successor_data(cid):
    """Co-citation's likely successor (cocite_shift.successor): None when the views aren't built."""
    try:
        from tribonian import cocite_shift
    except ImportError:                      # numpy missing
        return None
    return cocite_shift.successor(int(cid))


PARENS_COLOR = "green!8"       # later courts' words: not this opinion, not ours either
PARENS_TOP = 8


def parens_data(con, cid):
    """How later courts summarise the case: CourtListener's parentheticals ("holding that ...")
    about its opinions, in CourtListener's similarity groups, largest first. Each group shows its
    best-scored wording and the decision that wrote it. None when the table isn't loaded."""
    try:
        ops = [o for (o,) in con.execute("SELECT id FROM ops.opinions WHERE cluster_id = ?", (cid,))]
        if not ops:
            return None
        q = ",".join("?" * len(ops))
        total = con.execute("SELECT count(*) FROM parens WHERE described IN (%s)" % q, ops).fetchone()[0]
        groups = con.execute("SELECT group_id, count(*) AS n FROM parens WHERE described IN (%s) AND group_id IS NOT NULL "
                             "GROUP BY group_id ORDER BY n DESC LIMIT ?" % q, ops + [PARENS_TOP]).fetchall()
        out = []
        for gid, n in groups:
            text, describing = con.execute("SELECT text, describing FROM parens WHERE described IN (%s) AND group_id = ? "
                                           "ORDER BY score DESC LIMIT 1" % q, ops + [gid]).fetchone()
            who = con.execute("SELECT cl.id, coalesce(nullif(cl.case_name_short,''), cl.case_name), substr(cl.date_filed,1,4), "
                              "ct.citation_string FROM ops.opinions o JOIN clusters cl ON cl.id = o.cluster_id "
                              "LEFT JOIN dockets d ON d.id = cl.docket_id LEFT JOIN courts ct ON ct.id = d.court_id "
                              "WHERE o.id = ?", (describing,)).fetchone() or (None, None, None, None)
            out.append({"n": n, "text": re.sub(r"\s+", " ", text or "").strip(), "cluster": who[0], "case": who[1],
                        "year": who[2], "court": who[3]})
        return {"total": total, "groups": out}
    except sqlite3.Error as e:               # no parentheticals loaded, or an older database
        print("  note: no parentheticals (%s)" % e)
        return None


def parens_md(meta):
    pd = (meta.get("profile") or {}).get("parens")
    if not pd or not pd.get("groups"):
        return []
    L = ["## How later courts describe it", "",
         "*The parentheticals later courts wrote after citing this case (\"holding that …\"), grouped by CourtListener "
         "into similar wordings, largest group first; each shows the group's best-scored wording. %s parentheticals in all. "
         "Other courts' words, not this opinion's: quote the opinion.*" % format(pd["total"], ","), ""]
    for i, g in enumerate(pd["groups"], 1):
        src = ", ".join(x for x in (esc(g["case"] or "", "md"), g["court"] or "", g["year"] or "") if x)
        L.append("%d. **%d** · (%s)%s" % (i, g["n"], esc(g["text"][:500], "md"), " — *%s*" % src if src else ""))
    return L + [""]


def parens_tex(meta):
    pd = (meta.get("profile") or {}).get("parens")
    if not pd or not pd.get("groups"):
        return ""
    items = "".join("\\item[\\textbf{%d}] (%s)%s\n" % (
        g["n"], esc(g["text"][:500], "tex"),
        " {\\footnotesize --- \\emph{%s}}" % esc(", ".join(x for x in (g["case"] or "", g["court"] or "", g["year"] or "") if x), "tex")
        if g["case"] else "") for g in pd["groups"])
    return ("\\pdfbookmark[1]{How later courts describe it}{parens}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!40!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n"
            "\\textbf{How later courts describe it.} {\\footnotesize Parentheticals later courts wrote after citing this "
            "case, in CourtListener's groups of similar wordings, largest first (the number is the group's size; %s in all). "
            "Other courts' words, not this opinion's.}\\par\\vspace{2pt}\n"
            "\\begin{list}{}{\\leftmargin=2.2em\\labelwidth=1.8em\\labelsep=0.4em\\itemsep=2pt\\parsep=0pt\\topsep=2pt}\n%s"
            "\\end{list}\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        PARENS_COLOR, PARENS_COLOR.split("!")[0], format(pd["total"], ","), items)


def profile_stats(prof, decided):
    """Totals and the recent window, from the per-year counts."""
    years = {int(y): n for y, n in (prof or {}).get("years", {}).items()}
    if not years:
        return None
    end = int((prof.get("stamp") or str(max(years)))[:4]) - 1          # last full year in the data
    first = int(decided[:4]) if (decided or "")[:4].isdigit() else min(years)
    span = list(range(first, end + 1))            # full years only: the bulk data's own year is partial
    recent = sum(years.get(y, 0) for y in range(end - RECENT + 1, end + 1))
    before = sum(years.get(y, 0) for y in range(end - 2 * RECENT + 1, end - RECENT + 1))
    total = sum(years.values())
    peak = max(years, key=years.get)
    trend = ("new" if not before else "rising" if recent > 1.2 * before else
             "falling" if recent < 0.8 * before else "steady")
    return {"years": years, "span": span, "end": end, "recent": recent, "before": before,
            "total": total, "peak": peak, "trend": trend, "stamp": prof.get("stamp", ""),
            "goodlaw": prof.get("goodlaw"), "successor": prof.get("successor")}


GL_COLORS = {"green": ("#2e8b57", "green!55!black"), "yellow": ("#d4a017", "yellow!85!orange"),
             "red": ("#c0392b", "red!75!black")}
GL_WORDS = {"green": "no warning words found", "yellow": "doubts: check its status", "red": "likely overruled or superseded: check before relying on it"}


def bar_status(st, label):
    """Colour of the bar that starts at year <label>: the check's status as of that year."""
    gl, y = st.get("goodlaw") or {}, int(label)
    if gl.get("red_year") and y >= int(gl["red_year"]):
        return "red"
    if gl.get("yellow_year") and y >= int(gl["yellow_year"]):
        return "yellow"
    return "green"


def quick_window(gl):
    w = gl.get("window")
    return "filed %d–%d" % tuple(w) if w and w[0] != w[1] else "filed %d" % w[0] if w else "none found"


def goodlaw_md(st):
    gl = st.get("goodlaw")
    if not gl:
        return []
    if gl.get("mode") == "quick":
        L = ["**Good-law check (quick): %s** — %s. Read only the newest %s of %s citing opinions (%s), so this is the "
             "status now, not the year it changed; a full Build reads every one from a court that could overrule it." % (
                 gl["status"].upper(), GL_WORDS[gl["status"]], format(gl["read"], ","), format(gl["of"], ","), quick_window(gl)), ""]
    else:
        L = ["**Good-law check: %s** — %s. Read %s of %s citing opinions (every one from a court that could overrule it, "
             "plus the %d newest)." % (gl["status"].upper(), GL_WORDS[gl["status"]], format(gl["read"], ","), format(gl["of"], ","),
                                       __import__("tribonian.casemap", fromlist=["casemap"]).GOODLAW_RECENT), ""]
    rc = gl.get("recent")
    if rc:
        L += ["Includes %d citing cases filed after %s that the local database lacks (CourtListener search, %s; "
              "%d of them read)." % (rc["listed"], rc["after"], rc["searched"], rc["read"]), ""]
    if gl.get("red_year"):
        later = sum(n for y, n in st["years"].items() if y > int(gl["red_year"]))
        L += ["Still cited by %s cases after %s." % (format(later, ","), gl["red_year"]), ""]
    for e in gl["evidence"][:12]:
        L.append("- %s (%s): %s %s%s — [%s](%s/opinion/%d/%s/) · %s" % (
            e["year"], e["court"], e["kind"], "“%s”" % e["word"], " · could overrule" if e["above"] else "",
            esc(e["name"][:60], "md"), CL, e["cluster"], e["slug"], esc(e["snippet"], "md")))
    if gl.get("n_evidence", 0) > 12:
        L.append("- … %d more" % (gl["n_evidence"] - 12))
    return L + ([""] if gl["evidence"] else [])


SUCCESSOR_NOTE = "co-citation, not the text: it never changes the colour"


def successor_text(st):
    sc = st.get("successor")
    if not sc:
        return None
    return ("Role changed %s: likely successor %s (%.0f%% of the %s decisions citing this case in %s also cite %s)" % (
        sc["slice"], sc["name"], 100 * sc["share"], format(sc["citing"], ","), sc["slice"], sc["name"].split(",")[0]))


def successor_md(st):
    t = successor_text(st)
    return ["%s. *%s.*" % (esc(t, "md"), SUCCESSOR_NOTE.capitalize()), ""] if t else []


def successor_tex(st):
    t = successor_text(st)
    return "{\\small %s. {\\scriptsize(%s)}\\par}" % (esc(t, "tex"), esc(SUCCESSOR_NOTE, "tex")) if t else ""


def binned(st):
    """(label, count) bars: single years, or 5-year bins (ending at the last full year) past 60 years."""
    span, yrs = st["span"], st["years"]
    if len(span) <= 60:
        return [(str(y), yrs.get(y, 0)) for y in span]
    starts = list(range(span[-1] - 4, span[0] - 5, -5))[::-1]
    return [("%d" % max(a, span[0]), sum(yrs.get(y, 0) for y in range(a, a + 5))) for a in starts]


def profile_line(st):
    pct = 100.0 * st["recent"] / st["total"] if st["total"] else 0
    change = "" if not st["before"] else " (%+.0f%% on %d–%d)" % (100.0 * (st["recent"] - st["before"]) / st["before"],
                                                                 st["end"] - 2 * RECENT + 1, st["end"] - RECENT)
    return ("Cited by %s cases · %d–%d: %s, %.0f%% of all%s · %s · busiest year %d (%s)" % (
        format(st["total"], ","), st["end"] - RECENT + 1, st["end"], format(st["recent"], ","), pct, change,
        st["trend"], st["peak"], format(st["years"][st["peak"]], ",")))


def profile_md(meta):
    st = profile_stats(meta.get("profile"), meta.get("decided"))
    if not st:
        return []
    bars = binned(st)
    per = "per year" if len(bars) == len(st["span"]) else "per 5 years"
    series = []
    for colour in ("green", "yellow", "red"):          # one series per colour, zero elsewhere
        vals = [n if bar_status(st, l) == colour else 0 for l, n in bars]
        if any(vals):
            series.append((colour, vals))
    palette = ", ".join(GL_COLORS[c][0] for c, _ in series)
    # Mermaid's bar chart can't rotate x-axis labels and merges blank ones, so a long per-year
    # chart gets two-digit labels ('83) at a smaller size instead of overlapping four-digit ones.
    short = per == "per year" and len(bars) > 20
    labels = ["'%s" % str(l)[-2:] if short else l for l, _ in bars]
    axis = ', "xyChart": {"xAxis": {"labelFontSize": 11, "labelPadding": 4}}' if short else ""
    return (["## Case profile", "",
             profile_line(st) + ". *Counted from the CourtListener bulk data of %s.*" % st["stamp"], ""]
            + goodlaw_md(st) + successor_md(st) +
            ["```mermaid",
             '%%%%{init: {"themeVariables": {"xyChart": {"plotColorPalette": "%s"}}%s}}%%%%' % (palette, axis),
             "xychart-beta",
             '    title "Citing cases %s"' % per,
             "    x-axis [%s]" % ", ".join('"%s"' % l for l in labels),
             '    y-axis "cases"']
            + ["    bar [%s]" % ", ".join(str(n) for n in vals) for _, vals in series]
            + ["```", ""])


def profile_tex(meta):
    st = profile_stats(meta.get("profile"), meta.get("decided"))
    if not st:
        return ""
    bars = binned(st)
    top = max(n for _, n in bars) or 1
    W, H, n = 12.0, 1.8, len(bars)                 # cm; the box is about 17 cm wide on the page
    w = W / n
    rects = "".join("\\fill[%s] (%.3f,0) rectangle (%.3f,%.3f);" % (GL_COLORS[bar_status(st, l)][1], i * w + 0.1 * w,
                                                                    (i + 0.9) * w, H * c / top)
                    for i, (l, c) in enumerate(bars) if c)
    step = max(1, round(n / 8))                    # about 8 labels under the bars
    labels = "".join("\\node[anchor=north,font=\\tiny] at (%.3f,0) {%s};" % ((i + 0.5) * w, l)
                     for i, (l, _) in enumerate(bars) if i % step == 0)
    chart = ("\\begin{tikzpicture}\\draw[gray] (0,0) -- (%.2f,0);%s%s"
             "\\node[anchor=south west,font=\\tiny] at (0,%.2f) {%s %s};\\end{tikzpicture}") % (
        W, rects, labels, H, format(top, ","), "per year" if len(bars) == len(st["span"]) else "per 5 years")
    return ("\\pdfbookmark[1]{Case profile}{profile}\n"
            "\\begin{tcolorbox}[enhanced, colback=%s, colframe=cyan!50!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n"
            "%s\\par%s\\vspace{3pt}{\\centering %s\\par}\n"
            "{\\scriptsize Counted from the CourtListener bulk data of %s.\\par}\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        PROFILE_COLOR, esc(profile_line(st), "tex"), goodlaw_tex(st) + successor_tex(st), chart, esc(st["stamp"], "tex"))


def goodlaw_tex(st):
    gl = st.get("goodlaw")
    if not gl:
        return ""
    quick = gl.get("mode") == "quick"
    head = ("\\vspace{2pt}{\\color{%s}\\rule{0.8em}{0.8em}}\\ \\textbf{Good-law check%s: %s} --- %s "
            "{\\scriptsize(%s %s of %s citing opinions%s)}\\par") % (
        GL_COLORS[gl["status"]][1], " (quick)" if quick else "", gl["status"].upper(), esc(GL_WORDS[gl["status"]], "tex"),
        "read the newest" if quick else "read", format(gl["read"], ","), format(gl["of"], ","),
        esc("; %s; status now, not the year it changed" % quick_window(gl), "tex") if quick else "")
    items = "".join("{\\scriptsize %s (%s): %s ``%s''%s --- \\emph{%s}\\par}" % (
        e["year"], esc(e["court"], "tex"), e["kind"], esc(e["word"], "tex"), ", could overrule" if e["above"] else "",
        esc(e["name"][:60], "tex")) for e in gl["evidence"][:6])
    rc = gl.get("recent")
    if rc:
        items += "{\\scriptsize includes %d citing cases filed after %s (CourtListener search, %s)\\par}" % (
            rc["listed"], esc(rc["after"], "tex"), esc(rc["searched"], "tex"))
    return head + items


# ---------------------------------------------------------------- headmatter
# The published case's front matter (Harvard CAP): parties, docket number, dates, counsel,
# with its own footnotes. In the cluster's "headmatter" field; not part of any opinion.
HEAD_COLOR = "yellow!40"       # the PDF box; the reMarkable Paper Pro shows it as a pale yellow
HEAD_LINE = ("docketnumber", "otherdate", "decisiondate")   # run together on one line


def headmatter_parts(meta, mode):
    """-> (blocks, footnotes): blocks are (tag, text) in order, footnotes (label, text)."""
    raw = meta.get("headmatter") or ""
    if not raw.strip():
        return [], []
    root = parse_html(raw)
    ctx = Ctx(meta.get("reporter", ""), "headmatter", -1)
    ctx.record = False                                   # page markers here don't move the running header

    def marks_for(n):                                    # <a class="footnote" href="#fn*">*</a> -> footnotemark
        for i, k in enumerate(n.kids):
            if isinstance(k, El):
                if k.tag == "a" and "footnote" in k.attrs.get("class", "") and k.attrs.get("href", "").startswith("#fn"):
                    m = El("footnotemark")
                    m.kids = [k.text().strip()]
                    n.kids[i] = m
                else:
                    marks_for(k)
    blocks, notes = [], []

    def walk(n):
        for k in n.kids:
            if not isinstance(k, El) or k.tag == "br":
                continue
            if k.tag == "div" and "footnotes" in k.attrs.get("class", ""):
                for f in k.kids:
                    if isinstance(f, El) and f.tag == "div":
                        f.kids = [x for x in f.kids if not (isinstance(x, El) and x.tag == "a")]   # the back link
                        marks_for(f)
                        text = " ".join(clean(inline(x, mode, ctx, [])) for x in f.kids if isinstance(x, El)) \
                            or clean(inline(f, mode, ctx, []))
                        notes.append((f.attrs.get("label") or "?", text))
            elif k.tag == "div":
                walk(k)
            else:
                marks_for(k)
                t = re.sub(r"\s+(\[\^|\\textsuperscript)", r"\1", clean(inline(k, mode, ctx, [])))   # mark hugs its word
                if t:
                    blocks.append((k.tag, t))
    walk(root)
    return blocks, notes


def headmatter_md(meta):
    blocks, notes = headmatter_parts(meta, "md")
    if not blocks:
        return []
    ctx = Ctx("", "", -1)
    fix = lambda t: re.sub(r"\[\^([^\]]+)\]", lambda m: "[^hm-%s]" % m.group(1), t)   # keys apart from the opinions'
    L, line = ["## Headmatter", ""], []
    for tag, t in blocks + [("", "")]:
        if tag in HEAD_LINE:
            line.append(t.rstrip(" —-"))
            continue
        if line:
            L += [" · ".join(line), ""]
            line = []
        if tag == "parties":
            L += ["**%s**" % t, ""]
        elif tag:
            L += [t, ""]
    for label, t in notes:
        L += ["[^%s]: %s" % (ctx.fnkey(label), t), ""]
    return [fix(re.sub(r"\s+(\[\^)", r"\1", x)) for x in L]


def headmatter_tex(meta):
    blocks, notes = headmatter_parts(meta, "tex")
    if not blocks:
        return ""
    out, line = [], []
    for tag, t in blocks + [("", "")]:
        if tag in HEAD_LINE:
            line.append(t.rstrip(" —-"))
            continue
        if line:
            out.append("{\\centering %s\\par}\\vspace{2pt}" % " \\textperiodcentered\\ ".join(line))
            line = []
        if tag == "parties":
            out.append("{\\centering\\bfseries %s\\par}\\vspace{2pt}" % t)
        elif tag:
            out.append(t + "\\par")
    if notes:
        out.append("\\vspace{2pt}{\\footnotesize " + "\\par ".join("\\textsuperscript{%s} %s" % (esc(l, "tex"), t)
                                                               for l, t in notes) + "\\par}")
    return ("\\pdfbookmark[1]{Headmatter}{headmatter}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!60!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        HEAD_COLOR, HEAD_COLOR.split("!")[0], "\n".join(out))


# ---------------------------------------------------------------- headings

ROMAN_RE = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")


def to_roman(n):
    out = ""
    for v, s in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
                 (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= v:
            out, n = out + s, n - v
    return out


def from_roman(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    n = 0
    for a, b in zip(s, s[1:] + " "):
        n += -vals[a] if vals.get(b, 0) > vals[a] else vals[a]
    return n


def is_roman(s):
    return bool(s) and bool(ROMAN_RE.match(s))


def heading_candidate(p):
    """A <p> that is a Part heading ("II", "A", "1") -- or OCR wreckage of one
    ("r-H h-I" for III, ">" for IV). Short, no sentence in it."""
    t = clean(p.text())
    if not t or len(t) > 10 or p.find("page-number") is not None:
        return None
    if is_roman(t) or re.fullmatch(r"[A-Z]|\d{1,2}", t):
        return t
    if t.endswith(".") or re.search(r"[a-z]{3,}", t):
        return None
    return t


def valid_label(t):
    return is_roman(t) or bool(re.fullmatch(r"[A-Z]|\d{1,2}", t))


def repair_headings(labels, reference, warn):
    """labels: candidate heading texts, in order, across the whole case.
    reference: the <h2> headings of CourtListener's 'combined' copy of the same
    case (a different digitization), when it has one."""
    if all(valid_label(t) for t in labels):
        return labels
    if reference and len(reference) == len(labels) and all(
            a == b for a, b in zip(labels, reference) if valid_label(a)):
        return list(reference)
    fixed = list(labels)                     # fall back: fill I, II, ?, ?, V by counting
    i = 0
    while i < len(fixed):
        if valid_label(fixed[i]):
            i += 1
            continue
        j = i
        while j < len(fixed) and not valid_label(fixed[j]):
            j += 1
        prev = next((from_roman(t) for t in reversed(fixed[:i]) if is_roman(t)), 0)
        nxt = from_roman(fixed[j]) if j < len(fixed) and is_roman(fixed[j]) else None
        if nxt is not None and nxt - prev - 1 == j - i:
            for k in range(i, j):
                fixed[k] = to_roman(prev + 1 + k - i)
        else:
            warn("unreadable heading(s) %r left as-is" % fixed[i:j])
        i = j
    return fixed


def assign_levels(labels):
    """-> [(level, path)], level 1 = Roman part, 2 = letter, 3 = number."""
    out, rn, letter, digit, cur = [], 0, None, 0, ["", "", ""]
    for t in labels:
        nxt_letter = "A" if letter is None else chr(ord(letter) + 1)
        if t == to_roman(rn + 1):
            lvl = 1
        elif t == nxt_letter:
            lvl = 2
        elif t.isdigit() and int(t) == digit + 1:
            lvl = 3
        elif is_roman(t):
            lvl = 1
        elif t.isdigit():
            lvl = 3
        elif re.fullmatch(r"[A-Z]", t):
            lvl = 2
        else:
            lvl = 1
        if lvl == 1:
            rn, letter, digit = (from_roman(t) if is_roman(t) else rn + 1), None, 0
            cur = [t, "", ""]
        elif lvl == 2:
            letter, digit = t, 0
            cur = [cur[0], t, ""]
        else:
            digit = int(t)
            cur = [cur[0], cur[1], t]
        out.append((lvl, "-".join(c for c in cur if c)))
    return out


# ---------------------------------------------------------------- opinion model

TYPE_NAMES = {"majority": "opinion of the Court", "plurality": "plurality opinion",
              "concurrence": "concurring", "dissent": "dissenting",
              "rehearing": "on rehearing", "remittitur": "remittitur"}


def short_author(author, fallback):
    a = clean(author).rstrip(",.:; ")
    m = re.match(r"(?i)^(m[re]\.? )?(chief )?justice (.+)$", a)      # "Me." = the scanner's misreading of "Mr."
    if m:
        return "%s, %s" % (m.group(3).title() if m.group(3).isupper() else m.group(3),
                           "C.J." if m.group(2) else "J.")
    if re.match(r"(?i)^per curiam", a):
        return "Per Curiam"
    return a or fallback or "Opinion"


class Part:
    def __init__(self, level=0, label="", path=""):
        self.level, self.label, self.path = level, label, path
        self.blocks, self.children = [], []       # blocks: ("p"|"q", El)


def html_footnotes(root):
    """The other footnote form CourtListener serves (<a class="footnote" href="#fn1">1</a> in the text, the notes in
    <div class="footnotes"><div class="footnote" label="1">) -> Harvard's (<footnotemark>, <footnote label>), which the
    rest of the reader knows. Without it the marks print as a plain "1" and the notes as a closing paragraph."""
    def walk(n):
        out = []
        for k in n.kids:
            if isinstance(k, El) and k.tag == "a" and "footnote" in k.attrs.get("class", "") \
                    and re.match(r"#fn\d+$", k.attrs.get("href", "")):
                m = El("footnotemark")
                m.kids = [k.text().strip()]
                out.append(m)
            elif isinstance(k, El) and k.tag == "div" and "footnotes" in k.attrs.get("class", "").split():
                for f in k.kids:
                    if isinstance(f, El) and f.tag == "div" and "footnote" in f.attrs.get("class", "").split():
                        fn = El("footnote", {"label": f.attrs.get("label") or re.sub(r"\D", "", f.attrs.get("id", ""))})
                        fn.kids = [x for x in f.kids if not (isinstance(x, El) and x.tag == "a")]   # the back link
                        for x in fn.kids:                # "<p>. The program is …": the mark's leftover period
                            if isinstance(x, El) and x.kids and isinstance(x.kids[0], str):
                                x.kids[0] = re.sub(r"^\s*\.\s+", "", x.kids[0])
                                break
                        out.append(fn)
            else:
                if isinstance(k, El):
                    walk(k)
                out.append(k)
        n.kids = out
    walk(root)
    return root


def read_opinion(op, oi):
    """API opinion dict -> dict with byline pieces, a flat block list, footnotes."""
    root = html_footnotes(parse_html(op.get("html_with_citations") or op.get("xml_harvard") or ""))
    body = root.find("opinion") or root
    o = {"oi": oi, "kind": body.attrs.get("type", ""), "author": "", "byline": [],
         "flat": [], "footnotes": {}, "api": op}
    for k in body.kids:
        if isinstance(k, str):
            if k.strip():
                p = El("p")
                p.kids.append(k)
                o["flat"].append(["p", p])
            continue
        if k.tag == "author":
            o["author"] = clean(k.text())
        elif k.tag == "judges":
            o["byline"].append(k)
        elif k.tag == "footnote":
            o["footnotes"][k.attrs.get("label", "").strip()] = k
        elif k.tag == "blockquote":
            o["flat"].append(["q", k])
        elif k.tag == "p":
            t = clean(k.text())
            if not o["flat"] and not o["byline"] and t[:1].islower():   # "delivered the opinion of the Court."
                o["byline"].append(k)
            elif t:
                lab = heading_candidate(k)
                o["flat"].append(["h", lab] if lab else ["p", k])
        else:
            o["flat"].append(["p", k])
    first = root.find("page-number")
    o["start"] = str(int(first.attrs["label"]) - 1) if first is not None and first.attrs.get("label", "").isdigit() else ""
    return o


def tree_of(o, levels):
    """Flat blocks -> Part tree, mirroring a statute's subsection/paragraph nesting."""
    top = Part()
    stack = [top]
    it = iter(levels)
    for kind, val in o["flat"]:
        if kind == "h":
            lvl, path = next(it)
            while stack[-1].level >= lvl:
                stack.pop()
            p = Part(lvl, val, path)
            stack[-1].children.append(p)
            stack.append(p)
        else:
            stack[-1].blocks.append((kind, val))
    return top


# ---------------------------------------------------------------- markdown

def fn_md(o, ctx, label):
    fn = o["footnotes"].get(label)
    if fn is None:
        return None
    paras = []
    for k in fn.kids:
        if isinstance(k, El) and k.tag in ("p", "blockquote"):
            t = clean(inline(k, "md", ctx, []))
            if t:
                paras.append(("> " if k.tag == "blockquote" else "") + t)
    return "[^%s]: %s" % (ctx.fnkey(label), "\n\n    ".join(paras))


def front_matter(meta, stub):
    L = yaml_head([("title", meta["title"]), ("aliases", meta["aliases"]), ("type", Raw("case")), ("stub", stub),
                   ("citation", meta["cite"]), ("citations", meta["cites"]), ("court", Raw(meta["court"])),
                   ("docket", meta["docket"]), ("decided", Raw(meta["decided"])), ("argued", meta["argued"] or None),
                   ("judges", meta["judges"]), ("courtlistener_cluster", Raw(meta["cluster"])),
                   ("cited_by_count", Raw(meta["cited_by"]) if meta.get("cited_by") is not None else None),
                   ("source", Raw(meta["url"])), ("retrieved", Raw(date.today().isoformat())), ("synthetic", False)])
    return L + ["", "# %s, %s (%s)" % (esc(meta["name"], "md"), meta["cite"], meta["year"]), ""]


def editorial(cluster):
    """The reporter's summary and syllabus, when CourtListener has them, as Obsidian
    callouts that say what they are. Neither is part of the opinion: for the US Supreme
    Court the syllabus is the Reporter of Decisions' work (United States v. Detroit Timber
    & Lumber Co., 200 U.S. 321, 337), so quote the opinion, not this. A few state courts
    (Ohio, West Virginia, Kansas) do write their own syllabus; the data does not say which."""
    import html as _html
    L = []
    for field, title in (("summary", "Reporter's summary"), ("syllabus", "Syllabus")):
        raw = cluster.get(field) or ""
        paras = [clean(_html.unescape(re.sub(r"<[^>]+>", " ", p))) for p in re.split(r"</p>|<br\s*/?>|\n\s*\n", raw)]
        paras = [esc(p, "md") for p in paras if p]
        if paras:
            L += ["> [!abstract]- %s (editorial, not part of the opinion)" % title] + ["> " + p + "\n>" for p in paras]
            L[-1] = L[-1][:-2]
            L.append("")
    return L


def editorial_tex(meta):
    """The reporter's summary and syllabus (editorial(), the note's callouts) as a front box in the PDF."""
    import html as _html
    cl = meta.get("editorial") or {}
    parts = []
    for field, title in (("summary", "Reporter's summary"), ("syllabus", "Syllabus")):
        raw = cl.get(field) or ""
        paras = [clean(_html.unescape(re.sub(r"<[^>]+>", " ", p))) for p in re.split(r"</p>|<br\s*/?>|\n\s*\n", raw)]
        paras = [esc(p, "tex") for p in paras if p]
        if paras:
            parts.append("\\textbf{%s.} {\\footnotesize\\itshape (editorial, not part of the opinion)}\\par\\vspace{2pt}%s"
                         % (title, "\\par\\vspace{2pt}".join(paras)))
    if not parts:
        return ""
    return ("\\pdfbookmark[1]{Reporter's summary and syllabus}{editorial}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=gray!6, colframe=gray!40!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n%s\n\\end{tcolorbox}\n"
            % "\\par\\vspace{6pt}".join(parts))


def cocite_md(meta):
    """'## Co-citation' from cocite.py's appellate view; nothing when it isn't built (or numpy is missing)."""
    if not meta or not meta.get("cluster"):
        return []
    try:
        from tribonian import cocite
        return cocite.case_md(int(meta["cluster"]), load_index())
    except (ImportError, sqlite3.Error, SystemExit, OSError) as e:   # view not built / numpy missing; a bug still raises
        print("  (co-citation section skipped: %s)" % e, file=sys.stderr)
        return []


def cocite_tex(meta):
    """The PDF's co-citation box (cocite.case_tex); '' when it isn't built."""
    if not meta or not meta.get("cluster"):
        return ""
    try:
        from tribonian import cocite
        return cocite.case_tex(int(meta["cluster"]), lambda t: esc(t, "tex"))
    except (ImportError, sqlite3.Error, SystemExit, OSError) as e:   # view not built / numpy missing; a bug still raises
        print("  (co-citation box skipped: %s)" % e, file=sys.stderr)
        return ""


# ---------------------------------------------------------------- what it draws on, coupled cases
# Bibliographic coupling (cocite.draws_on / cocite.coupling): a case placed by what it cites, so it
# works for cases the co-citation map never holds (cited too rarely, recent, unpublished). "What it
# draws on" is cheap and in every build; "Coupled with" only when casemap.py coupling has cached it
# (or build --coupling computes it first), because it reads the whole citation cache.
DRAWS_COLOR = "orange!8"       # like the co-citation box: derived by counting, not the court's text
COUPLING_COLOR = "orange!4"
COUPLING_SHOW_REFS = 5         # shared references named per coupled case; the JSON has them all
COUPLING_SHOW = 7              # coupled cases listed in the note and PDF; the JSON keeps cocite.COUPLING_TOP (30)
DRAWS_TOP = 8                  # sub-clusters listed; the rest are summed as "other sub-clusters"


def shared_db():
    """The one read-only database connection per process (profile_data opens it too), or None."""
    if not _PROFILE_DB:
        _PROFILE_DB.append(local_db())
    return _PROFILE_DB[0]


def bulk_stamp(con):
    """The bulk data's date (the citation map's file date), as profile.json stamps it."""
    row = con.execute("SELECT file FROM loaded WHERE name='citation-map'").fetchone()
    m = re.search(r"\d{4}-\d\d-\d\d", row[0]) if row else None
    return m.group(0) if m else ""


def json_cited(cid, data=None):
    """Opinion ids the case's own JSON says it cites (`opinions_cited` on each opinion). The fallback
    for cases newer than the bulk data: they have no rows in `edges`, but the API copy lists them."""
    if data is None:
        ent = load_index().get(str(cid), {})
        f = CASES / ent["full"] if ent.get("full") else None
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if f else {}
        except (OSError, ValueError):
            data = {}
    ids = set()
    for o in data.get("opinions") or []:
        for u in o.get("opinions_cited") or []:
            m = re.search(r"/opinions/(\d+)/", str(u))
            if m:
                ids.add(int(m.group(1)))
    return sorted(ids)


def draws_data(cid, data=None):
    """cocite.draws_on for this build: recomputed every time (it takes well under a second), never cached."""
    con = shared_db() if cid is not None else None
    if con is None:
        return None
    try:
        from tribonian import cocite
        return cocite.draws_on(int(cid), con, opinions=json_cited(cid, data))
    except (ImportError, sqlite3.Error, SystemExit, OSError) as e:   # view/opinions not there, numpy missing
        print("  (draws-on section skipped: %s)" % e, file=sys.stderr)
        return None


def coupling_path(cid):
    return CASES / "citedby" / str(cid) / "coupling.json"


def coupling_cache(cid, con=None, max_df=None, top=None):
    """Compute the coupled cases (cocite.coupling) and cache them in cases/citedby/<cluster>/coupling.json,
    stamped with the bulk data's date like profile.json. casemap.py coupling and build --coupling call this."""
    from tribonian import cocite
    con = con or shared_db()
    if con is None:
        sys.exit("coupling needs the local database (tribonian db status)")
    res = cocite.coupling(int(cid), con, max_df or cocite.COUPLING_MAX_DF, top or cocite.COUPLING_TOP,
                         opinions=json_cited(cid))
    res["stamp"] = bulk_stamp(con)
    res["computed"] = time.strftime("%Y-%m-%d %H:%M")
    write_profile(coupling_path(cid), res)            # atomic: temp file, then rename
    return res


def coupling_data(cid, compute=False):
    """The cached coupled cases, or None. compute=True (every build; --no-coupling turns it off) computes them when
    there is no cache yet, or the cache was made from older bulk data; otherwise the cache is used as it is."""
    if cid is None:
        return None
    if compute:
        try:
            con = shared_db()
            old = json.loads(coupling_path(cid).read_text(encoding="utf-8")) if coupling_path(cid).exists() else None
            if old and con is not None and old.get("stamp") == bulk_stamp(con):
                return old                               # current: nothing to compute
            return coupling_cache(cid, con)
        except (ImportError, sqlite3.Error, SystemExit, OSError, ValueError) as e:
            print("  (coupling skipped: %s)" % e, file=sys.stderr)
    try:
        return json.loads(coupling_path(cid).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def draws_md(meta):
    d = meta.get("draws")
    if not d:
        return []

    def wl(vault, text, cell=True):                  # a vault note link, as cocite.case_md makes them
        text = (text or "").replace("|", "/")
        return "[[%s%s%s]]" % (vault, "\\|" if cell else "|", text) if vault else text
    L = ["## What it draws on", "",
         "*Where the %d cases this one cites sit in the co-citation map: each cited case's sub-cluster in %s, the "
         "newest full slice (bibliographic coupling: a case placed by what it cites, so it works for any case). "
         "Counted from the citations; sub-cluster names were written by Claude from each cluster's evidence and "
         "are not part of the record.*" % (d["refs"], d["label"]), ""]
    if not d.get("node"):
        L += ["*This case is not in the co-citation map itself (cited too rarely, or too recently), so this is where "
              "the map places it.*", ""]
    if d["subs"]:
        L += ["| Sub-cluster | Cites | Share | Area |", "|---|---:|---:|---|"]
        for s in d["subs"][:DRAWS_TOP]:
            L.append("| `%s` %s | %d | %d%% | %s |" % (s["code"], wl(s.get("vault"), s["name"]), s["n"],
                                                      round(100 * s["share"]), wl(s.get("area_vault"), s.get("area_name") or "")))
        rest = d["subs"][DRAWS_TOP:]
        if rest:
            n = sum(s["n"] for s in rest)
            L.append("| *%d other sub-clusters* | %d | %d%% | |" % (len(rest), n, round(100.0 * n / d["refs"])))
        L.append("")
    if d["unclustered"]:
        L += ["**Unclustered:** %d of the %d cited cases are in no sub-cluster of %s (cited too rarely then)." % (
            d["unclustered"], d["refs"], d["label"]), ""]
    if d["areas"]:
        L += ["**Areas:** " + " · ".join("%s %d%%" % (wl(a.get("vault"), a["name"], False), round(100 * a["share"]))
                                         for a in d["areas"][:6]), ""]
    return L


def draws_tex(meta):
    d = meta.get("draws")
    if not d:
        return ""
    E = lambda t: esc(t or "", "tex")
    lab = E(d["label"].replace("-", "\u2013"))
    out = ["{\\scriptsize\\itshape Not part of the record. Where the %d cases this one cites sit in the co-citation map: "
           "each one's sub-cluster in %s, the newest full slice. Counted from the citations; sub-cluster names were "
           "written by Claude from each cluster's evidence.\\par}\\vspace{3pt}" % (d["refs"], lab)]
    if not d.get("node"):
        out.append("\\textbf{Not in the co-citation map itself} {\\footnotesize (cited too rarely, or too recently)}: "
                   "this is where its own citations place it.\\par\\vspace{2pt}")
    subs = d["subs"][:DRAWS_TOP]
    if subs:
        mx = max(s["share"] for s in subs) or 1
        rows = ["\\raggedright\\textcolor{gray}{\\texttt{%s}} %s & %d & \\textcolor{orange!70!black}{\\rule{%.2fcm}{0.55em}}\\ %d\\%% "
                "& \\raggedright %s \\tabularnewline" % (E(s["code"]), E(s["name"]), s["n"], max(0.05, 1.8 * s["share"] / mx),
                                                        round(100 * s["share"]), E(s.get("area_name")))
                for s in subs]
        rest = d["subs"][DRAWS_TOP:]
        if rest:
            n = sum(s["n"] for s in rest)
            rows.append("\\emph{%d other sub-clusters} & %d & %d\\%% & \\tabularnewline" % (len(rest), n, round(100.0 * n / d["refs"])))
        out.append("{\\scriptsize\\setlength{\\tabcolsep}{3pt}\\renewcommand{\\arraystretch}{1.15}\\noindent"
                   "\\begin{tabular}{@{}p{0.42\\linewidth} r p{0.2\\linewidth} p{0.26\\linewidth}@{}}"
                   "\\textbf{Sub-cluster} & \\textbf{Cites} & \\textbf{Share} & \\textbf{Area}\\tabularnewline\\hline %s"
                   "\\end{tabular}\\par}" % "\n".join(rows))
    tail = []
    if d["unclustered"]:
        tail.append("%d of the %d cited cases are in no sub-cluster of %s (cited too rarely then)." % (
            d["unclustered"], d["refs"], lab))
    if d["areas"]:
        tail.append("\\textbf{Areas:} " + " \\textperiodcentered{} ".join(
            "%s %d\\%%" % (E(a["name"]), round(100 * a["share"])) for a in d["areas"][:6]) + ".")
    if tail:
        out.append("\\vspace{3pt}{\\footnotesize %s\\par}" % " ".join(tail))
    return ("\\pdfbookmark[1]{What it draws on}{drawson}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!50!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n"
            "\\textbf{What it draws on.}\\par\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        DRAWS_COLOR, DRAWS_COLOR.split("!")[0], "\n".join(out))


def coupling_note(c, tex=False):
    """The one-line method note both outputs carry."""
    fmt = lambda n: format(n, ",")
    return ("Cases that cite the most of the same cases as this one, rare shared citations counting more: each shared "
            "cited case, cited by df decisions, adds log(N/df), N = %s decisions citing anything. Cited cases cited "
            "by more than %s decisions are left out (%d of this case's %d). Computed %s from the bulk data of %s by "
            "%s." % (fmt(c["N"]), fmt(c["max_df"]), c["skipped"], c["refs"], c.get("computed", "?"), c.get("stamp") or "?",
                     "\\texttt{casemap.py coupling}" if tex else "`tribonian map coupling`"))


def coupling_md(meta):
    c = meta.get("coupling")
    if not c or not c.get("results"):
        return []
    index = load_index()

    def link(r, text):
        e = index.get(str(r))
        return "[[%s|%s]]" % (e["slug"], text.replace("|", "/")) if e else text
    L = ["## Coupled with", "", "*%s Not part of the record.*" % coupling_note(c), ""]
    for i, x in enumerate(c["results"][:COUPLING_SHOW], 1):
        where = ", ".join(t for t in (x["cite"], x["court"], x["year"]) if t)
        refs = "; ".join(esc(r["name"], "md") for r in x["refs"][:COUPLING_SHOW_REFS])
        more = " …" if len(x["refs"]) > COUPLING_SHOW_REFS else ""
        L.append("%d. %s%s · score %.1f · %d shared: %s%s" % (i, link(x["cluster"], esc(x["name"], "md")),
                                                             " (%s)" % where if where else "", x["score"], x["shared"], refs, more))
    return L + [""]


def coupling_items_tex(c):
    """The coupled cases of one coupling result, as list items (coupling_tex, scotus.coupling_appendix_tex)."""
    E = lambda t: esc(t or "", "tex")
    return "".join("\\item[\\textbf{%d}] %s%s {\\footnotesize\\ score %.1f; shared: %s%s}\n" % (
        x["shared"], E(x["name"]), " (%s)" % E(", ".join(t for t in (x["cite"], x["court"], x["year"]) if t)) if x["cite"] or x["year"] else "",
        x["score"], E("; ".join(r["name"] for r in x["refs"][:COUPLING_SHOW_REFS])), " \\dots" if len(x["refs"]) > COUPLING_SHOW_REFS else "")
        for x in c["results"][:COUPLING_SHOW])


def coupling_tex(meta):
    c = meta.get("coupling")
    if not c or not c.get("results"):
        return ""
    E = lambda t: esc(t or "", "tex")
    items = "".join("\\item[\\textbf{%d}] %s%s {\\footnotesize\\ score %.1f; shared: %s%s}\n" % (
        x["shared"], E(x["name"]), " (%s)" % E(", ".join(t for t in (x["cite"], x["court"], x["year"]) if t)) if x["cite"] or x["year"] else "",
        x["score"], E("; ".join(r["name"] for r in x["refs"][:COUPLING_SHOW_REFS])), " \\dots" if len(x["refs"]) > COUPLING_SHOW_REFS else "")
        for x in c["results"][:COUPLING_SHOW])
    return ("\\pdfbookmark[1]{Coupled with}{coupling}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!50!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n"
            "\\textbf{Coupled with.} {\\footnotesize Not part of the record. %s The number is the count of shared cited cases.}\\par\\vspace{2pt}\n"
            "\\begin{list}{}{\\leftmargin=2.2em\\labelwidth=1.8em\\labelsep=0.4em\\itemsep=1pt\\parsep=0pt\\topsep=2pt}\n%s"
            "\\end{list}\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        COUPLING_COLOR, COUPLING_COLOR.split("!")[0], coupling_note(c, True), items)


def render_stub(meta, tail, why="Text not fetched.", cluster=None):
    L = front_matter(meta, True) + profile_md(meta) + cocite_md(meta) + parens_md(meta) + headmatter_md(meta) + editorial(cluster or {})
    L += ["*Stub: %s To get the text: `tribonian case fetch --cluster %s`, then `build`.*" % (why, meta["cluster"]), ""]
    return "\n".join(L) + "\n" + tail


def render_md(meta, ops, tail="", cluster=None, head=None):
    L = list(head) if head is not None else front_matter(meta, False) + profile_md(meta) + cocite_md(meta) + draws_md(meta) + coupling_md(meta) + parens_md(meta) + headmatter_md(meta) + editorial(cluster or {})
    notes = []
    for o in ops:
        ctx = o["ctx"]
        L += ["## %s — %s" % (ctx.who, o["kindname"]), ""]
        if o["byline_md"]:
            L += ["*%s*" % o["byline_md"], ""]

        def walk(part):
            blocks = list(part.blocks)
            if part.level:
                title = ""                       # RECAP: "I.   Background" -> "### I. Background"
                if blocks and blocks[0][1].attrs.get("heading") == "title":
                    title = ". " + esc(clean(blocks[0][1].text()), "md")
                    blocks = blocks[1:]
                L.extend(["#" * (2 + part.level) + " " + part.label + title, ""])
            for kind, el in blocks:
                marks = []
                t = clean(inline(el, "md", ctx, marks))
                L.extend([("> " if kind == "q" else "") + t, ""])
                for m in marks:
                    d = fn_md(o, ctx, m)
                    if d:
                        notes.append(d)
            for c in part.children:
                walk(c)
        walk(o["tree"])
    return "\n".join(L) + "\n" + tail + ("\n" if tail else "") + "\n".join(notes) + "\n"


def tail_sections(data, index, sketches=False):
    """'## Cites' (from the links in this case's text) and '## Cited by' (written
    by casemap.py citedby, if it has been run for this case). sketches (--cases): each cited case's sketch under it,
    as in the PDF's The Cases Cited."""
    L = []
    cited = cited_clusters(data)
    if cited:
        con = shared_db()
        from tribonian import casesummary, scotus
        named = scotus.case_names(con, [k for k in cited if str(k) not in index]) if con is not None else {}
        L += ["## Cites", ""]
        def key(kv):
            e = index.get(str(kv[0]), {})
            return (-kv[1], e.get("cite") or "~")
        for cid, n in sorted(cited.items(), key=key):
            e = index.get(str(cid))
            times = " ×%d" % n if n > 1 else ""
            if e:
                L.append("- [[%s|%s, %s (%s)]]%s" % (e["slug"], esc(e["short"] or e["name"], "md"), e["cite"], e["year"], times))
            elif named.get(cid) and not named[cid]["name"].startswith("cluster "):
                v = named[cid]
                L.append("- [%s](%s)%s%s" % (esc(v["name"], "md"), opinion_url(cid),
                                            ", " + ", ".join(x for x in (v["cite"], " ".join(x for x in (v["court"], v["year"]) if x)) if x)
                                            if v["cite"] or v["year"] else "", times))
            else:
                L.append("- [cluster %d](%s)%s" % (cid, opinion_url(cid), times))
            if sketches:
                L += casesummary.sketch_md(con, cid, lambda t: esc(t, "md"))
        L.append("")
    elif not data.get("opinions"):
        L += ["## Cites", "", "*Unknown until the text is fetched.*", ""]
    stat = {}
    for o in usable_opinions(data):
        for t in re.findall(r'<span class="citation[^"]*">([^<]*U\.\s?S\.\s?C\.[^<]*)</span>', o.get("html_with_citations") or ""):
            if statute_note(t):
                stat[statute_note(t)] = stat.get(statute_note(t), 0) + 1
    if stat:
        have = statute_notes()
        L += ["## Statutes cited", ""]
        for name, n in sorted(stat.items(), key=lambda kv: (-kv[1], kv[0])):
            t, sec = name.split("-usc-")
            label = "%s U.S.C. § %s" % (t, sec)
            L.append("- %s%s" % ("[[%s|%s]]" % (name, label) if name in have else label + " (no note: not in the current Code)",
                                 " ×%d" % n if n > 1 else ""))
        L.append("")
    frag = CASES / "citedby" / str(data["cluster"].get("id")) / "section.md"
    if frag.exists():
        L += [frag.read_text(encoding="utf-8").rstrip(), ""]
    return "\n".join(L)


def write_note(cid, index):
    """(Re)write md/<slug>.md for a case in the index: full text when we have it
    and can parse it, else a stub. Returns 'full', 'stub' or 'unparsed'."""
    data, is_full = load_case(cid, index)
    ent = index[str(cid)]
    tail = tail_sections(data, index)
    kind = "stub"
    if is_full:
        try:
            meta, ops, _ = prepare(data, index)
            meta["profile"] = profile_data(cid)          # counts only; build adds the good-law check
            body, kind = render_md(meta, ops, tail, data["cluster"]), "full"
        except Unsupported as e:
            meta = case_meta(data)
            meta["profile"] = profile_data(cid)
            body, kind = render_stub(meta, tail, "text fetched, but %s" % e, data["cluster"]), "unparsed"
    else:
        meta = case_meta(data)
        meta["profile"] = profile_data(cid)
        body = render_stub(meta, tail, cluster=data["cluster"])
    (note_dir(ent["slug"], data.get("docket")) / (ent["slug"] + ".md")).write_text(body, encoding="utf-8")
    return kind


# ---------------------------------------------------------------- LaTeX

def fn_tex(o, ctx, label):
    fn = o["footnotes"].get(label)
    if fn is None:
        return ""
    ctx.record = False                       # a *page marker inside a footnote must not move the header
    paras = []
    for k in fn.kids:
        if isinstance(k, El) and k.tag in ("p", "blockquote"):
            t = clean(inline(k, "tex", ctx, []))
            if t:
                paras.append((k.tag == "blockquote", t))
    ctx.record = True
    # A quoted paragraph is indented by a grouped \leftskip. When it is the LAST
    # thing in the note the group is dropped instead (the footnote is its own
    # group): closing with \par} would leave an empty line for the final strut.
    paras = [("{\\leftskip=1.2em\\rightskip=1em\\relax %s\\par}" % t if i < len(paras) - 1
              else "\\leftskip=1.2em\\rightskip=1em\\relax %s" % t) if q else
             (t + "\\par" if i < len(paras) - 1 else t)
             for i, (q, t) in enumerate(paras)]
    return "\\begingroup\\renewcommand{\\thefootnote}{%s}\\footnotetext{%s}\\endgroup\n" % (
        esc(label, "tex"), " ".join(paras))


def box_opts(rec):
    """Same frame arithmetic as usc2md.render_tex: a box at depth d draws the
    side lines of its d enclosing groups, plus the rule that opens/closes one."""
    d = rec["depth"]
    opts = ["left=%d\\lvlstep+4pt" % d, "right=%d\\lvlstep+4pt" % d]
    opts += ["borderline west={0.5pt}{%d\\lvlstep}{black}, borderline east={0.5pt}{%d\\lvlstep}{black}" % (k, k)
             for k in range(d)]
    north = "\\hrulestep{%d}{north}" % (d - 1) if rec["opens"] else ""
    south = "".join("\\hrulestep{%d}{south}" % k for k in sorted(rec["closes"]))
    if north:
        opts += ["overlay first={%s}" % north, "top=4pt", "before skip=3pt"]   # clear the descenders of the unboxed line above
    if south:
        opts += ["overlay last={%s}" % south, "bottom=4pt"]
    if north or south:
        opts.append("overlay unbroken={%s%s}" % (north, south))
    return ", ".join(opts)


# Clickable contents page (scotus.py; meta["toc"]). The reMarkable follows internal GoTo links,
# so the contents page links to every stage, document and top heading, and each of those carries
# an arrow in the left margin linking back. The arrow is a tikz overlay pinned to the page's left
# edge, so it sits in the same place whether its heading is boxed or not (remember picture: needs
# the second pass, which every build runs anyway). Entries go through the .toc file, so a contents
# page that grows past its first-pass length needs a third pass (run_pdflatex checks).
TOC_DEFS = r"""\makeatletter
\hypersetup{linktoc=all}
\setcounter{tocdepth}{3}
\newcommand{\tocback}{\leavevmode\tikz[remember picture,overlay]\node[anchor=base west,inner sep=0pt]
  at ([xshift=3pt]current page.west |- 0,0) {\hyperlink{contents}{\makebox[18pt][c]{\normalsize$\uparrow$}}};}
% a contents line without hyperref's bookmark: \addcontentsline would also put every line in the PDF outline,
% beside the \pdfbookmark each heading writes (doubled, and at the wrong level). The outline comes from \pdfbookmark alone.
\newcommand{\tocadd}[2]{\phantomsection\protected@write\@auxout{}{\string\@writefile{toc}{\protect\contentsline{#1}{#2}{\thepage}{\@currentHref}\protected@file@percent}}}
\newcommand*\l@tocstage[2]{\par\addvspace{6pt}{\bfseries\@dottedtocline{0}{0pt}{0pt}{#1}{#2}}}
\newcommand*\l@tocsec{\@dottedtocline{1}{1em}{0pt}}
\newcommand*\l@tocdoc{\@dottedtocline{1}{1em}{0pt}}
\newcommand*\l@tochead{\@dottedtocline{2}{2.4em}{1.6em}}
\newcommand*\l@tocsub{\@dottedtocline{3}{3.6em}{1.6em}}
\newcommand{\contentspage}{\clearpage\contentsbody}
\newcommand{\contentsbody}{\noindent\hypertarget{contents}{}\pdfbookmark[0]{Contents}{contents}%
  {\Large\bfseries Contents\par}\vspace{2pt}
  {\scriptsize\itshape Tap an entry to go there. The \ensuremath{\uparrow} in the left margin beside each heading comes back here.\par}\vspace{4pt}
  {\small\@starttoc{toc}}\clearpage}
\makeatother
"""


def toc_entry(level, text):
    """\\tocadd for one contents line (level: tocstage | tocsec | tocdoc | tochead)."""
    return "\\tocadd{%s}{%s}" % (level, esc(text, "tex"))


def toc_head(part, el, n=10):
    """Contents text for a top heading: a word label ("Statement") stands alone; a numeral or
    letter label ("II") takes the heading's first n words (the outline keeps 10; the contents page takes 60,
    so a brief's argument heading reads whole there)."""
    words = plain(el, n) if el is not None else ""
    if re.fullmatch(r"[IVXLC]+|[A-Z]|\d+|[a-z]", part.label or "") and words:
        return "%s. %s" % (part.label, words)
    return part.label


# ---------------------------------------------------------------- legend of marks (first page, after the headmatter)
# Every mark the PDF uses, once, each drawn by its own macro: cite tints (citemarks.py), old US Code cites (usc_out),
# quote hatching (mark_quotes). Only the marks found in this PDF's body are listed; none, no legend.
LEGEND_SPOT = "%%MARK-LEGEND%%\n"
LEGEND_ROWS = [   # (macro that shows it is used, sample, explanation)
    ("\\citeok{", "\\citeok{463 U.S. 29}", "cite checked: the case is in CourtListener (found by citation, case name or "
     "docket number), or the law's text is stored here"),
    ("\\citebad{", "\\citebad{761 F. App'x 732}", "not found, or not fetched yet: a likely miscite, or law whose text "
     "is not here; listed with the reason in Cites not resolved"),
    ("\\citensrc{", "\\citensrc{2009 WL 773499}", "no source: it exists, but nothing here can read it (a "
     "LexisNexis-only code, a Westlaw-only decision); listed in Cites not resolved"),
    ("\\uscmark{", "\\uscmark{42 U.S.C. \\S\\ 1983}", "a US Code section changed since the decision, with a tag: "
     "amd amended, rd redesignated"),
    ("\\usclight{", "\\usclight{5 U.S.C. \\S\\ 552}", "its section was amended, but elsewhere than the part cited"),
    ("\\uscstrike{", "\\uscstrike{15 U.S.C. \\S\\ 1394}", "the number moved or is gone (mv moved, rep repealed, om "
     "omitted, nf not found), followed by the current number"),
    ("\\quoteA{", "\\usebox\\qlA\\kern-.3pt\\quoteA{2--9}\\kern-.3pt\\usebox\\qrA\\,"
     "\\usebox\\qlB\\kern-.3pt\\quoteB{10--49}\\kern-.3pt\\usebox\\qrB\\,"
     "\\usebox\\qlC\\kern-.3pt\\quoteC{50+}\\kern-.3pt\\usebox\\qrC",
     "what later opinions cite the case for: the sentence their parentheticals restate, darker with more of "
     "them; the \\(\\times\\)N at its end goes to its entry in Quote lineage"),
]


def legend_tex(body):
    """The 'Marks in this PDF' box for the marks body uses; '' when it uses none."""
    rows = [(sample, text) for used, sample, text in LEGEND_ROWS
            if used in body or (used == "\\quoteA{" and ("\\quoteB{" in body or "\\quoteC{" in body))]
    if not rows:
        return ""
    lines = "".join("\\item[{%s}] %s\n" % r for r in rows)
    return ("\\par\\vspace{8pt}\\noindent{\\bfseries Marks in this PDF}\\par\\vspace{2pt}\n"
            "{\\footnotesize\\begin{list}{}{\\leftmargin=11.5em\\labelwidth=11em\\labelsep=.5em\\itemsep=3pt"
            "\\parsep=0pt\\topsep=0pt\\renewcommand\\makelabel[1]{#1\\hfil}}\n%s\\end{list}}\\par\n" % lines)


def render_tex(meta, ops, fontpkg, style):
    from tribonian.render import PREAMBLE, RULES
    toc = meta.get("toc", True)              # contents page: cases, dockets, scotus.py (US Code has its own render_tex)
    _anchors.clear()                         # bookmark names are unique per PDF
    citemarks.reset()                        # this PDF's red and orange cites, for the closing list

    def sec(tex, title):                     # a front box: contents entry + back arrow on its first line
        i = tex.find("\\begin{tcolorbox}")
        j = tex.find("]\n", i)
        if not toc or i < 0 or j < 0:
            return tex
        tex = tex.replace("\\pdfbookmark[1]", "\\pdfbookmark[0]", 1)      # top of the outline, as on the contents page
        return tex[:j + 2] + "\\tocback" + toc_entry("tocsec", title) + tex[j + 2:]
    head = headmatter_tex(meta)
    if toc and head:
        head = toc_entry("tocsec", "Headmatter") + head.replace("\\pdfbookmark[1]", "\\pdfbookmark[0]", 1)   # the yellow box: listed, no arrow
    # the legend of marks sits after the headmatter on the first page; the contents always start a fresh page
    contents = "" if not toc or meta.get("pretex") else "\\contentspage\n"
    pretex = meta.get("pretex", "")
    if toc and "\\contentspage" in pretex:
        pretex = pretex.replace("\\contentspage", LEGEND_SPOT + "\\contentspage", 1)
    body = [TOC_DEFS if toc else "", pretex, head, LEGEND_SPOT if toc and not meta.get("pretex") else "", contents, sec(profile_tex(meta), "Case profile"),
            sec(cocite_tex(meta), "Co-citation"), sec(draws_tex(meta), "What it draws on"),
            sec(coupling_tex(meta), "Coupled with"), sec(parens_tex(meta), "How later courts describe it"),
            sec(editorial_tex(meta), "Reporter's summary and syllabus")]
    stage = None                             # scotus.py: documents grouped under top-level outline entries
    base = 1 if meta.get("stages") else 0    # outline level of a document: under its stage, else at the top
    from tribonian import citefind
    for n, o in enumerate(ops):
        citefind.DOC.reset(meta.get("cites") or (), meta.get("cluster"))   # Id./supra never reach back past it
        ctx = o["ctx"]
        ctx.page, ctx.part = o["start"] or meta["page"], ""
        parts = []                           # str = unboxed text, dict = one flat box

        def block(kind, el, lead=""):
            marks = []
            t = lead + clean(inline(el, "tex", ctx, marks))
            if kind == "q":
                t = "\\begin{quoted}%s\\end{quoted}" % t
            return t, "".join(fn_tex(o, ctx, m) for m in marks)

        def emit(part, depth, first, listed=False):
            """Returns the last box emitted for this part's subtree (or None).
            listed: this part's heading goes on the contents page."""
            last = None
            blocks = list(part.blocks) or [(None, None)]
            for i, (kind, el) in enumerate(blocks):
                lead = ""
                if i == 0 and part.level:
                    ctx.part = part.path
                    # PDF outline entry (the reMarkable's table of contents), as the contents page words it:
                    # "B. The Archdiocese of Denver… · *6"; nested by its place in the heading tree under its
                    # document (not by part.level: a brief whose "I." was not found has "A." two levels down,
                    # and hyperref drops an entry that skips a level)
                    title = toc_head(part, el) + (" · *" + ctx.page if ctx.page else "")
                    mark = "\\pdfbookmark[%d]{%s}{o%d-%s}" % (depth + 1 + base, esc(title, "tex"), ctx.oi, anchor(part.path))
                    lead = "\\recordpath{%s}" % ctx.path() + mark + "\\lab{%s.}" % esc(part.label, "tex")   # "I." not "I": a bare I reads as the pronoun
                    if toc and listed:
                        lead = "\\tocback" + toc_entry("tochead" if part.level == 1 else "tocsub", toc_head(part, el, 60)) + lead
                    if depth <= 0:               # unboxed top-level heading: not alone at a page's foot (boxed ones
                        lead = "\\headroom{5}" + lead      # already need 3 lines before tcolorbox breaks them)
                    if kind == "q" or kind is None:          # label on its own line
                        parts_add(depth, [lead + "\\par"], first and i == 0)
                        lead = ""
                        if kind is None:
                            break
                        first = False
                text, fns = block(kind, el, lead)
                r = parts_add(depth, [text + "\\par"], first and i == 0, fns)
                last = r or last
            group_last = None
            bare = toc and part.level == 1 and not re.fullmatch(r"[IVXLC]+|[A-Z]|\d+|[a-z]", part.label or "")   # "Argument": list its I., II. too
            for i, c in enumerate(part.children):
                r = emit(c, depth + 1, i == 0, bare)
                group_last = r or group_last
            if part.children and group_last is not None:
                group_last["closes"].append(depth)
                last = group_last
            return last

        def parts_add(depth, paras, opens, fns=""):
            if depth <= 0:
                parts.append("\\vspace{%s}%s\n" % ("3pt", "\n".join(paras)))
                rec = None
            else:
                rec = {"depth": depth, "paras": paras, "opens": opens, "closes": []}
                parts.append(rec)
            if fns:
                parts.append(fns)
            return rec

        fresh = False                            # this filing starts a page
        if o.get("stage") and o["stage"] != stage:
            stage, fresh = o["stage"], True
            name = (meta.get("stages") or {}).get(stage, stage)
            body.append("\\clearpage%s\\pdfbookmark[0]{%s}{stage%d}{\\Large\\bfseries %s\\par}\\vspace{8pt}\n" % (
                "\\noindent\\tocback" + toc_entry("tocstage", name) if toc else "", esc(name, "tex"), n, esc(name, "tex")))
        elif n and meta.get("doc_newpage"):
            fresh = True
            body.append("\\clearpage\n")         # scotus.py: every filing on its own page (one cover color per page)
        elif n:
            # new opinion: start a fresh page unless 8 lines still fit under its heading
            body.append("\\par\\ifdim\\dimexpr\\pagegoal-\\pagetotal\\relax<8\\baselineskip\\clearpage"
                        "\\else\\vspace{12pt}\\hrule\\vspace{6pt}\\fi\n")
        if toc:
            entry = toc_entry("tocdoc", "%s — %s" % (ctx.who, o["kindname"]))
            if o.get("cover"):                   # its cover color on the contents line (scotus.py)
                entry = entry.replace("\\tocadd{tocdoc}{", "\\tocadd{tocdoc}{\\protect\\coverswatch{%s}\\ " % o["cover"], 1)
            body.append("\\noindent\\tocback" + entry)
        if o.get("cover"):                       # scotus.py: the page band takes this filing's cover color from here
            body.append("\\recordcover{%s}" % o["cover"])
        # a filing that starts a page names that page's header, not the end of the filing before
        body.append(("\\recordstart{%s}" if fresh else "\\recordpath{%s}") % ctx.path() + "\\pdfbookmark[%d]{%s}{o%d}{\\large\\bfseries %s}\\ {\\large %s}\\par\n" % (
            base, esc("%s — %s · *%s" % (ctx.who, o["kindname"], ctx.page), "tex"), ctx.oi,
            esc(ctx.who, "tex"), esc(o["kindname"], "tex")))
        if o["byline_tex"]:
            body.append("{\\small\\itshape %s\\par}\n" % o["byline_tex"])
        if o.get("headbox_tex"):                 # scotus.py: the filing's header in its cover color
            body.append("\\vspace{3pt}" + o["headbox_tex"])
        body.append("\\vspace{2pt}\n")

        top = o["tree"]
        for kind, el in top.blocks:          # text before Part I
            text, fns = block(kind, el)
            parts_add(0, [text + "\\par"], False, fns)
        for c in top.children:               # Parts sit at depth 0, like subsections
            parts.append("\\vspace{5pt}\n")
            emit(c, 0, False, True)
        for p in parts:
            body.append("\\begin{lvl}[%s]\n%s\n\\end{lvl}\n" % (box_opts(p), "\n".join(p["paras"]))
                        if isinstance(p, dict) else p)

    src = " \\textbullet\\ ".join(esc(s, "tex") for s in meta["srcline"] if s)
    pre = (PREAMBLE.replace("__FONTPKG__", fontpkg).replace("__RULES__", RULES[style])
           .replace("__DOCTITLE__", esc(meta["title"], "tex"))
           .replace("__DOCHEAD__", esc(meta["name"], "tex"))
           .replace("__SRCLINE__", src))
    foot = "\\renewcommand{\\docfoot}{%s}\n" % esc(meta["foot"], "tex") if meta.get("foot") else ""
    tex = pre + foot + "".join(body) + meta.get("posttex", "") + citemarks.section_tex(lambda t: esc(t, "tex")) + "\n\\end{document}\n"
    body_tex = tex[tex.find("\n\\begin{document}"):]       # the document itself: the preamble defines every macro
    return tex.replace(LEGEND_SPOT, legend_tex(body_tex), 1).replace(LEGEND_SPOT, "")


# ---------------------------------------------------------------- driver

def case_meta(data):
    c, d = data["cluster"], data.get("docket") or {}
    cites = c.get("citations") or []
    main = next((x for x in cites if x.get("reporter") == "U.S."), cites[0] if cites else {})
    cite = cite_str(main) if main else "cluster %s" % c.get("id")
    name = c.get("case_name") or c.get("case_name_full") or "Case"
    short = c.get("case_name_short") or name
    year = (c.get("date_filed") or "")[:4]
    argued = clean((c.get("other_dates") or "").replace("—", " ")).rstrip(" .;,")
    return {
        "name": name, "short": short, "cite": cite, "year": year,
        "reporter": "%s %s" % (main.get("volume", ""), main.get("reporter", "")), "page": str(main.get("page", "")),
        "title": "%s, %s (%s)" % (short, cite, year),
        "aliases": list(dict.fromkeys([short, cite, "%s (%s)" % (short, year), name])),
        "cites": [cite_str(x) for x in cites], "court": d.get("court_id") or "",
        "docket": d.get("docket_number") or "", "decided": c.get("date_filed") or "", "argued": argued,
        "judges": c.get("judges") or "", "cluster": c.get("id"), "cited_by": c.get("citation_count"), "url": CL + (c.get("absolute_url") or ""),
        "headmatter": c.get("headmatter") or "",
        "srcline": ["No. " + d["docket_number"] if d.get("docket_number") else "", argued,
                    "Decided " + (c.get("date_filed") or ""),
                    "CourtListener cluster %s (Harvard CAP text)" % c.get("id"),
                    "retrieved " + date.today().isoformat()],
        "slug": "%s-%s" % (slugify(cite.replace(".", "")), slugify(short, 30)),
    }


def prepare(data, index=None):
    """Fetched JSON -> (meta, opinions ready to render, warnings)."""
    warnings = []
    meta = case_meta(data)
    if index and str(meta["cluster"]) in index:
        meta["slug"] = index[str(meta["cluster"])]["slug"]
    links = {int(k): e["slug"] for k, e in (index or {}).items()}
    statutes = statute_notes()

    def harvard(o):
        return "<opinion" in (o.get("html_with_citations") or o.get("xml_harvard") or "")
    use = [o for o in data["opinions"] if harvard(o)]
    if not use:
        slip = [o for o in data["opinions"] if (o.get("plain_text") or o.get("html_with_citations") or "").strip()]
        if not slip:
            raise Unsupported("it has no opinion text.")
        return prepare_slip(meta, slip, links, statutes)
    reference = []
    for o in data["opinions"]:
        if not harvard(o):
            reference += [clean(h) for h in re.findall(r"<h2[^>]*>(.*?)</h2>", o.get("html_with_citations") or "", re.S)]
    reference = [re.sub(r"<[^>]+>", "", h) for h in reference if clean(h).upper() != "NOTES"]

    ops = [read_opinion(o, i) for i, o in enumerate(use)]
    heads = [b for o in ops for b in o["flat"] if b[0] == "h"]
    raw = [b[1] for b in heads]
    fixed = repair_headings(raw, reference, warnings.append)
    for b, new in zip(heads, fixed):
        if b[1] != new:
            warnings.append("heading %r read as %r" % (b[1], new))
        b[1] = new
    for o in ops:
        labels = [b[1] for b in o["flat"] if b[0] == "h"]
        o["tree"] = tree_of(o, assign_levels(labels))
        o["ctx"] = Ctx(meta["reporter"], short_author(o["author"], o["api"].get("author_str")), o["oi"])
        o["ctx"].links, o["ctx"].statutes = links, statutes
        o["ctx"].year = int((meta.get("decided") or "0")[:4] or 0)
        o["kindname"] = TYPE_NAMES.get(o["kind"], o["kind"].replace("-", " ") or "opinion")
        o["ctx"].record = False
        o["byline_md"] = clean(" ".join([esc(o["author"], "md")] + [inline(b, "md", o["ctx"], []) for b in o["byline"]]))
        o["byline_tex"] = clean(" ".join([esc(o["author"], "tex")] + [inline(b, "tex", o["ctx"], []) for b in o["byline"]]))
        o["ctx"].record = True
        used = {m.text().strip() for b in o["flat"] if b[0] != "h" for m in iter_tag(b[1], "footnotemark")}
        for lab in o["footnotes"]:
            if lab not in used:
                warnings.append("%s: footnote %s has no mark in the text" % (o["ctx"].who, lab))
    return meta, ops, warnings


def build(args):
    here = ROOT
    data = json.loads(Path(args.json).read_text(encoding="utf-8"))
    with index_lock():
        index = load_index()
        register(data, index, args.json)
        save_index(index)
    from tribonian import lawappendix, statecites
    # forms that do not name their state ("Penal Code § 187") are read as the court's state's
    statecites.CONTEXT["states"] = statecites.court_states((data.get("docket") or {}).get("court_id"))
    try:
        meta, ops, warnings = prepare(data, index)
    except Unsupported as e:
        sys.exit("Cannot build: %s" % e)
    # the law the opinions cite: every provision once at the end of the PDF, each cite linking to it
    for o in ops:
        for b in o["flat"]:
            mark_law(b[1])
        for fn in o["footnotes"].values():
            mark_law(fn)
    law = list(dict.fromkeys(lawappendix.keys_in([o["flat"] for o in ops] + [o["footnotes"] for o in ops])))
    printed = law if getattr(args, "law", True) else []          # --no-law: no appendix, nothing fetched
    missing = lawappendix.fetch_missing(printed, federal=getattr(args, "fetch", False),
                                        state=getattr(args, "fetch_state_law", False), warnings=warnings)
    targets = {lawcites_section(k): lawappendix.anchor(k) for k in printed}
    for o in ops:
        o["ctx"].lawtargets = targets
    meta["law"] = law
    meta["posttex"] = lawappendix.appendix_tex(
        printed, intro="Not part of the opinion. Every statute, constitutional provision and regulation the opinions cite, "
        "with its text where it is stored locally: today's text, not the law as of %s; a cite of one in the opinion "
        "links here." % (meta.get("year") or "the decision"), editions=law_editions(ops))
    if getattr(args, "cases", False):              # --cases: The Cases Cited before The Law Cited
        from tribonian import casesummary
        meta["posttex"] = casesummary.section_tex(shared_db(), casesummary.cited(shared_db(), ops), "opinion") + meta["posttex"]
    qd = None                                           # quote marks and Quote lineage (quotelineage.py)
    if QUOTE_MARKS and meta.get("cluster"):
        from tribonian import quotelineage
        qd = quotelineage.data(shared_db(), data, index, QUOTED_MIN, meta["cluster"])
    meta["editorial"] = data.get("cluster") or {}       # the reporter's summary and syllabus, as in the note
    frag = CASES / "citedby" / str(meta.get("cluster")) / "section.md"   # map citedby's Cited by, as in the note
    if frag.exists():
        from tribonian import mdtex
        meta["posttex"] = mdtex.part_tex(frag.read_text(encoding="utf-8"), "Cited by", "citedby") + meta["posttex"]
    if qd:
        for o in ops:
            o["ctx"].quotes = [(e["n"], e["compact"], e["k"]) for e in qd["entries"]]
        meta["posttex"] = quotelineage.section_tex(qd, bulk_stamp(shared_db())) + meta["posttex"]
    # --no-goodlaw (the GUI's Quick build): everything but the slow good-law check
    quick = getattr(args, "quick_goodlaw", None)
    meta["profile"] = profile_data(meta["cluster"], scan=not (getattr(args, "no_goodlaw", False) or quick), quick=quick)
    # bibliographic coupling: what it draws on (always, cheap); coupled cases only if cached or --coupling
    meta["draws"] = draws_data(meta["cluster"], data)
    meta["coupling"] = coupling_data(meta["cluster"], compute=getattr(args, "coupling", True))

    (here / "build").mkdir(exist_ok=True)
    ndir = note_dir(meta["slug"], data.get("docket"))
    md_path = ndir / (meta["slug"] + ".md")
    tail = tail_sections(data, index, sketches=getattr(args, "cases", False))
    law_md = "\n".join(lawappendix.md_lines(law, statute_notes()))
    if qd:
        from tribonian import quotelineage
        tail = "\n".join(quotelineage.section_md(qd)) + "\n" + tail
    tex = render_tex(meta, ops, args.font, args.style)     # first: it registers the cites not resolved
    tail += "\n".join(citemarks.section_md(lambda t: esc(t, "md")))
    md_path.write_text(render_md(meta, ops, (law_md + "\n" + tail) if law_md else tail, data["cluster"]), encoding="utf-8")
    tex_path = here / "build" / (meta["slug"] + ".tex")
    tex_path.write_text(tex, encoding="utf-8")
    print("wrote %s\nwrote build/%s" % (md_path.relative_to(here), tex_path.name))

    if not args.no_pdf:
        pdf = run_pdflatex(tex_path, pdf_dir(ndir))
        if pdf:
            print("wrote %s" % pdf.relative_to(here))
    unmapped_note()
    for o in ops:
        n = sum(1 for b in o["flat"] if b[0] != "h")
        print("  %-22s %-45s %3d paragraphs, %2d footnotes, starts at *%s" % (
            o["ctx"].who, o["kindname"][:45], n, len(o["footnotes"]), o["start"] or "?"))
    for w in warnings:
        print("  note:", w)
    for k, hint in missing:
        print("  law not local: %s%s" % (lawcites_label(k), " (%s)" % hint if hint else ""))


def lawcites_section(k):
    from tribonian import lawcites
    return lawcites.section_of(k)


def lawcites_label(k):
    from tribonian import lawcites
    return lawcites.label(k)


def law_editions(ops):
    """{key: (publisher, year)} for cites followed by an edition parenthetical ("(West 2019)"), from the opinions' text."""
    from tribonian import lawcites
    out = {}
    for o in ops:
        for _, val in o["flat"]:
            t = val.text() if isinstance(val, El) else str(val)
            for k, ed in lawcites.find(t, flat=False)["editions"].items():
                out.setdefault(k, ed)
    return out


# ================================================================ folders
# Where a note goes. A note that already exists anywhere under md-cases/ is rewritten where it
# is: moving it (inside md-cases/, not in the vault, which tribonian sync-vault overwrites) is how you
# override everything below. A RECAP document goes where its docket's note is. Otherwise the
# first rule whose pattern matches that field of the docket wins; edit the table to change the
# defaults. No match: md-cases/ itself.
FOLDERS = [
    ("nature_of_suit", r"freedom of information|privacy act", "FOIA"),
    ("cause", r"\b0?5:552a?\b", "FOIA"),
]
_NOTE_DIRS = None


def note_dir(slug, docket=None, near=None):
    """-> the folder md-cases/<...>/ that the note <slug>.md belongs in (created)."""
    global _NOTE_DIRS
    if _NOTE_DIRS is None:
        _NOTE_DIRS = {p.stem: p.parent for p in sorted(NOTES.rglob("*.md"))} if NOTES.is_dir() else {}
    where = _NOTE_DIRS.get(slug) or (near and _NOTE_DIRS.get(near))
    if not where:
        where = NOTES
        for field, pat, folder in FOLDERS:
            if re.search(pat, (docket or {}).get(field) or "", re.I):
                where = NOTES / folder
                break
    where.mkdir(parents=True, exist_ok=True)
    _NOTE_DIRS[slug] = where
    return where


def pdf_dir(ndir):
    """pdf/ mirrors the note folders: md-cases/FOIA/x.md -> pdf/FOIA/x.pdf."""
    d = ROOT / "pdf" / ndir.relative_to(NOTES)
    d.mkdir(parents=True, exist_ok=True)
    return d


def unmapped_note():
    """Say which characters the PDF could not print (render.tex_safe dropped or marked them)."""
    from tribonian import render
    for ch, n in sorted(render.UNMAPPED.items()):
        o = ord(ch)
        print("  note: %s(U+%04X) x%d %s in the PDF" % (ch + " " if ch.isprintable() else "", o, n,
              "dropped (control character)" if o < 0x20 or 0x7F <= o < 0xA0 else
              "dropped (scan speck)" if 0x2580 <= o <= 0x25FF else "shown as a boxed ?"))
    render.UNMAPPED.clear()


def run_pdflatex(tex_path, out_dir):
    """Two passes (tcolorbox places split boxes on the second). -> the PDF's path, or None."""
    stem = tex_path.stem
    # SOURCE_DATE_EPOCH: pdflatex then stamps dates and the trailer ID from it, so the same
    # source gives the same bytes and sync_remarkable.py does not re-upload (and lose annotations)
    env = dict(os.environ, SOURCE_DATE_EPOCH=os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with tempfile.TemporaryDirectory() as work:
        toc, prev = Path(work) / (stem + ".toc"), None
        for n in range(4):
            try:
                r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                                    "-output-directory", work, str(tex_path)], capture_output=True, text=True,
                                   errors="replace", env=env, timeout=PDFLATEX_TIMEOUT)
            except FileNotFoundError:
                print("FAIL pdflatex not installed")
                return None
            except subprocess.TimeoutExpired:
                print("FAIL pdflatex ran over %d s on %s (a TeX loop?)" % (PDFLATEX_TIMEOUT, stem))
                return None
            if r.returncode or not toc.exists():
                if n >= 1:
                    break
                continue
            now = toc.read_text(errors="replace")
            if n >= 1 and now == prev:           # contents page numbers settled
                break
            prev = now
        pdf = Path(work) / (stem + ".pdf")
        if r.returncode or not pdf.exists():
            shutil.copy(Path(work) / (stem + ".log"), tex_path.with_suffix(".log"))
            print("FAIL pdflatex (see build/%s.log)\n%s" % (stem, "\n".join(
                l for l in r.stdout.splitlines() if l.startswith("!"))[:600]))
            return None
        shutil.copy(pdf, out_dir / pdf.name)
        return out_dir / pdf.name


# ================================================================ RECAP (PACER filings)
# A district court's orders are in CourtListener only as RECAP documents: the PDFs filed on
# PACER, with the text pdftotext pulled out of them (plain_text). There are no Harvard tags, so
# the structure is read back from the layout: page headers and numbers, double- versus single-
# spacing, indents, short last lines, headings standing alone, footnote numbers on a line of
# their own at the foot of the page.

RECAP = CASES / "recap"
STORAGE = "https://storage.courtlistener.com/"
DOCKET_FIELDS = ("id", "court_id", "case_name", "case_name_short", "case_name_full", "docket_number",
                 "nature_of_suit", "cause", "date_filed", "date_terminated", "assigned_to_str",
                 "referred_to_str", "jurisdiction_type", "pacer_case_id", "absolute_url")
KEEP = ("id", "docket_entry_id", "document_number", "entry_number", "attachment_number", "entry_date_filed",
        "description", "short_description", "page_count", "cites", "is_available", "document_type",
        "filepath_local", "absolute_url", "pacer_doc_id")
# The court's own writing, by the start of the clerk's docket text. Everything else (motions,
# briefs, exhibits) is left out unless named with --docs or --all.
ORDER_RE = re.compile(r"^\s*(?:COURT\s+)?(ORDER|OPINION|MEMORANDUM|JUDGMENT|FINAL JUDGMENT|REPORT AND RECOMMENDATION|"
                      r"FINDINGS|RULING|DECISION|PER CURIAM)", re.I)


def docket_slug(d):
    num = re.sub(r"^\d+:", "", d.get("docket_number") or str(d.get("id")))
    return "%s-%s-%s" % (d.get("court_id") or "court", slugify(num), slugify(d.get("case_name_short") or d.get("case_name") or "case", 30))


def doc_slug(dslug, r):
    att = r.get("attachment_number")
    return "%s-ecf-%s%s" % (dslug, r.get("document_number"), "-%s" % att if att else "")


def recap_files():
    """Every fetched docket: cases/recap/<court>/<docket>.json (and any still at cases/recap/ itself, from before
    the court folders: tribonian dockets index moves those). Not _index.json, not the pdf/ downloads."""
    if not RECAP.is_dir():
        return []
    return sorted(p for p in list(RECAP.glob("*.json")) + list(RECAP.glob("*/*.json"))
                  if not p.name.startswith("_") and p.parent.name != "pdf")


def recap_path(d):
    """Where a docket's JSON goes: cases/recap/<court id>/<slug>.json."""
    return RECAP / (d.get("court_id") or "court") / (docket_slug(d) + ".json")


def recap_file(did):
    """The saved JSON for a docket id, if fetched before."""
    for p in recap_files():
        try:
            if json.loads(p.read_text(encoding="utf-8"))["docket"]["id"] == did:
                return p
        except (ValueError, KeyError, TypeError):
            pass
    return None


def doc_numbers(spec):
    """'35,55,66' or '35 55-1' -> {'35', '55', '66'} / {'35', '55-1'}."""
    return {x for x in re.split(r"[,\s]+", spec or "") if x}


def doc_key(r):
    att = r.get("attachment_number")
    return "%s%s" % (r.get("document_number"), "-%s" % att if att else "")


# The party list after "by Plaintiffs"/"by Defendants" fills the listing's 90 columns, and cuts off what tells
# four affidavits of service apart ("regarding Summons served on Thomas Dart").
_PARTY_LIST = re.compile(r"\b(by (?:All )?(?:Plaintiffs?|Defendants?))\s+[^;()]+?(?=\s+(?:regarding|in support|in opposition|to|for|re|pursuant)\b)")


def listing_text(r):
    """One line for the fetch listing: an attachment by its own name, an entry without its party list."""
    if r.get("attachment_number"):
        return "attachment: %s" % clean(r.get("short_description") or "") or "attachment"
    return _PARTY_LIST.sub(r"\1", clean(r.get("description") or ""))


def pdf_text(r):
    """No plain_text from the API: download the filed PDF (public storage, not an API request)
    and run pdftotext -layout on it, which is what CourtListener does."""
    if not r.get("filepath_local"):
        return "", "no text and no PDF in RECAP"
    out = RECAP / "pdf" / Path(r["filepath_local"]).name
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists() and not tconfig.online():
        return "", "offline mode: the filed PDF isn't downloaded"
    if not out.exists():
        try:
            req = urllib.request.Request(STORAGE + r["filepath_local"], headers={"User-Agent": "case2md/0.2"})
            with urllib.request.urlopen(req, timeout=120) as f:
                out.write_bytes(f.read())
        except (urllib.error.URLError, OSError) as e:
            return "", "PDF download failed (%s)" % e
    t = subprocess.run(["pdftotext", "-layout", str(out), "-"], capture_output=True, text=True, timeout=600).stdout
    return t, "pdftotext -layout of the filed PDF"


def find_docket(court, number, tok):
    """A court's case number, as the court writes it ("26-2230", "1:26-cv-00376") -> its CourtListener docket id.
    One API request: the local dockets table has no index on the number (a full scan), and the monthly bulk
    files lag the live dockets this is for."""
    q = urllib.parse.urlencode({"court": court, "docket_number": number, "fields": "id,case_name,docket_number,date_filed"})
    hits = (call("%s/dockets/?%s" % (API, q), tok) or {}).get("results") or []
    if not hits:
        sys.exit("No docket %s in court %s on CourtListener. Check the number as the court writes it, or pass "
                 "--docket with the CourtListener docket id." % (number, court))
    if len(hits) > 1:
        print("More than one docket %s in %s; pass --docket with one of these:" % (number, court))
        for h in hits:
            print("  %-10s %s  %s  filed %s" % (h["id"], h.get("docket_number"), h.get("case_name"), h.get("date_filed")))
        sys.exit(1)
    return hits[0]["id"]


def recap_fetch(args):
    tok = token()
    if args.docket:
        m = re.search(r"(\d+)", args.docket)
        if not m:
            sys.exit("--docket takes a CourtListener docket id or URL, e.g. 4949363")
        did = int(m.group(1))
    elif args.court and args.number:
        did = find_docket(args.court.lower(), args.number.strip(), tok)
    else:
        sys.exit("Name the docket: --docket ID (CourtListener), or --court ID --number N (e.g. --court ca4 --number 26-2230)")
    old = recap_file(did)
    data = json.loads(old.read_text(encoding="utf-8")) if old else {"docket": None, "documents": []}
    if not data.get("docket"):
        data["docket"] = local_docket(did) or call("%s/dockets/%d/?fields=%s" % (API, did, ",".join(DOCKET_FIELDS)), tok)
    d = data["docket"]
    out = old or recap_path(d)
    out.parent.mkdir(parents=True, exist_ok=True)

    def save():
        out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    # every document RECAP holds for this docket (search API, 20 a page)
    have = {r["id"]: r for r in data["documents"]}
    url = "%s/search/?%s" % (API, urllib.parse.urlencode({"type": "rd", "q": "docket_id:%d" % did,
                                                           "order_by": "entry_date_filed asc"}))
    while url:
        page = call(url, tok)
        for r in page.get("results") or []:
            have.setdefault(r["id"], {}).update({k: r.get(k) for k in KEEP})
        url = page.get("next")
    data["documents"] = sorted(have.values(), key=lambda r: (int(r.get("entry_number") or r.get("document_number") or 0),
                                                             int(r.get("attachment_number") or 0)))
    save()

    want = doc_numbers(args.docs)
    def wanted(r):
        if want:
            return doc_key(r) in want
        if args.all:
            return True
        return not r.get("attachment_number") and bool(ORDER_RE.match(r.get("description") or r.get("short_description") or ""))

    print("%s, No. %s (%s)  docket %d  nature of suit: %s" % (d.get("case_name"), d.get("docket_number"),
                                                              d.get("court_id"), did, d.get("nature_of_suit") or "-"))
    for r in data["documents"]:
        mark = " "
        if wanted(r):
            if not r.get("is_available"):
                mark = "-"                          # listed, but nobody has bought it from PACER yet
            elif not r.get("plain_text"):
                t = call("%s/recap-documents/%d/?fields=plain_text,ocr_status" % (API, r["id"]), tok, soft=(403, 404))
                text, src = (t or {}).get("plain_text") or "", "CourtListener plain_text"
                if not text.strip():
                    text, src = pdf_text(r)
                r["plain_text"], r["text_source"] = text, src
                save()                              # after each: a stop at the daily limit loses nothing
                mark = "*" if text.strip() else "!"
            else:
                mark = "*"
        print("  %s %-6s %s %3s pp  %s" % (mark, doc_key(r) if r.get("document_number") is not None else "-", r.get("entry_date_filed") or "          ",
                                           r.get("page_count") or "?", listing_text(r)[:90]))
    print("  (* text saved, - not in RECAP, ! no text; others not selected: --docs N,N or --all)")
    print("saved %s" % out.relative_to(ROOT))
    from tribonian import dockets
    dockets.index(quiet=True)                   # the GUI's View Dockets reads it
    print("next:  tribonian case recap build %s" % out.relative_to(ROOT))


# ---------------------------------------------------------------- layout text -> blocks

SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "under",
         "v", "vs", "with"}
# the court's filing stamp atop each page: "Case 2:09-cv-06732 Document 30 ... Page 3 of 7" (district),
# "USCA4 Appeal: 26-2230  Doc: 30  Filed: 10/07/2026  Pg: 3 of 29" (Fourth Circuit)
HEADER_RE = re.compile(r"^\s*(?:Case:?\s+\S+.*?\bDoc(?:ument)?\.?\s*(?:#\s*)?:?\s*\d+.*?\bPage"
                       r"|USCA\d+\s+Appeal:\s*\S+.*?\bDoc:\s*\d+.*?\bPg):?\s*(\d+)\s*of\s*\d+", re.I)
PAGENUM_RE = re.compile(r"^\s*(?:-\s*)?\d{1,3}(?:\s*-)?\s*$")
FNLABEL_RE = re.compile(r"^\s*(\d{1,3})\s*$")
NUMHEAD_RE = re.compile(r"^([IVX]{1,6}|[A-Z]|\d{1,2})\.\s+(\S.{0,90})$")
# a table of contents / authorities line: dot leaders, then the page(s) at the right margin
LEADER_RE = re.compile(r"\s*\.(?:\s?\.){3,}\s*((?:\d+|[ivxlc]+|passim)(?:\s*(?:,|&|–|-|and)\s*(?:\d+|passim))*)\s*$", re.I)
ENUM_RE = re.compile(r"^\((?:\d{1,2}|[a-z]|[ivx]{1,4})\)\s")
STATUTE_RE = re.compile(r"\d+\s*U\.\s?S\.\s?C\.?\s*(?:A\.\s*)?§+\s*\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?")
# a footnote number glued to the end of a word or punctuation: "FOIA1", "(MMS),1", "SOPs.3" -- not dot leaders ("....2")
FNMARK = r"(?<!\.\.)(?<!\d[.,])(?<=[A-Za-z)\].,;:”\"’'!?])%s(?=[\s,;:.)\]”\"’]|$)"
# the same number set off by a space after punctuation, not the start of a citation ("7(E), 5 U.S.C.")
# nor a reporter volume ("Madison, 1 Cranch 137", "F. Supp. 1 (D.D.C. 1991)": a word then a page, or a parenthetical);
# a citation signal after it is not a reporter ("IDEA. 3 See 20 U.S.C. …": the mark, then the cite)
FNMARK_SPACED = r"(?<=[.,;:)\]”\"’] )(?<!\.\. )(?<!\. \. )(?<!No\. )(?<!Supp\. )%s(?=[\s,;:.)\]”\"’]|$)(?!,\s*\d)" \
    r"(?!\s*(?:U\.|F\.|S\.|Stat\.|C\.F\.R|Fed\.))(?!\s+(?!(?:See|Cf|Accord|Compare|But|E\.g)\b)[A-Z][A-Za-z.’']*(?:\s+[A-Za-z.’']+){0,2}\s+\d)(?!\s*\()"

# last resort, after both of those miss on the footnote's page: a mark set off by spaces mid-sentence, between two
# words ("Members of Congress 2 who"). Not after a word a number belongs to ("Title 2 of", "the 2 cases"), nor
# before one ("2 million", "2 years"): fn_mid_ok
FNMARK_MID = r"(?<=[A-Za-z] )%s(?= [a-z]|[—–)])"               # "Medicine 3—was", "partners 2) with" too
MID_BEFORE = set("""a an the of to at in on by for from with and or nor but than over under about around nearly
    almost only just all some each every top first last next title titles section sections sec secs part parts chapter
    chapters ch art article articles amend amendment clause cl rule rules step steps count counts page pages p pp
    paragraph para no number vol volume footnote fn n note notes item items class group phase level tier stage table
    fig figure exhibit ex app appendix id were was is are be been had has have nearly roughly approximately
    least most exactly some""".split())
MID_AFTER = set("""million billion thousand hundred percent times years year months month weeks week days day hours
    hour minutes cases members states votes of to and or other more less fewer additional""".split())


def fn_mid_ok(text, m):
    """A mid-sentence spaced candidate is a footnote mark, not a number in the prose."""
    before = re.findall(r"[A-Za-z]+", text[:m.start()])
    after = re.match(r"\s*([A-Za-z]+)", text[m.end():])
    return bool(before) and before[-1].lower() not in MID_BEFORE and not (after and after.group(1).lower() in MID_AFTER)


class Ln:
    __slots__ = ("pi", "ind", "text", "end", "gap")

    def __init__(self, pi, raw, gap):
        self.pi, self.ind, self.end, self.gap = pi, len(raw) - len(raw.lstrip()), len(raw), gap
        self.text = re.sub(r"\s{2,}", " ", raw.strip())


def is_caps(t):
    letters = [ch for ch in t if ch.isalpha()]
    return len(letters) >= 4 and sum(ch.isupper() for ch in letters) >= 0.9 * len(letters)


def titlecase(t):
    if not is_caps(t):
        return t
    ws = t.split()
    return " ".join(w.capitalize() if i == 0 or w.lower() not in SMALL else w.lower() for i, w in enumerate(ws))


def headingish(t):
    """Short, capitalized like a title, not a sentence."""
    if len(t) > 90 or LEADER_RE.search(t) or t.endswith((",", ";", ":")) or (t.endswith(".") and not is_caps(t)):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]*", t)
    if words and words[-1].islower() and words[-1] in SMALL:     # "... February 25, 2009, the"
        return False
    big = [w for w in words if w.lower() not in SMALL]
    return 0 < len(words) <= 12 and bool(big) and sum(w[0].isupper() for w in big) >= 0.75 * len(big)


# Words seen in the document being read (layout_pages fills it): a line-end hyphen is a soft one
# ("re-" + "striction") unless the hyphenated word also occurs mid-line ("case-by-case").
_VOCAB = {"plain": set(), "hyphen": set()}


def set_vocab(text):
    _VOCAB["plain"] = set(re.findall(r"[a-z]+", text.lower()))
    _VOCAB["hyphen"] = set(re.findall(r"[a-z]+(?:-[a-z]+)+(?=[^\S\n]|[^\w\n-])", text.lower()))


def soft_hyphen(before, after):
    """True when 'before' ends in a line-break hyphen that is not part of the word."""
    m = re.search(r"([A-Za-z]+(?:-[A-Za-z]+)*)-$", before)
    right = re.match(r"[A-Za-z]*", after).group(0).lower()
    return bool(m and right and after[:1].islower() and "%s-%s" % (m.group(1).lower(), right) not in _VOCAB["hyphen"])


def join_lines(parts):
    out = ""
    for p in parts:
        if not out:
            out = p
        elif out.endswith(("—", "\u00ad")):       # a dash, or a soft hyphen (U+00AD) left by the PDF
            out = out.rstrip("\u00ad") + p
        elif out.endswith("-") and out[-2:-1].isalpha():
            out = out[:-1] + p if soft_hyphen(out, p) else out + p
        else:
            out += " " + p
    return out


def split_footnotes(raw, nxt, prev_open):
    """One page's lines -> (body lines, [(label or None, [[lines of a paragraph], ...])], next label).
    The footnotes start at a line holding only the next footnote number. label None = the rest of
    the previous page's last footnote (prev_open: that one does not end a sentence yet)."""
    rule = next((j for j in range(len(raw) - 1, 8, -1) if FNRULE_RE.match(raw[j])), None)
    texty = [j for j, x in enumerate(raw) if x.strip()]         # double-spaced body: a blank line after each line
    gaps = Counter(b - a for a, b in zip(texty, texty[1:]))      # (the notes at its foot are single-spaced)
    double = len(texty) > 6 and gaps[2] > gaps[1]
    if rule is not None and any(x.strip() for x in raw[rule + 1:]):
        return split_ruled_footnotes(raw, rule, nxt)
    k = None
    for j, l in enumerate(raw):
        m = FNLABEL_RE.match(l)
        if m and nxt <= int(m.group(1)) <= nxt + 2 and len(l) - len(l.lstrip()) <= 12 and any(x.strip() for x in raw[:j]) and any(x.strip() for x in raw[j + 1:]):
            k, nxt = j, int(m.group(1))         # a skipped number (lost in the text layer) costs one footnote, not the rest
            break
    inline = False
    if k is None:                   # "1  Plaintiffs refer to ...": the number on the same line as the note's text
        # (court of appeals slip opinions); after a blank line, below the page's first lines, the next number only.
        # Double-spaced text has a blank line around every line, so there a number starting a line one blank line
        # after a line that stops mid-sentence, with a blank line after it too, is the body going on ("ECF No. 21,
        # at\n\n3 (referring to\n\nexample of" is not footnote 3); a note runs on single-spaced, follows a wider
        # gap, or follows a full stop
        for j in range(4, len(raw)):
            m = FNSTART_RE.match(raw[j])
            if m and double and raw[j - 2].strip() and not (j + 1 < len(raw) and raw[j + 1].strip()):
                if not re.search(r"[.:;)\]”’\"]\s*$", raw[j - 2]):    # one blank line, as the body's own
                    continue
            # or, under the last page's note running on, right after its tail's full stop with no blank line (the
            # tail sits below a gap of 2+ blank lines): "… JA293-294.\n  3 These schools provide …"
            after_tail = prev_open and re.search(r"[.)\]”’\"]\s*$", raw[j - 1]) and \
                any(not raw[i].strip() and not raw[i + 1].strip() for i in range(max(0, j - 14), j - 1))
            if m and nxt <= int(m.group(2)) <= nxt + 1 and (not raw[j - 1].strip() or after_tail) and any(x.strip() for x in raw[:j]) and \
                    not m.group(3)[:1].isdigit() and not NOT_FNTEXT_RE.match(m.group(3)):
                k, nxt, inline = j, int(m.group(2)), True
                break
    end = k if k is not None else len(raw)
    fns, body_end = [], end
    if prev_open:                   # its tail: the last group above, after 2+ blank lines, set wider than the body
        j = end - 1
        while j >= 0 and not raw[j].strip():
            j -= 1
        g_end = j + 1
        while j >= 0 and raw[j].strip():
            j -= 1
        g_start, blanks = j + 1, 0
        while j >= 0 and not raw[j].strip():
            blanks, j = blanks + 1, j - 1
        widths = sorted(len(x) for x in raw[:g_start] if len(x.strip()) > 30)
        # set wider than the body, or single-spaced under a double-spaced body (a brief's notes)
        if g_start < g_end and j >= 0 and blanks >= 2 and widths and \
                (max(len(x) for x in raw[g_start:g_end]) > widths[len(widths) // 2] * 1.12 or (double and g_end - g_start >= 2)):
            fns.append((None, [[x.strip() for x in raw[g_start:g_end]]]))
            body_end = g_start
    if k is not None:
        cur = None
        prev_blank = True
        for l in raw[k:]:
            m = FNLABEL_RE.match(l)
            mi = FNSTART_RE.match(l) if inline and prev_blank else None
            prev_blank = not l.strip() or re.search(r"[.)\]”’\"]\s*$", l) is not None   # a note ends a sentence
            if m and int(m.group(1)) == nxt:
                cur = (m.group(1), [[]])
                fns.append(cur)
                nxt += 1
            elif mi and int(mi.group(2)) == nxt:
                cur = (mi.group(2), [[mi.group(3).strip()]])
                fns.append(cur)
                nxt += 1
            elif cur is None:
                continue
            elif not l.strip():
                if cur[1][-1]:
                    cur[1].append([])
            else:
                cur[1][-1].append(l.strip())
        for lab, paras in fns:
            while paras and not paras[-1]:
                paras.pop()
    return raw[:body_end], fns, nxt


FNRULE_RE = re.compile(r"^\s*—{4,}\s*$")        # the Supreme Court's footnote rule
FNSTART_RE = re.compile(r"^(\s{0,6})(\d{1,3})\s+(\S.*)$")
NOT_FNTEXT_RE = re.compile(r"(?:U\.\s?S\.|C\.\s?F\.\s?R|Stat\.|F\.\s?(?:\d|Supp)|S\.\s?Ct\.|L\.\s?Ed|[a-z])")   # a cite or a line's tail


def split_ruled_footnotes(raw, rule, nxt):
    """Footnotes under a rule, each starting '  6 The dissent ...' (Supreme Court slips). Blank lines
    are pdftotext noise there; a new paragraph is a line indented past the footnote's own text."""
    fns, cur, base = [], None, None
    for l in raw[rule + 1:]:
        if not l.strip():
            continue
        m = FNSTART_RE.match(l)
        ind = len(l) - len(l.lstrip())
        if m and int(m.group(2)) in (nxt, nxt + 1):
            cur = (m.group(2), [[m.group(3).strip()]])
            fns.append(cur)
            nxt, base = int(m.group(2)) + 1, None
            continue
        if cur is None:                              # the rest of the last page's footnote
            cur = (None, [[]])
            fns.append(cur)
        if base is None:
            base = ind
        if ind >= base + 2 and cur[1][-1]:
            cur[1].append([])
        cur[1][-1].append(l.strip())
    return raw[:rule], fns, nxt


# Supreme Court slip opinions: every page opens with "Cite as: 585 U. S. ____ (2018)  3" (odd),
# "4  TRUMP v. HAWAII" (even) or "(Slip Opinion)  OCTOBER TERM, 2017  1", then a centred line naming
# the opinion ("Opinion of the Court", "KENNEDY, J., concurring", "Syllabus"). Each opinion is
# paginated, and footnoted, from 1.
SCOTUS_HEAD_RE = re.compile(r"^\s*(?:\(Slip Opinion\)\s+OCTOBER TERM,\s*\d{4}(?:\s+(\d+))?"
                            r"|Cite as:\s*\d+\s*U\.\s*S\.\s*\S*\s*\(\d{4}\)(?:\s+(\d+))?"
                            r"|(\d+)\s{2,}[A-Z](?:[A-Z0-9.,'’&()\- ]|\bv\.)*)\s*$")
SCOTUS_COURT = "SUPREME COURT OF THE UNITED STATES"
_CAPNAME = r"[A-Z0-9][A-Z0-9 .,'’&:()\-]*\bV\.\s+[A-Z0-9 .,'’&:()\-]+?"
SLIP_RUNHEAD_RE = re.compile(r"^\s*(?:(\d{1,3})\s{2,}%s|%s\s{2,}(\d{1,3}))\s*$" % (_CAPNAME, _CAPNAME))


def foot_key(l):
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", l.strip()))


def running_feet(text):
    """Lines that recur (digits aside) among the last few lines of most pages: a running footer
    such as "U.S. v. SOA, et al.   7" / "Case No: 3:26-cv-". Page-foot lines only, so body text never matches."""
    pages = [p for p in text.replace("\r", "").split("\f") if p.strip()]
    seen = Counter()
    for pg in pages:
        tail = [l for l in pg.split("\n") if l.strip()][-5:]
        seen.update({foot_key(l) for l in tail if not HEADER_RE.match(l)})
    need = max(3, (len(pages) + 1) // 2)
    return {k for k, n in seen.items() if n >= need and len(k) < 80}


def layout_pages(text, heads=None):
    """-> (lines, footnotes [[label, page index, paragraphs]], page labels by page index).
    heads: a list to fill with each page's opinion label (Supreme Court slip opinions), else ''."""
    from tribonian.render import FOLD
    text = text.translate(FOLD)
    lines, footnotes, labels, nxt = [], [], [], 1
    set_vocab(text)
    feet = running_feet(text)
    for pi, pg in enumerate(text.replace("\r", "").split("\f")):
        raw = [l.expandtabs(8).rstrip() for l in pg.split("\n")]
        label, head = str(pi + 1), ""
        for j, l in enumerate(raw):             # running header: "Case 2:09-cv-06732 ... Page 3 of 7"
            if l.strip():
                m = HEADER_RE.match(l)
                ms = SCOTUS_HEAD_RE.match(l)
                if m:
                    label, raw[j] = m.group(1), ""
                elif ms and (not ms.group(3) or (heads is not None and any(h["head"] for h in heads))
                             or SCOTUS_COURT in pg[:2000]):
                    starts = SCOTUS_COURT in "\n".join(x for x in raw[j:j + 14] if x.strip()) or pi == 0
                    num = next((g for g in ms.groups() if g), None)
                    label, raw[j] = num or ("1" if starts else str(int(labels[-1]) + 1 if labels and labels[-1].isdigit() else pi + 1)), ""
                    k = next((k for k in range(j + 1, min(j + 4, len(raw))) if raw[k].strip()), None)
                    if k is not None and ms.group(3) and is_caps(raw[k].strip()) and " v. " in " " + raw[k] and \
                            not re.search(r"\b(?:J|JJ)\.", raw[k]):
                        raw[k] = ""             # the case name ran onto a second line
                        k = next((k for k in range(k + 1, min(k + 4, len(raw))) if raw[k].strip()), None)
                    if k is not None and len(raw[k]) - len(raw[k].lstrip()) >= 6 and len(raw[k].strip()) < 90:
                        head, raw[k] = raw[k].strip(), ""
                    if starts:
                        nxt = 1                 # a new opinion: its footnotes start again at 1
                elif PAGENUM_RE.match(l):       # slip opinion: the page number heads the page
                    label, raw[j] = re.sub(r"\D", "", l), ""
                elif SLIP_RUNHEAD_RE.match(l):  # circuit slip opinion: "10   LITEKYAN V. USAF" / "LITEKYAN V. USAF   11"
                    mh = SLIP_RUNHEAD_RE.match(l)
                    label, raw[j] = mh.group(1) or mh.group(2), ""
                break
        if heads is not None:
            heads.append({"head": head, "new": bool(head) and (label == "1" or pi == 0)})
        for _ in range(6):                      # the foot: page number, ECF stamp, running footer
            j = next((j for j in range(len(raw) - 1, -1, -1) if raw[j].strip()), None)
            if j is None:
                break
            m = HEADER_RE.match(raw[j])
            if m:
                label = m.group(1)
            elif not (PAGENUM_RE.match(raw[j]) or foot_key(raw[j]) in feet):
                break
            raw[j] = ""
        labels.append(label)
        if not any(l.strip() for l in raw):
            continue
        margin = min(len(l) - len(l.lstrip()) for l in raw if l.strip())
        raw = [l[margin:] if l.strip() else "" for l in raw]
        prev_open = bool(footnotes) and not re.search(r"[.)”\"’]\s*$", join_lines(footnotes[-1][2][-1] if footnotes[-1][2] else []))
        body, fns, nxt = split_footnotes(raw, nxt, prev_open)
        for lab, paras in fns:
            if lab is None:
                if footnotes and paras:
                    footnotes[-1][2][-1] += paras[0]
                    footnotes[-1][2].extend(paras[1:])
            else:
                footnotes.append([lab, pi, paras])
        gap, seen = 0, False
        for l in body:
            if not l.strip():
                gap += 1
                continue
            lines.append(Ln(pi, l, gap if seen else None))
            seen, gap = True, 0
    return lines, footnotes, labels


def split_caption(lns):
    """The caption (court, parties, case number) ends at the document's title: the first line in
    capitals, set off by blank lines, after the VERSUS line. -> (index where the text starts, title)."""
    first = [i for i in range(min(40, len(lns))) if lns[i].pi == lns[0].pi]
    vs = next((i for i in first if re.match(r"(?i)^(versus|vs?\.?|v\.)(\s|$)", lns[i].text)
               or re.search(r"\s\*(\s|$)", lns[i].text)), None)
    if vs is None:
        return 0, None
    for i in range(vs + 1, min(vs + 15, len(lns))):
        l = lns[i]
        if is_caps(l.text) and l.ind >= 5 and (l.gap is None or l.gap >= 2) and not re.search(r"\s\*(\s|$)", l.text):
            j = i + 1
            while j < len(lns) and lns[j].gap is not None and lns[j].gap <= 1 and is_caps(lns[j].text) \
                    and lns[j].ind >= 5 and len(lns[j].text) < 60:
                j += 1
            return j, " ".join(x.text for x in lns[i:j])
    return 0, None


def recap_blocks(lines, labels):
    """Lines -> blocks: dicts {kind: p|q|h|title|head|sig, pieces: [(text, page) | El], label}."""
    # right margin: the median end of the longer lines (the max is thrown off by lines that
    # pdftotext spreads out to justify them); a page with too few falls back on the document's
    right, base = {}, {}
    def median(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None
    whole = median([l.end for l in lines if len(l.text) >= 25]) or max((l.end for l in lines), default=80)
    for pi in {l.pi for l in lines}:
        mine = [l for l in lines if l.pi == pi]
        ends = [l.end for l in mine if len(l.text) >= 25]
        right[pi] = median(ends) if len(ends) >= 4 else whole
        spaced = [l.ind for l in mine if l.gap != 0] or [l.ind for l in mine]
        base[pi] = Counter(spaced).most_common(1)[0][0]
    known = [l.gap for l in lines if l.gap is not None]
    double = sum(g == 1 for g in known) > sum(g == 0 for g in known)

    runs = []
    for l in lines:
        if runs and l.gap == 0 and l.pi == runs[-1][-1].pi:
            runs[-1].append(l)
        else:
            runs.append([l])

    blocks, st = [], {"pi": lines[0].pi if lines else 0}
    cur = None                                  # the paragraph still open

    def short(l):
        return l.end < right[l.pi] - 12

    def add(b, l):
        turn = l.pi != st["pi"]
        st["pi"] = l.pi
        k = next((k for k in range(len(b["pieces"]) - 1, -1, -1) if isinstance(b["pieces"][k], tuple)), None)
        prev = b["pieces"][k][0] if k is not None else ""
        sep = "" if not prev or (prev.endswith(("-", "—")) and prev[-2:-1].isalpha()) else " "
        joined = False
        if prev.endswith("\u00ad"):
            b["pieces"][k], sep, joined = (prev[:-1], b["pieces"][k][1]), "", True
        elif prev.endswith("-") and soft_hyphen(prev, l.text):
            b["pieces"][k], joined = (prev[:-1], b["pieces"][k][1]), True
        if turn and joined:                     # "loca-" | page 12 | "tion": the page mark goes after the word
            word, _, rest = l.text.partition(" ")
            b["pieces"] += [(word, l.pi), El("page-number", {"label": labels[l.pi]})] + ([(" " + rest, l.pi)] if rest else [])
        else:
            if turn:
                b["pieces"].append(El("page-number", {"label": labels[l.pi]}))
            b["pieces"].append((sep + l.text, l.pi))
        b["last"] = l

    def new(kind, **kw):
        b = dict(dict(kind=kind, pieces=[], last=None, cont=None), **kw)
        blocks.append(b)
        return b

    sig, carry = None, None
    for run in runs:
        # single-spaced text: a page break is not a blank line, so a paragraph that reaches the
        # foot of a page at full width runs on at the top of the next
        if not double and cur is None and carry is not None and run[0].gap is None \
                and run[0].pi != carry["last"].pi and not short(carry["last"]) \
                and not (valid_label(run[0].text.rstrip(".")) or NUMHEAD_RE.match(run[0].text)):
            cur = carry
        carry = None
        if sig is not None:
            # a signature is a few lines on one page; anything past that is a new document
            # (an appendix holds several opinions and orders back to back), not more signature
            if (sig["last"] is None or run[0].pi == sig["last"].pi) and sig["n"] + len(run) <= SIG_LINES:
                for l in run:
                    add(sig, l)
                sig["n"] += len(run)
                continue
            sig = None
        # the signature: a rule or /s/, or a judge's name and title in capitals
        if re.fullmatch(r"_{5,}", run[0].text) or run[0].text.startswith("/s/") or \
                (len(run) <= 3 and all(is_caps(l.text) for l in run) and re.search(r"JUDGE$", run[-1].text)):
            cur, sig = None, new("sig", n=len(run))
            for l in run:
                if not re.fullmatch(r"_{5,}", l.text):
                    add(sig, l)
            continue
        if len(run) == 1:
            l, t = run[0], run[0].text
            closed = cur is None or short(cur["last"])
            m = NUMHEAD_RE.match(t)
            if (closed or re.search(r"[.:;)”\"]$", cur["last"].text)) and m and headingish(m.group(2)):
                cur = None
                new("h", label=m.group(1))
                new("title", pieces=[(m.group(2), l.pi)])
                continue
            # a centred label alone on its line: "II", or with its period "II." / "A." (the Fourth Circuit)
            # (after a sentence's end even when that line ran to the margin; well indented counts as centred for a
            # label this short: a caption or a quotation can move the page's margins)
            ended = closed or re.search(r"[.:;)”\"](?:\s?\d{1,2})?$", cur["last"].text)   # "2026). 1": a footnote mark
            if ended and l.ind >= 8 and valid_label(t.rstrip(".")) and \
                    (abs(l.ind - (right[l.pi] - l.end)) <= 10 or (l.ind >= 20 and len(t) <= 5)):
                cur = None
                new("h", label=t.rstrip("."))
                continue
            if closed and l.ind >= 8 and right[l.pi] - l.end >= 8 and abs(l.ind - (right[l.pi] - l.end)) <= 10 \
                    and len(t) <= 70 and headingish(t):
                cur = None
                new("head", pieces=[(t, l.pi)])
                continue
        b = base[run[0].pi]
        mi = min(l.ind for l in run)
        if (len(run) >= 3 or (len(run) >= 2 and mi >= b + 4)) if double else (len(run) >= 2 and mi >= b + 4):
            if run[0].gap is None and blocks and blocks[-1]["kind"] == "q":
                q = blocks[-1]                  # a quotation running on from the previous page
            else:
                q = new("q")
            for l in run:
                add(q, l)
            cur = None
            continue
        goes_on = run[0].gap is None and blocks and blocks[-1]["kind"] == "q"   # a quotation from the page before
        qlines = set() if double else quote_stretches(run, min(b, min(l.ind for l in run)), goes_on)
        for i, l in enumerate(run):
            if i in qlines:                     # single-spaced text: a quotation inside the run (no blank lines)
                if i == 0:
                    cur, q = None, blocks[-1]
                elif i - 1 not in qlines:
                    cur, q = None, new("q")
                add(q, l)
                continue
            if len(run) > 1 and l.ind >= 8 and valid_label(l.text.rstrip(".")) and abs(l.ind - (right[l.pi] - l.end)) <= 12:
                cur = None                      # a centred "II" with no blank line around it
                new("h", label=l.text.rstrip("."))
                continue
            mh = NUMHEAD_RE.match(l.text) if len(run) > 1 and l.ind >= 6 else None
            if mh and headingish(mh.group(2)) and (cur is None or re.search(r"[.:;)”\"]$", cur["last"].text)):
                cur = None                      # "I. Statutory and Regulatory Background", set in the run
                new("h", label=mh.group(1))
                new("title", pieces=[(mh.group(2), l.pi)])
                continue
            glued = double and i > 0            # single-spaced pair inside double-spaced text: one paragraph
            if cur is None or (not glued and (short(cur["last"]) or ENUM_RE.match(l.text)
                                              or (cur["cont"] is not None and l.ind > cur["cont"] + 2))):
                cur = new("p")
            elif cur["cont"] is None:
                cur["cont"] = l.ind
            add(cur, l)
        if not double:
            carry, cur = cur, None              # single-spaced text: a blank line ends the paragraph
    return split_leaders(blocks)


# a list item's first line: a bullet (Word's private-use \uf0b7 too) or "(a)" / "1." / "iv)"; its hanging
# indent is not a quotation
BULLET_START = r"^[•\uf0b7\uf0a7▪●◦‣\-–*]|^\(?\w{1,3}[.)]\s"


def quote_stretches(run, b, goes_on=False):
    """Single-spaced briefs set a block quotation inside the run, with no blank line around it: 2+ lines in a row
    indented 4-14 past the page's margin, after a line at the margin. Not a bulleted or numbered item's
    continuation, a heading set in capitals or numbered, or a dot-leader entry."""
    out, i = set(), 1
    if goes_on:                                 # the run opens with the rest of the last page's quotation
        m = min(l.ind for l in run)             # (its lines can skew the page's margin: measure from the run's own)
        j = 0
        while j < len(run) and m + 4 <= run[j].ind <= m + 14:
            j += 1
        out.update(range(j))
        i = max(j, 1)
    while i < len(run):
        j = i
        while j < len(run) and b + 4 <= run[j].ind <= b + 14:      # may run on to the next page
            j += 1
        prev = run[i - 1]
        if j - i >= 2 and prev.ind < b + 4 and prev.pi == run[i].pi \
                and not re.match(BULLET_START, prev.text) and not re.match(BULLET_START, run[i].text) \
                and not any(is_caps(l.text) or NUMHEAD_RE.match(l.text) or ENUM_RE.match(l.text)
                            or LEADER_RE.search(l.text) for l in run[i:j]):
            out.update(range(i, j))
        i = max(j, i + 1)
    return out


def split_leaders(blocks):
    """A table of contents or authorities reaches the right margin on every entry's last line, so
    its entries run together as one paragraph. End a block after each line with dot leaders."""
    out = []
    for b in blocks:
        if b["kind"] not in ("p", "q") or not any(isinstance(x, tuple) and LEADER_RE.search(x[0]) for x in b["pieces"]):
            out.append(b)
            continue
        cur = dict(b, pieces=[], toc=True)
        for x in b["pieces"]:
            cur["pieces"].append(x if cur["pieces"] or not isinstance(x, tuple) else (x[0].lstrip(), x[1]))
            if isinstance(x, tuple) and LEADER_RE.search(x[0]):
                out.append(cur)
                cur = dict(b, pieces=[], toc=True)
        if any(isinstance(x, tuple) and x[0].strip() for x in cur["pieces"]):
            cur["toc"] = False
            out.append(cur)
        elif cur["pieces"] and out:             # a page marker left over: keep it with the last entry
            out[-1]["pieces"] += cur["pieces"]
    return out


def leader_entry(e):
    """The dots and page(s) at the end of a toc entry -> <leader pages="...">."""
    for i in range(len(e.kids) - 1, -1, -1):
        k = e.kids[i]
        if isinstance(k, str):
            m = LEADER_RE.search(k)
            if m:
                e.kids[i:i + 1] = [x for x in (k[:m.start()], El("leader", {"pages": m.group(1)})) if x != ""]
            return e
    return e


def _continues_number(pieces, pj):
    """Does the text piece after pj go on with a digit? Then a digit ending piece pj is part of a number the
    layout reader split ("42" as "4" + "2" across two pieces), not a footnote mark."""
    nxt = next((q for q in pieces[pj + 1:] if isinstance(q, tuple)), None)
    return bool(nxt and nxt[0][:1].isdigit())


def place_marks(blocks, footnotes, warn, spaced=False):
    """Put a footnotemark where each footnote's number sits in the text, looking forward from the
    previous mark, no further than the page the footnote is printed on."""
    pos = [0, 0, 0]                             # block, piece, character
    for lab, pi, _ in footnotes:
        for pat in (FNMARK, FNMARK_SPACED, FNMARK_MID) if spaced else (FNMARK,):   # slip opinions: "7(E). 1" too
            rx = re.compile(pat % re.escape(lab))
            found, last = None, None
            bi, pj, ch = pos
            while bi < len(blocks) and found is None:
                pieces = blocks[bi]["pieces"]
                while pj < len(pieces):
                    p = pieces[pj]
                    if isinstance(p, tuple) and blocks[bi]["kind"] in ("p", "q"):
                        if p[1] > pi:
                            break
                        last = (bi, pj)
                        m = rx.search(p[0], ch)
                        while m and ((m.end() == len(p[0]) and _continues_number(pieces, pj))
                                     or (pat is FNMARK_MID and not fn_mid_ok(p[0], m))):
                            m = rx.search(p[0], m.start() + 1)   # "Act, 4" + "2 U.S.C.": a split number, not a mark
                        if m:
                            found = (bi, pj, m, pat in (FNMARK_SPACED, FNMARK_MID))
                            break
                    pj, ch = pj + 1, 0
                if found is None:
                    if pj < len(pieces):        # reached a later page
                        break
                    bi, pj, ch = bi + 1, 0, 0
            if found:
                break
        mark = El("footnotemark")
        mark.kids.append(lab)
        if found:
            bi, pj, m, spaced_mark = found
            text, page = blocks[bi]["pieces"][pj]
            before = text[:m.start()].rstrip() if spaced_mark else text[:m.start()]   # "2026). 1" -> "2026).¹"
            blocks[bi]["pieces"][pj:pj + 1] = [(before, page), mark, (text[m.end():], page)]
            pos = [bi, pj + 2, 0]
        elif last:
            bi, pj = last
            blocks[bi]["pieces"].insert(pj + 1, mark)
            pos = [bi, pj + 2, 0]
            warn("footnote %s: its number was not found in the text; marked at the end of page %s" % (lab, pi + 1))
        elif pos[0] < len(blocks) and pos[1] > 0:  # nothing left on its page after the previous mark: beside that mark
            bi, pj = pos[0], min(pos[1], len(blocks[pos[0]]["pieces"]))
            blocks[bi]["pieces"].insert(pj, mark)
            pos = [bi, pj + 1, 0]
            warn("footnote %s: its number was not found in the text; marked after the footnote before it" % lab)
        else:
            warn("footnote %s: nowhere to attach it; left out" % lab)


def _whole_cites(spans):
    """lawcites marks a list ("42 U.S.C. §§ 12112, 12102") as one span per section, all but the last only one
    character long. Wrapping those would cut the cite ("4" + "2 U.S.C. ..."), and the US Code status marks then
    read "2 U.S.C." (not found). One span per cite instead: the first one's start and key, the last one's end."""
    out, open_ = [], False                      # open_: the last span so far ended on a one-character part
    for a, b, key in spans:
        if out and open_ and key.split(":")[:2] == out[-1][2].split(":")[:2] and a > out[-1][0]:
            out[-1] = (out[-1][0], b, out[-1][2])   # "§§ 1400(d)(1)(A), 1411, 1412, 1419": every part, not two
        else:
            out.append((a, b, key))
        open_ = b - a == 1
    return out


def with_statutes(el):
    """Wrap each statute or constitutional cite ('5 U.S.C. § 552', 'Colo. Rev. Stat. § 26.5-4-205',
    'U.S. Const. amend. I') in <law key=...>, so inline() links it: to the US Code note in Markdown,
    to the provision's text in the PDF when the build prints it."""
    from tribonian import lawcites
    # The whole paragraph is read at once, so a short form ("§ 1396a(10)(A)(i)") finds the full cite before it even
    # when that one sits in a CourtListener span or italics; only cites inside one run of plain text are wrapped.
    def text_of(n):
        if isinstance(n, str):
            return n
        if n.tag in ("page-number", "footnotemark"):
            return " "
        return "".join(text_of(x) for x in n.kids)
    starts, pos = [], 0
    for k in el.kids:
        starts.append(pos)
        pos += len(text_of(k))
    found = _whole_cites(lawcites.spans_in("".join(text_of(k) for k in el.kids)))
    kids = []
    for k, s0 in zip(el.kids, starts):
        if not isinstance(k, str):
            kids.append(k)
            continue
        at = 0
        for a, b, key in found:
            a, b = a - s0, b - s0
            if a < at or b > len(k) or a < 0:
                continue                         # elsewhere, or across an element: left as it is
            span = El("law", {"key": key})
            span.kids.append(k[a:b])
            kids += [k[at:a], span]
            at = b
        kids.append(k[at:])
    el.kids = [k for k in kids if k != ""]
    return el


NO_LAW_MARK = {"a", "law", "usc", "page-number", "footnotemark", "leader"}


def mark_law(el):
    """with_statutes through a whole tree: every run of text not already inside a link or a cite. The opinions read
    from HTML get their statute cites marked here (the text-layout path marks them as it builds, in element())."""
    if not isinstance(el, El) or el.tag in NO_LAW_MARK:
        return el
    if any(isinstance(k, str) for k in el.kids):
        with_statutes(el)
    for k in el.kids:
        if isinstance(k, El):
            mark_law(k)
    return el


def element(tag, pieces, attrs=None):
    e = El(tag, attrs)
    for p in pieces:
        if isinstance(p, tuple):
            if e.kids and isinstance(e.kids[-1], str):
                e.kids[-1] += p[0]
            else:
                e.kids.append(p[0])
        else:
            e.kids.append(p)
    return with_statutes(e)


def judge_of(r, d):
    """'Signed by Magistrate Judge Joseph C. Wilkinson, Jr on 11/3/2010' -> 'Wilkinson, M.J.'"""
    m = re.search(r"Signed by ((?:Chief |Senior |District |U\.S\. )*)(Magistrate )?Judge (.+?) on \d", r.get("description") or "")
    name, mj = (m.group(3), bool(m.group(2))) if m else (d.get("assigned_to_str") or "", False)
    name = re.sub(r",?\s+(Jr|Sr|II|III|IV)\.?$", "", name.strip())
    last = name.split()[-1] if name.split() else ""
    return "%s, %s" % (last, "M.J." if mj else "J.") if last else "Court"


# A party's filing names its filer in the docket text: "MEMORANDUM by Thomas Dart, ..., The County of Cook in
# support of motion for summary judgment 80 (Yeddanapudi, Prathima)". The court's own documents don't ("ORDER:
# ... Signed by the Honorable ..."), so a "by <filer>" in the first words marks a party filing.
_FILER = re.compile(r"^\W*\w+(?:[ ,]+[\w.,']+){0,4}? by (?!the Honorable|Judge|Magistrate)(.+?)"
                    r"(?=\s+(?:in support|in response|in opposition|regarding|to|for|pursuant)\b|\s*\(|\s+\d+\b|$)", re.I)
_FILED_FOR = re.compile(r"\s+((?:in support|in response|in opposition|regarding|to|for|pursuant)\b[^(]*?)"
                        r"(?=(?<!No\.)\s+\d+\b|\s*\(|$)", re.I)
# a brief's first heading in capitals is often its front matter, not its title
_FRONT_MATTER = re.compile(r"(?i)^\s*(table of contents|table of authorities|index|contents|introduction|"
                           r"preliminary statement|background|argument|statement of (the )?(facts|case))\s*$")


def filer_of(r, d):
    """'Plaintiffs' / 'Defendants' / the filer as the docket text names it, or None for the court's own documents."""
    desc = clean(r.get("description") or "")
    m = _FILER.match(desc)
    if not m:
        return None
    who = m.group(1).strip(" ,")
    if re.fullmatch(r"(?i)(all )?(plaintiffs?|defendants?)", who):
        return who.split()[-1].capitalize()
    cap = d.get("case_name") or ""
    pl, _, df = cap.partition(" v. ")
    key = lambda x: re.sub(r"(?i)^the\s+|,.*$", "", x).strip().lower()
    if pl and key(pl) and key(pl) in who.lower():
        return "Plaintiffs" if "," in who else who
    if df and key(df) and key(df) in who.lower():
        return "Defendants" if "," in who else who
    return who if len(who) <= 40 else who[:38].rstrip(" ,") + "..."


def filed_title(r):
    """A party filing's title from its docket text: 'Memorandum in support of motion for summary judgment'."""
    desc = clean(r.get("description") or "")
    kind = (desc.split() or ["Document"])[0].strip(":,").capitalize()
    m = _FILER.match(desc)
    rest = _FILED_FOR.match(desc[m.end():]) if m else None
    return ("%s %s" % (kind, rest.group(1).strip())) if rest else kind


def read_recap(r, d, oi, warn):
    """One RECAP document -> the same opinion dict read_opinion() makes, so render_md and
    render_tex print it unchanged."""
    lines, footnotes, labels = layout_pages(r.get("plain_text") or "")
    start, title = split_caption(lines)
    lines = lines[start:]
    filer = filer_of(r, d)
    if filer:                                   # a party filing: the docket text names it whole; a brief's first
        title = filed_title(r)                  # capitals are often front matter ("TABLE OF CONTENTS") or cut short
    elif title and _FRONT_MATTER.match(title):
        title = None
    if not title:
        m = ORDER_RE.match(r.get("description") or "")
        title = m.group(1) if m else "Document %s" % doc_key(r)
    blocks = recap_blocks(lines, labels)
    place_marks(blocks, footnotes, warn, spaced=True)   # "(Aug. 25, 2026). 1": a mark set off by a space
    o = {"oi": oi, "kind": "", "author": "", "byline": [], "flat": [], "footnotes": {}, "api": r,
         "start": labels[lines[0].pi] if lines else "1", "title": titlecase(title), "who": filer or judge_of(r, d)}
    fill_opinion(o, blocks, footnotes)
    desc = El("p")
    desc.kids.append(clean(r.get("description") or ""))
    o["byline"] = [desc]
    return o


def fill_opinion(o, blocks, footnotes):
    """Layout blocks + footnotes -> o["flat"], o["footnotes"], o["summary"]."""
    for b in blocks:
        if b["kind"] == "h":
            o["flat"].append(["h", b["label"]])
        elif b["kind"] in ("title", "head"):
            bold = element("b", b["pieces"])
            o["flat"].append(["p", El("p", {"heading": "title" if b["kind"] == "title" else "yes"})])
            o["flat"][-1][1].kids.append(bold)
        elif b["kind"] == "sig":
            if b["pieces"]:
                o["flat"].append(["p", El("p")])
                o["flat"][-1][1].kids.append(element("i", b["pieces"]))
        elif any(isinstance(p, tuple) and p[0].strip() for p in b["pieces"]):
            el = element("blockquote" if b["kind"] == "q" else "p", b["pieces"])
            o["flat"].append(["q" if b["kind"] == "q" else "p", leader_entry(el) if b.get("toc") else el])
    for lab, pi, paras in footnotes:
        fn = El("footnote", {"label": lab})
        for para in paras:
            fn.kids.append(element("p", [(join_lines(para), pi)]))
        o["footnotes"][lab] = fn
    heads = sum(1 for b in blocks if b["kind"] == "h")
    o["summary"] = "%d paragraphs, %d quotations, %d headings, %d footnotes" % (
        sum(1 for b in blocks if b["kind"] == "p"), sum(1 for b in blocks if b["kind"] == "q"), heads, len(footnotes))


# ---------------------------------------------------------------- slip opinions
# Some opinions (recent circuit cases, mostly) are in CourtListener only as the court's slip
# opinion: pdftotext of the PDF, served as plain_text and, with citation links added, as <pre>
# in html_with_citations ('combined', no Harvard tags). Same layout reading as RECAP; the
# caption ends where the first judge starts writing ("MILLETT, Circuit Judge: ..."), and each
# later "KATSAS, Circuit Judge, concurring: ..." starts a separate opinion.

# a judge's name: up to four capitalised words, accented capitals included ("MARIA ARAÚJO KAHN",
# "JOHN M. WALKER"); the first word may be in title case ("Maria KAHN")
_UC = "A-ZÀ-ÖØ-Þ"
SLIP_AUTHOR_RE = re.compile(r"^(?:(PER CURIAM)|((?:[A-Z][a-z]+ )?[%s][%s'’\-]+(?: [%s][%s'’\-]*\.?){0,3}),"
                            % (_UC, _UC, _UC, _UC) +
                            r" (?:Senior |Chief )?(?:Circuit |District |Magistrate )?Judge(?:,? ([^:]{3,120}?))?)[.:]\s*(.*)$")


def slip_links(html):
    """{citation text, whitespace normalized: href} from CourtListener's links in the <pre> text."""
    from tribonian.render import FOLD                  # folded like the text they are matched against
    out = {}
    for href, t in re.findall(r'<a href="(/opinion/[^"]+)"[^>]*>(.*?)</a>', (html or "").translate(FOLD), re.S):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t)).strip()
        if len(t) >= 8:
            out.setdefault(t, href)
    return out


def relink(el, links):
    """Put CourtListener's case links back on the citations in a block read from plain text."""
    if not links:
        return
    rx = re.compile("|".join(re.escape(t).replace(r"\ ", r"[\s-]*\s") for t in sorted(links, key=len, reverse=True)))
    norm = {re.sub(r"\W", "", t): h for t, h in links.items()}
    kids = []
    for k in el.kids:
        if isinstance(k, El):
            if k.tag != "a":
                relink(k, links)
            kids.append(k)
            continue
        at = 0
        for m in rx.finditer(k):
            href = norm.get(re.sub(r"\W", "", m.group(0)))
            if not href:
                continue
            a = El("a", {"href": href})
            a.kids.append(m.group(0))
            kids += [k[at:m.start()], a]
            at = m.end()
        kids.append(k[at:])
    el.kids = [k for k in kids if k != ""]


def read_slip(op, oi, warn):
    """One slip-opinion text -> [opinion dicts] (the Court's, then any separate opinions)."""
    import html as _html
    text = op.get("plain_text") or _html.unescape(re.sub(r"<[^>]+>", "", op.get("html_with_citations") or ""))
    heads = []
    lines, footnotes, labels = layout_pages(text, heads)
    if sum(1 for h in heads if h["head"]) >= 2:
        return read_scotus_slip(op, oi, warn, lines, footnotes, labels, heads)
    starts = [(i, m) for i, l in enumerate(lines) for m in [SLIP_AUTHOR_RE.match(l.text)] if m]
    if not starts:
        warn("no 'JUDGE, Circuit Judge:' line found; the caption is left in the text")
        starts = [(0, None)]
    caption = lines[:starts[0][0]]
    b = next((i for i, l in enumerate(caption) if l.text.startswith("Before:")), None)
    byline = []
    if b is not None:
        p = El("p")
        p.kids.append(join_lines([l.text for l in caption[b:]]))
        byline = [p]
    links = slip_links(op.get("html_with_citations"))
    out = []
    for k, (i, m) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        seg = lines[i:end]
        if m and seg:
            seg[0].text = m.group(4)
            if not seg[0].text.strip():
                seg = seg[1:]
        if not seg:
            continue
        lo, hi = seg[0].pi, (lines[end].pi if end < len(lines) else 10 ** 6)
        fns = [f for f in footnotes if lo <= f[1] < hi or (k == 0 and f[1] < lo)]
        blocks = recap_blocks(seg, labels)
        place_marks(blocks, fns, warn, spaced=True)
        if m is None:
            who, kind = "Court", ""
        elif m.group(1):
            who, kind = "Per Curiam", "per curiam"
        else:
            who, kind = "%s, J." % m.group(2).split()[-1].title(), clean(m.group(3) or "")   # surname: "Kahn, J."
        o = {"oi": oi + len(out), "kind": kind, "author": "", "byline": byline if k == 0 else [],
             "flat": [], "footnotes": {}, "api": op, "start": labels[seg[0].pi], "who": who}
        fill_opinion(o, blocks, fns)
        for _, el in o["flat"]:
            if isinstance(el, El):
                relink(el, links)
        for fn in o["footnotes"].values():
            relink(fn, links)
        out.append(o)
    return out


SCOTUS_AUTHOR_RE = re.compile(r"^(?:(?:CHIEF )?JUSTICE [A-Z][A-Z'’\-]+|PER CURIAM|[A-Z][A-Z'’\-]+, (?:C\. )?J\., (?:concurring|dissenting))\b")
SCOTUS_WHO_RE = re.compile(r"^([A-Z][A-Za-z'’\-]+),\s*(C\.\s*)?J\.,\s*(.+)$")


def scotus_who(sentence, head):
    """The author line ('JUSTICE SOTOMAYOR, with whom JUSTICE GINSBURG joins, dissenting.') -> (who, kind)."""
    t = re.sub(r"\s+", " ", sentence or "").strip()
    if re.match(r"PER CURIAM", t):
        return "Per Curiam", "per curiam"
    if re.search(r"delivered the opinion of the Court|announced the judgment of the Court", t):
        return "Court", ""
    lead = re.split(r",\s*with whom\b", t)[0]
    m = re.search(r",\s*((?:concurring|dissenting)[^.]*?)\.?\s*$", t)
    kind = clean(m.group(1)) if m else ""
    names = re.findall(r"(CHIEF )?JUSTICE ([A-Z][A-Z'’\-]+)", lead)
    if not names:                                       # 'THOMAS, J., concurring.'
        mm = re.match(r"([A-Z][A-Z'’\-]+), (C\. )?J\.", lead)
        names = [(mm.group(2) or "", mm.group(1))] if mm else []
    if names:
        if len(names) == 1:
            return "%s, %sJ." % (names[0][1].title(), "C. " if names[0][0] else ""), kind
        return "%s%s and %s, JJ." % (", ".join(n.title() for _, n in names[:-1]), "," if len(names) > 2 else "",
                                     names[-1][1].title()), kind
    return head or "Opinion", kind


def read_scotus_slip(op, oi, warn, lines, footnotes, labels, heads):
    """A Supreme Court slip opinion -> [opinion dicts]: the syllabus, the Court's opinion, then each
    separate opinion. Each opinion starts on a page numbered 1 that carries the Court's caption; the
    caption (notice, court, docket, parties, date) is dropped and the 'JUSTICE X ...' sentence after
    it becomes the byline and names the author. Running heads are only a fallback: their text layer
    is often damaged ('ORSUCH, J., concurring')."""
    starts = [pi for pi, h in enumerate(heads) if h["new"]] or [0]
    if starts[0] != 0:
        starts = [0] + starts
    links = slip_links(op.get("html_with_citations"))
    out = []
    for g, lo in enumerate(starts):
        hi = starts[g + 1] if g + 1 < len(starts) else 10 ** 6
        seg = [l for l in lines if lo <= l.pi < hi]
        if not seg:
            continue
        head = heads[lo]["head"]
        byline, sentence = [], ""
        syllabus = head.lower().startswith("syllabus")
        if syllabus:                                # drop its caption: court, parties, writ
            c = next((i for i in range(min(30, len(seg))) if seg[i].text.strip() == SCOTUS_COURT), None)
            n = next((i for i in range(c or 0, min(45, len(seg))) if re.match(r"No\.\s", seg[i].text.strip())), None) \
                if c is not None else None
            if n is not None:
                seg = seg[:c] + seg[n:]
        if not syllabus:
            a = next((i for i in range(min(60, len(seg))) if SCOTUS_AUTHOR_RE.match(seg[i].text)), None)
            if a is None:
                warn("opinion at slip page %s (%s): no 'JUSTICE ...' line found; its caption is left in the text"
                     % (labels[lo], head))
            else:
                b = a
                while b < len(seg) - 1 and b < a + 4 and not re.search(r"\.\s*$", seg[b].text):
                    b += 1
                sentence = join_lines([l.text.strip() for l in seg[a:b + 1]])
                p = El("p")
                p.kids.append(sentence)
                byline, seg = [p], seg[b + 1:]
        if not seg:
            continue
        fns = [f for f in footnotes if lo <= f[1] < hi]
        blocks = recap_blocks(seg, labels)
        place_marks(blocks, fns, warn, spaced=True)
        if syllabus:
            who, kind = "Syllabus", "by the Reporter of Decisions, not part of the opinion"
        else:
            who, kind = scotus_who(sentence, head)
        o = {"oi": oi + len(out), "kind": kind, "author": "", "byline": byline, "flat": [], "footnotes": {},
             "api": op, "start": labels[seg[0].pi], "who": who}
        fill_opinion(o, blocks, fns)
        for _, el in o["flat"]:
            if isinstance(el, El):
                relink(el, links)
        for fn in o["footnotes"].values():
            relink(fn, links)
        out.append(o)
    return out


def prepare_slip(meta, api_ops, links, statutes):
    warnings, ops = [], []
    meta["srcline"] = [s.replace("(Harvard CAP text)", "(slip opinion text)") for s in meta["srcline"]]
    for op in api_ops:
        ops += read_slip(op, len(ops), warnings.append)
    for o in ops:
        o["ctx"] = Ctx("slip op.", o["who"], o["oi"])
        o["ctx"].links, o["ctx"].statutes = links, statutes
        o["ctx"].year = int((meta.get("decided") or "0")[:4] or 0)
        o["kindname"] = o["kind"] or ("opinion of the Court" if o["oi"] == 0 or o["who"] == "Court" else "opinion")
        o["ctx"].record = False
        o["byline_md"] = clean(" ".join(inline(b, "md", o["ctx"], []) for b in o["byline"]))
        o["byline_tex"] = clean(" ".join(inline(b, "tex", o["ctx"], []) for b in o["byline"]))
        o["ctx"].record = True
        o["tree"] = tree_of(o, assign_levels([b[1] for b in o["flat"] if b[0] == "h"]))
    return meta, ops, warnings


# ---------------------------------------------------------------- the local database, for names

_COURTS = {}


def local_db():
    """Read-only connection to casedb's database with the opinion text attached, or None."""
    try:
        import sqlite3
        from tribonian import casedb
    except ImportError:
        return None
    if not casedb.DB.exists() or Path(str(casedb.DB) + ".loading").exists():
        return None
    con = sqlite3.connect("file:%s?mode=ro" % casedb.DB, uri=True, timeout=30)
    if casedb.OPS_DB.exists():
        con.execute("ATTACH DATABASE ? AS ops", ("file:%s?mode=ro" % casedb.OPS_DB,))
    return con


def local_docket(did):
    """The docket from casedb's dockets table when it has the full columns (saves an API request)."""
    con = local_db()
    if con is None:
        return None
    cols = [r[1] for r in con.execute("PRAGMA table_info(dockets)")]
    if "nature_of_suit" not in cols:
        return None
    row = con.execute("SELECT * FROM dockets WHERE id=?", (did,)).fetchone()
    if row is None:
        return None
    d = {k: v for k, v in zip(cols, row)}
    d["absolute_url"] = "/docket/%d/%s/" % (did, d.pop("slug", None) or "x")
    d["case_name_full"] = ""
    return {k: (v if v is not None else "") for k, v in d.items()}


def court_cite(court_id, con):
    """'laed' -> 'E.D. La.'"""
    if court_id not in _COURTS:
        row = con.execute("SELECT citation_string FROM courts WHERE id=?", (court_id,)).fetchone() if con else None
        cite = row[0] if row and row[0] else ""
        if not cite:                            # no database: the court tree (tribonian dockets courts), if built
            from tribonian import dockets
            cite = dockets.court_cites().get(court_id, "")
        _COURTS[court_id] = cite or court_id or ""
    return _COURTS[court_id]


def opinions_named(ids, con):
    """RECAP 'cites' (opinion ids) -> [(cluster id, name, cite, court, year)], or None without the database."""
    if not ids:
        return []
    if con is None:
        return None
    try:
        rows = con.execute("SELECT DISTINCT o.cluster_id, c.case_name, c.date_filed, d.court_id FROM opmap o "
                           "JOIN clusters c ON c.id = o.cluster_id LEFT JOIN dockets d ON d.id = c.docket_id "
                           "WHERE o.id IN (%s)" % ",".join("?" * len(ids)), list(ids)).fetchall()   # opmap: main database
    except sqlite3.Error:
        return None
    out = []
    for cid, name, filed, court in rows:
        cs = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id=? ORDER BY type, reporter", (cid,)).fetchall()
        main = next((x for x in cs if x[1] == "U.S."), cs[0] if cs else None)
        out.append((cid, name or "case", "%s %s %s" % main if main else "", court_cite(court, con), (filed or "")[:4]))
    return sorted(out, key=lambda x: (x[1].lower(), x[4]))


MONTHS = ("Jan.", "Feb.", "Mar.", "Apr.", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec.")


def long_date(iso):
    """'2010-06-25' -> 'June 25, 2010' (Bluebook month abbreviations)."""
    try:
        dt = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return "%s %d, %d" % (MONTHS[dt.month - 1], dt.day, dt.year)


# ---------------------------------------------------------------- recap build

def recap_build(args):
    path = Path(args.json)
    data = json.loads(path.read_text(encoding="utf-8"))
    d = data["docket"]
    want = doc_numbers(args.docs)
    docs = [r for r in data["documents"] if (r.get("plain_text") or "").strip() and (not want or doc_key(r) in want)]
    if not docs:
        sys.exit("No document text in %s%s. Run tribonian case recap fetch --docket %s first." % (
            path, " for --docs " + args.docs if want else "", d.get("id")))
    con = local_db()
    court = court_cite(d.get("court_id"), con)
    name = d.get("case_name") or "Case"
    dslug = docket_slug(d)
    ndir = note_dir(dslug, d)
    index = load_index()
    links = {int(k): e["slug"] for k, e in index.items()}
    statutes = statute_notes()
    from tribonian import lawappendix, statecites
    # forms that do not name their state ("Penal Code § 187") are read as the court's state's
    statecites.CONTEXT["states"] = statecites.court_states(d.get("court_id"))
    warnings, ops = [], []
    for i, r in enumerate(docs):
        o = read_recap(r, d, i, lambda w, k=doc_key(r): warnings.append("ECF No. %s: %s" % (k, w)))
        o["ctx"] = Ctx("ECF No. %s, p." % doc_key(r), o["who"], i)
        o["ctx"].links, o["ctx"].statutes = links, statutes
        o["ctx"].year = int((r.get("entry_date_filed") or "0")[:4] or 0)
        o["kindname"] = "ECF No. %s · %s · %s" % (doc_key(r), o["title"], r.get("entry_date_filed") or "")
        o["byline_md"] = clean(inline(o["byline"][0], "md", o["ctx"], []))
        o["byline_tex"] = clean(inline(o["byline"][0], "tex", o["ctx"], []))
        labels = [b[1] for b in o["flat"] if b[0] == "h"]
        o["tree"] = tree_of(o, assign_levels(labels))
        for b in o["flat"]:
            mark_law(b[1])
        for fn in o["footnotes"].values():
            mark_law(fn)
        o["law"] = list(dict.fromkeys(lawappendix.keys_in([o["flat"], o["footnotes"]])))
        ops.append(o)

    # the law the documents cite: every provision once at the end of the PDF, each cite linking to it
    law = list(dict.fromkeys(k for o in ops for k in o["law"]))
    printed = law if getattr(args, "law", True) else []          # --no-law: no appendix, nothing fetched
    missing = lawappendix.fetch_missing(printed, federal=getattr(args, "fetch", False),
                                        state=getattr(args, "fetch_state_law", False), warnings=warnings)
    targets = {lawcites_section(k): lawappendix.anchor(k) for k in printed}
    for o in ops:
        o["ctx"].lawtargets = targets

    # one note per document, then the docket's note
    written = []
    for o in ops:
        r = o["api"]
        slug = doc_slug(dslug, r)
        where = note_dir(slug, d, near=dslug)
        cite = "%s, No. %s, ECF No. %s (%s %s)" % (name, d.get("docket_number"), doc_key(r), court, long_date(r.get("entry_date_filed")))
        head = yaml_head([("title", cite), ("aliases", ["%s, ECF No. %s" % (name, doc_key(r))]),
                          ("type", Raw("court-document")), ("document", o["title"]), ("court", Raw(d.get("court_id"))),
                          ("docket", "%s" % d.get("docket_number")), ("docket_note", "[[%s]]" % dslug), ("ecf", "%s" % doc_key(r)),
                          ("filed", Raw(r.get("entry_date_filed"))), ("judge", o["who"]), ("pages", Raw(r.get("page_count") or "")),
                          ("courtlistener_docket", Raw(d.get("id"))), ("recap_document", Raw(r.get("id"))),
                          ("text_source", r.get("text_source") or "CourtListener plain_text"),
                          ("source", Raw("%s%s" % (CL, r.get("absolute_url") or ""))), ("retrieved", Raw(date.today().isoformat())),
                          ("synthetic", False)]) + ["", "# %s" % esc(cite, "md"), "",
                "Docket: [[%s|%s, No. %s (%s)]]" % (dslug, esc(name, "md"), d.get("docket_number"), court), ""]
        tail = []
        named = opinions_named(r.get("cites") or [], con)
        if named:
            tail += ["## Cites", ""]
            for cid, nm, ct, cc, yr in named:
                label = "%s, %s (%s %s)" % (esc(nm, "md"), ct, cc, yr) if ct else "%s (%s %s)" % (esc(nm, "md"), cc, yr)
                tail.append("- [[%s|%s]]" % (links[cid], label) if cid in links else "- [%s](%s)%s (%s %s)" % (
                    esc(nm, "md"), opinion_url(cid, nm), ", " + ct if ct else "", cc, yr))
                if getattr(args, "cases", False):         # --cases: the sketch, as in the PDF's The Cases Cited
                    from tribonian import casesummary
                    tail += casesummary.sketch_md(con, cid, lambda t: esc(t, "md"))
            tail.append("")
        elif named is None:
            tail += ["## Cites", "", "*%d opinions (CourtListener opinion ids %s); names need the local database (casedb.py).*"
                     % (len(r["cites"]), ", ".join(map(str, r["cites"]))), ""]
        law_md = lawappendix.md_lines(o["law"], statutes)
        if law_md:
            tail += law_md + [""]
        md = render_md(None, [o], "\n".join(tail), head=head)
        (where / (slug + ".md")).write_text(md, encoding="utf-8")
        written.append(where / (slug + ".md"))

    tex_meta = {"title": "%s, No. %s (%s)" % (name, d.get("docket_number"), court), "name": name, "page": "1",
                "srcline": ["No. %s (%s)" % (d.get("docket_number"), court),
                            "ECF No%s. %s" % ("s" if len(ops) > 1 else "", ", ".join(doc_key(o["api"]) for o in ops)),
                            "CourtListener docket %s (RECAP, text from the filed PDFs)" % d.get("id"),
                            "retrieved " + date.today().isoformat()],
                "law": law,
                "posttex": lawappendix.appendix_tex(
                    printed, intro="Not part of the filing. Every statute, constitutional provision and regulation the "
                    "document%s cite%s, with its text where it is stored locally: today's text, not the law as of %s; "
                    "a cite of one in the text links here." % ("s" if len(ops) > 1 else "", "" if len(ops) > 1 else "s",
                                                                 (ops[0]["api"].get("entry_date_filed") or "")[:4] or "filing"),
                    editions=law_editions(ops))}
    if getattr(args, "cases", False):              # --cases: The Cases Cited before The Law Cited
        from tribonian import casesummary
        tex_meta["posttex"] = casesummary.section_tex(shared_db(), casesummary.cited(shared_db(), ops),
                                                      "filing" + ("s" if len(ops) > 1 else "")) + tex_meta["posttex"]
    pdf_name = dslug + (("-ecf-" + "-".join(slugify(x) for x in sorted(want, key=lambda x: (len(x), x)))) if want else "")
    rel_pdf = (pdf_dir(ndir) / (pdf_name + ".pdf")).relative_to(ROOT)

    L = yaml_head([("title", tex_meta["title"]), ("aliases", [name, "No. %s" % re.sub(r"^\d+:", "", d.get("docket_number") or "")]),
                   ("type", Raw("docket")), ("court", Raw(d.get("court_id"))), ("docket", "%s" % d.get("docket_number")),
                   ("nature_of_suit", d.get("nature_of_suit") or ""), ("cause", d.get("cause") or ""),
                   ("filed", Raw(d.get("date_filed") or "")), ("terminated", Raw(d.get("date_terminated") or "")),
                   ("judge", d.get("assigned_to_str") or ""), ("referred_to", d.get("referred_to_str") or ""),
                   ("courtlistener_docket", Raw(d.get("id"))), ("source", Raw("%s%s" % (CL, d.get("absolute_url") or ""))),
                   ("retrieved", Raw(date.today().isoformat())), ("synthetic", False)]) + ["",
         "# %s, No. %s (%s)" % (esc(name, "md"), d.get("docket_number"), court), "",
         "%s · filed %s%s · %s" % (court, d.get("date_filed") or "?", " · closed %s" % d["date_terminated"] if d.get("date_terminated") else "",
                                   esc(d.get("nature_of_suit") or d.get("cause") or "", "md")), "",
         "PDF: `%s`" % rel_pdf, "", "## Orders and opinions", ""]
    for o in ops:
        r = o["api"]
        L.append("- [[%s|ECF No. %s — %s (%s)]] — %s, %s pp." % (doc_slug(dslug, r), doc_key(r), esc(o["title"], "md"),
                                                                r.get("entry_date_filed"), o["who"], r.get("page_count") or "?"))
    rest = [r for r in data["documents"] if r not in docs]
    if rest:
        L += ["", "## Other documents in RECAP", ""]
        for r in rest:
            L.append("- ECF No. %s (%s)%s — %s [↗](%s%s)" % (doc_key(r), r.get("entry_date_filed") or "?",
                                                           "" if r.get("is_available") else " *not purchased*",
                                                           esc(clean(r.get("description") or "")[:160], "md"), CL, r.get("absolute_url") or ""))
    tex = render_tex(tex_meta, ops, args.font, args.style)  # first: it registers the cites not resolved
    L += [""] + citemarks.section_md(lambda t: esc(t, "md"))
    (ndir / (dslug + ".md")).write_text("\n".join(L) + "\n", encoding="utf-8")
    for p in [ndir / (dslug + ".md")] + written:
        print("wrote %s" % p.relative_to(ROOT))

    (ROOT / "build").mkdir(exist_ok=True)
    tex_path = ROOT / "build" / (pdf_name + ".tex")
    tex_path.write_text(tex, encoding="utf-8")
    print("wrote %s" % tex_path.relative_to(ROOT))
    if not args.no_pdf:
        pdf = run_pdflatex(tex_path, pdf_dir(ndir))
        if pdf:
            print("wrote %s" % pdf.relative_to(ROOT))
    unmapped_note()
    for o in ops:
        print("  ECF No. %-6s %-14s %-32s %s" % (doc_key(o["api"]), o["who"], o["title"][:32], o["summary"]))
    from tribonian import dockets
    dockets.index(quiet=True)                   # the GUI's View Dockets reads it
    for w in warnings:
        print("  note:", w)
    for k, hint in missing:
        print("  law not local: %s%s" % (lawcites_label(k), " (%s)" % hint if hint else ""))


# ================================================================ find

def find(args):
    """Search what you already have, then case names and parentheticals in the local database."""
    import sqlite3
    from tribonian import casedb
    # each argument is a term; one quoted with spaces ("state farm") is a phrase: those words, in order
    terms = [t for t in ([w for w in re.findall(r"[\w'’.-]+", a) if w.strip(".-")] for a in args.words) if t]
    if not terms:
        sys.exit("find what?")
    words = ['"%s"' % " ".join(t) if len(t) > 1 else t[0] for t in terms]          # for display
    pats = [re.compile(r"[\W_]+".join(re.escape(w) for w in t), re.I) for t in terms]
    allw = lambda t: all(rx.search(t or "") for rx in pats)

    # 1. already here: note titles/aliases (front matter) and file names
    have = []
    for p in sorted(NOTES.rglob("*.md")) if NOTES.is_dir() else []:
        head = p.read_text(encoding="utf-8", errors="replace").split("\n---", 1)[0]
        if allw(p.stem.replace("-", " ") + " " + head):
            pdf = next(iter(sorted((ROOT / "pdf").rglob(p.stem + "*.pdf"))), None) if (ROOT / "pdf").is_dir() else None
            have.append((p, pdf))
    print("Already have (%d):" % len(have))
    for p, pdf in have[:args.max]:
        print("  %s%s" % (p.relative_to(ROOT), "   PDF: %s" % pdf.relative_to(ROOT) if pdf else "   (no PDF yet)"))

    if Path(str(casedb.DB) + ".loading").exists():
        sys.exit("\ncasedb.py load is running; the database is searched after it finishes (only your notes were searched).")
    con = local_db()
    if con is None:
        sys.exit("\nNo local database (casedb.py); only your own notes were searched.")
    stamp = casedb.search_stamp(con)
    ok = casedb.SEARCH_DB.exists() and sqlite3.connect(casedb.SEARCH_DB).execute("SELECT stamp FROM meta").fetchone()[0] == stamp
    if not ok:
        print("\nBuilding the search index first (once, and again after a reload of clusters or parentheticals).")
        casedb.build_search(con)
    s = sqlite3.connect("file:%s?mode=ro" % casedb.SEARCH_DB, uri=True)
    q = " AND ".join('"%s"' % " ".join(w.replace('"', "") for w in t) for t in terms)   # FTS5: "a b" = phrase
    index = load_index()

    def describe(cid):
        r = con.execute("SELECT c.case_name, c.date_filed, c.citation_count, d.court_id FROM clusters c "
                        "LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id = ?", (cid,)).fetchone()
        if not r:
            return None
        cs = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id=? ORDER BY type, reporter", (cid,)).fetchall()
        main = next((x for x in cs if x[1] == "U.S."), cs[0] if cs else None)
        return {"cid": cid, "name": r[0] or "?", "year": (r[1] or "")[:4], "cited": r[2] or 0,
                "cite": "%s %s %s" % main if main else "", "court": court_cite(r[3], con)}

    results = []
    by_name = [describe(cid) for (cid,) in s.execute("SELECT cluster_id FROM names WHERE names MATCH ? LIMIT 5000", (q,))]
    by_name = sorted((x for x in by_name if x), key=lambda x: -x["cited"])[:args.max]
    counts = Counter(oid for (oid,) in s.execute("SELECT described FROM parens WHERE parens MATCH ?", (q,)))
    by_cid = Counter()
    for oid, n in counts.items():
        row = con.execute("SELECT cluster_id FROM ops.opinions WHERE id=?", (oid,)).fetchone()
        if row:
            by_cid[row[0]] += n
    by_paren = [dict(describe(cid), n=n) for cid, n in by_cid.most_common(args.max) if describe(cid)]

    def show(title, rows, note):
        print("\n%s:" % title)
        if not rows:
            print("  (none)")
        for x in rows:
            results.append(x["cid"])
            mine = index.get(str(x["cid"]), {})
            tag = "have" if mine.get("full") else "note" if mine else ""
            print("  %2d. %-4s %s%s (%s %s)  cited %d%s" % (len(results), tag, clean(x["name"])[:70],
                                                        ", " + x["cite"] if x["cite"] else "", x["court"], x["year"],
                                                        x["cited"], note(x)))
    show("Case name contains %s" % " ".join(words), by_name, lambda x: "")
    show("Courts' parentheticals about the case mention %s" % " ".join(words), by_paren,
         lambda x: "  · %d parentheticals" % x["n"])
    print("\n  have = text on disk, note = a stub note only")

    if args.get:
        if not 1 <= args.get <= len(results):
            sys.exit("--get takes a result number, 1 to %d" % len(results))
        cid = results[args.get - 1]
        subprocess.run([sys.executable, "-m", "tribonian", "map", "get", "--cluster", str(cid)], check=True)
        ent = load_index().get(str(cid), {})
        if not ent.get("full"):
            sys.exit("casemap.py get did not save the text for cluster %d" % cid)
        subprocess.run([sys.executable, "-m", "tribonian", "case", "build", str(CASES / ent["full"])], check=True)
    elif results:
        print("next: tribonian case find %s --get N" % shlex.join(args.words))


# ---------------------------------------------------------------- API search (the local database's gap)
# The bulk data stops at its dump date; CourtListener's search knows what came after. One request per
# search (20 results), cached under cases/api/search/ so repeating a search costs nothing. The GUI's
# "Include CourtListener" runs this and reads the JSON it prints.

API_LAG_DAYS = 30        # cases filed shortly before a dump often reach CourtListener after it


def api_search(query, mode="name", after=None, max_age=24):
    """-> {"after", "count", "hits": [...], "cached", "fetched"}. mode: name (case_name), cite (citation)
    or text (full text). after: "YYYY-MM-DD", "" for all dates, None = the bulk dump date less API_LAG_DAYS.
    Each hit says whether the local database already has it (local) and whether a case file exists (have)."""
    import datetime, hashlib, html as _html
    con = shared_db()
    if after is None:
        stamp = bulk_stamp(con) if con is not None else ""
        after = (str(date.fromisoformat(stamp) - datetime.timedelta(days=API_LAG_DAYS)) if stamp else "")
    params = {"type": "o", "order_by": "score desc"}
    params[{"name": "case_name", "cite": "citation", "text": "q"}[mode]] = query
    if after:
        params["filed_after"] = after
    key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    path = CASES / "api" / "search" / (key + ".json")
    out = None
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - out.get("time", 0) > max_age * 3600:
            out = None
    except (OSError, ValueError):
        pass
    cached = out is not None
    if out is None:
        res = call("%s/search/?%s" % (API, urllib.parse.urlencode(params)), token())
        hits = []
        for r in res.get("results") or []:
            sn = next((o.get("snippet") for o in r.get("opinions") or [] if (o.get("snippet") or "").strip()), "")
            sn = re.sub(r"\s+", " ", _html.unescape(sn.replace("<mark>", "\x01").replace("</mark>", "\x02"))).strip()
            sn = sn if "\x01" in sn else ""          # without a highlighted match it's just the caption
            hits.append({"cluster": r.get("cluster_id"), "name": r.get("caseName") or "",
                         "cite": ", ".join(r.get("citation") or []), "date": r.get("dateFiled") or "",
                         "court": r.get("court_citation_string") or r.get("court_id") or "",
                         "cited": r.get("citeCount") or 0, "snippet": sn})
        out = {"params": params, "time": time.time(), "count": res.get("count"), "hits": hits}
        write_profile(path, out)
    ids = [h["cluster"] for h in out["hits"] if h["cluster"]]
    local = set()
    if con is not None and ids:
        local = {r for (r,) in con.execute("SELECT id FROM clusters WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    index = load_index()
    for h in out["hits"]:
        e = index.get(str(h["cluster"]), {})
        h["local"] = h["cluster"] in local
        h["have"] = bool(e.get("full") and (CASES / e["full"]).exists())
    return {"after": after, "count": out["count"], "hits": out["hits"], "cached": cached,
            "fetched": time.strftime("%Y-%m-%d %H:%M", time.localtime(out["time"]))}


def api_search_cmd(args):
    res = api_search(" ".join(args.words), args.mode, "" if args.all else args.after, args.max_age)
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
        return
    print("CourtListener, filed %s: %s matches (%s)" % ("after " + res["after"] if res["after"] else "any date",
                                                      res["count"], "cached " + res["fetched"] if res["cached"] else "1 request"))
    for i, h in enumerate(res["hits"], 1):
        tag = "have" if h["have"] else "local" if h["local"] else "NEW"
        print("  %2d. %-5s %s%s (%s %s)  cluster %s" % (i, tag, h["name"][:70], ", " + h["cite"] if h["cite"] else "",
                                                     h["court"], h["date"][:4], h["cluster"]))
    print("  NEW = not in the local database; get one with: tribonian map get --cluster ID")


def iter_tag(el, tag):
    for k in el.kids:
        if isinstance(k, El):
            if k.tag == tag:
                yield k
            yield from iter_tag(k, tag)


def usage(args):
    """How much of the API allowance is used, from the local logs (this machine's requests only)."""
    logp, hp = CASES / "_api_log.json", CASES / "_api_history.tsv"
    now = time.time()
    log = sorted(t for t in (json.loads(logp.read_text()) if logp.exists() else []) if now - t < WINDOWS[-1])
    print("CourtListener API, requests from this machine (limits: CL_LIMITS=%s)" % ",".join(map(str, LIMITS)))
    for lim, win, name in zip(LIMITS, WINDOWS, ("minute", "hour", "day")):
        recent = [t for t in log if now - t < win]
        free = ""
        if recent:
            free = "   next frees at %s" % time.strftime("%a %H:%M", time.localtime(recent[0] + win))
        print("  last %-6s %4d of %-4d %s%s" % (name, len(recent), lim, "FULL" if len(recent) >= lim else "left %d" % (lim - len(recent)), free))
    if not hp.exists():
        print("  (no history yet: cases/_api_history.tsv starts with the next request)")
        return
    rows = [l.split("\t") for l in hp.read_text(encoding="utf-8").splitlines() if l.count("\t") == 2]
    days = Counter(r[0][:10] for r in rows)
    print("by day (last %d):" % min(args.days, len(days)))
    for d in sorted(days)[-args.days:]:
        by = Counter(r[1] for r in rows if r[0].startswith(d))
        print("  %s %4d   %s" % (d, days[d], ", ".join("%s %d" % kv for kv in by.most_common(4))))
    print("total logged: %d requests since %s" % (len(rows), rows[0][0][:10] if rows else "-"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download a case to cases/*.json")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--cite", help='reporter citation, e.g. "463 U.S. 29"')
    g.add_argument("--cluster", type=int, help="CourtListener cluster id")
    f.set_defaults(func=fetch)
    u = sub.add_parser("usage", help="CourtListener API requests used, from the local logs")
    u.add_argument("--days", type=int, default=7, help="days of history to show (default 7)")
    u.set_defaults(func=usage)
    b = sub.add_parser("build", help="cases/*.json -> md/, build/*.tex, pdf/")
    b.add_argument("json", help="file written by fetch")
    b.add_argument("--font", default="XCharter", help="LaTeX font package (default XCharter)")
    b.add_argument("--style", default="boxes", choices=["boxes", "rails"])
    b.add_argument("--no-pdf", action="store_true", help="skip pdflatex")
    b.add_argument("--no-goodlaw", action="store_true",
                   help="skip the good-law check (slow: reads up to ~2,000 citing opinions); keeps an earlier result")
    b.add_argument("--quick-goodlaw", dest="quick_goodlaw", type=int, nargs="?", const=100, metavar="N",
                   help="quick good-law check instead: only the newest N citing opinions (default 100, casemap.GOODLAW_QUICK); "
                   "status now, not the year it changed; a current full result is kept")
    b.add_argument("--fetch", action="store_true", help="download the text of cited federal law that is not local "
                   "(C.F.R. titles, the U.S. Constitution); a refusal or a dropped connection is reported and the build goes on")
    b.add_argument("--no-law", dest="law", action="store_false", help="leave out The Law Cited (the cited provisions’ text at the end of the PDF) and fetch nothing; the note keeps its list")
    b.add_argument("--cases", action="store_true", help="end the PDF with The Cases Cited: each cited case once, with its "
                   "reporter summary or syllabus, else how later courts describe it")
    b.add_argument("--fetch-state-law", dest="fetch_state_law", action="store_true",
                   help="download and build the cited state law that is not local (each code's item: a title or chapter)")
    b.add_argument("--coupling", action=argparse.BooleanOptionalAction, default=True,
                   help="Coupled with: the cases citing the most of the same cases, computed when not cached (default; "
                        "--no-coupling shows them only when already cached)")
    b.set_defaults(func=build)
    sa = sub.add_parser("api-search", help="search CourtListener for cases newer than the local database (1 request, cached)")
    sa.add_argument("words", nargs="+")
    sa.add_argument("--mode", choices=["name", "cite", "text"], default="name",
                    help="name = case name (default), cite = a citation, text = the opinions' full text")
    sa.add_argument("--after", help="filed after this date (default: the bulk data's date less %d days)" % API_LAG_DAYS)
    sa.add_argument("--all", action="store_true", help="any filing date")
    sa.add_argument("--max-age", dest="max_age", type=float, default=24, help="hours a cached search is reused (default 24)")
    sa.add_argument("--json", action="store_true", help="print JSON (for the GUI)")
    sa.set_defaults(func=api_search_cmd)
    fd = sub.add_parser("find", help="search case names and courts' parentheticals (local database)")
    fd.add_argument("words", nargs="+", help='all must match; quote a phrase: glomar "state farm"')
    fd.add_argument("--get", type=int, metavar="N", help="fetch result N from the local database and build its PDF")
    fd.add_argument("--max", type=int, default=10, help="results per list (default 10)")
    fd.set_defaults(func=find)
    rc = sub.add_parser("recap", help="district and appeals court filings from RECAP: fetch, build")
    rsub = rc.add_subparsers(dest="rcmd", required=True)
    rf = rsub.add_parser("fetch", help="download a docket's orders to cases/recap/<court>/*.json")
    rf.add_argument("--docket", help="CourtListener docket id or URL, e.g. 4949363")
    rf.add_argument("--court", help="with --number: CourtListener court id, e.g. ca4 (4th Cir.), mdd (D. Md.)")
    rf.add_argument("--number", help="with --court: the court's case number, e.g. 26-2230")
    rf.add_argument("--docs", help="document numbers, e.g. 35,55,66 or 12-1 for an attachment (default: the court's orders and opinions)")
    rf.add_argument("--all", action="store_true", help="every available document, attachments included")
    rf.set_defaults(func=recap_fetch)
    rb = rsub.add_parser("build", help="cases/recap/<court>/*.json -> one PDF, a note per document, a docket note")
    rb.add_argument("json", help="file written by recap fetch")
    rb.add_argument("--docs", help="only these document numbers (default: every one with text)")
    rb.add_argument("--font", default="XCharter", help="LaTeX font package (default XCharter)")
    rb.add_argument("--style", default="boxes", choices=["boxes", "rails"])
    rb.add_argument("--no-pdf", action="store_true", help="skip pdflatex")
    rb.add_argument("--fetch", action="store_true", help="download the text of cited federal law that is not local "
                    "(C.F.R. titles, the U.S. Constitution); a refusal or a dropped connection is reported and the build goes on")
    rb.add_argument("--no-law", dest="law", action="store_false", help="leave out The Law Cited (the cited provisions’ text at the end of the PDF) and fetch nothing; the note keeps its list")
    rb.add_argument("--cases", action="store_true", help="end the PDF with The Cases Cited: each cited case once, with its "
                   "reporter summary or syllabus, else how later courts describe it")
    rb.add_argument("--fetch-state-law", dest="fetch_state_law", action="store_true",
                    help="download and build the cited state law that is not local (each code's item: a title or chapter)")
    rb.set_defaults(func=recap_build)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
