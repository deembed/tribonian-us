# Examples

Finished outputs, copied from `pdf/` and `md-cases/` (those folders are git-ignored). Each PDF's header
says when it was retrieved or built; these were built in October 2026 and are refreshed rarely, so the
current code may lay things out a little differently.

| File | Made by | What it shows |
|---|---|---|
| `1-usc-ch1.pdf` | `./build_all.sh usc` | US Code formatting: 1 U.S.C. ch. 1, Rules of Construction (2 pages) |
| `463-us-29-state-farm.pdf` | `./case2md.py fetch --cite "463 U.S. 29"` then `./build_all.sh cases` | A case: headmatter, case profile with the good-law check and cited-by chart, co-citation boxes, then the opinions (16 pages) |
| `463-us-29-state-farm.md` | same build | The Obsidian note for the same case. Its `[[wikilinks]]` resolve inside a vault, not on GitHub |
| `subcluster-2020-24-2.3-agency-review.pdf` | `./cocite.py subdoc 2020-24:2.3` | A co-citation sub-cluster explainer: judicial review of agency action, 2020–24 (261 pages; the case table is its contents page) |

The page size (about 7.0 × 9.4 in) is the reMarkable Paper Pro's screen. The case and sub-cluster PDFs need the
local CourtListener database for their profile, good-law and co-citation boxes; see the main README.
