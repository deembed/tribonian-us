"""covers.py -- the cover colors of Supreme Court filings (Rule 33.1(g)), one table for the PDF and the notes.

The Court colors booklet covers so a reader tells documents apart at a glance; the scotus build tints each
filing's header box (and its swatch on the contents page and in the coupling table) the same way, and the
filing notes get an Obsidian callout of that color. Source: the Court's booklet-format chart
(supremecourt.gov/casehand/BookletFormatSpecificChart2026.pdf), which restates Rule 33.1(g).

The tints are for the reMarkable Paper Pro's muted color screen, checked there with the swatch page
(tribonian scotus swatches): text stays black on every one; white gets a visible frame; dark green is a
lighter tint with a dark edge, so light and dark green still read as two colors.

    python3 -m tribonian.covers > snippet.css      the Obsidian CSS (sync-vault writes it into the vault)
"""

# name -> (what the Court's chart calls it, fill, frame), hex RGB
COLORS = {
    "white":      ("white",       "FFFFFF", "6E6E6E"),
    "orange":     ("orange",      "F7CFA0", "B8732A"),
    "cream":      ("cream",       "F7F0D6", "A89A62"),
    "tan":        ("tan",         "E4D2B0", "8E7448"),
    "lightblue":  ("light blue",  "CDE2F6", "4A7DB0"),
    "lightred":   ("light red",   "F6CFCF", "B04A4A"),
    "lightgreen": ("light green", "D8EED0", "5E9A52"),
    "darkgreen":  ("dark green",  "A9D3A0", "2E6B2E"),
    "yellow":     ("yellow",      "F7EFA2", "A8962A"),
    "gray":       ("gray (not in the Rule)", "E2E2E2", "7A7A7A"),
}

# for the swatch page and the docs: which filings take which color
ROLES = [
    ("white", "Petition for a writ of certiorari"),
    ("orange", "Brief in opposition"),
    ("cream", "Amicus brief at the petition stage (either side)"),
    ("tan", "Reply to the brief in opposition; supplemental brief; joint appendix; anything not listed"),
    ("lightblue", "Petitioner's brief on the merits"),
    ("lightred", "Respondent's brief on the merits"),
    ("lightgreen", "Amicus brief on the merits supporting petitioner, or neither party"),
    ("darkgreen", "Amicus brief on the merits supporting respondent"),
    ("yellow", "Reply brief on the merits"),
    ("gray", "Decision below (not a filing; proposed)"),
]


def cover(kind, stage, side=None):
    """A filing's cover color under Rule 33.1(g). kind: petition | opposition | reply | brief | amicus |
    supplemental | joint-appendix | below; stage: cert | merits | after argument | below."""
    if kind == "below" or stage == "below":
        return "gray"
    if stage == "application":
        return "white"                            # Rule 22: applications go on letter-size paper, no colored cover
    if kind == "petition":
        return "white"
    if kind == "opposition":
        return "orange"
    if kind == "amicus":
        if stage == "cert":
            return "cream"
        return "darkgreen" if side == "respondent" else "lightgreen"
    if kind == "brief":
        return "lightred" if side == "respondent" else "lightblue"
    if kind == "reply":
        return "tan" if stage == "cert" else "yellow"
    return "tan"                                  # supplemental, joint appendix, anything else


def tex_defs():
    """xcolor definitions (cover-<name>, cover-<name>-edge) and \\coverswatch{name}: a small framed square."""
    out = []
    for name, (_, fill, edge) in COLORS.items():
        out.append("\\definecolor{cover-%s}{HTML}{%s}\\definecolor{cover-%s-edge}{HTML}{%s}" % (name, fill, name, edge))
    out.append("\\newcommand{\\coverswatch}[1]{{\\setlength{\\fboxsep}{0pt}\\setlength{\\fboxrule}{0.4pt}"
               "\\fcolorbox{cover-#1-edge}{cover-#1}{\\rule{0pt}{0.62em}\\rule{0.62em}{0pt}}}}")
    return "\n".join(out) + "\n"


def css():
    """Obsidian callouts > [!cover-<name>] in the cover colors (black text), for the filing notes."""
    out = ["/* tribonian-covers.css -- Supreme Court cover colors (Rule 33.1(g)) for the filing notes' callouts.",
           "   Written by tribonian sync-vault from src/tribonian/covers.py; enable it under Appearance > CSS snippets. */"]
    for name, (label, fill, edge) in COLORS.items():
        out.append('.callout[data-callout="cover-%s"] { --callout-color: %s; --callout-icon: lucide-book-open; '
                   'background-color: #%s; border: 1px solid #%s; color: #000; }' % (
                       name, ", ".join(str(int(edge[i:i + 2], 16)) for i in (0, 2, 4)), fill, edge))
        out.append('.callout[data-callout="cover-%s"] .callout-title, .callout[data-callout="cover-%s"] .callout-content '
                   '{ color: #000; }' % (name, name))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import sys
    sys.stdout.write(css())
