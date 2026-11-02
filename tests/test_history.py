#!/usr/bin/env python3
"""Case history on Loper Bright's real docket text (D.D.C. -> D.C. Cir. -> U.S. -> back). No network."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tribonian import history as h                             # noqa: E402

DCD = {"court": "dcd", "number": "1:20-cv-00466", "found": True, "case_name": "Loper Bright v. Ross", "date_filed": "2020-02-19",
       "date_terminated": "2021-06-24", "lower_numbers": [], "appeal_from": None, "origin": "", "entries": [
           {"date": "2021-06-23", "number": 38, "text": "FINAL JUDGMENT. Signed by Judge Emmet G. Sullivan on 6/23/2021."},
           {"date": "2021-07-15", "number": 40, "text": "NOTICE OF APPEAL TO DC CIRCUIT COURT as to 39 Judgment"},
           {"date": "2021-07-15", "number": 41, "text": "Transmission of the Notice of Appeal to US Court of Appeals."},
           {"date": "2021-07-19", "text": "USCA Case Number 21-5166 for 40 Notice of Appeal to DC Circuit Court"},
           {"date": "2022-10-05", "number": 42, "text": "RECALLED PURSUANT TO USCA ORDER FILED 7/30/2024.....MANDATE of USCA as to 40 Notice of Appeal; USCA Case Number 21-5166."},
       ]}
CADC = {"court": "cadc", "number": "21-5166", "found": True, "case_name": "", "date_filed": "2021-07-19", "date_terminated": "2022-08-12",
        "noa_date": "2021-07-15", "lower_numbers": ["1:20-cv-00466"], "appeal_from": "dcd", "origin": "D.D.C.", "entries": [
            {"date": "2022-08-12", "text": "[VACATED PURSUANT TO SUPREME COURT JUDGMENT FILED ON 07/30/2024]--PER CURIAM JUDGMENT [1959084] filed"},
            {"date": "2022-08-12", "text": "CLERK'S ORDER [1959088] filed withholding issuance of the mandate."},
            {"date": "2022-10-05", "text": "MANDATE ISSUED to Clerk, U.S. District Court. [21-5166]"},
            {"date": "2022-11-15", "text": "LETTER [1975291] received from the Clerk of the Supreme Court of the United States notifying this court: A petition for writ of certiorari was filed and placed on the docket on 11/15/2022 as No. 22-451."},
            {"date": "2024-06-28", "text": "OPINION [2062278] received from the Clerk of the Supreme Court of the United States notifying this court of the following disposition of case No. 22-451 argued on 01/17/2024: the lower court decision was vacated and the case remanded on 06/28/2024."},
            {"date": "2024-07-30", "text": "CLERK'S ORDER [2067373] filed recalling mandate to district court [1967718-2]."},
            {"date": "2024-08-07", "text": "UNOPPOSED MOTION [2068861] to postpone oral argument filed"},
        ]}
US = {"court": "scotus", "number": "22-451", "found": True, "case_name": "", "date_filed": "2022-11-15", "date_terminated": "",
      "lower_numbers": ["21-5166"], "appeal_from": "cadc", "origin": "", "entries": []}
ALL = {("dcd", "1:20-cv-00466"): DCD, ("cadc", "21-5166"): CADC, ("scotus", "22-451"): US}


def chain_from(court, number):
    return h.walk((court, number), lambda c, n: ALL[(c, n)])


def trips(chain):
    return {(l["from"][0], l["to"][0], l["kind"]) for l in chain["links"]}


class Classify(unittest.TestCase):
    def test_kinds(self):
        for text, kind in [("MANDATE ISSUED to Clerk", "mandate"), ("NOTICE OF APPEAL TO DC CIRCUIT", "appeal"),
                           ("CLERK'S ORDER filed withholding issuance of the mandate.", None), ("MOTION to remand", None),
                           ("NOTICE of intention to participate as amicus", None),
                           ("Judgment VACATED and case REMANDED", "remand"),
                           ("NOTICE of Appeal Due letter sent to counsel", None),
                           ("EO: The Court asks whether counsel will represent Plaintiff on remand", None),
                           ("ORDER: The court is prepared to rule on the parties' cross-motions, but the appeal may be dismissed", None),
                           ("Petition DENIED.", "cert_denied"), ("JUDGMENT ISSUED.", "mandate"), ("Argued. For petitioners: X", "argued")]:
            self.assertEqual(h.classify(text)[0], kind, text)

    def test_vacated_prefix(self):
        self.assertEqual(h.classify("[VACATED PURSUANT TO X]--PER CURIAM JUDGMENT filed"), ("judgment", True))

    def test_usca_spellings(self):
        for t in ("USCA Case Number 23-15605 for 24", "MANDATE of USCA; USCA No. 23-15605", "USCA No.23-15605"):
            self.assertEqual(h.USCA_RE.findall(t), ["23-15605"], t)

    def test_judge_suffix(self):
        self.assertEqual(h.clean_number("1:20-cv-00466-EGS"), "1:20-cv-00466")


class Walk(unittest.TestCase):
    def test_same_chain_from_any_seed(self):
        for seed in ALL:
            self.assertEqual({d["number"] for d in chain_from(*seed)["dockets"]}, {"1:20-cv-00466", "21-5166", "22-451"})

    def test_all_jumps(self):
        self.assertEqual(trips(chain_from("cadc", "21-5166")), {("dcd", "cadc", "appeal"), ("cadc", "dcd", "mandate"),
                                                                ("cadc", "scotus", "cert"), ("scotus", "cadc", "remand")})

    def test_state_court_not_followed(self):
        raw = dict(US, appeal_from=None, lower_numbers=["12-3"], origin="Supreme Court of Ohio")
        chain = h.walk(("scotus", "22-451"), lambda c, n: raw)
        self.assertEqual(len(chain["dockets"]), 1)
        self.assertIn("Supreme Court of Ohio", chain["notes"][0])

    def test_docket_not_found_is_said(self):
        raw = lambda c, n: ALL.get((c, n)) or dict(CADC, court=c, number=n, found=False, entries=[], lower_numbers=[], appeal_from=None)
        g = dict(DCD, entries=DCD["entries"] + [{"date": "2025-01-01", "text": "USCA Case Number 25-1 for appeal"}])
        chain = h.walk(("dcd", "1:20-cv-00466"), lambda c, n: g if c == "dcd" else raw(c, n))
        self.assertTrue(any("25-1" in n and "not on CourtListener" in n for n in chain["notes"]))

    def test_missing_entries(self):
        self.assertEqual(h.missing_numbers(DCD["entries"]), 38)


class Render(unittest.TestCase):
    def test_graph(self):
        g = h.render(chain_from("cadc", "21-5166"), cites={"dcd": "D.D.C."})
        print("\n" + g)
        for s in ("D.D.C. 1:20-cv-00466", "D.C. Cir. 21-5166", "U.S. 22-451", "remand -> D.C. Cir. 21-5166",
                  "judgment (later vacated or recalled)", "mandate (later vacated or recalled)"):
            self.assertIn(s, g)


if __name__ == "__main__":
    unittest.main()
