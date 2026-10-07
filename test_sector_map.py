"""sector_map.classify_industry_labelsの単体テスト（2026-10-07追加・スワップ売却機能）。

実行: python3 -m unittest test_sector_map.py -v
"""
from __future__ import annotations

import unittest

import sector_map


class TestClassifyIndustryLabels(unittest.TestCase):
    def test_known_industries_map_to_expected_etf(self):
        self.assertEqual(sector_map.classify_industry_labels(["Banks - Diversified"]), "XLF")
        self.assertEqual(sector_map.classify_industry_labels(["Oil & Gas Integrated"]), "XLE")
        self.assertEqual(sector_map.classify_industry_labels(["Semiconductors"]), "XLK")
        self.assertEqual(sector_map.classify_industry_labels(["Biotechnology"]), "XLV")
        self.assertEqual(sector_map.classify_industry_labels(["Utilities - Regulated Electric"]), "XLU")

    def test_reit_not_misclassified_as_financial(self):
        # "REIT - Mortgage"は"mortgage"(Financial)より先に"reit"(Real Estate)が一致するべき
        self.assertEqual(sector_map.classify_industry_labels(["REIT - Mortgage"]), "XLRE")

    def test_unknown_industry_returns_none(self):
        self.assertIsNone(sector_map.classify_industry_labels(["Some Unclassified Niche Label"]))

    def test_empty_list_returns_none(self):
        self.assertIsNone(sector_map.classify_industry_labels([]))

    def test_first_matching_label_wins_when_multiple_labels_given(self):
        result = sector_map.classify_industry_labels(["Unclassified Thing", "Software - Application"])
        self.assertEqual(result, "XLK")


if __name__ == "__main__":
    unittest.main()
