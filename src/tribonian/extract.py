"""extract.py -- a filed PDF -> the text the builds read, with its footnotes found from the PDF's own type.

The PDF is the only input kept; everything here can be regenerated (`tribonian scotus extract`). For
<docket>/<name>.pdf it writes

  <docket>/extracted/<name>.txt   pdftotext -layout's lines, page by page (form feeds), with each page's
                                  footnotes moved out of the body into a block at the end of the page, and
                                  each footnote mark in the body set in look-alike digits (MARK_DIGITS)
  <docket>/extracted/<name>.json  what was found, and the extractor version and the PDF's size and date
                                  (`extract --stale` redoes a brief when either changes)

Why both pdftotext and pdfplumber: the paragraph rules (case2md.recap_blocks) are tuned to pdftotext's
columns, but pdftotext sets a superscript on the line as a plain digit ("in 1822.62": the year, then note 62).
pdfplumber gives each character's size and baseline. The two reads of a page are aligned character by
character (whitespace aside), and the size and raise carry over to pdftotext's characters:
  - a mark: digits smaller than the text before them and raised above it
  - a footnote line: most of its characters set smaller than the brief's body (the most common size)
A page where that fails (no notes block found under its marks, or a font pdfplumber cannot map) is left as
pdftotext wrote it, and the builds' text rules (case2md.split_footnotes / place_marks) read it as before.

The block, in the text file:
  NOTES_START                     a line of its own
  NOTE_LABEL + "12"               note 12 starts; its lines follow, as pdftotext set them
  ...                             a blank line: a new paragraph in the note
  lines before the first label    the rest of the previous page's last note
"""

import json
import re
import subprocess
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from tribonian import rules, trace

VERSION = 4                     # 2: a mark is matched to its note anywhere later in the brief (endnotes)
                                # 3: labels in order, "6. Text" labels, a wrapped "12, 2024)." is not a label
                                # 4: numbering that starts again at 1 (an appendix's own opinions); "5.56mm" is not a label
NOTES_START = ""
NOTE_LABEL = ""
MARK_DIGITS = "".join(chr(0xE030 + d) for d in range(10))   # a footnote mark's digits: same width as 0-9
TO_MARK = str.maketrans("0123456789", MARK_DIGITS)
FROM_MARK = str.maketrans(MARK_DIGITS, "0123456789")
DROP_MARK = {ord(ch): None for ch in MARK_DIGITS}
MARK_RE = re.compile("[%s]+" % MARK_DIGITS)

SMALL = rules.PARAMS.small      # a footnote's type, or a mark: below this share of the body size
RAISE = rules.PARAMS.raise_pt   # points a mark's baseline sits above the character before it
FOOT_RE = re.compile(r"^\s*(?:[(\[]?(?:\d{1,4}a?|[ivxlcdm]{1,8})[)\]]?|-\s*\d+\s*-)\s*$", re.I)   # a page number
RULE_RE = re.compile(r"^\s*[_—–-]{5,}\s*$")


def marks_to_digits(s):
    """A mark's look-alike digits back to plain digits (a heading's own note number, a safety net before TeX)."""
    return s.translate(FROM_MARK)


def drop_marks(s):
    """Text for searching (cites, quotes): the marks taken out, so "U.S. 120.62" does not read as page 120.62."""
    return s.translate(DROP_MARK)


def plain(text):
    """The extracted text as plain pdftotext lines would read: notes blocks left out, marks dropped."""
    out = []
    for pg in text.split("\f"):
        k = pg.find("\n" + NOTES_START)
        out.append(pg[:k] if k >= 0 else pg)
    return drop_marks("\f".join(out))


def out_paths(pdf):
    pdf = Path(pdf)
    d = pdf.parent / "extracted"
    return d / (pdf.stem + ".txt"), d / (pdf.stem + ".json")


def stale(pdf):
    """True when the extracted files are missing or older than the PDF or this extractor."""
    txt, meta = out_paths(pdf)
    if not txt.exists() or not meta.exists():
        return True
    try:
        m = json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return True
    st = Path(pdf).stat()
    return m.get("version") != VERSION or m.get("pdf_bytes") != st.st_size or m.get("pdf_mtime") != int(st.st_mtime)


# ---------------------------------------------------------------- the PDF's own type

def plumber_pages(pdf):
    """-> (body size, [page: [(char, size, raised mark?)]]) in content order, whitespace left out.
    A glyph pdfplumber cannot map ("(cid:49)") is kept as a character that matches nothing."""
    import pdfplumber
    pages, sizes = [], Counter()
    with pdfplumber.open(str(pdf)) as doc:
        for pg in doc.pages:
            ch = [c for c in pg.chars if c["text"].strip()]
            sizes.update(round(c["size"], 1) for c in ch)
            out, ref, last = [], None, None      # ref: the text a mark is measured against
            for c in ch:
                t = c["text"] if len(c["text"]) == 1 else "\x00"
                raised = False
                if t.isdigit() and ref is not None and c["size"] < ref["size"] * 0.95 and c["bottom"] < ref["bottom"] - RAISE:
                    raised = True
                elif t.isdigit() and out and out[-1][2] and abs(c["bottom"] - last["bottom"]) < 0.5 \
                        and abs(c["size"] - last["size"]) < 0.3:
                    raised = True                # the next digit of the same mark
                else:
                    ref = c
                out.append((t, c["size"], raised))
                last = c
            pg.flush_cache()
            pages.append(out)
    body = sizes.most_common(1)[0][0] if sizes else 0
    return body, pages


def pdftotext_pages(pdf):
    r = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, timeout=900)
    if r.returncode:
        raise RuntimeError("pdftotext failed: %s" % r.stderr.decode(errors="replace")[:200])
    return r.stdout.decode("utf-8", errors="replace").replace("\r", "").split("\f")


def align(page_text, chars):
    """-> {offset in page_text: (size, raised)} for the characters the two reads share."""
    pos = [i for i, ch in enumerate(page_text) if not ch.isspace()]
    a = "".join(page_text[i] for i in pos)
    b = "".join(c[0] for c in chars)
    attr = {}
    sm = SequenceMatcher(None, a, b, autojunk=False)
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            attr[pos[i + k]] = chars[j + k][1:]
    return attr, (sum(n for _, _, n in sm.get_matching_blocks()) / len(a) if a else 1.0)


# ---------------------------------------------------------------- one page

def page_marks(lines, starts, attr, upto):
    """Raised small digits in lines[:upto] -> [(line, start col, end col, number)]."""
    marks = []
    for k in range(upto):
        l, run = lines[k], None
        for i, ch in enumerate(l + " "):
            a = attr.get(starts[k] + i)
            if ch.isdigit() and a and a[1]:
                run = (run[0], i + 1) if run else (i, i + 1)
            elif run:
                marks.append((k, run[0], run[1], int(l[run[0]:run[1]])))
                run = None
    return marks


# A line in the notes area that starts with a number: is it the next note's label? The first rule that holds
# decides (rules.py): "label:..." starts a note, "not-label:..." keeps the line in the note above.
LABEL = rules.Decision("label", default="not-label:out-of-order")


@LABEL.rule("not-label:wrapped-line")           # "12, 2024)." under "... (Feb.": a cite running on
def _(c):
    return bool(re.match(r"[,;:)\]]", c.rest))


@LABEL.rule("not-label:decimal-number")         # "5.56mm NATO caliber": a number, not note 5
def _(c):
    return bool(re.match(r"[.,]\d", c.rest))


@LABEL.rule("not-label:alone-not-expected")     # a number alone on its line that is neither due nor a mark here
def _(c):
    return not c.rest and not (c.n in c.want or c.n == c.nxt or restart(c))


@LABEL.rule("label:next-number")                # the count going on
def _(c):
    return c.n == c.nxt


@LABEL.rule("label:next-mark-on-page")          # the page's next raised mark (a note lost before it costs only that one)
def _(c):
    return c.n == c.ahead


@LABEL.rule("label:skip-one")                   # no mark read on the page: one number past the count
def _(c):
    return c.ahead is None and c.n == c.nxt + 1


@LABEL.rule("label:restart-at-1")               # the numbering starts again (an appendix's own opinions): a raised 1 here
def _(c):
    return restart(c)


def restart(c):
    return c.n == 1 and c.nxt > 1 and 1 in c.want


def split_page(text, attr, body, nxt, prev_notes):
    """One pdftotext page -> (body lines, notes block lines, marks in the body lines, labels, next label,
    notes found?). No notes found: the page's lines as they are, and its marks (a note printed later, at the
    end of a section or the brief, can be theirs)."""
    lines = text.split("\n")
    starts, o = [], 0
    for l in lines:
        starts.append(o)
        o += len(l) + 1
    none = lambda: (lines, [], page_marks(lines, starts, attr, len(lines)), [], nxt, False)

    def kind(k):
        """'small' (a footnote's type), 'body', or None (too few characters read)."""
        l = lines[k]
        got = [attr[starts[k] + i] for i, ch in enumerate(l) if not ch.isspace() and starts[k] + i in attr]
        got = [g for g in got if not g[1]]      # marks aside
        if len(got) < 3:
            return None
        small = sum(sz < body * SMALL for sz, _ in got)
        return "small" if small >= 0.6 * len(got) else "body"

    # the notes: from the foot of the page up, small lines (and blank, rule or unreadable ones), past the
    # page number and any running foot set in body type below them
    k = len(lines) - 1
    while k >= 0 and (not lines[k].strip() or FOOT_RE.match(lines[k])):
        k -= 1
    end = k + 1
    top = None
    while k >= 0:
        l = lines[k]
        if not l.strip() or RULE_RE.match(l):
            k -= 1
            continue
        kd = kind(k)
        if kd == "body":
            break
        if kd == "small":
            top = k
        k -= 1
    if top is None:
        trace.hit("region", "none:no-small-type-at-foot", keep=False)
        return none()
    k = top - 1                                  # short lines just above it (a label set alone: "1")
    while k >= 0:
        if lines[k].strip():
            if kind(k) is not None:
                break
            top = k
        k -= 1
    marks = page_marks(lines, starts, attr, top)  # in the body above the notes
    want = {m[3] for m in marks}
    # the notes' labels: a line that starts with the next number (a mark on this page, or the count going on)
    notes, cur, prev_blank, first_label = [], None, False, None
    for k in range(top, end):
        l = lines[k]
        if RULE_RE.match(l):
            continue
        m = re.match(r"^\s*(\d{1,3})\.?(?=\s|[^\d\s]|$)\s*(.*)$", l)      # "6 Text", "6. Text", "6" alone
        n = int(m.group(1)) if m else None
        ahead = min((w for w in want if w >= nxt), default=None)   # the next mark on the page still unclaimed
        # LABEL: in order, the count going on or the page's next mark; with the note's text, or alone on its line
        ok = m is not None and LABEL.decide(rules.ctx(n=n, nxt=nxt, ahead=ahead, want=want, rest=m.group(2)),
                                            None, l).startswith("label:")
        if ok:
            cur = [str(n), [[m.group(2)] if m.group(2) else []]]
            notes.append(cur)
            nxt = n + 1
            prev_blank = False
            if first_label is None:
                first_label = k
            continue
        if not l.strip():
            prev_blank = True
            continue
        if cur is None:
            cur = [None, [[]]]                   # the rest of the last page's note
            notes.append(cur)
        if prev_blank and cur[1][-1] and re.search(r"[.)\]”\"’:]\s*$", cur[1][-1][-1]):
            cur[1].append([])                    # a blank line after a sentence: a paragraph
        cur[1][-1].append(l.strip())
        prev_blank = False
    labelled = [n for n in notes if n[0] is not None]
    if notes and notes[0][0] is None and not prev_notes:
        if not labelled:
            trace.hit("region", "none:small-type-no-label", None, lines[top])
            return none()                        # small type at the foot, no note number, nothing to continue
        trace.hit("region", "kept-in-body:small-lines-above-first-label", None, lines[top])
        top, notes = first_label, notes[1:]      # small lines above the first note stay in the body
        marks = page_marks(lines, starts, attr, top)
    trace.hit("region", "found:continues-last-page" if notes and notes[0][0] is None else "found", keep=False)
    body_lines = lines[:top] + lines[end:]       # the marks' line numbers hold: they are all above top
    block = [NOTES_START]
    for lab, paras in notes:
        if lab is not None:
            block.append(NOTE_LABEL + lab)
        for i, p in enumerate(paras):
            if i:
                block.append("")
            block += p
    return body_lines, block, marks, [n[0] for n in labelled], nxt, True


# ---------------------------------------------------------------- one PDF

def extract(pdf, write=True):
    """pdf -> (extracted text, summary dict); writes extracted/<name>.txt and .json unless write=False."""
    pdf = Path(pdf)
    with trace.collect(keep=()) as (counts, _):
        text, summary = _extract(pdf)
    summary["rules"] = {"%s/%s" % k: n for k, n in sorted(counts.items())}   # which rule decided, how often
    if write:
        txt, meta = out_paths(pdf)
        txt.parent.mkdir(exist_ok=True)
        txt.write_text(text, encoding="utf-8")
        meta.write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    return text, summary


def _extract(pdf):
    texts = pdftotext_pages(pdf)
    try:
        body, chars = plumber_pages(pdf)
    except Exception as e:                       # pdfplumber cannot read it: pdftotext's text as it is
        body, chars, err = 0, [], repr(e)[:200]
    else:
        err = None
    pages, notes, nxt, prev, unread = [], [], 1, False, 0      # pages: [body lines, block, marks]
    for pi, t in enumerate(texts):
        if pi >= len(chars) or not t.strip():
            pages.append([t.split("\n"), [], []])
            prev = False
            continue
        attr, share = align(t, chars[pi])
        if share < rules.PARAMS.agree:           # the two reads disagree (an unmapped font): leave the page be
            trace.hit("page", "unread:reads-disagree", pi, t.strip()[:80])
            unread += 1
            pages.append([t.split("\n"), [], []])
            prev = False
            continue
        lines, block, marks, labels, nxt2, ok = split_page(t, attr, body, nxt, prev)
        if ok:
            nxt = nxt2
            notes += [(lab, pi) for lab in labels]
        pages.append([lines, block, marks])
        prev = ok
    # each note's mark: the last raised digit run with its number at or before the note's page and after the
    # mark before it (a note under its mark, or notes gathered at the end of a section). A raised digit no note
    # claims stays a digit.
    marked, after = 0, (-1, -1, -1)
    for lab, npi in notes:
        cands = [(pi, k, a, b) for pi in range(npi + 1) for k, a, b, num in pages[pi][2]
                 if num == int(lab) and (pi, k, a) > after]
        if not cands:
            trace.hit("mark", "none:no-raised-digits-before-note", npi, "note %s" % lab)
            continue
        pi, k, a, b = max(cands)
        trace.hit("mark", "mark:same-page" if pi == npi else "mark:earlier-page", keep=False)
        l = pages[pi][0][k]
        pages[pi][0][k] = l[:a] + l[a:b].translate(TO_MARK) + l[b:]
        after, marked = (pi, k, a), marked + 1
    text = "\f".join("\n".join(lines + block) for lines, block, _ in pages)
    summary = {"version": VERSION, "pdf": pdf.name, "pdf_bytes": pdf.stat().st_size, "pdf_mtime": int(pdf.stat().st_mtime),
               "pages": len(texts), "body_size": body, "notes": len(notes), "marked": marked,
               "unread_pages": unread, "how": "superscript" if notes else "text", "error": err}
    return text, summary
