"""lawtext.py -- the text of a cited provision, from what is stored locally.

U.S.C. sections from the US Code XML in xml/ (usc2md.py's parser); C.F.R. sections and parts
from eCFR's title XML in <law_dir>/cfr/ (cfr.py fetch); Colorado statutes and constitution from
the OLLS title files in <law_dir>/co/crs/ (statelaw.py). The U.S. Constitution, the Code of Colorado
Regulations and other states come once their local copies exist (scope-scotus-law.md §3); until then text_of() returns None and
the caller says the text is not local.
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
    if el is None:
        return None
    node = usc2md.build(el, el.get("identifier"))
    rows = []
    for ch in node.children:
        _flatten(ch, 0, rows)
    if not node.children:
        lead = " ".join(x for x in (node.chapeau[1], node.content[1]) if x)
        rows = [(0, "", "", lead)] + [(0, "", "", t) for _, t in node.more]
    return node.heading[1], rows


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


# ---------------------------------------------------------------- Colorado (statelaw.py fetches the OLLS title files)

def _co_paras(title):
    """An OLLS C.R.S. title file (Word-filtered HTML) -> [(text, bold_start, centered)], or None if not local."""
    import html
    key = "co" + title
    if key not in _ROOTS:
        from tribonian import statelaw
        path = statelaw.state_dir("CO") / "crs" / ("title-%s.htm" % statelaw.crs_title_name(title))
        paras = None
        if path.exists():
            raw = path.read_text(encoding="cp1252", errors="replace")
            paras = []
            for m in re.finditer(r"<p\b([^>]*)>(.*?)</p>", raw, re.S | re.I):
                body = m.group(2)
                text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", body)).split())
                if text:
                    paras.append((text, re.match(r"\s*(<span[^>]*>\s*)?<b>", body, re.I) is not None, "center" in m.group(1)))
        _ROOTS[key] = paras
    return _ROOTS[key]


CO_LABEL_RE = re.compile(r"\(([0-9]+(?:\.[0-9]+)?|[a-z]+(?:\.[0-9]+)?|[IVXL]+(?:\.[0-9]+)?|[A-Z](?:\.[0-9]+)?)\)")


def _co_depth(label, prev_cap):
    """C.R.S. paragraphs: (1) 0, (a) 1, (I) 2, (A) 3. (I), (V), (X) are letters after (H), (U), (W)."""
    lab = label.split(".")[0]
    if lab.isdigit():
        return 0
    if lab.islower():
        return 1
    if set(lab) <= set("IVXL") and not (len(lab) == 1 and prev_cap == chr(ord(lab) - 1)):
        return 2
    return 3


def _co_rows(paras, start, stop):
    """Body paragraphs -> rows; the Source note in small type; annotations left out."""
    rows, depth, cap = [], 0, None
    for text, bold, centered in paras[start:stop]:
        if text.startswith("Source:") or text.startswith("Editor's note") or text.startswith("Cross references"):
            rows.append((0, "", "", "{\\footnotesize %s}" % c.esc(text, "tex")))
            continue
        labels = []
        rest = text
        while True:
            m = CO_LABEL_RE.match(rest)
            if not m:
                break
            labels.append(m.group(1))
            rest = rest[m.end():].lstrip()
        if labels:
            depth = _co_depth(labels[-1], cap)
            if depth == 3:
                cap = labels[-1].split(".")[0]
        rows.append((depth if labels else 0, "", "", c.esc(text, "tex")))
    return rows


def _co_end(paras, i, is_start):
    """Index of the paragraph after a section's text: the next section, a centered heading, or ANNOTATION."""
    j = i + 1
    while j < len(paras):
        text, bold, centered = paras[j]
        if centered or is_start(paras[j]) or text == "ANNOTATION":
            break
        j += 1
    return j


CRS_START = re.compile(r"^(\d+(?:\.\d+)?-\d+(?:\.\d+)?-\d+(?:\.\d+)?)\.\s+(.*)")


def co_crs(section):
    """'26.5-4-205' -> (heading, rows) from the local OLLS title file, or None."""
    from tribonian import statelaw
    paras = _co_paras(section.split("-")[0])
    if paras is None:
        return None
    is_start = lambda p: p[1] and CRS_START.match(p[0]) is not None
    i = next((k for k, p in enumerate(paras) if is_start(p) and CRS_START.match(p[0]).group(1) == section), None)
    if i is None:
        return None
    head = c.esc(CRS_START.match(paras[i][0]).group(2), "tex")
    meta = statelaw.local_meta(statelaw.state_dir("CO") / "crs" / ("title-%s.htm" % statelaw.crs_title_name(section.split("-")[0]))) or {}
    when = " {\\normalfont\\footnotesize (C.R.S. %s)}" % meta["edition"] if meta.get("edition") else ""
    return head + when, _co_rows(paras, i + 1, _co_end(paras, i, is_start))


SEC_START = re.compile(r"^Section (\d+[a-z]?)\.\s+(.*)")


def co_const(article, section=None):
    """Colo. Const. art. IX, § 7 -> (heading, rows); the article alone lists its sections. From title 00."""
    from tribonian import lawcites, statelaw
    paras = _co_paras("0")
    if paras is None:
        return None
    roman = {v: k for k, v in lawcites.ROMAN.items()}.get(int(article))
    a = next((k for k, p in enumerate(paras) if p[0] == "ARTICLE %s" % roman and p[2]), None)
    if a is None:
        return None
    end = next((k for k in range(a + 1, len(paras)) if re.fullmatch(r"ARTICLE [IVXL]+", paras[k][0]) and paras[k][2]), len(paras))
    is_start = lambda p: SEC_START.match(p[0]) is not None
    meta = statelaw.local_meta(statelaw.state_dir("CO") / "crs" / "title-00.htm") or {}
    when = " {\\normalfont\\footnotesize (C.R.S. %s)}" % meta["edition"] if meta.get("edition") else ""
    if section is None:
        title = paras[a + 1][0] if a + 1 < end and paras[a + 1][2] else ""
        rows, seen = [], set()
        for k in range(a + 1, end):
            m = SEC_START.match(paras[k][0])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                rows.append((0, c.esc("§ " + m.group(1), "tex"), c.esc(m.group(2).rstrip("."), "tex"), ""))
        return c.esc(("Article %s. %s" % (roman, title.title())).strip(), "tex") + when, rows
    i = next((k for k in range(a + 1, end) if is_start(paras[k]) and SEC_START.match(paras[k][0]).group(1) == section), None)
    if i is None:
        return None
    return c.esc(SEC_START.match(paras[i][0]).group(2), "tex") + when, _co_rows(paras, i + 1, min(_co_end(paras, i, is_start), end))


def text_of(key):
    """A provision's local text: (heading, rows) or None."""
    p = key.split(":")
    try:                                         # a malformed or unexpected file: say "not local", do not fail the build
        if p[0] == "usc":
            return usc(p[1], p[2])
        if p[0] == "cfr":
            return cfr(p[1], p[2])
        if p[0] == "co-crs":
            return co_crs(p[1])
        if p[0] == "co-const":
            return co_const(p[1].split("-")[1], p[2] if len(p) > 2 else None)
    except Exception:
        return None
    return None


def fetch_hint(key):
    """The command that would store a provision's text locally, or '' when there is none yet."""
    p = key.split(":")
    if p[0] == "cfr":
        return "tribonian cfr fetch %s" % p[1]
    if p[0] == "usc":
        return "the US Code XML for title %s in xml/ (tribonian usc)" % p[1]
    if p[0] == "co-crs":
        return "tribonian state fetch CO crs %s" % p[1].split("-")[0]
    if p[0] == "co-const":
        return "tribonian state fetch CO crs 00"
    return ""
