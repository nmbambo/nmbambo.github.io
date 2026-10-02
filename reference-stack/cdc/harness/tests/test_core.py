import unittest

from core import (Mutation, Obs, build_workload, check_deletes, check_delivery, check_ordering, latencies_ms,
                  latency_summary, parse_airbyte_record, parse_bytes, parse_debezium, parse_docker_stats_line,
                  parse_olake_row, percentile, summarize_resources, throughput)
import json


class WorkloadTests(unittest.TestCase):
    def test_deterministic_for_a_seed_and_different_across_seeds(self):
        a = build_workload(list(range(1, 101)), 101, 1, 500, seed=7)
        b = build_workload(list(range(1, 101)), 101, 1, 500, seed=7)
        c = build_workload(list(range(1, 101)), 101, 1, 500, seed=8)
        self.assertEqual(a, b)
        self.assertNotEqual(a[0], c[0])

    def test_invariants(self):
        muts, alive_after, next_key = build_workload(list(range(1, 51)), 51, 1000, 2000, seed=1)
        self.assertEqual([m.seq for m in muts], list(range(1000, 3000)))
        alive, deleted = set(range(1, 51)), set()
        for m in muts:
            if m.op == "insert":
                self.assertNotIn(m.key, alive | deleted)
                alive.add(m.key)
            elif m.op == "update":
                self.assertIn(m.key, alive)
            else:
                self.assertIn(m.key, alive)
                alive.remove(m.key)
                deleted.add(m.key)
        self.assertEqual(alive, set(alive_after))
        self.assertEqual(next_key, 51 + sum(1 for m in muts if m.op == "insert"))
        ops = [m.op for m in muts]
        self.assertTrue(all(ops.count(o) > 0 for o in ("insert", "update", "delete")))
        keys = [m.key for m in muts if m.op == "update"]
        self.assertLess(len(set(keys)), len(keys))  # keys repeat, so per-key ordering is exercised

    def test_empty_alive_forces_insert(self):
        muts, _, _ = build_workload([], 1, 1, 1, seed=0, mix=(0, 0, 1))
        self.assertEqual(muts[0].op, "insert")

    def test_mutation_ids(self):
        self.assertEqual(Mutation("update", 5, 9).mid, (5, 9))
        self.assertEqual(Mutation("delete", 5, 10).mid, ("d", 5))


def dbz(op, key, seq=None, extra=None):
    after = None if op == "d" else {"decision_id": key, "workload_seq": seq, **(extra or {})}
    return json.dumps({"op": op, "before": {"decision_id": key} if op in ("d", "u") else None, "after": after,
                       "source": {"lsn": 1}}).encode()


class ParserTests(unittest.TestCase):
    def test_debezium_update_insert_delete_tombstone_snapshot(self):
        k = json.dumps({"decision_id": 7}).encode()
        u = parse_debezium(k, dbz("u", 7, 12, {"risk_band": "x"}), 1.0, (0, 5))
        self.assertEqual((u.key, u.op, u.seq, u.mid), (7, "u", 12, (7, 12)))
        self.assertIn("risk_band", u.cols)
        self.assertEqual(parse_debezium(k, dbz("c", 7, 13), 1.0).op, "c")
        d = parse_debezium(k, dbz("d", 7), 1.0)
        self.assertEqual((d.op, d.seq, d.mid), ("d", None, ("d", 7)))
        t = parse_debezium(k, None, 1.0)
        self.assertEqual((t.op, t.mid), ("tombstone", None))
        r = parse_debezium(k, dbz("r", 7, None), 1.0)
        self.assertEqual((r.op, r.mid), ("r", None))

    def test_debezium_schema_wrapped_payload_and_garbage(self):
        k = json.dumps({"payload": {"decision_id": 3}}).encode()
        v = json.dumps({"payload": json.loads(dbz("u", 3, 4))}).encode()
        self.assertEqual(parse_debezium(k, v, 0).mid, (3, 4))
        self.assertIsNone(parse_debezium(b"nope", b"{}", 0))
        self.assertIsNone(parse_debezium(k, b"[]", 0))
        self.assertIsNone(parse_debezium(k, json.dumps({"op": "x"}).encode(), 0))

    def test_olake_ops_spellings(self):
        for raw, op in (("u", "u"), ("UPDATE", "u"), ("d", "d"), ("delete", "d"), ("r", "r"), ("insert", "c")):
            o = parse_olake_row({"_op_type": raw, "decision_id": 1, "workload_seq": 3}, 0)
            self.assertEqual(o.op, op)
        self.assertIsNone(parse_olake_row({"_op_type": "?", "decision_id": 1}, 0))
        self.assertIsNone(parse_olake_row({"_op_type": "u"}, 0))

    def test_airbyte_delete_marker_and_wrapper(self):
        self.assertEqual(parse_airbyte_record(json.dumps({"_airbyte_data": {"decision_id": 2, "workload_seq": 8}}).encode(), 0).mid, (2, 8))
        self.assertEqual(parse_airbyte_record(json.dumps({"decision_id": 2, "_ab_cdc_deleted_at": "2026-01-01"}).encode(), 0).mid, ("d", 2))
        self.assertIsNone(parse_airbyte_record(b"x", 0))


class MetricTests(unittest.TestCase):
    def test_percentile(self):
        self.assertIsNone(percentile([], 50))
        self.assertEqual(percentile([5], 95), 5)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(percentile(list(range(1, 101)), 95), 95.05)

    def test_latencies_use_first_arrival_and_ignore_unmatched(self):
        commit = {(1, 10): 100.0, ("d", 2): 100.5}
        obs = [Obs(1, "u", 10, 100.2), Obs(1, "u", 10, 100.9),   # duplicate delivery: first arrival counts
               Obs(2, "d", None, 101.0), Obs(9, "u", 99, 100.1), Obs(3, "r", None, 100.0)]
        lat = latencies_ms(commit, obs)
        self.assertEqual(sorted(round(x) for x in lat), [200, 500])
        s = latency_summary(lat)
        self.assertEqual(s["n"], 2)
        self.assertEqual(s["max"], 500.0)

    def test_negative_latency_is_clamped(self):
        self.assertEqual(latencies_ms({(1, 1): 10.0}, [Obs(1, "u", 1, 9.0)]), [0.0])

    def test_throughput(self):
        commit = {(1, 1): 0.0, (1, 2): 1.0, (1, 3): 2.0}
        obs = [Obs(1, "u", 1, 1.0), Obs(1, "u", 2, 3.0), Obs(1, "u", 3, 4.0)]
        t = throughput(commit, obs)
        self.assertEqual(t["committed_per_s"], 1.5)
        self.assertEqual(t["delivered_per_s"], 0.8)
        self.assertEqual(throughput({}, [])["delivered"], 0)


class CheckTests(unittest.TestCase):
    def test_ordering_detects_regression_and_update_after_delete(self):
        ok = [Obs(1, "u", 1, 0, (0, 1)), Obs(1, "u", 2, 0, (0, 2)), Obs(1, "d", None, 0, (0, 3))]
        self.assertEqual(check_ordering(ok), {"keys_checked": 1, "violations": 0})
        back = [Obs(1, "u", 2, 0, (0, 1)), Obs(1, "u", 1, 0, (0, 2))]
        self.assertEqual(check_ordering(back)["violations"], 1)
        after_del = [Obs(1, "d", None, 0, (0, 1)), Obs(1, "u", 5, 0, (0, 2))]
        self.assertEqual(check_ordering(after_del)["violations"], 1)

    def test_ordering_sorts_by_sink_order_not_list_order(self):
        shuffled = [Obs(1, "u", 2, 0, (0, 2)), Obs(1, "u", 1, 0, (0, 1))]
        self.assertEqual(check_ordering(shuffled)["violations"], 0)

    def test_duplicates_gaps_and_final_state(self):
        exp = [Mutation("insert", 1, 1), Mutation("update", 1, 2), Mutation("update", 1, 3), Mutation("delete", 2, 4)]
        obs = [Obs(1, "c", 1, 0), Obs(1, "c", 1, 0),            # duplicate of the insert
               Obs(1, "u", 3, 0)]                                 # update seq 2 coalesced; delete of key 2 lost
        r = check_delivery(exp, obs)
        self.assertEqual(r, {"expected": 4, "delivered_unique": 2, "duplicates": 1, "missing": 2, "missing_final": 1})

    def test_snapshot_rows_are_not_counted_as_workload(self):
        r = check_delivery([Mutation("update", 1, 5)], [Obs(1, "r", None, 0)])
        self.assertEqual((r["delivered_unique"], r["missing"]), (0, 1))

    def test_deletes_and_tombstones(self):
        exp = [Mutation("delete", 1, 1), Mutation("delete", 2, 2), Mutation("update", 3, 3)]
        obs = [Obs(1, "d", None, 0), Obs(1, "tombstone", None, 0), Obs(2, "d", None, 0)]
        self.assertEqual(check_deletes(exp, obs), {"expected": 2, "observed_as_delete": 2, "tombstones_observed": 1})


class ResourceTests(unittest.TestCase):
    def test_parse_bytes(self):
        self.assertEqual(parse_bytes("1.5GiB"), 1.5 * 1024 ** 3)
        self.assertEqual(parse_bytes("150MiB"), 150 * 1024 ** 2)
        self.assertEqual(parse_bytes("512kB"), 512e3)
        self.assertIsNone(parse_bytes("weird"))

    def test_parse_line_and_summary(self):
        line = json.dumps({"Name": "ingqiqo-reference-kafka-1", "CPUPerc": "12.50%", "MemUsage": "256MiB / 7.6GiB"})
        s = parse_docker_stats_line(line)
        self.assertEqual(s, {"name": "ingqiqo-reference-kafka-1", "cpu_pct": 12.5, "mem_mib": 256.0})
        self.assertIsNone(parse_docker_stats_line("not json"))
        self.assertIsNone(parse_docker_stats_line(json.dumps({"Name": "x", "CPUPerc": "--", "MemUsage": "0B / 0B"})))
        out = summarize_resources([s, dict(s, cpu_pct=37.5, mem_mib=300.0)])["ingqiqo-reference-kafka-1"]
        self.assertEqual((out["cpu_avg_pct"], out["cpu_peak_pct"], out["mem_peak_mib"], out["samples"]), (25.0, 37.5, 300.0, 2))


if __name__ == "__main__":
    unittest.main()
