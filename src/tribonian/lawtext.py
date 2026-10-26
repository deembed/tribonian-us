"""lawtext.py -- the text of a cited provision, from what is stored locally.

U.S.C. sections from the US Code XML in xml/ (usc2md.py's parser); C.F.R. sections and parts
from eCFR's title XML in <law_dir>/cfr/ (cfr.py fetch). The Constitution and state codes come
once their local copies exist (scope-scotus-law.md §3); until then text_of() returns None and
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


def text_of(key):
    """A provision's local text: (heading, rows) or None."""
    p = key.split(":")
    try:                                         # a malformed or unexpected file: say "not local", do not fail the build
        if p[0] == "usc":
            return usc(p[1], p[2])
        if p[0] == "cfr":
            return cfr(p[1], p[2])
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
    return ""
