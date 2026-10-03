import contextlib
import io
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from scripts import probe_announcement_sources as probe


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.legacy = self.root / "probe_log.jsonl"
        self.legacy.write_text("existing history\n", encoding="utf-8")
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(probe, "PROBE_LOG", self.legacy, create=True))
        clock = self.patches.enter_context(patch.object(probe, "datetime", wraps=datetime))
        clock.now.return_value = datetime(2026, 10, 3, 0, 40, tzinfo=probe.TPE)
        self.urls = []
        self.fail_source = None
        self.patches.enter_context(patch.object(probe.urllib.request, "urlopen", side_effect=self.response))

    def response(self, request, timeout):
        url = request.full_url
        self.urls.append(url)
        source = "openapi" if url == probe.OPENAPI else "rwd"
        if source == self.fail_source:
            raise OSError("source unavailable")
        if source == "openapi":
            payload = [{"Code": "2330", "Name": "台積電", "Date": "115/10/02",
                        "DispositionPeriod": "115/10/03～115/10/16"}]
        else:
            payload = {"title": "test", "data": [
                [1, "115/10/02", "2330", "台積電", "", "", "115/10/03～115/10/16"]]}
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def run_probe(self, *args):
        stdout = io.StringIO()
        with patch.object(probe.sys, "argv", ["probe", *args]), contextlib.redirect_stdout(stdout):
            probe.main()
        return stdout.getvalue()

    def result(self, stdout):
        lines = [line for line in stdout.splitlines() if line.startswith("{")]
        self.assertEqual(len(lines), 1, "print exactly one JSON result")
        return json.loads(lines[0])

    def test_default_prints_without_file_writes(self):
        with patch.object(Path, "open", side_effect=AssertionError("unexpected file open")) as opened, \
                patch.object(Path, "mkdir") as mkdir:
            stdout = self.run_probe()
        opened.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(self.legacy.read_text(), "existing history\n")
        entry = self.result(stdout)
        self.assertEqual(entry["reference_date"], "2026-10-03")
        self.assertEqual(entry["reference_date_source"], "taiwan_local_default")
        self.assertIn("taiwan_local_default", stdout)
        self.assertEqual(entry["openapi"]["forward"], 0)
        self.assertEqual(parse_qs(urlparse(self.urls[1]).query)["startDate"], ["20261003"])

    def test_output_writes_one_json_result_not_history(self):
        output = self.root / ".run" / "probe.json"
        stdout = self.run_probe("--output", str(output))
        self.assertTrue(output.is_file(), "--output must create its parent and result")
        self.assertEqual(json.loads(output.read_text()), self.result(stdout))
        self.assertEqual(self.legacy.read_text(), "existing history\n")
        self.run_probe("--output", str(output))
        self.assertIsInstance(json.loads(output.read_text()), dict)

    def test_explicit_reference_date_after_midnight_is_consistent(self):
        entry = self.result(self.run_probe("--reference-date", "2026-10-02"))
        self.assertEqual(entry["reference_date"], "2026-10-02")
        self.assertEqual(entry["reference_date_source"], "explicit")
        self.assertEqual(entry["tpe_date"], "2026-10-03")
        self.assertEqual(entry["at_utc"], "2026-10-02T16:40:00Z")
        for source in ("openapi", "rwd"):
            self.assertEqual(entry[source]["forward"], 1)
        query = parse_qs(urlparse(self.urls[1]).query)
        self.assertEqual(query["startDate"], ["20261002"])
        self.assertEqual(query["endDate"], ["20261012"])

    def test_source_failure_does_not_hide_other_source(self):
        for failed, healthy in (("openapi", "rwd"), ("rwd", "openapi")):
            with self.subTest(failed=failed):
                self.fail_source = failed
                output = self.root / "failure.json"
                stdout = self.run_probe("--output", str(output))
                entry = self.result(stdout)
                self.assertEqual(entry[failed]["error"], "OSError: source unavailable")
                self.assertEqual(entry[healthy]["rows"], 1)
                self.assertEqual(json.loads(output.read_text()), entry)

    def test_invalid_reference_date_rejected_before_network(self):
        for value in ("2026-02-30", "20261002", "2026-1-02"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.run_probe("--reference-date", value)
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.urls, [])

    def test_identity_and_content_hash_ignore_row_order(self):
        rows = [
            {"code": "2330", "name": "台積電", "ann": "2026-10-02", "ps": "2026-10-03", "pe": "2026-10-16"},
            {"code": "1101", "name": "台泥", "ann": "2026-10-01", "ps": "2026-10-02", "pe": "2026-10-15"},
        ]
        summary = probe._summarize(rows, "2026-10-02")
        self.assertIn("event_identities", summary)
        self.assertEqual(summary["event_identities"], [
            ["1101", "2026-10-01", "2026-10-02", "2026-10-15"],
            ["2330", "2026-10-02", "2026-10-03", "2026-10-16"],
        ])
        self.assertRegex(summary["content_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(summary, probe._summarize(rows[::-1], "2026-10-02"))
        self.assertEqual(summary["content_hash"], probe._summarize(rows, "2026-10-03")["content_hash"])
        changed = [dict(rows[0], ann="2026-10-01"), rows[1]]
        self.assertNotEqual(summary["content_hash"], probe._summarize(changed, "2026-10-02")["content_hash"])
        renamed = [dict(rows[0], name="new name"), rows[1]]
        renamed_summary = probe._summarize(renamed, "2026-10-02")
        self.assertEqual(summary["event_identities"], renamed_summary["event_identities"])
        self.assertNotEqual(summary["content_hash"], renamed_summary["content_hash"])

    def test_empty_success_has_stable_summary(self):
        summary = probe._summarize([], "2026-10-02")
        self.assertEqual(summary.get("event_identities"), [])
        self.assertEqual(summary["content_hash"], "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945")
        self.assertEqual(summary["rows"], 0)
        self.assertEqual(summary["forward"], 0)
        self.assertIsNone(summary["max_ann"])


if __name__ == "__main__":
    unittest.main()
