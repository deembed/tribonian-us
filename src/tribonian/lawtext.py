"""lawtext.py -- the text of a cited provision, from what is stored locally.

U.S.C. sections from the US Code XML in xml/ (usc2md.py's parser); C.F.R. sections and parts
from eCFR's title XML in <law_dir>/cfr/ (cfr.py fetch); the U.S. Constitution from govinfo's
House Document 110-50 in <law_dir>/us/const/. Every state's statutes, constitution and regulations
from the USLM that `tribonian state build` writes (<law_dir>/<st>/uslm/<code>/<item>.xml), found
through the code's [cite] table (statecites.py). Text that is not local: text_of() returns None
and fetch_hint() says what gets it, or why nothing can (a code with no source).
"""
import re

from tribonian import case2md as c

_ROOTS = {}


def _usc_root(title):
    if title in _ROOTS:
        return _ROOTS[title]
    from lxml import etree
    from tribonian import usc2md
    t = title.upper()
    name = "usc%02d%s.xml" % (int("".join(ch for ch in t if ch.isdigit())), "".join(ch for ch in t if ch.isalpha()))
    path = c.ROOT / "xml" / name
    root = None
    if path.exists():
        parser = etree.XMLParser(huge_tree=True, remove_comments=True)
        root = usc2md.strip_ns(etree.parse(str(path), parser)).getroot()
    _ROOTS[title] = root
    return root


def _flatten(n, depth, out):
    """usc2md Node tree -> [(depth, label, heading, text)] in reading order (tex strings)."""
    lead = " ".join(x for x in (n.chapeau[1], n.content[1]) if x)
    out.append((depth, n.tail or n.label, n.heading[1], lead))
    for _, t in n.more:
        out.append((depth, "", "", t))
    for ch in n.children:
        _flatten(ch, depth + 1, out)
    if n.continuation[1]:
        out.append((depth, "", "", n.continuation[1]))


def usc(title, section):
    """-> (heading, [(depth, label, heading, text)]) in TeX, or None when the title's XML is not local."""
    from tribonian import usc2md
    root = _usc_root(title)
    if root is None:
        return None
    el = usc2md.find_by_identifier(root, "/us/usc/t%s/s%s" % (title.lower(), section), "section")
    if el is None and "-" in section:            # the Code's identifiers write 2000bb–1 with an en dash
        el = usc2md.find_by_identifier(root, "/us/usc/t%s/s%s" % (title.lower(), section.replace("-", "\u2013")), "section")
    if el is None:
        return None
    node = usc2md.build(el, el.get("identifier"))
    rows = []
    for ch in node.children:
        _flatten(ch, 0, rows)
    if not node.children:
        lead = " ".join(x for x in (node.chapeau[1], node.content[1]) if x)
        rows = [(0, "", "", lead)] + [(0, "", "", t) for _, t in node.more]
    fn = lambda t: re.sub("\x00FN:[^\x00]*\x00", "", t or "")    # usc2md's footnote placeholders: its notes are not printed here
    return fn(node.heading[1]), [(d, fn(l), fn(h), fn(t)) for d, l, h, t in rows]


# ---------------------------------------------------------------- C.F.R. (cfr.py fetches eCFR's title XML)

def _cfr_root(title):
    key = "cfr" + title
    if key not in _ROOTS:
        from lxml import etree
        from tribonian import cfr
        path, root = cfr.cfr_dir() / ("title-%s.xml" % title), None
        if path.exists():
            root = etree.parse(str(path), etree.XMLParser(huge_tree=True, remove_comments=True)).getroot()
        _ROOTS[key] = root
    return _ROOTS[key]


def _inline(el):
    """eCFR inline markup -> TeX: <I> and <E> italic, the rest plain."""
    out = [c.esc(el.text or "", "tex")]
    for k in el:
        inner = _inline(k)
        out.append("\\emph{%s}" % inner if k.tag in ("I", "E") and inner.strip() else inner)
        out.append(c.esc(k.tail or "", "tex"))
    return " ".join("".join(out).split())


LABEL_RE = re.compile(r"\(([a-z]{1,4}|[0-9]{1,3}|[A-Z]{1,2})\)")
ROMAN_CH = set("ivxl")


def _depth(label, prev_letter):
    """(a) 0, (1) 1, (i) 2, (A) 3 -- eCFR's paragraph scheme. (i), (v), (x) are letters when they follow
    (h), (u), (w) at the top level."""
    if label.isdigit():
        return 1
    if label.isupper():
        return 3
    if set(label) <= ROMAN_CH and not (len(label) == 1 and prev_letter == chr(ord(label) - 1)):
        return 2
    return 0


def cfr(title, section):
    """A section ('104.4') or a part ('pt-104') of a C.F.R. title -> (heading, rows) in TeX, or None when
    the title is not local or has no such provision. A part lists its sections."""
    from tribonian import cfr as fetcher
    root = _cfr_root(title)
    if root is None:
        return None
    meta = fetcher.local(title) or {}
    when = " {\\normalfont\\footnotesize (eCFR, current as of %s)}" % c.long_date(meta["date"]) if meta.get("date") else ""
    if section.startswith("pt-"):
        part = root.find(".//DIV5[@N='%s']" % section[3:])
        if part is None:
            return None
        head = c.esc(" ".join((part.findtext("HEAD") or "").split()), "tex")
        rows = []
        for sec in part.iter("DIV8"):
            h = " ".join((sec.findtext("HEAD") or "").split())
            m = re.match(r"§\s*(\S+)\s+(.*)", h)
            rows.append((0, c.esc("§ " + m.group(1), "tex") if m else "", c.esc(m.group(2) if m else h, "tex").rstrip("."), ""))
        return head + when, rows
    sec = root.find(".//DIV8[@N='%s']" % section)
    if sec is None:
        return None
    h = " ".join((sec.findtext("HEAD") or "").split())
    head = c.esc(re.sub(r"^§\s*\S+\s+", "", h), "tex") + when
    rows, depth, letter = [], 0, None
    for el in sec:
        if el.tag in ("P", "FP"):
            t = _inline(el)
            m = LABEL_RE.match(t)
            if m:
                depth = _depth(m.group(1), letter)
                if depth == 0:
                    letter = m.group(1)
            rows.append((depth if m else 0, "", "", t))
        elif el.tag in ("HD1", "HD2", "HD3"):
            rows.append((0, "", _inline(el), ""))
        elif el.tag == "EXTRACT":
            rows += [(depth + 1, "", "", _inline(p)) for p in el.iter("P", "FP")]
        elif el.tag in ("GPOTABLE", "TABLE", "DIV") or el.find(".//TABLE") is not None:
            rows.append((depth, "", "", "\\emph{[A table: see the section on ecfr.gov.]}"))
        elif el.tag in ("NOTE", "EDNOTE"):
            rows += [(depth, "", "", "\\emph{%s}" % _inline(p)) for p in el.iter("P")]
        elif el.tag == "CITA":
            rows.append((0, "", "", "{\\footnotesize %s}" % _inline(el)))
    return head, rows


# ---------------------------------------------------------------- state law: one reader for every state, from USLM

def _E(t, fmt):
    return c.esc(t, fmt)


def _small(t, fmt):
    """A note line (Source:, Cross references): small type in TeX, italic in Markdown."""
    return "{\\footnotesize %s}" % t if fmt == "tex" else "*%s*" % t


def _uslm(path):
    """A built USLM file -> its root (namespace stripped), cached."""
    key = "uslm:%s" % path
    if key not in _ROOTS:
        from lxml import etree
        root = etree.parse(str(path), etree.XMLParser(huge_tree=True, remove_comments=True)).getroot()
        for el in root.iter():
            if isinstance(el.tag, str) and "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
        _ROOTS[key] = root
    return _ROOTS[key]


def _ptext(el):
    return " ".join("".join(el.itertext()).split())


LEVELS = {"subsection", "paragraph", "subparagraph", "clause", "subclause", "item", "subitem", "subsubitem", "level"}
SKIP_NOTES = {"annotation", "toc"}


def _uslm_rows(el, fmt, depth=0, rows=None):
    """A USLM section (or level) -> [(depth, label, heading, text)]: its chapeau, content and continuation
    paragraphs; nested levels one deeper, their number as the label of their first line; source credits and
    editorial notes in small type; case annotations and tables of contents left out."""
    rows = [] if rows is None else rows
    for k in el:
        if not isinstance(k.tag, str) or k.tag in ("num", "heading"):
            continue
        if k.tag in LEVELS:
            num = k.find("num")
            head = k.find("heading")
            label = _E(_ptext(num), fmt) if num is not None else ""
            first = len(rows)
            d = depth if el.tag == "section" else depth + 1   # a section's subsections at 0, as the Code prints them
            _uslm_rows(k, fmt, d, rows)
            if len(rows) > first:                              # "(1) (a) The department ...": one line
                d0, lab0, h, t = rows[first]
                rows[first] = (min(d0, d), (label + " " + lab0).strip(), _E(_ptext(head), fmt) if head is not None else h, t)
            else:
                rows.append((d, label, _E(_ptext(head), fmt) if head is not None else "", ""))
        elif k.tag in ("chapeau", "content", "continuation", "proviso"):
            ps = [p for p in k.iter("p")] or [k]
            for p in ps:
                t = _ptext(p)
                if t:
                    rows.append((depth, "", "", _E(t, fmt)))
        elif k.tag == "sourceCredit":
            t = _ptext(k)
            if t:
                rows.append((depth, "", "", _small(_E(t, fmt), fmt)))
        elif k.tag == "note":
            if k.get("topic") in SKIP_NOTES or k.get("role") in ("crossHeading",):
                continue
            for p in k.iter("p"):
                t = _ptext(p)
                if t and t != "ANNOTATION":
                    rows.append((depth, "", "", _small(_E(t, fmt), fmt)))
        elif k.tag == "p":
            t = _ptext(k)
            if t:
                rows.append((depth, "", "", _E(t, fmt)))
    return rows


def _num(el):
    n = el.find("num")
    return (n.get("value") or _ptext(n)).strip().rstrip(".") if n is not None else ""


def _same(a, b):
    """Section or article numbers equal: case, leading zeros and Roman vs Arabic ("XIV" = "14") aside."""
    from tribonian import statecites
    if a.casefold().lstrip("0") == b.casefold().lstrip("0") or a.casefold() == b.casefold():
        return True
    return statecites.arabic(a) == statecites.arabic(b) != ""


def _article_of(sec):
    """The article (or level) number a constitution's section sits in: from its identifier (/a2/s1) or an ancestor."""
    from tribonian import statecites
    m = re.search(r"/(?:a|ar|art|t)([^/]+)/s[^/]+$", sec.get("identifier", ""))
    if m:
        return statecites.arabic(m.group(1))
    p = sec.getparent()
    while p is not None:
        if p.tag in ("article", "level", "chapter", "part", "title") and p.find("num") is not None:
            return statecites.arabic(_num(p))
        p = p.getparent()
    return ""


def article_heading(tb, art):
    """A constitution article's own heading ("Revenue"), or ''."""
    from tribonian import statecites
    path, _ = state_file(tb, {"art": art})
    if path is None:
        return ""
    for x in _uslm(path).iter("section"):
        if _same(_article_of(x), art):
            p = x.getparent()
            while p is not None and p.find("heading") is None:
                p = p.getparent()
            return _ptext(p.find("heading")).title() if p is not None else ""
    return ""


def _when(root, path, fmt):
    """'(retrieved 2026-10-28; 2025 edition)' from the USLM meta and the source's .json."""
    props = {p.get("role"): (p.text or "").strip() for p in root.iter("property")}
    ed = props.get("edition")
    got = props.get("retrieved")
    bits = []
    if ed and ed not in ("current", "None"):
        bits.append("%s edition" % ed)
    if got:                                      # index builds record "<layer> fetched <date>; ...": the dates only
        dates = list(dict.fromkeys(re.findall(r"\d{4}-\d\d-\d\d", got))) or [got]
        bits.append("retrieved %s" % ", ".join(dates))
    if not bits:
        return ""
    t = "current text, " + "; ".join(bits)
    return " {\\normalfont\\footnotesize (%s)}" % t if fmt == "tex" else ""


def state_file(tb, vals):
    """(path to the item's USLM, item) for a parsed state key: the first of the code's formulas (and `also`)
    that has the item built here; (None, item) when none has."""
    from tribonian import statelaw
    item = tb.item_of(vals)
    if item is None:
        return None, None
    for code in [tb.code] + tb.also:
        p = statelaw.uslm_file(tb.st, code, item)
        if p is not None:
            return p, item
    return None, item


def state(key, fmt="tex"):
    """Any state's statute section, constitution article/section or rule (statecites key) -> (heading, rows) from
    its built USLM, or None. A constitution's article alone lists its sections; a rule alone gives every section."""
    from tribonian import statecites
    tb, vals = statecites.parse(key)
    if tb is None or vals is None or tb.source_error:
        return None
    path, item = state_file(tb, vals)
    if path is None:
        return None
    root = _uslm(path)
    when = _when(root, path, fmt)
    sec = vals.get("sec")
    secs = list(root.iter("section"))
    want = statecites.fill(tb.num_t, statecites.with_roman(vals)) if tb.num_t and sec is not None else None
    if tb.ident_t:                                         # the number repeats in the file: only this act's sections
        frag = statecites.fill(tb.ident_t, vals)
        if frag:
            secs = [x for x in secs if frag.lower() in x.get("identifier", "").lower()]
    if want is not None and ("{art}" in tb.num_t or "{roman}" in tb.num_t or tb.kind != "const"):
        hit = next((x for x in secs if _same(_num(x), want)), None)
    elif tb.kind == "const":
        art = vals.get("art", "")
        inart = [x for x in secs if _same(_article_of(x), art)]
        if sec is None:
            if not inart:
                return None
            rows = [(0, _E("§ " + _num(x), fmt), _E(_ptext(x.find("heading")) if x.find("heading") is not None else "", fmt).rstrip("."), "")
                    for x in inart]
            return _E(tb.label({"art": art}), fmt) + when, rows
        hit = next((x for x in inart if _same(_num(x), sec)), None)
    elif sec is None:                                     # a whole rule (regs) or item
        rows = []
        for x in secs:
            h = x.find("heading")
            rows.append((0, _E("§ " + _num(x), fmt), _E(_ptext(h), fmt) if h is not None else "", ""))
            rows += _uslm_rows(x, fmt, 1)
        lvl = next((e for e in root.iter() if e.tag in ("title", "level", "chapter") and e.find("heading") is not None), None)
        return (_E(_ptext(lvl.find("heading")), fmt) if lvl is not None else _E(tb.label(vals), fmt)) + when, rows
    else:
        sub = vals.get("sub")
        hits = [x for x in secs if _same(_num(x), sec)]
        if len(hits) > 1 and sub:                          # sections repeat across sub-codes in one file: by identifier
            hits = [x for x in hits if ("/%s" % sub.lower()) in x.get("identifier", "").lower()] or hits
        hit = hits[0] if hits else None
    if hit is None:
        return None
    h = hit.find("heading")
    rows = _uslm_rows(hit, fmt)
    return (_E(_ptext(h), fmt).rstrip(".") if h is not None else "") + when, rows


def state_hint(key):
    """Why a state key has no text: its source error, the fetch command, or that the item has no such section."""
    from tribonian import statecites
    tb, vals = statecites.parse(key)
    if tb is None or vals is None:
        return ""
    if tb.source_error:
        return "source error: %s" % tb.source_error
    path, item = state_file(tb, vals)
    if item is None:
        return "no item for %s (its [cite] table has no item rule)" % tb.label(vals)
    if path is not None:
        return "not in %s (renumbered or repealed since, or a cite we misread)" % path.name
    return "tribonian state fetch %s %s %s" % (tb.st, tb.fetch_code() or tb.code, item)


# ---------------------------------------------------------------- the U.S. Constitution (govinfo, H. Doc. 110-50)

def _us_const():
    """-> {("art"|"amend", n): [paragraphs]}, or None when not local. The House Document's text rendition:
    a <pre> block; the original articles, then the amendments after "ARTICLES IN ADDITION TO ...". Footnote
    blocks (between rules of dashes), "Proposal and Ratification" notes, footnote marks (\\12\\) and the
    signers are left out."""
    import html
    if "uscon" not in _ROOTS:
        from tribonian import statelaw
        path, parts = statelaw.state_dir("US") / "const" / "constitution.htm", None
        if path.exists():
            t = html.unescape(re.sub(r"<[^>]+>", "", path.read_text(encoding="utf-8", errors="replace")))
            t = t[t.find("We the People"):]
            t = re.sub(r"\n-{20,}\n.*?\n-{20,}\n", "\n", t, flags=re.S)          # footnote blocks
            t = re.sub(r"\\[0-9a-z]+\\", "", t)                                   # \12\ marks, W\m\ superscripts
            amend_at = t.find("ARTICLES IN ADDITION TO")
            parts = {}
            heads = list(re.finditer(r"^\s+Article \[?([IVXL]+)\.?\]?\.?\s*$", t, re.M))
            for i, m in enumerate(heads):
                end = heads[i + 1].start() if i + 1 < len(heads) else len(t)
                kind = "amend" if m.start() > amend_at > 0 else "art"
                if kind == "art" and amend_at > 0:
                    end = min(end, amend_at)
                body = t[m.end():end]
                for stop in ("Proposal and Ratification", "done in Convention", "ARTICLES IN ADDITION"):
                    body = body.split(stop)[0]
                paras, cur = [], []
                for line in body.split("\n"):
                    if not line.strip():
                        continue
                    if re.match(r"\s{4}\S", line) and cur:      # an indented line starts a paragraph
                        paras.append(" ".join(" ".join(cur).split()))
                        cur = []
                    cur.append(line.strip())
                if cur:
                    paras.append(" ".join(" ".join(cur).split()))
                from tribonian import lawcites
                parts[(kind, lawcites.ROMAN.get(m.group(1)))] = paras
            pre = t[:heads[0].start()] if heads else ""
            parts[("preamble", 0)] = [" ".join(pre.split())]
        _ROOTS["uscon"] = parts
    return _ROOTS["uscon"]


def us_const(kind, n, section=None, fmt="tex"):
    """const:amend-1, const:art-3 (every section), const:art-3:2 -> (heading, rows), or None."""
    from tribonian import statelaw
    parts = _us_const()
    if not parts or (kind, int(n)) not in parts:
        return None
    meta = statelaw.local_meta(statelaw.state_dir("US") / "const" / "constitution.htm") or {}
    roman = {v: k for k, v in __import__("tribonian.lawcites", fromlist=["ROMAN"]).ROMAN.items()}[int(n)]
    head = ("Amendment %s" if kind == "amend" else "Article %s") % roman
    head += " {\\normalfont\\footnotesize (%s)}" % meta["edition"] if meta.get("edition") and fmt == "tex" else ""
    rows, cur = [], None
    for p in parts[(kind, int(n))]:
        m = re.match(r"Section (\d+)\.\s*(.*)", p)
        if m:
            cur = m.group(1)                     # a section's later clauses are paragraphs of their own
        if section is not None and cur != str(section):
            continue
        rows.append((0, _E("§ %s" % m.group(1), fmt) if m else "", "", _E(m.group(2) if m else p, fmt)))
    return (head, rows) if rows else None


def text_of(key):
    """A provision's local text: (heading, rows) or None."""
    p = key.split(":")
    try:                                         # a malformed or unexpected file: say "not local", do not fail the build
        if p[0] == "usc":
            return usc(p[1], p[2])
        if p[0] == "cfr":
            return cfr(p[1], p[2])
        if p[0] == "const":
            kind, n = p[1].split("-")
            return us_const(kind, n, p[2] if len(p) > 2 else None)
        if "-" in p[0]:
            return state(key)
    except Exception:
        return None
    return None


def fetch_spec(key):
    """What would store a provision's text locally: ("cfr", title) | ("state", state, code, item) | None
    (None for a code with no source: its hint says why)."""
    p = key.split(":")
    if p[0] == "cfr":
        return ("cfr", p[1])
    if p[0] == "const":
        return ("state", "US", "const", "")
    if "-" in p[0]:
        from tribonian import statecites
        tb, vals = statecites.parse(key)
        if tb is None or vals is None or tb.source_error:
            return None
        item, code = tb.item_of(vals), tb.fetch_code()
        return ("state", tb.st, code, item) if item is not None and code else None
    return None


def fetch_hint(key):
    """The command that would store a provision's text locally, a source error, or ''."""
    p = key.split(":")
    if "-" in p[0]:
        return state_hint(key)
    spec = fetch_spec(key)
    if spec is None:
        return "the US Code XML for title %s in xml/ (tribonian usc)" % p[1] if p[0] == "usc" else ""
    return "tribonian cfr fetch %s" % spec[1] if spec[0] == "cfr" else ("tribonian state fetch %s %s %s" % spec[1:]).rstrip()


def is_state_spec(spec):
    return spec is not None and spec[0] == "state" and spec[1] != "US"


def run_fetch(spec):
    """Fetch what spec names, in this process. -> (True, '') or (False, why). Never raises and never exits:
    the fetchers sys.exit on refusals (robots.txt, HTTP errors, no such title), which becomes the reason;
    a dropped connection or timeout mid-download likewise. A half-written file stays as *.part, never
    under the real name."""
    try:
        if spec[0] == "cfr":
            from tribonian import cfr
            known, _ = cfr.titles()
            if spec[1] not in known:
                return False, "no title %s in eCFR's list" % spec[1]
            cfr.fetch_one(spec[1], known[spec[1]])
        else:
            from tribonian import statelaw
            if (spec[1], spec[2]) in statelaw.formulas():     # every state code: fetch the item, build its USLM
                statelaw.fetch_and_build(spec[1], spec[2], spec[3])
            else:
                reader = statelaw.FETCHERS.get((spec[1], spec[2]))
                if reader is None:
                    return False, "no reader for %s %s yet" % (spec[1], spec[2])
                reader([spec[3]] if spec[3] else [])
    except SystemExit as e:
        return False, str(e.code) if e.code not in (None, 0) else "stopped"
    except KeyboardInterrupt:
        raise
    except Exception as e:                        # network gone, timeout, a page that changed shape
        return False, "%s: %s" % (type(e).__name__, e)
    _ROOTS.clear()                               # read the new files, not the cached "not local"
    return True, ""
