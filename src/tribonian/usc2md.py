"""
usc2md.py - convert a section of the US Code (USLM XML) to
            (a) a flat, full-citation-path markdown file for Obsidian
            (b) a dense LaTeX file sized for the reMarkable Paper Pro

Usage:
    tribonian usc usc05.xml --section /us/usc/t5/s552 --out ./out
    tribonian usc usc05.xml --section /us/usc/t5/s552a --out ./out
    tribonian usc usc05.xml --list            # show every section identifier

Source XML: https://uscode.house.gov/download/download.shtml  (USLM format)
"""

import argparse
import os
import re
import sys
from datetime import date

import unicodedata
from lxml import etree

# USLM hierarchy levels, outermost first. Anything in this set is a "level node".
LEVELS = [
    "subsection", "paragraph", "subparagraph", "clause",
    "subclause", "item", "subitem", "subsubitem",
]
LEVEL_SET = set(LEVELS) | {"level"}   # generic <level>: OLRC uses it where the numbering breaks the usual (1)(A)(i) pattern, e.g. 18 USC 2511

# What gets one markdown note each, and what groups them into one PDF.
UNIT_TAGS = ("section", "courtRule")
CONTAINER_TAGS = ("chapter", "courtRules", "compiledAct")

# Federal court rule sets, keyed by a word in the <courtRules> heading:
# (set key, citation form, short form)
RULE_SETS = [
    ("APPELLATE",  "App",   "Fed. R. App. P.",   "FRAP"),
    ("CIVIL",      "Civil", "Fed. R. Civ. P.",   "FRCP"),
    ("CRIMINAL",   "Crim",  "Fed. R. Crim. P.",  "FRCrP"),
    ("EVIDENCE",   "Evid",  "Fed. R. Evid.",     "FRE"),
    ("BANKRUPTCY", "Bankr", "Fed. R. Bankr. P.", "FRBP"),
]
DOC_TITLE = "?"      # title number of the file in hand ("5", "28a"); set in main()

# Inline elements that carry text but are not structure.
INLINE = {"ref", "i", "b", "inline", "date", "term", "span", "quote", "sup", "sub"}

from tribonian.render import (TEX_ESCAPES, TEX_ACCENTS, TEX_OK, UNMAPPED, tex_safe, tex_chars,   # noqa: F401
                              ODD_SPACES, LIGATURES, FOLD, PREAMBLE, RULES)


# ---------------------------------------------------------------- parsing

def strip_ns(tree):
    """USLM declares a default namespace. Drop it so tag names are plain."""
    for el in tree.iter():
        # Narrow/figure/thin spaces are typographic only. Fold them to a plain
        # no-break space here, once, so no later path can leak one to pdflatex
        # (which has no U+202F) or into markdown headings and file names.
        if el.text:
            el.text = unicodedata.normalize("NFC", el.text.translate(FOLD))
        if el.tail:
            el.tail = unicodedata.normalize("NFC", el.tail.translate(FOLD))
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        for k in list(el.attrib):
            if "}" in k:
                el.attrib[k.split("}", 1)[1]] = el.attrib.pop(k)
    return tree


def gather_text(el, mode):
    """Flatten an element's mixed content. mode is 'md' or 'tex'."""
    if el is None:
        return ""
    out = []

    def esc(s):
        if mode == "tex":
            return tex_chars(s)
        return s

    def walk(node, first=True):
        if not first and node.text:
            out.append(esc(node.text))
        for child in node:
            tag = child.tag if isinstance(child.tag, str) else ""
            if tag == "ref":
                if "footnoteRef" in (child.get("class") or ""):
                    idref = child.get("idref") or ""
                    out.append("\x00FN:%s\x00" % idref)   # placeholder, resolved later
                else:
                    txt = "".join(child.itertext())
                    hit = usc_ref(child.get("href")) if mode == "md" else None
                    if hit and hit[0] != CUR_SLUG and txt.strip():
                        out.append("[[%s|%s]]" % (hit[0], txt.replace("|", "/")))
                    else:
                        out.append(esc(txt))
            elif tag in INLINE:
                walk(child, first=False)
            elif tag == "note" or tag == "notes":
                pass       # body collected separately; the tail text still belongs here
            else:
                walk(child, first=False)
            if child.tail:
                out.append(esc(child.tail))

    if el.text:
        out.append(esc(el.text))
    walk(el, first=True)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def gather_text_str(s, mode):
    """Escape and space-collapse a bare string the way gather_text does."""
    if mode == "tex":
        s = tex_chars(s)
    return re.sub(r"\s+", " ", s).strip()


def paragraphs(el, mode):
    """An element's text as a list of paragraphs. USLM sets unnumbered runs of
    text (1 U.S.C. 1, for instance) as several <p> inside one <content>."""
    if el is None:
        return []
    ps = [c for c in el if isinstance(c.tag, str) and c.tag == "p"]
    if not ps:
        t = gather_text(el, mode)
        return [t] if t else []
    out = []
    lead = (el.text or "").strip()
    if lead:
        out.append(gather_text_str(lead, mode))
    for c in ps:
        t = gather_text(c, mode)
        if t:
            out.append(t)
    return out


def path_components(identifier, section_id):
    """/us/usc/t5/s552/a/1/A  ->  ['a','1','A']"""
    if not identifier or not identifier.startswith(section_id + "/"):
        return []
    return identifier[len(section_id) + 1:].split("/")


def label_for(components):
    """['a','1','A'] -> '(a)(1)(A)'"""
    return "".join("(%s)" % c for c in components)


def is_repealed(el, text):
    """USLM marks repealed material with status="repealed"; the text also says so."""
    if el.get("status") == "repealed":
        return True
    t = (text or "").lstrip(" [\u201c\"")
    return t.startswith("Repealed")


def note_blocks(note, mode):
    """Flatten one <note> into blocks: ("p", text) | ("quote", [lines]) | ("table", rows)."""
    blocks = []
    for ch in note:
        if not isinstance(ch.tag, str) or ch.tag == "heading":
            continue
        if ch.tag == "p":
            q = ch.find("quotedContent")
            if q is not None:
                # inline run before the quote (citation refs and all), the quote, then the tail
                esc = (lambda t: tex_chars(t)) if mode == "tex" else (lambda t: t)
                pre = [esc(ch.text or "")]
                for sib in ch:
                    if sib is q:
                        break
                    pre.append(gather_text(sib, mode))
                    pre.append(esc(sib.tail or ""))
                pre_t = re.sub(r"\s+", " ", "".join(pre)).strip()
                if pre_t:
                    blocks.append(("p", pre_t))
                blocks.append(("quote", quoted_lines(q, mode)))
                post = [esc(q.tail or "")]
                seen = False
                for sib in ch:
                    if sib is q:
                        seen = True
                        continue
                    if seen:
                        post.append(gather_text(sib, mode))
                        post.append(esc(sib.tail or ""))
                post_t = re.sub(r"\s+", " ", "".join(post)).strip()
                if post_t:
                    blocks.append(("p", post_t))
            else:
                t = gather_text(ch, mode)
                if t:
                    blocks.append(("p", t))
        elif ch.tag == "quotedContent":
            blocks.append(("quote", quoted_lines(ch, mode)))
        elif ch.tag == "table":
            rows = []
            for tr in ch.iter("tr"):
                rows.append([gather_text(td, mode) for td in tr if td.tag in ("td", "th")])
            if rows:
                blocks.append(("table", rows))
        else:
            t = gather_text(ch, mode)
            if t:
                blocks.append(("p", t))
    return blocks


def quoted_lines(q, mode, depth=0):
    """Enacted text quoted inside a note, as (indent, text) lines. Inline runs
    (text, <ref>, <date>, <inline>...) merge into one line; a level element or
    <p> starts a new one, with the (a)(1)(A) structure kept by walking the level
    elements and prefixing their num."""
    esc = (lambda t: tex_chars(t)) if mode == "tex" else (lambda t: t)
    out, buf = [], []

    def flush():
        t = re.sub(r"\s+", " ", "".join(buf)).strip()
        if t:
            out.append((depth, t))
        del buf[:]

    if q.text:
        buf.append(esc(q.text))
    for ch in q:
        if not isinstance(ch.tag, str):
            continue
        if ch.tag in LEVEL_SET or ch.tag == "section":
            flush()
            parts = []
            for sub in ("num", "heading", "chapeau", "content"):
                e = ch.find(sub)
                if e is not None:
                    parts.append(gather_text(e, mode))
            if any(parts):
                out.append((depth, " ".join(x for x in parts if x)))
            out.extend(quoted_lines(ch, mode, depth + 1))
            cont = ch.find("continuation")
            if cont is not None:
                out.append((depth, gather_text(cont, mode)))
        elif ch.tag == "p":
            flush()
            t = gather_text(ch, mode)
            if t:
                out.append((depth, t))
        elif ch.tag in ("num", "heading", "chapeau", "content", "continuation"):
            continue        # consumed by the level handling above
        else:                # inline: ref, date, inline, i, b, term...
            buf.append(gather_text(ch, mode))
        if ch.tail:
            buf.append(esc(ch.tail))
    flush()
    return out


def collect_notes(sec, mode):
    """[(role, heading, blocks)] for a section's <notes>, in document order."""
    notes = sec.find("notes")
    if notes is None:
        return []
    out = []
    for n in notes.iter("note"):
        h = n.find("heading")
        out.append((n.get("role") or "", gather_text(h, mode) if h is not None else "",
                    note_blocks(n, mode)))
    return out


def collect_footnotes(el):
    """id -> (md text, tex text) for every <note type="footnote"> in the section."""
    fns = {}
    for n in el.iter("note"):
        if n.get("type") == "footnote" and n.get("id"):
            fns[n.get("id")] = (gather_text(n, "md"), gather_text(n, "tex"))
    return fns


def strip_fn(text):
    """Remove footnote placeholders (for titles, YAML, listings, file names)."""
    return re.sub("\x00FN:[^\x00]*\x00", "", text).strip()


def resolve_fn(text, fns, mode, sink=None):
    """Turn footnote placeholders into footnotes; drop the ones we can't resolve.
    md: an Obsidian [^id] reference.
    tex: a numbered superscript mark; the body is appended to `sink` as
    (number, text) for the caller to place with \\footnotetext once it is
    outside any unbreakable box. \\footnote inside a nested tcolorbox is
    typeset at the box bottom, uncounted, and pushes the box into the footer."""
    def rep(m):
        body = fns.get(m.group(1))
        if not body:
            return ""
        if mode == "md":
            return "[^%s]" % m.group(1)
        sink.append(body[1])
        n = sink.counter = getattr(sink, "counter", 0) + 1
        return "\\textsuperscript{%d}" % n
    return re.sub("\x00FN:([^\x00]*)\x00", rep, text)


class FnSink(list):
    """Pending footnote bodies for the current section, with document-wide numbering."""
    counter = 0


USC_SEC_RE = re.compile(r"^/us/usc/t([0-9A-Za-z]+)/s([0-9][0-9A-Za-z.\-\u2010-\u2015]*)")
# The section number must start with a digit: USLM writes subtitles as
# /us/usc/t26/stA, which would otherwise read as section "tA". Dashes in
# section numbers (15 U.S.C. 80a-1) are en dashes in the source hrefs.

CUR_SLUG = ""   # slug of the section being rendered, so it never links to itself


def usc_ref(href):
    """(vault slug, citation) for a US Code href, else None.

    Only /us/usc/... targets have a note in the vault. Public Law, Statutes at
    Large and session-act hrefs stay plain text -- the source credit already
    carries them in readable form.
    """
    m = USC_SEC_RE.match(href or "")
    if not m:
        return None
    title_num, sec_num = m.group(1).lower(), m.group(2).rstrip(".")
    return ("%s-usc-%s" % (title_num, safe(sec_num)),
            "%s U.S.C. \u00a7 %s" % (title_num, sec_num))


def collect_refs(el, section_id):
    """US Code cross references for the note's References section, deduped."""
    seen, out = set(), []
    for r in el.iter("ref"):
        href = r.get("href")
        if not href or href == section_id or href.startswith(section_id + "/"):
            continue
        hit = usc_ref(href)
        if hit and hit[0] != CUR_SLUG and hit[0] not in seen:
            seen.add(hit[0])
            out.append(hit)
    return out


# ---------------------------------------------------------------- model

class Node:
    __slots__ = ("tag", "components", "label", "tail", "heading", "chapeau",
                 "content", "continuation", "children", "repealed", "more")

    def __init__(self):
        self.tag = ""
        self.components = []
        self.label = ""
        self.tail = ""
        self.heading = ""
        self.chapeau = ""
        self.content = ""
        self.continuation = ""
        self.children = []
        self.repealed = False
        self.more = []          # further paragraphs after the first, (md, tex) pairs


def build(el, section_id, mode_pair=("md", "tex")):
    """Turn a level element into a Node. Text is stored twice, md and tex."""
    n = Node()
    n.tag = el.tag
    n.components = path_components(el.get("identifier", ""), section_id)
    if not n.components:
        num = el.find("num")
        if num is not None and num.get("value"):
            n.components = [num.get("value")]
    n.label = label_for(n.components)
    n.tail = label_for(n.components[-1:]) if n.components else n.label

    h = el.find("heading")
    n.heading = (gather_text(h, "md"), gather_text(h, "tex")) if h is not None else ("", "")
    c = el.find("chapeau")
    n.chapeau = (gather_text(c, "md"), gather_text(c, "tex")) if c is not None else ("", "")
    ct = el.find("content")
    pm, pt = paragraphs(ct, "md"), paragraphs(ct, "tex")
    n.content = (pm[0], pt[0]) if pm else ("", "")
    n.more = list(zip(pm[1:], pt[1:]))
    if n.chapeau[0] and pm:                 # chapeau is the lead; all content follows it
        n.more = list(zip(pm, pt))
    cont = el.find("continuation")
    n.continuation = (gather_text(cont, "md"), gather_text(cont, "tex")) if cont is not None else ("", "")
    n.repealed = is_repealed(el, n.heading[0] or n.content[0])

    for child in el:
        if isinstance(child.tag, str) and child.tag in LEVEL_SET:
            n.children.append(build(child, section_id))
    return n


# ---------------------------------------------------------------- markdown

def md_notes(notes, fns):
    lines = ["", "## Notes", ""]
    for role, head, blocks in notes:
        if role == "crossHeading":
            lines += ["### " + head, ""]
            continue
        if head:
            lines += ["**" + head + "**", ""]
        for kind, val in blocks:
            if kind == "p":
                lines += [resolve_fn(val, fns, "md"), ""]
            elif kind == "quote":
                for depth, line in val:
                    lines.append("> " + "\u00a0\u00a0" * depth + resolve_fn(line, fns, "md"))
                lines.append("")
            elif kind == "table":
                ncol = max(len(r) for r in val)
                lines.append("| " + " | ".join(val[0] + [""] * (ncol - len(val[0]))) + " |")
                lines.append("|" + " --- |" * ncol)
                for r in val[1:]:
                    lines.append("| " + " | ".join(r + [""] * (ncol - len(r))) + " |")
                lines.append("")
    return lines


def render_md(section, nodes, meta, refs, fns, notes=None, credits=False, extra=None):
    lines = []
    aliases = meta["aliases"]
    lines.append("---")
    lines.append('title: "%s"' % meta["cite"])
    lines.append('heading: "%s"' % meta["heading"].replace('"', "'"))
    lines.append('citation: "%s"' % meta["cite"])
    lines.append("identifier: %s" % meta["identifier"])
    lines.append("source: uscode.house.gov (USLM)")
    lines.append("release_point: %s" % meta["release"])
    lines.append("retrieved: %s" % date.today().isoformat())
    lines.append("synthetic: false")
    if meta["repealed"]:
        lines.append("repealed: true")
    lines.append("aliases:")
    for a in aliases:
        lines.append('  - "%s"' % a)
    lines.append("---")
    lines.append("")
    h1 = "%s \u2014 %s" % (meta["cite"], resolve_fn(meta["heading_raw"], fns, "md"))
    lines.append("# " + ("~~%s~~" % h1 if meta["repealed"] else h1))
    lines.append("")
    if credits and meta["credit"]:
        lines += ["*" + meta["credit"] + "*", ""]

    def md(t):
        return resolve_fn(t, fns, "md")

    def emit(n):
        lead = md(n.chapeau[0] or n.content[0])
        if n.tag in UNIT_TAGS:
            if lead:
                lines.append(lead); lines.append("")
            for t, _ in n.more:
                lines.append(md(t)); lines.append("")
            for c in n.children:
                emit(c)
            if n.continuation[0]:
                lines.append(md(n.continuation[0])); lines.append("")
            return
        head = " **%s**" % md(n.heading[0]) if n.heading[0] else ""
        line = ("**%s**%s %s" % (n.label, head, lead)).rstrip()
        if n.repealed:
            line = "~~%s~~" % line
        lines.append(line)
        lines.append("")
        for t, _ in n.more:
            lines.append(md(t)); lines.append("")
        for c in n.children:
            emit(c)
        if n.continuation[0]:
            lines.append("**%s** *(cont.)* %s" % (n.label, md(n.continuation[0])))
            lines.append("")

    for n in nodes:
        emit(n)
    if notes:
        lines += md_notes(notes, fns)
    if extra:                                   # derived sections ("Cited with"), after the law and its notes
        lines.append("")
        lines += extra
    if refs:
        lines.append("")
        lines.append("## References")
        lines.append("")
        for target, cite in refs:
            lines.append("- [[%s|%s]]" % (target, cite))
    if fns:
        lines.append("")
        for k, (mdtext, _) in fns.items():
            lines.append("[^%s]: %s" % (k, mdtext))
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- latex


def tex_table(rows):
    ncol = max(len(r) for r in rows)
    w = "p{\\dimexpr(\\linewidth-%d\\tabcolsep)/%d\\relax}" % (2 * ncol, ncol)
    out = ["\\par\\noindent\\begin{tabular}{%s}" % (w * ncol)]
    for r in rows:
        r = r + [""] * (ncol - len(r))
        out.append(" & ".join(r) + " \\\\")
    out.append("\\end{tabular}\\par")
    return "\n".join(out) + "\n\n"


def tex_notes(notes, secpath, fns, sink, flush):
    out = ["\\recordpath{%s notes}\\notesrule\n{\\small\n" % secpath]
    for role, head, blocks in notes:
        if role == "crossHeading":
            out.append("\\vspace{3pt}{\\normalsize\\bfseries\\itshape %s\\par}\\vspace{1pt}\n" % head)
            continue
        if head:
            out.append("{\\bfseries %s}\\par\n" % head)
        for kind, val in blocks:
            if kind == "p":
                out.append("%s\\par\n\n" % resolve_fn(val, fns, "tex", sink))
            elif kind == "quote":
                out.append("\\begin{quoted}\n")
                for depth, line in val:
                    out.append("\\hspace*{%.1fem}%s\\par\n" % (1.2 * depth, resolve_fn(line, fns, "tex", sink)))
                out.append("\\end{quoted}\n")
            elif kind == "table":
                out.append(tex_table(val))
            out.extend(flush())
    out.append("}\n\n")
    return "".join(out)


# The derived sections (cited_with: Cited with, Cited by, Recent appellate cites) come as markdown lines, the
# same lines the note gets. derived_tex() reads that small subset back: ##/### headings, a whole-line *italic*
# caption, plain paragraphs and pipe tables, with [[note\|text]] -> text (no links across PDFs) and
# [text](url) -> \href. One source for both outputs, so the PDF never says something the note doesn't.
PDF_ROWS = {"Recent appellate cites": 10}       # table rows kept in the PDF per section (the note keeps them all)
_INLINE = re.compile(r"\[\[([^\]]*?)\]\]|\[([^\]]+)\]\((https?://[^)\s]+)\)|`([^`]+)`")


def inline_tex(t):
    out, i = [], 0
    for m in _INLINE.finditer(t):
        out.append(tex_chars(t[i:m.start()]))
        if m.group(1) is not None:                           # [[note|text]]: the text only
            out.append(tex_chars(re.split(r"\\?\|", m.group(1), 1)[-1]))
        elif m.group(2) is not None:
            url = m.group(3).replace("\\", "/").replace("%", "\\%").replace("#", "\\#")
            out.append("\\href{%s}{%s}" % (url, tex_chars(m.group(2))))
        else:
            out.append("\\texttt{%s}" % tex_chars(m.group(4)))
        i = m.end()
    return "".join(out) + tex_chars(t[i:])


def table_tex(rows, keep=None):
    """rows: lists of markdown cells, the header first. A longtable: the column with the longest text
    wraps, numeric columns sit right."""
    head, body = rows[0], rows[1:]
    dropped = 0
    if keep is not None and len(body) > keep:
        body, dropped = body[:keep], len(body) - keep
    n = len(head)
    plain = lambda c: re.sub(r"\[\[[^\]]*?\\?\|([^\]]*)\]\]|\[([^\]]+)\]\([^)]*\)", lambda m: m.group(1) or m.group(2) or "", c)
    avg = [sum(len(plain(r[k])) for r in body if k < len(r)) / max(1, len(body)) for k in range(n)]
    wide = max(range(n), key=lambda k: avg[k])
    num = [all(re.fullmatch(r"[\d,.\s\u2013-]*", plain(r[k])) for r in body if k < len(r)) for k in range(n)]
    spec = ["p{%.2f\\linewidth}" % max(0.3, 1 - 0.13 * (n - 1)) if k == wide else ("r" if num[k] else "l")
            for k in range(n)]
    line = lambda r: " & ".join(inline_tex(c) for c in (r + [""] * n)[:n]) + " \\\\"
    out = ["{\\footnotesize\\setlength{\\LTleft}{0pt}\\setlength{\\LTpre}{2pt}\\setlength{\\LTpost}{2pt}",
           "\\begin{longtable}{@{}%s@{}}" % " ".join(spec),
           "\\textbf{" + "} & \\textbf{".join(inline_tex(c) for c in head) + "} \\\\ \\hline\\endhead"]
    out += [line(r) for r in body]
    out.append("\\end{longtable}}")
    if dropped:
        out.append("{\\footnotesize\\itshape and %d more in the note.\\par}" % dropped)
    return "\n".join(out) + "\n"


def derived_tex(lines, secno):
    """TeX for the derived sections after a section's law (and notes); '' when there are none."""
    out, table, section = [], [], ""

    def flush():
        if table:
            out.append(table_tex(table, PDF_ROWS.get(section)))
            del table[:]

    for ln in lines:
        cells = ln.strip()
        if cells.startswith("|"):
            row = [c.strip() for c in re.split(r"(?<!\\)\|", cells.strip("|"))]
            if not all(re.fullmatch(r":?-+:?", c) for c in row):
                table.append(row)
            continue
        flush()
        if ln.startswith("## "):
            section = ln[3:].strip()
            out.append("\\vspace{8pt}\\pdfbookmark[2]{%s}{derived%d-%d}{\\bfseries %s\\par}\\nopagebreak\n"
                       % (gather_text_str(section, "tex"), secno, len(out), inline_tex(section)))
        elif ln.startswith("### "):
            out.append("\\vspace{3pt}{\\small\\bfseries %s\\par}\\nopagebreak\n" % inline_tex(ln[4:].strip()))
        elif re.fullmatch(r"\*[^*].*\*", cells):
            out.append("{\\scriptsize\\itshape %s\\par}\\vspace{2pt}\n" % inline_tex(cells[1:-1]))
        elif cells:
            out.append("{\\footnotesize %s\\par}\\vspace{2pt}\n" % inline_tex(cells))
    flush()
    return "".join(out)


def render_tex(docs, doctitle, dochead, fontpkg, style, release, credits=False, notes=False):
    """docs: list of (meta, nodes, fns, notes). One .tex, one section after another."""
    body = []

    sink = FnSink()                         # sink.counter: document-wide mark number

    def pending_footnotes():
        """\\footnotetext lines for every mark issued since the last flush,
        numbered to match the marks."""
        first = sink.counter - len(sink) + 1
        out = ["\\footnotetext[%d]{%s}\n" % (first + i, body) for i, body in enumerate(sink)]
        del sink[:]
        return out

    def flush_footnotes():
        """Called only outside every box: tcolorbox captures footnotes inside a
        breakable box too (its content is a split \\vbox, and inserts do not
        migrate out of one), so the text lands after the subsection's box closes."""
        body.extend(pending_footnotes())

    for i, (meta, nodes, fns, secnotes) in enumerate(docs):
        def tx(t, fns=fns):
            return resolve_fn(t, fns, "tex", sink)

        secpath = meta["path_tex"]
        sechead = "%s. %s" % (secpath, tx(meta["heading_tex"]))
        if meta["repealed"]:
            sechead = "\\sout{%s}" % sechead
        if i:
            body.append("\\vspace{10pt}\n")
        # outline: one entry per section, one per top-level item under it
        sectitle = "%s. %s" % (secpath, gather_text_str(meta["heading"], "tex"))
        if meta["repealed"]:
            sectitle += " [repealed]"
        body.append("\\pdfbookmark[1]{%s}{sec%d}" % (sectitle, i))
        body.append("\\recordpath{%s}{\\large\\bfseries %s\\par}\n" % (secpath, sechead))
        marks = [0]

        def bookmark(n, secno=i):
            """\\pdfbookmark for a top-level item: "(a) Heading", or its first words."""
            src, k = n, n
            while not (k.heading[0] or k.chapeau[0] or k.content[0]) and k.children:
                k = k.children[0]            # 552a(g): the heading sits on (g)(1)
            src = k
            text = strip_fn(src.heading[0] or src.chapeau[0] or src.content[0])
            words = re.sub(r"\.?\s*\u2014$", "", text).split()   # drop the ".—" that ends a run-in heading
            title = " ".join(words[:9]) + ("\u2026" if len(words) > 9 else "")
            marks[0] += 1
            return "\\pdfbookmark[2]{%s}{sec%d-%d}" % (
                gather_text_str(("%s %s" % (n.tail, title)).strip(), "tex"), secno, marks[0])

        if credits and meta["credit_tex"]:
            body.append("{\\footnotesize\\itshape %s\\par}\n" % meta["credit_tex"])
        body.append("\\vspace{2pt}\n\n")

        # Flat emission: every item becomes one record; frames are derived from
        # which groups open (first item of a group) or close (last box in the
        # group's subtree) at that record. `parts` holds either a record dict
        # (a box) or a plain string (unboxed text at depth 0).
        parts = []

        def box(depth, paras):
            rec = {"depth": depth, "paras": paras, "opens": False, "closes": []}
            parts.append(rec)
            return rec

        def emit(n, depth=0, first=False):
            """Emit n and its subtree; return the last record emitted (or None)."""
            lead = tx(n.chapeau[1] or n.content[1])
            if n.tag in UNIT_TAGS:
                if lead:
                    parts.append("%s\\par\n\n" % lead)
                for _, t in n.more:
                    parts.append("%s\\par\n\n" % tx(t))
                parts.extend(pending_footnotes())
                for c in n.children:
                    emit(c, 0)
                if n.continuation[1]:
                    parts.append("%s\\par\n\n" % tx(n.continuation[1]))
                parts.extend(pending_footnotes())
                return None
            head = " \\textbf{%s}" % tx(n.heading[1]) if n.heading[1] else ""
            # Every node gets a label line, even with no text of its own. Otherwise a
            # subsection whose heading sits on its first paragraph (552a(g), (i), (l),
            # (m), (t)) loses its letter entirely under tail-only labels.
            item = "\\lab{%s}%s %s" % (n.tail, head, lead)
            if n.repealed:
                item = "\\sout{%s}" % item
            paras = ["\\recordpath{%s%s}%s%s\\par" % (secpath, n.label, bookmark(n) if depth == 0 else "", item)]
            paras += ["%s\\par" % tx(t) for _, t in n.more]
            if depth == 0:
                parts.append("\n".join(paras) + "\n\n")
                last = None
            else:
                last = box(depth, paras)
                last["opens"] = first
            # children form a group whose sides sit at offset `depth`
            group_last = None
            for i, c in enumerate(n.children):
                r = emit(c, depth + 1, first=(i == 0))
                if r is not None:
                    group_last = r
            if n.continuation[1]:
                cont = "%s\\par" % tx(n.continuation[1])
                if depth == 0:
                    parts.append(cont + "\n\n")
                else:
                    group_last = last = box(depth, [cont])
            if n.children and group_last is not None:
                group_last["closes"].append(depth)   # the group at offset `depth` ends here
                last = group_last
            if depth == 0:
                parts.extend(pending_footnotes())
                parts.append("\n")
            return last

        def box_opts(rec):
            d = rec["depth"]
            opts = ["left=%d\\lvlstep+4pt" % d, "right=%d\\lvlstep+4pt" % d]
            opts += ["borderline west={0.5pt}{%d\\lvlstep}{black}, borderline east={0.5pt}{%d\\lvlstep}{black}" % (k, k)
                     for k in range(d)]
            north = "\\hrulestep{%d}{north}" % (d - 1) if rec["opens"] else ""
            south = "".join("\\hrulestep{%d}{south}" % k for k in sorted(rec["closes"]))
            if north:
                opts.append("overlay first={%s}" % north)
                opts.append("top=4pt")
            if south:
                opts.append("overlay last={%s}" % south)
                opts.append("bottom=4pt")
            if north or south:
                opts.append("overlay unbroken={%s%s}" % (north, south))
            return ", ".join(opts)

        for n in nodes:
            emit(n, 0)
        for part in parts:
            if isinstance(part, dict):
                body.append("\\begin{lvl}[%s]\n%s\n\\end{lvl}\n" % (box_opts(part), "\n".join(part["paras"])))
            else:
                body.append(part)
        if notes and secnotes:
            body.append(tex_notes(secnotes, secpath, fns, sink, pending_footnotes))
        if meta.get("derived"):
            body.append(derived_tex(meta["derived"], i))

    pre = (PREAMBLE
           .replace("__FONTPKG__", fontpkg)
           .replace("__RULES__", RULES[style])
           .replace("__DOCTITLE__", doctitle)
           .replace("__DOCHEAD__", dochead)
           .replace("__SRCLINE__", "uscode.house.gov USLM \\textbullet\\ release %s \\textbullet\\ retrieved %s"
                    % (release, date.today().isoformat())))
    return pre + "".join(body) + "\n\\end{document}\n"


# ---------------------------------------------------------------- driver

def find_by_identifier(root, identifier, tag=None):
    for el in root.iter(tag) if tag else root.iter():
        if el.get("identifier") == identifier:
            return el
    return None


_RELEASE = {}


def release_of(root):
    """The release point ("119-108"), from the <meta> block's currency note.

    <docNumber> is the TITLE number, not the release -- reading it there put
    "release_point: 5" in every Title 5 note.  Memoized: section_meta calls
    this once per section.
    """
    key = id(root)
    if key in _RELEASE:
        return _RELEASE[key]
    release = "unknown"
    for n in root.iter("note"):
        m = re.search(r"Current through\s+(\S+)", gather_text(n, "md"))
        if m:
            release = m.group(1).rstrip(".")
            break
    for p in root.iter("property"):
        if p.get("role") in ("publicLawNumber", "releasePoint"):
            release = gather_text(p, "md") or release
    _RELEASE[key] = release
    return release


def tex_esc(t):
    return tex_chars(t)


def ancestor(el, tags):
    p = el.getparent()
    while p is not None and p.tag not in tags:
        p = p.getparent()
    return p


def rule_set(cr):
    """(key, citation form, short form) for a <courtRules> element."""
    h = strip_fn(gather_text(cr.find("heading"), "md")) if cr is not None else ""
    for word, key, cite, short in RULE_SETS:
        if word in h.upper():
            return key, cite, short
    return safe(h) or "rules", (titlecase(h) or "Rule").rstrip(".") + ", Rule", safe(h) or "rules"


def titlecase(h):
    small = {"of", "on", "the", "and", "for", "in", "to", "or", "a", "an"}
    ws = h.lower().split()
    return " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(ws))


def md_subdir(sec, title_num):
    """Vault folder for one section: t05/ch07 -- one level per Code division."""
    m = re.match(r"([0-9]+)(.*)", title_num)
    tdir = "t%s%s" % (m.group(1).zfill(2), m.group(2)) if m else "t%s" % title_num
    ch = ancestor(sec, ("chapter", "courtRules", "compiledAct"))
    if ch is None:
        return tdir
    if ch.tag == "chapter":
        last = (ch.get("identifier") or "").rsplit("/", 1)[-1]
        m = re.match(r"ch([0-9]+)(.*)", last)
        sub_ = "ch%s%s" % (m.group(1).zfill(2), m.group(2)) if m else (last or "ch")
    elif ch.tag == "courtRules":
        sub_ = safe(rule_set(ch)[2])
    else:
        sub_ = safe(titlecase(strip_fn(gather_text(ch.find("heading"), "md"))))[:60]
    return os.path.join(tdir, sub_ or "misc")


def section_meta(sec, root):
    """Everything the renderers need to name and cite one unit (a Code section,
    a court rule, or a section of an act set out in an appendix)."""
    ident = sec.get("identifier", "")
    num = sec.find("num")
    sec_num = (num.get("value") if num is not None and num.get("value")
               else re.sub(r"[^0-9A-Za-z\-, .]", "", gather_text(num, "md")))
    m = re.match(r"/us/usc/t([0-9A-Za-z]+)/", ident)
    title_num = (m.group(1) if m else DOC_TITLE).lower()
    heading_el = sec.find("heading")
    heading_raw = gather_text(heading_el, "md")
    heading = strip_fn(heading_raw)
    sc = sec.find("sourceCredit")

    act = ancestor(sec, ("compiledAct",))
    if sec.tag == "courtRule":
        key, cite_form, short = rule_set(ancestor(sec, ("courtRules",)))
        slug_mid = short.lower()
        # Supplemental rules restart at Rule 1 inside the same set; keep them apart.
        lvl = ancestor(sec, ("level",))
        lh = strip_fn(gather_text(lvl.find("heading"), "md")).upper() if lvl is not None else ""
        if lh.startswith("SUPPLEMENTAL"):
            sk, sc_form = (("ss", "Supp. Soc. Sec. R.") if "SOCIAL SECURITY" in lh else
                           ("adm", "Supp. Adm. R.") if "ADMIRALTY" in lh else ("supp", "Supp. R."))
            cite_form, short, slug_mid = sc_form, sc_form, "%s-supp-%s" % (slug_mid, sk)
        cite = "%s %s" % (cite_form, sec_num)
        aliases = [cite] + (["%s %s" % (short, sec_num)] if short != cite_form else [])
        path_tex = "Rule %s" % tex_esc(sec_num)
        slug_ = "%s-%s-rule-%s" % (title_num, slug_mid, safe(sec_num))
    elif act is not None:
        actname = titlecase(strip_fn(gather_text(act.find("heading"), "md")))
        cite = "%s § %s" % (actname, sec_num)
        aliases = [cite]
        path_tex = "\\S\\ %s" % tex_esc(sec_num)
        slug_ = "%s-%s-%s" % (title_num, safe(actname), safe(sec_num))
    else:
        cite = "%s U.S.C. § %s" % (title_num, sec_num)
        aliases = [cite, "%s USC %s" % (title_num, sec_num)]
        path_tex = "\\S\\ %s" % tex_esc(sec_num)
        slug_ = "%s-usc-%s" % (title_num, safe(sec_num))
    return {
        "credit": gather_text(sc, "md") if sc is not None else "",
        "credit_tex": gather_text(sc, "tex") if sc is not None else "",
        "identifier": ident,
        "sec_num": sec_num,
        "title_num": title_num,
        "cite": cite,
        "cite_tex": tex_esc(cite).replace("§", "\\S").replace(". ", ".\\ ").replace("\\S ", "\\S\\ "),
        "aliases": aliases,
        "path_tex": path_tex,
        "slug": slug_,
        "mdsubdir": md_subdir(sec, title_num),
        "heading": heading,
        "heading_raw": heading_raw,
        "heading_tex": gather_text(heading_el, "tex"),
        "release": release_of(root),
        "repealed": is_repealed(sec, heading),
    }


def safe(s):
    return re.sub(r"[^0-9a-z]+", "-", s.lower()).strip("-")


def slug(meta):
    return meta["slug"]


def cited_with(meta, args):
    """The derived sections after the law: '## Cited with' (co-citation profile), '## Cited by' and
    '## Recent appellate cites' (cocite_usc.py usc-citedby). Each is [] when not built; --no-cocite leaves all out."""
    if args.no_cocite:
        return []
    out = []
    try:
        from tribonian import cocite_usc
    except Exception as e:
        print("  derived sections skipped: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return []
    for name, fn in (("cited-with", cocite_usc.section_md), ("cited-by", cocite_usc.citedby_md),
                     ("recent-appellate", cocite_usc.recent_md)):
        try:
            out += fn(meta["identifier"])
        except Exception as e:                      # the law note must never fail over a derived section
            print("  %s skipped for %s: %s: %s" % (name, meta["identifier"], type(e).__name__, e), file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xml")
    sel = ap.add_mutually_exclusive_group()
    sel.add_argument("--section", nargs="+", metavar="ID",
                     help="one or more section identifiers, e.g. /us/usc/t5/s552 /us/usc/t5/s552a")
    sel.add_argument("--chapter", metavar="ID",
                     help="every section in a chapter, e.g. /us/usc/t5/ptI/ch5")
    sel.add_argument("--all", action="store_true", help="every section in the file")
    sel.add_argument("--list", action="store_true", help="list section identifiers and exit")
    ap.add_argument("--list-chapters", action="store_true", help="list chapter identifiers and exit")
    ap.add_argument("--name", help="output base name (default derived from the selection)")
    ap.add_argument("--credits", action="store_true",
                    help="show each section's source credit (Added Pub. L. ...) under its heading")
    ap.add_argument("--notes", action="store_true",
                    help="append each section's editorial and statutory notes")
    ap.add_argument("--out", help="write the markdown tree and the .tex here and stop (what `tribonian build` does); "
                    "default: notes into md/, the PDF compiled into pdf/ under the work folder")
    ap.add_argument("--no-cocite", dest="no_cocite", action="store_true",
                    help="leave out the derived sections: Cited with, Cited by, Recent appellate cites (when built)")
    ap.add_argument("--style", choices=list(RULES), default="boxes",
                    help="boxes: rule on all four sides; rails: left rule only")
    ap.add_argument("--font", default="XCharter",
                    help="LaTeX font package to load")
    args = ap.parse_args()

    parser = etree.XMLParser(huge_tree=True, remove_comments=True)
    tree = strip_ns(etree.parse(args.xml, parser))
    root = tree.getroot()

    # The file's own title number ("28a"), for identifiers that omit it (11a does).
    global DOC_TITLE
    for el in root.iter("title", "appendix"):
        m = re.match(r"/us/usc/t([0-9A-Za-z]+)$", el.get("identifier") or "")
        if m:
            DOC_TITLE = m.group(1).lower()
            break
    # A rule set with no identifier of its own (11a again) gets one, so it can be selected.
    for cr in root.iter("courtRules"):
        if not cr.get("identifier"):
            cr.set("identifier", "/us/usc/t%s/courtRules/%s" % (DOC_TITLE, rule_set(cr)[0]))

    def units(el):
        return [x for x in el.iter(*UNIT_TAGS) if x.get("identifier")]

    if args.list:
        for sec in units(root):
            print("%s\t%s" % (sec.get("identifier"), strip_fn(gather_text(sec.find("heading"), "md"))[:70]))
        return 0
    if args.list_chapters:
        for ch in root.iter(*CONTAINER_TAGS):
            if ch.get("identifier") and units(ch):
                print("%s\t%s\t%d %s" % (ch.get("identifier"),
                      strip_fn(gather_text(ch.find("heading"), "md"))[:60], len(units(ch)),
                      "rules" if ch.tag == "courtRules" else "sections"))
        return 0

    # ---- select sections -------------------------------------------------
    dochead, container = "", None
    if args.section and len(args.section) == 1 and any(
            x.get("identifier") == args.section[0] for x in root.iter(*CONTAINER_TAGS)):
        args.chapter, args.section = args.section[0], None     # a container named as a section
    if args.section:
        secs = []
        for ident in args.section:
            sec = next((x for x in units(root) if x.get("identifier") == ident), None)
            if sec is None:
                print("section %s not found" % ident, file=sys.stderr)
                return 1
            secs.append(sec)
    elif args.chapter:
        ch = container = next((x for x in root.iter(*CONTAINER_TAGS)
                               if x.get("identifier") == args.chapter), None)
        if ch is None:
            print("chapter %s not found" % args.chapter, file=sys.stderr)
            return 1
        secs = units(ch)
        dochead = strip_fn(gather_text(ch.find("heading"), "tex"))
        chnum = gather_text(ch.find("num"), "tex").rstrip("—–- .")   # USLM's num already ends in a dash
        if chnum:
            dochead = ("%s \\textemdash\\ %s" % (chnum, dochead)).strip()
    elif args.all:
        secs = units(root)
        t = root.find(".//title")
        if t is not None:
            dochead = "%s \\textemdash\\ %s" % (gather_text(t.find("num"), "tex"),
                                             strip_fn(gather_text(t.find("heading"), "tex")))
    else:
        ap.error("choose --section, --chapter, --all, or --list")
    if not secs:
        print("no sections selected", file=sys.stderr)
        return 3

    docs = []
    for sec in secs:
        globals()["CUR_SLUG"] = ""
        meta = section_meta(sec, root)
        globals()["CUR_SLUG"] = meta["slug"]
        # The section's own text (chapeau, content, continuation) comes through as
        # a root node with no label; its children are the subsections.
        nodes = [build(sec, meta["identifier"])]
        docs.append((meta, nodes, collect_footnotes(sec), collect_refs(sec, meta["identifier"]),
                     collect_notes(sec, "md") if args.notes else [],
                     collect_notes(sec, "tex") if args.notes else []))

    # ---- names ------------------------------------------------------------
    first, last = docs[0][0], docs[-1][0]
    if len(docs) == 1:
        doctitle = first["cite_tex"]
        if not dochead:
            dochead = strip_fn(first["heading_tex"])
        base = slug(first)
    else:
        doctitle = "%s U.S.C. \\S\\S\\ %s\\textendash %s" % (first["title_num"], first["sec_num"], last["sec_num"])
        if container is not None and container.tag == "courtRules":
            key, cite_form, short = rule_set(container)
            doctitle = tex_esc(cite_form).replace(". ", ".\\ ")
            base = "%s-%s" % (first["title_num"], short.lower())
        elif container is not None and container.tag == "compiledAct":
            actname = titlecase(strip_fn(gather_text(container.find("heading"), "md")))
            doctitle = "%s \\S\\S\\ %s\\textendash %s" % (tex_esc(actname), first["sec_num"], last["sec_num"])
            base = "%s-%s" % (first["title_num"], safe(actname))
        elif args.chapter:
            base = "%s-usc-%s" % (first["title_num"], safe(args.chapter.rsplit("/", 1)[-1]))
        elif args.all:
            base = "%s-usc-all" % first["title_num"]
        else:
            base = "%s-usc-%s-to-%s" % (first["title_num"], safe(first["sec_num"]), safe(last["sec_num"]))
    if args.name:
        base = args.name

    # ---- write ------------------------------------------------------------
    # --out: the generator alone (build.py compiles in its own scratch folder). Without it, a standalone
    # run lands where every other builder's output does: md/ and pdf/ under the work folder, compiled.
    standalone = args.out is None
    if standalone:
        from tribonian.tconfig import WORK
        md_root, tex_root = str(WORK / "md"), str(WORK / "build")
    else:
        md_root = tex_root = args.out
    os.makedirs(md_root, exist_ok=True)
    os.makedirs(tex_root, exist_ok=True)
    for meta, nodes, fns, refs, nmd, _ in docs:   # markdown: always one file per section
        md_dir = os.path.join(md_root, meta["mdsubdir"])
        os.makedirs(md_dir, exist_ok=True)
        md_path = os.path.join(md_dir, slug(meta) + ".md")
        with open(md_path, "w", encoding="utf-8") as f:
            derived = cited_with(meta, args)
            meta["derived"] = derived                 # the PDF prints the same sections (render_tex)
            f.write(render_md(None, nodes, meta, refs, fns, nmd, args.credits, derived))
        print("wrote %s" % md_path)

    tex_path = os.path.join(tex_root, base + ".tex")
    with open(tex_path, "w", encoding="utf-8") as f:
        f.write(render_tex([(m, n, fn, nt) for m, n, fn, _, _, nt in docs], doctitle, dochead,
                           args.font, args.style, first["release"], args.credits, args.notes))
    print("wrote %s" % tex_path)
    if not standalone:
        print("compile: pdflatex %s   (run twice)" % os.path.basename(tex_path))
        return 0
    from pathlib import Path
    from tribonian.case2md import run_pdflatex, unmapped_note
    pdf_root = WORK / "pdf"
    pdf_root.mkdir(exist_ok=True)
    pdf = run_pdflatex(Path(tex_path), pdf_root)
    unmapped_note()
    if not pdf:
        return 1
    print("wrote %s" % os.path.relpath(pdf, WORK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
