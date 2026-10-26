"""render.py -- what both corpora share on the way to pdflatex: the LaTeX preamble (boxes,
running header, footer), the box-style rules, and the escaping that is the single path to pdflatex.

Used by usc2md, case2md, scotus and cocite_subdoc, so the US Code and the cases look the same on the
tablet. UNMAPPED counts every character tex_safe() had to drop or mark; callers report and clear it.
"""

TEX_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    # spaces pdflatex's T1 encoding has no glyph for
    "\u00a0": "~", "\u202f": r"\,", "\u2009": r"\,", "\u2007": "~",
}

# Characters pdflatex (inputenc utf8 + T1) has no definition for. Found by
# scanning every title's XML and compiling one test line per character.
_FRACS = {"⅓": (1, 3), "⅔": (2, 3), "⅕": (1, 5), "⅖": (2, 5), "⅗": (3, 5), "⅘": (4, 5),
          "⅙": (1, 6), "⅚": (5, 6), "⅐": (1, 7), "⅛": (1, 8), "⅜": (3, 8), "⅝": (5, 8),
          "⅞": (7, 8), "⅑": (1, 9), "⅒": (1, 10)}
TEX_ESCAPES.update({c: r"\vfrac{%d}{%d}" % nd for c, nd in _FRACS.items()})   # \vfrac: see PREAMBLE
TEX_ESCAPES.update({
    "\u2032": r"\ensuremath{'}", "\u2033": r"\ensuremath{''}",       # prime, double prime (33°27.8′)
    "\u2212": r"\ensuremath{-}", "≤": r"\ensuremath{\leq}", "≥": r"\ensuremath{\geq}",
    "α": r"\ensuremath{\alpha}", "β": r"\ensuremath{\beta}", "η": r"\ensuremath{\eta}",
    "μ": r"\ensuremath{\mu}", "Φ": r"\ensuremath{\Phi}", "ϕ": r"\ensuremath{\phi}",
    "\u02bb": r"\textquoteleft{}",                                   # Hawaiian okina
    "☐": r"{\fboxsep0pt\fbox{\rule{0pt}{.6em}\rule{.6em}{0pt}}}",      # ballot box on court forms
    "Ⓜ": r"\textcircled{\scriptsize M}",                             # mask-work symbol, 17 U.S.C. 909
    "\ue0e9": "", "\ue1ab": "",                                       # private-use junk inside tables
})
# Combining marks that survive NFC (no precomposed letter, e.g. A + overline in
# HALEAKALA): LaTeX wants the accent command BEFORE the letter.
TEX_ACCENTS = {"\u0300": r"\`", "\u0301": r"\'", "\u0302": r"\^", "\u0303": r"\~",
               "\u0304": r"\=", "\u0305": r"\=", "\u0308": r'\"', "\u030c": r"\v"}


# Safety net for characters neither table above knows. pdflatex (utf8 + T1) handles ASCII, Latin-1,
# Latin Extended-A and these; anything else would stop the whole build with "Unicode character ...
# not set up for use with LaTeX". Block and geometric shapes (U+2580-25FF, e.g. the "■" in scanned
# opinions) are scanner specks: dropped. Any other unknown prints as a boxed question mark (a plain
# "?" in PDF bookmarks), so the gap stays visible.
# Every substitution is counted in UNMAPPED for the caller to report.
TEX_OK = set("‐‑‒–—―‘’‚“”„†‡•…‰‹›€™№←↑→↓ȘșȚț")
UNMAPPED = {}


def tex_safe(c):
    o = ord(c)
    if (0x20 <= o < 0x180 and not 0x7F <= o < 0xA0) or c in "\t\n\r" or c in TEX_OK:
        return c
    UNMAPPED[c] = UNMAPPED.get(c, 0) + 1
    if o < 0x20 or 0x7F <= o < 0xA0 or 0x2580 <= o <= 0x25FF:     # control characters (BEL in a parenthetical), specks
        return ""
    return r"\texorpdfstring{{\fboxsep0.6pt\fbox{\scriptsize ?}}}{?}"


def tex_chars(s):
    """Escape a string for LaTeX, character by character."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if nxt in TEX_ACCENTS:
            out.append("%s{%s}" % (TEX_ACCENTS[nxt], TEX_ESCAPES.get(c, c)))
            i += 2
            continue
        if c not in TEX_ACCENTS:             # a stray mark with no letter before it is dropped
            out.append(TEX_ESCAPES[c] if c in TEX_ESCAPES else tex_safe(c))
        i += 1
    return "".join(out)


ODD_SPACES = {0x202F: 0xA0, 0x2009: 0xA0, 0x2007: 0xA0}
# Typographic ligatures (pdftotext keeps them from slip opinions and filings: "Plaintiﬀs"). Folded to
# plain letters at parse time, like the odd spaces: T1 can't set them, and search needs the letters.
LIGATURES = {0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl", 0xFB05: "st", 0xFB06: "st"}
FOLD = {**ODD_SPACES, **LIGATURES}


PREAMBLE = r"""% __DOCTITLE__ -- compile with pdflatex (twice; tcolorbox places split boxes on the second pass)
\documentclass[10pt]{article}
% Reproducible bytes: the trailer /ID is normally an MD5 of the output path and the clock,
% and every build runs in a fresh scratch folder. Fixing it to the job name (with
% SOURCE_DATE_EPOCH set by the build scripts for the dates) makes an unchanged source
% rebuild to an identical PDF, so sync_remarkable.py does not re-upload it.
\ifdefined\pdftrailerid\pdftrailerid{\jobname}\fi

% reMarkable Paper Pro: 1620 x 2160 px @ 229 PPI.
\usepackage[paperwidth=509.34pt,paperheight=679.13pt,margin=24pt,
            headheight=11pt,headsep=5pt,footskip=13pt]{geometry}

\usepackage{__FONTPKG__}
\usepackage[T1]{fontenc}
\usepackage{microtype}
\usepackage[normalem]{ulem}    % \sout for repealed text; normalem keeps \emph italic
% case2md.py: US Code cites that changed since the opinion (usc_status.py). Yellow, breakable (ulem leaders):
% \uscmark = highlight; \usclight = pale (amended elsewhere in the section); \uscstrike = highlight + strike (number moved or gone).
\newcommand\uscmark{\bgroup\markoverwith{\textcolor{yellow}{\rule[-.55ex]{2pt}{2.45ex}}}\ULon}
\newcommand\usclight{\bgroup\markoverwith{\textcolor{yellow!35}{\rule[-.55ex]{2pt}{2.45ex}}}\ULon}
\newcommand\uscstrike{\bgroup\markoverwith{\rlap{\textcolor{yellow}{\rule[-.55ex]{2pt}{2.45ex}}}\rule[.55ex]{2pt}{.5pt}}\ULon}
\usepackage{tcolorbox}
\tcbuselibrary{skins,breakable}   % skins -> "enhanced"; breakable -> page splits
\usepackage{longtable}          % tables that run across pages (scotus.py: authorities by side)
% PDF outline (the reMarkable shows it as the table of contents). hidelinks: no
% coloured/boxed links. hyperfootnotes=false: marks and \footnotetext are paired
% by hand here, and hyperref's footnote links assume \footnote.
\usepackage[unicode,hidelinks,bookmarksopen,hyperfootnotes=false,pdftitle={__DOCTITLE__}]{hyperref}
% \headroom{n}: start a new page unless n lines still fit, so a heading never sits alone at a page's foot
\newcommand{\headroom}[1]{\par\ifdim\dimexpr\pagegoal-\pagetotal\relax<#1\baselineskip\newpage\fi}

\setlength{\parindent}{0pt}
\setlength{\parskip}{0.0\baselineskip}

% Notes block (--notes): set small, unboxed, under a rule.
\newcommand{\notesrule}{\par\vspace{5pt}\hrule height 0.3pt\vspace{3pt}}
\newenvironment{quoted}{\par\begingroup\small\advance\leftskip by 1.4em\advance\rightskip by 1em\ignorespaces}{\par\endgroup}

% Fractions the font has no glyph for (66 2/3 percent): small raised numerator,
% slash, small denominator. In a PDF outline title it degrades to "2/3".
\newcommand{\vfrac}[2]{\kern.08em\textsuperscript{#1}\kern-.12em/\kern-.08em{\scriptsize #2}}
\AtBeginDocument{\pdfstringdefDisableCommands{\def\vfrac#1#2{#1/#2}}}

% Bold label hangs at the left; wrapped lines align under the body text.
\newsavebox{\labbox}
\newcommand{\lab}[1]{%
  \sbox{\labbox}{\bfseries #1\ }%
  \hangindent=\wd\labbox \hangafter=1
  \usebox{\labbox}}

% enforce breakable is the load-bearing key: a tcolorbox nested inside another
% is unbreakable by default, which is what made the tall subsections overflow.
% enhanced gives flat open edges at a page break; arc=0pt removes the rounded
% corners everywhere else.
% lines before break: the minimum that must fit before tcolorbox will start or
% split a box here. The default of 2 is too low once boxes nest 4-5 deep --
% each level's padding eats the remaining height, so a sliver of page passes
% the 2-line test and you get pages holding one line. 4 clears it.
% Every item is its own top-level breakable box; nothing is nested. The
% frames of enclosing groups are drawn BY each item: a pair of side lines per
% ancestor at fixed offsets (borderline west/east), a top rule on a group's
% first item and a bottom rule on its last (tikz overlays on the frame node).
% Nested tcolorboxes cannot break across pages, and a nested group taller
% than a page simply overflows; this has no nesting, so nothing can.
\newlength{\lvlstep}\setlength{\lvlstep}{6pt}   % horizontal distance between nested frames
\newtcolorbox{lvl}[1][]{enhanced, breakable, lines before break=3,
  colback=white, colframe=black, boxrule=0pt, arc=0pt, outer arc=0pt,
  boxsep=0pt, top=1.5pt, bottom=1.5pt, before skip=0pt, after skip=0pt,
  #1}
% \sides{d}: side lines for d enclosing groups. \topline{d} / \botline{d}:
% the rule that opens/closes the group whose sides sit at offset (d-1)*step.
\newcommand{\sides}[1]{\tcbset{borderline west={0.5pt}{#1\lvlstep}{black},
                               borderline east={0.5pt}{#1\lvlstep}{black}}}
\newcommand{\hrulestep}[2]{\draw[line width=0.5pt]
  ([xshift=#1\lvlstep]frame.#2 west) -- ([xshift=-#1\lvlstep]frame.#2 east);}
__RULES__
% Running header: the citation path in effect at the top of each page.
% \markright is lost inside breakable tcolorboxes (tcolorbox has no mark
% handling), so each label instead writes its path and page number to the
% .aux at shipout, when \thepage is real. The header for page N shows the
% last path recorded on any earlier page. Needs the usual second pass.
\makeatletter
\newcommand{\pathmark}[2]{\expandafter\gdef\csname hdr@#1\endcsname{#2}}
% \protected@write, not \write: robust commands in the path (\S, \textbackslash, \fbox
% via \texorpdfstring) are written as themselves, not expanded into the .aux; \thepage
% still expands at shipout.
\newcommand{\recordpath}[1]{\protected@write\@auxout{}{\string\pathmark{\thepage}{#1}}}
\newcommand{\pathheader}{%
  \begingroup\count@=\value{page}\advance\count@ by -1
  \loop\ifnum\count@>0
    \ifcsname hdr@\number\count@\endcsname
      \csname hdr@\number\count@\endcsname\count@=0
    \else\advance\count@ by -1\fi
  \repeat\endgroup}
% Footer: the citation and "page N of M". The LaTeX kernel (2021+) writes the
% last page number to the .aux itself as \@abspage@last; "?" on the first pass.
\newcommand{\lastpg}{\@ifundefined{@abspage@last}{?}{\@abspage@last}}
% \docfoot: the footer's citation; a renderer with a long title sets a short one (scotus.py: "St. Mary
% Catholic Parish v. Roy, No. 25-581").
\newcommand{\docfoot}{__DOCTITLE__}
\def\ps@pathhead{%
  \def\@oddhead{{\footnotesize\itshape\pathheader}\hfil}\let\@evenhead\@oddhead
  \def\@oddfoot{\footnotesize \docfoot\hfil page \thepage\ of \lastpg}\let\@evenfoot\@oddfoot}
\makeatother
\pagestyle{pathhead}

\begin{document}

{\Large\bfseries __DOCTITLE__\par}
{\large __DOCHEAD__\par}
{\footnotesize __SRCLINE__\par}
\hrule
\vspace{4pt}
"""


RULES = {
    "boxes": "",
    "rails": "% rails: east sides and group rules are not drawn\n\\renewcommand{\\sides}[1]{\\tcbset{borderline west={0.5pt}{#1\\lvlstep}{black}}}\n\\renewcommand{\\hrulestep}[2]{}",
}
