"""html_paragraphs.py -- parser family for Word-style HTML, where every line of the code is a <p>.

Structure comes from three things the publisher's formatting carries, all named in the state's formula:
    centered paragraphs   container headings (TITLE 26.5, ARTICLE 1, PART 2) via [containers] regexes;
                          the next centered paragraph is the heading unless heading = "self"
    a bold opening run    a section start, via [sections].match on the bold text
    a leading (label)     a numbered paragraph, placed on a level by the shared Ladder (tree.py)
Notes start at a paragraph matching [notes]; from there to the next section or container, everything is
notes (Source:, Editor's note, the case ANNOTATION). Centered lines inside notes stay in the notes.

Nothing is dropped: a paragraph that fits nowhere becomes ("unmapped", text) on the node in hand.
Used by: Colorado (C.R.S. and the Colorado Constitution).
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


def paragraphs(raw):
    """HTML -> [(text, bold_run, centered)]: text normalised, bold_run the paragraph's bold opening text."""
    doc = lhtml.fromstring(raw)
    out = []
    for p in doc.iter("p"):
        text = norm(p.text_content())
        if not text:
            continue
        style = (p.get("align") or "") + (p.get("style") or "")
        centered = "center" in style.lower()
        bold, done = [], False
        for t in p.xpath(".//text()"):
            owner = t.getparent()
            if t.is_tail:
                owner = owner.getparent()
            is_b = any(a.tag in ("b", "strong") for a in [owner] + list(owner.iterancestors()) if isinstance(a.tag, str))
            if not norm(t):
                if bold:
                    bold.append(" ")
                continue
            if not is_b:
                break
            bold.append(t)
        out.append((text, norm("".join(bold)), centered))
    return out


def _regex_list(v):
    return [re.compile(x) for x in ([v] if isinstance(v, str) else v or [])]


class Builder:
    def __init__(self, f, ladder, item_name):
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
        self.note_rx = [(tag, rx) for tag, v in n.items() if tag != "annotation" for rx in _regex_list(v)]
        self.root = Node("lawDoc", ident=self.doc, rank=0)
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
        num = re.sub(r"\s+", "", m.group("num"))          # "44. 1" -> "44.1"
        heading = m.group("heading").strip()
        num_text = probe[:m.start("heading")].strip()
        parent = self.stack[-1]
        base = parent.ident if self.scope == "container" else self.stack[min(1, len(self.stack) - 1)].ident
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

    def note_tag(self, text):
        for tag, rx in self.note_rx:
            if rx.match(text):
                return tag
        return None

    def start_note(self, tag, topic, text):
        node = self.section or self.stack[-1]
        item = ["note", tag, topic, [text]]
        node.items.append(item)
        self.in_notes = item

    def feed(self, text, bold, centered):
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
        tag = self.note_tag(text)
        if tag and not (self.in_notes and self.in_notes[2] == "annotation"):
            self.start_note(tag, tag, text)
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


def parse(raw, formula, item_name=""):
    """raw HTML -> (root Node, Ladder with the ambiguity / anomaly log)."""
    ladder = Ladder(formula["ladder"]["types"])
    b = Builder(formula, ladder, item_name)
    for text, bold, centered in paragraphs(raw):
        b.feed(text, bold, centered)
    ladder.versions = b.versions
    return b.root, ladder


def source_text(raw):
    """Every character of the page body, straight from the HTML: the conservation check's reference."""
    doc = lhtml.fromstring(raw)
    body = doc.find("body") if doc.tag != "body" else doc
    return norm((body if body is not None else doc).text_content())
