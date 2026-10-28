"""PDF -> HTML blocks for the html-paragraphs family (states that publish only PDF: Wyoming, Iowa, ...).

pdftotext -layout keeps each printed line with its indentation; this joins the lines back into paragraphs and
writes one <p> per paragraph, with style="margin-left:<n>pt" from the paragraph's first-line indentation (n = spaces),
so a formula can use [html].indent_css = {unit = ..., base = ...} for the ladder. Settings, [<ST>.<code>.pdf]:
  drop   = [regex, ...]   whole lines that are page furniture: running heads, page numbers, footers (counted, logged)
  join   = "indent"       (default) a line starts a new paragraph when it is indented more than the body margin,
                          follows a blank line, or matches `starts`; otherwise it continues the paragraph above
           "blank"        only a blank line (or `starts`) starts a paragraph
           "none"         every line is its own paragraph
           "hanging"      a line continues the paragraph when it is back at the body margin, or the paragraph opens
                          with a label and the line starts at or right of the label's text column (Oklahoma's
                          "a.  text" items); any other line starts a paragraph
  starts = [regex, ...]   lines that always start a paragraph (section lines, labels "(a)", headings)
  starts_after = "sentence"  ... only when the text above ended a sentence (. : ; ? ! ) ]) or after a blank/page top
  center = n              a line indented at least n spaces and about as far from the right edge (center_tol spaces,
                          default max(6, indent/4)) is a centred heading (<p align=center>)
  hyphen = true           "inter-" + "state" across lines -> "interstate" (default: "inter-state", hyphen kept, no space);
                          a soft hyphen (U+00AD) at a line end is always dropped and the word joined
  pages  = "1-"           page range passed to pdftotext (-f/-l), e.g. "3-" to skip a cover
  label  = regex          what counts as a label for "hanging" (default: (a) a. 1. A. (1) (iv) and similar)
Blank lines next to a dropped line (the gap around a page footer) do not end a paragraph, and a page break is a line
break (page = "break": a blank line), so text runs across pages. Each page's left margin is aligned to the smallest
(page_margin = false: keep pdftotext's columns as they are).
Text is never dropped except by `drop`; the joined text is checked against pdftotext's own (whitespace aside)."""
import html
import re
import subprocess
import sys
import tempfile


def _pdftotext(raw, pages):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
        fh.write(raw)
        fh.flush()
        cmd = ["pdftotext", "-layout", "-enc", "UTF-8"]
        if pages:
            lo, _, hi = str(pages).partition("-")
            if lo:
                cmd += ["-f", lo]
            if hi:
                cmd += ["-l", hi]
        try:
            return subprocess.run(cmd + [fh.name, "-"], check=True, capture_output=True).stdout.decode("utf-8")
        except FileNotFoundError:
            sys.exit("pdftotext not found: install poppler-utils (apt install poppler-utils)")


def to_html(raw, formula):
    cfg = (formula or {}).get("pdf", {})
    drop = [re.compile(x) for x in cfg.get("drop", [])]
    starts = [re.compile(x) for x in cfg.get("starts", [])]
    join = cfg.get("join", "indent")
    center = cfg.get("center")
    pages = _pdftotext(raw, cfg.get("pages")).split("\f")
    # each page's own left margin (it shifts between odd and even pages: North Dakota) is brought to the smallest,
    # and a page break is a line break, not a blank line (page = "break" makes it one): text runs across pages
    margins = []
    for pg in pages:
        ls = [l for l in pg.split("\n") if l.strip() and not any(rx.search(l) for rx in drop)]
        margins.append(min((len(l) - len(l.lstrip(" ")) for l in ls), default=0))
    base = min((m for m, pg in zip(margins, pages) if pg.strip()), default=0)
    lines = []
    for m, pg in zip(margins, pages):
        shift = (m - base) if cfg.get("page_margin", True) else 0
        pl = [l[shift:] if l[:shift].strip() == "" else l for l in pg.split("\n")]
        if pl and pl[-1] == "":           # the newline that ends each page is not a blank line
            pl.pop()
        if lines and cfg.get("page") == "break":
            lines.append("")
        lines += pl
    body = [l for l in lines if l.strip() and not any(rx.search(l) for rx in drop)]
    margin = min((len(l) - len(l.lstrip(" ")) for l in body), default=0)      # the body's left margin
    width = max((len(l.rstrip()) for l in body), default=80)                    # the page's text width
    def starts_here(s):
        """a `starts` line opens a paragraph; with starts_after = "sentence" only when the text above ended a sentence
        (or a blank line / page top came first): a cross-reference wrapped to a line start ("12.1-32-07. Any ...")
        is not a new section (North Dakota)"""
        if not any(rx.match(s) for rx in starts):
            return False
        if cfg.get("starts_after") != "sentence" or blank or not paras:
            return True
        return bool(re.search(r"""[.:;!?)\]"']\s*$""", paras[-1][1]))

    label_rx = re.compile(cfg.get("label", r"(?:\(?[0-9]{1,3}[a-z]?[.)]|\(?[A-Za-z]{1,2}[.)]|\([ivxlcIVXLC]{1,6}\)|§+\s*[0-9][^ ]*)\s+"))
    paras, blank, dropped, after_drop, pending_blank = [], True, 0, False, False
    for line in lines:
        if not line.strip():
            if not after_drop:
                pending_blank = True
            continue
        if any(rx.search(line) for rx in drop):
            dropped += 1
            after_drop, pending_blank = True, False      # the gap before and after page furniture is no break
            continue
        blank, after_drop, pending_blank = pending_blank or not paras, False, False
        ind = len(line) - len(line.lstrip(" "))
        lead = ind - margin
        s = line.strip()
        s_one = re.sub(r"\s{2,}", " ", s)               # layout gaps inside a line (columns of a heading) -> one space
        right = width - (ind + len(s))                   # centred: indented at least `center` AND about as much
        cen = center is not None and lead >= center and abs(right - ind) <= cfg.get("center_tol", max(6, ind // 4))   # room right as left
        if join == "hanging" and paras and not blank:
            labelled = paras[-1][3] is not None
            cont_ok = lead == 0 or (labelled and ind >= paras[-1][3])   # back at the body margin, or under the
            new = cen or starts_here(s) or not cont_ok or paras[-1][2]   # text of a label
        else:
            new = (join == "none" or blank or cen or starts_here(s)
                   or (join == "indent" and lead > 0) or not paras or paras[-1][2])
        if new:
            m = label_rx.match(s)
            cont = ind + m.end() if m else None            # a label's text column: its wrapped lines start there
            paras.append([lead, s_one, cen, cont])
        else:
            prev = paras[-1][1]
            if prev.endswith("\u00ad"):                                  # soft hyphen: a word broken by the layout
                paras[-1][1] = prev[:-1] + s_one
            elif prev.endswith("-") and re.match(r"[a-z]", s_one):       # "off-" + "road": no space; the hyphen is
                paras[-1][1] = (prev[:-1] if cfg.get("hyphen") else prev) + s_one   # kept unless hyphen = true
            else:
                paras[-1][1] = prev + " " + s_one
        blank = False
    out = ['<html><body><div id="pdf">']
    for lead, s, cen, _ in paras:
        if cen:
            out.append('<p align="center">%s</p>' % html.escape(s))
        else:
            out.append('<p style="margin-left:%dpt">%s</p>' % (max(lead, 0), html.escape(s)))
    out.append("</div></body></html>")
    # check: every non-space character pdftotext gave (bar dropped lines; hyphen joins) is in the HTML, in order
    nos = lambda t: re.sub(r"[\s\u00ad]+", "", t)
    src = nos("\n".join(l for l in lines if l.strip() and not any(rx.search(l) for rx in drop)))
    got = nos(" ".join(p[1] for p in paras))
    if not cfg.get("hyphen") and src != got:
        k = next((i for i, (a, b) in enumerate(zip(src, got)) if a != b), min(len(src), len(got)))
        sys.exit("pdf-text lost text at %d: %r / %r" % (k, src[k - 40:k + 40], got[k - 40:k + 40]))
    if dropped:
        print("  pdf-text: %d furniture lines dropped by [pdf].drop" % dropped)
    return "\n".join(out).encode("utf-8")
