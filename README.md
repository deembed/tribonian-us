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
| `md/` | US Code notes, one per section, in `t<NN>/ch<NN>/` | `build_all.sh` |
| `md-cases/` | case notes (full and stub); subfolders such as `FOIA/` | `case2md.py`, `casemap.py` |
| `pdf/` | the PDFs; case PDFs mirror `md-cases/` subfolders | `build_all.sh`, `case2md.py build` |
| `build/` | `.tex`, logs, build records, scratch | the build |
| `bulk/`, `db/` | CourtListener bulk files and the local SQLite databases | `casedb.py` |

Everything the scripts generate is ignored by git (`.gitignore`); tracked are the scripts, the docs, `cocite_labels.json` and `cocite_gold.json` (hand- and model-written inputs that can't be rebuilt), and `templates/`.

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

How it decides what to build: `build/built.tsv` records the PDF that each successful job made. A job gets built when it has no record, its PDF is gone, or its source is newer than its PDF. Failures aren't recorded, so the next run retries them. The list of jobs that were due is saved in `build/todo.txt`.

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
./case2md.py build cases/463-u-s-29_*.json      # -> md-cases/, build/, pdf/
./case2md.py find glomar                        # search your notes, then the local database
./case2md.py find glomar --get 3                # take result 3 from the database and build it
./case2md.py recap fetch --docket 4949363       # district-court filings (uses the API)
./case2md.py recap build cases/recap/<docket>.json
./case2md.py usage                              # API requests used; --days N for history
```

- **Where notes go:** a note that already exists anywhere in `md-cases/` is rewritten in place, so moving a note (inside `md-cases/`, not in the vault) is how you file it. New notes follow the `FOLDERS` table in the script (for example, Freedom of Information Act and Privacy Act cases go to `FOIA/`).
- **API token:** `COURTLISTENER_TOKEN`, or the GNOME keyring (`secret-tool store --label courtlistener service courtlistener`).
- **Rate limits:** free tier 5 a minute, 50 an hour, 125 a day; set `CL_LIMITS=10,75,300` for a paid tier. Requests pace themselves. At the daily limit a command stops cleanly, and rerunning it resumes from the cache.
- **Usage log:** `cases/_api_log.json` keeps the last 24 hours (used for the limits). `cases/_api_history.tsv` keeps every request (time, command, API path). Any command that made requests ends with `CourtListener API: N requests by this command; M of 125 left today`.

## casemap.py — citation graph (no API unless `--api`)

```
./casemap.py get --cite "401 U.S. 402"          # one case from the local database -> cases/*.json + note
./casemap.py map --cite "463 U.S. 29" --depth 2 # notes for everything within 2 citation steps
./casemap.py citedby cases/463-u-s-29_*.json    # "Cited by" section: who cites it, for what
./casemap.py stubs --list                       # stub notes, most-cited first
./casemap.py stubs                              # fill stubs from the local database
./casemap.py stubs --api --max 20               # fall back to CourtListener for the rest
./casemap.py stubs brown 108283                 # only matching note names or cluster IDs
```

- **`map`:** `--text K` gives full text to the first K steps (default `depth - 1`); the outer ring becomes stubs. `--max` caps the size of the map.
- **`citedby`:** options are `--list`, `--full`, `--level`, `--sample-over`, `--margin`, `--top`, `--steps` and `--min-run`; see `./casemap.py citedby -h`.
- **`stubs`:** a stub becomes a full case only if `build` can read its text. Otherwise it stays a stub and the output says why. After filling stubs, run `./build_all.sh cases`, or use `./build_all.sh --stubs` to do both.

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
- **Clusters:** Leiden (modularity). Each case also gets a share in every cluster, so it can belong to several. Partners are ranked by cosine; lift is shown alongside.
- **Sub-clusters:** every cluster of `--split-over` (20) or more is clustered again (`1.3` = sub-cluster 3 of cluster 1). The split is kept only if it beats the same cluster rewired at random (`--nulls` 5 copies) by `--sub-z` (4) standard deviations; otherwise the cluster stays whole.
- **Chance baseline:** each slice's modularity is compared with its own rewired copy, so `status` column "above" can be compared across slices.
- **Court views:** `--courts` keeps only citing decisions from those levels (scotus, circuit, district, state-supreme, state-appellate, other; shortcuts `appellate`, `federal`). The cited cases can come from any court. Each view has its own results file.
- **Lineage:** clusters and sub-clusters in adjacent slices that are each other's best match (Jaccard >= `--carry`, 0.2) share a lineage.

**Good-law signals (`shift`, in `cocite_shift.py`):** per slice transition, four signals: (1) a later-decided successor B, cited by at least 25% of the decisions citing A and growing at least 3 times faster; (2) A's sub-cluster lineage breaks; (3) A's citing count falls further than 90% of cases like it (same decade, similar count); (4) A slides from the core of its sub-cluster to the edge. On `goodlaw_eval.py`'s cases (appellate view), signal 1 fires at the overruling for 37 of 41 and names the overruling decision for 35, but it also fires for 11 of the 20 never-overruled cases (a newer leading case taking over: Heller→Bruen, Twombly→Iqbal). No signal fires before an overruling more often than chance. Case notes show signal 1 as a line in the case-profile box ("Role changed 2005-09: likely successor Bowles ..."); it never changes the good-law colour. Every transition of the eval: `build/cocite/shift-eval-<view>.tsv`.

Results go to `bulk/cocite.sqlite` (`CL_COCITE_DB`). Derived and disposable: a slice is rebuilt when the bulk load or the parameters change. `cocite.summary(cluster_id)` returns what a case note will show.

## Syncing

- **`./sync_vault.sh`:** mirrors `md/` into `~/Obsidian/usc-vault/08-Reference/USC`, `md-cases/` into `08-Reference/Cases` and `md-cocite/` into `08-Reference/Co-citation`. It deletes anything in those vault folders that isn't in the source, so keep your own notes elsewhere in the vault. It refuses to run when a source holds fewer than 90% of the notes already in the vault (a partial build); `FORCE=1` overrides. Options: `VAULT=/path`, `DRY=1`, `FORCE=1`. `build_all.sh` exits non-zero when any job failed, so `./build_all.sh && ./sync_vault.sh` is safe.
- **`./sync_remarkable.py`:** uploads new or changed PDFs to the tablet through rmfakecloud and rmapi, under `/Archive/Laws/USC/Title-NN/` and `/Archive/Laws/Cases/...`. It never deletes on the tablet. `build/rm_manifest.json` records what was uploaded. Use `--dry-run` to see the plan. A changed PDF **replaces** the tablet copy and its pen annotations go with it, so more than 10 replacements in one run stop unless `--yes` is given. Builds are byte-reproducible (`SOURCE_DATE_EPOCH` plus a fixed PDF trailer ID), so rebuilding an unchanged source does not count as a change.

## Other

- **`./link_audit.py [md]`:** counts US Code notes with no wikilinks in or out, grouped by title and heading type.

## Running things at the same time

- **`./casemap.py stubs` during `./build_all.sh usc`:** safe; they don't share files.
- **Two `build_all.sh` runs at once:** works. The only side effect is that one PDF may be rebuilt on the next run, because both runs update `build/built.tsv`.
- **Case builds in parallel:** safe; they lock `cases/_index.json` while updating it.

## Requirements

- Python 3 with `lxml`
- numpy, scipy, python-igraph for `cocite.py` (`sudo apt install python3-numpy python3-scipy python3-igraph`)
- pdflatex (TeX Live) with the XCharter and tcolorbox packages
- `pdftotext` (poppler-utils), for RECAP documents without text
- `secret-tool`, for a token stored in the keyring
- `rsync`, for `sync_vault.sh`
- `rmapi`, for `sync_remarkable.py`

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
