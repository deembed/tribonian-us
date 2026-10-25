#!/usr/bin/env bash
# Build every PDF that is missing or older than its source, in parallel.
#
#   source                  ->  output
#   xml/*.xml               ->  pdf/<n>-usc-ch<NN>.pdf, one per chapter   + md/ notes
#   cases/*.json            ->  pdf/[<folder>/]<case>.pdf                 + md-cases/ note
#   cases/recap/*.json      ->  pdf/[<folder>/]<docket>.pdf               + md-cases/ notes
#
# So: fetch cases (tribonian case fetch / case recap fetch) or drop in XML, then run this.
#
#   tribonian build                build everything out of date
#   tribonian build cases          only cases/ and cases/recap/   (or: usc)
#   tribonian build --todo         list what would be built; build nothing
#   tribonian build --stubs        first turn stub notes into full cases (tribonian map stubs),
#                                 then build; STUB_ARGS="--api --max 20" passes options
#   FORCE=1 tribonian build        rebuild regardless of dates (after changing a generator)
#   tribonian build list.txt       build exactly the jobs in a list, always
#   tribonian build build/failed_jobs.txt   retry what failed last run
#   JOBS=8 tribonian build         parallel jobs (default: number of cores)
#   STYLE=rails / FONT=... / EXTRA="--credits --notes" (US Code only) / MD_ONLY=1
#
# "Out of date": state/built.tsv records <job> -> <pdf> for every success. A job
# is built when it has no record, its PDF is gone, or its source is newer than
# the PDF. Failures are never recorded, so the next run tries them again.
# The list of jobs that were due is kept in build/todo.txt.
#
# list.txt: one job per line. Any of
#   cases/463-u-s-29_motor-vehicle-mfrs....json          (a case or cases/recap/ docket)
#   xml/usc05.xml /us/usc/t5/s552 /us/usc/t5/s552a      (sections -> one PDF)
#   xml/usc05.xml /us/usc/t5/ptI/ch5                    (a chapter)
#   xml/usc05.xml                                        (a whole title)
#   usc|case|recap <args...>                             (a job line as in build/todo.txt)
# Lines starting with # are ignored.
set -uo pipefail
# Runs in the current folder: `tribonian build` starts it in the work folder (tconfig.WORK).
SELF="$(realpath "$0")"
# The interpreter `tribonian` runs under, so jobs see the same packages (set by tribonian build).
PY="${TRIBONIAN_PY:-python3}"

export STYLE="${STYLE:-boxes}"
export EXTRA="${EXTRA:-}"          # e.g. EXTRA="--credits --notes"
export FONT="${FONT:-XCharter}"
export MD_ONLY="${MD_ONLY:-}"      # MD_ONLY=1: markdown only, skip pdflatex (ignores dates, records nothing)
export FORCE="${FORCE:-}"
export SCRATCH="${SCRATCH:-build}"  # per-job scratch dirs; set outside the repo if
                                    # this folder forbids deletes
# Durable state (what was built, skip stamps) lives in state/, not in the scratch dir: rm -rf build
# must never force a full rebuild. The first run after 2026-10-04 moves the old files over.
export MANIFEST=state/built.tsv
mkdir -p state
if [[ -f build/built.tsv && ! -f state/built.tsv ]]; then
    mv build/built.tsv state/built.tsv
    [[ -d build/stamps ]] && mv build/stamps state/stamps
    sed -i "s#\tbuild/stamps/#\tstate/stamps/#" state/built.tsv
fi
JOBS="${JOBS:-$(nproc)}"
# Reproducible PDFs: pdflatex takes CreationDate, ModDate and the trailer ID from this
# instead of the clock, so an unchanged source gives a byte-identical PDF and
# tribonian sync-remarkable (which hashes PDFs) leaves the tablet copy -- and its pen
# annotations -- alone.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"

# a success: remember which PDF this job made. Lines this short are appended
# atomically, so parallel workers can share the file.
record() { [[ -z $MD_ONLY ]] && printf '%s\t%s\n' "$JOB" "$1" >> "$MANIFEST"; }

# ---- worker: one US Code PDF. Runs in its own scratch dir so parallel jobs never
# ---- see each other's .tex/.aux/.md files. Prints exactly one status block.
build_usc() {                       # $1 = xml path, $2.. = generator selection args
    local xml=$1; shift
    local out base rc work
    work=$(mktemp -d "$SCRATCH/job.XXXXXX")
    # shellcheck disable=SC2086
    out=$("$PY" -m tribonian usc "$xml" "$@" --out "$work" --style "$STYLE" --font "$FONT" $EXTRA 2>&1); rc=$?
    if [[ $rc -eq 3 ]]; then
        # a stamp file stands in for the PDF, so this isn't retried until the XML changes
        rm -rf "$work"; mkdir -p state/stamps
        local stamp="state/stamps/$(tr -c 'A-Za-z0-9._-' _ <<< "$JOB").skip"
        touch "$stamp"; record "$stamp"
        echo "skip $JOB   (nothing the generator handles)"; return
    fi
    if [[ $rc -ne 0 ]]; then
        rm -rf "$work"
        printf 'FAIL generate  %s\n%s\n' "$JOB" "$(echo "$out" | tail -3 | sed 's/^/     /')"; return
    fi
    # the generator names the file; read it back rather than guessing
    base=$(printf '%s\n' "$out" | sed -n 's#^wrote .*/\([^/]*\)\.tex$#\1#p')
    [[ -z $base ]] && { rm -rf "$work"; echo "FAIL generate  $JOB   (no .tex reported)"; return; }
    if [[ -z $MD_ONLY ]]; then
        for pass in 1 2; do
            timeout "${PDFLATEX_TIMEOUT:-1800}" \
            pdflatex -interaction=nonstopmode -halt-on-error -output-directory "$work" "$work/$base.tex" >/dev/null 2>&1 \
            || { cp -f "$work/$base.log" "build/$base.log" 2>/dev/null; rm -rf "$work"
                 echo "FAIL pdflatex  $JOB   (see build/$base.log)"; return; }
        done
        mv -f "$work/$base.pdf" pdf/ || { rm -rf "$work"; echo "FAIL pdflatex  $JOB   (no pdf)"; return; }
    fi
    # markdown keeps the t<NN>/ch<NN> tree the generator built
    while IFS= read -r -d '' f; do
        rel=${f#"$work"/}; dir=${rel%/*}; [[ $dir == "$rel" ]] && dir=.
        mkdir -p "md/$dir" && mv -f "$f" "md/$rel"
    done < <(find "$work" -name '*.md' -print0)
    rm -rf "$work"
    record "pdf/$base.pdf"
    echo "ok   pdf/$base.pdf"
}

# ---- worker: one case (case2md.py build) or RECAP docket (case2md.py recap build)
build_case() {                      # $1 = case|recap, $2 = json
    local kind=$1 json=$2 out rc pdf cmd=("$PY" -m tribonian case)
    [[ $kind == recap ]] && cmd+=(recap)
    out=$("${cmd[@]}" build "$json" --style "$STYLE" --font "$FONT" ${MD_ONLY:+--no-pdf} 2>&1); rc=$?
    if [[ $rc -ne 0 ]]; then
        printf 'FAIL generate  %s\n%s\n' "$JOB" "$(echo "$out" | tail -3 | sed 's/^/     /')"; return
    fi
    [[ -n $MD_ONLY ]] && { echo "ok   $json   (markdown only)"; return; }
    # case2md prints "wrote pdf/..." only when pdflatex succeeded
    pdf=$(printf '%s\n' "$out" | sed -n 's#^wrote \(pdf/.*\.pdf\)$#\1#p' | tail -1)
    if [[ -z $pdf ]]; then
        printf 'FAIL pdflatex  %s\n%s\n' "$JOB" "$(echo "$out" | grep -A3 '^FAIL' | sed 's/^/     /')"; return
    fi
    record "$pdf"
    echo "ok   $pdf"
}

run_job() {                         # $1 = job line: "usc <xml> <sel...>" | "case <json>" | "recap <json>"
    export JOB=$1
    local kind rest; read -r kind rest <<< "$JOB"
    # shellcheck disable=SC2086
    case $kind in
        usc)        build_usc $rest ;;
        case|recap) build_case "$kind" $rest ;;
        *)          echo "FAIL unknown   $JOB" ;;
    esac
}

# ---- one XML's chapter jobs, cached in build/chapters/ until the XML changes.
# ---- Writes the cache only: parallel workers printing to one pipe can split
# ---- each other's lines, so the main script reads the caches afterwards.
chapter_cache() { echo "build/chapters/$(basename "$1" .xml).txt"; }
list_usc() {
    local xml=$1 cache chapters
    cache=$(chapter_cache "$xml")
    if [[ ! -f $cache || $xml -nt $cache ]]; then
        chapters=$("$PY" -m tribonian usc "$xml" --list-chapters) || return   # no cache: built as one --all job
        chapters=$(cut -f1 <<< "$chapters")
        if [[ -z $chapters ]]; then
            echo "usc $xml --all"
        else
            while read -r ch; do echo "usc $xml --chapter $ch"; done <<< "$chapters"
        fi > "$cache.$$" && mv -f "$cache.$$" "$cache"
    fi
}

# ---- a line of a list file -> a job line
list_line() {                       # $1 = xml|json|job, $2 = rest
    local first=$1 ids=$2
    case $first in
        usc|case|recap)  echo "$first $ids" ;;
        */recap/*.json)  echo "recap $first" ;;
        *.json)          echo "case $first" ;;
        *)  if   [[ -z $ids ]];                        then echo "usc $first --all"
            elif [[ $ids == */ch* && $ids != *" "* ]]; then echo "usc $first --chapter $ids"
            else                                            echo "usc $first --section $ids"
            fi ;;
    esac
}

# ---- is this job due? (no record, PDF gone, or source newer than PDF)
declare -A BUILT=()
due() {
    [[ -n $FORCE || -n $MD_ONLY ]] && return 0
    local src pdf=${BUILT[$1]:-}
    read -r _ src _ <<< "$1"
    [[ -z $pdf || ! -f $pdf || $src -nt $pdf ]]
}

# ---- internal re-entry points used by xargs below
if [[ ${1:-} == --job ]];  then run_job "$2"; exit 0; fi
if [[ ${1:-} == --list ]]; then list_usc "$2"; exit 0; fi

TODO=; SCOPE=all; LISTFILE=; STUBS=
for a in "$@"; do
    case $a in
        --todo)          TODO=1 ;;
        --stubs)         STUBS=1 ;;
        usc|cases|all)   SCOPE=$a ;;
        *)               LISTFILE=$a ;;
    esac
done

mkdir -p pdf md build build/chapters "$SCRATCH"
touch "$MANIFEST"
# stubs first, so the cases they become are in this build. A stop (e.g. the API's
# daily limit) still builds what was fetched; rerunning resumes.
# shellcheck disable=SC2086
[[ -n $STUBS && -z $TODO ]] && { "$PY" -m tribonian map stubs ${STUB_ARGS:-} || echo "(stub fetch stopped early; building what there is)"; }
joblist=$(mktemp "$SCRATCH/jobs.XXXXXX"); log=$(mktemp "$SCRATCH/log.XXXXXX"); all=$(mktemp "$SCRATCH/all.XXXXXX")
trap 'rm -f "$joblist" "$log" "$all"' EXIT

if [[ -n $LISTFILE ]]; then
    # a list is built as given: no date check
    while read -r first ids; do
        [[ -z $first || $first == \#* ]] && continue
        list_line "$first" "$ids"
    done < "$LISTFILE" > "$joblist"
else
    {
        if [[ $SCOPE != usc ]]; then
            for f in cases/*.json;       do [[ -f $f && ${f##*/} != _* ]] && echo "case $f";  done
            for f in cases/recap/*.json; do [[ -f $f ]]                    && echo "recap $f"; done
        fi
        if [[ $SCOPE != cases ]]; then
            # listing parses every changed XML, so do that in parallel as well
            printf '%s\n' xml/*.xml | xargs -d '\n' -P "$JOBS" -n 1 bash "$SELF" --list
            for x in xml/*.xml; do
                c=$(chapter_cache "$x")
                if [[ -f $c ]]; then cat "$c"; else echo "usc $x --all"; fi
            done
        fi
    } > "$all"
    while IFS=$'\t' read -r j p; do BUILT[$j]=$p; done < "$MANIFEST"
    while IFS= read -r j; do due "$j" && echo "$j"; done < "$all" > "$joblist"
    echo "$(wc -l < "$all") PDFs known, $(wc -l < "$joblist") out of date"
fi
cp -f "$joblist" build/todo.txt

if [[ -n $TODO ]]; then cat "$joblist"; exit 0; fi

total=$(wc -l < "$joblist")
[[ $total -eq 0 ]] && { echo "nothing to build"; exit 0; }
echo "$total jobs, $JOBS at a time"
# the log keeps the raw lines (counted below); the screen gets a countdown.
# ok, FAIL and skip all count as finished; indented detail lines pass through.
xargs -d '\n' -P "$JOBS" -n 1 bash "$SELF" --job < "$joblist" | tee "$log" \
| while IFS= read -r line; do
    if [[ $line == "ok "* || $line == "FAIL "* || $line == "skip "* ]]; then
        finished=$((${finished:-0}+1))
        printf 'Remaining %d of %d\t: %s\n' $((total-finished)) "$total" "$line"
    else
        printf '%s\n' "$line"
    fi
done

# keep one record per job (the newest)
awk -F'\t' '{m[$1]=$2} END{for (k in m) print k "\t" m[k]}' "$MANIFEST" | sort > "$MANIFEST.$$" && mv -f "$MANIFEST.$$" "$MANIFEST"

echo "built $(grep -c '^ok ' "$log"), failed $(grep -c '^FAIL' "$log")   (pdf/ md/ md-cases/; scratch in build/)"

# failures: repeat them together at the end, keep them in a file, and write a
# retry list of job lines:  tribonian build build/failed_jobs.txt
# (a plain tribonian build retries them too, since failures are never recorded)
grep -E '^(FAIL|     )' "$log" > build/failures.txt
sed -n 's/^FAIL [a-z]*  *//p' "$log" | sed -E 's/   \(.*$//' > build/failed_jobs.txt
if [[ -s build/failures.txt ]]; then
    echo; echo "---- failures (saved in build/failures.txt; retry: tribonian build build/failed_jobs.txt) ----"
    cat build/failures.txt
fi
# non-zero when anything failed, so "tribonian build && tribonian sync-vault" never syncs a broken build
[[ $(grep -c '^FAIL' "$log") -eq 0 ]]
