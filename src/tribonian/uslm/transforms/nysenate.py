"""NY Open Legislation JSON -> HTML blocks for the html-paragraphs family (reference for the shared "json-tree" step).

Input: /api/3/laws/{lawId}?full=true  (result.documents = root node, children in documents.items, recursive)
   or  /api/3/laws/{lawId}/{locationId} (result = one node, no children).
Each node's `text` is the LBDC fixed-width (80-column) print: the JSON string holds a LITERAL backslash-n ("\\n" in
the JSON) as its line separator, not a newline. Rules:
  * section (docType SECTION): a line that starts with a space opens a paragraph; a line at column 0 continues the
    previous one (joined with one space; no end-of-line hyphenation seen). The first paragraph's
    "§ <num>. <title>." prefix is wrapped in <b> when it matches the node title (whitespace-normalised).
  * any other node (CHAPTER ARTICLE TITLE PART ... PREAMBLE): layout text kept line by line; a line indented
    >= 8 spaces is centred (a heading or the TOC's continuation column -- the TOC lines are matched by [sections].toc).
"""
import html, json, re, sys

SEP = "\\n"          # two characters: backslash, n


def _ws(s):
    return " ".join(s.split())


def paragraphs(text):
    out = []
    for line in text.split(SEP):
        if not line.strip():
            continue
        if line[:1] == " " or not out:
            out.append(line.strip())
        else:
            out[-1] += " " + line.strip()
    return out


def _title_end(p, start, t, cut):
    """end index (after the period) of title t in paragraph p at start, whitespace-insensitive; a truncated
    title (cut) runs on to the first period followed by a space or the end."""
    m = re.compile(r"\s*".join(map(re.escape, t.split()))).match(p, start)
    if not m:
        return None
    end = re.compile(r"\.(?=\s|$)").search(p, m.end()) if cut else re.compile(r"\.").match(p, m.end())
    return end.end() if end else None


def section_html(node):
    paras = paragraphs(node["text"] or "")
    out = []
    for i, p in enumerate(paras):
        e = html.escape(p, quote=False)
        if i == 0:
            m = re.match(r"(§\s*[\w.-]+\.\s+)", p)
            t = _ws(node.get("title") or "")
            cut = t.endswith("...")                 # the API truncates long titles to ~100 chars + "..."
            t = t[:-3].rstrip() if cut else t
            k = _title_end(p, m.end(), t, cut) if m and t else None
            if k is not None:
                e = "<b>%s</b>%s" % (html.escape(p[:k], quote=False), html.escape(p[k:], quote=False))
            elif m:                                 # title not found: bold the number only (heading stays in text)
                n = len(m.group(1).rstrip())
                e = "<b>%s</b>%s" % (html.escape(p[:n], quote=False), html.escape(p[n:], quote=False))
        out.append("<p>%s</p>" % e)
    return out


def container_html(node):
    """Heading lines (indented >= 8, before any column-0 line) are centred; from the first column-0 line on
    (the chapter's enacting clause, an article's table of sections) the lines are front matter: the first gets
    class="front-first" (a [notes] rule by class), the rest plain, so they stay in that note."""
    out, front = [], False
    for line in (node["text"] or "").split(SEP):
        if not line.strip():
            continue
        lead = len(line) - len(line.lstrip(" "))
        e = html.escape(line.strip(), quote=False)
        if not front and lead >= 8:
            out.append('<p align="center">%s</p>' % e)
        elif not front:
            front = True
            out.append('<p class="front-first">%s</p>' % e)
        else:
            out.append("<p>%s</p>" % e)
    return out


def walk(node):
    yield node
    for k in (node.get("documents") or {}).get("items", []):
        yield from walk(k)


def to_html(raw):
    d = json.loads(raw)
    r = d["result"]
    root = r.get("documents", r)
    out = []
    for n in walk(root):
        out += section_html(n) if n["docType"] == "SECTION" else container_html(n)
    return ('<html><body><div id="law">\n%s\n</div></body></html>\n' % "\n".join(out)).encode("utf-8")


if __name__ == "__main__":
    sys.stdout.buffer.write(to_html(open(sys.argv[1], "rb").read()))
