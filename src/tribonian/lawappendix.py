"""lawappendix.py -- the law a document cites, printed at its end: every cited provision once, with its text where it
is stored locally (lawtext.py), each cite in the document linking to its entry. Used by case2md.py (court opinions,
"The Law Cited") and scotus.py (a docket's filings: the law in question first, then the rest by side).

Fetching is per kind of law: federal (C.F.R. titles, the U.S. Constitution) and state (any state's code item, via its
formula: `--fetch-state-law`). A fetch never fails the build; what stays missing is listed at the end of the run.
A provision whose code has no source (Purdon's, a LexisNexis-only state) prints "source error" and the reason.
"""
import re
from collections import OrderedDict

from tribonian import case2md as c, lawcites


def anchor(key):
    return "law-" + re.sub(r"[^A-Za-z0-9]+", "-", lawcites.section_of(key)).strip("-")


def keys_in(obj, out=None):
    """Every lawcites key in rendered material: <law key=...> elements anywhere in nested lists, dicts and El trees."""
    out = [] if out is None else out
    if isinstance(obj, c.El):
        if obj.tag == "law" and obj.attrs.get("key"):
            out.append(obj.attrs["key"])
        for k in obj.kids:
            keys_in(k, out)
    elif isinstance(obj, (list, tuple)):
        for k in obj:
            keys_in(k, out)
    elif isinstance(obj, dict):
        for k in obj.values():
            keys_in(k, out)
    return out


def is_state(key):
    return "-" in key.split(":", 1)[0]


def fetch_missing(keys, federal=False, state=False, warnings=None):
    """Fetch what is not local: federal kinds with `federal`, state codes with `state`. -> [(key, hint)] still
    missing (a state code with no source gives its source error as the hint)."""
    from tribonian import lawtext
    warnings = [] if warnings is None else warnings
    keys = list(dict.fromkeys(keys))
    todo = [k for k in keys if lawtext.text_of(k) is None]
    specs = []
    for k in todo:
        spec = lawtext.fetch_spec(k)
        if spec is None:
            continue
        if (lawtext.is_state_spec(spec) and state) or (not lawtext.is_state_spec(spec) and federal):
            specs.append(spec)
    for spec in dict.fromkeys(specs):
        print("fetch: %s" % " ".join(x for x in (spec[1:] if spec[0] == "state" else spec) if x), flush=True)
        ok, why = lawtext.run_fetch(spec)
        if not ok:
            warnings.append("could not fetch %s: %s" % (" ".join(x for x in spec[1:] if x), why))
    return [(k, lawtext.fetch_hint(k)) for k in todo if lawtext.text_of(k) is None]


def grouped(keys):
    """section_of(key) -> the pinned keys under it (a constitution's sections, a rule's sections), in sort order."""
    g = OrderedDict()
    for k in sorted(dict.fromkeys(keys), key=lawcites.sort_key):
        p = lawcites.section_of(k)
        g.setdefault(p, [])
        if k != p:
            g[p].append(k)
    return g


def _text_tex(got):
    head, rows = got
    out = ["{\\small\\bfseries %s\\par}" % head] if head else []
    for depth, label, heading, text in rows:
        out.append("%s{\\small\\leftskip=%.1fem %s%s%s\\par}" % (
            "\\headroom{4}" if label and heading and not text else "", 1.2 * depth,
            "\\textbf{%s} " % label if label else "", "\\emph{%s.} " % heading if heading else "", text))
    return out


def _missing_tex(key):
    from tribonian import lawtext
    E = lambda t: c.esc(t, "tex")
    hint = lawtext.fetch_hint(key)
    if hint.startswith("source error"):
        return ["{\\small\\itshape Source error: %s\\par}" % E(hint[len("source error: "):])]
    if hint.startswith("tribonian "):
        return ["{\\small\\itshape Text not stored locally (\\texttt{%s} gets it).\\par}" % E(hint)]
    return ["{\\small\\itshape Text not stored locally%s.\\par}" % (" (%s)" % E(hint) if hint else "")]


def entries_tex(keys, note=None, editions=None, level=1):
    """TeX for each provision (grouped at section level): a heading with the anchor every cite links to, an optional
    note line (note(key) -> TeX), the edition the document cites if it says ("West 2019"), then the text."""
    from tribonian import lawtext
    E = lambda t: c.esc(t, "tex")
    editions = editions or {}
    out = []
    for parent, pins in grouped(keys).items():
        lab = lawcites.label(parent)
        a = anchor(parent)
        out.append("\\headroom{6}\\vspace{6pt}\\hypertarget{%s}{}\\pdfbookmark[%d]{%s}{%s}\\noindent{\\large\\bfseries %s\\par}\\vspace{2pt}"
                   % (a, level, E(lab), a, E(lab)))
        if note:
            n = note(parent)
            if n:
                out.append("{\\footnotesize %s\\par}\\vspace{2pt}" % n)
        eds = sorted({"%s %s" % editions[k] for k in [parent] + pins if k in editions})
        if eds:
            out.append("{\\footnotesize Cited as the %s edition; the text below is the current one.\\par}\\vspace{2pt}"
                       % E(", ".join(x.strip() for x in eds)))
        if pins:                                  # a constitution's or a rule's cited sections, each in full
            for k in pins:
                out.append("\\headroom{4}\\vspace{3pt}\\noindent{\\bfseries %s\\par}" % E(lawcites.label(k)))
                got = lawtext.text_of(k)
                out += _text_tex(got) if got else _missing_tex(k)
            continue
        got = lawtext.text_of(parent)
        out += _text_tex(got) if got else _missing_tex(parent)
    return out


def appendix_tex(keys, title="The Law Cited", intro="", note=None, editions=None):
    """The whole closing part: a contents entry, the intro line, then entries_tex."""
    if not keys:
        return ""
    out = ["\\clearpage\\recordcover{none}\\recordpath{%s}\\noindent\\tocback\\tocadd{tocstage}{%s}\\pdfbookmark[0]{%s}{lawcited}"
           "{\\Large\\bfseries %s\\par}\\vspace{4pt}" % (title, title, title, title)]
    if intro:
        out.append("{\\scriptsize\\itshape %s\\par}\\vspace{6pt}" % intro)
    out += entries_tex(keys, note, editions)
    return "\n".join(out) + "\n"


def md_lines(keys, statutes, title="The law cited", hints=None):
    """The note's list: each provision once, linked to its note where one exists; a source error or 'not local'
    after those without text."""
    from tribonian import lawtext
    if not keys:
        return []
    E = lambda t: c.esc(t, "md")
    L = ["## %s" % title, ""]
    for parent, pins in grouped(keys).items():
        note = lawcites.note_name(parent)
        lab = E(lawcites.label(parent))
        item = "[[%s|%s]]" % (note, lab) if note in statutes else lab
        if pins:
            item += " (" + ", ".join(E(lawcites.label(k)) for k in pins) + ")"
        hint = lawtext.fetch_hint(parent) if lawtext.text_of(pins[0] if pins else parent) is None else ""
        if hint.startswith("source error"):
            item += " — *%s*" % E(hint)
        L.append("- " + item)
    return L + [""]
