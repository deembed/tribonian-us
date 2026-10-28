"""Source formats that are not HTML, turned into HTML blocks for the html-paragraphs family before parsing.
A formula names one with `transform = "<name>"`. Each takes the raw bytes and returns UTF-8 HTML; a transform must not
drop text (each is checked on real samples: conservation then runs on its HTML)."""
import importlib


def _ny(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.nysenate").to_html(raw)


def _ut(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.utah_xml").convert(raw).encode("utf-8")


def _al(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.alison").to_html(raw)


def _pdf(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.pdf_text").to_html(raw, formula)


def _ia(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.iowa_xml").to_html(raw, formula)


def _doc(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.pandoc_doc").to_html(raw, formula)


def _docx(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.docx_styles").to_html(raw, formula)


def _text(raw, formula):
    return importlib.import_module("tribonian.uslm.transforms.text_lines").to_html(raw, formula)


TRANSFORMS = {
    "nysenate-json": _ny,     # New York Open Legislation API: /api/3/laws/{lawId}?full=true
    "utah-xml": _ut,          # Utah xcode whole-title XML
    "alison-json": _al,       # Alabama ALISON GraphQL answer (sections of a chapter)
    "pdf-text": _pdf,         # PDF via pdftotext -layout, lines joined to paragraphs ([pdf] settings)
    "iowa-xml": _ia,          # Iowa Code per-chapter _slim.xml
    "pandoc": _doc,           # RTF / Word .docx via pandoc ([pandoc].from = "rtf" | "docx")
    "text-lines": _text,
    "docx-styles": _docx,     # Word .docx by paragraph style, pure Python ([docx] settings): Colorado CCR rules      # plain text, one paragraph per line (New Jersey bulk .TXT)
}


def apply(raw_bytes, formula):
    name = formula.get("transform")
    if not name:
        return raw_bytes, False
    if name not in TRANSFORMS:
        raise SystemExit("unknown transform %r (known: %s)" % (name, ", ".join(sorted(TRANSFORMS))))
    return TRANSFORMS[name](raw_bytes, formula), True
