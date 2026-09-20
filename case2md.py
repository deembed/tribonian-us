#!/usr/bin/env python3
"""case2md.py -- court opinions (CourtListener) -> Obsidian markdown + LaTeX.

    ./case2md.py fetch --cite "463 U.S. 29"      # -> cases/<cite>_<name>.json
    ./case2md.py fetch --cluster 110991
    ./case2md.py build cases/463-u-s-29_*.json   # -> md/, build/, pdf/
The citation graph (stub notes, wikilinks, cited-by statistics) is casemap.py.

build reuses the LaTeX preamble (page size, boxes, running header/footer) and
the escaping table from usc2md.py; everything opinion-specific lives here.
Text source: the Harvard Caselaw Access Project version of each opinion
(custom tags <author>, <page-number>, <footnotemark>, <footnote>), as served
by CourtListener in html_with_citations.

Token: environment variable COURTLISTENER_TOKEN, else the GNOME keyring via
    secret-tool lookup service courtlistener
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, unicodedata
from datetime import date
from html.parser import HTMLParser
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

API = "https://www.courtlistener.com/api/rest/v4"
# Everything is read and written under ROOT (the folder this script sits in).
# CASE2MD_ROOT overrides it, which is how the tests run without touching real output.
ROOT = Path(os.environ.get("CASE2MD_ROOT") or Path(__file__).resolve().parent)
CASES = ROOT / "cases"
NOTES = ROOT / "md-cases"      # case notes; md/ belongs to the US Code build (sync_vault.sh mirrors both)
# CourtListener API limits since May 2026, free tier: 5 a minute, 50 an hour,
# 125 a day. A member sets CL_LIMITS, e.g. CL_LIMITS=10,75,300 for the $10 tier.
LIMITS = [int(x) for x in (os.environ.get("CL_LIMITS") or "5,50,125").split(",")]
WINDOWS = (60, 3600, 86400)
PAUSE = 0               # extra seconds between requests, on top of the limits


def throttle():
    """Wait until one more request fits in every window; if the DAY is used up,
    stop cleanly instead (everything is cached, so rerunning the same command
    later picks up where this stopped). Request times persist in cases/_api_log.json."""
    CASES.mkdir(parents=True, exist_ok=True)
    logp = CASES / "_api_log.json"
    while True:
        now = time.time()
        log = [t for t in (json.loads(logp.read_text()) if logp.exists() else []) if now - t < WINDOWS[-1]]
        waits = []
        for lim, win in zip(LIMITS, WINDOWS):
            recent = [t for t in log if now - t < win]
            if len(recent) >= lim:
                waits.append((recent[-lim] + win - now + 1, win))
        if not waits:
            logp.write_text(json.dumps(log + [now]))
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
    t = os.environ.get("COURTLISTENER_TOKEN")
    if t:
        return t.strip()
    try:
        t = subprocess.run(["secret-tool", "lookup", "service", "courtlistener"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        t = ""
    if not t:
        sys.exit("No token: set COURTLISTENER_TOKEN or store one with\n"
                 "  secret-tool store --label='CourtListener API' service courtlistener")
    return t


def call(url, tok, data=None, soft=False):
    """GET url (or POST when data is a dict); return parsed JSON.
    Paces itself, waits and retries when rate-limited. soft=True: a 404 returns None."""
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(5):
        throttle()
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
                delay = int(e.headers.get("Retry-After") or 0) or 30 * (attempt + 1)
                print(f"  HTTP {e.code}; waiting {delay}s then retrying", file=sys.stderr)
                time.sleep(delay)
                continue
            if e.code == 404 and soft:
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

def load_index():
    p = CASES / "_index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_index(index):
    CASES.mkdir(parents=True, exist_ok=True)
    (CASES / "_index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")


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
    t = Tree()
    t.feed(s)
    t.close()
    return t.root


# ---------------------------------------------------------------- inline text

CL = "https://www.courtlistener.com"
FN_NAMES = {"*": "star", "†": "dagger", "‡": "ddagger", "§": "sect", "**": "star2"}
MD_ESC = {"*": r"\*", "_": r"\_", "$": r"\$", "<": r"\<", "`": r"\`"}   # $ = Obsidian math


def esc(s, mode):
    if mode == "tex":
        from usc2md import tex_chars
        return tex_chars(s).replace("•", r"\textbullet{}")
    return "".join(MD_ESC.get(c, c) for c in s)


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

    def path(self):
        bits = ["%s %s" % (self.reporter, self.page), self.who] + ([self.part] if self.part else [])
        return esc(" -- ".join(b for b in bits if b.strip()), "tex").replace(r"\textbackslash{}", "")

    def fnkey(self, label):
        key = FN_NAMES.get(label, re.sub(r"[^A-Za-z0-9]", "", label) or "x")
        return key if self.oi == 0 else "%d-%s" % (self.oi + 1, key)


def inline(node, mode, ctx, marks):
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
        elif k.tag == "a" and mode == "md" and k.attrs.get("href", "").startswith("/"):
            m = re.match(r"/opinion/(\d+)/", k.attrs["href"])
            if m and int(m.group(1)) in ctx.links:       # a case we have a note for
                out.append(wrap(inline(k, mode, ctx, marks), "[[%s|" % ctx.links[int(m.group(1))], "]]"))
            else:
                out.append(wrap(inline(k, mode, ctx, marks), "[", "](%s%s)" % (CL, k.attrs["href"])))
        elif k.tag == "span" and mode == "md" and "citation" in cls and statute_note(k.text()) in ctx.statutes:
            out.append(wrap(inline(k, mode, ctx, marks), "[[%s|" % statute_note(k.text()), "]]"))
        elif k.tag == "br":
            out.append(" ")
        else:
            out.append(inline(k, mode, ctx, marks))
    return "".join(out)


def plain(el, words=7):
    """First few words of a block, without page markers or footnote marks."""
    def walk(n):
        return "".join(k if isinstance(k, str) else
                       "" if k.tag in ("page-number", "footnotemark") else walk(k) for k in n.kids)
    w = clean(walk(el)).split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")


def statute_note(text):
    """'15 U. S. C. § 1392' -> '15-usc-1392', the name usc2md.py gives that section's note."""
    m = re.match(r"\s*(\d+)\s*U\.\s?S\.\s?C\.\s*(?:A\.\s*)?§+\s*(\d+[A-Za-z]*(?:-\d+[A-Za-z]*)?)", text or "")
    return "%s-usc-%s" % (m.group(1), m.group(2).lower()) if m else None


def statute_notes():
    md = ROOT / "md"
    return {p.stem for p in md.rglob("*-usc-*.md")} if md.is_dir() else set()


def clean(s):
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"\s*(•|\\textbullet\{\})$", "", s)     # scan speck read as a bullet at a paragraph's end


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


def read_opinion(op, oi):
    """API opinion dict -> dict with byline pieces, a flat block list, footnotes."""
    root = parse_html(op.get("html_with_citations") or op.get("xml_harvard") or "")
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
    L = ["---", 'title: "%s"' % meta["title"].replace('"', "'"), "aliases:"]
    L += ['  - "%s"' % a.replace('"', "'") for a in meta["aliases"]]
    L += ["type: case", "stub: %s" % ("true" if stub else "false"),
          'citation: "%s"' % meta["cite"], "citations:"]
    L += ['  - "%s"' % c for c in meta["cites"]]
    L += ["court: %s" % meta["court"], 'docket: "%s"' % meta["docket"], "decided: %s" % meta["decided"]]
    if meta["argued"]:
        L.append('argued: "%s"' % meta["argued"])
    L += ['judges: "%s"' % meta["judges"].replace('"', "'"), "courtlistener_cluster: %s" % meta["cluster"]]
    if meta.get("cited_by") is not None:
        L.append("cited_by_count: %s" % meta["cited_by"])
    L += ["source: %s" % meta["url"], "retrieved: %s" % date.today().isoformat(), "synthetic: false", "---", "",
          "# %s, %s (%s)" % (esc(meta["name"], "md"), meta["cite"], meta["year"]), ""]
    return L


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


def render_stub(meta, tail, why="Text not fetched.", cluster=None):
    L = front_matter(meta, True) + editorial(cluster or {})
    L += ["*Stub: %s To get the text: `./case2md.py fetch --cluster %s`, then `build`.*" % (why, meta["cluster"]), ""]
    return "\n".join(L) + "\n" + tail


def render_md(meta, ops, tail="", cluster=None):
    L = front_matter(meta, False) + editorial(cluster or {})
    notes = []
    for o in ops:
        ctx = o["ctx"]
        L += ["## %s — %s" % (ctx.who, o["kindname"]), ""]
        if o["byline_md"]:
            L += ["*%s*" % o["byline_md"], ""]

        def walk(part):
            if part.level:
                L.extend(["#" * (2 + part.level) + " " + part.label, ""])
            for kind, el in part.blocks:
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


def tail_sections(data, index):
    """'## Cites' (from the links in this case's text) and '## Cited by' (written
    by casemap.py citedby, if it has been run for this case)."""
    L = []
    cited = cited_clusters(data)
    if cited:
        L += ["## Cites", ""]
        def key(kv):
            e = index.get(str(kv[0]), {})
            return (-kv[1], e.get("cite") or "~")
        for cid, n in sorted(cited.items(), key=key):
            e = index.get(str(cid))
            times = " ×%d" % n if n > 1 else ""
            if e:
                L.append("- [[%s|%s, %s (%s)]]%s" % (e["slug"], esc(e["short"] or e["name"], "md"), e["cite"], e["year"], times))
            else:
                L.append("- [cluster %d](%s/opinion/%d/)%s" % (cid, CL, cid, times))
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
            body, kind = render_md(meta, ops, tail, data["cluster"]), "full"
        except Unsupported as e:
            meta = case_meta(data)
            body, kind = render_stub(meta, tail, "text fetched, but %s" % e, data["cluster"]), "unparsed"
    else:
        body = render_stub(case_meta(data), tail, cluster=data["cluster"])
    NOTES.mkdir(exist_ok=True)
    (NOTES / (ent["slug"] + ".md")).write_text(body, encoding="utf-8")
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


def render_tex(meta, ops, fontpkg, style):
    from usc2md import PREAMBLE, RULES
    body = []
    for n, o in enumerate(ops):
        ctx = o["ctx"]
        ctx.page, ctx.part = o["start"] or meta["page"], ""
        parts = []                           # str = unboxed text, dict = one flat box

        def block(kind, el, lead=""):
            marks = []
            t = lead + clean(inline(el, "tex", ctx, marks))
            if kind == "q":
                t = "\\begin{quoted}%s\\end{quoted}" % t
            return t, "".join(fn_tex(o, ctx, m) for m in marks)

        def emit(part, depth, first):
            """Returns the last box emitted for this part's subtree (or None)."""
            last = None
            blocks = list(part.blocks) or [(None, None)]
            for i, (kind, el) in enumerate(blocks):
                lead = ""
                if i == 0 and part.level:
                    ctx.part = part.path
                    # PDF outline entry (the reMarkable's table of contents): "V-A · *46 · first words"
                    title = " · ".join(x for x in (part.path, "*" + ctx.page if ctx.page else "",
                                                  plain(el) if el is not None else "") if x)
                    mark = "\\pdfbookmark[%d]{%s}{o%d-%s}" % (part.level + 1, esc(title, "tex"), ctx.oi, part.path)
                    lead = "\\recordpath{%s}" % ctx.path() + mark + "\\lab{%s.}" % esc(part.label, "tex")   # "I." not "I": a bare I reads as the pronoun
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
            for i, c in enumerate(part.children):
                r = emit(c, depth + 1, i == 0)
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

        if n:
            # new opinion: start a fresh page unless 8 lines still fit under its heading
            body.append("\\par\\ifdim\\dimexpr\\pagegoal-\\pagetotal\\relax<8\\baselineskip\\clearpage"
                        "\\else\\vspace{12pt}\\hrule\\vspace{6pt}\\fi\n")
        body.append("\\recordpath{%s}\\pdfbookmark[1]{%s}{o%d}{\\large\\bfseries %s}\\ {\\large %s}\\par\n" % (
            ctx.path(), esc("%s — %s · *%s" % (ctx.who, o["kindname"], ctx.page), "tex"), ctx.oi,
            esc(ctx.who, "tex"), esc(o["kindname"], "tex")))
        if o["byline_tex"]:
            body.append("{\\small\\itshape %s\\par}\n" % o["byline_tex"])
        body.append("\\vspace{2pt}\n")

        top = o["tree"]
        for kind, el in top.blocks:          # text before Part I
            text, fns = block(kind, el)
            parts_add(0, [text + "\\par"], False, fns)
        for c in top.children:               # Parts sit at depth 0, like subsections
            parts.append("\\vspace{5pt}\n")
            emit(c, 0, False)
        for p in parts:
            body.append("\\begin{lvl}[%s]\n%s\n\\end{lvl}\n" % (box_opts(p), "\n".join(p["paras"]))
                        if isinstance(p, dict) else p)

    src = " \\textbullet\\ ".join(esc(s, "tex") for s in meta["srcline"] if s)
    pre = (PREAMBLE.replace("__FONTPKG__", fontpkg).replace("__RULES__", RULES[style])
           .replace("__DOCTITLE__", esc(meta["title"], "tex"))
           .replace("__DOCHEAD__", esc(meta["name"], "tex"))
           .replace("__SRCLINE__", src))
    return pre + "".join(body) + "\n\\end{document}\n"


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
        "srcline": ["No. " + d["docket_number"] if d.get("docket_number") else "", argued,
                    "Decided " + (c.get("date_filed") or ""),
                    "CourtListener cluster %s (Harvard CAP text)" % c.get("id"),
                    "retrieved " + date.today().isoformat()],
        "slug": "%s-%s" % (slugify(cite.replace(".", "")), slugify(short, 30)),
    }


def prepare(data, index=None):
    """Fetched JSON -> (meta, opinions ready to render, warnings)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
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
        raise Unsupported("it is only in CourtListener's 'combined' HTML, which is not handled yet.")
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
    index = load_index()
    register(data, index, args.json)
    save_index(index)
    try:
        meta, ops, warnings = prepare(data, index)
    except Unsupported as e:
        sys.exit("Cannot build: %s" % e)

    for d in (NOTES, here / "build", here / "pdf"):
        d.mkdir(exist_ok=True)
    md_path = NOTES / (meta["slug"] + ".md")
    md_path.write_text(render_md(meta, ops, tail_sections(data, index), data["cluster"]), encoding="utf-8")
    tex = render_tex(meta, ops, args.font, args.style)
    tex_path = here / "build" / (meta["slug"] + ".tex")
    tex_path.write_text(tex, encoding="utf-8")
    print("wrote md-cases/%s\nwrote build/%s" % (md_path.name, tex_path.name))

    if not args.no_pdf:
        with tempfile.TemporaryDirectory() as work:
            for _ in range(2):
                r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                                    "-output-directory", work, str(tex_path)], capture_output=True, text=True)
            pdf = Path(work) / (meta["slug"] + ".pdf")
            if r.returncode or not pdf.exists():
                shutil.copy(Path(work) / (meta["slug"] + ".log"), here / "build" / (meta["slug"] + ".log"))
                print("FAIL pdflatex (see build/%s.log)\n%s" % (meta["slug"], "\n".join(
                    l for l in r.stdout.splitlines() if l.startswith("!"))[:600]))
            else:
                shutil.copy(pdf, here / "pdf" / pdf.name)
                print("wrote pdf/%s" % pdf.name)
    for o in ops:
        n = sum(1 for b in o["flat"] if b[0] != "h")
        print("  %-22s %-45s %3d paragraphs, %2d footnotes, starts at *%s" % (
            o["ctx"].who, o["kindname"][:45], n, len(o["footnotes"]), o["start"] or "?"))
    for w in warnings:
        print("  note:", w)


def iter_tag(el, tag):
    for k in el.kids:
        if isinstance(k, El):
            if k.tag == tag:
                yield k
            yield from iter_tag(k, tag)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download a case to cases/*.json")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--cite", help='reporter citation, e.g. "463 U.S. 29"')
    g.add_argument("--cluster", type=int, help="CourtListener cluster id")
    f.set_defaults(func=fetch)
    b = sub.add_parser("build", help="cases/*.json -> md/, build/*.tex, pdf/")
    b.add_argument("json", help="file written by fetch")
    b.add_argument("--font", default="XCharter", help="LaTeX font package (default XCharter)")
    b.add_argument("--style", default="boxes", choices=["boxes", "rails"])
    b.add_argument("--no-pdf", action="store_true", help="skip pdflatex")
    b.set_defaults(func=build)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
