"""scotus.py -- a pending Supreme Court case: its docket, filings and the decision below, as one
PDF for the tablet plus Obsidian notes, and the authorities the parties fight over.

    tribonian scotus fetch 25-579
        The Court's docket (supremecourt.gov/RSS/Cases/JSON/25-579.json, Proceedings and
        Orders) -> cases/scotus/25-579.json, and every filed PDF it links (proofs of service
        and word-count certificates skipped) -> cases/scotus/25-579/, with pdftotext -layout
        beside each. Not CourtListener: no API limit. Rerun any time; only new filings download.

    tribonian scotus build 25-579 [--amici] [--appendix] [--depth N]
        One PDF, pdf/Pending SCOTUS/<slug>.pdf, in parts: the Clerk's question presented, the
        decision below (from the local database), the cert-stage filings, the merits filings.
        Notes: md-cases/Pending SCOTUS/<slug>.md (the docket) and one per filing.
        Authorities: each filing's reporter citations, resolved to CourtListener clusters
        through the local database, and which filings share them (bibliographic coupling).
        --amici   include amicus briefs (default: the parties' filings only)
        --appendix  keep appendices (the petition's appendix repeats the decision below)
        --depth N  also write case notes for the decision below and every authority the
                   filings cite (depth 1), and what those cite (depth 2, ...), the same way
                   casemap.py map does: full text for the first --text steps (default 1),
                   stubs beyond. Then tribonian build builds their PDFs.

    tribonian scotus recent [--json] [--days 365]
        What the Court did in the last year, from its public pages -> cases/scotus/_recent.json:
        opinions (opinions/slipopinion/<term>), cases granted review (orders/<term>grantednotedlist.pdf)
        and the orders lists (orders/ordersofthecourt/<term>: grants, summary dispositions, denials,
        orders in pending cases). Order PDFs are kept in cases/scotus/_recent/. There is no public
        list of all pending petitions for certiorari; a petition appears only once the Court acts.

No language model anywhere: regular expressions, text layout, set arithmetic.
"""
import argparse, json, re, sqlite3, subprocess, sys, urllib.parse
from difflib import SequenceMatcher
from collections import Counter, OrderedDict
from datetime import date, datetime
from pathlib import Path

from tribonian import case2md as c
from tribonian import lawcites

SCOTUS = c.CASES / "scotus"
FEED = "https://www.supremecourt.gov/RSS/Cases/JSON/%s.json"
SITE = "https://www.supremecourt.gov/"
UA = "Mozilla/5.0 (X11; Linux x86_64) case2md/0.3"
FOLDER = "Pending SCOTUS"
SKIP_LINK = re.compile(r"proof of service|certificate|affidavit", re.I)


def norm_number(s):
    """'25-579', '25-00579', 'No. 25-579 ' -> '25-579'."""
    m = re.search(r"(\d{2})[-A]0*(\d+)", s or "")
    if not m:
        sys.exit("cannot read %r as a docket number like 25-579" % s)
    return "%s-%s" % (m.group(1), m.group(2))


def get(url, out=None):
    """curl, because supremecourt.gov's front end refuses some clients. -> bytes, or None on failure."""
    cmd = ["curl", "-sSfL", "--retry", "2", "-m", "180", "-A", UA] + (["-o", str(out)] if out else [])
    r = subprocess.run(cmd + ["--", url], capture_output=True)   # -- : a feed URL is never read as a flag; -o must come before it
    if r.returncode:
        print("  ! %s: %s" % (url, r.stderr.decode(errors="replace").strip()[:200]))
        return None
    return out.read_bytes() if out else r.stdout


def when(s):
    """'Nov 14 2025' -> '2025-11-14'; '' when unparsed (callers treat '' as unknown, never compare it to a date)."""
    for fmt in ("%b %d %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime((s or "").strip(), fmt).date().isoformat()
        except ValueError:
            pass
    if s and s not in _undated:
        _undated.add(s)
        print("  ! unparsed date %r" % s)
    return ""


_undated = set()


# ---------------------------------------------------------------- what each docket entry is

def classify(text):
    """Docket text -> (kind, side, filer). side: 'petitioner' | 'respondent' | 'neither' | ''."""
    t = " ".join((text or "").split())
    side = ""
    m = re.search(r"\b(?:in support of|supporting)\s+(?:the\s+)?(petitioners?|respondents?|neither party)", t, re.I)
    if m:
        side = "neither" if m.group(1).lower().startswith("neither") else m.group(1).lower().rstrip("s")
    m = re.match(r"(?i)brief amic(?:us|i) curiae of (.+?)(?:\s+(?:in support of|supporting)\b.*)?\s+filed", t)
    if m:
        return "amicus", side, m.group(1).strip(" ,.")
    rules = [
        (r"(?i)^petition for a writ of certiorari", "petition", "petitioner"),
        (r"(?i)^brief of respondents?\b.*\bin opposition", "opposition", "respondent"),
        (r"(?i)^reply of petitioners?", "reply", "petitioner"),
        (r"(?i)^(?:reply )?brief of petitioners?\b.*\b(?:reply|in reply)", "reply", "petitioner"),
        (r"(?i)^reply brief of petitioners?", "reply", "petitioner"),
        (r"(?i)^brief of petitioners?\b", "brief", "petitioner"),
        (r"(?i)^brief of respondents?\b", "brief", "respondent"),
        (r"(?i)^joint appendix", "joint-appendix", ""),
        (r"(?i)^supplemental brief of petitioners?", "supplemental", "petitioner"),
        (r"(?i)^supplemental brief of respondents?", "supplemental", "respondent"),
        (r"(?i)^(?:letter|motion|application|waiver|record|response|consent|blanket|notice)", "other", ""),
    ]
    for pat, kind, s in rules:
        if re.match(pat, t):
            m = re.match(r"(?i)^(?:reply |supplemental )?(?:brief )?(?:of )?(?:petitioners?|respondents?)\s+(.+?)\s+(?:in opposition\s+)?filed", t)
            return kind, s or side, (m.group(1).strip(" ,.") if m else "")
    return "other", side, ""


def stage_dates(entries):
    """-> (date cert granted, date argued), ISO, or ''."""
    grant = next((e["date"] for e in entries if re.search(r"(?i)\bpetition granted\b", e["text"])), "")
    argued = next((e["date"] for e in entries if re.match(r"(?i)^argued\b", e["text"])), "")
    return grant, argued


def stage_of(day, grant, argued):
    if not day:   # unparsed date (when() already warned): nothing to compare, take the default
        return "cert"
    if argued and day >= argued:
        return "after argument"
    if grant and day > grant:
        return "merits"
    return "cert"


# ---------------------------------------------------------------- fetch

def case_file(num):
    return SCOTUS / ("%s.json" % num)


def fetch(args):
    num = norm_number(args.number)
    SCOTUS.mkdir(parents=True, exist_ok=True)
    raw = get(FEED % num)
    if not raw:
        sys.exit("could not read the docket for %s" % num)
    feed = json.loads(raw.decode("utf-8-sig"))
    folder = SCOTUS / num
    folder.mkdir(exist_ok=True)
    old = json.loads(case_file(num).read_text(encoding="utf-8")) if case_file(num).exists() else {}
    have = {l["url"]: l for e in old.get("entries", []) for l in e.get("links", [])}

    entries = []
    for i, p in enumerate(feed.get("ProceedingsandOrder") or []):
        e = {"n": i + 1, "date": when(p.get("Date")), "text": " ".join((p.get("Text") or "").split()), "links": []}
        e["kind"], e["side"], e["filer"] = classify(e["text"])
        for l in p.get("Links") or []:
            url = l.get("DocumentUrl") or ""
            ent = dict(have.get(url) or {})
            ent.update({"desc": (l.get("Description") or "").strip(), "file": l.get("File") or "", "url": url})
            e["links"].append(ent)
        entries.append(e)
    grant, argued = stage_dates(entries)
    for e in entries:
        e["stage"] = stage_of(e["date"], grant, argued)

    qp = feed.get("QPLink") or ""
    qp_url = urllib.parse.urljoin(SITE + "docket/DocketFiles/HTML/Public/", qp) if qp else ""   # relative to the docket page
    data = {"number": num, "feed": {k: v for k, v in feed.items() if k != "ProceedingsandOrder"},
            "source": FEED % num, "retrieved": date.today().isoformat(), "granted": grant, "argued": argued,
            "qp": {"url": qp_url, **({k: v for k, v in old.get("qp", {}).items() if k != "url"})} if qp_url else {},
            "entries": entries}

    def save():
        case_file(num).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def pull(link, name):
        out = folder / name
        if not out.exists() or out.stat().st_size == 0:
            if get(link["url"], out) is None:
                return "!"
            new = True
        else:
            new = False
        txt = out.with_suffix(".txt")
        if not txt.exists():
            t = subprocess.run(["pdftotext", "-layout", str(out), str(txt)], capture_output=True, timeout=600)
            if t.returncode:
                return "!"
        link["pdf"], link["txt"] = str(out.relative_to(c.ROOT)), str(txt.relative_to(c.ROOT))
        link["chars"] = len(txt.read_text(encoding="utf-8", errors="replace").strip())
        save()
        return "+" if new else "="

    print("%s v. %s, No. %s" % (data["feed"].get("PetitionerTitle", "?").replace(", Petitioners", ""),
                                data["feed"].get("RespondentTitle", "?"), num))
    print("  below: %s %s, decided %s; cert granted %s; argued %s" % (
        data["feed"].get("LowerCourt", ""), data["feed"].get("LowerCourtCaseNumbers", ""),
        data["feed"].get("LowerCourtDecision", ""), grant or "-", argued or "-"))
    save()
    if qp_url:
        mark = pull(data["qp"], "question-presented.pdf")
        print("  %s question presented" % mark)
    for e in entries:
        marks = []
        for l in e["links"]:
            if SKIP_LINK.search(l["desc"]) or not l["url"]:
                continue
            name = Path(urllib.parse.unquote(urllib.parse.urlparse(l["url"]).path)).name
            marks.append(pull(l, re.sub(r"[^\w.\-]+", "_", name)))
        if marks or e["kind"] != "other":
            print("  %s %s  %-13s %-10s %s" % ("".join(marks) or " ", e["date"], e["kind"], e["side"] or "", e["text"][:80]))
    print("  (+ downloaded, = already here, ! failed)")
    print("saved %s" % case_file(num).relative_to(c.ROOT))
    print("next:  tribonian scotus build %s" % num)



# ================================================================ reading a brief
# Briefs are typeset booklets: page numbers alone at the head or foot ("12", "viii", "(I)",
# "3a"), a cover, front matter in roman pages, the body from page 1, appendices in "a" pages.
# The structure is given by the brief's own table of contents: each entry is found again in the
# body (on the page the table names, or next to it), and its indent and label (I., A., 1., a.)
# give the nesting. Between headings the RECAP layout reader does paragraphs, quotations and
# footnotes, so the text reaches the same renderer as every other document.

LABEL_RE = re.compile(r"^\s*\(?([ivxlcdm]{1,7}|[IVXLCDM]{1,7}|\d{1,3}a?)\)?\s*$")
TOC_PAGE_RE = re.compile(r"^\s*(?:(\(?[ivxlcdmIVXLCDM]+\)?|\d+a?)(?:\s*[-–,]\s*\d+a?)*|passim)\s*$")
TOC_LEAD_RE = re.compile(r"^(.*?)(?:\s*\.(?:\s?\.){2,}\s*|\s{3,})((?:\(?[ivxlcdmIVXLCDM]+\)?|\d+a?)(?:\s*[-–,]\s*\d+a?)*)\s*$")
ENTRY_LABEL_RE = re.compile(r"^([IVX]{1,5}|[A-Z]|[a-z]|\d{1,2})\.\s+(.*)$")
QP_RE = re.compile(r"^\s*QUESTIONS?\s+PRESENTED\b", re.I)
RANK = {"roman": 0, "upper": 1, "digit": 2, "lower": 3}


def page_label(lines):
    """Remove the page number at the head or foot of a page. -> (label or '', lines)."""
    idx = [i for i, l in enumerate(lines) if l.strip()]
    for i in (idx[:1] + idx[-1:]) if idx else []:
        m = LABEL_RE.match(lines[i])
        if m:
            return m.group(1).lower(), lines[:i] + [""] + lines[i + 1:]
    return "", lines


def norm(t):
    return re.sub(r"[^0-9a-z]", "", (t or "").lower())


def label_type(lab, stack):
    if re.fullmatch(r"\d+", lab):
        return "digit"
    if lab.islower():
        return "lower"
    prev = next((s for s in reversed(stack) if s[0] == "upper"), None)
    if len(lab) == 1 and prev and ord(lab) == ord(prev[1]) + 1:
        return "upper"                       # "C." after "B.", "I." after "H."
    if c.is_roman(lab):
        return "roman"
    return "upper"


def read_toc(pages):
    """pages: [(label, [lines])] of the front matter -> entries {label, text, page, level}."""
    rows, on = [], False
    for lab, lines in pages:
        for l in lines:
            t = l.strip()
            if re.match(r"(?i)^table of contents$", t):
                on = True
                continue
            if not on:
                continue
            if re.match(r"(?i)^table of (authorities|cited authorities)$", t) or re.match(r"(?i)^table of authorities", t) and not TOC_LEAD_RE.match(t):
                on = False
                break
            if not t or re.fullmatch(r"(?i)page\(?s?\)?", t):
                continue
            # a contents page that runs over: "TABLE OF CONTENTS—Continued" heads the next page
            cont = re.match(r"(?i)^table of contents\s*[-–—(]*\s*continued\)?\s*", t)
            if cont:
                t = t[cont.end():]
                if not t or re.fullmatch(r"(?i)page\(?s?\)?", t):
                    continue
                l = " " * (len(l) - len(l.lstrip())) + t
            rows.append(l.rstrip())
        if rows and not on:
            break
    entries, cur = [], None
    for l in rows:
        ind = len(l) - len(l.lstrip())
        if cur is None:
            cur = {"ind": ind, "text": ""}
        m = TOC_LEAD_RE.match(l.strip())
        body = m.group(1) if m else l.strip()
        cur["text"] = c.join_lines([cur["text"], body]) if cur["text"] else body
        if m or l.rstrip().endswith(":"):
            cur["page"] = m.group(2).strip("()").lower() if m else ""
            entries.append(cur)
            cur = None
    out, stack = [], []
    for e in entries:
        text = e["text"].strip().rstrip(":").strip()
        m = ENTRY_LABEL_RE.match(text)
        if m:
            typ = label_type(m.group(1), stack)
            while stack and RANK[stack[-1][0]] >= RANK[typ]:
                stack.pop()
            stack.append((typ, m.group(1)))
            out.append({"label": m.group(1), "text": m.group(2).strip(), "page": e["page"], "level": 1 + len(stack)})
        else:
            stack = []
            out.append({"label": "", "text": text, "page": e["page"], "level": 1})
    return out


def find_heading(lines, labels, pos, e):
    """Index range (i, j) of the body lines that hold TOC entry e, searching from pos, or None."""
    want = norm((e["label"] + " " if e["label"] else "") + e["text"])
    bare = norm(e["text"])
    if not bare:
        return None
    page = e["page"]
    ok_pages = {page}
    if page.isdigit():
        ok_pages |= {str(int(page) - 1), str(int(page) + 1)}
    for i in range(pos, len(lines) if page else min(len(lines), pos + 400)):
        if page and labels[lines[i].pi] not in ok_pages:
            if labels[lines[i].pi].isdigit() and page.isdigit() and int(labels[lines[i].pi]) > int(page) + 1:
                return None
            continue
        s, j = "", i
        while j < len(lines) and len(s) < len(want) + 4 and j - i < 8:
            s += norm(lines[j].text)
            j += 1
            for w in (want, bare):
                if s[:len(w)] == w or (len(s) >= len(w) and SequenceMatcher(None, s[:len(w)], w).ratio() >= 0.92):
                    if len(s) - len(w) <= 6:           # the heading fills its lines (a footnote mark at most)
                        return i, j
    return None


def classify_side(cover):
    sq = re.sub(r"[^A-Z]", "", cover.upper())
    for pat, side in (("NEITHERPARTY", "neither"), ("SUPPORTINGPETITIONER", "petitioner"), ("SUPPORTOFPETITIONER", "petitioner"),
                      ("SUPPORTINGRESPONDENT", "respondent"), ("SUPPORTOFRESPONDENT", "respondent"),
                      ("SUPPORTINGREVERSAL", "petitioner"), ("SUPPORTINGAFFIRMANCE", "respondent")):
        if pat in sq:
            return side
    return ""


def fill_labels(labs):
    """A page whose number pdftotext lost (a heading page, a page ending in footnotes) takes it
    from its neighbours: '', '2' -> '1', '2'; 'ii', '' -> 'ii', 'iii'. The cover stays ''."""
    labs = list(labs)
    def step(lab, d):
        if re.fullmatch(r"\d+", lab):
            return str(int(lab) + d) if int(lab) + d > 0 else ""
        m = re.fullmatch(r"(\d+)a", lab)
        if m:
            return "%da" % (int(m.group(1)) + d) if int(m.group(1)) + d > 0 else ""
        if c.is_roman(lab.upper()):
            n = c.from_roman(lab.upper()) + d
            return c.to_roman(n).lower() if n > 0 else ""
        return ""
    for _ in range(3):                         # the next page first: '' between 'xvii' and '2' is '1'
        for k in range(1, len(labs)):
            if not labs[k]:
                if k + 1 < len(labs) and labs[k + 1]:
                    labs[k] = step(labs[k + 1], -1)
                elif labs[k - 1]:
                    labs[k] = step(labs[k - 1], 1)
    return labs


def split_brief(text, appendix=False):
    """-> dict(cover, front [(label, lines)], body [(label, lines)], qp [(label, lines)], toc)."""
    pages = [page_label(p.replace("\r", "").split("\n")) for p in text.split("\f")]
    pages = [(lab, ls) for lab, ls in pages if any(x.strip() for x in ls) or lab]
    labs = fill_labels([lab for lab, ls in pages])
    pages = [(labs[k], ls) for k, (lab, ls) in enumerate(pages)]
    start = next((k for k, (lab, ls) in enumerate(pages) if lab == "1" and k > 0), None)   # a "1" on the cover is not page 1
    if start is None:                         # no page numbers: everything after the cover
        start = 1 if len(pages) > 1 else 0
    end = len(pages)
    for k in range(start + 1, len(pages)):
        lab, ls = pages[k]
        first = next((x.strip() for x in ls if x.strip()), "")
        if re.fullmatch(r"\d+a", lab) or re.fullmatch(r"APPENDIX(?:\s+[A-Z])?", first):
            end = k
            break
    front = pages[1:start] if start > 0 else []
    qp = [p for p in front if QP_RE.match(next((x for x in p[1] if x.strip()), ""))]
    return {"cover": "\n".join(pages[0][1]) if pages else "", "front": front, "qp": qp,
            "body": pages[start:end] + (pages[end:] if appendix else []), "toc": read_toc(front),
            "appendix_pages": len(pages) - end, "main": end - start}


def segment_blocks(lines, labels, prev_pi, demote=True):
    """recap_blocks on one stretch between headings, with a page marker if it starts a new page.
    demote: the table of contents gives the headings, so a heading the layout reader guessed
    becomes a bold line ("A." + its title) instead of an outline entry."""
    if not lines:
        return []
    blocks = c.recap_blocks(lines, labels)
    if demote:
        out, lab = [], None
        for b in blocks:
            if b["kind"] == "h":
                lab = b["label"]
                continue
            if lab is not None:
                if b["kind"] == "title":
                    b = dict(b, kind="head", pieces=[(lab + ". ", b["pieces"][0][1])] + b["pieces"])
                else:
                    out.append({"kind": "head", "pieces": [(lab + ".", 0)]})
                lab = None
            out.append(b)
        blocks = out
    if blocks and lines[0].pi != prev_pi:
        blocks[0]["pieces"].insert(0, c.El("page-number", {"label": labels[lines[0].pi]}))
    return blocks


def read_brief(text, f, oi, warn, appendix=False):
    """One brief -> the opinion dict render_md / render_tex print."""
    sp = split_brief(text, appendix)
    keep = sp["qp"] + sp["body"]
    mylabels = [lab or str(k + 1) for k, (lab, ls) in enumerate(keep)]
    lines, footnotes, _ = c.layout_pages("\f".join("\n".join(ls) for lab, ls in keep))
    labels = mylabels
    nqp = len(sp["qp"])
    # headings: "Questions Presented" first, then the table of contents, found in order
    heads = []                                  # (i, j, entry)
    if nqp:
        k = next((k for k, l in enumerate(lines) if l.pi < nqp and QP_RE.match(l.text)), None)
        if k is not None:
            heads.append((k, k + 1, {"label": "", "text": "Questions Presented", "level": 1, "page": ""}))
    pos = next((k for k, l in enumerate(lines) if l.pi >= nqp), len(lines))
    missed = []
    for e in sp["toc"]:
        if e["page"] and not re.fullmatch(r"\d+", e["page"]):
            continue                            # front matter (roman pages) and appendices; "Statement:" (no page) is looked for
        r = find_heading(lines, labels, pos, e)
        if r is None:
            missed.append(e)
            continue
        heads.append((r[0], r[1], e))
        pos = r[1]
    for e in missed:
        warn("heading not found in the text: %s%s (p. %s)" % (e["label"] + ". " if e["label"] else "", e["text"][:60], e["page"]))
    o = {"oi": oi, "kind": "", "author": "", "byline": [], "flat": [], "footnotes": {}, "api": f,
         "start": labels[lines[0].pi] if lines else "1", "title": f["title"], "who": f["who"]}
    blocks, levels, at, prev_pi = [], [], 0, None
    heading_marks = []                          # (index of the first block after a heading, footnote label)
    path = []
    for i, j, e in heads:
        blocks += segment_blocks(lines[at:i], labels, prev_pi)
        if at < i:
            prev_pi = lines[i - 1].pi
        lab = e["label"] or c.titlecase(e["text"])
        path = path[:e["level"] - 1]
        short = e["label"] or " ".join(c.titlecase(e["text"]).split()[:3]).rstrip(",;:")   # "Summary of Argument"
        path += [""] * (e["level"] - 1 - len(path)) + [short]
        blocks.append({"kind": "h", "label": lab, "pieces": [], "level": e["level"], "path": "-".join(p for p in path if p)})
        tail = re.search(r"(?:\s|(?<=[A-Za-z’'”]))(\d{1,2})\s*$", lines[j - 1].text)   # "INTEREST OF AMICI CURIAE 1", or "CURIAE1"
        if tail and any(f[0] == tail.group(1) for f in footnotes) and not norm(e["text"]).endswith(tail.group(1)):
            heading_marks.append((len(blocks), tail.group(1)))
        if lines[i].pi != prev_pi:              # the page turns before the heading: mark it at the end of what precedes
            prev = next((b for b in reversed(blocks[:-1]) if b["kind"] in ("p", "q", "head", "title")), None)
            if prev is not None:
                prev["pieces"].append(c.El("page-number", {"label": labels[lines[i].pi]}))
            else:
                blocks[-1]["page"] = labels[lines[i].pi]
        prev_pi = lines[j - 1].pi
        if e["label"]:
            blocks.append({"kind": "title", "pieces": [(c.titlecase(e["text"]), lines[i].pi)]})
        at = j
    blocks += segment_blocks(lines[at:], labels, prev_pi)
    taken = set()
    for k, lab in heading_marks:                # a heading's own footnote: mark it at the start of the text below
        b = next((b for b in blocks[k:] if b["kind"] in ("p", "q")), None)
        if b is not None:
            mark = c.El("footnotemark")
            mark.kids.append(lab)
            at_ = next((n for n, p in enumerate(b["pieces"]) if isinstance(p, tuple)), 0)
            b["pieces"].insert(at_, mark)
            taken.add(lab)
    c.place_marks(blocks, [f for f in footnotes if f[0] not in taken], warn, spaced=True)   # "Overview. 2 Affected"
    fill = []
    if not heads:                               # no table of contents: the layout reader's own headings
        blocks = segment_blocks(lines, labels, None, demote=False)
        guessed = c.assign_levels([b["label"] for b in blocks if b["kind"] == "h"])
        for b, (lv, pth) in zip([b for b in blocks if b["kind"] == "h"], guessed):
            b["level"], b["path"] = lv, pth
    for b in blocks:
        if b["kind"] == "h":
            levels.append((b["level"], b["path"]))
            if b.get("page"):                   # the heading starts a page: keep the page marker
                fill.append({"kind": "p0", "page": b["page"]})
        fill.append(b)
    pending_page = None
    for b in fill:
        if b["kind"] == "p0":
            pending_page = b["page"]
            continue
        if pending_page and b["kind"] not in ("h",):
            b["pieces"].insert(0, c.El("page-number", {"label": pending_page}))
            pending_page = None
    c.fill_opinion(o, [b for b in fill if b["kind"] != "p0"], footnotes)
    o["levels"] = levels
    o["sections"] = len(heads)
    o["side_cover"] = classify_side(sp["cover"])
    o["appendix_pages"] = sp["appendix_pages"]
    o["body_text"] = "\n".join(l.text for l in lines) + "\n" + "\n".join(
        c.join_lines(p) for _, _, paras in footnotes for p in paras)
    # what the filing itself cites: not its appendix (the lower courts' opinions, the record), even when --appendix prints it
    main = nqp + sp["main"]
    o["cite_text"] = "\n".join(l.text for l in lines if l.pi < main) + "\n" + "\n".join(
        c.join_lines(p) for _, pi, paras in footnotes if pi < main for p in paras)
    return o



# ================================================================ authorities (citations in the filings)
# Reporter citations, found with regular expressions and resolved to CourtListener clusters
# through the local database's citations table. "605 U.S. at 181" counts for the case cited in
# full earlier in the same filing; a parallel citation next to its main one is the same mention.

REPORTERS = ["U.S.", "S. Ct.", "L. Ed. 2d", "L. Ed.", "F.4th", "F.3d", "F.2d", "F.", "F. Supp. 3d", "F. Supp. 2d",
             "F. Supp.", "F. App'x", "Fed. Cl.", "Ct. Cl.", "Cl. Ct.", "B.R.", "F.R.D.", "M.J.", "Vet. App.",
             "P.3d", "P.2d", "P.", "A.3d", "A.2d", "A.", "N.E.3d", "N.E.2d", "N.E.", "N.W.2d", "N.W.", "S.E.2d", "S.E.",
             "S.W.3d", "S.W.2d", "S.W.", "So. 3d", "So. 2d", "So.", "Cal. Rptr. 3d", "Cal. Rptr. 2d", "Cal. 5th",
             "Cal. 4th", "N.Y.3d", "N.Y.2d", "Wash. 2d", "Haw."]


def _rep_rx(r):
    r"""'F. Supp. 2d' -> F\.\s?Supp\.\s?2d: a space allowed after every period ("F. 4th" and "F.4th")."""
    out, s = "", r.replace(" ", "")
    for k, ch in enumerate(s):
        out += {".": r"\.", "'": "['’]"}.get(ch, re.escape(ch))
        if ch == "." and k + 1 < len(s):
            out += r"\s?"
    return out


REP_RX = "|".join(_rep_rx(r) for r in sorted(REPORTERS, key=len, reverse=True))
CITE_RE = re.compile(r"(?<![\d.])(\d{1,4})\s+(%s)\s+(at\s+)?(\d{1,5})(?![\d])" % REP_RX)
CANON = {re.sub(r"[\s.'’]", "", r).lower(): r for r in REPORTERS}


def canon(rep):
    return CANON.get(re.sub(r"[\s.'’]", "", rep).lower(), rep)


def find_cites(text):
    """-> [(position, volume, reporter, page, short)] in order; short = an 'at' pin cite."""
    t = " ".join(text.split())
    return t, [(m.start(), m.group(1), canon(m.group(2)), m.group(4), bool(m.group(3))) for m in CITE_RE.finditer(t)]


_RESOLVED = {}


def resolve(con, vol, rep, page, before=""):
    """One cluster for a citation. Several (CourtListener keeps two digitizations of many cases;
    a memorandum page holds many orders): the one whose name shares most words with the text
    just before the citation, then the most cited."""
    key = (vol, rep, page)
    if key not in _RESOLVED:
        rows = con.execute("SELECT DISTINCT c.id, c.case_name, c.citation_count FROM cites t JOIN clusters c ON c.id = t.cluster_id "
                           "WHERE t.volume=? AND t.reporter=? AND t.page=?", key).fetchall()
        _RESOLVED[key] = rows
    rows = _RESOLVED[key]
    if len(rows) <= 1:
        return rows[0][0] if rows else None
    near = set(re.findall(r"[a-z]{4,}", before.lower()))
    def score(r):
        words = set(re.findall(r"[a-z]{4,}", (r[1] or "").lower())) - {"united", "states", "america", "company", "corporation"}
        return (len(words & near), r[2] or 0)
    best = max(rows, key=score)
    return best[0] if score(best)[0] or len({r[1] for r in rows}) == 1 else (
        best[0] if len(rows) == 2 and SequenceMatcher(None, rows[0][1] or "", rows[1][1] or "").ratio() > 0.6 else None)


def authorities(con, text):
    """-> (Counter cluster id -> mentions, Counter unresolved 'vol rep page' -> mentions)."""
    t, found = find_cites(text)
    got, miss, by_vr, last = Counter(), Counter(), {}, (None, -999)
    for pos, vol, rep, page, short in found:
        if short:
            cid = by_vr.get((vol, rep))
            if cid:
                got[cid] += 1
                last = (cid, pos)
            continue
        cid = resolve(con, vol, rep, page, t[max(0, pos - 160):pos]) if con is not None else None
        if cid is None:
            miss["%s %s %s" % (vol, rep, page)] += 1
            continue
        by_vr[(vol, rep)] = cid
        if cid == last[0] and pos - last[1] < 90:       # "605 U.S. 168, 145 S. Ct. 1497": one mention
            last = (cid, pos)
            continue
        got[cid] += 1
        last = (cid, pos)
    return got, miss


def canonical(con, cids):
    """Duplicate clusters (same decision date, same name once squashed) -> the most-cited one."""
    if con is None:
        return {}
    groups = {}
    for k in cids:
        r = con.execute("SELECT date_filed, case_name, case_name_short, citation_count FROM clusters WHERE id=?", (k,)).fetchone()
        if not r:
            continue
        generic = {"united", "states", "city", "the", "and", "inc", "corp", "company", "state", "people", "county", "dept", "department"}
        words = [w for w in re.findall(r"[a-z]{3,}", (r[1] or r[2] or "").lower()) if w not in generic]
        key = (r[0], " ".join(words[:2]) or str(k))
        groups.setdefault(key, []).append((r[3] or 0, k))
    out = {}
    for g in groups.values():
        best = max(g)[1]
        for _, k in g:
            out[k] = best
    return out


def case_names(con, cids):
    """cluster id -> {name, cite, court, year}."""
    out = {}
    for cid in cids:
        r = con.execute("SELECT c.case_name_short, c.case_name, c.date_filed, d.court_id FROM clusters c "
                        "LEFT JOIN dockets d ON d.id = c.docket_id WHERE c.id=?", (cid,)).fetchone() if con else None
        if not r:
            out[cid] = {"name": "cluster %s" % cid, "cite": "", "court": "", "year": ""}
            continue
        cs = con.execute("SELECT volume, reporter, page FROM cites WHERE cluster_id=? ORDER BY type, reporter", (cid,)).fetchall()
        main = next((x for x in cs if x[1] == "U.S."), cs[0] if cs else None)
        out[cid] = {"name": r[1] or r[0] or "case", "short": r[0] or r[1] or "case", "cite": "%s %s %s" % main if main else "",
                    "court": c.court_cite(r[3], con), "year": (r[2] or "")[:4]}
    return out


def area_for(db, ref):
    """cocite.py's area for a case (newest complete slice, its sub-cluster's lineage), or None."""
    try:
        import time
        from tribonian import cocite
        rows = [r for r in (cocite.summary(ref, db, 8) or []) if r["home"]]
        if not rows:
            return None
        year = time.localtime().tm_year
        now = ([r for r in rows if r["slice"] + cocite.WIDTH - 1 < year] or rows)[-1]
        sub = now["sub"]["code"] if now["sub"] else None
        name, lin, _ = (cocite.unit_name(db, now["slice"], sub, "sub") if sub and sub % 1000
                        else cocite.unit_name(db, now["slice"], now["home"]))
        ar = cocite.area_of(db, lin)
        return (ar, cocite.area_name(db, ar)) if ar else None
    except (sqlite3.Error, SystemExit, KeyError, TypeError):   # view not built, or the case is in no slice
        return None


# ================================================================ build

ABBR = {("petition", "cert"): "Pet.", ("opposition", "cert"): "Br. in Opp.", ("reply", "cert"): "Cert. Reply",
        ("brief", "petitioner"): "Pet'rs Br.", ("brief", "respondent"): "Resp. Br.", ("reply", "merits"): "Reply Br.",
        ("reply", "after argument"): "Reply Br."}
TITLES = {"petition": "Petition for a Writ of Certiorari", "opposition": "Brief in Opposition",
          "reply": "Reply Brief", "brief": "Brief on the Merits", "supplemental": "Supplemental Brief",
          "amicus": "Brief Amicus Curiae"}
STAGES = [("below", "Decision Below"), ("cert", "Certiorari Stage"), ("merits", "Merits Stage"),
          ("after argument", "After Argument")]
CIRCUITS = {"first": "ca1", "second": "ca2", "third": "ca3", "fourth": "ca4", "fifth": "ca5", "sixth": "ca6",
            "seventh": "ca7", "eighth": "ca8", "ninth": "ca9", "tenth": "ca10", "eleventh": "ca11",
            "district of columbia": "cadc", "federal": "cafc"}


def party(title):
    """'Department of the Air Force, et al., Petitioners' -> 'Department of the Air Force'."""
    t = re.split(r",\s*(?:et al\.?|fka|aka|petitioners?|respondents?)\b", title or "", flags=re.I)[0]
    return t.strip(" ,") or "?"


def short_filer(filer):
    t = re.sub(r"^(?:the)\s+", "", filer or "", flags=re.I)
    t = re.split(r",?\s+et al\b|,", t)[0]
    words = t.split()
    while len(words) > 4:
        words = words[:4]
    while words and words[-1].lower() in c.SMALL:
        words.pop()
    return " ".join(words) or "amicus"


def filings(data, con):
    """The briefs (not motions, letters or the joint appendix), read and with their authorities."""
    out, feed = [], data["feed"]
    pet, resp = party(feed.get("PetitionerTitle")), party(feed.get("RespondentTitle"))
    for e in data["entries"]:
        if e["kind"] in ("other", "joint-appendix"):
            continue
        link = next((l for l in e["links"] if l.get("txt") and not SKIP_LINK.search(l["desc"])), None)
        if not link:
            continue
        f = dict(e, link=link, brief=(c.ROOT / link["txt"]).read_text(encoding="utf-8", errors="replace"))
        if e["kind"] == "amicus":
            name = short_filer(e["filer"])
            f["abbr"], f["who"] = "%s Amicus Br." % name, name
        else:
            f["abbr"] = ABBR.get((e["kind"], e["stage"])) or ABBR.get((e["kind"], e["side"])) or e["kind"].title()
            f["who"] = pet if e["side"] == "petitioner" else resp if e["side"] == "respondent" else e["filer"] or "?"
        f["title"] = TITLES.get(e["kind"], e["kind"].title())
        out.append(f)
    return out


def below_cluster(data, con):
    """The decision under review: cached in the case file, else the local database by docket number and date."""
    if data.get("below_cluster"):
        return data["below_cluster"]
    feed = data["feed"]
    num = (feed.get("LowerCourtCaseNumbers") or "").strip("() ").split(",")[0].strip()
    m = re.search(r"for the (.+?) Circuit", feed.get("LowerCourt") or "", re.I)
    court = CIRCUITS.get(m.group(1).lower()) if m else None
    day = when(feed.get("LowerCourtDecision"))
    if not num or con is None:
        return None
    q = "SELECT c.id, c.date_filed FROM dockets d JOIN clusters c ON c.docket_id = d.id WHERE d.docket_number=?"
    rows = con.execute(q + (" AND d.court_id=?" if court else ""), (num, court) if court else (num,)).fetchall()
    rows = [r for r in rows if r[1] == day] or rows
    if not rows:
        return None
    data["below_cluster"] = rows[0][0]
    case_file(data["number"]).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return rows[0][0]


def below_case(cid, con, index):
    """{cluster, docket, opinions} for the decision below: a fetched file if there is one, else the database."""
    ent = index.get(str(cid)) or {}
    if ent.get("full") and (c.CASES / ent["full"]).exists():
        return json.loads((c.CASES / ent["full"]).read_text(encoding="utf-8"))
    from tribonian import casemap
    return casemap.db_case(con, cid, True) if con is not None else None


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def group_sets(fs, setof, below=None):
    """The summary table's rows: each party filing, then the amici as one group per stage and side,
    then the decision below. -> ([(label, set, briefs)], matrix of shared ÷ either)"""
    groups = [(f["abbr"], setof(f), 1) for f in fs if f["kind"] != "amicus"]
    for stage, label in STAGES[1:]:
        for side in ("petitioner", "respondent", "neither"):
            am = [setof(f) for f in fs if f["kind"] == "amicus" and f["stage"] == stage and (f["side"] or "neither") == side]
            if am:
                groups.append(("%s amici, %s" % (label.replace(" Stage", "").replace("Certiorari", "Cert."),
                                                 {"petitioner": "pet.", "respondent": "resp.", "neither": "neither"}[side]),
                               set().union(*am), len(am)))
    if below:
        groups.append(("Below", set(below), 1))
    return groups, [[jaccard(a, b) for _, b, _ in groups] for _, a, _ in groups]


def side_split(fs, cntof, tie=lambda k: 0):
    """Each side's counts, its parties' filings and its amici apart. cntof(f) -> Counter for one filing.
    P/R (and nP/nR, filings) are the side totals; Pp/Pa/Rp/Ra the party and amici parts (nPp... filings).
    Bands: both = cited by both *parties*; pet / resp = by that side's party and not the other party
    (whatever the other side's amici do); amici = by no party at all.
    Neutral amici ("neither") count on no side, as before."""
    d = {k: Counter() for k in ("P", "R", "nP", "nR", "Pp", "Pa", "Rp", "Ra", "nPp", "nPa", "nRp", "nRa")}
    for f in fs:
        s = {"petitioner": "P", "respondent": "R"}.get(f["side"])
        if not s:
            continue
        cnt, part = cntof(f), "a" if f["kind"] == "amicus" else "p"
        d[s].update(cnt); d["n" + s].update(set(cnt))
        d[s + part].update(cnt); d["n" + s + part].update(set(cnt))
    P, R, Pp, Rp = d["P"], d["R"], d["Pp"], d["Rp"]
    d["contested"] = sorted(set(Pp) & set(Rp), key=lambda k: (-min(Pp[k], Rp[k]), -(P[k] + R[k]), tie(k)))
    d["only_p"] = sorted(set(Pp) - set(Rp), key=lambda k: (-Pp[k], -P[k], tie(k)))
    d["only_r"] = sorted(set(Rp) - set(Pp), key=lambda k: (-Rp[k], -R[k], tie(k)))
    d["amici_only"] = sorted((set(P) | set(R)) - set(Pp) - set(Rp), key=lambda k: (-(P[k] + R[k]), tie(k)))
    d["sides_both"] = len(set(P) & set(R))             # the old measure: both sides, amici included
    return d


def split_cells(d, k, dash="--"):
    """The four count cells (pet. party, pet. amici, resp. party, resp. amici) and the filings cell."""
    cells = [str(d[x][k] or dash) for x in ("Pp", "Pa", "Rp", "Ra")]
    return cells, "%d+%d/%d+%d" % (d["nPp"][k], d["nPa"][k], d["nRp"][k], d["nRa"][k])


def coupling(fs, below_cites, con):
    """The citation sets and what they share. fs: filings with ['cites'] (Counter), ['side'], ['kind']."""
    d = side_split(fs, lambda f: f["cites"])
    below = set(below_cites)

    def jac(a, b):
        return len(a & b) / len(a | b) if a | b else 0.0
    sets = [(f["abbr"], set(f["cites"])) for f in fs] + ([("Below", below)] if below else [])
    matrix = [[jac(a, b) for _, b in sets] for _, a in sets]
    engage = [(f["abbr"], len(set(f["cites"]) & below), len(f["cites"])) for f in fs]
    groups, gmatrix = group_sets(fs, lambda f: set(f["cites"]), below)
    return dict(d, groups=groups, gmatrix=gmatrix, below=below, sets=[s for s, _ in sets], matrix=matrix, engage=engage)


PROV_RE = re.compile(r"PROVISIONS?\s+INVOLVED\s*\n")
PROV_END_RE = re.compile(r"\n\s*(?:STATEMENT|INTRODUCTION|SUMMARY|ARGUMENT|REASONS|JURISDICTION|OPINIONS?\s+BELOW)\b[A-Z ,]*\n")


def provisions_involved(text):
    """The text of a brief's "Constitutional, Statutory, and Regulatory Provisions Involved" section, or ''."""
    m = PROV_RE.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    e = PROV_END_RE.search(rest)
    return rest[:e.start() if e else 3000]


def law_analysis(fs, qp):
    """The statutes and constitutional provisions the filings cite, compared the way the cases are.
    Keys at section level (lawcites.section_of); mentions by name ("the First Amendment") kept apart:
    nearly every filing names the clause in issue, so they would make every pair look alike."""
    sec = lambda f: {lawcites.section_of(k) for k in f["laws"]["cites"]}

    def per_section(cnt):
        out = Counter()
        for k, n in cnt.items():
            out[lawcites.section_of(k)] += n
        return out
    d = side_split(fs, lambda f: per_section(f["laws"]["cites"]), lawcites.sort_key)
    P, R = d["P"], d["R"]
    nameP, nameR = Counter(), Counter()
    for f in fs:
        names = per_section(f["laws"]["named"])
        if f["side"] == "petitioner":
            nameP.update(names)
        elif f["side"] == "respondent":
            nameR.update(names)
    # the law in question: what the Clerk's questions and the parties' provisions-involved sections name
    src = " ".join(qp or [])
    etseq = set()
    for f in fs:
        if f["kind"] != "amicus":
            src += "\n" + provisions_involved(f["o"]["cite_text"])
    q = lawcites.find(src)
    named_q = set(q["cites"]) | set(q["named"])
    every = set(P) | set(R) | {lawcites.section_of(k) for f in fs for k in f["laws"]["cites"]}
    liq = sorted({k for k in every if any(lawcites.covers(n, k, q["etseq"]) for n in named_q)} |
                 {lawcites.section_of(n) for n in named_q}, key=lawcites.sort_key)
    groups, gmatrix = group_sets(fs, sec)
    return dict(d, nameP=nameP, nameR=nameR, liq=liq, groups=groups, gmatrix=gmatrix)


REPRO_RE = re.compile(r"(Pet\.?\s*)?App\.?\s*(\d+a)\s*[-–—]\s*(\d+a)")


def law_sources(fs):
    """Each party filing's provisions-involved section, and where a filing says it reproduces a
    provision ("reproduced in relevant part at App.187a-208a"). -> ([(abbr, text)], {section key: [where]})"""
    secs, repro = [], {}
    for f in fs:
        if f["kind"] == "amicus":
            continue
        t = provisions_involved(f["o"]["cite_text"])
        if not t.strip():
            continue
        secs.append((f["abbr"], t))
        flat, at = lawcites.flatten(t), 0
        for m in REPRO_RE.finditer(flat):
            # "Colo. Rev. Stat. §§ 26.5-4-201 et seq. is reproduced ... at App.187a-208a": the last cite before the
            # page range, with "reproduced" between them, is what the range holds
            got = lawcites.find(flat[at:m.start()])
            between = lambda sp: flat[at + sp[1]:m.start()]
            last = [sp for sp in got["spans"] if re.search(r"(?i)repro-?\s?duc|set out|appears", between(sp))
                    and not any(o[0] > sp[1] for o in got["spans"])]
            where = "Pet. App. %s–%s" % (m.group(2), m.group(3)) if m.group(1) or f["kind"] == "petition" else \
                "%s App. %s–%s" % (f["abbr"].rstrip("."), m.group(2), m.group(3))
            for _, _, k in last:
                repro.setdefault(k, (set(), []))
                repro[k][1].append(where)
                if k in got["etseq"]:
                    repro[k][0].add(k)
            at = m.end()
    return secs, repro


def repro_for(repro, key):
    """Where the filings reproduce a provision: its own entry, or a part it falls in ("§§ 26.5-4-201 et seq.")."""
    out = []
    for k, (etseq, where) in repro.items():
        if lawcites.covers(k, key, etseq):
            out += where
    return list(dict.fromkeys(out))


def law_anchor(key):
    return "law-" + re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")


def law_paras(text):
    """A pdftotext section -> paragraphs: a new one where a line starts indented."""
    out, cur = [], []
    for line in text.split("\n"):
        if not line.strip():
            continue
        if cur and re.match(r"\s{3,}\S", line):
            out.append(c.join_lines(cur))
            cur = []
        cur.append(line.strip())
    if cur:
        out.append(c.join_lines(cur))
    return out


def law_appendix_tex(lc, fs):
    """The last part of the PDF: the law in question, as the parties set it out and, where it is
    stored locally, its text. Every cite of these provisions in the filings links here."""
    from tribonian import lawtext
    if not lc["liq"]:
        return ""
    E = lambda t: c.esc(t, "tex")
    secs, repro = law_sources(fs)
    out = ["\\clearpage\\noindent\\tocback\\tocadd{tocstage}{The Law in Question}\\pdfbookmark[0]{The Law in Question}{lawq}"
           "{\\Large\\bfseries The Law in Question\\par}\\vspace{4pt}",
           "{\\scriptsize\\itshape Not part of the record. The provisions the questions presented and the parties' "
           "provisions-involved sections name: first as each party sets them out, then each provision's text where it is "
           "stored locally (the US Code from the Code's XML; the Constitution, C.F.R. and state law once their local copies "
           "are built). A cite of one of these provisions anywhere in this PDF links to its entry here.\\par}\\vspace{6pt}"]
    for n, (abbr, t) in enumerate(secs):
        out.append("\\pdfbookmark[1]{%s: provisions involved}{lawq-src%d}\\noindent{\\large\\bfseries %s: provisions involved\\par}\\vspace{2pt}" % (E(abbr), n, E(abbr)))
        out += ["{\\small %s\\par}\\vspace{2pt}" % E(p) for p in law_paras(t)]
        out.append("\\vspace{6pt}")
    for k in lc["liq"]:
        lab = lawcites.label(k)
        out.append("\\par\\vspace{6pt}\\hypertarget{%s}{}\\pdfbookmark[1]{%s}{%s}\\noindent{\\large\\bfseries %s\\par}\\vspace{2pt}" % (
            law_anchor(k), E(lab), law_anchor(k), E(lab)))
        out.append("{\\footnotesize Cited by the petitioners %d times (%d filings) and their amici %d (%d); by the respondent %d (%d) "
                   "and its amici %d (%d).%s\\par}\\vspace{2pt}" % (
            lc["Pp"][k], lc["nPp"][k], lc["Pa"][k], lc["nPa"][k], lc["Rp"][k], lc["nRp"][k], lc["Ra"][k], lc["nRa"][k],
            " Reproduced in: %s." % E("; ".join(repro_for(repro, k))) if repro_for(repro, k) else ""))
        got = lawtext.text_of(k)
        if got is None:
            hint = lawtext.fetch_hint(k)
            out.append("{\\small\\itshape Text not stored locally%s.\\par}" % (
                " (\\texttt{%s} gets it)" % E(hint) if hint.startswith("tribonian ") else ""))
            continue
        head, rows = got
        if head:
            out.append("{\\small\\bfseries %s\\par}" % head)
        for depth, label, heading, text in rows:
            out.append("{\\small\\leftskip=%.1fem %s%s%s\\par}" % (
                1.2 * depth, "\\textbf{%s} " % label if label else "", "\\emph{%s.} " % heading if heading else "", text))
    return "\n".join(out) + "\n"


def law_appendix_md(lc, fs, statutes):
    if not lc["liq"]:
        return []
    E = lambda t: c.esc(t, "md")
    secs, repro = law_sources(fs)
    L = ["## The law in question", "",
         "*The provisions the questions presented and the parties' provisions-involved sections name. The PDF ends with "
         "their text where it is stored locally.*", ""]
    for k in lc["liq"]:
        note = lawcites.note_name(k)
        lab = E(lawcites.label(k))
        L.append("- %s — cited %d× by the petitioners (%d× their amici), %d× by the respondent (%d× its amici)%s" % (
            "[[%s|%s]]" % (note, lab) if note in statutes else lab, lc["Pp"][k], lc["Pa"][k], lc["Rp"][k], lc["Ra"][k],
            "; reproduced in %s" % E("; ".join(repro_for(repro, k))) if repro_for(repro, k) else ""))
    for abbr, t in secs:
        L += ["", "### %s: provisions involved" % E(abbr), ""] + ["> " + E(p) + "\n>" for p in law_paras(t)]
    return L + [""]


def law_rows(lc):
    """Every provision once: the law in question first, then both sides, each side alone. -> [(key, group)]"""
    seen, out = set(), []
    for g, keys in (("liq", lc["liq"]), ("both", lc["contested"]), ("pet", lc["only_p"]), ("resp", lc["only_r"]),
                    ("amici", lc["amici_only"])):
        for k in keys:
            if k not in seen:
                seen.add(k)
                out.append((k, g))
    return out


BAND = {"both": "Cited by both parties", "pet": "The petitioners, not the respondent",
        "resp": "The respondent, not the petitioners", "amici": "Amici only (no party cites it)"}
BAND_SHORT = {"liq": "in question", "both": "both", "pet": "pet.", "resp": "resp.", "amici": "amici"}
LAW_GROUP = dict(BAND, liq="The law in question (questions presented; provisions involved)")
SPLIT_NOTE = ("Pet./Resp.: mentions by the side's party filings and by its amici, apart; Filings = party+amici filings "
              "citing it, pet./resp. Bands follow the parties: a case one party cites and the other side reaches only "
              "through amici sits in the citing party's band.")


def split_head(before, after):
    """Two header rows: Pet. / Resp. over party / amici."""
    top = [""] * len(before) + ["\\multicolumn{2}{c}{Pet.}", "\\multicolumn{2}{c}{Resp.}"] + [""] * len(after)
    return " & ".join(top) + "\\\\\n" + " & ".join(before + ["party", "amici", "party", "amici"] + after)


def summary_lines(d, what, unit="citations"):
    """-> [(label, text)]: each side split party/amici, then the both-parties count."""
    out = []
    for side, s in (("petitioner", "P"), ("respondent", "R")):
        out.append((SIDE_NAME[side], "%d %s (party %d, amici %d), %d %s" % (
            len(d[s]), what, len(d[s + "p"]), len(d[s + "a"]), sum(d[s].values()), unit)))
    out.append(("Cited by both parties", "%d %s (both sides counting amici: %d); by amici only: %d" % (
        len(d["contested"]), what, d["sides_both"], len(d["amici_only"]))))
    return out


def law_tex(lc):
    E = lambda t: c.esc(t, "tex")
    if not law_rows(lc):
        return ""
    out = ["{\\scriptsize\\itshape Not part of the record. Statutes, regulations and constitutional provisions cited in each "
           "filing (its appendix left out), at section level; a short form (``\\S\\ 26.5-4-205'') takes the code of the last full "
           "cite. Named = mentions by name (``the First Amendment''), counted apart. Sides: the parties' filings plus the amici supporting each.\\par}\\vspace{3pt}"]
    out += ["\\textbf{%s:} %s.\\par" % (a, b) for a, b in summary_lines(lc, "provisions")]
    out.append("\\vspace{4pt}")
    if lc["liq"]:
        out.append("\\textbf{The law in question} {\\footnotesize (named in the questions presented or a party's provisions-involved section)}: "
                   "{\\footnotesize %s}\\par\\vspace{4pt}" % "; ".join(E(lawcites.label(k)) for k in lc["liq"]))

    def table(rows, head, spec):
        return ("{\\footnotesize\\setlength{\\tabcolsep}{3pt}\\begin{tabular}{%s}\n%s\\\\\\hline\n%s\\\\\n\\end{tabular}\\par}\\vspace{4pt}\n"
                % (spec, " & ".join(head), "\\\\\n".join(" & ".join(r) for r in rows)))
    out.append("\\textbf{Coupling between the filings} {\\footnotesize (shared provisions $\\div$ provisions cited by either of the two, "
               "\\%; amici grouped by stage and side, with the number of briefs; $n$ = provisions cited; appendices not counted)}\\par")
    out.append(couple_table(lc, E, table))
    rows, prev = [], None
    for i, (k, g) in enumerate(law_rows(lc), 1):
        if g != prev:
            rows.append("\\multicolumn{8}{@{}l}{\\rule{0pt}{2.4ex}\\textbf{%s}}" % LAW_GROUP[g])
            prev = g
        lab = E(lawcites.label(k))
        cells, fil = split_cells(lc, k)
        rows.append(" & ".join([str(i), "\\hyperlink{%s}{%s}" % (law_anchor(k), lab) if g == "liq" else lab] + cells + [fil,
                                "%d/%d" % (lc["nameP"][k], lc["nameR"][k]) if lc["nameP"][k] or lc["nameR"][k] else ""]))
    return ("\\pdfbookmark[1]{Statutes and provisions the filings cite}{laws}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!40!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n"
            "\\tocback\\tocadd{tocsec}{Statutes and provisions the filings cite}\\textbf{Statutes and provisions the filings cite.}\\par\\vspace{2pt}\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n"
            "\\clearpage\\pdfbookmark[1]{Every provision, by side}{provisions}\n"
            "\\noindent\\tocback\\tocadd{tocsec}{Every provision, by side}{\\large\\bfseries Every provision, by side\\par}\\vspace{2pt}\n"
            "{\\scriptsize\\itshape %s Named = mentions by name, pet./resp. (amici included).\\par}\\vspace{3pt}\n"
            "{\\footnotesize\\setlength{\\tabcolsep}{3pt}%s\\renewcommand{\\arraystretch}{1.2}\n"
            "\\begin{longtable}{@{}r p{7.4cm} r r r r r r@{}}\n"
            "%s\\\\\\hline\\endhead\n%s\\\\\\hline\n\\end{longtable}}\n") % (
        COUPLE_COLOR, COUPLE_COLOR.split("!")[0], "\n".join(out), E(SPLIT_NOTE), RULED,
        split_head(["\\#", "Provision"], ["Filings", "Named"]), "\\\\\\hline\n".join(rows))


def law_md(lc, statutes):
    E = lambda t: c.esc(t, "md")
    if not law_rows(lc):
        return []
    def link(k, table=False):
        if lawcites.note_name(k) not in statutes:
            return E(lawcites.label(k))
        return "[[%s%s|%s]]" % (lawcites.note_name(k), "\\" if table else "", E(lawcites.label(k)))
    L = ["## Statutes and provisions the filings cite", "",
         "*Not part of the record. Statutes, regulations and constitutional provisions cited in each filing (its appendix "
         "left out), at section level; a short form takes the code of the last full cite. Named = mentions by name "
         "(\"the First Amendment\"), counted apart. Sides: the parties' filings plus the amici supporting each.*", ""]
    L += ["- **%s:** %s" % ab for ab in summary_lines(lc, "provisions")] + [""]
    if lc["liq"]:
        L += ["**The law in question** (named in the questions presented or a party's provisions-involved section): " +
              "; ".join(link(k) for k in lc["liq"]), ""]
    L += ["### Coupling between filings (provisions)", "",
          "Shared provisions ÷ provisions cited by either of the two, in percent; amici grouped by stage and side; n = provisions cited.", ""]
    rows = couple_rows(lc)
    L += ["| | %s | n |" % " | ".join(r[0] for r in rows), "|---|%s--:|" % ("--:|" * len(rows))]
    for letter, lab, n, cells in rows:
        L.append("| %s %s | %s | %d |" % (letter, E(lab), " | ".join(cells), n))
    L += ["", "### Every provision, by side", "", SPLIT_NOTE, "",
          "| # | Provision | Group | Pet. party | Pet. amici | Resp. party | Resp. amici | Filings | Named |",
          "|--:|---|---|--:|--:|--:|--:|--:|--:|"]
    for i, (k, g) in enumerate(law_rows(lc), 1):
        cells, fil = split_cells(lc, k, "0")
        L.append("| %d | %s | %s | %s | %s | %s |" % (
            i, link(k, True), BAND_SHORT[g], " | ".join(cells), fil,
            "%d/%d" % (lc["nameP"][k], lc["nameR"][k]) if lc["nameP"][k] or lc["nameR"][k] else ""))
    return L + [""]


def area_profile(cp, names, db):
    """Mentions per cocite area, each side. -> [(area name, share P, share R, n P, n R)]."""
    if db is None:
        return []
    tot = {"P": Counter(), "R": Counter()}
    for side in ("P", "R"):
        for cid, n in cp[side].items():
            a = names[cid].get("area")
            tot[side][a[1] if a else "(no area)"] += n
    sp, sr = sum(tot["P"].values()) or 1, sum(tot["R"].values()) or 1
    keys = sorted(set(tot["P"]) | set(tot["R"]), key=lambda k: -(tot["P"][k] / sp + tot["R"][k] / sr))
    return [(k, tot["P"][k] / sp, tot["R"][k] / sr, tot["P"][k], tot["R"][k]) for k in keys]



# ---------------------------------------------------------------- the analysis, as Markdown and TeX

SIDE_NAME = {"petitioner": "Petitioners' side", "respondent": "Respondent's side"}
COUPLE_COLOR = "violet!8"
TOP = 25


def nm(names, cid, n=48):
    e = names.get(cid) or {}
    s = e.get("short") or e.get("name") or str(cid)
    s = s if len(s) <= n else s[:n - 1].rstrip() + "…"
    return "%s (%s)" % (s, e.get("year") or "?")


def cite_of(names, cid):
    """The citation to fetch it by ("tribonian case fetch --cite ..."), U.S. Reports first."""
    return (names.get(cid) or {}).get("cite") or ""


def all_rows(cp):
    """Every authority: both parties first (ranked by the lesser party's mentions), then each party
    without the other, then the cases only amici cite. -> [(cid, group)]."""
    return ([(k, "both") for k in cp["contested"]] + [(k, "pet") for k in cp["only_p"]] +
            [(k, "resp") for k in cp["only_r"]] + [(k, "amici") for k in cp["amici_only"]])


RULED = "\\setlength{\\arrayrulewidth}{0.2pt}"          # thin lines between rows, to follow a row across


def link_md(names, index, cid, n=60):
    e, label = index.get(str(cid)), c.esc(nm(names, cid, n), "md")
    return "[[%s\\|%s]]" % (e["slug"], label) if e else label


def couple_md(cp, names, index, fs, areas):
    E = lambda t: c.esc(t, "md")
    L = ["## Authorities the filings share", "",
         "*Not part of the record. Reporter citations in each filing (body and footnotes; tables, covers and appendices "
         "left out), resolved to CourtListener clusters through the local database by `scotus.py`; counts are mentions, "
         "short cites (\"605 U.S. at 181\") included. Sides: the parties' filings plus the amici supporting each, from "
         "the briefs' covers.*", ""]
    L += ["- **%s:** %s" % ab for ab in summary_lines(cp, "cases", "mentions")] + [""]
    L += ["### Every authority, by side", "",
          "Cited by both parties first (ranked by how much the less-reliant party leans on them), then the petitioners "
          "and not the respondent, then the respondent and not the petitioners, then amici only. " + SPLIT_NOTE +
          " Cite is what `tribonian case fetch --cite` takes.", "",
          "| # | Case | Cite | Group | Pet. party | Pet. amici | Resp. party | Resp. amici | Filings | Below | Area |",
          "|--:|---|---|---|--:|--:|--:|--:|--:|:-:|---|"]
    for i, (k, g) in enumerate(all_rows(cp), 1):
        a = (names[k].get("area") or (None, ""))[1] or ""
        cells, fil = split_cells(cp, k, "0")
        L.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
            i, link_md(names, index, k), cite_of(names, k), BAND_SHORT[g], " | ".join(cells), fil,
            "✓" if k in cp["below"] else "", E(a.replace("|", "/"))))      # before esc: it now writes \| itself
    if cp["below"]:
        L += ["", "### Engagement with the decision below", "",
              "How many of each filing's cases the court of appeals' opinion also cites (%d cases there)." % len(cp["below"]), "",
              "| Filing | Shared with below | Of its cases | Share |", "|---|--:|--:|--:|"]
        for abbr, k, n in cp["engage"]:
            L.append("| %s | %d | %d | %s |" % (E(abbr), k, n, "%d%%" % round(100 * k / n) if n else "–"))
    L += ["", "### Coupling between filings", "",
          "Shared cases ÷ cases cited by either of the two (Jaccard), in percent. Amici are grouped by stage and side, "
          "with the number of briefs in each group; n = cases cited (a group: all its briefs together). Appendices are not counted.", ""]
    rows = couple_rows(cp)
    L += ["| | %s | n |" % " | ".join(r[0] for r in rows), "|---|%s--:|" % ("--:|" * len(rows))]
    for letter, lab, n, cells in rows:
        L.append("| %s %s | %s | %d |" % (letter, E(lab), " | ".join(cells), n))
    if areas:
        L += ["", "### Areas each side draws on", "",
              "Mentions by the cited case's co-citation area (`cocite.py`, appellate view; area names written by Claude "
              "from each area's evidence).", "", "| Area | Pet. side | Resp. side |", "|---|--:|--:|"]
        for a, p, r, np_, nr in areas[:15]:
            L.append("| %s | %d%% (%d) | %d%% (%d) |" % (E(a.replace("|", "/")), round(100 * p), np_, round(100 * r), nr))
    return L + [""]


def couple_rows(cp):
    """The grouped coupling table: (letter, label, n, cells), one rule for every cell, the diagonal '·'."""
    g = cp["groups"]
    return [(chr(65 + a), lab + (" (%d)" % k if k > 1 or "amici" in lab else ""), len(st),
             ["·" if a == b else "%d" % round(100 * cp["gmatrix"][a][b]) for b in range(len(g))])
            for a, (lab, st, k) in enumerate(g)]


def couple_table(cp, E, table):
    rows = couple_rows(cp)
    body = [["%s %s" % (L, E(lab))] + [("$\\cdot$" if v == "·" else v) for v in cells] + ["%d" % n] for L, lab, n, cells in rows]
    return table(body, [""] + [L for L, _, _, _ in rows] + ["$n$"], "l" + "r" * len(rows) + "|r")


def couple_tex(cp, names, fs, areas):
    E = lambda t: c.esc(t, "tex")
    out = ["{\\scriptsize\\itshape Not part of the record. Reporter citations in each filing, resolved to CourtListener "
           "clusters through the local database (\\texttt{scotus.py}); counts are mentions. Sides: the parties' filings "
           "plus the amici supporting each.\\par}\\vspace{3pt}"]
    out += ["\\textbf{%s:} %s.\\par" % (a, b) for a, b in summary_lines(cp, "cases", "mentions")]
    out.append("\\vspace{4pt}")

    def table(rows, head, spec):
        return ("{\\footnotesize\\setlength{\\tabcolsep}{3pt}\\begin{tabular}{%s}\n%s\\\\\\hline\n%s\\\\\n\\end{tabular}\\par}\\vspace{4pt}\n"
                % (spec, " & ".join(head), "\\\\\n".join(" & ".join(r) for r in rows)))
    out.append("{\\footnotesize Every authority is listed by side in the table after this box (``Every authority, by side'').}\\par\\vspace{3pt}")
    if cp["below"]:
        out.append("\\textbf{Engagement with the decision below} {\\footnotesize (of each filing's cases, how many the "
                   "court of appeals also cites; %d there):} {\\footnotesize %s}\\par\\vspace{3pt}" % (
                       len(cp["below"]), "; ".join("%s %d/%d" % (E(a), k, n) for a, k, n in cp["engage"])))
    parties = [i for i, f in enumerate(fs) if f["kind"] != "amicus"] + ([len(fs)] if cp["below"] else [])
    out.append("\\textbf{Coupling between the filings} {\\footnotesize (shared cases $\\div$ cases cited by either of the two, "
               "\\%; amici grouped by stage and side, with the number of briefs; $n$ = cases cited; appendices not counted)}\\par")
    out.append(couple_table(cp, E, table))
    am = []
    for i, f in enumerate(fs):
        if f["kind"] != "amicus":
            continue
        best = max((j for j in parties if j != len(fs)), key=lambda j: cp["matrix"][i][j], default=None)
        if best is not None:
            am.append("%s (%s): closest to %s, %d\\%%" % (E(f["who"]), E(f["side"][:4] + "." if f["side"] else "?"),
                                                     E(cp["sets"][best]), round(100 * cp["matrix"][i][best])))
    if am:
        out.append("\\textbf{Amici:} {\\footnotesize %s}\\par\\vspace{3pt}" % "; ".join(am))
    if areas:
        rows = [[E(a[:58]), "%d\\%%" % round(100 * p), "%d\\%%" % round(100 * r)] for a, p, r, _, _ in areas[:12]]
        out.append("\\textbf{Areas each side draws on} {\\footnotesize (share of mentions by the cited case's co-citation area)}\\par")
        out.append(table(rows, ["Area", "Pet.", "Resp."], "p{7.4cm}rr"))
    return ("\\pdfbookmark[1]{Authorities the filings share}{coupling}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!40!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n"
            "\\tocback\\tocadd{tocsec}{Authorities the filings share}\\textbf{Authorities the filings share.}\\par\\vspace{2pt}\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        COUPLE_COLOR, COUPLE_COLOR.split("!")[0], "\n".join(out))


def authorities_tex(cp, names):
    """Every authority in one ruled longtable: both sides, then each side alone (the other side's
    count is 0). Runs across pages with the header repeated."""
    E = lambda t: c.esc(t, "tex")
    rows, prev = [], None
    for i, (k, g) in enumerate(all_rows(cp), 1):
        if g != prev:                            # a labelled band where the groups change
            rows.append("\\multicolumn{9}{@{}l}{\\rule{0pt}{2.4ex}\\textbf{%s}}" % BAND[g])
            prev = g
        cells, fil = split_cells(cp, k)
        rows.append(" & ".join([str(i), E(nm(names, k, 60)), E(cite_of(names, k))] + cells + [fil,
                                "x" if k in cp["below"] else ""]))
    if not rows:
        return ""
    head = split_head(["\\#", "Case", "Cite"], ["Filings", "Below"])
    return ("\\pdfbookmark[1]{Every authority, by side}{authorities}\n"
            "\\noindent\\tocback\\tocadd{tocsec}{Every authority, by side}{\\large\\bfseries Every authority, by side\\par}\\vspace{2pt}\n"
            "{\\scriptsize\\itshape Not part of the record. %s Below = cited in the court of appeals' opinion. "
            "Cite is what \\texttt{tribonian case fetch --cite} takes.\\par}\\vspace{3pt}\n"
            "{\\footnotesize\\setlength{\\tabcolsep}{3pt}%s\\renewcommand{\\arraystretch}{1.2}\n"
            "\\begin{longtable}{@{}r p{6.5cm} p{2.9cm} r r r r r c@{}}\n"
            "%s\\\\\\hline\\endhead\n%s\\\\\\hline\n\\end{longtable}}\n") % (
        c.esc(SPLIT_NOTE, "tex"), RULED, head, "\\\\\\hline\n".join(rows))


def docket_tex(data, qp_text):
    """The yellow box at the top: parties, the decision below, dates, the questions presented."""
    E = lambda t: c.esc(t, "tex")
    f = data["feed"]
    lines = ["{\\centering\\bfseries %s\\par}\\vspace{2pt}" % E("%s v. %s" % (party(f.get("PetitionerTitle")), party(f.get("RespondentTitle")))),
             "{\\centering No. %s \\textperiodcentered\\ docketed %s \\textperiodcentered\\ cert.\\ granted %s%s\\par}\\vspace{2pt}" % (
                 E(data["number"]), E(f.get("DocketedDate", "")), E(c.long_date(data.get("granted")) or "–"),
                 " \\textperiodcentered\\ argued %s" % E(c.long_date(data["argued"])) if data.get("argued") else ""),
             "%s, No.~%s, decided %s; rehearing denied %s.\\par" % (
                 E(f.get("LowerCourt", "")), E((f.get("LowerCourtCaseNumbers") or "").strip("() ")),
                 E(f.get("LowerCourtDecision", "")), E(f.get("LowerCourtRehearingDenied", "") or "–"))]
    if qp_text:
        lines.append("\\vspace{3pt}\\textbf{Questions presented} {\\footnotesize (the Clerk's statement)}\\par")
        lines += ["{\\small %s\\par}" % E(p) for p in qp_text]
    return ("\\tocadd{tocsec}{The case}\\pdfbookmark[1]{The case}{docket}\n"
            "\\begin{tcolorbox}[enhanced, breakable, colback=%s, colframe=%s!60!black, boxrule=0.4pt, arc=1.5pt,\n"
            "  left=5pt, right=5pt, top=4pt, bottom=4pt, fontupper=\\small]\n%s\n\\end{tcolorbox}\n\\vspace{6pt}\n") % (
        c.HEAD_COLOR, c.HEAD_COLOR.split("!")[0], "\n".join(lines))


def qp_paragraphs(data):
    """The Clerk's question-presented PDF -> paragraphs (the docket line and case name left out)."""
    p = data.get("qp", {}).get("txt")
    if not p or not (c.ROOT / p).exists():
        return []
    paras, cur = [], []
    for l in (c.ROOT / p).read_text(encoding="utf-8", errors="replace").split("\n"):
        t = " ".join(l.split())
        if not t:
            if cur:
                paras.append(c.join_lines(cur))
            cur = []
        elif re.match(r"(?i)^(decision below|lower court case number|\d{2}-\d+\s*$|questions? presented\s*$)", t):
            continue
        elif not re.match(r"(?i)^.* v\. .*$", t) or cur:
            cur.append(t)
    if cur:
        paras.append(c.join_lines(cur))
    return [x for x in paras if len(x) > 30]


# ---------------------------------------------------------------- good law, per side

def _goodlaw_one(cid):
    """In a worker process: casemap.goodlaw via case2md.profile_data, which caches it in
    cases/citedby/<cluster>/profile.json (the same file the case notes read)."""
    try:
        return cid, (c.profile_data(cid, scan=True) or {}).get("goodlaw")
    except Exception as e:
        return cid, {"error": str(e)}


def goodlaw_all(cids, workers=None):
    """cluster id -> casemap.goodlaw result. Cached ones are instant; the rest read citing opinions."""
    import os
    from concurrent.futures import ProcessPoolExecutor
    out, todo = {}, []
    for k in cids:
        p = c.CASES / "citedby" / str(k) / "profile.json"
        try:
            from tribonian import casemap
            g = json.loads(p.read_text(encoding="utf-8")).get("goodlaw") if p.exists() else None
            if g and g.get("version") == casemap.GOODLAW_VERSION:
                out[k] = g
                continue
        except (ValueError, OSError):
            pass
        todo.append(k)
    if todo:
        workers = workers or max(1, (os.cpu_count() or 2) // 2)
        print("good-law check: %d cases cached, %d to read (%d workers; cached afterwards)" % (len(out), len(todo), workers))
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for n, (k, g) in enumerate(ex.map(_goodlaw_one, todo), 1):
                out[k] = g
                if n % 10 == 0 or n == len(todo):
                    print("  %d of %d" % (n, len(todo)), flush=True)
    return out


def plaintiff_side(data, below_meta):
    """Which side brought the suit: the first-named party in the decision below's caption."""
    cap = (below_meta or {}).get("name") or ""
    first = re.split(r"\s+v\.?\s+", cap, 1)[0].lower()
    resp = party(data["feed"].get("RespondentTitle")).lower()
    pet = party(data["feed"].get("PetitionerTitle")).lower()
    words = lambda s: set(re.findall(r"[a-z]{4,}", s)) - {"united", "states", "department"}
    if words(resp) & words(first):
        return "respondent"
    if words(pet) & words(first):
        return "petitioner"
    return "respondent"


def gl_word(g):
    if not g or g.get("error"):
        return "unknown", "not checked"
    st = g.get("status")
    if st == "red":
        return st, "bad law? from %s" % (g.get("red_year") or "?")
    if st == "yellow":
        return st, "doubts from %s" % (g.get("yellow_year") or "?")
    return st, "good"


def status_columns(data, cp, names, gl, below_meta):
    """-> [(side, party name, role, [(cid, mentions)])], plaintiff first."""
    pl = plaintiff_side(data, below_meta)
    order = [pl, "petitioner" if pl == "respondent" else "respondent"]
    cols = []
    for side in order:
        cnt = cp["P"] if side == "petitioner" else cp["R"]
        name = party(data["feed"].get("PetitionerTitle" if side == "petitioner" else "RespondentTitle"))
        cols.append((side, name, "plaintiff" if side == pl else "defendant", cnt.most_common()))
    return cols


def status_parts(cols, amici_only):
    """-> (common [(cid, mentions left, mentions right)], left-only [(cid, n)], right-only [(cid, n)]).
    Common ones ranked by the lesser side's mentions; each side's own by its mentions."""
    left, right = dict(cols[0][3]), dict(cols[1][3])
    common = sorted((k for k in left if k in right), key=lambda k: (-min(left[k], right[k]), -(left[k] + right[k])))
    return ([(k, left[k], right[k]) for k in common], [(k, n) for k, n in cols[0][3] if k not in right],
            [(k, n) for k, n in cols[1][3] if k not in left])


def status_md(cols, names, index, gl, amici_only):
    E = lambda t: c.esc(t, "md")
    mark = lambda st: {"red": "🔴", "yellow": "🟡", "green": "🟢"}.get(st, "⚪")
    common, lonly, ronly = status_parts(cols, amici_only)
    (ls, ln, lr, _), (rs, rn, rr, _) = cols
    L = ["## Authorities and their status", "",
         "*Status from `casemap.py goodlaw` (rules %s): words in later opinions from courts that could overrule it; "
         "red is a lead to check, not a holding. Counts are each side's, amici included; † = cited only by amici, "
         "no party.*" % (next((g.get("version") for g in gl.values() if g), "?")), "",
         "### Common authorities", "", "| # | Case | Cite | Status | %s | %s |" % (E(ln), E(rn)), "|--:|---|---|---|--:|--:|"]
    for i, (k, a, b) in enumerate(common, 1):
        st, w = gl_word(gl.get(k))
        L.append("| %d | %s%s | %s | %s %s | %d | %d |" % (i, link_md(names, index, k, 60), " †" if k in amici_only else "",
                                                         cite_of(names, k), mark(st), w, a, b))
    L += ["", "### Each side's own", "",
          "| # | %s (%s, %s below) | Cite | # | %s (%s, %s below) | Cite |" % (E(ln), ls, lr, E(rn), rs, rr),
          "|--:|---|---|--:|---|---|"]
    def cell(lst, i):
        if i >= len(lst):
            return "", "", ""
        k, n = lst[i]
        st, w = gl_word(gl.get(k))
        return str(i + 1), "%s %s%s ×%d — %s" % (mark(st), link_md(names, index, k, 50), " †" if k in amici_only else "",
                                                 n, w), cite_of(names, k)
    for i in range(max(len(lonly), len(ronly))):
        L.append("| %s | %s | %s | %s | %s | %s |" % (cell(lonly, i) + cell(ronly, i)))
    return L + [""]


def status_tex(cols, names, gl, amici_only):
    """Common authorities first, one ruled row each (status, cite, each side's mentions); then each
    side's own in two numbered columns, plaintiff below on the left. Longtables, so they run across
    pages with the header repeated."""
    E = lambda t: c.esc(t, "tex")
    col = {"red": c.GL_COLORS["red"][1], "yellow": c.GL_COLORS["yellow"][1], "green": c.GL_COLORS["green"][1]}
    dot = lambda st: "\\textcolor{%s}{$\\bullet$}" % col[st] if st in col else "$\\circ$"
    common, lonly, ronly = status_parts(cols, amici_only)
    (ls, ln, lr, _), (rs, rn, rr, _) = cols
    out = ["\\clearpage\\noindent\\tocback\\tocadd{tocstage}{Authorities and their status}\\pdfbookmark[0]{Authorities and their status}{status}{\\Large\\bfseries Authorities and their status\\par}\\vspace{4pt}\n"
           "{\\scriptsize\\itshape Not part of the record. Status from \\texttt{casemap.py goodlaw}: words in later opinions from "
           "courts that could overrule it --- %s~good, %s~doubts, %s~likely overruled or superseded (a lead to check, with the "
           "year the words start). Counts are each side's, amici included; \\textdagger\\ = cited only by amici, no party.\\par}\\vspace{4pt}\n"
           % (dot("green"), dot("yellow"), dot("red"))]
    dag = lambda k: "\\,\\textdagger" if k in amici_only else ""
    if common:
        rows = []
        for i, (k, a, b) in enumerate(common, 1):
            st, w = gl_word(gl.get(k))
            rows.append("%d & %s %s & %s & {\\scriptsize\\itshape %s} & %d & %d" % (
                i, dot(st), E(nm(names, k, 62)) + dag(k), E(cite_of(names, k)), E(w), a, b))
        out.append("\\noindent\\tocback\\tocadd{tocsec}{Common authorities}\\pdfbookmark[1]{Common authorities}{status-common}{\\large\\bfseries Common authorities\\par}\\vspace{2pt}\n"
                   "{\\footnotesize\\setlength{\\tabcolsep}{3pt}%s\\renewcommand{\\arraystretch}{1.2}\n"
                   "\\begin{longtable}{@{}r p{7.4cm} p{3.0cm} p{2.4cm} r r@{}}\n"
                   "\\# & Case & Cite & Status & \\parbox[b]{1.2cm}{\\raggedleft %s} & \\parbox[b]{1.2cm}{\\raggedleft %s}\\\\\\hline\\endhead\n"
                   "%s\\\\\\hline\n\\end{longtable}}\n" % (RULED, E(ln[:18]), E(rn[:18]), "\\\\\\hline\n".join(rows)))

    def cell(lst, i):
        if i >= len(lst):
            return ""
        k, n = lst[i]
        st, w = gl_word(gl.get(k))
        cite = cite_of(names, k)
        return "%d.~%s %s~{\\scriptsize$\\times$%d}\\newline{\\scriptsize %s%s\\itshape %s}" % (
            i + 1, dot(st), E(nm(names, k, 44)) + dag(k), n, E(cite), " --- " if cite else "", E(w))
    rows = max(len(lonly), len(ronly))
    if rows:
        head = " & ".join("\\textbf{%s}\\newline{\\scriptsize %s side only, %s below}" % (E(n_), s_, r_)
                          for s_, n_, r_ in ((ls, ln, lr), (rs, rn, rr)))
        body = "\\\\\\hline\n".join("%s & %s" % (cell(lonly, i), cell(ronly, i)) for i in range(rows))
        out.append("\\noindent\\tocback\\tocadd{tocsec}{Each side's own}\\pdfbookmark[1]{Each side's own}{status-own}{\\large\\bfseries Each side's own\\par}\\vspace{2pt}\n"
                   "{\\small\\setlength{\\tabcolsep}{3pt}%s\\renewcommand{\\arraystretch}{1.15}\n"
                   "\\begin{longtable}{@{}p{0.49\\linewidth}|p{0.49\\linewidth}@{}}\n%s\\\\\\hline\\endhead\n%s\\\\\\hline\n\\end{longtable}}\n"
                   % (RULED, head, body))
    return "".join(out)


# ---------------------------------------------------------------- build

def case_slug(data):
    f = data["feed"]
    return "scotus-%s-%s" % (data["number"], c.slugify("%s v %s" % (party(f.get("PetitionerTitle")), party(f.get("RespondentTitle"))), 50))


def pending_dir(slug):
    """md-cases/Pending SCOTUS/, unless the note was moved: an existing note's place wins, as for every case note."""
    existing = {p.stem: p.parent for p in c.NOTES.rglob("%s*.md" % slug)} if c.NOTES.is_dir() else {}
    where = existing.get(slug) or c.NOTES / FOLDER
    where.mkdir(parents=True, exist_ok=True)
    return where


def build(args):
    num = norm_number(args.number)
    if not case_file(num).exists():
        sys.exit("No %s. Run tribonian scotus fetch %s first." % (case_file(num).relative_to(c.ROOT), num))
    data = json.loads(case_file(num).read_text(encoding="utf-8"))
    con = c.local_db()
    if con is None:
        print("note: no local database (casedb.py): authorities stay unresolved and the decision below is left out")
    index = c.load_index()
    links = {int(k): e["slug"] for k, e in index.items()}
    statutes = c.statute_notes()
    slug = case_slug(data)
    ndir = pending_dir(slug)
    warnings = []

    # ---- the filings
    fs = filings(data, con)
    for i, f in enumerate(fs):
        o = read_brief(f["brief"], f, i, lambda w, a=f["abbr"]: warnings.append("%s: %s" % (a, w)), args.appendix)
        if f["kind"] == "amicus":
            f["side"] = o["side_cover"] or f["side"]
        f["o"] = o
        f["cites"], f["miss"] = authorities(con, o["cite_text"])
        f["laws"] = lawcites.find(o["cite_text"])
        f["slug"] = "%s-%s-%s" % (slug, f["date"], c.slugify(f["abbr"], 30))

    # ---- the decision below
    below_ops, below_cites, below_meta = [], {}, None
    cid = below_cluster(data, con)
    if cid:
        bdata = below_case(cid, con, index)
        if bdata:
            below_cites = c.cited_clusters(bdata)
            try:
                below_meta, below_ops, w = c.prepare(bdata, index)
                warnings += ["decision below: %s" % x for x in w]
            except c.Unsupported as e:
                warnings.append("decision below: cannot read its text (%s)" % e)
    elif con is not None:
        warnings.append("decision below: not found in the local database (%s %s)" % (
            data["feed"].get("LowerCourt"), data["feed"].get("LowerCourtCaseNumbers")))

    # ---- one cluster per case (CourtListener keeps two digitizations of many)
    canon_ = canonical(con, {k for f in fs for k in f["cites"]} | set(below_cites))
    for f in fs:
        merged = Counter()
        for k, n in f["cites"].items():
            merged[canon_.get(k, k)] += n
        f["cites"] = merged
    below_cites = {canon_.get(int(k), int(k)): v for k, v in below_cites.items()}

    # ---- the analysis
    cp = coupling(fs, below_cites, con)
    lc = law_analysis(fs, qp_paragraphs(data))
    allc = set(cp["P"]) | set(cp["R"]) | cp["below"] | {k for f in fs for k in f["cites"]}
    names = case_names(con, allc)
    areas = []
    try:
        from tribonian import cocite
        db = cocite.open_view(cocite.NOTE_VIEW)
    except (ImportError, sqlite3.Error, SystemExit, OSError):   # view not built
        db = None
    if db is not None:
        for k in set(cp["P"]) | set(cp["R"]):
            names[k]["area"] = area_for(db, k)
        areas = area_profile(cp, names, db)

    # ---- good law for every cited case (cached per case; --no-goodlaw skips it)
    gl = goodlaw_all(set(cp["P"]) | set(cp["R"])) if args.goodlaw and con is not None else {}
    cols = status_columns(data, cp, names, gl, below_meta)

    # ---- one PDF
    ops = []
    for o in below_ops:
        o["stage"] = "below"
        ops.append(o)
    for f in fs:
        if f["kind"] == "amicus" and not args.amici:
            continue
        o = f["o"]
        o["ctx"] = c.Ctx(f["abbr"], f["who"], 0)
        o["ctx"].links, o["ctx"].statutes = links, statutes
        o["kindname"] = "%s · %s%s" % (f["title"], c.long_date(f["date"]),
                                       " · supporting %ss" % f["side"] if f["kind"] == "amicus" and f["side"] in SIDE_NAME else "")
        o["byline"] = []
        o["byline_md"] = c.esc(f["text"], "md")              # the docket's words for it
        o["byline_tex"] = c.esc(f["text"], "tex")
        o["tree"] = c.tree_of(o, o["levels"])
        o["stage"] = f["stage"]
        ops.append(o)
    targets = {k: law_anchor(k) for k in lc["liq"]}
    for n, o in enumerate(ops):                  # unique ids for bookmarks and footnote keys across the documents
        o["oi"] = o["ctx"].oi = n
        o["ctx"].lawtargets = targets
    qp = qp_paragraphs(data)
    feed = data["feed"]
    title = "%s v. %s, No. %s (U.S.)" % (party(feed.get("PetitionerTitle")), party(feed.get("RespondentTitle")), num)
    meta = {"title": title, "name": "%s v. %s" % (party(feed.get("PetitionerTitle")), party(feed.get("RespondentTitle"))),
            "page": "1", "toc": True, "pretex": docket_tex(data, qp) + "\\contentspage\n" + couple_tex(cp, names, fs, areas) + authorities_tex(cp, names) + law_tex(lc) +
                                   (status_tex(cols, names, gl, set(cp["amici_only"])) if gl else ""),
            "stages": dict(STAGES), "posttex": law_appendix_tex(lc, fs),
            "srcline": ["No. %s (U.S.)" % num, "Supreme Court docket and filings (supremecourt.gov)",
                        "decision below: CourtListener cluster %s" % cid if cid else "", "retrieved " + data.get("retrieved", "")]}
    (c.ROOT / "build").mkdir(exist_ok=True)
    tex_path = c.ROOT / "build" / (slug + ".tex")
    tex_path.write_text(c.render_tex(meta, ops, args.font, args.style), encoding="utf-8")

    # ---- notes: one per filing, then the docket
    rel_pdf = (c.pdf_dir(ndir) / (slug + ".pdf")).relative_to(c.ROOT)
    for f in fs:
        o = f["o"]
        if "ctx" not in o:                       # an amicus brief left out of the PDF still gets its note
            o["ctx"] = c.Ctx(f["abbr"], f["who"], 0)
            o["ctx"].links, o["ctx"].statutes = links, statutes
            o["kindname"] = "%s · %s" % (f["title"], c.long_date(f["date"]))
            o["byline_md"], o["byline_tex"] = "", ""
            o["tree"] = c.tree_of(o, o["levels"])
        cite = "%s, %s, No. %s (U.S. filed %s)" % (f["who"], f["abbr"], num, c.long_date(f["date"]))
        head = c.yaml_head([("title", cite), ("aliases", ["%s %s" % (f["abbr"], num)]), ("type", c.Raw("supreme-court-filing")),
                            ("kind", c.Raw(f["kind"])), ("stage", c.Raw(f["stage"])), ("side", c.Raw(f["side"] or "none")),
                            ("filer", f["who"]), ("docket", num), ("docket_note", "[[%s]]" % slug), ("filed", c.Raw(f["date"])),
                            ("source", c.Raw(f["link"]["url"])), ("retrieved", c.Raw(data.get("retrieved", ""))),
                            ("synthetic", False)]) + ["", "# %s" % c.esc(cite, "md"), "",
                "Docket: [[%s|No. %s]] · %s · [PDF](%s)" % (slug, num, c.esc(f["text"], "md"), f["link"]["url"]), ""]
        tail = ["## Authorities", "", "*Reporter citations in this filing, resolved through the local database; ×n = mentions.*", ""]
        for k, n in f["cites"].most_common():
            tail.append("- %s ×%d" % (link_md(names, index, k), n))
        if f["miss"]:
            tail += ["", "Not resolved: " + ", ".join("%s ×%d" % (k, n) if n > 1 else k for k, n in f["miss"].most_common())]
        stat = f["laws"]["cites"]
        if stat or f["laws"]["named"]:
            tail += ["", "## Statutes and provisions cited", "",
                     "*Statutes, regulations and constitutional provisions in this filing (its appendix left out); ×n = citations.*", ""]
            for k, n in sorted(stat.items(), key=lambda kv: (-kv[1], lawcites.sort_key(kv[0]))):
                lab, sn = c.esc(lawcites.label(k), "md"), lawcites.note_name(k)
                tail.append("- %s%s" % ("[[%s|%s]]" % (sn, lab) if sn in statutes else lab, " ×%d" % n if n > 1 else ""))
            if f["laws"]["named"]:
                tail.append("- By name: " + "; ".join("%s ×%d" % (c.esc(lawcites.label(k), "md"), n)
                                                      for k, n in f["laws"]["named"].most_common()))
        md = c.render_md(None, [o], "\n".join(tail) + "\n", head=head)
        (ndir / (f["slug"] + ".md")).write_text(md, encoding="utf-8")

    L = c.yaml_head([("title", title), ("aliases", ["No. %s" % num, meta["name"]]), ("type", c.Raw("supreme-court-docket")),
                     ("status", c.Raw("pending")), ("docket", num), ("granted", c.Raw(data.get("granted") or "")),
                     ("argued", c.Raw(data.get("argued") or "")), ("decision_below", c.Raw(cid or "")), ("source", c.Raw(data["source"])),
                     ("retrieved", c.Raw(data.get("retrieved", ""))), ("synthetic", False)]) + ["", "# %s" % c.esc(title, "md"), "",
         "%s, No. %s, decided %s · cert. granted %s%s" % (c.esc(feed.get("LowerCourt", ""), "md"), (feed.get("LowerCourtCaseNumbers") or "").strip("() "),
                                                         feed.get("LowerCourtDecision", ""), c.long_date(data.get("granted")) or "–",
                                                         " · argued %s" % c.long_date(data["argued"]) if data.get("argued") else ""), "",
         "PDF: `%s`" % rel_pdf, ""]
    if qp:
        L += ["## Questions presented", ""] + [c.esc(p, "md") + "\n" for p in qp]
    if cid:
        L += ["## Decision below", "", "- %s" % link_md(names, index, cid, 90) if str(cid) in index else
              "- %s — no note yet (`tribonian scotus build %s --depth 1`, or `tribonian map get --cluster %s`)" % (
                  c.esc(below_meta["title"] if below_meta else "cluster %s" % cid, "md"), num, cid), ""]
    L += ["## Filings", ""]
    for stage, label in STAGES[1:]:
        mine = [f for f in fs if f["stage"] == stage]
        if mine:
            L += ["### %s" % label, ""]
            L += ["- [[%s|%s]] — %s, %s%s" % (f["slug"], c.esc(f["abbr"], "md"), c.esc(f["who"], "md"), f["date"],
                                              " · supporting %ss" % f["side"] if f["kind"] == "amicus" and f["side"] else "") for f in mine]
            L.append("")
    L += couple_md(cp, names, index, fs, areas)
    L += law_md(lc, statutes)
    L += law_appendix_md(lc, fs, statutes)
    if gl:
        L += status_md(cols, names, index, gl, set(cp["amici_only"]))
    L += ["## Proceedings and orders", ""]
    for e in data["entries"]:
        docs = " ".join("[%s](%s)" % (c.esc(l["desc"], "md"), l["url"]) for l in e["links"] if l.get("url") and not SKIP_LINK.search(l["desc"]))
        L.append("- %s — %s%s" % (e["date"], c.esc(e["text"], "md"), " · " + docs if docs else ""))
    (ndir / (slug + ".md")).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("wrote %s (+ %d filing notes)" % ((ndir / (slug + ".md")).relative_to(c.ROOT), len(fs)))
    print("wrote %s" % tex_path.relative_to(c.ROOT))
    if not args.no_pdf:
        pdf = c.run_pdflatex(tex_path, c.pdf_dir(ndir))
        if pdf:
            print("wrote %s" % pdf.relative_to(c.ROOT))
    c.unmapped_note()
    for o in ops:
        print("  %-14s %-34s %s" % (o.get("stage", ""), (o["ctx"].who + " " + (o["ctx"].reporter if o.get("stage") != "below" else ""))[:34], o.get("summary", "")))
    print("  authorities: %d cases cited, %d by both parties (%d by both sides with amici), %d by amici only, %d unresolved citations" % (
        len(allc - cp["below"]), len(cp["contested"]), cp["sides_both"], len(cp["amici_only"]), sum(len(f["miss"]) for f in fs)))
    for w in warnings:
        print("  note:", w)
    return data, fs, cp, cid




def crawl(args, data, fs, cid):
    """--depth: case notes for the decision below and every case the filings cite (step 1), then what
    those cite (step 2, ...). Full text for steps up to --text, stubs beyond; the same files and notes
    casemap.py map writes, so tribonian build builds their PDFs."""
    from tribonian import casemap
    con, done = casemap.open_db()
    index = c.load_index()
    level = {}
    for k in ([cid] if cid else []) + [k for f in fs for k, _ in f["cites"].most_common()]:
        level.setdefault(int(k), 1)
    print("depth 1: %d cases (the decision below and the filings' authorities)" % len(level))
    frontier = list(level)
    for d in range(2, args.depth + 1):
        nxt = []
        for k in frontier:
            for k2 in (casemap.db_case(con, k, False) or {}).get("cites", {}):
                if int(k2) not in level:
                    level[int(k2)] = d
                    nxt.append(int(k2))
        print("depth %d: %d new cases (%d so far)" % (d, len(nxt), len(level)))
        if len(level) > args.max:
            sys.exit("%d cases is over --max %d. Lower --depth, or raise --max if you mean it." % (len(level), args.max))
        frontier = nxt
    for k, d in sorted(level.items(), key=lambda kv: kv[1]):
        ent = index.get(str(k), {})
        want_text = d <= args.text
        have_api_copy = ent.get("full") and (c.CASES / ent["full"]).exists()
        if have_api_copy and want_text:
            continue
        data_k = casemap.db_case(con, k, want_text or bool(ent.get("full")))
        if data_k is None:
            level[k] = None
            continue
        if have_api_copy:                          # keep the fetched file; just record what it cites
            old = json.loads((c.CASES / ent["full"]).read_text(encoding="utf-8"))
            old["cites"] = data_k["cites"]
            (c.CASES / ent["full"]).write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")
        else:
            casemap.save_case(data_k, index, want_text)
    c.save_index(index)
    kinds = Counter(c.write_note(k, index) for k, d in level.items() if d is not None and str(k) in index)
    print("wrote %d case notes in md-cases/: %s" % (sum(kinds.values()), ", ".join("%d %s" % (n, k) for k, n in kinds.items())))
    print("their PDFs: tribonian build   (then rerun tribonian scotus build %s so its notes link to them)" % data["number"])


def build_cmd(args):
    data, fs, cp, cid = build(args)
    if args.depth > 0:
        crawl(args, data, fs, cid)


# ================================================================ recent activity
# What the Court has done lately, from its own public pages (no API, no language model):
#   decided  - the slip-opinion tables, /opinions/slipopinion/<YY>, one per term (HTML)
#   granted  - the Granted & Noted lists, /orders/<YY>grantednotedlist.pdf (cases granted
#              review for argument in that term, with the date granted and argued)
#   orders   - the order lists and miscellaneous orders linked from
#              /orders/ordersofthecourt/<YY> (PDFs): grants, summary dispositions, denials and
#              orders in pending cases, each docket line under its section heading
# There is no public list of every pending petition for certiorari: a petition shows up here
# only once the Court acts on it. Term YY = October Term 20YY, which starts in October.
# Order and list PDFs are kept in cases/scotus/_recent/ (a dated order list never changes; the
# Granted & Noted lists do, so they are fetched again every run).

RECENT_FILE = SCOTUS / "_recent.json"
RECENT_DIR = SCOTUS / "_recent"
RECENT_DAYS = 365
DOCKET_RE = r"\d{2}-\d{1,5}|\d{2}A\d{1,4}|\d{1,3},?\s+ORIG\.?|\d{2}O\d{1,4}"
TAG_RE = re.compile(r"<[^>]+>")
TERM_END_MONTH = 10            # a term is named for the year its October starts


def term_of(day):
    """date -> the October Term it falls in, as two digits ('25' for Oct 2025 - Sep 2026)."""
    y = day.year if day.month >= TERM_END_MONTH else day.year - 1
    return "%02d" % (y % 100)


def us_date(s):
    """'6/30/26', '06/30/2026' -> '2026-06-30'; '' if it isn't one."""
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\b", s or "")
    if not m:
        return ""
    y = int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return date(y, int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return ""


def cell_text(html):
    """Inner HTML of a table cell -> its text, entities decoded, spaces collapsed."""
    import html as h
    return " ".join(h.unescape(TAG_RE.sub(" ", html)).split())


def first_docket(s):
    """The first docket number in a string, normalized ('25-579', '25A374', '22O141'); '' if none."""
    m = re.search(DOCKET_RE, s or "")
    if not m:
        return ""
    d = m.group(0)
    o = re.match(r"(\d+),?\s+ORIG", d)
    return "%s Orig." % o.group(1) if o else d


def parse_slipopinions(html, base, term):
    """A slip-opinion page -> [{date, docket, dockets, name, pdf, justice, citation, term}].
    Every <tr> of every table is read the same way, by what its cells look like rather than by
    column position (the Court adds and drops columns, e.g. 'Revised'): the date is the first cell
    shaped like M/D/YY, the docket the first cell holding a docket number, the name and PDF the
    first link to an opinion PDF that is not a revision ('_diff_'), the citation the last cell."""
    out, seen = [], set()
    for row in re.findall(r"(?is)<tr\b.*?</tr>", html):
        cells = re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", row)
        if len(cells) < 4:
            continue
        texts = [cell_text(x) for x in cells]
        day = next((us_date(t) for t in texts if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", t)), "")
        dk_cell = next((t for t in texts if re.fullmatch(r"(?:%s)(?:\s*[,&]\s*(?:%s))*" % (DOCKET_RE, DOCKET_RE), t)), "")
        link = None
        for href, inner in re.findall(r"""(?is)<a\b[^>]*href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", row):
            if re.search(r"(?i)\.pdf\b", href) and "_diff_" not in href:
                link = (urllib.parse.urljoin(base, href.strip()), cell_text(inner))
                break
        if not (day and dk_cell and link):
            continue
        dockets = [first_docket(x) for x in re.split(r"\s*[,&]\s*", dk_cell)]
        key = (day, dockets[0], link[1])
        if key in seen:
            continue
        seen.add(key)
        just = next((t for t in texts if re.fullmatch(r"[A-Z]{1,3}", t)), "")
        out.append({"date": day, "docket": dockets[0], "dockets": dockets, "name": link[1], "pdf": link[0],
                    "justice": just, "citation": texts[-1] if texts[-1] != link[1] else "", "term": term})
    return out


def parse_granted(text, url, term):
    """pdftotext -layout of a Granted & Noted list -> [{docket, name, court, granted, argued, term, list}].
    An entry starts at a docket number at the left margin ('25-459 CFX SALAZAR V. PARAMOUNT GLOBAL';
    CFX/CSX/... is the Clerk's case-type code, dropped). Consolidated cases list each docket with
    ')' and a numbered name ('25-238)' / '1 CFX VIRAMONTES V. COOK COUNTY'), then one
    'Court: ... Granted: 6/30/26' line and maybe 'Argument Date: 12/2/26' for the whole group."""
    out, group, pending = [], [], []      # pending: docket numbers alone on a line, names to follow

    def entry(dk, rest):
        rest = re.sub(r"^\d\s+", "", rest.strip())
        rest = re.sub(r"^(?:C[A-Z]{2}|CA)\s+", "", rest)
        group.append({"docket": dk, "name": rest, "court": "", "granted": "", "argued": "", "term": term, "list": url})

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(%s)\)?\s*(.*)$" % DOCKET_RE, line)
        if m:
            if group and group[-1]["granted"]:      # a new entry after the last one's Granted: line
                out.extend(group)
                group.clear()
            rest = m.group(2).strip()
            if not rest or re.fullmatch(r"\d", rest):
                pending.append(first_docket(m.group(1)))
            else:
                entry(first_docket(m.group(1)), rest)
            continue
        n = re.match(r"^\d\s+(.+)$", line)
        if n and pending:
            entry(pending.pop(0), n.group(1))
            continue
        g = re.search(r"(?i)\bGranted:\s*(\S+)", line)
        cm = re.search(r"(?i)\bCourt:\s*(.+?)(?:\s+Granted:|$)", line)
        a = re.search(r"(?i)\bArgument Date:\s*(\S+)", line)
        for e in group:
            if g:
                e["granted"] = us_date(g.group(1))
            if cm and not e["court"]:
                e["court"] = cm.group(1).strip()
            if a:
                e["argued"] = us_date(a.group(1))
    out.extend(group)
    return out


SECTION_RE = re.compile(r"^[A-Z][A-Z ,'\-–—()/.]{6,}$")
ORDER_KINDS = [                       # section heading or order text -> kind, first match wins
    (r"CERTIORARI\s+GRANTED|certiorari (?:is|are) granted|probable jurisdiction (?:is )?noted", "granted"),
    (r"SUMMARY DISPOSITION|judgment is vacated, and the case is remanded|GRANTED, VACATED", "gvr"),
    (r"REHEARINGS?\s+DENIED|rehearing is denied", "rehearing denied"),
    (r"CERTIORARI\s+DENIED|certiorari (?:is|are) denied", "denied"),
    (r"(MANDAMUS|PROHIBITION|HABEAS CORPUS)\s+DENIED", "denied"),
    (r"ATTORNEY DISCIPLINE", "discipline"),
    (r"ORDERS? IN PENDING CASES?", "pending"),
]


def order_kind(section, text):
    for pat, kind in ORDER_KINDS:
        if re.search(pat, section or ""):
            return kind
    for pat, kind in ORDER_KINDS:
        if re.search(pat, text or "", re.I):
            return kind
    return "order"


def parse_orders(text, day, url, label):
    """pdftotext -layout of an order list or miscellaneous order -> [{date, docket, name, section,
    kind, text, url, list}]. Docket lines ('25-579   DEPT. OF AIR FORCE, ET AL. V. GUAHAN, PRUTEHI')
    sit under all-caps section headings (CERTIORARI GRANTED, CERTIORARI DENIED, ORDERS IN PENDING
    CASES...); the indented prose after a run of docket lines is the order for all of them."""
    out, section, run, prose = [], "", [], []

    def close():
        t = " ".join(" ".join(prose).split())
        listed = order_kind(section, "") in ("denied", "rehearing denied")
        for i, e in enumerate(run):
            # under a list of denials, prose (a dissent, a granted motion) belongs to the line just
            # above it, not to every docket since the last prose; elsewhere one order covers the run
            mine = t if not listed or i == len(run) - 1 else ""
            e["text"] = mine[:600]
            e["kind"] = order_kind(section, mine)
            out.append(e)
        run.clear()
        prose.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"\d{1,3}", line) or line.startswith("(ORDER LIST"):
            continue
        m = re.match(r"^(%s)\s{2,}(\S.*)$" % DOCKET_RE, line) or re.match(r"^(%s)\s+([A-Z0-9].*\bV\.\s.*|IN RE\b.*)$" % DOCKET_RE, line)
        if m:
            if prose:
                close()
            run.append({"date": day, "docket": first_docket(m.group(1)), "name": " ".join(m.group(2).split()),
                        "section": section, "url": url, "list": label})
            continue
        if SECTION_RE.match(line) and not re.search(r"\bV\.\s", line) and not re.match(r"(?i)^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\b", line):
            if run or prose:
                close()
            section = " ".join(line.split())
            continue
        if run:
            prose.append(line)
    close()
    return out


def orders_page(html, base):
    """/orders/ordersofthecourt/<YY> -> [(date, label, pdf url)]: every link to a PDF under
    courtorders/, its label (Order List, Miscellaneous Order...) and the M/D/YY date before it."""
    out = []
    for m in re.finditer(r"""(?is)(\d{1,2}/\d{1,2}/\d{2,4})(?:(?!\d{1,2}/\d{1,2}/\d{2}).){0,400}?<a\b[^>]*href\s*=\s*["']([^"']*courtorders/[^"']+\.pdf)["'][^>]*>(.*?)</a>""", html):
        out.append((us_date(m.group(1)), cell_text(m.group(3)), urllib.parse.urljoin(base, m.group(2).strip())))
    return out


def pdf_text(url, keep=True):
    """A PDF from the Court -> its pdftotext -layout text, or None. Kept in cases/scotus/_recent/
    (keep=False: fetched again, for lists that change)."""
    RECENT_DIR.mkdir(parents=True, exist_ok=True)
    out = RECENT_DIR / re.sub(r"[^\w.\-]+", "_", Path(urllib.parse.urlparse(url).path).name)
    if not keep or not out.exists() or out.stat().st_size == 0:
        if get(url, out) is None:
            return None
    t = subprocess.run(["pdftotext", "-layout", str(out), "-"], capture_output=True, timeout=300)
    return t.stdout.decode("utf-8", errors="replace") if t.returncode == 0 else None


def recent(args):
    """Fetch and parse the Court's recent activity -> cases/scotus/_recent.json (atomic write)."""
    import os
    from datetime import timedelta
    today = date.today()
    since = (today - timedelta(days=args.days)).isoformat()
    terms = sorted({term_of(today - timedelta(days=args.days)), term_of(today)})
    errors, sources = [], []

    def page(url):
        sources.append(url)
        raw = get(url)
        if raw is None:
            errors.append("could not read %s" % url)
        return raw.decode("utf-8", errors="replace") if raw else ""

    decided = []
    for t in terms:
        url = SITE + "opinions/slipopinion/%s" % t
        decided += parse_slipopinions(page(url), url, t)
    decided = sorted((d for d in decided if d["date"] >= since), key=lambda d: d["date"], reverse=True)
    print("decided: %d opinions since %s (terms %s)" % (len(decided), since, ", ".join(terms)))

    granted = []
    for t in sorted(set(terms) | {"%02d" % ((int(terms[-1]) + 1) % 100)}):
        url = SITE + "orders/%sgrantednotedlist.pdf" % t
        sources.append(url)
        text = pdf_text(url, keep=False)
        if text is None:
            if t != terms[-1] and t in terms:
                errors.append("could not read %s" % url)
            continue                    # next term's list exists only once the Court grants for it
        granted += parse_granted(text, url, t)
    by_docket = {}
    for d in decided:
        for k in d["dockets"]:
            by_docket.setdefault(k, d)
    for g in granted:
        d = by_docket.get(g["docket"])
        g["decided"] = d["date"] if d else ""
    print("granted: %d cases on the Granted & Noted lists, %d not yet decided" % (len(granted), sum(not g["decided"] for g in granted)))

    orders = []
    for t in terms:
        url = SITE + "orders/ordersofthecourt/%s" % t
        for day, label, pdf in orders_page(page(url), url):
            if not day or day < since:
                continue
            text = pdf_text(pdf)
            if text is None:
                errors.append("could not read %s" % pdf)
                continue
            orders += parse_orders(text, day, pdf, label)
    orders.sort(key=lambda o: o["date"], reverse=True)
    kinds = Counter(o["kind"] for o in orders)
    print("orders: %d docket lines since %s (%s)" % (len(orders), since, ", ".join("%d %s" % (n, k) for k, n in kinds.most_common())))

    have = {p.stem for p in SCOTUS.glob("*.json") if not p.stem.startswith("_")}
    data = {"retrieved": datetime.now().isoformat(timespec="seconds"), "since": since, "terms": terms,
            "sources": sources, "errors": errors, "fetched": sorted(have),
            "decided": decided, "granted": granted, "orders": orders}
    for e in errors:
        print("  ! " + e)
    if args.json:
        print(json.dumps(data, indent=1, ensure_ascii=False))
    if not (decided or granted or orders):
        sys.exit("nothing read (no network?); %s left as it was" % RECENT_FILE.relative_to(c.ROOT))
    SCOTUS.mkdir(parents=True, exist_ok=True)
    tmp = RECENT_FILE.with_suffix(".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, RECENT_FILE)       # atomic: the GUI may be reading it
    print("saved %s" % RECENT_FILE.relative_to(c.ROOT))


# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="the Court's docket and filed PDFs -> cases/scotus/")
    f.add_argument("number", help="docket number, e.g. 25-579")
    f.set_defaults(func=fetch)
    b = sub.add_parser("build", help="one PDF, a docket note, a note per filing, the authorities they share")
    b.add_argument("number", help="docket number, e.g. 25-579")
    b.add_argument("--amici", action="store_true", help="put the amicus briefs in the PDF too (their notes are always written)")
    b.add_argument("--appendix", action="store_true", help="keep appendices (the petition's repeats the decision below)")
    b.add_argument("--depth", type=int, default=0, help="also write case notes for the decision below and the cited cases (1), "
                   "and what those cite (2, ...)")
    b.add_argument("--text", type=int, default=1, help="with --depth: steps that get full text (default 1); stubs beyond")
    b.add_argument("--max", type=int, default=3000, help="with --depth: stop if it would write more notes than this")
    b.add_argument("--no-goodlaw", dest="goodlaw", action="store_false",
                   help="skip the good-law check of every cited case (on by default; slow the first time, cached after)")
    b.add_argument("--font", default="XCharter", help="LaTeX font package (default XCharter)")
    b.add_argument("--style", default="boxes", choices=["boxes", "rails"])
    b.add_argument("--no-pdf", action="store_true", help="skip pdflatex")
    b.set_defaults(func=build_cmd)
    r = sub.add_parser("recent", help="the Court's recent opinions, grants and orders -> cases/scotus/_recent.json")
    r.add_argument("--json", action="store_true", help="also print the result as JSON")
    r.add_argument("--days", type=int, default=RECENT_DAYS, help="how far back (default %d)" % RECENT_DAYS)
    r.set_defaults(func=recent)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
