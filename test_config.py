"""config.py の validate_whitelist() 単体テスト。

WHITELISTのtype省略・誤記でis_stock()が黙ってFalseを返し、個別株ガードレール
（5%上限・合計20%・損切り-20%）が外れるフェイルオープンを、起動時エラーで
止められているかを検証する。
実行: python3 -m unittest test_config.py -v
"""
from __future__ import annotations

import unittest

import config


class TestValidateWhitelist(unittest.TestCase):
    def test_current_whitelist_passes_validation(self):
        """現行のconfig.WHITELISTはvalidate_whitelistを例外なく通ること。"""
        config.validate_whitelist(config.WHITELIST)

    def test_missing_type_raises_value_error(self):
        """typeキーが欠落したエントリはValueErrorになること。"""
        bad_whitelist = {"NVDA": {"currency": "USD", "name": "x"}}
        with self.assertRaises(ValueError):
            config.validate_whitelist(bad_whitelist)

    def test_misspelled_type_raises_value_error(self):
        """typeが誤記（例 'stocks'）のエントリはValueErrorになること。"""
        bad_whitelist = {"NVDA": {"currency": "USD", "name": "x", "type": "stocks"}}
        with self.assertRaises(ValueError):
            config.validate_whitelist(bad_whitelist)


if __name__ == "__main__":
    unittest.main()
