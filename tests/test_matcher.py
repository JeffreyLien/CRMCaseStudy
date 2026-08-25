import unittest

from matcher import compare, normalize_address, rank_candidates
from pipeline import classify


FACILITY = {
    "name": "Bellhaven Riverside",
    "address": "100 West Main Street",
    "city": "Dayton",
    "state": "OH",
    "zip": "45402",
}


def account(**overrides):
    result = {
        "account_id": "A1",
        "name": "Legacy Riverside Center",
        "billing_street": "100 W Main St",
        "billing_city": "Dayton",
        "billing_state": "OH",
        "billing_zip": "45402",
        "parent_name": "Former Owner",
        "lifetime_revenue": 0,
        "outstanding_ar": 0,
    }
    result.update(overrides)
    return result


class MatcherTests(unittest.TestCase):
    def test_exact_address_different_name_is_strong_match(self):
        signals = compare(FACILITY, account())
        self.assertTrue(signals["address_exact"])
        self.assertGreaterEqual(signals["score"], 90)

    def test_exact_name_different_state_is_capped(self):
        candidate = account(
            name=FACILITY["name"], billing_street="900 Other Road",
            billing_city="Dayton", billing_state="MI", billing_zip="48101",
        )
        signals = compare(FACILITY, candidate)
        self.assertTrue(signals["geo_conflict"])
        self.assertLessEqual(signals["score"], 49)
        row, matched = classify(FACILITY, rank_candidates(FACILITY, [candidate]))
        self.assertEqual(row["proposal_type"], "CREATE_NEW")
        self.assertEqual(matched, set())

    def test_directional_and_street_suffix_normalization(self):
        self.assertEqual(normalize_address("100 West Main Street"), normalize_address("100 W Main St"))

    def test_pk_and_pike_normalization(self):
        self.assertEqual(normalize_address("25 County Pk"), normalize_address("25 County Pike"))


class ChowThresholdTests(unittest.TestCase):
    def test_revenue_and_ar_trigger_chow(self):
        candidate = account(lifetime_revenue=1, outstanding_ar=1)
        row, _ = classify(FACILITY, rank_candidates(FACILITY, [candidate]))
        self.assertEqual(row["proposal_type"], "CHOW_CREATE_NEW")

    def test_zero_ar_allows_reparent(self):
        candidate = account(name=FACILITY["name"], lifetime_revenue=100, outstanding_ar=0)
        row, _ = classify(FACILITY, rank_candidates(FACILITY, [candidate]))
        self.assertEqual(row["proposal_type"], "REPARENT")


if __name__ == "__main__":
    unittest.main()
