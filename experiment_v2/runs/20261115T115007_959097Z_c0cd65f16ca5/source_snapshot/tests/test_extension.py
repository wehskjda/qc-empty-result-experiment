"""Independent regression and boundary checks for the first-attempt coverage gate.

No expected file is consulted to choose an action. Tests use the frozen v1
boundary suite and small independent counterexamples, not a second simulator.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import sys
import unittest


# Frozen source modules must not acquire generated .pyc files under inputs/.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "inputs" / "baseline_snapshot"
sys.path.insert(0, str(ROOT / "src"))

from replay_engine import ReplayEngine, load_inputs  # noqa: E402


# Reuse all 25 frozen boundary checks against the newly imported v2 engine.
# The baseline module's local paths are redirected to its frozen input copy;
# its policies and assertions are not rewritten.
_spec = importlib.util.spec_from_file_location(
    "frozen_baseline_checks", ROOT / "inputs" / "baseline_source" / "tests" / "test_replay.py"
)
_baseline = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_baseline)
_baseline.ROOT = BASELINE


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def response(text="", status=422, code="PARSE_FAILED"):
    return {"text": text, "http_status": status, "code": code,
            "message": "任意提示文字，不用于保护分类", "claimed_page": None}


class BaselineBoundaryTests(_baseline.ReplayProtocolTests):
    """The frozen v1 checks execute the v2 implementation with v1 conditions."""

    @classmethod
    def setUpClass(cls):
        cls.protocol, cls.frozen_fixture, cls.validator, cls.document = load_inputs(ROOT)
        cls.keys = json.loads((BASELINE / "inputs" / "key_numbers.json").read_text("utf-8"))
        cls.references = {
            p: (BASELINE / "inputs" / "reference_pages" / f"{p}.txt").read_text("utf-8")
            for p in cls.protocol["page_order"]
        }


class CoverageExtensionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol, cls.fixture, cls.validator, cls.document = load_inputs(ROOT)
        cls.references = {
            p: (BASELINE / "inputs" / "reference_pages" / f"{p}.txt").read_text("utf-8")
            for p in cls.protocol["page_order"]
        }

    def engine(self, fixture=None):
        return ReplayEngine(copy.deepcopy(self.protocol), fixture or self.fixture,
                            self.validator, copy.deepcopy(self.document))

    def run_case(self, scenario, strategy="C1", budget=20, k=2, order=None, fixture=None):
        engine = self.engine(fixture)
        state = engine.initial_state(scenario, strategy, budget,
                                     k if strategy in ("C", "C1") else None,
                                     initial_order=order)
        sequence = engine.run(state)
        return engine, state, sequence

    @staticmethod
    def actions(sequence):
        return [(e["page_id"], e["method"], e["attempt_n"], e["raw_response"])
                for e in sequence]

    def test_extension_inputs_remain_frozen(self):
        manifest = json.loads((ROOT / "input_freeze_manifest.json").read_text("utf-8"))
        self.assertTrue(manifest["extension_engine_absent_at_freeze"])
        self.assertEqual(29, len(manifest["input_files"]))
        for item in manifest["input_files"]:
            with self.subTest(path=item["path"]):
                raw = (ROOT / item["path"]).read_bytes()
                self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(item["size_bytes"], len(raw))

    def test_initial_order_changes_only_queue_not_source_identity(self):
        order = ["p4", "p3", "p2", "p1"]
        _, state, sequence = self.run_case("P", order=order)
        self.assertEqual(order, [e["page_id"] for e in sequence])
        self.assertEqual(order, state["initial_order"])
        self.assertEqual(["p1", "p2", "p3", "p4"], self.protocol["page_order"])
        for event in sequence:
            self.assertEqual(self.fixture.get("P", event["page_id"], "T", 1)["response"],
                             event["raw_response"])
            self.assertEqual(f'{self.document["document_sha256"]}:{event["page_id"]}',
                             event["source_page_key"])

    def test_invalid_initial_order_is_rejected(self):
        engine = self.engine()
        for order in (["p1", "p1", "p3", "p4"], ["p1", "p2", "p3"],
                      ["p1", "p2", "p3", "p5"], ["p1", "p2", "p3", "p4", "p4"]):
            with self.subTest(order=order):
                with self.assertRaises(ValueError):
                    engine.initial_state("P", "C1", 20, 2, initial_order=order)

    def test_all_failed_first_round_stops_on_fourth_call_without_reset(self):
        for k in (2, 3, 4):
            with self.subTest(k=k):
                engine, state, sequence = self.run_case("L", k=k)
                self.assertEqual((4, 4, 0), (state["calls"], state["S"], len(state["accepted"])))
                self.assertEqual("stagnation", engine.stop_info(state)["reason"])
                self.assertTrue(state["coverage_complete"])
                self.assertEqual([1, 2, 3, 4], [e["S_after"] for e in sequence])
                self.assertFalse(any(e["coverage_complete_after"] for e in sequence[:3]))
                self.assertTrue(sequence[-1]["coverage_completed_this_call"])

    def test_fourth_new_source_network_failure_counts_as_coverage_and_freezes_S(self):
        ordinary = response()
        fixture = _baseline.TestFixture(self.references, {
            ("p1", "T", 1): ordinary, ("p2", "T", 1): ordinary,
            ("p3", "T", 1): ordinary,
            ("p4", "T", 1): response(status=503, code="SERVICE_UNAVAILABLE"),
        })
        for k in (2, 3):
            with self.subTest(k=k):
                engine, state, sequence = self.run_case("test", fixture=fixture, k=k)
                self.assertEqual((4, 3, 1), (state["calls"], state["S"], state["E"]))
                self.assertEqual("stagnation", engine.stop_info(state)["reason"])
                event = sequence[-1]
                self.assertTrue(event["network_protected"])
                self.assertEqual((3, 3), (event["S_before"], event["S_after"]))
                self.assertEqual({"p1", "p2", "p3"}, set(event["attempted_pages_before"]))
                self.assertEqual(set(self.protocol["page_order"]), set(event["attempted_pages_after"]))
                self.assertTrue(event["coverage_completed_this_call"])
                self.assertEqual(1, state["attempts"]["p4"]["T"])

    def test_fourth_new_source_success_resets_S_before_guard(self):
        fixture = _baseline.TestFixture(self.references, {
            (p, "T", 1): response() for p in ("p1", "p2", "p3")
        })
        engine = self.engine(fixture)
        state = engine.initial_state("test", "C1", 20, 2)
        sequence = [engine.step(state) for _ in range(4)]
        self.assertEqual([1, 2, 3, 0], [e["S_after"] for e in sequence])
        self.assertEqual(1, len(state["accepted"]))
        self.assertTrue(state["coverage_complete"])
        self.assertTrue(sequence[-1]["new_progress"])
        self.assertIsNone(engine.stop_info(state)["reason"])

    def test_budget_can_end_before_first_round_coverage(self):
        engine, state, sequence = self.run_case("L", budget=2, k=2)
        self.assertEqual((2, 2), (state["calls"], state["S"]))
        self.assertFalse(state["coverage_complete"])
        self.assertEqual("budget_exhausted", engine.stop_info(state)["reason"])
        self.assertEqual(2, len(sequence))

    def test_network_failure_is_attempted_even_without_acceptance(self):
        engine = self.engine()
        state = engine.initial_state("N", "C1", 20, 2)
        event = engine.step(state)
        self.assertFalse(event["validation"]["accepted"])
        self.assertTrue(event["network_failure"])
        self.assertEqual(["p1"], state["attempted_pages"])
        self.assertEqual(["p1"], event["attempted_pages_after"])
        self.assertEqual((0, 1), (state["S"], state["E"]))

    def test_S_easy_first_still_stops_with_accumulated_S_three(self):
        engine, state, sequence = self.run_case("S", order=["p4", "p1", "p2", "p3"])
        self.assertEqual((4, 3, 1), (state["calls"], state["S"], len(state["accepted"])))
        self.assertEqual([0, 1, 2, 3], [e["S_after"] for e in sequence])
        self.assertTrue(sequence[2]["stagnation_blocked_by_coverage"])
        self.assertEqual("stagnation", engine.stop_info(state)["reason"])
        self.assertEqual(set(self.protocol["page_order"]), set(state["attempted_pages"]))

    def test_same_source_keeps_its_faults_in_all_24_orders(self):
        seen_orders = 0
        for order in itertools.permutations(self.protocol["page_order"]):
            with self.subTest(order=order):
                _, state, sequence = self.run_case("S", strategy="B", order=list(order))
                self.assertEqual((4, 7), (len(state["accepted"]), state["calls"]))
                self.assertEqual(list(order), [e["page_id"] for e in sequence[:4]])
                for event in sequence:
                    frozen = self.fixture.get("S", event["page_id"], event["method"], event["attempt_n"])
                    self.assertEqual(frozen["response"], event["raw_response"])
                    if event["method"] == "T" and event["page_id"] != "p4":
                        self.assertEqual("NEEDS_OCR", event["raw_response"]["code"])
                        self.assertFalse(event["new_progress"])
                    else:
                        self.assertTrue(event["new_progress"])
                seen_orders += 1
        self.assertEqual(24, seen_orders)

    def test_representative_orders_C1_prefixes_shadow_and_monotonicity(self):
        orders = [["p4", "p1", "p2", "p3"], ["p1", "p4", "p2", "p3"],
                  ["p1", "p2", "p4", "p3"], ["p1", "p2", "p3", "p4"]]
        for scenario in ("S", "R", "L"):
            for order in orders:
                _, baseline, b_events = self.run_case(scenario, strategy="B", order=order)
                for k in (2, 3, 4):
                    with self.subTest(scenario=scenario, order=order, k=k):
                        _, c_state, _ = self.run_case(scenario, strategy="C", order=order, k=k)
                        engine, main, main_events = self.run_case(scenario, order=order, k=k)
                        self.assertGreaterEqual(main["calls"], c_state["calls"])
                        self.assertGreaterEqual(len(main["accepted"]), len(c_state["accepted"]))
                        self.assertEqual(self.actions(b_events[:len(main_events)]), self.actions(main_events))
                        before = canonical(main)
                        if engine.stop_info(main)["reason"] == "stagnation":
                            self.assertEqual(set(self.protocol["page_order"]), set(main["attempted_pages"]))
                            shadow = copy.deepcopy(main)
                            shadow["guard_enabled"] = False
                            tail = engine.run(shadow)
                            self.assertEqual(self.actions(b_events), self.actions(main_events + tail))
                            self.assertEqual(baseline["calls"], shadow["calls"])
                            self.assertEqual(set(baseline["accepted"]), set(shadow["accepted"]))
                            self.assertLessEqual(shadow["calls"], 20)
                        self.assertEqual(before, canonical(main))

    def test_original_canonical_70_results_remain_unchanged(self):
        with (ROOT / "inputs" / "baseline_summary.csv").open(encoding="utf-8-sig", newline="") as f:
            baseline_rows = list(csv.DictReader(f))
        self.assertEqual(70, len(baseline_rows))
        for row in baseline_rows:
            with self.subTest(run_id=row["run_id"]):
                k = int(row["k"]) if row["k"] else None
                engine, state, _ = self.run_case(row["scenario"], strategy=row["strategy"],
                                                budget=int(row["budget"]), k=k)
                self.assertEqual(int(row["completed_pages"]), len(state["accepted"]))
                self.assertEqual(int(row["calls"]), state["calls"])
                self.assertEqual(row["stop_reason"], engine.stop_info(state)["reason"])
                for field in ("no_progress_calls", "repeated_calls", "old_page_returns",
                              "network_failures", "protected_network_failures"):
                    self.assertEqual(int(row[field]), state[field])


if __name__ == "__main__":
    unittest.main(verbosity=2)
