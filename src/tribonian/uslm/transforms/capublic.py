"""capublic.py -- California PUBINFO ("capublic" database) code tables -> one code's HTML for html-paragraphs.

The Legislative Counsel's pubinfo_<session>.zip holds the codes as MySQL load files (columns and field format from
the <table>.sql LOAD DATA scripts in pubinfo_load.zip):
    tab-separated, text optionally enclosed in backquotes, LINES TERMINATED BY '\\n', MySQL escapes (\\t \\n \\\\ ...),
    bare NULL = no value; LAW_SECTION_TBL's content_xml cell is the NAME of a member LAW_SECTION_TBL_<n>.lob whose
    text is CAML: <caml:Content xmlns:caml=...><p>(a)<span class="EnSpace"/>text</p>...</caml:Content>
Tables used: LAW_TOC_TBL (the container tree: heading, node_level, node_sequence, node_treepath, history_note),
LAW_TOC_SECTIONS_TBL (which sections sit under which node, section_order), LAW_SECTION_TBL (section_num, history,
the .lob). One item = one law_code (PEN, GOV, ... CONS).

Output, in node_sequence order:
    <div id="pubinfo" data-code="PEN">
    <p class="toc" data-level="3" data-path="1.8">TITLE 8. OF CRIMES AGAINST THE PERSON [187. - 248.]</p>
    <p class="tocnote">Title 8 enacted 1872.</p>                   history_note, when present
    <p class="sec"><b>187.</b></p>                                  LAW_SECTION_TBL.section_num
    <p>(a) Murder is ...</p>                                        the .lob's children; EnSpace -> one space
    <p class="history">Amended by Stats. 1996, Ch. 1023, Sec. 385.</p>
Adds no text and drops none: check() compares the non-blank characters of every source value written with the
output's text, in order. A .lob named by a row but absent from the archive is an error (missing="skip" for tests)."""
import html, re, sys
from lxml import etree, html as lhtml

COLUMNS = {   # pubinfo_load.zip <table>.sql, 2021-05 (confirmed 2026-10)
    "LAW_TOC_TBL": ["law_code", "division", "title", "part", "chapter", "article", "heading", "active_flg", "trans_uid",
                    "trans_update", "node_sequence", "node_level", "node_position", "node_treepath",
                    "contains_law_sections", "history_note", "op_statues", "op_chapter", "op_section"],
    "LAW_TOC_SECTIONS_TBL": ["id", "law_code", "node_treepath", "section_num", "section_order", "title", "op_statues",
                             "op_chapter", "op_section", "trans_uid", "trans_update", "law_section_version_id", "seq_num"],
    "LAW_SECTION_TBL": ["id", "law_code", "section_num", "op_statues", "op_chapter", "op_section", "effective_date",
                        "law_section_version_id", "division", "title", "part", "chapter", "article", "history",
                        "content_xml", "active_flg", "trans_uid", "trans_update"],
}
_ESC = {"t": "\t", "n": "\n", "r": "\r", "0": "\0", "Z": "\x1a"}
_CACHE = {}


def _field(s):
    if s == "NULL":
        return None
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        s = s[1:-1]
    return re.sub(r"\\(.)", lambda m: _ESC.get(m.group(1), m.group(1)), s) if "\\" in s else s


def _member(z, name):
    want = name.upper()
    for n in z.namelist():
        if n.upper().rsplit("/", 1)[-1] == want:
            return n
    return None


def table(z, name):
    """rows of <name>.dat as dicts, grouped by law_code (cached per archive)"""
    key = (getattr(z, "filename", id(z)), name)
    if key not in _CACHE:
        cols, m = COLUMNS[name], _member(z, name + ".dat")
        if m is None:
            raise SystemExit("capublic: %s.dat is not in the archive" % name)
        by_code = {}
        for i, line in enumerate(z.read(m).decode("utf-8").split("\n")):
            if not line:
                continue
            vals = line.split("\t")
            if len(vals) != len(cols):
                raise SystemExit("capublic: %s.dat line %d has %d fields, expected %d" % (name, i + 1, len(vals), len(cols)))
            r = dict(zip(cols, map(_field, vals)))
            by_code.setdefault(r["law_code"], []).append(r)
        _CACHE[key] = by_code
    return _CACHE[key]


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("inf")


BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "table", "ul", "ol", "blockquote"}
INLINE_RENAME = {"b": "strong"}     # <b> is the formula's section-opening tag (p.sec > b): law text's bold becomes <strong>


def _local(e):
    return e.tag.split("}")[-1] if isinstance(e.tag, str) else ""


def _has_block(e):
    return any(_local(d) in BLOCK for d in e.iterdescendants())


def _inline(e):
    """an inline element as HTML: CAML-only elements (Fraction, Numerator, LabelledField, TipIn ...) become
    <span class="Name">; empty *Space spans carry one space (a self-closed <span/> would swallow the line in HTML)"""
    t = _local(e)
    if t in ("span", "i", "u", "sub", "sup", "br", "b", "strong", "em"):
        tag, cls = INLINE_RENAME.get(t, t), e.get("class")
    else:
        tag, cls = "span", t
    if tag == "br":
        return "<br>" + html.escape(e.tail or "")
    inner = html.escape(e.text or "")
    if not inner and not len(e) and (cls or "").endswith("Space"):
        inner = " "
    inner += "".join(_inline(c) for c in e)
    return "<%s%s>%s</%s>%s" % (tag, ' class="%s"' % html.escape(cls) if cls else "", inner, tag, html.escape(e.tail or ""))


def _blocks(e, out):
    """block element e -> flat <p> lines (+ <table>s): HTML forbids a <p>, <h1> or <table> inside a <p>, and its parser
    would close the outer <p> and move the text (FAM's <p><h1>IMPORTANT NOTICE</h1></p>). A block nested in a block
    or in an inline element (b > p, LabelledField > p) ends the current line and becomes its own line; a heading
    element becomes <p class="h1"> (law text, not a container); text and inline children stay in order."""
    t = _local(e)
    if t == "table":
        s = etree.tostring(e, encoding="unicode", method="html", with_tail=False)
        out.append(re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", s))
        return
    cls = e.get("class") if t == "p" else (t if t in BLOCK else None)
    cur = [html.escape(e.text or "")]

    def flush():
        line = "".join(cur)
        if re.sub(r"\s+", "", re.sub(r"<[^>]*>", "", line)):
            out.append("<p%s>%s</p>" % (' class="%s"' % html.escape(cls) if cls else "", line))
        cur[:] = []

    for c in e:
        if not isinstance(c.tag, str):
            cur.append(html.escape(c.tail or ""))
        elif _local(c) in BLOCK or _has_block(c):
            flush()
            _blocks(c, out)
            cur.append(html.escape(c.tail or ""))
        else:
            cur.append(_inline(c))
    flush()


def _body(xml):
    """the .lob's <caml:Content> children as flat HTML lines for html-paragraphs (no text added or dropped)"""
    root = etree.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out = []
    _blocks(root, out)
    return "".join(out), "".join(root.itertext())


def assemble(z, item, missing="error"):
    toc = sorted((r for r in table(z, "LAW_TOC_TBL").get(item, []) if (r["active_flg"] or "Y") == "Y"),
                 key=lambda r: _num(r["node_sequence"]))
    if not toc:
        raise SystemExit("capublic: no LAW_TOC_TBL rows for code %s" % item)
    under = {}
    for t in table(z, "LAW_TOC_SECTIONS_TBL").get(item, []):
        under.setdefault(t["node_treepath"], []).append(t)
    secs = {s["law_section_version_id"]: s for s in table(z, "LAW_SECTION_TBL").get(item, []) if (s["active_flg"] or "Y") == "Y"}
    out, src, lost, placed = ['<div id="pubinfo" data-code="%s">' % html.escape(item)], [], [], set()
    for n in toc:
        out.append('<p class="toc lvl%s" data-level="%s" data-path="%s">%s</p>' % (
            html.escape(n["node_level"] or ""), html.escape(n["node_level"] or ""), html.escape(n["node_treepath"] or ""), html.escape(n["heading"] or "")))
        src.append(n["heading"] or "")
        if n["history_note"] and n["history_note"].strip():
            out.append('<p class="tocnote">%s</p>' % html.escape(n["history_note"].strip()))
            src.append(n["history_note"])
        for t in sorted(under.get(n["node_treepath"], []), key=lambda t: _num(t["section_order"])):
            s = secs.get(t["law_section_version_id"])
            if s is None:
                raise SystemExit("capublic: %s %s: version %s not in LAW_SECTION_TBL" % (item, t["section_num"], t["law_section_version_id"]))
            lob = _member(z, s["content_xml"] or "")
            if lob is None:
                if missing == "skip":
                    lost.append(s["section_num"])
                    continue
                raise SystemExit("capublic: %s %s: member %s is not in the archive" % (item, s["section_num"], s["content_xml"]))
            placed.add(t["law_section_version_id"])
            body, text = _body(z.read(lob))
            out.append('<p class="sec"><b>%s</b></p>' % html.escape(s["section_num"]))
            out.append(body)
            src += [s["section_num"], text]
            if s["history"] and s["history"].strip():
                out.append('<p class="history">%s</p>' % html.escape(s["history"].strip()))
                src.append(s["history"])
    out.append("</div>")
    orphans = [s["section_num"] for v, s in secs.items() if v not in placed and s["section_num"] not in lost]
    if orphans and missing != "skip":
        raise SystemExit("capublic: %s: %d sections in no TOC node (%s ...)" % (item, len(orphans), ", ".join(orphans[:5])))
    data = "\n".join(out)
    check(src, data, item)
    if lost:
        print("capublic: %s: %d sections skipped, .lob not in the archive (test mode)" % (item, len(lost)), file=sys.stderr)
    return data.encode("utf-8")


def check(src, data, item):
    a = re.sub(r"\s+", "", "".join(src))
    b = re.sub(r"\s+", "", lhtml.fromstring(data).text_content())
    if a != b:
        i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
        raise SystemExit("capublic: %s: output text differs from the tables at char %d:\n  src: %s\n  out: %s"
                         % (item, i, a[max(0, i - 60):i + 60], b[max(0, i - 60):i + 60]))
