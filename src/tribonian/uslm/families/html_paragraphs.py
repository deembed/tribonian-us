"""html_paragraphs.py -- parser family for HTML where each line of the code is its own block element.

The formula's [html] table says which elements carry what (defaults are Colorado's Word-filtered HTML):
    blocks    the elements that are lines of text                       ["p"]
    headings  blocks that are headings (containers, cross headings)    ["p:centered"]
    opening   the leading run that can open a section                  ["b", "strong"]
    verbatim  blocks taken whole, no label parsing (a statutory form    []
              set as a table): one text item, cells joined by spaces
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


HTML_DEFAULTS = {"blocks": ["p"], "headings": ["p:centered"], "opening": ["b", "strong"], "verbatim": [], "drop": []}


def selector(sel):
    """'tag', 'tag.class', '.class', 'tag:centered', 'tag:noclass' -> predicate on an lxml element."""
    sel, _, pseudo = sel.partition(":")
    centered, noclass = pseudo == "centered", pseudo == "noclass"
    tag, _, cls = sel.partition(".")

    def match(el):
        if not isinstance(el.tag, str) or (tag and el.tag != tag):
            return False
        if cls and cls not in (el.get("class") or "").split():
            return False
        if noclass and (el.get("class") or "").strip():
            return False
        if centered and "center" not in ((el.get("align") or "") + (el.get("style") or "")).lower():
            return False
        return True
    return match


def _any(sels):
    preds = [selector(s) for s in sels]
    return lambda el: any(p(el) for p in preds)


def html_cfg(formula):
    return dict(HTML_DEFAULTS, **formula.get("html", {}))


def load(raw, formula):
    """-> (the parsed page with [html].drop removed, {selector: characters dropped})."""
    cfg = html_cfg(formula)
    doc = lhtml.fromstring(raw)
    dropped = {}
    for sel in cfg["drop"]:
        pred = selector(sel)
        for el in [e for e in doc.iter() if pred(e)]:
            dropped[sel] = dropped.get(sel, 0) + len(re.sub(r"\s+", "", norm(el.text_content())))
            el.drop_tree()                 # the element and its text go; its tail stays with the parent
    return doc, dropped


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


def paragraphs(doc, formula):
    """-> [(text, opening_run, is_heading, classes, verbatim)] in document order; a block inside a block
    (a <p> in a verbatim table) counts once, as part of the outer one."""
    cfg = html_cfg(formula)
    is_block, is_head, is_open, is_verb = _any(cfg["blocks"]), _any(cfg["headings"]), _any(cfg["opening"]), _any(cfg["verbatim"])
    any_block = lambda e: is_block(e) or is_head(e) or is_verb(e)
    out = []
    for el in doc.iter():
        if not any_block(el) or any(any_block(a) for a in el.iterancestors()):
            continue
        if is_verb(el):
            cells = el.xpath(".//td|.//th") or [el]
            text = norm(" ".join(c.text_content() for c in cells))
            if text:
                out.append((text, "", False, set((el.get("class") or "").split()), True))
            continue
        text = norm(el.text_content())
        if text:
            out.append((text, _opening(el, is_open), is_head(el), set((el.get("class") or "").split()), False))
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
        self.toc_rx = _regex_list(s.get("toc"))
        self.version_rx = re.compile(s["versions"]) if s.get("versions") else None
        self.versions = []                 # labels that repeat as another version (effective-date variants)
        n = f.get("notes", {})
        self.annot_rx = re.compile(n["annotation"]) if n.get("annotation") else None
        self.note_rules = []               # (element, topic, regex or None, class or None)
        for tag, v in n.items():
            if tag == "annotation":
                continue
            for r in (v if isinstance(v, list) else [v]):
                if isinstance(r, str):
                    self.note_rules.append((tag, tag, re.compile(r), None))
                else:
                    self.note_rules.append((r.get("tag", tag), r.get("topic", tag),
                                            re.compile(r["match"]) if r.get("match") else None, r.get("class")))
        self.root = Node("lawDoc", ident=root_ident or self.doc, rank=0)
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

    def container(self, text):
        for c in self.containers:
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
        probe = bold if self.sec_bold else text
        m = self.sec_rx.match(probe) if probe else None
        if not m or (self.sec_alone and text != probe):
            return False
        self.close_section()
        num = re.sub(r"\s+", "_", re.sub(r"\s*\.\s*", ".", m.group("num").strip()))   # "44. 1" -> 44.1; "1-2 to 1-5" -> 1-2_to_1-5
        heading = m.group("heading").strip()
        num_text = probe[:m.start("heading")].strip()
        parent = self.stack[-1]
        title = next((n for n in self.stack if n.tag == "title"), self.root)
        base = parent.ident if self.scope == "container" else title.ident
        is_version = bool(self.version_rx and self.version_rx.match(text[len(probe):].strip()))
        node = Node("section", num_text=num_text, num=num, heading=heading,
                    ident=self.unique("%s/s%s" % (base, num), num_text, version=is_version), rank=90)
        parent.add("node", node)
        self.section = node
        rest = text[len(probe):].strip() if text.startswith(probe) else text[len(num_text):].strip()
        if not text.startswith(probe):     # bold run was not a prefix of the text (odd markup): keep it all
            node.heading, rest = "", text[len(num_text):].strip()
            self.ladder.anomalies.append("%s: section opening not at the paragraph start; heading left in the text" % num_text)
        if rest:
            self.body(rest)
        return True

    # ---------------------------------------------------------------- numbered paragraphs

    def body(self, text):
        labels, raws, rest = split_labels(text)
        placed = 0
        for label, raw in zip(labels, raws):
            open_ords = [None] * (self.levels[-1].rank + 1 if self.levels else 0)
            for n in self.levels:
                open_ords[n.rank] = _ord(n, self.ladder.types, n.rank)
            where = "%s %s" % (self.section.num if self.section else "", "".join("(%s)" % n.num for n in self.levels))
            prev = self.here().last_text() if self.levels or self.section else ""
            same = [n.rank for n in self.levels if n.num == label]
            is_version = bool(same and self.version_rx and self.version_rx.match(rest.lstrip()) and raw is raws[-1])
            d = same[-1] if is_version else self.ladder.place(label, open_ords, prev, where.strip())
            if d is None:
                break
            while self.levels and self.levels[-1].rank >= d:
                self.levels.pop()
            if self.levels and self.levels[-1].rank < d - 1:   # skipped a level: say so, keep going under the deepest open one
                self.ladder.anomalies.append("%s (%s): skips from depth %d to %d" % (where.strip(), label, self.levels[-1].rank, d))
            parent = self.levels[-1] if self.levels else self.section
            tag = self.f["ladder"]["tags"][d] if d < len(self.f["ladder"]["tags"]) else "level"
            node = Node(tag, num_text=raw.strip(), num=label,
                        ident=self.unique("%s/%s" % (parent.ident, label), parent.ident + "/" + label, version=is_version))
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

    def feed(self, text, bold, centered, classes=frozenset(), verbatim=False):
        if verbatim:                            # a form or table: one text item where it stands, labels not read
            target = self.in_notes[3] if self.in_notes is not None else None
            if target is not None:
                target.append(text)
            else:
                self.here().add("text", text)
            return
        if centered and self.container(text):
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
        self.body(text)


def _ord(node, types, d):
    from tribonian.uslm.tree import level_ordinal
    o = level_ordinal(types[d], node.num) if d < len(types) else None
    return o or (0, 0)


def parse(raw, formula, item_name="", root_ident=None):
    """raw HTML -> (root Node, Ladder with the ambiguity / anomaly log; .dropped: characters [html].drop removed)."""
    ladder = Ladder(formula["ladder"]["types"])
    b = Builder(formula, ladder, item_name, root_ident)
    doc, dropped = load(raw, formula)
    for text, opening, heading, classes, verbatim in paragraphs(doc, formula):
        b.feed(text, opening, heading, classes, verbatim)
    ladder.versions = b.versions
    ladder.dropped = dropped
    return b.root, ladder


def source_text(raw, formula):
    """Every character of the page body, straight from the HTML (less [html].drop): the conservation check's
    reference. Read from the element tree as a whole, not from paragraphs(), so text outside every block fails."""
    doc, _ = load(raw, formula)
    body = doc.find("body") if doc.tag != "body" else doc
    return norm((body if body is not None else doc).text_content())
