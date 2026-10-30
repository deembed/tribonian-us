"""citemarks.py -- a light tint behind every cite in a PDF, by whether it could be checked, and the closing
"Cites not resolved" part that lists the ones that could not (scope: claude/scope-cite-marks.md).

    green   resolved: the case is in CourtListener (a link, or found in the local database), or the law's text is local
    red     likely a miscite or misread (no such case or provision found), or not fetched yet
    orange  no source: it exists but nothing here can read it (a LexisNexis-only code, a blocked site, a Westlaw-only
            decision CourtListener does not have)

The yellow marks for old US Code cites (case2md.usc_out) stay as they are and take precedence. Only PDFs are
marked; the notes are unchanged. CITE_MARKS=0 turns it off. One build at a time per process: reset() at the start
of a document, section_tex() at its end.
"""
import os
import re

CITE_MARKS = os.environ.get("CITE_MARKS", "1") != "0"
MACRO = {"ok": "\\citeok", "red": "\\citebad", "orange": "\\citensrc"}
# A tint is ulem's \markoverwith, set word by word: anything that writes to the .aux, sets a target or a footnote
# mark, or is itself a ulem mark, stays unmarked rather than risk the build.
UNSAFE = ("\\recordpath", "\\hypertarget", "\\textsuperscript", "\\usc", "\\leaders", "\\footnote", "\\label",
          "\\cite", "\\par", "\\pdfbookmark")

_REG = []                # (status, shown text, reason, label) for every red and orange mark in this document
_LAW = {}                # law key -> (status, reason)


def reset():
    del _REG[:]


def law_status(key):
    """('ok' | 'red' | 'orange', reason) for a lawcites key."""
    if key not in _LAW:
        from tribonian import lawtext
        if lawtext.text_of(key) is not None:
            _LAW[key] = ("ok", "")
        else:
            hint = lawtext.fetch_hint(key) or "no text found"
            if hint.startswith("source error"):
                _LAW[key] = ("orange", hint[len("source error: "):] if hint.startswith("source error: ") else hint)
            elif hint.startswith("tribonian ") or hint.startswith("not fetched"):
                _LAW[key] = ("red", "not fetched (%s)" % hint)
            else:
                _LAW[key] = ("red", hint)
    return _LAW[key]


def mark(status, inner, text="", reason=""):
    """inner (TeX) tinted for status; red and orange are also labelled and listed at the end."""
    if not CITE_MARKS or not inner.strip() or any(u in inner for u in UNSAFE):
        return inner
    out = "%s{%s}" % (MACRO[status], inner)
    if status != "ok":
        lab = "citemark-%d" % len(_REG)
        _REG.append((status, " ".join((text or "").split()), reason, lab))
        out += "\\label{%s}" % lab
    return out


def section_tex(esc):
    """The closing part: every red and orange cite once, with its reason and the pages it is on. '' when none."""
    if not CITE_MARKS or not _REG:
        return ""
    groups = (("red", "Likely miscites, or not fetched yet",
               "No case or provision was found for these, or its text has not been fetched. Check the cite in the source."),
              ("orange", "No source",
               "These exist, but nothing here can read their text: law with no usable source (see DATASOURCES.md), "
               "or Westlaw-only decisions CourtListener does not have."))
    out = ["\\clearpage\\recordcover{none}\\recordstart{Cites not resolved}\\noindent\\tocback"
           "\\tocadd{tocstage}{Cites not resolved}\\pdfbookmark[0]{Cites not resolved}{citesnotresolved}"
           "{\\Large\\bfseries Cites not resolved\\par}\\vspace{4pt}"
           "{\\scriptsize\\itshape Every cite tinted red or orange in this document, once each, with the pages it is on. "
           "Green: checked; red: not found or not fetched; orange: no source; yellow: an old US Code cite.\\par}\\vspace{6pt}"]
    for status, head, note in groups:
        seen = {}
        for st, text, reason, lab in _REG:
            if st == status:
                seen.setdefault((text, reason), []).append(lab)
        if not seen:
            continue
        out.append("{\\bfseries %s} {\\footnotesize(%d)}\\par{\\scriptsize\\itshape %s\\par}\\vspace{2pt}"
                   % (head, len(seen), note))
        out.append("\\begin{list}{}{\\leftmargin=1.2em\\itemindent=-1.2em\\itemsep=1pt\\parsep=0pt\\topsep=0pt}")
        for (text, reason), labs in sorted(seen.items(), key=lambda kv: kv[0][0].lower()):
            pages = ", ".join("\\pageref{%s}" % l for l in labs[:12]) + (", \\dots" if len(labs) > 12 else "")
            out.append("\\item {\\small %s%s}\\hfill{\\footnotesize p.\\,%s}"
                       % (MACRO[status] + "{" + esc(text) + "}", " --- {\\footnotesize\\itshape %s}" % esc(reason) if reason else "",
                          pages))
        out.append("\\end{list}\\vspace{6pt}")
    return "\n".join(out) + "\n"


def section_md(esc):
    """The note's '## Cites not resolved': the PDF's red and orange cites, once each with their reason and count.
    Call after the PDF's render (the marks are registered as it renders). [] when none."""
    if not CITE_MARKS or not _REG:
        return []
    L = ["## Cites not resolved", "",
         "*Every cite the PDF tints red or orange, once each, with its reason (×n: how many times).*", ""]
    for status, head in (("red", "Likely miscites, or not fetched yet"), ("orange", "No source")):
        seen = {}
        for st, text, reason, _ in _REG:
            if st == status:
                seen[(text, reason)] = seen.get((text, reason), 0) + 1
        if seen:
            L += ["### %s (%d)" % (head, len(seen)), ""]
            L += ["- %s%s%s" % (esc(t), " — *%s*" % esc(r) if r else "", " ×%d" % n if n > 1 else "")
                  for (t, r), n in sorted(seen.items(), key=lambda kv: kv[0][0].lower())]
            L.append("")
    return L
