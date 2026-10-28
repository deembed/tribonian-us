"""Print the 'Sources by state' table of DATASOURCES.md from the formulas."""
import urllib.parse
from tribonian import statelaw as s
rows = {}
for (st, code), f in sorted(s.formulas().items()):
    url = f.get("url") or (f.get("layers") or [{}])[0].get("url") or f.get("list_url") or ""
    host = urllib.parse.urlsplit(url.replace("{", "").replace("}", "")).netloc
    how = f.get("fetch", "") + (" (range)" if f.get("remote_members") else "") + (" (manual)" if f.get("manual") else "") \
        + (" (postback)" if f.get("postback") else "")
    fmt = f.get("assemble") or f.get("transform") or "html"
    rows.setdefault(st, []).append("%s: %s, %s, %s" % (code, host, how, fmt))
print("| State | Code: host, fetch, format |\n|---|---|")
for st in sorted(rows):
    print("| %s | %s |" % (st, "<br>".join(rows[st])))
