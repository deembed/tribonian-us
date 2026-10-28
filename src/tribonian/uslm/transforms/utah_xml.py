"""UT.xml2html.py -- throwaway prototype of the Utah code/constitution XML -> HTML-blocks transform (batch 3c).

Spec for the shared pre-pass ("xml-to-blocks"), see UT.status.md. Every text node of the XML is copied, in
document order, into <p>/<h1>/<h2> blocks for the html-paragraphs family; the only text ADDED is labels taken
from attributes (container/section/subsection numbers) and version markers from <effdate>/<enddate>. The only
text DROPPED is <modyear> (the year again, already in <history>). check() proves both.
"""
import html, re, sys
import xml.etree.ElementTree as ET

CONTAINERS = {"title", "const", "chapter", "part", "subpart", "article"}   # numbered levels above the section
DROP = {"modyear"}                                                          # redundant: the year of <history>
SKIP_META = {"effdate", "enddate", "contingent", "histories", "catchline"}  # read by the section, not inline
INLINE = {"xref", "modchap", "bold"}                                         # text kept, tag dropped


class Out:
    def __init__(self):
        self.blocks, self.cur = [], None

    def start(self, tag="p", cls=None, lead=""):
        self.end()
        self.cur = [tag, cls, [lead] if lead else []]

    def text(self, t):
        if t:
            if self.cur is None:
                self.start()
            self.cur[2].append(html.escape(t))

    def end(self):
        if self.cur is not None:
            tag, cls, parts = self.cur
            body = re.sub(r"\s+", " ", "".join(parts)).strip()
            if body:
                self.blocks.append('<%s%s>%s</%s>' % (tag, ' class="%s"' % cls if cls else "", body, tag))
            self.cur = None


def inline(el, out):
    """el's text and its inline children, in order (tail handled by the caller)."""
    out.text(el.text)
    for c in el:
        walk(c, out, None)
        out.text(c.tail)


def paren_label(num, parent_num):
    """'3-1-2(10)(a)' under '3-1-2(10)' -> '(a)'; the last parenthesised group."""
    m = re.search(r"(\([^()]+\))$", num)
    return m.group(1) if m else num[len(parent_num):]


def walk(el, out, parent_num):
    tag = el.tag
    if tag in DROP or tag in SKIP_META:
        return
    if tag == "tab":
        out.text(" ")
        return
    if tag == "eol":
        out.end()
        return
    if tag in INLINE:
        inline(el, out)
        return
    num = el.get("number", "")
    if tag in CONTAINERS:
        cat = el.findtext("catchline") or ""
        if tag in ("title", "const"):          # the document's own level: its heading as an <h1> ("Title 3 Uniform
            out.start("h1")                     # ..."; the constitution's bare name) -- build merges it into the title
            out.text("Title %s %s" % (num, cat) if tag == "title" else (cat or "Constitution"))
            out.end()
            for c in el:                        # the title version's own dates (77: <effdate>7/1/2026</effdate>)
                if c.tag in ("effdate", "enddate") and (c.text or "").strip():
                    out.start()
                    out.text("[%s %s]" % ("Effective" if c.tag == "effdate" else "Effective until", c.text.strip()))
                    out.end()
            for c in el:
                walk(c, out, num)
            out.end()
            return
        elif tag == "article":
            head = "%s %s" % (num, cat) if num.startswith("Article") else cat     # "Article I Declaration ..."; Preamble
            out.start("h2")
        else:
            short = num.rsplit("-", 1)[-1]                                       # chapter "3-1" -> 1, part "26B-1-2" -> 2
            head = "%s %s %s" % (tag.capitalize(), short, cat)
            out.start("h2")
        out.text(head)
        out.end()
        for c in el:
            walk(c, out, num)
            if c.tail and c.tail.strip():
                out.text(c.tail)
        out.end()
        return
    if tag == "section":
        cat = el.findtext("catchline") or ""
        marks = []
        if el.find("effdate") is not None:
            marks.append("[Effective %s]" % el.findtext("effdate").strip())
        if el.find("enddate") is not None:
            marks.append("[Effective until %s]" % el.findtext("enddate").strip())
        lab = re.sub(r"^Article [^,]+, ", "", num)                               # constitution: "Section 8"
        lab = ("§ %s." % lab) if not lab.startswith("Section") else (lab + ".")
        out.start("p", lead="<b>%s</b>" % html.escape(" ".join([lab, cat] + marks)))
        inline(el, out)
        out.end()
        for h in el.iter("history"):
            out.start("p", "history")
            inline(h, out)
            out.end()
        return
    if tag == "subsection":
        out.start("p", lead=html.escape(paren_label(num, parent_num or "")) + " ")
        out.text(el.text)
        for c in el:
            walk(c, out, num)
            if c.tail and c.tail.strip():
                if out.cur is None:
                    out.start()                                                  # text after a nested level
                out.text(c.tail)
        out.end()
        return
    if tag == "arttext":                                                         # an article with no sections
        inline(el, out)
        out.end()
        return
    raise SystemExit("UT.xml2html: unknown element <%s>" % tag)


def convert(raw):
    root = ET.fromstring(raw)
    out = Out()
    walk(root, out, None)
    out.end()
    return "<html><body><div id=\"law\">\n" + "\n".join(out.blocks) + "\n</div></body></html>"


def title_of(raw):
    """(num_text, num, heading) of the root element, for the build's title node: ("Title 3", "3", "Uniform ...")."""
    root = ET.fromstring(raw)
    num = root.get("number", "")
    return ("Title %s" % num if root.tag == "title" else ""), (num if root.tag == "title" else ""), root.findtext("catchline") or ""


def words(s):
    return re.findall(r"\w+", s)


def check(raw, h):
    """every XML word (but modyear) is in the HTML (as a multiset: <histories> come before the catchline in the
    XML and after the text in the HTML); returns (ok, missing words, added words)."""
    from collections import Counter
    root = ET.fromstring(raw)
    for el in root.iter("modyear"):
        el.text = ""
    src = Counter(words(" ".join(root.itertext())))
    dst = Counter(words(re.sub(r"<[^>]+>", " ", html.unescape(h)) + " " + " ".join(title_of(raw)[::2])))
    return not (src - dst), dict(src - dst), dict((dst - src).most_common(12))


if __name__ == "__main__":
    raw = open(sys.argv[1], "rb").read()
    h = convert(raw)
    print(check(raw, h), file=sys.stderr)
    open(sys.argv[2], "w", encoding="utf-8").write(h)


# --- tags beyond titles 3/18 and the constitution (titles 57, 77; batch 3d), and the subsection depth hint ---
INLINE |= {"ext_ref"}                      # Utah Constitution / Laws of Utah cites: text kept
SKIP_META |= {"highlight", "badspell"}     # empty editor markers: no text
_walk_base = walk


def walk(el, out, parent_num):            # noqa: F811 -- extends the walk above
    if el.tag in ("center", "right"):      # form titles, compact "ARTICLE I", signature lines: a block of their own
        out.start("p", el.tag)
        inline(el, out)
        out.end()
        return
    if el.tag == "format":                 # a form's transparent wrapper: block breaks before and after
        out.end()
        inline(el, out)
        out.end()
        return
    if el.tag == "subsection":             # depth from the number's parentheses -> margin-left (indent_css 10pt)
        prev = out.start
        depth = el.get("number", "").count("(") - 1
        out.start = lambda tag="p", cls=None, lead="": Out.start(out, tag, "ut-d%d" % depth, lead)
        try:
            return _walk_base(el, out, parent_num)
        finally:
            out.start = prev
    return _walk_base(el, out, parent_num)


_convert_base = convert


def convert(raw):                          # noqa: F811
    return re.sub(r'class="ut-d(\d+)"', lambda m: 'style="margin-left:%dpt"' % (10 * int(m.group(1))), _convert_base(raw))
