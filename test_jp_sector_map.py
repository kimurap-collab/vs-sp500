"""jp_sector_map.classify_infoの単体テスト（2026-10-07追加・JP枠スワップ売却機能）。

実行: python3 -m unittest test_jp_sector_map.py -v
"""
from __future__ import annotations

import unittest

import jp_sector_map


class TestClassifyInfo(unittest.TestCase):
    def test_known_industry_maps_to_expected_topix17_code(self):
        self.assertEqual(jp_sector_map.classify_info({"industry": "Semiconductors"}), "1625")
        self.assertEqual(jp_sector_map.classify_info({"industry": "Banks - Diversified"}), "1631")
        self.assertEqual(jp_sector_map.classify_info({"industry": "Biotechnology"}), "1621")

    def test_unmapped_industry_falls_back_to_sector(self):
        # industryが辞書に無くてもsectorで拾う
        info = {"industry": "Some Unmapped Niche", "sector": "Technology"}
        self.assertEqual(jp_sector_map.classify_info(info), "1625")

    def test_unknown_industry_and_sector_returns_none(self):
        info = {"industry": "Totally Unknown", "sector": "Totally Unknown Sector"}
        self.assertIsNone(jp_sector_map.classify_info(info))

    def test_none_info_returns_none(self):
        self.assertIsNone(jp_sector_map.classify_info(None))

    def test_ticker_override_wins_regardless_of_info(self):
        # 6367(ダイキン)はHELD17上書きで1624固定。infoがでたらめでも上書きが勝つ
        info = {"industry": "Totally Unknown", "sector": "Totally Unknown Sector"}
        self.assertEqual(jp_sector_map.classify_info(info, ticker="6367"), "1624")
        self.assertEqual(jp_sector_map.classify_info(None, ticker="9983"), "1630")

    def test_ticker_override_only_applies_to_known_tickers(self):
        self.assertIsNone(jp_sector_map.classify_info(None, ticker="9999"))


if __name__ == "__main__":
    unittest.main()
