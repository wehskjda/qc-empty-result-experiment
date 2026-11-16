"""Independent checks of the approved replay protocol, not result generators.

The seven-scenario hand table remains a separately frozen input.  These tests
exercise boundaries and adversarial bodies which the table alone cannot check.
Run with: python -m unittest discover -s experiment_v1/tests -v
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from replay_engine import (  # noqa: E402
    FixtureStore,
    PageValidator,
    ReplayEngine,
    levenshtein,
    load_inputs,
    normalize_text,
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def response(text="", *, status=200, code="OK", message="", claimed_page=None):
    return {
        "http_status": status,
        "code": code,
        "message": message,
        "claimed_page": claimed_page,
        "text": text,
    }


class TestFixture:
    """A tiny independent environment for boundary sequences."""

    def __init__(self, references, overrides=None, default_failure=None):
        self.references = references
        self.overrides = overrides or {}
        self.default_failure = default_failure

    def get(self, scenario, page_id, method, attempt_n):
        value = self.overrides.get((page_id, method, attempt_n))
        if value is None:
            value = self.default_failure
        if value is None:
            value = response(self.references[page_id], claimed_page=page_id)
        return {
            "response_id": f"test:{page_id}:{method}:{attempt_n}",
            "response": copy.deepcopy(value),
        }


class ReplayProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol, cls.frozen_fixture, cls.validator, cls.document = load_inputs(ROOT)
        cls.keys = json.loads((ROOT / "inputs" / "key_numbers.json").read_text("utf-8"))
        cls.references = {
            p: (ROOT / "inputs" / "reference_pages" / f"{p}.txt").read_text("utf-8")
            for p in cls.protocol["page_order"]
        }

    def engine(self, fixture=None, validator=None):
        return ReplayEngine(
            copy.deepcopy(self.protocol),
            fixture or self.frozen_fixture,
            validator or self.validator,
            copy.deepcopy(self.document),
        )

    def run_case(self, scenario, strategy="C", budget=20, k=2, fixture=None):
        engine = self.engine(fixture=fixture)
        state = engine.initial_state(scenario, strategy, budget, k if strategy == "C" else None)
        events = engine.run(state)
        return engine, state, events

    def test_frozen_inputs_have_not_changed(self):
        manifest = json.loads((ROOT / "input_freeze_manifest.json").read_text("utf-8"))
        self.assertTrue(manifest["experiment_engine_absent_at_freeze"])
        self.assertEqual(70, manifest["manual_expected_rows"])
        self.assertEqual(1120, manifest["fault_matrix_rows"])
        for item in manifest["input_files"]:
            with self.subTest(path=item["path"]):
                raw = (ROOT / item["path"]).read_bytes()
                self.assertEqual(item["size_bytes"], len(raw))
                self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())

    def test_edit_distance_is_computed_from_text(self):
        self.assertEqual(3, levenshtein("kitten", "sitting"))
        self.assertEqual(1, levenshtein("水温23.4", "水温23.5"))
        self.assertEqual(4, levenshtein("", "正文内容"))
        self.assertEqual("é 正文", normalize_text("  e\u0301\r\n\t正文  "))

    def test_exact_five_percent_boundary_and_number_gate(self):
        prefix = "采样深度为12米，水温为23.4摄氏度，盐度为31.8。"
        reference = prefix + "甲" * (100 - len(prefix))
        validator = PageValidator({"p1": reference}, self.keys, self.protocol)
        for edits, expected in ((5, True), (6, False)):
            with self.subTest(edits=edits):
                candidate = reference[:-edits] + "乙" * edits
                result = validator.validate("p1", response(candidate))
                self.assertEqual(edits, result["edit_distance"])
                self.assertEqual(100, result["reference_length"])
                self.assertEqual(expected, result["accepted"])

    def test_low_cer_does_not_hide_wrong_critical_number(self):
        text = self.references["p1"].replace("23.4", "23.5")
        result = self.validator.validate("p1", response(text))
        self.assertEqual(1, result["edit_distance"])
        self.assertLess(result["cer"], 0.05)
        self.assertFalse(result["accepted"])

    def test_exact_decimal_equivalence_and_sign_are_respected(self):
        equivalent = self.references["p1"].replace("31.8", "31.80")
        wrong_sign = self.references["p1"].replace("23.4", "-23.4")
        self.assertTrue(self.validator.validate("p1", response(equivalent))["accepted"])
        self.assertFalse(self.validator.validate("p1", response(wrong_sign))["accepted"])

    def test_field_values_are_not_an_unassigned_bag_of_numbers(self):
        text = self.references["p1"].replace("12米", "23.4米").replace("23.4摄氏度", "12摄氏度")
        result = self.validator.validate("p1", response(text))
        self.assertFalse(result["accepted"])

    def test_conflicting_duplicate_field_rejected_even_with_low_cer(self):
        reference = self.references["p1"].strip() + "甲" * 1000
        validator = PageValidator({"p1": reference}, self.keys, self.protocol)
        result = validator.validate("p1", response(reference + "水温为23.5摄氏度。"))
        self.assertLess(result["cer"], 0.05)
        self.assertFalse(result["accepted"])

    def test_duplicate_malformed_field_is_not_ignored_by_numeric_regex(self):
        reference = self.references["p1"].strip() + "甲" * 1000
        validator = PageValidator({"p1": reference}, self.keys, self.protocol)
        for duplicate in ("水温为未知。", "水温为23.4x摄氏度。"):
            with self.subTest(duplicate=duplicate):
                result = validator.validate("p1", response(reference + duplicate))
                self.assertLess(result["cer"], 0.05)
                self.assertFalse(result["accepted"])

    def test_success_status_and_claimed_current_page_cannot_accept_old_body(self):
        candidate = response(self.references["p1"], status=200, claimed_page="p2")
        result = self.validator.validate("p2", candidate)
        self.assertFalse(result["accepted"])
        self.assertTrue(self.validator.validate("p1", response(self.references["p1"]))["accepted"])

    def test_success_without_a_body_fails(self):
        self.assertFalse(self.validator.validate("p1", response("", message="解析成功"))["accepted"])

    def test_http_allowlist_not_error_wording_controls_network_protection(self):
        self.assertEqual([408, 502, 503, 504], self.protocol["protected_http_statuses"])
        for status in (408, 502, 503, 504):
            with self.subTest(status=status):
                fixture = TestFixture(self.references, {
                    ("p1", "T", 1): response(status=status, code="UNRELATED", message="并未使用网络二字")
                })
                engine = self.engine(fixture)
                state = engine.initial_state("test", "C", 20, 2)
                event = engine.step(state)
                self.assertTrue(event["network_protected"])
                self.assertEqual((0, 1), (state["S"], state["E"]))
                self.assertEqual(1, state["attempts"]["p1"]["T"])
                self.assertEqual(1, state["calls"])
        for status in (200, 400, 422, 429, 500, 501):
            with self.subTest(status=status):
                fixture = TestFixture(self.references, {
                    ("p1", "T", 1): response(status=status, code="NETWORK_TIMEOUT", message="网络超时，请重试")
                })
                engine = self.engine(fixture)
                state = engine.initial_state("test", "C", 20, 2)
                event = engine.step(state)
                self.assertFalse(event["network_protected"])
                self.assertEqual((1, 2), (state["S"], state["E"]))

    def test_network_freezes_existing_nonzero_stagnation_then_method_cap_wins(self):
        bad = response(status=422, code="PARSE_FAILED", message="正文尚未完成")
        fixture = TestFixture(self.references, {
            ("p4", "T", 1): bad,
            ("p4", "T", 2): response(status=503, code="SERVICE_UNAVAILABLE"),
            ("p4", "T", 3): response(status=504, code="GATEWAY_TIMEOUT"),
            ("p4", "T", 4): bad,
        })
        engine, state, events = self.run_case("test", fixture=fixture)
        self.assertEqual([1, 1, 1, 2], [e["S_after"] for e in events[-4:]])
        self.assertEqual([2, 1, 0, 0], [e["E_after"] for e in events[-4:]])
        self.assertEqual(7, state["calls"])
        self.assertEqual(4, state["attempts"]["p4"]["T"])
        self.assertEqual("O", state["methods"]["p4"])
        self.assertEqual("stagnation", engine.stop_info(state)["reason"])

    def test_network_protection_is_not_refilled_by_page_or_new_progress(self):
        network = response(status=503, code="UNAVAILABLE")
        fixture = TestFixture(self.references, {
            ("p1", "T", 1): network,
            ("p2", "T", 1): network,
            ("p3", "T", 1): network,
            ("p1", "T", 2): network,
            ("p2", "T", 2): network,
        })
        _, state, events = self.run_case("test", fixture=fixture)
        self.assertEqual([True, True, False, False, False, False], [e["network_protected"] for e in events])
        self.assertEqual([0, 0, 1, 0, 1, 2], [e["S_after"] for e in events])
        self.assertEqual(0, state["E"])
        self.assertEqual(6, state["calls"])

    def test_four_network_failures_consume_each_method_limit(self):
        network = response(status=503, code="UNAVAILABLE")
        overrides = {("p4", m, n): network for m in ("T", "O") for n in range(1, 5)}
        fixture = TestFixture(self.references, overrides)
        engine, state, events = self.run_case("test", strategy="B", fixture=fixture)
        self.assertEqual(11, state["calls"])
        self.assertEqual({"T": 4, "O": 4}, state["attempts"]["p4"])
        self.assertEqual("methods_exhausted", engine.stop_info(state)["reason"])
        self.assertFalse(any(e["attempt_n"] > 4 for e in events))

    def test_needs_ocr_route_requires_both_status_and_code(self):
        for status, expected_method in ((200, "T"), (422, "O")):
            fixture = TestFixture(self.references, {
                ("p1", "T", 1): response(status=status, code="NEEDS_OCR")
            })
            engine = self.engine(fixture)
            state = engine.initial_state("test", "B", 20)
            engine.step(state)
            self.assertEqual(expected_method, state["methods"]["p1"])

    def test_fourth_attempt_success_is_accepted_before_stagnation_check(self):
        engine, state, events = self.run_case("R", k=4)
        self.assertEqual((7, 4, 0), (state["calls"], len(state["accepted"]), state["S"]))
        self.assertEqual("complete", engine.stop_info(state)["reason"])
        self.assertEqual(("p4", "T", 4), (events[-1]["page_id"], events[-1]["method"], events[-1]["attempt_n"]))
        self.assertTrue(events[-1]["validation"]["accepted"])

    def test_budget_tie_has_priority_over_stagnation(self):
        fixture = TestFixture(self.references, default_failure=response(status=422, code="FAIL"))
        engine, state, _ = self.run_case("test", budget=2, k=2, fixture=fixture)
        self.assertEqual((2, 2), (state["calls"], state["S"]))
        self.assertEqual("budget_exhausted", engine.stop_info(state)["reason"])

    def test_completion_at_last_budget_call_has_priority(self):
        engine, state, _ = self.run_case("P", budget=4, k=2)
        self.assertEqual((4, 4), (state["calls"], len(state["accepted"])))
        self.assertEqual("complete", engine.stop_info(state)["reason"])

    def test_shadow_uses_next_attempt_in_independent_state_and_matches_B(self):
        engine, main, main_events = self.run_case("R", k=2)
        frozen = canonical(main)
        shadow = copy.deepcopy(main)
        shadow["guard_enabled"] = False
        shadow_events = engine.run(shadow)
        self.assertEqual(frozen, canonical(main))
        self.assertEqual((5, 3), (main["calls"], len(main["accepted"])))
        self.assertEqual((7, 4), (shadow["calls"], len(shadow["accepted"])))
        self.assertEqual(("p4", "T", 3), (shadow_events[0]["page_id"], shadow_events[0]["method"], shadow_events[0]["attempt_n"]))
        _, baseline, baseline_events = self.run_case("R", strategy="B")
        projection = lambda e: (e["page_id"], e["method"], e["attempt_n"], e["raw_response"])
        self.assertEqual([projection(e) for e in baseline_events], [projection(e) for e in main_events + shadow_events])
        self.assertEqual(set(baseline["accepted"]), set(shadow["accepted"]))

    def test_all_C_prefixes_and_shadow_actions_match_same_budget_B(self):
        projection = lambda e: (e["page_id"], e["method"], e["attempt_n"], e["raw_response"])
        paused = 0
        for scenario in self.protocol["scenarios"]:
            for budget in self.protocol["budgets"]:
                _, baseline, baseline_events = self.run_case(scenario, strategy="B", budget=budget)
                for k in self.protocol["stagnation_thresholds"]:
                    with self.subTest(scenario=scenario, budget=budget, k=k):
                        engine, main, main_events = self.run_case(scenario, budget=budget, k=k)
                        frozen = canonical(main)
                        self.assertEqual(
                            [projection(e) for e in baseline_events[:len(main_events)]],
                            [projection(e) for e in main_events],
                        )
                        if engine.stop_info(main)["reason"] == "stagnation":
                            paused += 1
                            shadow = copy.deepcopy(main)
                            shadow["guard_enabled"] = False
                            tail = engine.run(shadow)
                            self.assertEqual(frozen, canonical(main))
                            self.assertEqual(
                                [projection(e) for e in baseline_events],
                                [projection(e) for e in main_events + tail],
                            )
                            self.assertEqual(set(baseline["accepted"]), set(shadow["accepted"]))
                            self.assertEqual(baseline["calls"], shadow["calls"])
                            self.assertLessEqual(shadow["calls"], budget)
        self.assertEqual(26, paused)

    def test_identical_text_on_distinct_source_pages_counts_twice(self):
        references = copy.deepcopy(self.references)
        references["p2"] = references["p1"]
        keys = copy.deepcopy(self.keys)
        keys["pages"]["p2"] = keys["pages"]["p1"]
        validator = PageValidator(references, keys, self.protocol)
        fixture = TestFixture(references)
        engine = self.engine(fixture=fixture, validator=validator)
        state = engine.initial_state("test", "C", 20, 2)
        events = engine.run(state)
        self.assertEqual(4, len(state["accepted"]))
        self.assertEqual(4, sum(bool(e["new_progress"]) for e in events))
        self.assertFalse(any(e["old_page_return"] for e in events))
        self.assertEqual(0, state["old_page_returns"])

    def test_error_message_changes_do_not_create_new_progress_or_new_failure(self):
        overrides = {
            ("p4", "T", n): response(status=422, code="FAIL", message=f"措辞改变，第{n}次")
            for n in range(1, 5)
        }
        fixture = TestFixture(self.references, overrides)
        engine, state, events = self.run_case("test", k=3, fixture=fixture)
        self.assertEqual((6, 3, 3), (state["calls"], len(state["accepted"]), state["S"]))
        self.assertEqual(2, state["repeated_calls"])
        self.assertEqual("stagnation", engine.stop_info(state)["reason"])
        self.assertEqual([1, 2, 3], [e["S_after"] for e in events[-3:]])

    def test_numeric_prefixes_cannot_match_a_longer_malformed_value(self):
        for replacement in ("31.8x", "31.8.9", "31.8e5"):
            with self.subTest(replacement=replacement):
                candidate = self.references["p1"].replace("31.8", replacement)
                result = self.validator.validate("p1", response(candidate))
                self.assertLess(result["cer"], 0.05)
                self.assertFalse(result["accepted"])

    def test_faults_are_independent_of_call_order_and_caller_mutations(self):
        fixture = FixtureStore(ROOT / "inputs")
        first = fixture.get("D", "p2", "T", 1)
        preserved = copy.deepcopy(first)
        fixture.get("N", "p1", "T", 2)
        self.assertEqual(preserved, fixture.get("D", "p2", "T", 1))
        first["response"]["text"] = "caller mutation"
        self.assertEqual(preserved, fixture.get("D", "p2", "T", 1))

    def test_repetition_excludes_attempt_counter_but_not_distinct_methods(self):
        _, baseline, _ = self.run_case("F", strategy="B")
        self.assertEqual(6, baseline["repeated_calls"])
        for k, repeated in ((2, 2), (3, 3), (4, 3)):
            _, state, _ = self.run_case("F", k=k)
            self.assertEqual(repeated, state["repeated_calls"])
        _, gradual, _ = self.run_case("R", strategy="B")
        self.assertEqual(0, gradual["repeated_calls"])
        _, needs_ocr, _ = self.run_case("S", strategy="B")
        self.assertEqual(0, needs_ocr["repeated_calls"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
