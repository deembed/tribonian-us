"""html_paragraphs.py -- parser family for HTML where each line of the code is its own block element.

The formula's [html] table says which elements carry what (defaults are Colorado's Word-filtered HTML):
    blocks    the elements that are lines of text                       ["p"]
    headings  blocks that are headings (containers, cross headings)    ["p:centered"]
    opening   the leading run that can open a section                  ["b", "strong"]
    verbatim  blocks taken whole, no label parsing (a statutory form    []
              set as a table): one text item, cells joined by spaces
    root      the element holding the law; everything outside it is    (the page body)
              site furniture and is not read or checked
    split_br  elements whose lines are separated by <br> (Maryland):   []
              each line becomes a block
    nested    blocks may contain blocks (a div per level): an outer      false
              block's own text becomes blocks, its inner blocks read in order
    loose     elements whose bare text and inline children (text      []
              outside any <p>) become blocks of their own, one per run
              between block children (Delaware's div.Section)
    indent    non-breaking spaces per level at a line's start: the     0 (not used)
              ladder takes it as a hint between two plausible levels
    drop      site furniture removed before parsing (navigation bars)  []
Selectors: "tag", "tag.class", ".class", ":centered" (align or style says center), ":noclass".

Structure from there, all named in the formula:
    headings              container headings (TITLE 26.5, ARTICLE 1, CHAPTER 1) via [containers] regexes;
                          the next heading is the container's name unless heading = "self"
    an opening run        a section start, via [sections].match on that run
    a leading (label)     a numbered paragraph, placed on a level by the shared Ladder (tree.py)
Notes start at a block matching [notes] (by text regex, or by class); from there to the next section or
container, everything is notes. Headings inside notes stay in the notes.

Nothing is dropped silently: a block that fits nowhere becomes ("unmapped", text); what [html].drop removes
is counted per selector in the verify report, and the conservation check reads the source with the same
elements removed -- so any other text the parser misses fails the build.
Used by: Colorado (C.R.S., Colorado Constitution), Connecticut (General Statutes).
"""
import re
import unicodedata

from lxml import html as lhtml

from tribonian.uslm.tree import Node, Ladder, split_labels, roman_value

SPACES = dict.fromkeys(map(ord, "       "), " ")


def norm(s):
    """NFC, odd spaces to plain, invisible format characters (zero-width space, BOM: category Cf) dropped,
    whitespace collapsed. The conservation check reads the source through the same function."""
    s = unicodedata.normalize("NFC", s.translate(SPACES))
    return " ".join("".join(ch for ch in s if unicodedata.category(ch) != "Cf").split())


HTML_DEFAULTS = {"blocks": ["p"], "headings": ["p:centered"], "opening": ["b", "strong"], "verbatim": [], "drop": [],
                 "root": None, "split_br": [], "loose": [], "nested": False, "indent": 0}


def selector(sel):
    """'tag', 'tag.class', '.class', 'tag#id', 'tag[attr*=text]', 'tag:centered', 'tag:bold', 'tag:noclass' -> predicate on an
    lxml element. :centered and :bold also go by the page's CSS class rules."""
    attr = re.search(r"\[([a-z-]+)\*=([^\]]+)\]", sel)       # [style*=margin-left]: attribute contains
    sel = re.sub(r"\[[^\]]*\]", "", sel)
    sel, _, pseudo = sel.partition(":")
    centered, noclass, bold = pseudo == "centered", pseudo == "noclass", pseudo == "bold"
    sel, _, ident = sel.partition("#")
    tag, _, cls = sel.partition(".")

    def match(el):
        if not isinstance(el.tag, str) or (tag and el.tag != tag):
            return False
        if ident and el.get("id") != ident:
            return False
        if attr and attr.group(2).strip("'\"") not in (el.get(attr.group(1)) or ""):
            return False
        if cls and cls not in (el.get("class") or "").split():
            return False
        if noclass and (el.get("class") or "").strip():
            return False
        classes = set((el.get("class") or "").split())
        if centered and "center" not in ((el.get("align") or "") + (el.get("style") or "")).lower() \
                and not classes & _CSS["center"]:
            return False
        if bold and not re.search(r"font-weight:\s*(bold|[6-9]00)", (el.get("style") or "").lower()) \
                and not classes & _CSS["bold"]:
            return False
        return True
    return match


def _any(sels):
    preds = [selector(s) for s in sels]
    return lambda el: any(p(el) for p in preds)


def html_cfg(formula):
    return dict(HTML_DEFAULTS, **formula.get("html", {}))


_HTML_START = re.compile(r"<html[\s>]", re.I)
_CSS = {"center": set(), "bold": set()}


def parse_page(raw):
    """The page as one element tree. Some publishers serve several complete HTML documents back to back
    (South Dakota's whole-chapter pages); the HTML parser stops at the first </html>, so each document is
    parsed on its own and their bodies are joined in order."""
    starts = [m.start() for m in _HTML_START.finditer(raw)]
    one = lhtml.fromstring(raw)
    if len(starts) <= 1 or _visible_chars(raw) <= _parsed_chars(one) * 1.01:
        return one                       # one document, or the single parse already has all the text
                                         # (Maryland's pages hold an <html> inside a div: no split needed)
    head = raw[:starts[0]]
    out = lhtml.fromstring("<html><head></head><body></body></html>")
    body = out.find("body")
    for i, a in enumerate(starts):
        part = (head if i == 0 else "") + raw[a:starts[i + 1] if i + 1 < len(starts) else len(raw)]
        d = lhtml.document_fromstring(part)
        for st in d.iter("style"):
            out.find("head").append(st)
        b = d.find("body")
        src = b if b is not None else d
        if src.text:                                # text before the document's first element
            if len(body):
                body[-1].tail = (body[-1].tail or "") + src.text
            else:
                body.text = (body.text or "") + src.text
        for ch in list(src):
            body.append(ch)
    return out


def _css_classes(doc):
    """Class names that the page's own <style> rules make centered or bold (Word-made HTML puts it there,
    not in align= or <b>): read by the :centered and :bold selectors."""
    _CSS["center"], _CSS["bold"] = set(), set()
    for st in doc.iter("style"):
        for sels, body in re.findall(r"([^{}]+)\{([^}]*)\}", st.text_content() or ""):
            body = body.lower().replace(" ", "")
            for sel in sels.split(","):
                m = re.search(r"\.([A-Za-z0-9_-]+)\s*$", sel.strip())
                if not m:
                    continue
                if "text-align:center" in body:
                    _CSS["center"].add(m.group(1))
                if re.search(r"font-weight:(bold|[6-9]00)", body):
                    _CSS["bold"].add(m.group(1))


def parse_coverage(raw):
    """How much of the file's visible text the HTML parser kept: characters (whitespace removed) in the raw
    file with tags, scripts, styles and comments stripped, against the parsed tree's text. A parser that
    stops early or swallows a stretch of the file shows up here even when the conservation check -- which
    reads the source through the same parser -- passes."""
    a, b = _visible_chars(raw), _parsed_chars(parse_page(raw))
    return {"raw_chars": a, "parsed_chars": b, "ratio": round(b / a, 4) if a else 1.0}


def _visible_chars(raw):
    """Characters (no whitespace) inside the file's <body> elements -- all of them, for pages that are several
    documents -- with tags, scripts, styles and comments stripped. Independent of the HTML parser."""
    import html as _h
    t = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>|<!--.*?-->", " ", raw)
    bodies = re.findall(r"(?is)<body\b[^>]*>(.*?)(?:</body\s*>|$)", t)
    t = " ".join(bodies) if bodies else t
    t = _h.unescape(re.sub(r"<[^>]*>", " ", t))
    return len("".join(norm(t).split()))


def _parsed_chars(doc):
    body = doc.find(".//body") if doc.tag != "body" else doc
    el = body if body is not None else doc
    return len("".join(norm("".join(t for t in el.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]"))).split()))


def load(raw, formula):
    """-> (the parsed page with [html].drop removed, {selector: characters dropped})."""
    cfg = html_cfg(formula)
    doc = parse_page(raw)
    _css_classes(doc)
    if cfg["root"]:
        pred = selector(cfg["root"])
        found = next((e for e in doc.iter() if pred(e)), None)
        if found is None:
            raise ValueError("no %s in the page" % cfg["root"])
        found.tail = None
        doc = found
    for sel in cfg["split_br"]:
        pred = selector(sel)
        for el in [e for e in doc.iter() if pred(e)]:
            _split_br(el)
    for sel in cfg["loose"]:
        pred = selector(sel)
        for el in [e for e in doc.iter() if pred(e)]:
            _wrap_loose(el)
    if cfg["nested"]:                    # blocks inside blocks (a div per level, Florida): the outer one's own text
        is_block = _any(cfg["blocks"] + cfg["headings"])
        for el in [e for e in doc.iter() if is_block(e)]:
            if any(is_block(d) for d in el.iterdescendants()):
                _wrap_loose(el)          # its bare text becomes blocks; it is a container, not a block, from here
                el.set("data-container", "1")
    for br in doc.iter("br"):            # a line break inside a block reads as a space ("21-P<br>DEPARTMENT")
        br.tail = " " + (br.tail or "")
    dropped = {}
    for sel in cfg["drop"]:
        pred = selector(sel)
        for el in [e for e in doc.iter() if pred(e)]:
            dropped[sel] = dropped.get(sel, 0) + len(re.sub(r"\s+", "", norm(el.text_content())))
            el.drop_tree()                 # the element and its text go; its tail stays with the parent
    return doc, dropped


def _split_br(el):
    """Regroup el's content into one <p class="br-line"> per run between <br> elements. Block-level children
    (a <div>, a <table>) stay blocks of their own between the runs. Text order is kept exactly."""
    from lxml import etree
    parts, cur = [], [el.text or ""]
    el.text = None
    for ch in list(el):
        el.remove(ch)
        if isinstance(ch.tag, str) and ch.tag == "br":
            parts.append(cur)
            cur = [ch.tail or ""]
            continue
        if isinstance(ch.tag, str) and ch.tag in BLOCK_TAGS:
            parts.append(cur)
            tail, ch.tail = ch.tail, None
            parts.append(ch)
            cur = [tail or ""]
            continue
        cur.append(ch)
    parts.append(cur)
    for run in parts:
        if not isinstance(run, list):        # a block child: back in place as it was
            el.append(run)
            continue
        p = etree.SubElement(el, "p")
        p.set("class", "br-line")
        last = None
        for x in run:
            if isinstance(x, str):
                if last is None:
                    p.text = (p.text or "") + x
                else:
                    last.tail = (last.tail or "") + x
            else:
                p.append(x)
                last = x
        if not (p.text or "").strip() and not len(p):
            el.remove(p)


BLOCK_TAGS = {"p", "div", "table", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "hr", "br",
              "section", "article", "header", "footer", "nav", "dl"}


def _wrap_loose(el):
    """Runs of bare text and inline elements directly inside el -> one <p class="loose"> each; block children
    stay where they are. Order is kept exactly."""
    from lxml import etree
    kids, out, run = list(el), [], []
    lead = el.text or ""
    el.text = None

    def flush():
        if run and any((x if isinstance(x, str) else etree.tostring(x, method="text", encoding=str, with_tail=False)).strip()
                       for x in run):
            p = etree.Element("p")
            p.set("class", "loose")
            last = None
            for x in run:
                if isinstance(x, str):
                    if last is None:
                        p.text = (p.text or "") + x
                    else:
                        last.tail = (last.tail or "") + x
                else:
                    p.append(x)
                    last = x
            out.append(p)
        elif run:                           # whitespace only: keep it as a tail so nothing moves
            out.append("".join(x for x in run if isinstance(x, str)))
        run.clear()
    if lead:
        run.append(lead)
    for ch in kids:
        el.remove(ch)
        tail, ch.tail = ch.tail, None
        if isinstance(ch.tag, str) and ch.tag in BLOCK_TAGS:
            flush()
            out.append(ch)
        else:
            run.append(ch)
        if tail:
            run.append(tail)
    flush()
    prev = None
    for x in out:
        if isinstance(x, str):
            if prev is None:
                el.text = (el.text or "") + x
            else:
                prev.tail = (prev.tail or "") + x
        else:
            el.append(x)
            prev = x


def _indent(el):
    """Leading non-breaking spaces of the block's text."""
    t = el.text_content()
    return len(t) - len(t.lstrip("\u00a0"))


def _opening(el, is_open):
    """The block's leading run of text inside elements matching is_open (whitespace may sit between)."""
    run = []
    for t in el.xpath(".//text()"):
        owner = t.getparent()
        if t.is_tail:
            owner = owner.getparent()
        inside, a = False, owner
        while a is not None and not inside:
            inside = is_open(a)
            a = None if a is el else a.getparent()
        if not norm(t):
            if run:
                run.append(" ")
            continue
        if not inside:
            break
        run.append(t)
    return norm("".join(run))


def _ancestors(el, top):
    """el's ancestors up to and including top ([html].root), not the page around it."""
    for a in el.iterancestors():
        yield a
        if a is top:
            return


def paragraphs(doc, formula):
    """-> [(text, opening_run, is_heading, classes, verbatim, indent depth)] in document order; a block inside a block
    (a <p> in a verbatim table) counts once, as part of the outer one."""
    cfg = html_cfg(formula)
    is_block, is_head, is_open, is_verb = _any(cfg["blocks"]), _any(cfg["headings"]), _any(cfg["opening"]), _any(cfg["verbatim"])
    unit = cfg["indent"]
    depth = lambda el: (round(_indent(el) / unit) - 1 if _indent(el) >= unit else None) if unit else None
    any_block = lambda e: (is_block(e) or is_head(e) or is_verb(e)) and not e.get("data-container")
    out = []
    for el in doc.iter():
        if not any_block(el) or any(any_block(a) for a in _ancestors(el, doc)):
            continue
        if is_verb(el):
            cells = el.xpath(".//td|.//th") or [el]
            text = norm(" ".join(c.text_content() for c in cells))
            if text:
                out.append((text, "", False, set((el.get("class") or "").split()), True, None))
            continue
        text = norm(el.text_content())
        if text:
            out.append((text, _opening(el, is_open), is_head(el), set((el.get("class") or "").split()), False, depth(el)))
    return out


def _regex_list(v):
    return [re.compile(x) for x in ([v] if isinstance(v, str) else v or [])]


class Builder:
    def __init__(self, f, ladder, item_name, root_ident=None):
        self.f = f
        self.doc = f["doc"]
        self.ladder = ladder
        self.item = item_name
        self.containers = []
        for name, c in f.get("containers", {}).items():
            self.containers.append(dict(c, name=name, rx=re.compile(c["match"])))
        s = f["sections"]
        self.sec_rx, self.sec_bold, self.scope = re.compile(s["match"]), s.get("bold", True), s.get("scope", "title")
        self.sec_alone = s.get("alone", False)
        hb = s.get("heading_before")       # a block that is the NEXT section's heading (West Virginia's constitution)
        self.hb_rx = re.compile(hb["match"] if isinstance(hb, dict) else hb) if hb and (not isinstance(hb, dict) or hb.get("match")) else None
        self.hb_cls = hb.get("class") if isinstance(hb, dict) else None
        self.held_heading = None
        self.held_args = None
        self.moved = []                    # texts written in another place than the source's order (heading_before)
        self.cur_classes = frozenset()
        self.sec_cls = s.get("class")      # only blocks of this class can open a section (Nevada's constitution)
        self.heading_next = s.get("heading") == "next"   # the block after "§ 1.04.010." is its heading (Washington)
        self.awaiting_heading = None
        self.toc_rx = _regex_list(s.get("toc"))
        self.version_rx = re.compile(s["versions"]) if s.get("versions") else None
        self.versions = []                 # labels that repeat as another version (effective-date variants)
        n = f.get("notes", {})
        self.annot_rx = re.compile(n["annotation"]) if n.get("annotation") else None
        self.note_rules = []               # (element, topic, regex or None, class or None)
        for tag, v in n.items():
            if tag in ("annotation", "close"):
                continue
            for r in (v if isinstance(v, list) else [v]):
                if isinstance(r, str):
                    self.note_rules.append((tag, tag, re.compile(r), None))
                else:
                    self.note_rules.append((r.get("tag", tag), r.get("topic", tag),
                                            re.compile(r["match"]) if r.get("match") else None, r.get("class")))
        self.root = Node("lawDoc", ident=root_ident or self.doc, rank=0)
        # [notes].close: blocks that end the open section or its notes and stand as a cross heading in the
        # container (a group heading between sections: Nevada after a history line, Oregon's "COURT FACILITIES"
        # with no markup of its own) -- a regex, {class = ...}, {heading = true} (only heading blocks) or
        # {heading = false} (never a heading block: a form's centered title inside a section), combined freely
        self.close_rules = []
        for r in (n.get("close") or []) if isinstance(n.get("close"), list) else ([n["close"]] if n.get("close") else []):
            if isinstance(r, str):
                self.close_rules.append((re.compile(r), None, None))
            else:
                self.close_rules.append((re.compile(r["match"]) if r.get("match") else None, r.get("class"), r.get("heading")))
        self.stack = [self.root]           # open containers, root first
        self.section = None                # open section
        self.levels = []                   # open numbered paragraphs inside the section
        self.in_notes = None               # the open note item while in a section's / container's notes
        self.ids = {}
        self.pending_heading = None        # container waiting for its heading paragraph

    # ---------------------------------------------------------------- helpers

    def here(self):
        return self.levels[-1] if self.levels else self.section or self.stack[-1]

    def unique(self, ident, where, version=False):
        n = self.ids.get(ident, 0) + 1
        self.ids[ident] = n
        if n == 1:
            return ident
        if version:
            self.versions.append("%s: version %d kept as %s_v%d" % (where, n, ident, n))
            return "%s_v%d" % (ident, n)
        self.ladder.anomalies.append("%s: identifier %s repeats (version %d kept as %s_v%d)" % (where, ident, n, ident, n))
        return "%s_v%d" % (ident, n)

    def close_section(self):
        self.section, self.levels, self.in_notes = None, [], None

    # ---------------------------------------------------------------- containers

    def container(self, text, heading=True):
        """A heading block matching a [containers] regex opens that container; with any = true in the rule,
        a non-heading block does too (South Dakota's ARTICLE XXI, the one article heading not centered)."""
        for c in self.containers:
            if not heading and not c.get("any"):
                continue
            m = c["rx"].match(text)
            if not m:
                continue
            g = m.groupdict()
            num = g.get("num") or ""
            fields = {"num": num, "num_arabic": roman_value(num) or num}
            self.close_section()
            while len(self.stack) > 1 and self.stack[-1].rank >= c["rank"]:
                self.stack.pop()
            parent = self.stack[-1]
            local = c.get("id", "").format(**fields)
            ident = self.unique(parent.ident + ("/" + local if local else ""), text)
            heading_self = c.get("heading") == "self"
            node = Node(c["tag"], num_text="" if heading_self else text, num=num,
                        heading=g.get("heading", "") if heading_self else "", ident=ident, rank=c["rank"])
            parent.add("node", node)
            self.stack.append(node)
            self.pending_heading = None if heading_self else node
            return True
        return False

    # ---------------------------------------------------------------- sections

    def section_start(self, text, bold):
        if self.sec_cls and self.sec_cls not in self.cur_classes:
            return False
        probe = bold if self.sec_bold else text
        m = self.sec_rx.match(probe) if probe else None
        if not m or (self.sec_alone and text != probe):
            return False
        self.close_section()
        num = re.sub(r"\s+", "_", re.sub(r"\s*\.\s*", ".", m.group("num").strip()))   # "44. 1" -> 44.1; "1-2 to 1-5" -> 1-2_to_1-5
        num = num.replace("\u2013", "-").replace("\u2014", "-")                       # Maryland's "1\u2013101" -> 1-101
        heading = (m.groupdict().get("heading") or "").strip()
        num_text = probe[:m.start("heading")].strip() if m.groupdict().get("heading") is not None else probe[:m.end()].strip()
        if not self.sec_bold:              # matched on the text itself: what follows the match is the section's body
            probe = text[:m.end()]
        if not heading and self.held_heading is not None:   # [sections].heading_before: the block before was its heading
            heading, self.held_heading = self.held_heading, None
            self.moved.append(heading)
            heading_moved = True           # written after the number (USLM order); marked, so verify reads it back
        else:                              # in source order before comparing
            heading_moved = False
        parent = self.stack[-1]
        title = next((n for n in self.stack if n.tag == "title"), self.root)
        base = parent.ident if self.scope == "container" else title.ident
        is_version = bool(self.version_rx and (self.version_rx.match(text[len(probe):].strip())
                                               or self.version_rx.search(heading)))    # marker in the heading (NC)
        node = Node("section", num_text=num_text, num=num, heading=heading,
                    ident=self.unique("%s/s%s" % (base, num), num_text, version=is_version), rank=90)
        parent.add("node", node)
        self.section = node
        if heading_moved:
            node.label = "heading-before"
        if self.heading_next and not heading:
            self.awaiting_heading = node
        rest = text[len(probe):].strip() if text.startswith(probe) else text[len(num_text):].strip()
        if not text.startswith(probe):     # bold run was not a prefix of the text (odd markup): keep it all
            node.heading, rest = "", text[len(num_text):].strip()
            self.ladder.anomalies.append("%s: section opening not at the paragraph start; heading left in the text" % num_text)
        if rest:
            self.body(rest)
        return True

    # ---------------------------------------------------------------- numbered paragraphs

    def body(self, text, indent=None):
        labels, raws, rest = split_labels(text, self.ladder.dotted)
        placed = 0
        for label, raw in zip(labels, raws):
            open_labels = [None] * (self.levels[-1].rank + 1 if self.levels else 0)
            for n in self.levels:
                open_labels[n.rank] = n.label
            hint = indent if indent is not None and label is labels[0] else None   # a line's indent is its first label's
            where = "%s %s" % (self.section.num if self.section else "", "".join("(%s)" % n.num for n in self.levels))
            prev = self.here().last_text() if self.levels or self.section else ""
            same = [n.rank for n in self.levels if n.label == label]
            is_version = bool(same and self.version_rx and self.version_rx.match(rest.lstrip()) and raw is raws[-1])
            d = same[-1] if is_version else self.ladder.place(label, open_labels, prev, where.strip(), hint)
            if d is None:
                break
            while self.levels and self.levels[-1].rank >= d:
                self.levels.pop()
            if self.levels and self.levels[-1].rank < d - 1:   # skipped a level: say so, keep going under the deepest open one
                self.ladder.anomalies.append("%s (%s): skips from depth %d to %d" % (where.strip(), label, self.levels[-1].rank, d))
            parent = self.levels[-1] if self.levels else self.section
            tag = self.f["ladder"]["tags"][d] if d < len(self.f["ladder"]["tags"]) else "level"
            num = label.rstrip(".") if label.endswith(".") else label     # dotted "1." -> 1
            node = Node(tag, num_text=raw.strip(), num=num, label=label,
                        ident=self.unique("%s/%s" % (parent.ident, num), parent.ident + "/" + num, version=is_version))
            node.rank = d
            parent.add("node", node)
            self.levels.append(node)
            placed += 1
        if placed < len(labels):                # the text in parentheses was not a label: put it back
            rest = "".join(raws[placed:]) + rest
        rest = rest.strip()
        if rest:
            self.here().add("text", rest)

    # ---------------------------------------------------------------- driver

    def closes_notes(self, text, classes, heading):
        for rx, cls, head in self.close_rules:
            if (rx is None or rx.match(text)) and (cls is None or cls in classes) and (head is None or head == bool(heading)):
                return True
        return False

    def note_rule(self, text, classes):
        for tag, topic, rx, cls in self.note_rules:
            if (rx is None or rx.match(text)) and (cls is None or cls in classes):
                return tag, topic
        return None

    def start_note(self, tag, topic, text):
        node = self.section or self.stack[-1]
        item = ["note", tag, topic, [text]]
        node.items.append(item)
        self.in_notes = item

    def feed(self, text, bold, centered, classes=frozenset(), verbatim=False, indent=None):
        self.cur_classes = classes
        if self.held_heading is not None and not self.section_start_probe(text, bold):
            held, self.held_heading = self.held_heading, None          # not followed by a section: it goes the
            self._feed(*self.held_args)                                # normal way (notes.close, cross heading)
        if (self.hb_rx or self.hb_cls) and not verbatim and (self.hb_rx is None or self.hb_rx.match(text)) \
                and (self.hb_cls is None or self.hb_cls in classes) and not self.section_start_probe(text, bold):
            self.held_heading = text
            self.held_args = (text, bold, centered, classes, verbatim, indent)
            return
        self._feed(text, bold, centered, classes, verbatim, indent)

    def section_start_probe(self, text, bold):
        if self.sec_cls and self.sec_cls not in self.cur_classes:
            return False
        probe = bold if self.sec_bold else text
        m = self.sec_rx.match(probe) if probe else None
        return bool(m) and not (self.sec_alone and text != probe)

    def _feed(self, text, bold, centered, classes=frozenset(), verbatim=False, indent=None):
        if self.awaiting_heading is not None:
            node, self.awaiting_heading = self.awaiting_heading, None
            if not verbatim and not self.section_start_probe(text, bold) and not split_labels(text, self.ladder.dotted)[0]:
                node.heading = text
                if self.version_rx and self.version_rx.search(text) and re.search(r"_v\d+$", node.ident):
                    stale = [a for a in self.ladder.anomalies if node.ident in a]   # a repeat that is a version after all
                    for a in stale:
                        self.ladder.anomalies.remove(a)
                        self.versions.append(a.replace("repeats", "is another version"))
                return
        if verbatim:                            # a form or table: one text item where it stands, labels not read
            target = self.in_notes[3] if self.in_notes is not None else None
            if target is not None:
                target.append(text)
            else:
                self.here().add("text", text)
            return
        if self.container(text, centered):
            return
        if centered and self.pending_heading is not None and not self.pending_heading.heading:
            self.pending_heading.heading = text
            self.pending_heading = None
            return
        self.pending_heading = None
        if self.section_start(text, bold):
            return
        if centered and self.annot_rx is not None and self.annot_rx.match(text):
            self.start_note("note", "annotation", text)
            return
        rule = self.note_rule(text, classes)
        if rule and not (self.in_notes and self.in_notes[2] == "annotation" and rule[1] != "annotation"):
            self.start_note(rule[0], rule[1], text)
            return
        if self.closes_notes(text, classes, centered) and not self.section_start_probe(text, bold):
            # also with nothing open: a group heading between the table of contents and the first section
            self.in_notes = None                # [notes].close: this block ends the notes (a group heading after a
            node = self.section or self.stack[-1]   # history line, Nevada); it stands as a cross heading
            if self.section is not None:
                self.close_section()
            self.stack[-1].add("cross", text)
            return
        if self.in_notes is not None:           # everything to the next section/container is notes
            self.in_notes[3].append(text)
            return
        if self.section is None:                # container level: a table of contents, a centered line, or text
            node = self.stack[-1]
            if any(rx.match(text) for rx in self.toc_rx):
                last = node.items[-1] if node.items else None
                if last and last[0] == "note" and last[2] == "toc":
                    last[3].append(text)
                else:
                    node.items.append(["note", "note", "toc", [text]])
            elif centered:
                node.add("cross", text)
            elif len(self.stack) == 1:
                node.add("unmapped", text)
            else:
                node.add("text", text)
            return
        if centered:
            self.here().add("cross", text)
            return
        self.body(text, indent)


def _ord(node, types, d):
    from tribonian.uslm.tree import level_ordinal
    o = level_ordinal(types[d], node.label) if d < len(types) else None
    return o or (0, 0)


def parse(raw, formula, item_name="", root_ident=None):
    """raw HTML -> (root Node, Ladder with the ambiguity / anomaly log; .dropped: characters [html].drop removed)."""
    ladder = Ladder(formula["ladder"]["types"])
    b = Builder(formula, ladder, item_name, root_ident)
    doc, dropped = load(raw, formula)
    for text, opening, heading, classes, verbatim, indent in paragraphs(doc, formula):
        b.feed(text, opening, heading, classes, verbatim, indent)
    if b.held_heading is not None:           # a heading at the very end with no section after it
        b._feed(*b.held_args)
    ladder.moved = b.moved
    ladder.versions = b.versions
    ladder.dropped = dropped
    return b.root, ladder


def source_text(raw, formula):
    """Every character of the page body, straight from the HTML (less [html].drop): the conservation check's
    reference. Read from the element tree as a whole, not from paragraphs(), so text outside every block fails."""
    doc, _ = load(raw, formula)
    body = doc.find("body") if doc.tag != "body" else doc
    return norm((body if body is not None else doc).text_content())
