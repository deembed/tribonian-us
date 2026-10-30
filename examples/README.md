# Examples

Finished outputs, copied from `pdf/` and `md-cases/` (those folders are git-ignored). Each PDF's header
says when it was retrieved or built; these were built in October 2026 and are refreshed rarely, so the
current code may lay things out a little differently.

| File | Made by | What it shows |
|---|---|---|
| `1-usc-ch1.pdf` | `tribonian build usc` | US Code formatting: 1 U.S.C. ch. 1, Rules of Construction (2 pages) |
| `463-us-29-state-farm.pdf` | `tribonian case fetch --cite "463 U.S. 29"`, `tribonian map citedby --cite "463 U.S. 29" --full`, then `tribonian case build cases/463-u-s-29_*.json --cases --coupling --fetch --fetch-state-law` | A case: headmatter and the legend of marks, case profile with the good-law check and cited-by chart, co-citation boxes, then the opinions with cites tinted and the sentences later opinions cite it for hatched; at the end Quote lineage, Cited by, The Cases Cited, The Law Cited and Cites not resolved (37 pages) |
| `463-us-29-state-farm.md` | same build | The Obsidian note for the same case. Its `[[wikilinks]]` resolve inside a vault, not on GitHub |
| `8-usc-1101.pdf` | `tribonian usc xml/usc08.xml --section /us/usc/t8/s1101 --credits` | A long definitions section (visa classes down to (a)(15)(H)(i)(b)), then how appellate courts cite it: Cited with by five-year slice, Cited by, Recent appellate cites |
| `42-usc-1395ww.pdf` | `tribonian usc xml/usc42.xml --section /us/usc/t42/s1395ww --credits` | Depth: Medicare inpatient payment, nested to items like (d)(5)(F)(vii)(I) |
| `scotus-25-417.pdf` | `tribonian scotus fetch 25-417` then `tribonian scotus build 25-417 --fetch --fetch-state-law` | Nielsen v. Watanabe (a *Bivens* claim): each filing opens with its co-citation/statute header box in its Rule 33.1(g) cover color; the authority and provision tables by side; cites tinted by whether they resolved; The Law Cited; Cites not resolved |
| `scotus-25-581.pdf` | the same, for 25-581 | St. Mary Catholic Parish v. Roy: a large docket (600+ pages, many amici) and state law cited from many states, fetched sparse (only the cited sections) |
| `hid-22-cv-00168-watanabe.pdf` | `tribonian case recap fetch --docket 63239531` then `tribonian case recap build cases/recap/hid/hid-22-cv-00168-watanabe.json --cases --fetch --fetch-state-law` | The district court docket below No. 25-417 (D. Haw.), from RECAP: the legend of marks, the court's orders with their cites tinted (Westlaw cites matched to their RECAP dockets), The Cases Cited, The Law Cited, Cites not resolved |
| `subcluster-2020-24-2.3-agency-review.pdf` | `tribonian cocite subdoc 2020-24:2.3` | A co-citation sub-cluster explainer: judicial review of agency action, 2020–24 (261 pages; the case table is its contents page) |

The page size (about 7.0 × 9.4 in) is the reMarkable Paper Pro's screen. The case and sub-cluster PDFs need the
local CourtListener database for their profile, good-law and co-citation boxes; see the main README.
