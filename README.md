# Legal Formatter for Tablets

Turns the US Code and court opinions into dense PDFs for the reMarkable, plus Obsidian notes.

## Everyday use

```
./case2md.py fetch --cite "463 U.S. 29"   # 1. get a case (or drop new XML into xml/)
./build_all.sh                            # 2. build every PDF that is missing or out of date
./sync_vault.sh                           # 3. notes -> Obsidian vault
./sync_remarkable.py                      # 4. PDFs -> reMarkable
```

## Folders

| Folder | What's in it | Written by |
|---|---|---|
| `xml/` | US Code, USLM XML from [uscode.house.gov](https://uscode.house.gov/download/download.shtml) | you |
| `cases/` | one `.json` per case with full text; `cases/recap/` for district-court dockets | `case2md.py fetch`, `casemap.py get`/`stubs` |
| `cases/stubs/` | cases known only from the citation graph (no text yet) | `casemap.py map` |
| `cases/unparsed/` | text fetched but not in a format `build` can read; left out of builds | `casemap.py stubs --api` |
| `cases/scotus/` | pending Supreme Court dockets: `<docket>.json` plus `<docket>/` with the filed PDFs and their text; `_recent.json` + `_recent/` (recent opinions, grants, orders) | `scotus.py fetch`, `scotus.py recent` |
| `md/` | US Code notes, one per section, in `t<NN>/ch<NN>/` | `build_all.sh` |
| `md-cases/` | case notes (full and stub); subfolders such as `FOIA/` and `Pending SCOTUS/` | `case2md.py`, `casemap.py`, `scotus.py` |
| `md-cocite/` | co-citation notes: `Co-citation map.md`, `Areas/`, `Lineages/` | `cocite.py vault` |
| `pdf/` | the PDFs; case PDFs mirror `md-cases/` subfolders | `build_all.sh`, `case2md.py build`, `scotus.py build` |
| `build/` | `.tex`, logs, scratch; disposable | the build |
| `state/` | `built.tsv` (what each job built), skip stamps, `rm_manifest.json` (what is on the tablet); keep | `build_all.sh`, `sync_remarkable.py` |
| `build/cocite/` | co-citation evidence files, benchmark output, shift eval | `cocite.py` |
| `bulk/`, `db/` | CourtListener bulk files and the local SQLite databases | `casedb.py` |
| `tests/` | smoke tests: `python3 -m unittest discover tests` | you |

Everything the scripts generate is ignored by git (`.gitignore`); tracked are the scripts, the docs, `tests/`, `data/cocite_labels.json` and `data/cocite_gold.json` (hand- and model-written inputs that can't be rebuilt), and `templates/`.

## build_all.sh — build everything out of date, in parallel

```
./build_all.sh                  # everything out of date
./build_all.sh cases            # only cases/ and cases/recap/   (or: usc)
./build_all.sh --todo           # list what would be built; build nothing
./build_all.sh --stubs cases    # fill stub notes first (casemap.py stubs), then build
FORCE=1 ./build_all.sh          # rebuild regardless of dates (after changing a generator)
./build_all.sh list.txt         # build exactly the jobs in a list, always
./build_all.sh build/failed_jobs.txt   # retry last run's failures
```

Sources it scans: `xml/*.xml` (one PDF per chapter), `cases/*.json`, `cases/recap/*.json`.

How it decides what to build: `state/built.tsv` records the PDF that each successful job made. A job gets built when it has no record, its PDF is gone, or its source is newer than its PDF. Failures aren't recorded, so the next run retries them. The list of jobs that were due is saved in `build/todo.txt`.

Environment settings:

| Variable | Default | Effect |
|---|---|---|
| `JOBS` | number of cores | parallel jobs |
| `STYLE` | `boxes` | `boxes` or `rails` |
| `FONT` | `XCharter` | LaTeX font package |
| `EXTRA` | — | US Code generator flags, e.g. `"--credits --notes"` |
| `MD_ONLY` | — | `1`: notes only, no PDFs (ignores dates, records nothing) |
| `FORCE` | — | `1`: ignore dates |
| `STUB_ARGS` | — | options passed to `casemap.py stubs` with `--stubs` |
| `SCRATCH` | `build` | where per-job scratch folders go |

List file format (one PDF per line, `#` for comments):

```
cases/463-u-s-29_motor-vehicle-mfrs....json       # a case (cases/recap/... = a docket)
xml/usc05.xml /us/usc/t5/s552 /us/usc/t5/s552a    # these sections in one PDF
xml/usc05.xml /us/usc/t5/ptI/ch5                  # one chapter
xml/usc05.xml                                     # a whole title
```

After a run, failures are in `build/failures.txt`, and pdflatex logs for failed builds are in `build/<name>.log`.

## usc2md.py — one US Code selection → notes + LaTeX

`build_all.sh` usually runs this for you.

```
./usc2md.py xml/usc05.xml --list-chapters
./usc2md.py xml/usc05.xml --list                           # section identifiers
./usc2md.py xml/usc05.xml --section /us/usc/t5/s552 --out out
./usc2md.py xml/usc05.xml --chapter /us/usc/t5/ptI/ch5 --out out
```

Other flags: `--all`, `--name`, `--credits` (source credits under headings), `--notes` (editorial and statutory notes), `--style`, `--font`. It writes `.tex` and `.md` files; `build_all.sh` runs pdflatex on them.

## case2md.py — court opinions (CourtListener)

Each case note and PDF has, above the opinions:

- **Case profile** (light-blue box in the PDF): cited-by counts per year as a bar chart, the last 5 full years against the 5 before (rising / steady / falling), and the **good-law check**. It needs the local database; the counts are cached in `cases/citedby/<cluster>/profile.json` and recounted when the bulk data changes.
- **Headmatter** (yellow box in the PDF): parties, docket number, dates, counsel, from the published report (Harvard CAP), when the cluster has it.
- **What it draws on** (orange box in the PDF, after the co-citation box): where the cases this one cites sit in the co-citation map, each cited case's sub-cluster in the newest full slice, with counts, shares, areas and how many cited cases are unclustered (`cocite.draws_on`, bibliographic coupling). It works for any case, so for a case the map doesn't hold (cited too rarely, recent, unpublished) it is the main placement. Every build; well under a second.
- **Coupled with** (pale orange box): the cases citing the most of the same cases, rare shared citations weighted up. Only when `./casemap.py coupling` has cached `cases/citedby/<cluster>/coupling.json` for the case, or with `build --coupling`, which computes it first.

Good-law check: no language model, just the words later courts use about this case, next to its citations and in sentences that name it ("Chevron is overruled."). It reads every citing opinion from a court that could overrule the case (up to 1,500), plus the 500 newest from any court.

- **red** — "overruled", "abrogated", "superseded", "overturned", "reversed"/"vacated" as subsequent history right after the citation ("…, 467 U.S. 837 (1984), overruled by …"), "X is overruled", "we overrule X", "(overruling X)" — from a court that could overrule it, or from two lower courts.
- **yellow** — any other such word, "on other grounds", or softer words right next to the citation ("questioned", "called into doubt", "declined to follow", "limited to its facts") three times, or twice from a court above.
- **green** — none found in what was read.

Not counted: hypotheticals and arguments ("unless Roe is overruled", "defendant argues that Apodaca has been overruled", "the Court will declare…"), the case doing the overruling ("Brown … (overruling Plessy)"), negated or quoted softer words, and another case with the same name ("Aguilar v. Texas" for Aguilar v. Felton).

The chart's bars take the colour of the status as of that year. It is a pointer to what to read (the opinions behind the flag are listed), not a citator.

```
./casemap.py goodlaw --cite "467 U.S. 837"               # the check for one case, with its evidence
./casemap.py goodlaw --cite "467 U.S. 837" --show Loper  # plus every citation in opinions mentioning "Loper"
./goodlaw_eval.py collect                                # read the 60 test cases once (slow; resumable)
./goodlaw_eval.py score -v                               # score each ruleset; -v lists the misses
./goodlaw_eval.py show "410 U.S. 113" R9                 # one test case under one ruleset
```

`goodlaw_eval.py` tests the rules on 40 Supreme Court cases whose overruling year is known (set A tuned the rules; set B is a soft check) and 20 that were never overruled. A hit is red from one year before to five years after the overruling. The current rules (R9): set A 17 of 20 hits, controls 8 green, 2 yellow, 0 red; set B 18 of 20, controls 6 green, 4 yellow, 0 red. (The first version: 13 and 16 hits, 3 controls red.) Set B was consulted once during tuning (the same-name rule came from it), so it is a soft check, not a clean holdout; the controls were watched throughout. After changing the rules in `casemap.py`, bump `GOODLAW_VERSION` and rerun `score`.

```
./case2md.py fetch --cite "463 U.S. 29"         # or --cluster 110991   (uses the API)
./case2md.py build cases/463-u-s-29_*.json      # -> md-cases/, build/, pdf/   (--no-pdf: notes only; --no-goodlaw: skip the slow good-law check; --coupling: compute the coupled cases first)
./case2md.py find glomar                        # search your notes, then the local database (--max N results per list)
./case2md.py find glomar --get 3                # take result 3 from the database and build it
./case2md.py recap fetch --docket 4949363       # the court's orders and opinions (uses the API)
./case2md.py recap fetch --docket 4949363 --docs 35,55,12-1   # these documents (12-1 = attachment); --all: everything
./case2md.py recap build cases/recap/<docket>.json             # --docs 35,55: only these
./case2md.py usage                              # API requests used; --days N for history
```

- **Where notes go:** a note that already exists anywhere in `md-cases/` is rewritten in place, so moving a note (inside `md-cases/`, not in the vault) is how you file it. New notes follow the `FOLDERS` table in the script (for example, Freedom of Information Act and Privacy Act cases go to `FOIA/`).
- **API token:** `COURTLISTENER_TOKEN`, or the GNOME keyring (`secret-tool store --label courtlistener service courtlistener`).
- **Rate limits:** free tier 5 a minute, 50 an hour, 125 a day; set `CL_LIMITS=10,75,300` for a paid tier. Requests pace themselves. At the daily limit a command stops cleanly, and rerunning it resumes from the cache.
- **Usage log:** `cases/_api_log.json` keeps the last 24 hours (used for the limits). `cases/_api_history.tsv` keeps every request (time, command, API path). Any command that made requests ends with `CourtListener API: N requests by this command; M of 125 left today`.

## scotus.py — a pending Supreme Court case (supremecourt.gov, no API limit)

```
./scotus.py fetch 25-579                        # the Court's docket + every filed PDF -> cases/scotus/ (rerun: only new filings)
./scotus.py build 25-579                        # -> pdf/Pending SCOTUS/<slug>.pdf, md-cases/Pending SCOTUS/<slug>.md + a note per filing
./scotus.py build 25-579 --amici --appendix     # amicus briefs in the PDF too; keep the appendices
./scotus.py build 25-579 --depth 1 --text 1     # also case notes for the decision below and every cited case, then ./build_all.sh
./scotus.py build 25-579 --no-goodlaw --no-pdf  # skip the good-law check of the cited cases; skip pdflatex
./scotus.py recent [--json] [--days 365]        # the Court's last year: opinions, grants, orders -> cases/scotus/_recent.json
```

- **`recent`:** reads the slip-opinion pages (`opinions/slipopinion/<term>`), the Granted & Noted lists (`orders/<term>grantednotedlist.pdf`) and every order list and miscellaneous order of the last year (`orders/ordersofthecourt/<term>`, PDFs kept in `cases/scotus/_recent/`), and writes `cases/scotus/_recent.json` atomically. There is no public list of all pending cert petitions; one appears only when the Court acts on it. The GUI's SCOTUS page shows the result.
- **`fetch`:** reads the docket's JSON feed and downloads each linked PDF (proofs of service and certificates skipped) to `cases/scotus/<docket>/`, with `pdftotext -layout` beside it. Not CourtListener, so no rate limit.
- **`build`:** one PDF in parts: the question presented, the decision below (from the local database), the cert-stage filings, the merits filings. Notes: the docket note and one per filing, each with a Statutes cited list. Amicus notes are always written; `--amici` puts the briefs in the PDF as well. `--font`, `--style` and `--no-pdf` as for `case2md.py build`.
- **Authorities and their status:** each filing's reporter citations resolved through the local database, and which filings share them. Two tables: *Common authorities* (one row per case both sides cite, with its good-law status and each side's mentions, plaintiff below on the left) and *Each side's own* (two columns, one per party). Status comes from `casemap.py goodlaw`, cached per case; red is a lead to check, not a holding. `--no-goodlaw` skips it (slow the first time).
- **`--depth N`:** the crawl `casemap.py map --depth` does, rooted at the decision below and the filings' authorities: full text for the first `--text` steps (default 1), stubs beyond; `--max` (3000) stops a runaway crawl. It writes the same `cases/*.json` and notes, so `./build_all.sh` builds their PDFs; rerun `./scotus.py build` afterwards so its notes link to them.
- **Not a `build_all.sh` job:** `build_all.sh` has no scotus job kind (`cases/scotus/` is not scanned), so `./scotus.py build` makes the PDF itself. Rerun it after new filings arrive.

## casemap.py — citation graph (no API unless `--api`)

```
./casemap.py get --cite "401 U.S. 402"          # one case from the local database -> cases/*.json + note
./casemap.py map --cite "463 U.S. 29"           # notes for the root's co-citation sub-cluster (the default, --by cluster)
./casemap.py map --cite "463 U.S. 29" --depth 2 # the citation walk instead: everything within 2 citation steps
./casemap.py citedby cases/463-u-s-29_*.json    # "Cited by" section: who cites it, for what
./casemap.py coupling --cluster 9493818         # coupled cases: who cites the same (rare) cases -> coupling.json
./casemap.py stubs --list                       # stub notes, most-cited first
./casemap.py stubs                              # fill stubs from the local database
./casemap.py stubs --api --max 20               # fall back to CourtListener for the rest
./casemap.py stubs brown 108283                 # only matching note names or cluster IDs
./casemap.py fixlinks                           # give bare courtlistener.com/opinion/<id>/ links in md-cases/ their slug
```

- **`map --by cluster`** (the default): the cases in the root's sub-cluster lineage in the newest full slice of `cocite.py`'s case-note view, plus cases that joined it since, heaviest first, up to `--cluster-max` (60); all get full text (`--text 0`: stubs only). `--scope all` takes every lineage the root has belonged to; `--courts` picks another `cocite.py` view.
- **`map --depth N`** (or `--by depth`): the citation walk. `--text K` gives full text to the first K steps (default `depth - 1`); the outer ring becomes stubs. `--max` (3000) caps either kind of map.
- **`citedby`:** options are `--list`, `--full`, `--level`, `--sample-over`, `--margin`, `--top`, `--steps` and `--min-run`; see `./casemap.py citedby -h`.
- **`coupling`:** bibliographic coupling. For each case it cites, cited by df decisions (at most `--max-df`, default 2000; more-cited ones are left out and counted), every other decision citing it scores log(N/df), N = decisions citing anything; the top `--top` (30) by total score, with the shared cited cases. Reads `bulk/cocite-cache/` (run `./cocite.py prep` first), a few seconds for most cases, up to half a minute for a case citing 150+ cases on a cold disk. Prints a table (`--json`: the JSON) and caches `cases/citedby/<cluster>/coupling.json` with the bulk data's stamp; never run by a build. The GUI's "Find coupled cases" runs it.
- **`stubs`:** a stub becomes a full case only if `build` can read its text. Otherwise it stays a stub and the output says why. After filling stubs, run `./build_all.sh cases`, or use `./build_all.sh --stubs` to do both.
- **`fixlinks`:** takes no options (it is handled before argparse); rewrites notes and saved Cited-by sections in place and needs the local database.

## casedb.py — local CourtListener database

```
./casedb.py load                  # every bulk file in bulk/, smallest first; finished tables skipped
./casedb.py load citation-map     # one table: courts citations citation-map parentheticals
                                  #            opinion-clusters dockets opinions
./casedb.py status
./casedb.py columns               # per bulk file: columns loaded, columns left out
./casedb.py index                 # full-text index for case2md.py find
./casedb.py case cases/<case>.json   # smoke test
```

The bulk files are in `bulk/` (`CL_BULK`), the database is `bulk/courtlistener.sqlite` (`CL_DB`), and the opinion text (about 110 GB) is in `db/opinions.sqlite` (`CL_OPS_DB`).

## cocite.py — co-citation clusters, five years at a time

```
./cocite.py prep                  # one pass over the database -> bulk/cocite-cache/ (rerun after a bulk load)
./cocite.py build                 # cluster every slice, then link clusters across slices into lineages
./cocite.py build --slices 2005   # one slice (2005-09) only
./cocite.py status
./cocite.py case --cite "463 U.S. 29"      # its clusters and top co-cited partners, slice by slice
./cocite.py cluster 2005-09:3              # one cluster: members, sub-clusters, what it came from / turned into
./cocite.py cluster 2010-14:1.3            # one sub-cluster
./cocite.py tree 2010                      # one slice: clusters, each with its sub-clusters
./cocite.py note --cite "463 U.S. 29"     # preview the ## Co-citation section a case note gets (appellate view)
./cocite.py lineage 17                     # one lineage across slices
./cocite.py together --cite "550 U.S. 544" --cite "556 U.S. 662"   # same cluster? decisions citing both
./cocite.py diag                           # citing decisions per slice, by court level
./cocite.py build --courts appellate       # only appellate citing decisions -> bulk/cocite-appellate.sqlite
./cocite.py tree 2010 --courts appellate   # every read command takes --courts too
./cocite.py shift --cite "375 U.S. 384" --courts appellate   # good-law signals from cluster change, per slice transition
./cocite.py shift --eval --courts appellate                   # the same, scored on goodlaw_eval.py's cases
```

Two cases are co-cited when one later decision cites both. Citing decisions are sliced by filing year. In each slice:

- **Nodes:** cases cited by 0.002% of the slice's decisions (`--min-cited-frac`), never fewer than 5 (`--min-cited`). The share keeps slices comparable as court output and coverage grow; `status` shows the cut each slice used.
- **Pairs:** cited together by `--min-support` (5) or more decisions. Decisions citing more than `--max-cites` (150) nodes are left out.
- **Clusters:** Leiden (modularity). Each case also gets a share in every cluster, so it can belong to several (`--share`, 0.15, lists it in a non-home cluster). Partners are ranked by cosine; lift is shown alongside.
- **Sub-clusters:** every cluster of `--split-over` (20) or more is clustered again (`1.3` = sub-cluster 3 of cluster 1). The split is kept only if it beats every one of `--nulls` (5) rewired copies of the cluster and their mean by `--sub-z` (4) standard deviations; otherwise the cluster stays whole.
- **Chance baseline:** each slice's modularity is compared with its own rewired copies, so `status` column "above" can be compared across slices.
- **Court views:** `--courts` keeps only citing decisions from those levels (scotus, circuit, district, state-supreme, state-appellate, other; shortcuts `appellate`, `federal`). The cited cases can come from any court. Each view has its own results file.
- **Lineage:** clusters and sub-clusters in adjacent slices that are each other's best match (Jaccard >= `--carry`, 0.2) share a lineage.

Housekeeping: `prep` must be rerun after a bulk load. Read commands (`status`, `case`, `cluster`, `tree`, `note`, `lineage`, `together`, `shift`, ...) open the results read-only and never touch a running build; when the file's schema is older than the code they exit with "rebuild with ./cocite.py build", and after such a schema bump `build` must be rerun in each view (each `--courts`/`--weight`/`--mix` combination is its own file).

**Proximity weights (`--weight`, `--mix`; `cocite_positions.py`):**

```
./cocite.py positions                      # where every citation sits: one row per occurrence -> bulk/cocite-cache/pos/ (all cores, resumable)
./cocite.py positions --status             # shards done / to do
./cocite.py positions --show 690840        # one opinion's rows, with context
./cocite.py positions --sample 2000        # random opinions: rates and speed, nothing written
./cocite.py positions --index              # sort the rows by decision -> cocite-cache/posidx/, needed by build --weight
./cocite.py build --courts appellate --weight sentence             # pairs weighted by same-sentence co-citation -> cocite-appellate-sentence.sqlite
./cocite.py build --courts appellate --weight sentence --mix 0.2   # binary pairs kept, re-weighted -> cocite-appellate-sentence-m0.2.sqlite
./cocite.py note --cite "463 U.S. 29" --courts appellate --weight sentence --mix 0.2   # read commands take the same flags to pick the file
./cocite.py prox-evidence --courts appellate --slice 2010          # blind reading file: pairs by sentence share -> build/cocite/, key kept apart
```

- `--weight decision|paragraph|sentence|string|kernel:<chars>` replaces "decisions citing both" with a weight from where the citations sit. Nodes and `--min-support` are unchanged. `--mix α` keeps the binary pairs and weights them α × support + (1 − α) × proximity; α = 1 is the binary build. The results file is named by view, weight and mix: `cocite-appellate-sentence-m0.2.sqlite`. Case notes read that file (`NOTE_VIEW` in `cocite.py`).
- `positions` is the one text pass; any new weighting is a cheap pass over the stored rows. After changing the extractor, bump its `VERSION` and the stale shards are redone.
- `prox-evidence` options: `--n`, `--min-support`, `--per-case`, `--passages`, `--window`, `--seed`.

**Labels, areas and the vault:**

```
./cocite.py evidence --courts appellate                 # build/cocite/evidence-<view>.jsonl: the biggest unlabelled lineages, with evidence
./cocite.py evidence --courts appellate --family-of "463 U.S. 29"   # every sub-cluster lineage under this case's cluster, labelled together
./cocite.py evidence --courts appellate --stale         # lineages whose labels drifted or were displaced
./cocite.py labels import labels.jsonl                  # descriptions -> data/cocite_labels.json (git-tracked)
./cocite.py labels match --courts appellate             # re-attach labels to lineages after a rebuild
./cocite.py labels check --courts appellate             # stable / drifted / displaced / orphaned counts
./cocite.py labels list --courts appellate
./cocite.py areas --courts appellate --method cpm --resolution 0.5 --shrink 10 --consensus 20   # the settled setting: group lineages into areas
./cocite.py areas --courts appellate --grid "cpm:0.5,1@0,10;modularity:1,2"   # score settings against data/cocite_gold.json, write nothing
./cocite.py area-evidence --courts appellate            # build/cocite/areas-<view>.jsonl, for naming the areas (--top 80)
./cocite.py gold-common cocite-appellate cocite-appellate-sentence-m0.2   # benchmark names every listed view can score -> build/cocite/gold-common.json
./cocite.py vault --courts appellate                    # md-cocite/: Co-citation map.md, Areas/, Lineages/ (--min-size 30, --core 12, --link-min 0.1, --out)
```

- **Labels:** a language model writes a name and description per lineage from the evidence file; `labels import` stores them with a fingerprint (the lineage's heaviest cases) so they re-attach after a rebuild. `evidence` options: `--top`, `--top-clusters`, `--min-slices`, `--min-size`, `--redo`, `--family`, `--min-family-size`.
- **Areas:** sub-cluster lineages pooled over all slices into broad areas. The settled flags are `--method cpm --resolution 0.5 --shrink 10 --consensus 20` (`--consensus-rounds` 6, `--consensus-tau` 0.5; `--split-over` 150, `--sub-z`, `--nulls` for splitting giants). `--grid` scores many settings against `--gold` (default `data/cocite_gold.json`) by pairwise F1 and writes nothing; `--sweep` compares resolutions. The grid line's two extra columns, `gold.med` (median lineages in the areas holding benchmark lineages) and `nongold` (share of those areas' lineages outside the benchmark), say how much of each area the benchmark actually grades. Why these settings: `ARCHITECTURE.md` §7.4.
- **`gold-common`:** needs a `--grid` run in each view first (it reads the `gold-missing-<view>.json` each one writes), then intersects the benchmark so two views are scored on the same names.
- **`vault`:** generator-owned notes; `sync_vault.sh` mirrors them. Fixed headings so a personal map of content can transclude them; starter in `templates/cocite-personal-moc.md`.

**Good-law signals (`shift`, in `cocite_shift.py`):** per slice transition, four signals: (1) a later-decided successor B, cited by at least 25% of the decisions citing A and growing at least 3 times faster; (2) A's sub-cluster lineage breaks; (3) A's citing count falls further than 90% of cases like it (same decade, similar count); (4) A slides from the core of its sub-cluster to the edge. On `goodlaw_eval.py`'s cases (appellate view), signal 1 fires at the overruling for 37 of 41 and names the overruling decision for 35, but it also fires for 11 of the 20 never-overruled cases (a newer leading case taking over: Heller→Bruen, Twombly→Iqbal). No signal fires before an overruling more often than chance. Case notes show signal 1 as a line in the case-profile box ("Role changed 2005-09: likely successor Bowles ..."); it never changes the good-law colour. Every transition of the eval: `build/cocite/shift-eval-<view>.tsv`.

Results go to `bulk/cocite.sqlite` (`CL_COCITE_DB`). Derived and disposable: a slice is rebuilt when the bulk load or the parameters change. `cocite.summary(cluster_id)` returns what a case note will show.

## Syncing

- **`./sync_vault.sh`:** mirrors `md/` into `~/Obsidian/usc-vault/08-Reference/USC`, `md-cases/` into `08-Reference/Cases` and `md-cocite/` into `08-Reference/Co-citation`. It deletes anything in those vault folders that isn't in the source, so keep your own notes elsewhere in the vault. It refuses to run when a source holds fewer than 90% of the notes already in the vault (a partial build); `FORCE=1` overrides. Options: `VAULT=/path`, `DRY=1`, `FORCE=1`. `build_all.sh` exits non-zero when any job failed, so `./build_all.sh && ./sync_vault.sh` is safe.
- **`./sync_remarkable.py`:** uploads new or changed PDFs to the tablet through rmfakecloud and rmapi, under `/Archive/Laws/USC/Title-NN/` and `/Archive/Laws/Cases/...`. It never deletes on the tablet. `state/rm_manifest.json` records what was uploaded. Use `--dry-run` to see the plan. A changed PDF **replaces** the tablet copy and its pen annotations go with it, so more than 10 replacements in one run stop unless `--yes` is given. Builds are byte-reproducible (`SOURCE_DATE_EPOCH` plus a fixed PDF trailer ID), so rebuilding an unchanged source does not count as a change.

## Other

- **`./link_audit.py [md]`:** counts US Code notes with no wikilinks in or out, grouped by title and heading type.

## Running things at the same time

- **`./casemap.py stubs` during `./build_all.sh usc`:** safe; they don't share files.
- **Two `build_all.sh` runs at once:** works. The only side effect is that one PDF may be rebuilt on the next run, because both runs update `state/built.tsv`.
- **Case builds in parallel:** safe; they lock `cases/_index.json` while updating it.

## Requirements

- Python 3.10 with the packages in `requirements.txt` (`lxml`; numpy, scipy, python-igraph for `cocite.py`): `pip install -r requirements.txt`, or `sudo apt install python3-lxml python3-numpy python3-scipy python3-igraph`
- pdflatex (TeX Live) with the XCharter, tcolorbox and ulem packages
- `pdftotext` (poppler-utils), for RECAP and Supreme Court filings
- `curl`, for `scotus.py fetch`
- `lbzip2` or `pbzip2` (optional), to load bulk files on every core
- `secret-tool`, for a token stored in the keyring
- `rsync`, for `sync_vault.sh`
- `rmapi`, for `sync_remarkable.py`

Tests: `python3 -m unittest discover tests` from the repo root. No database or network; the fixture builds are skipped when `xml/usc01.xml`, a `cases/*.json` or pdflatex is missing.

## Local databases and how they map to CourtListener

`casedb.py load` reads CourtListener's quarterly bulk files (`bulk/<name>-YYYY-MM-DD.csv.bz2`) and keeps only the columns these scripts use. The IDs are CourtListener's own, so a local row and the website/API record are the same object.

**`bulk/courtlistener.sqlite`** — the citation graph and case metadata:

| Table | Bulk file | What's in it | CourtListener API equivalent |
|---|---|---|---|
| `courts` | `courts` | court ID, citation abbreviation, names, jurisdiction | `/courts/` |
| `cites` | `citations` | every reporter citation of a cluster (volume, reporter, page, type) | `citations` field of `/clusters/` |
| `edges` | `citation-map` | which opinion cites which, and how many times (`depth`) | `/opinions-cited/` |
| `parens` | `parentheticals` | the parentheticals courts wrote about a cited opinion, with a score | none: [bulk data only](https://wiki.free.law/c/courtlistener/help/api/rest/v4/case-law), so `find` and `citedby` can do things the API can't |
| `clusters` | `opinion-clusters` | one row per decision: names, date, citation count, judges, status, summary, syllabus, docket link | `/clusters/` |
| `dockets` | `dockets` | court, docket number, case name, nature of suit, cause, dates, judge, PACER case ID | `/dockets/` |
| `loaded` | — | which bulk file each table came from, and when it was loaded | — |

**`db/opinions.sqlite`** — `opinions` table from the `opinions` bulk file: one row per opinion (majority, concurrence, dissent…), with cluster, type and author. The text is compressed, and only the first available text field is kept (for example `html_with_citations`, which carries the Harvard-format tags `build` reads). The API equivalent is `/opinions/`.

**`bulk/search.sqlite`** — built by `casedb.py index`, not downloaded: full-text (FTS5) indexes of case names and parenthetical text, used by `case2md.py find`. It's rebuilt automatically when clusters or parentheticals are reloaded.

**Joins:** `cites.cluster_id` and `opinions.cluster_id` → `clusters.id`; `clusters.docket_id` → `dockets.id`; `edges.citing`/`edges.cited` and `parens.described`/`parens.describing` → `opinions.id`; `dockets.court_id` → `courts.id`.

## CourtListener bulk data not loaded locally

Files in the same bulk-data folder (`https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/`, listing at `list.html?prefix=bulk-data/`). They're regenerated quarterly (last day of March, June, September and December). Sizes are from the 2026-06-30 set, compressed.

| Bulk file | What it adds | Size |
|---|---|---|
| `fjc-integrated-database` | Federal Judicial Center case records for federal civil, criminal and bankruptcy cases (filing and termination, disposition, nature of suit), matched to dockets | 280 MB |
| `originating-court-information` | the lower court behind an appeal: docket number, judge, dates | 21 MB |
| `oral-arguments` | metadata for oral-argument recordings (case, court, date, duration, file location). The audio files themselves are separate downloads. | 684 MB |
| `people-db-people`, `-positions`, `-educations`, `-political-affiliations`, `-races`, `-retention-events`, `-schools` | the judges database: biographies, appointments and courts served, education, appointing president | about 2 MB together |
| `search_opinioncluster_panel`, `search_opinion_joined_by`, `search_opinioncluster_non_participating_judges` | which judges sat on each decision, joined each opinion, or didn't take part (links to the judges database) | 5 MB |
| `financial-disclosures` and `-investments`, `-gifts`, `-debts`, `-agreements`, `-reimbursements`, `-non-investment-income`, `-spousal-income` | judges' financial disclosure reports | about 45 MB |
| `unmatched-citations` | citations found in opinion text that don't resolve to a case in CourtListener | 216 MB |
| case-law embeddings | vector embeddings of the opinions (ModernBERT model), for semantic search; published separately from the CSV files | about 2 TB |
| `schema-YYYY-MM-DD.sql` | the database schema for all of the above, including every column not kept locally | — |

Columns `casedb.py` doesn't load are still in the files already downloaded to `bulk/`. `./casedb.py columns` lists, for each file, the columns loaded and the ones left out, and shows every column of the files that aren't loaded at all. Keeping one means adding it to `TABLES` in `casedb.py` and reloading that table with `./casedb.py load --force <name>`.

**API only, no bulk file:** RECAP docket entries and filed documents (what `case2md.py recap fetch` uses), and anything newer than the last quarterly export.

## License and how this was written

MIT — see `LICENSE`.

Most of the code and documentation in this repository was written by Claude (Anthropic's AI model), working from my specifications, design decisions and review. The design, the selection and arrangement of the parts, and the edits are mine. Parts that are purely AI-generated may not be protected by copyright; the MIT license covers whatever is, and the no-warranty terms apply to all of it.
