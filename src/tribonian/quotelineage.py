"""quotelineage.py -- what later opinions cite a case FOR, sentence by sentence: the hatching in a case's PDF
(case2md.mark_quotes) and the "Quote lineage" part at its end.

Each parenthetical about the case ("holding that ...", the parens table) is matched to the sentence of the opinion it
restates (casemap.credit_sentences: 5+ consecutive shared words), the same matcher `tribonian map citedby` uses. A
sentence matched by QUOTED_MIN+ parentheticals is hatched, with "xN" linking to its entry here:
    when   the years of the opinions that wrote them, by decade
    where  their courts, by level, the most frequent named
    how    their wordings, most frequent first, each with an example case
    from   the wording's lineage, when it is older than the case (map citedby's trace, cases/citedby/<cluster>/results.json)
Needs the local database (parens, opmap, clusters, dockets, courts); nothing is marked without it.
"""
import json
import re
from collections import Counter, defaultdict

from tribonian import case2md as c


def _compact(t):
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def cite_only(text):
    """A 'sentence' that is a citation ("Burlington Truck Lines, Inc. v. United States, 371 U. S. 156, 168 (1962)."):
    parentheticals that name the case it quotes ("quoting Burlington Truck ...") match it, but it says nothing."""
    from tribonian import scotus
    rest = scotus.CITE_RE.sub(" ", text)
    rest = re.sub(r"\([^()]*\d{4}\)|\b[A-Z][\w.,'&-]*|\b(?:v|at|see|also|cf|id|supra|ibid)\b\.?|[\d,;.\-\u2013()]", " ", rest)
    return scotus.CITE_RE.search(text) is not None and len(rest.split()) <= 3


def data(con, cdata, index, minimum, cid):
    """-> {"entries": [entry, ...] in the opinion's order, "bulk": date} or None. entry: k, n, text, where, compact,
    years, decades, levels, courts, how [(wording, count, example)], lineage [...]."""
    from tribonian import casemap
    if con is None:
        return None
    op_ids = [o["id"] for o in cdata.get("opinions") or [] if o.get("id")]
    if not op_ids:
        return None
    try:
        q = ",".join("?" * len(op_ids))
        parens = con.execute("SELECT describing, score, text FROM parens WHERE described IN (%s)" % q, op_ids).fetchall()
    except Exception:
        return None
    if not parens:
        return None
    _, src, _ = casemap.source_sentences(cdata, index)
    words = [casemap.norm(s["text"]).split() for s in src]
    shingles = {}
    for i, w in enumerate(words):
        for k in range(len(w) - 4):
            shingles.setdefault(" ".join(w[k:k + 5]), set()).add(i)
    by_sent = defaultdict(list)
    for describing, score, text in parens:
        for i in casemap.credit_sentences(casemap.norm(text).split(), shingles):
            by_sent[i].append((describing, score or 0, " ".join((text or "").split())))
    keep = sorted(i for i, v in by_sent.items() if len(v) >= minimum and not cite_only(src[i]["text"]))
    if not keep:
        return None
    # who wrote each parenthetical: opinion -> case, date, court
    ids = sorted({d for i in keep for d, _, _ in by_sent[i] if d})
    info = {}
    for a in range(0, len(ids), 500):
        chunk = ids[a:a + 500]
        for row in con.execute(
                "SELECT m.id, coalesce(nullif(cl.case_name_short,''), nullif(cl.case_name,''), 'cluster ' || cl.id), cl.date_filed, "
                "d.court_id, ct.citation_string, ct.jurisdiction FROM opmap m JOIN clusters cl ON cl.id = m.cluster_id "
                "LEFT JOIN dockets d ON d.id = cl.docket_id LEFT JOIN courts ct ON ct.id = d.court_id "
                "WHERE m.id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            info[row[0]] = row[1:]
    lineage = {}
    f = c.ROOT / "cases" / "citedby" / str(cid) / "results.json"
    if f.exists():
        try:
            for x in json.loads(f.read_text(encoding="utf-8")).get("sentences_credited") or []:
                if x.get("lineage"):
                    lineage[_compact(x["text"])] = x["lineage"]
        except ValueError:
            pass
    entries = []
    for k, i in enumerate(keep, 1):
        rows = by_sent[i]
        who = [info.get(d) for d, _, _ in rows if info.get(d)]
        years = sorted(int(w[1][:4]) for w in who if w[1] and w[1][:4].isdigit())
        wordings = Counter(t for _, _, t in rows)
        example = {}
        for d, score, t in sorted(rows, key=lambda r: -r[1]):
            if t not in example and info.get(d):
                v = info[d]
                example[t] = "%s, %s %s" % (v[0], v[3] or v[2] or "", (v[1] or "")[:4])
        comp = _compact(src[i]["text"])
        part = src[i]["part"]
        entries.append({
            "k": k, "n": len(rows), "text": src[i]["text"], "compact": comp,
            "where": "%s, %s, *%s" % (src[i]["who"], part if part.startswith("n.") else ("Part " + part if part else "intro"), src[i]["page"]),
            "years": (years[0], years[-1]) if years else None,
            "decades": sorted(Counter("%d0s" % (y // 10) for y in years).items()),
            "levels": Counter(casemap.level_of(w[2], w[4]) for w in who),
            "courts": Counter(w[3] for w in who if w[3]).most_common(4),
            "how": [(t, m, example.get(t, "")) for t, m in wordings.most_common(3)],
            "lineage": sorted(lineage.get(comp) or [], key=lambda x: x.get("date") or ""),
        })
    return {"entries": entries, "lineage_run": f.exists()}


def section_tex(qd, bulk=""):
    """The closing part: one entry per hatched sentence, linked both ways with the xN in the text."""
    from tribonian import casemap
    if not qd or not qd["entries"]:
        return ""
    E = lambda t: c.esc(t, "tex")
    out = ["\\clearpage\\recordcover{none}\\recordstart{Quote lineage}\\noindent\\tocback\\tocadd{tocstage}{Quote lineage}"
           "\\pdfbookmark[0]{Quote lineage}{quotelineage}{\\Large\\bfseries Quote lineage\\par}\\vspace{4pt}",
           "{\\scriptsize\\itshape What later opinions cite this case for, sentence by sentence: the parentheticals they wrote "
           "(\"holding that \\dots\") matched to the sentence they restate (5+ consecutive shared words), the hatched "
           "sentences in the text. \\(\\times\\)N: how many parentheticals; when, where and how they were written; and where "
           "the wording itself came from, when it is older than this case%s. Other courts' words: quote the opinion.%s\\par}"
           "\\vspace{6pt}" % ("" if qd.get("lineage_run") else " (not traced yet: tribonian map citedby)",
                              " Bulk data %s." % E(bulk) if bulk else "")]
    for e in qd["entries"]:
        text = e["text"] if len(e["text"]) <= 320 else e["text"][:320].rsplit(" ", 1)[0] + " \u2026"
        out.append("\\noindent\\hypertarget{ql-%d}{}{\\small\\bfseries\\textcolor{blue!70!black}{\\(\\times\\)%d}}\\enspace"
                   "{\\small %s}\\hfill{\\footnotesize\\hyperlink{qs-%d}{\\(\\uparrow\\)\\,%s, p.\\,\\pageref{qs-%d}}}\\par\\nopagebreak"
                   % (e["k"], e["n"], E(text), e["k"], E(e["where"]), e["k"]))
        items = []
        if e["years"]:
            items.append("\\textit{When:} %d\\textendash %d \\enspace{\\footnotesize(%s)}" % (
                e["years"][0], e["years"][1], E(", ".join("%s %d" % d for d in e["decades"]))))
        lv = ", ".join("%s %d" % (casemap.LEVEL_NAMES[l], e["levels"][l]) for l in casemap.LEVELS if e["levels"][l])
        if lv:
            items.append("\\textit{Where:} %s%s" % (E(lv), ("; most: " + E(", ".join("%s %d" % x for x in e["courts"]))) if e["courts"] else ""))
        for t, m, ex in e["how"]:
            t = t if len(t) <= 260 else t[:260].rsplit(" ", 1)[0] + " \u2026"
            items.append("\\textit{How:} (%s)%s%s" % (E(t), " \\(\\times\\)%d" % m if m > 1 else "",
                                                      " {\\footnotesize --- \\emph{%s}}" % E(ex) if ex else ""))
        if e["lineage"]:
            chain = " \\(\\rightarrow\\) ".join("%s, %s %s {\\footnotesize(%d words)}" % (
                E(x.get("case") or ""), E(x.get("court") or ""), E((x.get("date") or "")[:4]), x.get("words") or 0) for x in e["lineage"])
            items.append("\\textit{Wording from:} %s \\(\\rightarrow\\) this case" % chain)
        out.append("{\\leftskip=1.2em\\footnotesize\\noindent " + "\\par\\noindent ".join(items) + "\\par}\\vspace{5pt}")
    return "\n".join(out) + "\n"


def section_md(qd):
    """The note's '## Quote lineage': the same entries as the PDF's, without the links."""
    from tribonian import casemap
    if not qd or not qd["entries"]:
        return []
    E = lambda t: c.esc(t, "md")
    L = ["## Quote lineage", "",
         "*What later opinions cite this case for, sentence by sentence: their parentheticals (\"holding that …\") matched "
         "to the sentence they restate (5+ consecutive shared words); ×N = how many. Other courts' words: quote the opinion.%s*"
         % ("" if qd.get("lineage_run") else " Wording not traced yet: tribonian map citedby."), ""]
    for e in qd["entries"]:
        L += ["### ×%d · %s" % (e["n"], E(e["where"])), "", "> " + E(e["text"]), ""]
        if e["years"]:
            L.append("- **When:** %d–%d (%s)" % (e["years"][0], e["years"][1], ", ".join("%s %d" % d for d in e["decades"])))
        lv = ", ".join("%s %d" % (casemap.LEVEL_NAMES[l], e["levels"][l]) for l in casemap.LEVELS if e["levels"][l])
        if lv:
            L.append("- **Where:** %s%s" % (lv, ("; most: " + ", ".join("%s %d" % x for x in e["courts"])) if e["courts"] else ""))
        for t, m, ex in e["how"]:
            L.append("- **How:** (%s)%s%s" % (E(t), " ×%d" % m if m > 1 else "", " — *%s*" % E(ex) if ex else ""))
        if e["lineage"]:
            L.append("- **Wording from:** " + " → ".join("%s, %s %s (%d words)" % (
                E(x.get("case") or ""), x.get("court") or "", (x.get("date") or "")[:4], x.get("words") or 0) for x in e["lineage"]) + " → this case")
        L.append("")
    return L
