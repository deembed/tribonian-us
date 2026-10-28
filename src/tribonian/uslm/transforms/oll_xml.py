"""oll_xml.py -- Open Law Library XML (the DC Council's law-xml repo) -> one HTML page per title, for the
html-paragraphs family. Input: a title's index.xml (nested <container>s, <subheading>s, xi:includes of section
files); output: <h1..h5> container lines ("Chapter 1", then its heading), <h6> subheadings, <p><b>NUM HEADING</b></p>
section starts, <p>(a) text</p> lines, <p class="atype">TYPE</p> + <p class="annotation"> notes.

Element map (every element name in the whole repo, scanned 2026-10-28):
  container: prefix num heading | container | subheading | xi:include | text (a removed chapter's note)
  section:   prefix? num reason? heading | text | para | aftertext | annotations | annotation (loose, title 99)
  para:      prefix? num heading? (the line's lead) | text | para | aftertext | include (a wrapper) | codify:insert
             (an empty amendment marker, skipped)
  annotations: annotation | text (both typed by @type)
  inline (flattened to text): cite code-cite span em strong b u e s sup center br num text img Cite TODO;
  table (kept as a <table> block; formula verbatim = ["table"]); img has no text (an image of a form/table:
  nothing to keep; counted in the report line)
Lossless: the non-space characters of the XML (includes spliced in) equal those of the HTML minus the atype
labels; the only reordering is a section's <reason> ("Repealed"), read after the heading. Unknown elements raise.
usage: python3 oll_xml.py INDEX.xml SECTIONS_DIR OUT.html   (includes are resolved beside INDEX.xml first)"""
import html, sys
from pathlib import Path
from lxml import etree

NS = "{https://code.dccouncil.us/schemas/dc-library}"
XI = "{http://www.w3.org/2001/XInclude}include"
LEAD = ("prefix", "num", "heading", "reason")
LINES = ("text", "aftertext", "insert")
images = [0]


CODIFY = "{https://code.dccouncil.us/schemas/codify}"


def local(e):
    """element name without namespace; "" for comments and for codify:* instructions (codify:insert
    after="(h)": an empty marker of where an amendment went -- no text, checked)"""
    if not isinstance(e.tag, str):
        return ""
    if e.tag.startswith(CODIFY):
        if "".join(e.itertext()).strip():
            raise SystemExit("oll-xml: <codify:%s> holds text" % e.tag[len(CODIFY):])
        return ""
    return e.tag.split("}")[-1]


def inline(e):
    """an element's text, inline markup flattened (br -> space); tables kept as tables"""
    out = [html.escape(e.text or "")]
    for c in e:
        t = local(c)
        if t == "table":
            out.append(" " + etree.tostring(c, encoding="unicode", with_tail=False).replace(' xmlns="%s"' % NS[1:-1], "") + " ")
        elif t == "img":
            images[0] += 1
        elif t == "br":
            out.append(" ")
        elif t:
            out.append(" " + inline(c) + " " if t in ("center", "text", "num") else inline(c))
        out.append(html.escape(c.tail or ""))
    return "".join(out)


def ws(s):
    return " ".join(s.split())


class Doc:
    def __init__(self, index, sdir):
        self.base, self.sdir, self.out, self.seen = Path(index).parent, Path(sdir), [], []

    def resolve(self, href):
        p = (self.base / href)
        return p if p.exists() else self.sdir / Path(href).name

    def include(self, e):
        p = self.resolve(e.get("href"))
        if not p.exists():
            return None                                  # a test run with only some sections local
        self.seen.append(p)
        return etree.parse(str(p)).getroot()

    def p(self, text, depth=None, cls=None):
        """depth: the para nesting as a CSS margin (10pt per level) -- the formula's indent_css reads it back as
        the ladder hint for a line's first label, and as the owner of an unnumbered line (indent_closes)"""
        attrs = (' class="%s"' % cls if cls else "") + (' style="margin-left:%dpt"' % (10 * depth) if depth is not None else "")
        self.out.append("<p%s>%s</p>" % (attrs, text))

    def line(self, e, cls=None, depth=None):
        """a text-like block: a lone table stands as its own block; otherwise one <p>"""
        kids = [c for c in e if isinstance(c.tag, str)]
        if len(kids) == 1 and local(kids[0]) == "table" and not (e.text or "").strip() and not (kids[0].tail or "").strip():
            self.out.append(inline(e).strip())
            return
        s = ws(inline(e))
        if s:
            self.p(s, depth, cls)

    def notes(self, items):
        last = None
        for a in items:
            ty = a.get("type", "")
            if ty != last:
                self.out.append('<p class="atype">%s</p>' % html.escape(ty))
                last = ty
            self.line(a, "annotation")

    def body(self, c, where, depth=0):
        """depth: rank of the paras this element sits in (0: directly in the section)"""
        t = local(c)
        if t == "para":
            self.para(c, "", depth)
        elif t in LINES:
            self.line(c, None, depth)
        elif t == "annotations":
            self.notes([a for a in c if local(a)])
        elif t == "annotation":
            self.notes([c])
        elif c.tag == XI:
            r = self.include(c)
            if r is not None:
                self.body(r, where, depth)
        elif t == "include":                             # <include> (library namespace, title 99): a wrapper
            for k in c:                                  # of quoted paras/text, read in place
                self.body(k, where, depth)
        elif t:
            raise SystemExit("oll-xml: %s: unexpected <%s>" % (where, t))

    def para(self, p, lead="", depth=0, ld=None):
        ld = depth if ld is None else ld                  # ld: the depth the line is drawn at (its first label's)
        """lead = prefix/num/heading in document order + the first <text>: one line '(a) Heading. — text';
        a para with only a number merges into its first child's line: '(c)(1) text'"""
        kids = [c for c in p if local(c)]
        parts, i = [lead] if lead else [], 0
        while i < len(kids) and local(kids[i]) in LEAD:
            parts.append(ws(inline(kids[i])))
            i += 1
        bare = all(local(k) == "num" for k in kids[:i])
        if i < len(kids) and local(kids[i]) == "text":
            s = ws(inline(kids[i]))
            if s.lstrip().startswith("<table"):
                self.p(" ".join(x for x in parts if x), ld)
                self.line(kids[i], None, depth + 1)
            else:
                self.p(" ".join(x for x in parts + [s] if x), ld)
            i += 1
        elif i < len(kids) and local(kids[i]) == "para" and bare:
            self.para(kids[i], "".join(parts), depth + 1, ld)   # the line's first label is this para's
            i += 1
        elif parts:
            self.p(" ".join(x for x in parts if x), ld)
        for c in kids[i:]:
            if local(c) in LEAD:
                raise SystemExit("oll-xml: para: <%s> after the body" % local(c))
            self.body(c, "para", depth + 1)               # sub-paras one deeper; a continuation line at depth + 1
                                                          # belongs to this para (indent_closes = true)

    def section(self, s):
        num = ws(inline(s.find(NS + "num")))
        pre = s.find(NS + "prefix")
        h = s.find(NS + "heading")
        self.out.append("<p><b>%s%s %s</b></p>" % (ws(inline(pre)) + " " if pre is not None else "", num,
                                                  ws(inline(h)) if h is not None else ""))
        for c in s:
            t = local(c)
            if t in ("num", "heading", "prefix"):
                continue
            if t == "reason":                            # "Repealed": a line of its own after the heading
                self.line(c)
            else:
                self.body(c, "section %s" % num)

    def container(self, c, depth=1):
        for e in c:
            t = local(e)
            if t == "prefix":
                self.out.append("<h%d>%s %s</h%d>" % (depth, ws(inline(e)), ws(inline(c.find(NS + "num"))), depth))
                h = c.find(NS + "heading")
                if h is not None:
                    self.out.append("<h%d>%s</h%d>" % (depth, ws(inline(h)), depth))
            elif t in ("num", "heading"):
                continue
            elif t == "subheading":
                self.out.append("<h6>%s</h6>" % ws(inline(e)))
            elif t == "container":
                self.container(e, min(depth + 1, 5))
            elif t == "text":
                self.line(e)
            elif e.tag == XI:
                r = self.include(e)
                if r is not None:
                    self.section(r)
            elif t:
                raise SystemExit("oll-xml: container: unexpected <%s>" % t)


def main(index, sdir, dest, sec_files=None):
    images[0] = 0
    root = etree.parse(index).getroot()
    d = Doc(index, sdir)
    d.container(root)
    page = "<html><head><meta charset=\"utf-8\"></head><body>\n%s\n</body></html>\n" % "\n".join(d.out)
    Path(dest).write_text(page, encoding="utf-8")
    # conservation of the transformation itself
    from lxml import html as lh
    nos = lambda t: "".join(t.split())

    def walk(e):                          # document-order text, each xi:include replaced by its file
        kids = [c for c in e if isinstance(c.tag, str)]
        if local(e) == "section":         # the one reordering: <reason> is read after <heading>
            kids = [c for c in kids if local(c) in ("prefix", "num", "heading")] + \
                   [c for c in kids if local(c) not in ("prefix", "num", "heading")]
        out = [e.text or ""]
        for c in kids:
            if c.tag == XI:
                p = d.resolve(c.get("href"))
                if p.exists():
                    out.append(walk(etree.parse(str(p)).getroot()))
            else:
                out.append(walk(c))
            out.append(c.tail or "")
        out += [c.tail or "" for c in e if not isinstance(c.tag, str)]   # text after a comment
        return "".join(out)

    src = nos(walk(root))
    doc = lh.fromstring(page.encode())
    for el in doc.xpath('//p[@class="atype"]'):
        el.getparent().remove(el)
    got = nos(doc.text_content())
    print("  oll-xml: sections %d, images %d (no text); chars xml %d html %d; %s"
          % (len(d.seen), images[0], len(src), len(got), "EQUAL" if src == got else "DIFFER"))
    if src != got:
        k = next((i for i, (x, y) in enumerate(zip(src, got)) if x != y), min(len(src), len(got)))
        raise SystemExit("oll-xml lost text at %d: %r / %r" % (k, src[k-60:k+60], got[k-60:k+60]))
    return len(d.seen)


if __name__ == "__main__":
    main(*sys.argv[1:4])
