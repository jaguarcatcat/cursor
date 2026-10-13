#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serp_docx
from test_serp_xlsx import CURR, PREV


class DocxExportTests(unittest.TestCase):
    def test_writes_current_and_comparison_docx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            current_path = Path(tmp) / "vydacha.docx"
            compare_path = Path(tmp) / "sravnenie.docx"
            serp_docx.write_current_docx(CURR, current_path, checked_at="2026-10-13 00:00 MSK")
            serp_docx.write_comparison_docx(PREV, CURR, compare_path)
            self.assertTrue(current_path.exists())
            self.assertGreater(current_path.stat().st_size, 1000)
            self.assertTrue(compare_path.exists())
            self.assertGreater(compare_path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
