"""statelaw.py -- state law, fetched per state from its own publisher (scope-scotus-law.md §3).
One reader per state, written when a docket needs that state; the registry of sources is
data/state_law_sources.toml. Colorado so far:

    tribonian state fetch CO crs 26.5 00      C.R.S. titles (00 = the Colorado Constitution)
    tribonian state fetch CO crs 26.5 --force download again
    tribonian state list                      what is stored here

C.R.S.: the Office of Legislative Legal Services' per-title HTML (olls.info/crs/crs<edition>-title-NN.htm),
the edition current through the latest regular session. Personal use; republishing needs a statement to
the Committee on Legal Services (C.R.S. 2-5-118). Files: <law_dir>/co/crs/title-26.5.htm + .json.
robots.txt is obeyed: a disallowed URL is reported, not fetched.
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser
from datetime import date

from tribonian import cfr, tconfig

UA = cfr.UA
CRS_EDITION = 2026                    # the OLLS set current through the 2026 regular session
CRS_URL = "https://olls.info/crs/crs{edition}-title-{title}.htm"
_robots = {}


def state_dir(st):
    return cfr.law_dir() / st.lower()


def allowed(url):
    host = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if host not in _robots:
        _robots[host] = cfr.read_robots(host, UA)
    return _robots[host].can_fetch(UA, url)


def download(url, path):
    """url -> path (via a .part file). Returns bytes written, or exits with the reason."""
    if not allowed(url):
        sys.exit("robots.txt disallows %s; not fetched" % url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        sys.exit("%s: HTTP %d %s" % (url, e.code, e.reason))
    except urllib.error.URLError as e:
        sys.exit("%s: %s" % (url, e.reason))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, path)
    return len(body)


def crs_title_name(t):
    """'0' -> '00', '1' -> '01', '26.5' -> '26.5' (the OLLS file names)."""
    whole, _, frac = t.partition(".")
    return "%02d" % int(whole) + ("." + frac if frac else "")


def local_meta(path):
    meta = path.with_suffix(".json")
    if not (path.exists() and meta.exists()):
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except ValueError:
        return None


def fetch_co_crs(titles, force=False, edition=CRS_EDITION):
    for t in titles:
        name = crs_title_name(t)
        path = state_dir("CO") / "crs" / ("title-%s.htm" % name)
        have = local_meta(path)
        if have and have.get("edition") == edition and not force:
            print("C.R.S. title %s: local, %s edition (fetched %s); --force to replace" % (name, edition, have["fetched"]))
            continue
        url = CRS_URL.format(edition=edition, title=name)
        print("C.R.S. title %s (%d edition) ..." % (name, edition), flush=True)
        n = download(url, path)
        path.with_suffix(".json").write_text(json.dumps(
            {"edition": edition, "fetched": date.today().isoformat(), "url": url, "bytes": n}, indent=1), encoding="utf-8")
        print("  wrote %s (%.1f MB)" % (path, n / 1e6))
        time.sleep(1)


FETCHERS = {("CO", "crs"): fetch_co_crs}


def cmd_fetch(args):
    key = (args.state.upper(), args.code.lower())
    if key not in FETCHERS:
        sys.exit("no reader for %s %s yet (have: %s)" % (key[0], key[1], ", ".join("%s %s" % k for k in FETCHERS)))
    FETCHERS[key](args.items, args.force)


def cmd_list(args):
    base = cfr.law_dir()
    rows = sorted(p for p in base.glob("*/*/*.json") if p.parts[-3] != "cfr") if base.is_dir() else []
    if not rows:
        print("no state law in %s (tribonian state fetch CO crs <title>)" % base)
        return
    for p in rows:
        m = json.loads(p.read_text(encoding="utf-8"))
        print("%-3s %-4s %-14s %s edition  fetched %s  %6.1f MB" % (p.parts[-3].upper(), p.parts[-2], p.stem,
                                                                 m.get("edition", "?"), m["fetched"], m["bytes"] / 1e6))
    print("in %s" % base)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download a state's code, title by title, unless already local")
    f.add_argument("state", help="postal code, e.g. CO")
    f.add_argument("code", help="crs (Colorado Revised Statutes; title 00 is the constitution)")
    f.add_argument("items", nargs="+", help="titles, e.g. 26.5 00")
    f.add_argument("--force", action="store_true", help="download again whatever is local")
    f.set_defaults(func=cmd_fetch)
    l = sub.add_parser("list", help="what is stored here")
    l.set_defaults(func=cmd_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
