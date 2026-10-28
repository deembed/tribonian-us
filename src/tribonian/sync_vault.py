"""sync_vault.py -- mirror the generated notes into the Obsidian vault.

    tribonian sync-vault                      md/ -> ~/Obsidian/usc-vault/08-Reference/USC (and the rest below)
    VAULT=/path/to/vault tribonian sync-vault
    DRY=1 tribonian sync-vault                show what would change, touch nothing
    FORCE=1 tribonian sync-vault              sync even when the source looks like a partial build

    md/          -> 08-Reference/USC            US Code section notes (tribonian build)
    md-cases/    -> 08-Reference/Cases          case notes, subfolders included
    md-cocite/   -> 08-Reference/Co-citation    co-citation map (tribonian cocite vault)
    md-cfr/      -> 08-Reference/CFR            C.F.R. notes (tribonian cfr notes)
    md-state/    -> 08-Reference/State          state law and the U.S. Constitution (tribonian state notes)

Each target folder is generator-owned: a .md note in it that is not in the source is DELETED (other
files are left alone). Your own notes belong in 00-Inbox / 06-Zettelkasten / 06-Zettel MOC and link
into the statutes with [[5-usc-552a]]; to move a case note, move it inside md-cases/, not in the vault.
Also writes the filing notes' cover-color CSS snippet (enable it once under Settings > Appearance >
CSS snippets). Runs in the work folder; plain Python, so no rsync needed.
"""
import os, shutil, subprocess, sys
from pathlib import Path

TARGETS = [("md", "USC", "US Code"), ("md-cases", "Cases", "cases"), ("md-cocite", "Co-citation", "co-citation"),
           ("md-cfr", "CFR", "C.F.R."), ("md-state", "State", "state law")]
SHOW = 20                                       # change lines shown per folder


def notes(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*.md")} if root.is_dir() else set()


def same(a, b):
    sa, sb = a.stat(), b.stat()
    return sa.st_size == sb.st_size and int(sa.st_mtime) == int(sb.st_mtime)


def mirror(src, dest, dry, force):
    """src/**/*.md -> dest/, deleting dest notes src lacks. -> number of notes in src, or exits 2 on a partial source."""
    have, want = notes(dest), notes(src)
    # deleting to match a half-built source would prune the vault: refuse below 90% unless FORCE
    if have and not force and len(want) < len(have) * 9 // 10:
        sys.exit("refusing: %s has %d notes, %s has %d -- a partial build? (FORCE=1 to sync anyway)"
                 % (src, len(want), dest, len(have)))
    changes = []
    for rel in sorted(want):
        s, d = src / rel, dest / rel
        if d.exists() and same(s, d):
            continue
        changes.append(("updated " if d.exists() else "new     ") + rel)
        if not dry:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
    for rel in sorted(have - want):
        changes.append("deleted " + rel)
        if not dry:
            (dest / rel).unlink()
    if not dry:                                  # folders left empty by deletions, when the source has no such folder
        for d in sorted((p for p in dest.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            if not any(d.iterdir()) and not (src / d.relative_to(dest)).is_dir():
                d.rmdir()
    for line in changes[-SHOW:]:
        print("  " + line)
    if len(changes) > SHOW:
        print("  ... and %d more" % (len(changes) - SHOW))
    return len(want)


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    vault = Path(os.environ.get("VAULT") or Path.home() / "Obsidian" / "usc-vault").expanduser()
    dry, force = bool(os.environ.get("DRY")), bool(os.environ.get("FORCE"))
    if not (vault / ".obsidian").is_dir():
        sys.exit("not an Obsidian vault: %s" % vault)
    if not Path("md").is_dir():
        sys.exit("no md/ here -- run tribonian build first")
    for src, sub, what in TARGETS:
        if not Path(src).is_dir():
            continue
        dest = vault / "08-Reference" / sub
        if not dry:
            dest.mkdir(parents=True, exist_ok=True)
        n = mirror(Path(src), dest, dry, force)
        print("%s: %d notes -> %s" % (what, n, dest))
    if dry:
        print("(dry run -- nothing changed)")
        return 0
    css = vault / ".obsidian" / "snippets" / "tribonian-covers.css"
    css.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([os.environ.get("TRIBONIAN_PY") or sys.executable, "-m", "tribonian.covers"],
                       stdout=subprocess.PIPE, text=True, encoding="utf-8")
    if r.returncode == 0:
        css.write_text(r.stdout, encoding="utf-8")
        print("cover colors -> %s" % css)
    return 0


if __name__ == "__main__":
    sys.exit(main())
