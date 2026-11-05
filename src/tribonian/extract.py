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

VERSION = 7                     # 2: a mark is matched to its note anywhere later in the brief (endnotes)
                                # 3: labels in order, "6. Text" labels, a wrapped "12, 2024)." is not a label
                                # 4: numbering that starts again at 1 (an appendix's own opinions); "5.56mm" is not a label
                                # 5: a page whose text is drawn sideways is turned upright first (a landscape table)
                                # 6: a ruled table is read cell by cell into a table block
                                # 7: a table's line pdftotext set outside its box goes with it; "1a" is not note 1
NOTES_START = ""
NOTE_LABEL = ""
TABLE_START, TABLE_END = "\ue020", "\ue022"   # a table block, in place of the table's lines: one TABLE_ROW line
TABLE_ROW, TABLE_CELL = "\ue023", "\ue021"    # per row, its cells split by TABLE_CELL
TABLE_CHARS = TABLE_START + TABLE_END + TABLE_ROW
MARK_DIGITS = "".join(chr(0xE030 + d) for d in range(10))   # a footnote mark's digits: same width as 0-9
TO_MARK = str.maketrans("0123456789", MARK_DIGITS)
FROM_MARK = str.maketrans(MARK_DIGITS, "0123456789")
DROP_MARK = {ord(ch): None for ch in MARK_DIGITS + TABLE_CHARS}
DROP_MARK[ord(TABLE_CELL)] = " "                 # a table's cells read as text, a space between them
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

def turn_for(ch):
    """A page's characters -> the clockwise turn (90, 180, 270) that sets its text upright, or 0. Most of the page
    has to be sideways or upside down: a landscape table on a portrait page (Cruz et al.'s appendix), not a
    rotated label in a margin."""
    if len(ch) < 50 or sum(1 for c in ch if c.get("upright")) >= 0.5 * len(ch):
        return 0
    sign = lambda v: (v > 0.01) - (v < -0.01)   # the text matrix's direction (its scale is the font size)
    a, b = Counter((sign(c["matrix"][0]), sign(c["matrix"][1])) for c in ch).most_common(1)[0][0]
    return {(0, 1): 90, (0, -1): 270, (-1, 0): 180}.get((a, b), 0)


def turned_copy(pdf, turns, folder):
    """A copy of the PDF with these pages turned ({page index: degrees clockwise}); the original is not touched."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf))
    try:
        for pi, deg in turns.items():
            doc[pi].set_rotation((doc[pi].get_rotation() + deg) % 360)
        out = Path(folder) / pdf.name
        doc.save(str(out))
    finally:
        doc.close()
    return out


def cell_text(cs):
    """A table cell's characters [(char dict, raised mark?)] -> its text, lines joined, marks in look-alike digits."""
    if cs and sum(1 for c, _ in cs if not c.get("upright", True)) > len(cs) / 2:
        # a header set vertically: read along the text (up the page when it is turned left)
        up = Counter((c["matrix"][1] > 0) for c, _ in cs).most_common(1)[0][0]
        cols = []                                # its lines are columns: [x, [chars]]
        for c, _ in sorted(cs, key=lambda cr: cr[0]["x0"]):
            if cols and abs(cols[-1][0] - c["x0"]) < 2:
                cols[-1][1].append(c)
            else:
                cols.append([c["x0"], [c]])
        parts = []
        for _, col in (cols if up else cols[::-1]):          # turned left: the first line is the leftmost
            col.sort(key=lambda c: -c["bottom"] if up else c["top"])
            t, prev = "", None
            for c in col:
                gap = 0 if prev is None else (prev["top"] - c["bottom"]) if up else (c["top"] - prev["bottom"])
                if gap > 0.2 * ((prev or c)["size"] or 10):
                    t += " "
                t += c["text"]
                prev = c
            parts.append(t)
        return " ".join(" ".join(parts).split())
    # a mark, by the cell's own geometry (the page's content order can put another cell's text before it): digits
    # smaller than the cell's text and raised above the baseline of the text beside them
    if cs:
        sizes = sorted(c["size"] for c, _ in cs)
        text_size = sizes[len(sizes) // 2]
        full = [c for c, _ in cs if c["size"] >= text_size * 0.95]
        def raised(c):
            if not (c["text"].isdigit() and c["size"] < text_size * 0.95):
                return False
            beside = [f for f in full if f["bottom"] - 8 <= c["bottom"] <= f["bottom"] + 1 and abs(f["x1"] - c["x0"]) < 20]
            return any(c["bottom"] < f["bottom"] - RAISE for f in beside)
        cs = [(c, r or raised(c)) for c, r in cs]
    lines = []                                   # [bottom, [(char, raised)]]
    for c, r in sorted(cs, key=lambda cr: (cr[1], cr[0]["bottom"])):   # the text's lines first, then the marks
        if r:                                    # a mark goes on the line just below its baseline
            near = [ln for ln in lines if 0 <= ln[0] - c["bottom"] <= 8]
            if near:
                min(near, key=lambda ln: ln[0] - c["bottom"])[1].append((c, r))
                continue
        hit = next((ln for ln in lines if abs(ln[0] - c["bottom"]) < 2), None)
        if hit:
            hit[1].append((c, r))
        else:
            lines.append([c["bottom"], [(c, r)]])
    out = []
    for _, ln in sorted(lines, key=lambda ln: ln[0]):
        ln.sort(key=lambda cr: cr[0]["x0"])
        t, prev = "", None
        for c, r in ln:
            if prev is not None and c["x0"] - prev["x1"] > 0.2 * (prev["size"] or 10) and not r:
                t += " "
            t += c["text"].translate(TO_MARK) if r else c["text"]
            prev = c
        out.append(t)
    text = ""
    for l in out:                                # "consti-" + "tution": the word whole again
        text = text[:-1] + l if text.endswith("-") and l[:1].islower() else (text + " " + l if text else l)
    return text.strip()


def page_tables(pg, ch, out):
    """A page's ruled tables (pdfplumber finds them by their lines) -> [{top, bottom, rows: [[cell text]]}], each
    cell read from the characters inside its own box. Charts and figures that only look ruled (cells of a letter
    or two) are left out."""
    try:
        found = pg.find_tables()
    except Exception:
        return []
    res = []
    for t in found:
        rows = []
        for r in t.rows:
            cells = []
            for box in r.cells:
                if box is None:                  # covered by a cell spanning it
                    cells.append("")
                    continue
                x0, top, x1, bottom = box
                cells.append(cell_text([(c, o[2]) for c, o in zip(ch, out) if c["x0"] >= x0 - 1 and c["x1"] <= x1 + 1
                                        and c["top"] >= top - 1 and c["bottom"] <= bottom + 1]))
            if any(cells):
                rows.append(cells)
        if not rows:
            continue
        n = max(len(r) for r in rows)
        rows = [r + [""] * (n - len(r)) for r in rows]
        keep = [i for i in range(n) if any(r[i] for r in rows)]          # a spacer column, empty in every row: out
        rows = [[r[i] for i in keep] for r in rows]
        filled = [x for r in rows for x in r if x]
        # a table: 2+ rows, 2+ columns, half its rows with 2+ cells filled (ruled lines around text are not one),
        # and cells of more than a letter or two (a chart's axis labels are not one)
        if len(rows) < 2 or len(keep) < 2 or sum(1 for r in rows if sum(1 for x in r if x) >= 2) < len(rows) / 2 \
                or sum(len(x) for x in filled) / len(filled) < 3:
            continue
        res.append({"top": t.bbox[1], "bottom": t.bbox[3], "rows": rows})
    return res


def plumber_pages(pdf):
    """-> (body size, [page: [(char, size, raised mark?, top)]], {page index: turn}, [page: tables]) in content
    order, whitespace left out. A glyph pdfplumber cannot map ("(cid:49)") is kept as a character that matches nothing. The turns: pages
    whose text is drawn sideways (turn_for); their characters are read again once the pages are turned."""
    import logging
    import pdfplumber
    # pdfminer warns about each malformed font map ("Ignoring (part of) ToUnicode map"); the characters it cannot
    # map come out as (cid:N), the page's two reads disagree, and it keeps pdftotext's text: nothing to report
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    pages, sizes, turns, tables = [], Counter(), {}, []
    with pdfplumber.open(str(pdf)) as doc:
        for pi, pg in enumerate(doc.pages):
            ch = [c for c in pg.chars if c["text"].strip()]
            if turn_for(ch):
                turns[pi] = turn_for(ch)
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
                out.append((t, c["size"], raised, c["top"]))
                last = c
            try:
                tables.append(page_tables(pg, ch, out))
            except Exception as e:               # a table that can't be read costs that page's tables, not the brief
                tables.append([])
                trace.hit("table", "error:" + type(e).__name__, pi, repr(e)[:100])
            pg.flush_cache()
            pages.append(out)
    body = sizes.most_common(1)[0][0] if sizes else 0
    return body, pages, turns, tables


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
    got = set()
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            attr[pos[i + k]] = chars[j + k][1:]
            got.add(j + k)
    # a mark the alignment left out: the two reads take a table's cells in a different order (pdftotext by rows,
    # pdfplumber cell by cell), so short cells ("GA4") fall between the long matches. Find the mark with the few
    # characters before it ("GA" + "4") in pdftotext's text, the next place after the last one found.
    j, start = 0, 0
    while j < len(b):
        if not (chars[j][2] and j not in got):
            j += 1
            continue
        end = j
        while end < len(b) and chars[end][2]:
            end += 1
        pre = j
        while pre > 0 and j - pre < 6 and not chars[pre - 1][2]:
            pre -= 1
        at = -1                                  # the longest context that is found, down to 2 characters
        for pre in range(pre, j - 1):            # ("1799CT19": the year is a row above in pdftotext; "CT19")
            needle = b[pre:end]
            at = a.find(needle, start)
            if at < 0:
                at = a.find(needle)
            if at >= 0:
                break
        if at >= 0:
            for k in range(end - j):
                attr[pos[at + (j - pre) + k]] = chars[j + k][1:]
            start = at + len(needle)
            trace.hit("align", "mark:found-by-context", None, needle, keep=False)
        j = end
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


@LABEL.rule("not-label:appendix-page-number")    # "1a", "12a": an appendix page's number, not note 1
def _(c):
    return bool(re.fullmatch(r"[a-z]", c.rest))


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
    ys = []                                      # each line's place on the page (its characters' middle top)
    for k, l in enumerate(lines):
        tops = sorted(attr[starts[k] + i][2] for i, ch in enumerate(l) if not ch.isspace() and starts[k] + i in attr)
        ys.append(tops[len(tops) // 2] if tops else None)
    none = lambda: (lines, [], page_marks(lines, starts, attr, len(lines)), [], nxt, False, ys)

    def kind(k):
        """'small' (a footnote's type), 'body', or None (too few characters read)."""
        l = lines[k]
        got = [attr[starts[k] + i] for i, ch in enumerate(l) if not ch.isspace() and starts[k] + i in attr]
        got = [g for g in got if not g[1]]      # marks aside
        if len(got) < 3:
            return None
        small = sum(g[0] < body * SMALL for g in got)
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
        if RULE_RE.match(l) or re.fullmatch(r"\s*\(?\d{1,4}a\)?\s*", l):
            continue                             # a rule; an appendix page's number ("1a") among the notes
        l = re.sub(r"\s{3,}\(?\d{1,4}a\)?\s*$", "", l)     # "... yht8wnme.          2a": the page number at its end
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
    return body_lines, block, marks, [n[0] for n in labelled], nxt, True, ys[:top] + ys[end:]


# ---------------------------------------------------------------- tables (detection only, for now)

LEADER_WORD = re.compile(r"(?:\.\s?){4,}|…{2,}|·{4,}")    # dot leaders, in a word of their own or run into the text


def find_tables(pg, body):
    """One pdfplumber page -> its table-like regions: [{kind, top, bottom, rows, cols, cell, sample}].
    A line is split where the space between two words is far wider than a justified line's (columns); a table is
    3+ such lines, consecutive or with a wrapped cell's line between them, whose splits line up. kind:
      leaders      a table of contents or authorities (dot leaders): the build already sets these
      ruled        drawn lines around the cells (pdfplumber's own table finder agrees)
      short-cells  cells of a few words: a list of names, a signature block
      aligned      columns of text with no lines
    Not used by extract() yet: tribonian's tools/table_survey.py counts what it finds across the filings."""
    words = pg.extract_words(x_tolerance=1.5, y_tolerance=2, extra_attrs=["size"])
    if not words:
        return []
    gap_min = max(12.0, 1.3 * (body or 10))
    lines = []                                   # [(top, [(x0, x1, text)])]
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(w["top"] - lines[-1][0]) < 3:
            lines[-1][1].append(w)
        else:
            lines.append([w["top"], [w]])
    rows = []                                    # per line: (top, bottom, [segments (x0, text)], leader?)
    for top, ws in lines:
        ws.sort(key=lambda w: w["x0"])
        leader = any(LEADER_WORD.search(w["text"]) for w in ws)
        if leader and len(ws) > 1:               # an entry and its page: two cells, however close the dots run
            segs = [ws[:-1], ws[-1:]]
        else:
            segs, cur = [], [ws[0]]
            for a, b in zip(ws, ws[1:]):
                if b["x0"] - a["x1"] >= gap_min:
                    segs.append(cur)
                    cur = []
                cur.append(b)
            segs.append(cur)
        rows.append((top, max(w["bottom"] for w in ws), [(s[0]["x0"], " ".join(w["text"] for w in s)) for s in segs], leader))
    out, i = [], 0
    while i < len(rows):
        if len(rows[i][2]) < 2:
            i += 1
            continue
        j, last, starts = i, i, []
        while j < len(rows):                     # the run: split lines, and a wrapped cell's line between them
            segs = rows[j][2]
            if len(segs) >= 2:
                starts += [round(x) for x, _ in segs[1:]]
                last = j
            elif j - last > 1 or not any(abs(segs[0][0] - x) <= 6 for x in starts):
                break
            j += 1
        split = [r for r in rows[i:last + 1] if len(r[2]) >= 2]
        if len(split) >= 3:
            cols = sorted(set(starts))
            kept, run = [], []
            for x in cols:                       # column starts that 3+ lines share (within 6pt)
                if run and x - run[-1] > 6:
                    if sum(starts.count(y) for y in run) >= max(3, len(split) // 2):
                        kept.append(run[0])
                    run = []
                run.append(x)
            if run and sum(starts.count(y) for y in run) >= max(3, len(split) // 2):
                kept.append(run[0])
            if kept:
                cells = [t for r in split for _, t in r[2]]
                cell = sum(len(t.split()) for t in cells) / len(cells)
                top, bottom = split[0][0], split[-1][1]
                if sum(r[3] for r in split) >= len(split) / 2:
                    kind = "leaders"
                elif any(t.bbox[1] <= bottom and t.bbox[3] >= top for t in pg.find_tables()):
                    kind = "ruled"
                elif cell <= 4:
                    kind = "short-cells"
                else:
                    kind = "aligned"
                out.append({"kind": kind, "top": round(top), "bottom": round(bottom), "rows": len(split),
                            "cols": len(kept) + 1, "cell": round(cell, 1),
                            "sample": " / ".join(" | ".join(t for _, t in r[2]) for r in split[:2])[:160]})
        i = last + 1
    return out


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
    import tempfile
    texts = pdftotext_pages(pdf)
    turned = []
    try:
        body, chars, turns, tables = plumber_pages(pdf)
        if turns:                                # sideways text: read both again from a copy with those pages turned
            with tempfile.TemporaryDirectory() as tmp:
                copy = turned_copy(pdf, turns, tmp)
                texts = pdftotext_pages(copy)
                body, chars, _, tables = plumber_pages(copy)
            turned = sorted(turns)
            for pi in turned:
                trace.hit("page", "turned:sideways-text", pi, "%d degrees" % turns[pi])
    except Exception as e:                       # pdfplumber cannot read it: pdftotext's text as it is
        body, chars, tables, err = 0, [], [], repr(e)[:200]
    else:
        err = None
    pages, notes, nxt, prev, unread = [], [], 1, False, 0      # pages: [body lines, block, marks]
    for pi, t in enumerate(texts):
        if pi >= len(chars) or not t.strip():
            pages.append([t.split("\n"), [], [], []])
            prev = False
            continue
        attr, share = align(t, chars[pi])
        if share < rules.PARAMS.agree:           # the two reads disagree (an unmapped font): leave the page be
            trace.hit("page", "unread:reads-disagree", pi, t.strip()[:80])
            unread += 1
            pages.append([t.split("\n"), [], [], []])
            prev = False
            continue
        lines, block, marks, labels, nxt2, ok, ys = split_page(t, attr, body, nxt, prev)
        if ok:
            nxt = nxt2
            notes += [(lab, pi) for lab in labels]
        pages.append([lines, block, marks, ys])
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
    # each ruled table in place of its lines (pdftotext runs a table's rows and cells together): a table block, its
    # marks kept where a note claims their number and set back as digits where none does
    labels = {int(lab) for lab, _ in notes}
    ntables = 0
    for pi, pg in enumerate(pages):
        lines, _, _, ys = pg
        for t in sorted(tables[pi] if pi < len(tables) else [], key=lambda t: -t["top"]):
            idx = [k for k, y in enumerate(ys) if y is not None and t["top"] - 2 <= y <= t["bottom"] + 2]
            if not idx:
                continue
            a, b = min(idx), max(idx)
            # a line of the table pdftotext set just outside its box ("NY 3" under the row it belongs to): its text is
            # already in a cell, so it goes with the table
            squash = lambda v: re.sub(r"\W", "", v.translate(FROM_MARK)).lower()
            whole = squash(" ".join(x for r in t["rows"] for x in r))
            for step in (1, -1):
                k, n = (b if step > 0 else a) + step, 0
                while 0 <= k < len(lines) and n < 3:
                    v = squash(lines[k])
                    if not v:
                        k += step
                        continue
                    if len(v) < 2 or v not in whole:
                        break
                    a, b = (a, k) if step > 0 else (k, b)
                    k, n = k + step, n + 1
            rows = [TABLE_ROW + TABLE_CELL.join(MARK_RE.sub(lambda m: m.group() if int(m.group().translate(FROM_MARK)) in labels
                                                            else m.group().translate(FROM_MARK), c) for c in r)
                    for r in t["rows"]]
            lines[a:b + 1] = ["", TABLE_START] + rows + [TABLE_END, ""]
            ys[a:b + 1] = [None] * (len(rows) + 4)
            ntables += 1
            trace.hit("table", "table:ruled", pi, " | ".join(t["rows"][0])[:100])
    text = "\f".join("\n".join(lines + block) for lines, block, _, _ in pages)
    summary = {"version": VERSION, "pdf": pdf.name, "pdf_bytes": pdf.stat().st_size, "pdf_mtime": int(pdf.stat().st_mtime),
               "pages": len(texts), "body_size": body, "notes": len(notes), "marked": marked,
               "unread_pages": unread, "turned_pages": [pi + 1 for pi in turned], "tables": ntables,
               "how": "superscript" if notes else "text", "error": err}
    return text, summary
