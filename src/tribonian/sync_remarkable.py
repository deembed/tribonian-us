"""Mirror pdf/ onto the reMarkable (via rmapi; reMarkable cloud or rmfakecloud), split like the vault:

    US Code  pdf/5-usc-ch05.pdf        -> /LawFormatter/Laws/USC/Title-05/5-usc-ch05
    Cases    pdf/FOIA/710-f3d-422-*.pdf -> /LawFormatter/Laws/Cases/FOIA/710-f3d-422-*
             pdf/473-f3d-370-*.pdf      -> /LawFormatter/Laws/Cases/473-f3d-370-*
    Sub-clusters pdf/Co-citation/2020-24/*.pdf -> /LawFormatter/Laws/Co-citation/2020-24/*

A PDF is a case if md-cases/ has a note with the same stem (case2md.py mirrors
md-cases/<dir>/ into pdf/<dir>/); otherwise a top-level PDF whose name starts
with a title number is US Code. Anything else is reported and skipped.

Incremental: state/rm_manifest.json records the sha256 of each uploaded PDF.
  new file      -> rmapi put
  changed file  -> rmapi put --force   (replaces the doc on the tablet: its annotations are lost)
  unchanged     -> nothing
Nothing is ever deleted on the tablet; PDFs gone from pdf/ are only listed.
More than MAX_UPDATES replacements in one run stop with a message unless --yes is given,
since a FORCE=1 rebuild of every PDF would otherwise wipe every annotation in one go.
tribonian build sets SOURCE_DATE_EPOCH, so an unchanged source rebuilds to an identical PDF.

    tribonian sync-remarkable --dry-run     show the plan, touch nothing
    tribonian sync-remarkable               upload
    tribonian sync-remarkable --yes         upload even when many PDFs would be replaced
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

from tribonian import tconfig

ROOT     = tconfig.WORK
PDF      = ROOT / "pdf"
NOTES    = ROOT / "md-cases"
MANIFEST = ROOT / "state" / "rm_manifest.json"     # durable, so rm -rf build/ never forgets what is on the tablet
if not MANIFEST.exists() and (ROOT / "build" / "rm_manifest.json").exists():   # moved here 2026-10-04
    MANIFEST.parent.mkdir(exist_ok=True)
    (ROOT / "build" / "rm_manifest.json").rename(MANIFEST)
REMOTE   = "/LawFormatter/Laws"
# rmapi binary: $RMAPI, else rmapi on PATH, else ~/.local/bin/rmapi
RMAPI    = os.environ.get("RMAPI") or shutil.which("rmapi") or os.path.expanduser("~/.local/bin/rmapi")
# RMAPI_HOST (read by rmapi itself) points at a self-hosted rmfakecloud, e.g.
# RMAPI_HOST=http://<host>:3000; leave it unset to use the official reMarkable cloud.
RM_CONF  = os.environ.get("RMAPI_CONFIG") or os.path.expanduser("~/.config/rmapi/rmapi.conf")  # token from pairing
MAX_UPDATES = 10   # more replacements than this in one run need --yes (each one drops annotations)

USC_RE = re.compile(r"^(\d+)([a-z]?)-", re.I)


def remote_folder(pdf: Path, case_stems: set):
    """Tablet folder for a local PDF, or None if it can't be classified."""
    rel = pdf.relative_to(PDF)
    if rel.parts[0] == "Co-citation":                 # cocite.py subdoc: sub-cluster explainers
        return "/".join([REMOTE, *rel.parts[:-1]])
    if pdf.stem in case_stems or len(rel.parts) > 1:
        return "/".join([REMOTE, "Cases", *rel.parts[:-1]])
    m = USC_RE.match(pdf.name)
    if m:
        return "%s/USC/Title-%02d%s" % (REMOTE, int(m.group(1)), m.group(2).upper())
    return None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def save_manifest(manifest: dict):
    """Atomic: a kill mid-write would otherwise leave truncated JSON and stop every later run."""
    MANIFEST.parent.mkdir(exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.replace(tmp, MANIFEST)


def rm_put(pdf: Path, folder: str, overwrite: bool, made: set) -> str:
    """Your working single-file upload, with mkdir done once per folder per run.
    Returns "ok", "exists" (already on tablet, not overwritten) or "fail"."""
    exe = shutil.which(RMAPI)
    env = dict(os.environ, RMAPI_CONFIG=RM_CONF)

    # mkdir -p on the tablet; "already exists" errors are ignored
    parts = [p for p in folder.split("/") if p]
    for i in range(1, len(parts) + 1):
        d = "/" + "/".join(parts[:i])
        if d not in made:
            subprocess.run([exe, "mkdir", d], env=env, capture_output=True)
            made.add(d)

    cmd = [exe, "put"] + (["--force"] if overwrite else []) + [str(pdf), folder]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if r.returncode and "already exists" in out:
        return "exists"
    if r.returncode:
        print(f"rmapi put failed: {pdf.name}: {out[-500:]}", file=sys.stderr)
        return "fail"
    return "ok"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, upload nothing")
    ap.add_argument("--yes", action="store_true",
                    help="allow more than %d updates in one run (each update replaces the tablet copy "
                         "and loses its annotations)" % MAX_UPDATES)
    args = ap.parse_args()

    if not args.dry_run and not shutil.which(RMAPI):
        sys.exit(f"rmapi not found at {RMAPI}")

    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    case_stems = {p.stem for p in NOTES.rglob("*.md")} if NOTES.is_dir() else set()

    plan, skipped = [], []                          # plan: (rel, pdf, folder, digest, action)
    for pdf in sorted(PDF.rglob("*.pdf")):
        rel = str(pdf.relative_to(PDF))
        folder = remote_folder(pdf, case_stems)
        if folder is None:
            skipped.append(rel); continue
        digest = sha256(pdf)
        old = manifest.get(rel)
        if old and old["sha256"] == digest and old["folder"] == folder:
            continue
        action = "update" if old and old["folder"] == folder else "new"
        plan.append((rel, pdf, folder, digest, action))

    gone = sorted(set(manifest) - {str(p.relative_to(PDF)) for p in PDF.rglob("*.pdf")})

    for rel, _, folder, _, action in plan:
        print(f"{action:6}  {rel}  ->  {folder}/")
    for rel in skipped:
        print(f"skip    {rel}  (not a case note or a USC title file)")
    for rel in gone:
        print(f"gone    {rel}  (still on tablet at {manifest[rel]['folder']}/ -- not deleted)")
    if args.dry_run or not plan:
        print(f"{len(plan)} to upload, {len(skipped)} skipped, {len(gone)} gone"
              + ("   (dry run -- nothing changed)" if args.dry_run else ""))
        return
    updates = sum(1 for p in plan if p[4] == "update")
    if updates > MAX_UPDATES and not args.yes:
        sys.exit(f"{updates} PDFs would be replaced on the tablet (annotations lost). "
                 f"After a FORCE=1 rebuild this is expected; rerun with --yes if so, "
                 f"otherwise check the plan above.")

    made, ok, failed = set(), 0, []
    for rel, pdf, folder, digest, action in plan:
        status = rm_put(pdf, folder, overwrite=(action == "update"), made=made)
        if status == "exists":
            # on the tablet but not in the manifest (uploaded by hand / lost manifest):
            # adopt it rather than clobber annotations; a later change will --force it
            print(f"adopt   {rel}  (already on tablet)")
        elif status == "fail":
            failed.append(rel); continue
        else:
            print(f"ok      {rel}  ->  {folder}/")
        manifest[rel] = {"sha256": digest, "folder": folder}
        ok += 1
        save_manifest(manifest)                                                  # save as we go

    print(f"uploaded {ok}, failed {len(failed)}" + (f"  -- rerun to retry" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
