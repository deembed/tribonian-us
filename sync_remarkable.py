#!/usr/bin/env python3
"""Mirror pdf/ onto the reMarkable (via rmfakecloud + rmapi), split like the vault:

    US Code  pdf/5-usc-ch05.pdf        -> /Archive/Laws/USC/Title-05/5-usc-ch05
    Cases    pdf/FOIA/710-f3d-422-*.pdf -> /Archive/Laws/Cases/FOIA/710-f3d-422-*
             pdf/473-f3d-370-*.pdf      -> /Archive/Laws/Cases/473-f3d-370-*

A PDF is a case if md-cases/ has a note with the same stem (case2md.py mirrors
md-cases/<dir>/ into pdf/<dir>/); otherwise a top-level PDF whose name starts
with a title number is US Code. Anything else is reported and skipped.

Incremental: build/rm_manifest.json records the sha256 of each uploaded PDF.
  new file      -> rmapi put
  changed file  -> rmapi put --force   (replaces the doc on the tablet)
  unchanged     -> nothing
Nothing is ever deleted on the tablet; PDFs gone from pdf/ are only listed.

    ./sync_remarkable.py --dry-run     show the plan, touch nothing
    ./sync_remarkable.py               upload
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

ROOT     = Path(__file__).resolve().parent
PDF      = ROOT / "pdf"
NOTES    = ROOT / "md-cases"
MANIFEST = ROOT / "build" / "rm_manifest.json"
REMOTE   = "/Archive/Laws"
RMAPI    = os.path.expanduser("~/.local/bin/rmapi")
RM_HOST  = "http://<rmfakecloud-host>:3000"     # rmfakecloud on the hub Pi
RM_CONF  = os.path.expanduser("~/.config/rmapi/rmapi.conf")  # token from pairing

USC_RE = re.compile(r"^(\d+)([a-z]?)-", re.I)


def remote_folder(pdf: Path, case_stems: set):
    """Tablet folder for a local PDF, or None if it can't be classified."""
    rel = pdf.relative_to(PDF)
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


def rm_put(pdf: Path, folder: str, overwrite: bool, made: set) -> str:
    """Your working single-file upload, with mkdir done once per folder per run.
    Returns "ok", "exists" (already on tablet, not overwritten) or "fail"."""
    exe = shutil.which(RMAPI)
    env = dict(os.environ, RMAPI_HOST=RM_HOST, RMAPI_CONFIG=RM_CONF)

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
        MANIFEST.parent.mkdir(exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True))   # save as we go

    print(f"uploaded {ok}, failed {len(failed)}" + (f"  -- rerun to retry" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
