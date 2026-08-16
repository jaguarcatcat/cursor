#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import google_news_parser as gnp
import server as web

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class OtchetCollectTests(unittest.TestCase):
    def test_collect_writes_html_document_into_otchet_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = gnp.collect(
                queries=["лобов вадим университет синергия"],
                limit=20,
                output_root=tmp,
                from_html=str(FIXTURES / "sample_search.html"),
                query="лобов вадим университет синергия",
                pause=0,
            )
            html_path = Path(result["files"]["html"])
            self.assertTrue(html_path.exists())
            self.assertEqual(html_path.name, "otchet.html")
            self.assertEqual(Path(result["folder"]).parent, Path(tmp))
            text = html_path.read_text(encoding="utf-8")
            for header in ["Запрос", "Позиция", "Заголовок", "СМИ", "Дата", "Сниппет", "URL", "Домен"]:
                self.assertIn(header, text)
            self.assertIn("Example News", text)
            latest = Path(result["latest_html"])
            self.assertTrue(latest.exists())
            self.assertEqual(latest.name, "posledniy.html")
            self.assertTrue((Path(result["folder"]) / "dannye.csv").exists())
            self.assertTrue((Path(result["folder"]) / "otchet.md").exists())
            runs = gnp.list_otchet_runs(Path(tmp))
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["id"], result["run_id"])


class WebPanelTests(unittest.TestCase):
    def test_index_and_launchers_exist(self) -> None:
        self.assertTrue((ROOT / "index.html").exists())
        self.assertIn("Собрать отчёт", (ROOT / "index.html").read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "zapusk.bat").exists())
        self.assertTrue((ROOT / "zapusk.sh").exists())
        self.assertTrue((ROOT / "server.py").exists())

    def test_http_collect_saves_to_otchet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = web.OTCHET_DIR
            web.OTCHET_DIR = Path(tmp)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address
            try:
                payload = json.dumps(
                    {
                        "queries": ["лобов вадим университет синергия"],
                        "limit": 20,
                        "from_html": "tests/fixtures/sample_search.html",
                        "pause": 0,
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://{host}:{port}/api/collect",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as response:
                    data = json.loads(response.read().decode("utf-8"))
                self.assertTrue(data["ok"])
                self.assertGreater(data["count"], 0)
                self.assertTrue(Path(data["files"]["html"]).exists())
                status_req = urllib.request.Request(f"http://{host}:{port}/api/status")
                with urllib.request.urlopen(status_req, timeout=10) as response:
                    status = json.loads(response.read().decode("utf-8"))
                self.assertEqual(len(status["reports"]), 1)
                report_req = urllib.request.Request(f"http://{host}:{port}{data['report_url']}")
                with urllib.request.urlopen(report_req, timeout=10) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("Срез выдачи Google News", html)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)
                web.OTCHET_DIR = old


if __name__ == "__main__":
    unittest.main()
