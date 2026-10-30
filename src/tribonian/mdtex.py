"""mdtex.py -- the small markdown subset the generated note fragments use, as TeX for the PDF: '##' / '###'
headings, paragraphs, pipe tables, numbered and bulleted items with indented continuation lines and '> ' quotes,
**bold**, *italic*, [[note|label]] (the label) and [text](url). Used for map citedby's Cited by section."""
import re

from tribonian import case2md as c

_TOKEN = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|\[\[[^\]]+\]\]|\[[^\]]+\]\([^)\s]+\))")


def inline(t):
    out = []
    for part in _TOKEN.split(t):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append("\\textbf{%s}" % c.esc(part[2:-2], "tex"))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append("\\emph{%s}" % c.esc(part[1:-1], "tex"))
        elif part.startswith("[["):
            out.append(c.esc(part[2:-2].split("|")[-1], "tex"))
        elif re.fullmatch(r"\[[^\]]+\]\([^)\s]+\)", part):
            text, url = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", part).groups()
            out.append("\\href{%s}{%s}" % (url.replace("%", "\\%").replace("#", "\\#"), c.esc(text, "tex")))
        else:
            out.append(c.esc(part, "tex"))
    return "".join(out)


def _table(rows):
    cells = [[x.strip() for x in r.strip().strip("|").split("|")] for r in rows]
    sep = next((i for i, r in enumerate(cells) if all(re.fullmatch(r":?-{3,}:?", x) for x in r if x)), None)
    align = "".join("r" if x.endswith(":") else "l" for x in cells[sep]) if sep is not None else "l" * len(cells[0])
    body = [r for i, r in enumerate(cells) if i != sep]
    lines = []
    for i, r in enumerate(body):
        r = (r + [""] * len(align))[:len(align)]
        lines.append(" & ".join(("\\textbf{%s}" if i == 0 and sep == 1 else "%s") % inline(x) for x in r) + " \\\\")
        if i == 0 and sep == 1:
            lines.append("\\hline")
    return "\\par\\vspace{3pt}{\\footnotesize\\noindent\\begin{tabular}{%s}\n%s\n\\end{tabular}}\\par\\vspace{3pt}\n" % (align, "\n".join(lines))


def md_to_tex(md):
    out, i, lines, in_list = [], 0, md.splitlines(), None
    def close():
        nonlocal in_list
        if in_list:
            out.append("\\end{list}\n")
            in_list = None
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("## "):                          # the part's own title is drawn by the caller
            close()
            i += 1
            continue
        if ln.startswith("### "):
            close()
            out.append("\\par\\vspace{6pt}\\noindent{\\bfseries %s}\\par\\vspace{2pt}\n" % inline(ln[4:].strip()))
            i += 1
            continue
        if ln.lstrip().startswith("|"):
            close()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        m = re.match(r"(\d+\.|[-*])\s+(.*)", ln)
        if m:
            kind = "num" if m.group(1)[0].isdigit() else "bul"
            if in_list != kind:
                close()
                out.append("\\begin{list}{}{\\leftmargin=1.8em\\labelwidth=1.5em\\labelsep=.3em\\itemsep=3pt\\parsep=1pt\\topsep=2pt}\n")
                in_list = kind
            body = [inline(m.group(2))]
            i += 1
            while i < len(lines) and lines[i].startswith("   ") and lines[i].strip():
                s = lines[i].strip()
                body.append("{\\itshape %s}" % inline(s[2:]) if s.startswith("> ") else inline(s))
                i += 1
            out.append("\\item[%s] %s\n" % ("%s" % m.group(1) if kind == "num" else "--", "\\par ".join(body)))
            continue
        close()
        para = [ln.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"(#|\||\d+\.\s|[-*]\s)", lines[i].lstrip()):
            para.append(lines[i].strip())
            i += 1
        out.append("\\par\\noindent %s\\par\\vspace{2pt}\n" % inline(" ".join(para)))
    close()
    return "".join(out)


def part_tex(md, title, anchor, small=True):
    """A closing part of the PDF from a markdown fragment: a contents entry, its title, then the fragment."""
    if not md.strip():
        return ""
    return ("\\clearpage\\recordcover{none}\\recordstart{%s}\\noindent\\tocback\\tocadd{tocstage}{%s}"
            "\\pdfbookmark[0]{%s}{%s}{\\Large\\bfseries %s\\par}\\vspace{4pt}\n%s%s%s\n"
            % (title, title, title, anchor, title, "{\\small " if small else "", md_to_tex(md), "}" if small else ""))
