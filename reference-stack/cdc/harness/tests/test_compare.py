import json
import tempfile
import unittest
from pathlib import Path

import compare


def fake_debezium():
    return {"tool": "debezium", "tool_version": "quay.io/debezium/connect:3.7.0.Final", "measured_at": "T", "params": {},
            "arrival_basis": "x", "host": {"cpus": 4}, "scenarios": {
                "snapshot": {"status": "ok", "seconds": 1.5},
                "workload": {"status": "ok", "latency_ms": {"p50": 10.0, "p95": 30.0},
                             "ordering": {"violations": 0}, "deletes": {"observed_as_delete": 5, "expected": 5, "tombstones_observed": 5}},
                "throughput": {"status": "ok", "throughput": {"delivered_per_s": 900.0}},
                "restart_recovery": {"status": "ok", "seconds_from_restart_to_first_change": 4.0,
                                     "delivery": {"duplicates": 3, "missing": 0, "missing_final": 0}},
                "schema_change": {"status": "failed", "error": "boom"},
                "resources": {"status": "ok", "per_container": {"ingqiqo-reference-debezium-connect-1": {
                    "cpu_avg_pct": 5.0, "cpu_peak_pct": 20.0, "mem_avg_mib": 400.0, "mem_peak_mib": 500.0}}}}}


class CompareTests(unittest.TestCase):
    def test_empty_results_say_not_yet_measured_everywhere(self):
        md = compare.render({})
        self.assertIn("not yet measured", md)
        body = [l for l in md.splitlines() if l.startswith("| ") and "Metric" not in l]
        self.assertTrue(body)
        for line in body:
            cells = [c.strip() for c in line.strip("|").split("|")][1:]
            self.assertEqual(cells, [compare.NA] * 3, line)

    def test_values_come_only_from_results_and_statuses_are_shown(self):
        md = compare.render({"debezium": fake_debezium()})
        self.assertIn("| Latency p95 (ms) | 30.0 | not yet measured | not yet measured |", md)
        self.assertIn("| Initial snapshot (s) | 1.5 |", md)
        self.assertIn("| Schema change: rows carrying new column | failed |", md)
        self.assertIn("| Footprint: tool CPU avg / peak (%) | 5.0 / 20.0 |", md)
        self.assertIn("3.7.0.Final", md)

    def test_skipped_tool_scenarios_render_as_skipped(self):
        r = {"tool": "airbyte", "scenarios": {"snapshot": {"status": "skipped", "reason": "not configured"}}, "params": {}}
        md = compare.render({"airbyte": r})
        self.assertIn("| Initial snapshot (s) | not yet measured | not yet measured | skipped |", md)

    def test_load_reads_files(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "debezium.json").write_text(json.dumps(fake_debezium()))
            self.assertEqual(list(compare.load(d)), ["debezium"])
            self.assertEqual(compare.load("/nonexistent"), {})


if __name__ == "__main__":
    unittest.main()
