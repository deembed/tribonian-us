"""build.py -- build every PDF that is missing or older than its source, in parallel.

    source                  ->  output
    xml/*.xml               ->  pdf/<n>-usc-ch<NN>.pdf, one per chapter   + md/ notes
    cases/*.json            ->  pdf/[<folder>/]<case>.pdf                 + md-cases/ note
    cases/recap/*.json      ->  pdf/[<folder>/]<docket>.pdf               + md-cases/ notes

So: fetch cases (tribonian case fetch / case recap fetch) or drop in XML, then run this.

    tribonian build                build everything out of date
    tribonian build cases          only cases/ and cases/recap/   (or: usc)
    tribonian build --todo         list what would be built; build nothing
    tribonian build --stubs        first turn stub notes into full cases (tribonian map stubs),
                                   then build; STUB_ARGS="--api --max 20" passes options
    FORCE=1 tribonian build        rebuild regardless of dates (after changing a generator)
    tribonian build list.txt       build exactly the jobs in a list, always
    tribonian build build/failed_jobs.txt   retry what failed last run
    JOBS=8 tribonian build         parallel jobs (default: number of cores)
    STYLE=rails / FONT=... / EXTRA="--credits --notes" (US Code only) / MD_ONLY=1

"Out of date": state/built.tsv records <job> -> <pdf> for every success. A job is built when it
has no record, its PDF is gone, or its source is newer than the PDF. Failures are never recorded,
so the next run tries them again. The list of jobs that were due is kept in build/todo.txt.

list.txt: one job per line. Any of
    cases/463-u-s-29_motor-vehicle-mfrs....json          (a case or cases/recap/ docket)
    xml/usc05.xml /us/usc/t5/s552 /us/usc/t5/s552a      (sections -> one PDF)
    xml/usc05.xml /us/usc/t5/ptI/ch5                    (a chapter)
    xml/usc05.xml                                        (a whole title)
    usc|case|recap <args...>                             (a job line as in build/todo.txt)
Lines starting with # are ignored.

Runs in the work folder (tribonian build changes to it). Pure Python, so the same on Linux, macOS
and Windows; the only outside tool is pdflatex.
"""
import argparse, os, re, shlex, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

MANIFEST = Path("state/built.tsv")


def env(name, default=""):
    return os.environ.get(name) or default


PY = env("TRIBONIAN_PY", sys.executable)        # the interpreter tribonian runs under, so jobs see its packages
STYLE = env("STYLE", "boxes")
EXTRA = shlex.split(env("EXTRA"))               # e.g. EXTRA="--credits --notes"
FONT = env("FONT", "XCharter")
MD_ONLY = bool(env("MD_ONLY"))                  # markdown only, skip pdflatex (ignores dates, records nothing)
FORCE = bool(env("FORCE"))
SCRATCH = Path(env("SCRATCH", "build"))         # per-job scratch dirs
PDFLATEX_TIMEOUT = int(env("PDFLATEX_TIMEOUT", "1800"))


def tribonian(*args):
    """Run `python -m tribonian ...` -> (returncode, stdout+stderr)."""
    p = subprocess.run([PY, "-m", "tribonian", *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode, p.stdout


def tail(out, n=3):
    return "\n".join("     " + l for l in out.rstrip("\n").split("\n")[-n:])


# ---------------------------------------------------------------- workers: each returns (status, pdf or None, text)
# status is ok | FAIL | skip; text is the line(s) shown, as in the old shell script.

def build_usc(job, xml, sel):
    """One US Code PDF, in its own scratch dir so parallel jobs never see each other's files."""
    work = Path(tempfile.mkdtemp(prefix="job.", dir=SCRATCH))
    try:
        rc, out = tribonian("usc", xml, *sel, "--out", str(work), "--style", STYLE, "--font", FONT, *EXTRA)
        if rc == 3:                    # nothing the generator handles: a stamp stands in for the PDF until the XML changes
            Path("state/stamps").mkdir(parents=True, exist_ok=True)
            stamp = "state/stamps/%s.skip" % re.sub(r"[^A-Za-z0-9._-]", "_", job)
            Path(stamp).touch()
            return "skip", stamp, "skip %s   (nothing the generator handles)" % job
        if rc != 0:
            return "FAIL", None, "FAIL generate  %s\n%s" % (job, tail(out))
        m = re.findall(r"^wrote .*[/\\]([^/\\]*)\.tex$", out, re.M)   # the generator names the file; read it back
        if not m:
            return "FAIL", None, "FAIL generate  %s   (no .tex reported)" % job
        base = m[-1]
        if not MD_ONLY:
            for _ in (1, 2):
                try:
                    r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                                        "-output-directory", str(work), str(work / (base + ".tex"))],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=PDFLATEX_TIMEOUT)
                    ok = r.returncode == 0
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    ok = False
                if not ok:
                    if (work / (base + ".log")).exists():
                        shutil.copyfile(work / (base + ".log"), Path("build") / (base + ".log"))
                    return "FAIL", None, "FAIL pdflatex  %s   (see build/%s.log)" % (job, base)
            if not (work / (base + ".pdf")).exists():
                return "FAIL", None, "FAIL pdflatex  %s   (no pdf)" % job
            os.replace(work / (base + ".pdf"), Path("pdf") / (base + ".pdf"))
        for f in work.rglob("*.md"):            # markdown keeps the t<NN>/ch<NN> tree the generator built
            dest = Path("md") / f.relative_to(work)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(f, dest)
        return "ok", "pdf/%s.pdf" % base, "ok   pdf/%s.pdf" % base
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_case(job, kind, json_path):
    """One case (tribonian case build) or RECAP docket (tribonian case recap build)."""
    args = ["case"] + (["recap"] if kind == "recap" else []) + ["build", json_path, "--style", STYLE, "--font", FONT]
    rc, out = tribonian(*args, *(["--no-pdf"] if MD_ONLY else []))
    if rc != 0:
        return "FAIL", None, "FAIL generate  %s\n%s" % (job, tail(out))
    if MD_ONLY:
        return "ok", None, "ok   %s   (markdown only)" % json_path
    pdfs = re.findall(r"^wrote (pdf/.*\.pdf)$", out, re.M)   # printed only when pdflatex succeeded
    if not pdfs:
        lines = out.split("\n")
        detail = [l for i, l in enumerate(lines) if any(lines[j].startswith("FAIL") for j in range(max(0, i - 3), i + 1))]
        return "FAIL", None, "FAIL pdflatex  %s\n%s" % (job, "\n".join("     " + l for l in detail))
    return "ok", pdfs[-1], "ok   %s" % pdfs[-1]


def run_job(job):
    kind, _, rest = job.partition(" ")
    parts = rest.split()
    try:
        if kind == "usc" and parts:
            return build_usc(job, parts[0], parts[1:])
        if kind in ("case", "recap") and parts:
            return build_case(job, kind, rest)
    except Exception as e:                       # one broken job must not stop the others
        return "FAIL", None, "FAIL error     %s\n     %s: %s" % (job, type(e).__name__, e)
    return "FAIL", None, "FAIL unknown   %s" % job


# ---------------------------------------------------------------- the jobs

def chapter_cache(xml):
    return Path("build/chapters") / (Path(xml).stem + ".txt")


def list_usc(xml):
    """One XML's chapter jobs, cached in build/chapters/ until the XML changes."""
    cache = chapter_cache(xml)
    if cache.exists() and os.path.getmtime(xml) <= os.path.getmtime(cache):
        return
    rc, out = tribonian("usc", xml, "--list-chapters")
    if rc != 0:
        return                                    # no cache: built as one --all job
    chapters = [l.split("\t")[0] for l in out.splitlines() if l.strip()]
    lines = ["usc %s --chapter %s" % (xml, ch) for ch in chapters] or ["usc %s --all" % xml]
    tmp = cache.with_suffix(".txt.%d" % os.getpid())
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, cache)


def list_line(first, ids):
    """A line of a list file -> a job line."""
    if first in ("usc", "case", "recap"):
        return ("%s %s" % (first, ids)).rstrip()
    norm = first.replace("\\", "/")
    if norm.endswith(".json"):
        return ("recap " if "/recap/" in norm else "case ") + first
    if not ids:
        return "usc %s --all" % first
    if "/ch" in ids and " " not in ids:
        return "usc %s --chapter %s" % (first, ids)
    return "usc %s --section %s" % (first, ids)


def read_manifest():
    built = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            j, sep, p = line.partition("\t")
            if sep:
                built[j] = p
    return built


def due(job, built):
    """No record, PDF gone, or source newer than the PDF."""
    if FORCE or MD_ONLY:
        return True
    pdf = built.get(job)
    parts = job.split()
    src = parts[1] if len(parts) > 1 else ""
    if not pdf or not os.path.isfile(pdf):
        return True
    return os.path.exists(src) and os.path.getmtime(src) > os.path.getmtime(pdf)


def all_jobs(scope, jobs):
    out = []
    if scope != "usc":
        out += ["case %s" % p.as_posix() for p in sorted(Path("cases").glob("*.json")) if not p.name.startswith("_")]
        out += ["recap %s" % p.as_posix() for p in sorted(Path("cases/recap").glob("*.json"))]
    if scope != "cases":
        xmls = [p.as_posix() for p in sorted(Path("xml").glob("*.xml"))]
        with ThreadPoolExecutor(jobs) as ex:     # listing parses every changed XML, so in parallel too
            list(ex.map(list_usc, xmls))
        for x in xmls:
            c = chapter_cache(x)
            out += [l for l in c.read_text(encoding="utf-8").splitlines() if l.strip()] if c.exists() else ["usc %s --all" % x]
    return out


def migrate_state():
    """Durable state lives in state/, not the scratch dir (rm -rf build must never force a full rebuild).
    Moves the files from before 2026-10-04."""
    Path("state").mkdir(exist_ok=True)
    old = Path("build/built.tsv")
    if old.exists() and not MANIFEST.exists():
        os.replace(old, MANIFEST)
        if Path("build/stamps").is_dir():
            os.replace("build/stamps", "state/stamps")
        MANIFEST.write_text(MANIFEST.read_text(encoding="utf-8").replace("\tbuild/stamps/", "\tstate/stamps/"),
                            encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(prog="tribonian build", description=__doc__.split("\n\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", nargs="*", help="usc | cases | all (default), or a list file")
    ap.add_argument("--todo", action="store_true", help="list what would be built; build nothing")
    ap.add_argument("--stubs", action="store_true", help="first turn stub notes into full cases (tribonian map stubs)")
    a = ap.parse_args()
    scope, listfile = "all", None
    for w in a.what:
        if w in ("usc", "cases", "all"):
            scope = w
        else:
            listfile = w
    jobs = int(env("JOBS", str(os.cpu_count() or 1)))
    # Reproducible PDFs: pdflatex takes its dates and trailer ID from this instead of the clock, so an unchanged
    # source gives a byte-identical PDF and sync-remarkable leaves the tablet copy (and its pen annotations) alone.
    os.environ.setdefault("SOURCE_DATE_EPOCH", "0")

    migrate_state()
    for d in ("pdf", "md", "build", "build/chapters", SCRATCH):
        Path(d).mkdir(parents=True, exist_ok=True)
    MANIFEST.touch()
    if a.stubs and not a.todo:                   # stubs first, so the cases they become are in this build
        r = subprocess.run([PY, "-m", "tribonian", "map", "stubs", *shlex.split(env("STUB_ARGS"))])
        if r.returncode != 0:
            print("(stub fetch stopped early; building what there is)")

    if listfile:                                  # a list is built as given: no date check
        todo = []
        for line in Path(listfile).read_text(encoding="utf-8").splitlines():
            first, _, ids = line.strip().partition(" ")
            if first and not first.startswith("#"):
                todo.append(list_line(first, ids.strip()))
    else:
        known = all_jobs(scope, jobs)
        built = read_manifest()
        todo = [j for j in known if due(j, built)]
        print("%d PDFs known, %d out of date" % (len(known), len(todo)))
    Path("build/todo.txt").write_text("".join(j + "\n" for j in todo), encoding="utf-8")

    if a.todo:
        print("".join(j + "\n" for j in todo), end="")
        return 0
    total = len(todo)
    if total == 0:
        print("nothing to build")
        return 0
    print("%d jobs, %d at a time" % (total, jobs), flush=True)

    results = []
    with ThreadPoolExecutor(jobs) as ex, open(MANIFEST, "a", encoding="utf-8") as man:
        futs = {ex.submit(run_job, j): j for j in todo}
        try:
            for n, f in enumerate(as_completed(futs), 1):
                status, pdf, text = f.result()
                job = futs[f]
                results.append((status, job, text))
                if status in ("ok", "skip") and pdf and not MD_ONLY:
                    man.write("%s\t%s\n" % (job, pdf))
                    man.flush()
                first, _, rest = text.partition("\n")
                print("Remaining %d of %d\t: %s" % (total - n, total, first) + ("\n" + rest if rest else ""), flush=True)
        except KeyboardInterrupt:
            ex.shutdown(cancel_futures=True)
            raise

    # keep one record per job (the newest)
    rec = read_manifest()
    MANIFEST.write_text("".join("%s\t%s\n" % kv for kv in sorted(rec.items())), encoding="utf-8")

    ok = sum(s == "ok" for s, _, _ in results)
    fails = [(j, t) for s, j, t in results if s == "FAIL"]
    print("built %d, failed %d   (pdf/ md/ md-cases/; scratch in build/)" % (ok, len(fails)))
    # failures: repeated together at the end, kept in a file, plus a retry list of job lines
    Path("build/failures.txt").write_text("".join(t + "\n" for _, t in fails), encoding="utf-8")
    Path("build/failed_jobs.txt").write_text("".join(j + "\n" for j, _ in fails), encoding="utf-8")
    if fails:
        print("\n---- failures (saved in build/failures.txt; retry: tribonian build build/failed_jobs.txt) ----")
        for _, t in fails:
            print(t)
    # non-zero when anything failed, so "tribonian build && tribonian sync-vault" never syncs a broken build
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
