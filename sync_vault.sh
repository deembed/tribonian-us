#!/usr/bin/env bash
# Mirror the generated section notes into the USC Obsidian vault.
#
#   ./sync_vault.sh                      md/ -> ~/Obsidian/usc-vault/08-Reference/USC
#   VAULT=/path/to/vault ./sync_vault.sh
#   DRY=1 ./sync_vault.sh                show what would change, touch nothing
#
# 08-Reference/USC is generator-owned: anything in it that is not in md/ is
# DELETED. Your own notes belong in 00-Inbox / 06-Zettelkasten / 06-Zettel MOC
# and link into the statutes with [[5-usc-552a]].
set -euo pipefail
cd "$(dirname "$0")"

VAULT="${VAULT:-$HOME/Obsidian/usc-vault}"
DEST="$VAULT/08-Reference/USC"

[[ -d $VAULT/.obsidian ]] || { echo "not an Obsidian vault: $VAULT" >&2; exit 1; }
[[ -d md ]]               || { echo "no md/ here -- run ./build_all.sh first" >&2; exit 1; }

mkdir -p "$DEST"
rsync -a --delete ${DRY:+--dry-run} --itemize-changes \
      --include='*/' --include='*.md' --exclude='*' \
      md/ "$DEST/" | tail -20

# Case notes (case2md.py / casemap.py write md-cases/) -> 08-Reference/Cases, subfolders
# (md-cases/FOIA/ ...) included. Same rule: generator-owned, mirrored with --delete, so move
# a note inside md-cases/, not in the vault.
if [[ -d md-cases ]]; then
    mkdir -p "$VAULT/08-Reference/Cases"
    rsync -a --delete ${DRY:+--dry-run} --itemize-changes \
          --include='*/' --include='*.md' --exclude='*' \
          md-cases/ "$VAULT/08-Reference/Cases/" | tail -20
    echo "cases: $(find md-cases -name '*.md' | wc -l) notes -> $VAULT/08-Reference/Cases"
fi

# Co-citation map (cocite.py vault writes md-cocite/) -> 08-Reference/Co-citation. Generator-owned
# too; a personal MOC elsewhere in the vault transcludes its sections: ![[<note>#Timeline]].
if [[ -d md-cocite ]]; then
    mkdir -p "$VAULT/08-Reference/Co-citation"
    rsync -a --delete ${DRY:+--dry-run} --itemize-changes \
          --include='*/' --include='*.md' --exclude='*' \
          md-cocite/ "$VAULT/08-Reference/Co-citation/" | tail -20
    echo "co-citation: $(find md-cocite -name '*.md' | wc -l) notes -> $VAULT/08-Reference/Co-citation"
fi

if [[ -z ${DRY:-} ]]; then
    echo "synced $(find "$DEST" -name '*.md' | wc -l) notes -> $DEST"
else
    echo "(dry run -- nothing changed)"
fi
