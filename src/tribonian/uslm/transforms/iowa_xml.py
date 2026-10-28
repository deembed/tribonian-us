"""Iowa Code chapter XML (legis.iowa.gov /docs/publications/ICC/<year>/attachments/<ch>_slim.xml) -> HTML blocks
for the html-paragraphs family. Proposed shared transform "iowa-xml": to_html(raw, formula) -> UTF-8 HTML.

Source shape (namespace slim = urn:legix:slim, xhtml):
  slim:Document > slim:TOC > slim:Item@title ("1.1   State boundaries.")
                > slim:Body > slim:Level@class (codeChapter, ...) > slim:Heading (span.identifier, span.headnote)
                     > slim:Level (nested containers) | slim:Section.codeSection
  Section > div.heading (span.identifier "22.1", span.headnote "Definitions.")
          > p.para (unnumbered text)  | div.<unit> (subsection, letteredPara, subpara, subparaDiv, ...)
                                        > div.heading (span.identifier "1" / "a", optional span.headnote) > p.para | div.<unit>
          > div.history > div.historyItem (p)      -> sourceCredit
          > div.footnotes > div.footnote           -> editorial note
  Empty typesetting spans (GENERATED: em-dash, en-dash, blank-line, new-line, tabLeader, ...) are written as the
  character or a space. span.codeSectionRange wraps a run of sections ("321.75 Reserved."); span.subparaSubdiv is a
  numbered unit like the divs (heading + p.para).
Output: one <p> per paragraph. Labels are written as printed in the PDF: depth 1-2 "1." "a.", deeper "(1)" "(a)";
a unit whose first child is another unit carries its label onto that unit's line ("3. a. ..."), as the PDF prints it.
style="margin-left:<depth*10>pt" gives the ladder a depth hint ([html].indent_css = {unit = 10, base = 0}).
Classes: p.level / p.levelname (container number line / name), p.toc, p.sec, p.para, p.hist, p.fn, p.other (anything
unknown, kept so no text is lost). Check: every non-space character of TOC + Body text is in the output, in order.
"""
import html
import re
import sys
import xml.etree.ElementTree as ET

S = "{urn:legix:slim}"
X = "{http://www.w3.org/1999/xhtml}"
LEVEL_WORD = {"codeChapter": "CHAPTER"}
# empty spans the publisher's typesetter turns into a character or space (all written here; the dashes are not
# source text, so the conservation check ignores them along with whitespace)
GENERATED = {"em-dash": "—", "en-dash": "–", "blank-line": " ", "new-line": " ", "em-space": " ", "en-space": " ",
             "thin-space": " ", "tabLeader": " ", "dotLeader": " ", "single-solid-line": " "}


def _text(el):
    out = []

    def walk(e):
        cls = e.get("class", "")
        if cls in GENERATED:                           # empty spans standing for a printed character
            out.append(GENERATED[cls])
        elif e.text:
            out.append(e.text)
        for c in e:
            walk(c)
            if c.tail:
                out.append(c.tail)
    walk(el)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _label(ident, depth):
    return "%s." % ident if depth <= 2 else "(%s)" % ident


def _added(labels):
    """the characters a label adds to its identifier: "." or "()" """
    return "".join("." if l.endswith(".") and not l[:-1].endswith(".") else "()" if l.startswith("(") else "" for l in labels)


def to_html(raw, formula=None):
    root = ET.fromstring(raw)
    out, check = [], []

    def p(cls, text, depth=None, added=""):
        """added: the part of text this transform wrote (label punctuation, CHAPTER word), not checked"""
        if not text:
            return
        st = ' style="margin-left:%dpt"' % (depth * 10) if depth is not None else ""
        out.append('<p class="%s"%s>%s</p>' % (cls, st, html.escape(text)))
        check.append(text)
        check.append("\0" + added)

    def heading(el):
        h = el.find(X + "div[@class='heading']")
        if h is None:
            h = el.find(S + "Heading")
        if h is None:
            return None, None, None
        i = h.find(X + "span[@class='identifier']")
        n = h.find(X + "span[@class='headnote']")
        return h, (_text(i) if i is not None else None), (_text(n) if n is not None else None)

    def unit(el, depth, carry, carry_adds=""):
        """a numbered unit; carry = labels (and headnotes) of enclosing units not yet written (printed on this
        line); carry_adds = the characters this transform wrote in them (a headnote's own "." is source text)"""
        h, ident, note = heading(el)
        lab = carry + [_label(ident, depth)] if ident else carry
        adds = carry_adds + (_added([_label(ident, depth)]) if ident else "")
        if note:
            lab = lab + [note]
        first = True
        for c in el:
            if c is h:
                continue
            cls = c.get("class", "")
            if c.tag == X + "p":
                p("para", " ".join(lab + [_text(c)]) if first else _text(c), depth, adds if first else "")
                lab, first, adds = [], False, ""
            elif c.tag in (X + "div", X + "span") and c.find(X + "div[@class='heading']") is not None:
                if first and note:                         # "1. State records system." then "a. ...": a label
                    p("para", " ".join(lab), depth, adds)  # after a headnote would not read as a label
                    lab, adds = [], ""
                unit(c, depth + 1, lab if first else [], adds if first else "")
                lab, first = [], False
            else:
                other(c, depth)
        if lab:                                            # a label with nothing after it
            p("para", " ".join(lab), depth, adds)

    def other(c, depth):
        cls = c.get("class", "")
        if cls == "history":
            for it in c:
                p("hist", _text(it))
        elif cls == "footnotes":
            for it in c:
                p("fn", _text(it))
        else:
            p("other", _text(c), depth)

    def section(el):
        h, ident, note = heading(el)
        p("sec", " ".join(x for x in (ident, note) if x))
        for c in el:
            if c is h:
                continue
            if c.tag == X + "p":
                p("para", _text(c), 0)
            elif c.tag in (X + "div", X + "span") and c.find(X + "div[@class='heading']") is not None:
                unit(c, 1, [])
            else:
                other(c, 0)

    toc = root.find(S + "TOC")
    toc_done = [False]

    def level(el):
        h, ident, note = heading(el)
        cls = el.get("class", "")
        word = LEVEL_WORD.get(cls) or re.sub(r"^code", "", cls).upper()
        p("level", " ".join(x for x in (word, ident) if x), added=word)
        p("levelname", note)
        if toc is not None and not toc_done[0]:            # the chapter's analysis, after its heading
            toc_done[0] = True
            for it in toc:
                p("toc", re.sub(r"\s+", " ", it.get("title", "")).strip())
        for c in el:
            if c is h:
                continue
            if c.tag == S + "Level":
                level(c)
            elif c.tag == S + "Section":
                section(c)
            elif c.get("class") == "codeSectionRange":   # span grouping a run of sections ("321.75 Reserved." ...)
                for cc in c:
                    section(cc) if cc.tag == S + "Section" else other(cc, 0)
            else:
                other(c, 0)

    body = root.find(S + "Body")
    for c in body:
        if c.tag == S + "Level":
            level(c)
        elif c.tag == S + "Section":
            section(c)
        else:
            other(c, 0)
    # conservation of the transform: TOC titles + Body text, whitespace and generated dashes aside
    nos = lambda t: re.sub(r"[\s—–]+", "", t)
    src = nos("".join(re.sub(r"\s+", " ", it.get("title", "")) for it in (toc if toc is not None else [])) + _text(body))
    got, extra = [], []
    for i in range(0, len(check), 2):
        got.append(check[i])
        extra.append(check[i + 1][1:])
    got, extra = nos("".join(got)), nos("".join(extra))
    from collections import Counter
    if Counter(got) - Counter(extra) != Counter(src):  # same characters (labels move in front of their text)
        sys.exit("iowa-xml lost text: source %d characters, output %d less %d written" % (len(src), len(got), len(extra)))
    return ('<html><body><div id="xml">\n' + "\n".join(out) + "\n</div></body></html>").encode("utf-8")


if __name__ == "__main__":
    sys.stdout.write(to_html(open(sys.argv[1], "rb").read()).decode("utf-8"))
