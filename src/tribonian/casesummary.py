"""casesummary.py -- The Cases Cited: every case a document cites, once, with a short sketch of what it was about,
at the end of the PDF (--cases on case, recap and scotus builds; the GUI's "Summarize the cases cited").

The local database has no questions presented for most cases, so the sketch is, in order of preference:
    1. CourtListener's reporter summary or syllabus, cut to its first sentences (editorial, not the opinion), else
    2. the parentheticals later courts most often wrote about it ("holding that ..."), largest group first, else
    3. nothing but the name, cite and court.
Other people's words either way, and the section says so.
"""
import html
import re

SKETCH_CHARS = 420           # the summary or syllabus: whole sentences up to about this long
PAREN_GROUPS = 3             # parenthetical groups shown
PAREN_CHARS = 260


def _text(n, hrefs):
    if isinstance(n, str):
        return n
    if n.tag in ("page-number", "footnotemark"):     # so a cite's parenthetical runs on past a page break
        return ""
    m = re.match(r"/opinion/(\d+)/", n.attrs.get("href", "")) if n.tag == "a" else None
    if m:
        hrefs.append(int(m.group(1)))
    return "".join(_text(k, hrefs) for k in n.kids)


def cited(con, ops):
    """Cluster ids the documents cite, in order of first appearance: reporter cites in the text (found by citation,
    or by name, court and date) and CourtListener's links in HTML opinions."""
    from tribonian import scotus
    if con is None:
        return []
    hrefs, parts = [], []
    for o in ops:
        parts += [_text(b[1], hrefs) for b in o.get("flat", [])]
        parts += [_text(fn, hrefs) for fn in (o.get("footnotes") or {}).values() if not isinstance(fn, list)]
    text = "\n".join(parts)
    got, _ = scotus.authorities(con, text)
    out = list(got)
    t = " ".join(text.split())                         # cites CourtListener has only as RECAP dockets, by docket number
    for m in scotus.CITE_RE.finditer(t):
        if not m.group(3) and scotus.canon(m.group(2)) == "WL":
            d = scotus.resolve_docket(con, t[max(0, m.start() - 400):m.start()], t[m.end():m.end() + 160])
            if d:
                out.append(("docket", d[0]))
                scotus.FOUND[(m.group(1), "WL", m.group(4))] = ("docket", d[0])
    if hrefs:
        try:
            q = ",".join("?" * len(set(hrefs)))
            byop = dict(con.execute("SELECT id, cluster_id FROM opmap WHERE id IN (%s)" % q, list(set(hrefs))))
            out += [byop[h] for h in hrefs if h in byop]
        except Exception:
            pass
    own = {o.get("api", {}).get("cluster_id") for o in ops} | {o.get("cluster") for o in ops}
    return [k for k in dict.fromkeys(out) if k and k not in own]


def _sentences(raw, limit):
    t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).split())
    if not t:
        return ""
    out = ""
    for s in re.split(r"(?<=[.?!])\s+(?=[A-Z(])", t):
        if out and len(out) + len(s) > limit:
            break
        out = (out + " " + s).strip()
    return out if len(out) <= limit + 80 else out[:limit].rsplit(" ", 1)[0] + " …"


def sketch(con, cid):
    """-> ('summary' | 'syllabus' | 'parens' | '', [lines])"""
    try:
        r = con.execute("SELECT summary, syllabus FROM clusters WHERE id=?", (cid,)).fetchone()
    except Exception:
        r = None
    for field, raw in zip(("summary", "syllabus"), r or ()):
        s = _sentences(raw, SKETCH_CHARS)
        if s:
            return field, [s]
    groups = parens(con, cid)
    if groups:
        return "parens", ["(%s)%s" % (t[:PAREN_CHARS] + ("…" if len(t) > PAREN_CHARS else ""), " \u00d7%d" % n if n > 1 else "")
                          for n, t in groups]
    return "", []


def parens(con, cid):
    """[(group size, best-scored wording)] of the parentheticals about the case, largest group first (main database
    only: opmap, not the opinions database)."""
    try:
        ops = [o for (o,) in con.execute("SELECT id FROM opmap WHERE cluster_id = ?", (cid,))]
        if not ops:
            return []
        q = ",".join("?" * len(ops))
        out = []
        for gid, n in con.execute("SELECT group_id, count(*) AS n FROM parens WHERE described IN (%s) AND group_id IS NOT NULL "
                                  "GROUP BY group_id ORDER BY n DESC LIMIT ?" % q, ops + [PAREN_GROUPS]).fetchall():
            (text,) = con.execute("SELECT text FROM parens WHERE described IN (%s) AND group_id = ? ORDER BY score DESC LIMIT 1"
                                  % q, ops + [gid]).fetchone()
            out.append((n, " ".join((text or "").split())))
        return out
    except Exception:
        return []


LABEL = {"summary": "Reporter's summary", "syllabus": "Syllabus", "parens": "How later courts describe it"}


def section_tex(con, cids, doc="document"):
    """The closing part, or '' when nothing is cited."""
    from tribonian import case2md as c, scotus
    if con is None or not cids:
        return ""
    E = lambda t: c.esc(t, "tex")
    names = scotus.case_names(con, [k for k in cids if not isinstance(k, tuple)])
    out = ["\\clearpage\\recordcover{none}\\recordstart{The Cases Cited}\\noindent\\tocback\\tocadd{tocstage}{The Cases Cited}"
           "\\pdfbookmark[0]{The Cases Cited}{casescited}{\\Large\\bfseries The Cases Cited\\par}\\vspace{4pt}",
           "{\\scriptsize\\itshape Not part of the %s. Each case cited, once, in order of first citation, with a sketch "
           "of what the case was about: CourtListener's reporter summary or syllabus where it has one, otherwise the "
           "parentheticals later courts most often wrote about it (\u00d7n: how many wrote much the same). Other "
           "people's words, not the case's: read the opinion before relying on them.\\par}\\vspace{6pt}" % E(doc)]
    for cid in cids:
        if isinstance(cid, tuple):                     # ("docket", id): a RECAP docket, no opinion in the data
            out.append(docket_tex(con, cid[1], E))
            continue
        n = names.get(cid) or {}
        head = "{\\small\\bfseries %s}" % E(n.get("name") or "cluster %s" % cid)
        tail = ", ".join(x for x in (n.get("cite"), " ".join(x for x in (n.get("court"), n.get("year")) if x)) if x)
        kind, lines = sketch(con, cid)
        body = ("{\\footnotesize\\itshape %s:} " % LABEL[kind] if kind else "") + \
            ("{\\footnotesize %s}" % "\\par\\noindent ".join(E(l) for l in lines)
             if lines else "{\\footnotesize\\itshape No summary, syllabus or parentheticals in the data.}")
        out.append("\\noindent%s{\\small%s}\\par\\nopagebreak{\\leftskip=1.2em\\noindent %s\\par}\\vspace{4pt}"
                   % (head, (", " + E(tail)) if tail else "", body))
    return "\n".join(out) + "\n"


def docket_tex(con, did, E):
    """One RECAP docket's entry: its name, number, court and year, and what the docket says the suit was about."""
    r = con.execute("SELECT d.case_name, d.docket_number, ct.citation_string, d.date_filed, d.nature_of_suit, d.cause "
                    "FROM dockets d LEFT JOIN courts ct ON ct.id = d.court_id WHERE d.id=?", (did,)).fetchone()
    if not r:
        return ""
    name, number, court, filed, nos, cause = r
    about = "; ".join(" ".join(x.split()) for x in (nos, cause) if x)
    return ("\\noindent{\\small\\bfseries %s}{\\small, No. %s (%s)}\\par\\nopagebreak{\\leftskip=1.2em\\noindent "
            "{\\footnotesize\\itshape RECAP docket %s, no opinion text in the data%s}\\par}\\vspace{4pt}"
            % (E(name or "docket %s" % did), E(number or ""), E(" ".join(x for x in (court, (filed or "")[:4]) if x)),
               did, (":} {\\footnotesize " + E(about)) if about else ""))


def sketch_md(con, cid, esc):
    """The note's version of a case's sketch: indented lines under its bullet in '## Cites' (plain text, so the
    entry's [[link]] stays the only link: Obsidian's graph and backlinks are unchanged)."""
    if con is None:
        return []
    kind, lines = sketch(con, cid)
    if not kind:
        return []
    return ["    - *%s:*" % LABEL[kind]] + ["        - %s" % esc(l) for l in lines]
