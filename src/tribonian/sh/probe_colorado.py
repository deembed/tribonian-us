#!/usr/bin/env python3
"""probe_colorado.py -- one-off discovery for the Colorado fetcher (scope-scotus-law.md §3): saves the
pages that say where the C.R.S. title files and the 8 CCR 1404-1 rule document live, so the fetcher
can be written against real URLs. robots.txt obeyed, a second between requests, at most 25 pages.

    python3 src/tribonian/sh/probe_colorado.py        -> law/co/probe/NN-<name>.html + index.txt
"""
import re, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from pathlib import Path

UA = "tribonian-us/0.1 (personal legal formatter; one request per second)"
OUT = Path("law/co/probe")
START = ["https://www.sos.state.co.us/CCR/NumericalDeptList.do"]
# links worth following from a saved page: the early-childhood department, rule 1404-1, its documents
FOLLOW = re.compile(r"(?i)early\s*childhood|1404|DisplayRule|GenerateRulePdf|ruleVersion|ruleinfo")
robots, seen, log = {}, set(), []


def ok(url):
    host = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if host not in robots:                    # read with our agent: Python's default one gets 403 from the SOS
        rp = urllib.robotparser.RobotFileParser(host + "/robots.txt")
        try:
            with urllib.request.urlopen(urllib.request.Request(host + "/robots.txt", headers={"User-Agent": UA}), timeout=30) as r:
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
        except Exception:
            rp.parse([])
        robots[host] = rp
    return robots[host].can_fetch(UA, url)


def get(url):
    if not ok(url):
        log.append("robots.txt disallows %s" % url)
        return None, None
    time.sleep(1)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
            return r.geturl(), r.read()
    except Exception as e:
        log.append("%s: %s" % (url, e))
        return None, None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    queue = [(u, 0) for u in START]
    n = 0
    while queue and n < 25:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        final, body = get(url)
        if body is None:
            continue
        n += 1
        ext = ".pdf" if body[:4] == b"%PDF" else ".html"
        name = "%02d-%s%s" % (n, re.sub(r"[^A-Za-z0-9]+", "-", urllib.parse.urlsplit(final).path.strip("/"))[-60:], ext)
        (OUT / name).write_bytes(body)
        log.append("%s  %s  (%d bytes)" % (name, final, len(body)))
        print(log[-1], flush=True)
        if ext == ".pdf" or depth >= 3:
            continue
        html = body.decode("utf-8", "replace")
        for m in re.finditer(r"""<a\b[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", html, re.I | re.S):
            href, text = m.group(1), re.sub(r"<[^>]+>|\s+", " ", m.group(2))
            if FOLLOW.search(href + " " + text):
                queue.append((urllib.parse.urljoin(final, href.replace("&amp;", "&")), depth + 1))
    (OUT / "index.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    print("saved %d pages in %s (index.txt lists them)" % (n, OUT))


if __name__ == "__main__":
    main()
