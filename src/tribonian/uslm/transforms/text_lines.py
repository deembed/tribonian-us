"""Plain text (one paragraph per line; New Jersey's bulk .TXT files) -> HTML: every non-blank line one <p>, escaped,
in order. Leading tabs/spaces are layout, kept only as data-indent."""
import html
import re


def to_html(raw, formula=None):
    enc = (formula or {}).get("text_encoding", "cp1252")
    out = ['<html><body><div id="text">']
    for line in raw.decode(enc).splitlines():
        if not line.strip():
            continue
        lead = re.match(r"[\t ]*", line).group(0)
        out.append('<p%s data-indent="%d">%s</p>' % (' class="flush"' if not lead else "",   # flush: column 0
                                                      lead.count("\t") * 4 + lead.count(" "), html.escape(line.strip())))
    out.append("</div></body></html>")
    return "\n".join(out).encode("utf-8")
