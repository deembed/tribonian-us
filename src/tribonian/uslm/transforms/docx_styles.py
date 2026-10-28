"""Word .docx -> HTML blocks by paragraph style, for the html-paragraphs family (the Code of Colorado Regulations'
rules). Pure Python: reads word/document.xml from the zip, no LibreOffice.

[<ST>.<code>.docx] in the formula:
    center  styles that become centered headings (containers):  ["Heading1"]
    bold    styles whose whole text is a bold opening run (a section start): ["Heading2"]
    indent  {style = level}: that many levels of non-breaking-space indent (html.indent per level), a hint
            for the ladder between two plausible levels: { par1 = 0, par2 = 1, par3 = 2 }
Every other paragraph is a plain <p>. A table becomes one <p> per row, cells joined by " | ". Nothing is dropped:
empty paragraphs give no block, and every run of text lands in a block in document order."""
import html as _html
import io
import re
import zipfile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(p):
    out = []
    for el in p.iter():
        if el.tag == W + "t":
            out.append(el.text or "")
        elif el.tag in (W + "tab", W + "br", W + "cr"):
            out.append(" ")
    return " ".join("".join(out).split())


def _style(p):
    ps = p.find(W + "pPr/" + W + "pStyle")
    return ps.get(W + "val") if ps is not None else ""


def to_html(raw, formula):
    from lxml import etree
    cfg = (formula or {}).get("docx", {})
    center, bold = set(cfg.get("center", ["Heading1"])), set(cfg.get("bold", ["Heading2"]))
    indent = cfg.get("indent", {})
    per = int((formula or {}).get("html", {}).get("indent", 0) or 0) or 4
    body = etree.fromstring(zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml")).find(W + "body")
    out = ["<html><body><div id=\"doc\">"]
    for el in body:
        if el.tag == W + "p":
            t = _text(el)
            if not t:
                continue
            st, e = _style(el), _html.escape(t, quote=False)
            if st in center:
                out.append('<p align="center">%s</p>' % e)
            elif st in bold:
                out.append("<p><b>%s</b></p>" % e)
            else:
                out.append("<p>%s%s</p>" % ("&#160;" * per * int(indent.get(st, 0)), e))
        elif el.tag == W + "tbl":
            for tr in el.iter(W + "tr"):
                cells = [" ".join(_text(p) for p in tc.iter(W + "p") if _text(p)) for tc in tr.iter(W + "tc")]
                cells = [c for c in cells if c]
                if cells:
                    out.append("<p>%s</p>" % _html.escape(" | ".join(cells), quote=False))
        elif el.tag == W + "sdt":                       # a content control: its paragraphs, in order
            for p in el.iter(W + "p"):
                t = _text(p)
                if t:
                    out.append("<p>%s</p>" % _html.escape(t, quote=False))
    out.append("</div></body></html>")
    return "\n".join(out).encode("utf-8")
