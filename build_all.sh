#!/usr/bin/env bash
# Build ./xml/*.xml into ./pdf (one PDF per chapter) and ./md (one note per section).
#
#   ./build_all.sh                 every chapter of every XML; a title with no
#                                  chapters becomes one PDF
#   ./build_all.sh sections.txt    one PDF per line of the list instead
#   ./build_all.sh build/failed_jobs.txt   retry only what failed last run
#   JOBS=8 ./build_all.sh          parallel jobs (default: number of cores)
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
SELF="./$(basename "$0")"

export STYLE="${STYLE:-boxes}"
export EXTRA="${EXTRA:-}"          # e.g. EXTRA="--credits --notes"
export FONT="${FONT:-XCharter}"
export MD_ONLY="${MD_ONLY:-}"      # MD_ONLY=1: markdown only, skip pdflatex
export SCRATCH="${SCRATCH:-build}"  # per-job scratch dirs; set outside the repo if
                                    # this folder forbids deletes
JOBS="${JOBS:-$(nproc)}"

# ---- worker: one PDF. Runs in its own scratch dir so parallel jobs never
# ---- see each other's .tex/.aux/.md files. Prints exactly one status block.
build_one() {                       # $1 = xml path, $2.. = generator selection args
    local xml=$1; shift
    local sel="$*" out base rc work
    work=$(mktemp -d "$SCRATCH/job.XXXXXX")
    # shellcheck disable=SC2086
    out=$(python3 usc2md.py "$xml" "$@" --out "$work" --style "$STYLE" --font "$FONT" $EXTRA 2>&1); rc=$?
    if [[ $rc -eq 3 ]]; then
        rm -rf "$work"; echo "skip $xml $sel   (nothing the generator handles)"; return
    fi
    if [[ $rc -ne 0 ]]; then
        rm -rf "$work"
        printf 'FAIL generate  %s %s\n%s\n' "$xml" "$sel" "$(echo "$out" | tail -3 | sed 's/^/     /')"; return
    fi
    # the generator names the file; read it back rather than guessing
    base=$(printf '%s\n' "$out" | sed -n 's#^wrote .*/\([^/]*\)\.tex$#\1#p')
    [[ -z $base ]] && { rm -rf "$work"; echo "FAIL generate  $xml $sel   (no .tex reported)"; return; }
    if [[ -z $MD_ONLY ]]; then
        for pass in 1 2; do
            pdflatex -interaction=nonstopmode -halt-on-error -output-directory "$work" "$work/$base.tex" >/dev/null 2>&1 \
            || { cp -f "$work/$base.log" "build/$base.log" 2>/dev/null; rm -rf "$work"
                 echo "FAIL pdflatex  $xml $sel   (see build/$base.log)"; return; }
        done
        mv -f "$work/$base.pdf" pdf/ || { rm -rf "$work"; echo "FAIL pdflatex  $xml $sel   (no pdf)"; return; }
    fi
    # markdown keeps the t<NN>/ch<NN> tree the generator built
    while IFS= read -r -d '' f; do
        rel=${f#"$work"/}; dir=${rel%/*}; [[ $dir == "$rel" ]] && dir=.
        mkdir -p "md/$dir" && mv -f "$f" "md/$rel"
    done < <(find "$work" -name '*.md' -print0)
    rm -rf "$work"
    echo "ok   $base"
}

# ---- list the jobs for one XML, one per line: "<xml> <selection args>"
list_jobs() {
    local xml=$1 chapters
    chapters=$(python3 usc2md.py "$xml" --list-chapters | cut -f1)
    if [[ -z $chapters ]]; then
        echo "$xml --all"
    else
        while read -r ch; do echo "$xml --chapter $ch"; done <<< "$chapters"
    fi
}

# ---- internal re-entry points used by xargs below
if [[ ${1:-} == --job ]];  then read -r -a a <<< "$2"; build_one "${a[@]}"; exit 0; fi
if [[ ${1:-} == --list ]]; then list_jobs "$2"; exit 0; fi

mkdir -p pdf md build "$SCRATCH"
joblist=$(mktemp "$SCRATCH/jobs.XXXXXX"); log=$(mktemp "$SCRATCH/log.XXXXXX")
trap 'rm -f "$joblist" "$log"' EXIT

if [[ $# -ge 1 ]]; then
    while read -r xml ids; do
        [[ -z $xml || $xml == \#* ]] && continue
        if [[ -z $ids ]]; then
            echo "$xml --all"
        elif [[ $ids == */ch* && $ids != *" "* ]]; then
            echo "$xml --chapter $ids"
        else
            echo "$xml --section $ids"
        fi
    done < "$1" > "$joblist"
else
    # listing parses every XML too, so do that in parallel as well
    printf '%s\n' xml/*.xml | xargs -d '\n' -P "$JOBS" -n 1 "$SELF" --list > "$joblist"
fi

total=$(wc -l < "$joblist")
echo "$total jobs, $JOBS at a time"
# the log keeps the raw lines (counted below); the screen gets a countdown.
# ok, FAIL and skip all count as finished; indented detail lines pass through.
xargs -d '\n' -P "$JOBS" -n 1 "$SELF" --job < "$joblist" | tee "$log" \
| while IFS= read -r line; do
    if [[ $line == "ok "* || $line == "FAIL "* || $line == "skip "* ]]; then
        finished=$((${finished:-0}+1))
        printf 'Remaining %d of %d\t: %s\n' $((total-finished)) "$total" "$line"
    else
        printf '%s\n' "$line"
    fi
done

echo "built $(grep -c '^ok ' "$log"), failed $(grep -c '^FAIL' "$log")   (pdf/ md/; scratch in build/)"

# failures: repeat them together at the end, keep them in a file, and write a
# retry list in sections.txt format:  ./build_all.sh build/failed_jobs.txt
grep -E '^(FAIL|     )' "$log" > build/failures.txt
sed -n 's/^FAIL [a-z]*  *//p' "$log" | sed -E 's/   \(.*$//; s/ --(chapter|section|all)//' > build/failed_jobs.txt
if [[ -s build/failures.txt ]]; then
    echo; echo "---- failures (saved in build/failures.txt; retry: $SELF build/failed_jobs.txt) ----"
    cat build/failures.txt
fi
