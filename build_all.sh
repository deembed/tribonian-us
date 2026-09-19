#!/usr/bin/env bash
# Build ./xml/*.xml into ./pdf (one PDF per chapter) and ./md (one note per section).
#
#   ./build_all.sh                 every chapter of every XML; a title with no
#                                  chapters becomes one PDF
#   ./build_all.sh sections.txt    one PDF per line of the list instead
#   STYLE=rails ./build_all.sh     pass a generator style
#   EXTRA="--credits --notes" ./build_all.sh   pass any other generator flags
#
# sections.txt: one PDF per line, "<xml file> <identifier...>", where the
# identifiers are sections (several on one line go in one PDF) or a chapter:
#   xml/usc05.xml /us/usc/t5/s552 /us/usc/t5/s552a
#   xml/usc05.xml /us/usc/t5/ptI/ch5
# Lines starting with # are ignored.
set -uo pipefail
cd "$(dirname "$0")"

STYLE="${STYLE:-boxes}"
EXTRA="${EXTRA:-}"                 # e.g. EXTRA="--credits --notes"
FONT="${FONT:-XCharter}"
mkdir -p pdf md build

built=0; failed=0
build_one() {                       # $1 = xml path, $2.. = generator selection args
    local xml=$1; shift
    local sel="$*" out base
    out=$(python3 usc2md.py "$xml" "$@" --out build --style "$STYLE" --font "$FONT" $EXTRA 2>&1); local rc=$?
    if [[ $rc -eq 3 ]]; then echo "skip $xml $sel   (nothing the generator handles)"; return; fi
    if [[ $rc -ne 0 ]]; then
        echo "FAIL generate  $sel"; echo "$out" | tail -3 | sed 's/^/     /'; failed=$((failed+1)); return
    fi
    # the generator names the file; read it back rather than guessing
    base=$(printf '%s\n' "$out" | sed -n 's#^wrote .*/\([^/]*\)\.tex$#\1#p')
    [[ -z $base ]] && { echo "FAIL generate  $sec   (no .tex reported)"; failed=$((failed+1)); return; }
    for pass in 1 2; do
        pdflatex -interaction=nonstopmode -halt-on-error -output-directory build "build/$base.tex" >/dev/null 2>&1 \
        || { echo "FAIL pdflatex  $sel   (see build/$base.log)"; failed=$((failed+1)); return; }
    done
    mv -f "build/$base.pdf" pdf/ && mv -f build/*.md md/
    echo "ok   $base.pdf"; built=$((built+1))
}

if [[ $# -ge 1 ]]; then
    while read -r xml ids; do
        [[ -z $xml || $xml == \#* ]] && continue
        if [[ $ids == */ch* && $ids != *" "* ]]; then
            build_one "$xml" --chapter "$ids"
        else
            # shellcheck disable=SC2086
            build_one "$xml" --section $ids
        fi
    done < "$1"
else
    for xml in xml/*.xml; do
        chapters=$(python3 usc2md.py "$xml" --list-chapters | cut -f1)
        if [[ -z $chapters ]]; then
            build_one "$xml" --all
        else
            while read -r ch; do build_one "$xml" --chapter "$ch"; done <<< "$chapters"
        fi
    done
fi

echo "built $built, failed $failed   (pdf/ md/; scratch in build/)"
