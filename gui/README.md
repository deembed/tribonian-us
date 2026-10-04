# lawgui

Read-only desktop browser over what the LawFormatting scripts produce: co-citation areas, lineages,
slices, clusters and sub-clusters (`cocite*.sqlite`), and the built case notes (`cases/_index.json`,
`md-cases/`, `pdf/`). It never writes to any of them (its own writes: SVG exports and the recent-searches list, below). Scope: `claude/scope-gui.md` in the Project.

```
cd gui
cp gui.toml.example gui.toml     # then edit the paths
cargo run --release
```

- Start screen: View Clusters, View Cases. Search for Cases is v1; Sub-Cluster Search is TBD.
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
- Two buttons on the case page: **Quick build** (`case2md.py build --no-goodlaw`: note, PDF and profile without
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
