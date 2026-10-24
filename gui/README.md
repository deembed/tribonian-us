# lawgui

Read-only desktop browser over what the LawFormatting scripts produce: co-citation areas, lineages,
slices, clusters and sub-clusters (`cocite*.sqlite`), and the built case notes (`cases/_index.json`,
`md-cases/`, `pdf/`). It never writes to any of them (its own writes: SVG exports and the recent-searches list, below).

```
cd gui
cp gui.toml.example gui.toml     # then edit the paths
cargo run --release
```

- Start screen: View Clusters, View Cases, Search for Cases, SCOTUS. Sub-Cluster Search is TBD.
- Back/forward: the arrow buttons or Alt+Left / Alt+Right. The breadcrumb shows the last few pages.
- The app opens a view only if its `meta.schema` matches `db::SCHEMA` (cocite.py `SCHEMA_VERSION`);
  other views are listed greyed out. After a schema bump in cocite.py, update `db::SCHEMA` and the queries.
- Lineage pages need the `subs(lineage)` / `clusters(lineage)` indexes: `./cocite.py index --courts ...`
  once per view built before 2026-10-04 (a new build includes them).
- Area pages carry a history graph (`src/graph.rs`), drawn like `git log --graph`: one lane per lineage,
  time running down (or up, with "Newest at top"), or "Left to right" (slice labels along the top, names of
  labelled lineages in a band under the lanes, starting under their first dot). Click a dot to open its lineage.
- Export SVG (beside the graph controls) writes the graph as shown, orientation and filters included, on a white
  background, to `<repo_dir>/build/exports/area-<code>-<orientation>.svg` and opens it. This is the only file
  the app itself writes; everything else on disk comes from the Python scripts.
- Recent searches are kept between sessions in `$XDG_DATA_HOME/lawgui/recent-searches.json`
  (default `~/.local/share/lawgui/`), newest first, at most 30. "Clear" beside the heading empties it.
- Test aid: `LAWGUI_DELAY_MS=800 cargo run` slows every page load, to see the loading placeholders.
- Case tables sort by any column heading (click again to flip). The Good law column reads
  `cases/citedby/<ref>/profile.json`, so it is filled only for cases `casemap.py goodlaw` has run on.
- Hollow markers on the history graph are links to lineages outside the area; click one to go there.
- Search for Cases (`src/search.rs`, its own worker thread): case names (most cited first), a citation
  such as `463 U.S. 29`, or Parentheticals (what later courts said a case stands for, ranked by how many
  parentheticals describe it). Uses `search.sqlite` and `courtlistener.sqlite` from `bulk_dir` unless
  `search_db` / `main_db` say otherwise. Tests: `cargo test` (query building, citation detection).
- Build on the case page runs `casemap.py get --cluster N` then `case2md.py build cases/<file>` in `repo_dir`
  (interpreter: `python` in gui.toml), streaming output into a log panel at the bottom. A build only counts
  as done when case2md prints its `wrote pdf/...` line, as in build_all.sh. Afterwards the app rereads the
  case files and reloads the page. One build at a time.
- The case page shows at once, with no build: details from the main database (docket, dates, judges,
  CourtListener's citing count, every citation, headmatter, syllabus/summary when present), the cached case
  profile (citations per year, successor) when a build wrote one, how later courts describe the case (cached,
  or read live from the parentheticals), and the note's front matter. Good law says "not calculated" until a
  build computes it (it reads up to ~2,000 citing opinions, so it is slow). `opinions_db` in gui.toml
  (default repo_dir/db/opinions.sqlite) maps a case to its opinions for the live parentheticals.
- Two buttons on the case page: **Quick build** (`case2md.py build --quick-goodlaw`: note, PDF and profile with the quick good-law check, the newest 100 citing opinions, instead of
  the slow good-law check, keeping an earlier result) and **Build** (everything).
- Builds queue: the status line at the bottom shows "Building k of n" and what is queued, then "Done" (with any
  failures). A build added after "Done" starts a new run with fresh counts. "Output" opens the log.
- **What it draws on** (case page, live): the case's opinions (`opinions_db`) -> `edges` in the main database
  -> the cited cases, then each cited case's sub-cluster in the newest full slice: count, share bar, clickable
  sub-cluster and area, and how many cited cases are in no sub-cluster. Same definition as `cocite.draws_on`,
  which the notes and PDFs use. It places any case, also one the co-citation map never holds.
- **Find coupled cases** queues `casemap.py coupling --cluster N` through the build queue ("Running k of n").
  It writes `cases/citedby/<N>/coupling.json`; the page then shows **Coupled cases**: score, shared cited
  cases (hover a case for the list, rarest first), and when it was computed. Never run automatically.
- **Explain (note + PDF)** on every sub-cluster page (and unsplit clusters) queues `cocite.py subdoc <slice:sub> --view <open view>`:
  every member case with its citing counts, parentheticals and good law, plus the lineage's joins and departures. **Explain +
  good law** adds `--goodlaw` (the quick check on members with none cached first; `--goodlaw full` from a terminal for the slow one). **Open PDF** / **Open note** appear
  once the files exist (`pdf/Co-citation/<slice>/`, `md-cocite/Sub-clusters/<slice>/`; another view adds a `<view>/` folder).
- **Why red / yellow / green** (case page, under Good law; open for red and yellow): casemap.py's rule for that colour,
  what the check read (every citing opinion from a court that could overrule the case, up to 1,500, plus the 500 newest),
  and each warning from the profile: year, court, kind, the word, "from a court above", the citing case (a link) and the
  snippet with the citation highlighted. The Good law cell in tables shows the first three as a hover.
- **Grey = dormant**: never cited, or not cited in the last 5 full years before the bulk data's date (case2md RECENT).
  From the cached profile's citations per year; else CourtListener's citing count when it is 0; else, on the case page
  only and for cases cited at most 300 times, a live check of the newest citing decision. Red and yellow keep their
  colour and get a grey "dormant" tag; green or not calculated shows grey "dormant" with the reason.
- **SCOTUS** (`src/scotus.rs`, `src/scotus_page.rs`): every docket `scotus.py fetch` saved in `cases/scotus/<docket>.json`
  (status read from the docket's own entries, last entry, filings, whether the note/PDF is built). A docket page shows the
  question presented (the Clerk's PDF text, else the docket note's section), the decision below (opens its case page),
  the filings (note in Obsidian, local or web PDF; filing notes are matched by their front matter `source`), and the
  proceedings and orders. **Fetch updates** / **Build** / **Fetch & build** queue `scotus.py fetch|build <docket>` through the
  build queue; Build counts as done only on its `wrote pdf/...` line. "Add a docket" (side panel) takes `25-579` or
  `No. 25-579` and queues fetch then build; applications (`25A374`) are refused, as `scotus.py fetch` can't read them.
- **Recent activity** (SCOTUS page) reads `cases/scotus/_recent.json` from `scotus.py recent`: Decided (slip opinions),
  Granted, awaiting decision (Granted & Noted lists), Orders (order lists; denials hidden unless asked). Each row opens a
  fetched docket or offers Fetch & build. **Refresh** queues `scotus.py recent`. There is no public list of all pending
  cert petitions, so the page offers the docket box instead.
- Search for Cases (names mode) puts matching scotus.py dockets above the results: a docket number (`25-579`, `No. 25-579`)
  or words in the case title. A docket number not fetched yet gets a Fetch & build button. In View Cases, the
  `Pending SCOTUS/` docket and filing notes are links to their docket page.
- **Cited together** ("where used"; `Page::CoCited`, `db::cocited_in`, `src/cocache.rs`): ＋ beside a search hit or on a
  case page adds the case to the filter in the Search side panel (✓ takes it out); **Find decisions citing all n** lists every
  decision citing every case in the filter, newest first. One case is plain where-used; **Cited by…** on a case page opens that
  directly. Read from cocite.py's edge cache (`cocite-cache/` beside the views: `citing.npy`, `by_cited.npy`,
  `cited_sorted.npy`, `ref_year.npy`), the decision-level edges the co-citation map is built from, so a decision counts once
  whichever of its opinions cites, and the citing side is all courts. Meyer + Twombly: 141 decisions in under a second. Names,
  citations and courts come from the main database for the newest 500 only (each is a disk seek); the total is always shown.
  The cache is only as new as the last `./cocite.py prep` (its bulk-load date is on the page). Sort by any column heading;
  the page's Filter box matches name, citation or court (`ca4`, `scotus`) among the listed rows.
- **Pop out** (top bar): the current page in a window of its own, content only, with its own back/forward (Alt+Left /
  Alt+Right) and "Open in main window". **Ctrl+click** any link (in the main window or a pop-out) opens it in a new window
  instead. Pop-outs share the page cache and the build queue with the main window; closing one forgets its history.
