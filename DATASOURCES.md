# Data sources: state law

Where each jurisdiction's statutes and constitution come from, how they are fetched, and what is missing and why.
Generated table below from `src/tribonian/data/formulas/*.toml` (re-run the snippet at the end to refresh it);
the narrative sections are maintained by hand. Status per state: `tribonian state status` (docs/state-tracker.toml).

## Ground rules

- **Official sources only**, from the state's own publisher (legislature, revisor, code commission), or a bulk file
  the publisher itself provides. Where a state publishes an "official" print edition and an unofficial online one,
  the formula and its status file say which we use.
- **robots.txt is obeyed** (RFC 9309: an unreadable robots.txt means *don't*), one request per second per site.
- **We never work around a refusal.** If a server blocks our client, the official file is downloaded by hand in a
  browser (see "Manual downloads") or the state is left out.
- **Stated terms are respected.** If a site's terms forbid automated access, we don't fetch from it.
- **Every build proves nothing was lost** (text conservation, parse coverage, schema, unmapped text, numbering,
  identifiers). Verify proves no text is lost; it does not prove every section was *found* -- formulas are also
  checked by reading the output.

## Coverage

47 of 51 jurisdictions (50 states + DC) build from official sources. Not covered: **Arkansas, Georgia, Mississippi,
Tennessee** (see "States we can't cover"). Constitutions are covered wherever the state publishes one in a usable
form (DC has none; New Hampshire's is blocked, below).

## Sources by state

| State | Code: host, fetch, format |
|---|---|
| AK | const: ltgov.alaska.gov, template, html<br>statutes: www.akleg.gov, template, html |
| AL | code: alison.legislature.state.al.us, template, alison-json<br>const: alison.legislature.state.al.us, template, alison-json |
| AZ | ars: www.azleg.gov, index, html<br>const: www.azleg.gov, index, html |
| CA | code: downloads.leginfo.legislature.ca.gov, zip (range), ca-tables<br>const: downloads.leginfo.legislature.ca.gov, zip (range), ca-tables |
| CO | const: olls.info, template, html<br>crs: olls.info, template, html |
| CT | cgs: www.cga.ct.gov, index, html |
| DC | code: codeload.github.com, zip, oll-xml |
| DE | code: delcode.delaware.gov, index, html<br>const: delcode.delaware.gov, index, html |
| FL | const: www.flsenate.gov, template, html<br>fs: www.leg.state.fl.us, template, html |
| HI | const: lrb.hawaii.gov, template, html<br>hrs: data.capitol.hawaii.gov, index, html |
| IA | code: www.legis.iowa.gov, template, iowa-xml<br>codepdf: www.legis.iowa.gov, template, pdf-text<br>const: www.legis.iowa.gov, template, pdf-text |
| ID | code: legislature.idaho.gov, index, html<br>const: legislature.idaho.gov, index, html |
| IL | const: lrb.ilga.gov, template, html<br>ilcs: www.ilga.gov, index, html<br>ilcsweb: www.ilga.gov, index, html |
| IN | const: iga.in.gov, zip (manual), pdf-text<br>ic: iga.in.gov, zip (manual), html |
| KS | const: ksrevisor.gov, index, html<br>ksa: ksrevisor.gov, index, html |
| KY | const: apps.legislature.ky.gov, index, pdf-text<br>krs: apps.legislature.ky.gov, index, pdf-text |
| LA | const: www.legis.la.gov, index (postback), html<br>rs: www.legis.la.gov, index (postback), html |
| MA | const: malegislature.gov, template, html<br>gl: malegislature.gov, index, html |
| MD | code: mgaleg.maryland.gov, list, html<br>const: mgaleg.maryland.gov, list, html |
| ME | const: legislature.maine.gov, template, html<br>statutes: legislature.maine.gov, index, html |
| MI | const: www.legislature.mi.gov, template, pdf-text<br>mcl: legislature.mi.gov, template, html |
| MN | const: www.revisor.mn.gov, template, html<br>stat: www.revisor.mn.gov, template, html |
| MO | const: revisor.mo.gov, template, html<br>rsmo: revisor.mo.gov, template, html |
| MT | const: mca.legmt.gov, index, html<br>mca: mca.legmt.gov, index, html |
| NC | const: www.ncleg.gov, template, html<br>gs: www.ncleg.gov, template, html |
| ND | cc: www.ndlegis.gov, template, pdf-text<br>const: ndlegis.gov, template, pdf-text |
| NE | const: nebraskalegislature.gov, template, html<br>rs: nebraskalegislature.gov, template, html |
| NH | rsa: gc.nh.gov, index, html |
| NJ | const: njleg.gov, template, html<br>consttxt: pub.njleg.gov, zip, text-lines<br>statutes: pub.njleg.gov, zip, text-lines |
| NM | const: nmonesource.com, template, html<br>nmsa: nmonesource.com, template, html |
| NV | const: www.leg.state.nv.us, template, html<br>nrs: www.leg.state.nv.us, template, html |
| NY | const: legislation.nysenate.gov, template, nysenate-json<br>laws: legislation.nysenate.gov, template, nysenate-json |
| OH | const: codes.ohio.gov, index, html<br>orc: codes.ohio.gov, index, html |
| OK | const: www.oklegislature.gov, template, pandoc<br>os: www.oklegislature.gov, template, pdf-text |
| OR | const: www.oregonlegislature.gov, template, html<br>ors: www.oregonlegislature.gov, template, html |
| PA | const: www.palegis.us, template, html<br>cs: www.palegis.us, template, html |
| RI | const: rilegislature.gov, template, html<br>gl: webserver.rilegislature.gov, index, html |
| SC | code: www.scstatehouse.gov, index, html<br>const: www.scstatehouse.gov, template, pdf-text |
| SD | const: sdlegislature.gov, template, html<br>sdcl: sdlegislature.gov, template, html |
| TX | codes: tcss.legis.texas.gov, template, html<br>const: tcss.legis.texas.gov, template, html |
| UT | code: le.utah.gov, index, utah-xml<br>const: le.utah.gov, template, utah-xml |
| VA | code: law.lis.virginia.gov, template, html<br>const: law.lis.virginia.gov, template, html |
| VT | app: legislature.vermont.gov, index, html<br>const: legislature.vermont.gov, template, html<br>ucc: legislature.vermont.gov, index, html<br>vsa: legislature.vermont.gov, index, html |
| WA | const: leg.wa.gov, template, html<br>rcw: app.leg.wa.gov, index, html |
| WI | const: docs.legis.wisconsin.gov, scroll, html<br>stats: docs.legis.wisconsin.gov, scroll, html |
| WV | code: code.wvlegislature.gov, index, html<br>const: code.wvlegislature.gov, template, html |
| WY | const: wyoleg.gov, template, pdf-text<br>statutes: wyoleg.gov, template, pdf-text |

Formats: `html` = the shared html-paragraphs parser on the publisher's pages; the others are converters in
`src/tribonian/uslm/transforms/` that turn a non-HTML source into HTML blocks first, each checked to lose no text
(`pdf-text` PDF via pdftotext; `pandoc` RTF/Word via LibreOffice; `nysenate-json`, `alison-json` JSON APIs;
`utah-xml`, `iowa-xml`, `oll-xml` XML; `text-lines` plain text; `ca-tables` California's database tables).
Fetch: `template` one file per item; `index` index page(s) -> linked pages; `list` JSON list -> pages; `scroll`
windowed pages joined; `zip` a bulk archive (`range`: only the needed members, by HTTP Range requests).

## Manual downloads and keys

| What | Why | Where it goes |
|---|---|---|
| Indiana Code zip (https://iga.in.gov/laws/ic/downloads, "ZIP (HTML only)") | iga.in.gov answers our client with its empty app page, even for the zip | `law/in/ic/_bulk/` |
| New York Senate API key (free, https://legislation.nysenate.gov/) | the Open Legislation API requires one | env `NYSENATE_API_KEY` in your shell profile, never in the repo |

## Known issues

- **New Hampshire constitution:** www.nh.gov blocks non-browser clients (Akamai). Not worked around; left out.
- **Oklahoma constitution:** the legislature's RTF was last changed 2022-01-04 while the statutes are from 2025; it
  may lack recent amendments. Oklahoma's OSCN site disallows crawlers in robots.txt, so it is not used.
- **Iowa:** we use the per-chapter files (unofficial); only the eight authenticated volume PDFs are official.
- **Kentucky:** text is PDF only (one PDF per section); constitution catchlines that wrap lose their heading
  (the text is kept).
- **PDF states** (IA constitution, KY, ND, OK, WY, SC/IN constitutions): layout is reconstructed from the PDF; a few
  paragraph joins and headings are imperfect and flagged by verify.
- **Alaska:** about 8 section headers in title 11 are missing from the site's own print view; their text stays in
  the section before (flagged).
- **California, Wisconsin, Montana and others:** source quirks (duplicate headings, repealed gaps, compacts that
  restart numbering) are kept as found and flagged, not "fixed".

## States we can't cover: Arkansas, Georgia, Mississippi, Tennessee

These four states publish their code online only through LexisNexis, under contracts that make the vendor the
gatekeeper to the state's own law. The sites are free to read, but each requires accepting LexisNexis's terms, and
those terms forbid exactly what this project does: access "through mechanical, programmatic, robotic, scripted or any
other automated means without LN's express prior written permission", and storing the materials "in a database"
(LexisNexis General Terms, https://www.lexisnexis.com/en-us/terms/general/default.page, sections 1.2(a) and
1.2(b); each free-access site links its own terms page, which should be checked against these before relying on them). Accepting the terms would not help -- it would bind the user
to them. Arkansas's site also asserts the State's copyright in the Arkansas Code of 1987.

The law itself is not anyone's property. Statutes have never been copyrightable in the United States (*Wheaton v.
Peters*, 1834; *Banks v. Manchester*, 1888), and in *Georgia v. Public.Resource.Org* (2020) the Supreme Court held
that even the annotations a legislature's code commission prepares are government edicts, free for anyone to copy.
Yet in these four states the only current, official electronic text sits behind a private company's click-through
contract that bans bulk use. That is an arrangement the states chose and could end; until they do, citizens and
projects like this one can read their own law one page at a time and cannot build on it. Public.Resource.Org has
spent more than a decade fighting this, including a public-records suit in Tennessee that ended in 2024 without
access (records at https://law.resource.org/pub/us/code/tn/).

What exists instead, and why we don't use it:

| State | Best open copy | Why not used |
|---|---|---|
| Georgia | Public.Resource.Org, OCGA release 73 (2019, Word/ODT) | seven years out of date |
| Mississippi | Public.Resource.Org, XML 2013, scans 2014 | over a decade out of date |
| Arkansas | Public.Resource.Org, XML 2012, scans 2014 | over a decade out of date |
| Tennessee | none | Public.Resource.Org's records request was denied |
| all four | Justia (law.justia.com) | its terms allow access only through its own interface; it points bulk users to Public.Resource.Org |

Stale text that looks like current law is worse than no text, so these states are marked "vendor-only" in the
tracker. The way forward is a request to each state for its bulk files (Arkansas: Bureau of Legislative Research /
Arkansas Code Revision Commission; Georgia: Office of Legislative Counsel / Code Revision Commission; Mississippi:
Joint Legislative Committee on Compilation, Revision and Publication; Tennessee: Tennessee Code Commission), or a
change in their contracts.

## Refreshing the table

    PYTHONPATH=src TRIBONIAN_WORK=$PWD python3 docs/datasources-table.py
