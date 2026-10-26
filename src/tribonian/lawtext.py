"""lawtext.py -- the text of a cited provision, from what is stored locally.

Today: U.S.C. sections from the US Code XML in xml/ (usc2md.py's parser). The Constitution,
C.F.R. and state codes come once their local databases exist (scope-scotus-law.md §3);
until then text_of() returns None and the caller says the text is not local.
"""
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


def text_of(key):
    """A provision's local text: (heading, rows) or None."""
    p = key.split(":")
    if p[0] == "usc":
        try:
            return usc(p[1], p[2])
        except Exception:                        # a malformed or unexpected XML file: say "not local", do not fail the build
            return None
    return None
