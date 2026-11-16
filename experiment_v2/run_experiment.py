"""Run C1 and all 24 initial orders on the frozen deterministic environment."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import platform
import shutil
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.replay_engine import ReplayEngine, load_inputs


EXPECTED = ["run_id", "scenario", "order_id", "strategy", "budget", "k", "completed_pages",
            "calls", "stop_reason", "shadow_extra_calls", "shadow_final_pages",
            "shadow_total_calls", "lost_pages", "false_stop"]
EXTRA = ["no_progress_calls", "repeated_calls", "old_page_returns", "network_failures",
         "protected_network_failures", "attempted_count", "unattempted_count",
         "attempted_pages", "unattempted_pages", "coverage_call", "coverage_S",
         "deferred_stagnation_events", "main_trace_path", "shadow_trace_path"]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def cell(value):
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def write_csv(path, fields, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: cell(row[k]) for k in fields} for row in rows)


def rel(path, root):
    return path.relative_to(root).as_posix()


def verify_baseline(root, run_dir, phase):
    preservation = read_json(root / "baseline_preservation_manifest.json")
    issues = []
    for entry in preservation["files"]:
        path = root.parent / entry["path"]
        if not path.is_file() or sha(path) != entry["sha256"]:
            issues.append({"path": entry["path"], "reason": "missing_or_changed"})
    result = {"passed": not issues, "file_count": len(preservation["files"]), "issues": issues}
    write_json(run_dir / f"baseline_integrity_{phase}.json", result)
    if issues:
        raise RuntimeError("Original v1 files changed; preserve evidence and investigate")


def freeze_and_sources(root, run_dir):
    manifest = read_json(root / "input_freeze_manifest.json")
    issues, declared = [], set()
    for e in manifest["input_files"]:
        path = (root / e["path"]).resolve()
        if not path.is_relative_to((root / "inputs").resolve()):
            raise ValueError("Input path escapes inputs directory")
        declared.add(e["path"])
        if not path.is_file() or sha(path) != e["sha256"] or path.stat().st_size != e["size_bytes"]:
            issues.append(e["path"])
    actual = {rel(p, root) for p in (root / "inputs").rglob("*") if p.is_file()}
    if actual != declared:
        issues.append({"file_set_difference": sorted(actual ^ declared)})
    if sha(root / "baseline_preservation_manifest.json") != manifest["baseline_preservation_manifest_sha256"]:
        issues.append("baseline_preservation_manifest_sha256")
    write_json(run_dir / "input_integrity.json", {
        "passed": not issues, "file_count": len(declared), "issues": issues,
        "freeze_manifest_sha256": sha(root / "input_freeze_manifest.json")})
    if issues:
        raise ValueError("Frozen extension inputs changed")
    shutil.copy2(root / "input_freeze_manifest.json", run_dir / "input_freeze_manifest.json")
    shutil.copy2(root / "baseline_preservation_manifest.json", run_dir / "baseline_preservation_manifest.json")
    for e in manifest["input_files"]:
        dest = run_dir / "frozen_inputs" / e["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / e["path"], dest)
    sources = [root / "run_experiment.py"]
    for folder in ("src", "tests"):
        sources.extend(sorted((root / folder).rglob("*.py")))
    source_manifest = []
    for path in sources:
        dest = run_dir / "source_snapshot" / rel(path, root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        source_manifest.append({"path": rel(path, root), "sha256": sha(path)})
    write_json(run_dir / "source_manifest.json", source_manifest)
    return manifest, source_manifest


def execute(engine, state, folder):
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "initial_state.json", state)
    events = []
    with (folder / "events.jsonl").open("x", encoding="utf-8", newline="\n") as f:
        try:
            while engine.stop_info(state)["reason"] is None:
                event = engine.step(state)
                events.append(event)
                f.write(canonical(event) + "\n")
                f.flush()
            state["stop_reason"] = engine.stop_info(state)["reason"]
        except Exception:
            write_json(folder / "interrupted_state.json", state)
            raise
    write_json(folder / "final_state.json", state)
    return events


def configurations(protocol, orders):
    for scenario in protocol["permutation_scenarios"]:
        for order in orders:
            for budget in protocol["budgets"]:
                if order["order_id"] == protocol["canonical_order_id"]:
                    yield scenario, order, budget, "A", None
                yield scenario, order, budget, "B", None
                for strategy in ("C", "C1"):
                    for k in protocol["stagnation_thresholds"]:
                        yield scenario, order, budget, strategy, k


def main_row(rid, order_id, state, events, engine):
    visited = state["attempted_pages"]
    unvisited = [p for p in engine.pages if p not in visited]
    coverage = next((e for e in events if e["coverage_completed_this_call"]), None)
    row = {k: "" for k in EXPECTED + EXTRA}
    row.update({"run_id": rid, "scenario": state["scenario"], "order_id": order_id,
                "strategy": state["strategy"], "budget": state["budget"],
                "k": state["k"] if state["k"] is not None else "",
                "completed_pages": len(state["accepted"]), "calls": state["calls"],
                "stop_reason": engine.stop_info(state)["reason"],
                "attempted_count": len(visited), "unattempted_count": len(unvisited),
                "attempted_pages": "|".join(visited), "unattempted_pages": "|".join(unvisited),
                "coverage_call": coverage["global_call"] if coverage else "",
                "coverage_S": coverage["S_after"] if coverage else "",
                "deferred_stagnation_events": sum(e["stagnation_blocked_by_coverage"] for e in events)})
    for key in ("no_progress_calls", "repeated_calls", "old_page_returns", "network_failures",
                "protected_network_failures"):
        row[key] = state[key]
    return row


def compare_tables(run_dir, name, expected, actual, fields):
    expected_map = {r["run_id"]: r for r in expected}
    actual_map = {r["run_id"]: r for r in actual}
    if len(expected_map) != len(expected) or len(actual_map) != len(actual):
        raise ValueError("Duplicate comparison IDs")
    comparisons, differences = [], []
    for rid in sorted(set(expected_map) | set(actual_map)):
        for field in fields:
            e = cell(expected_map.get(rid, {}).get(field, "<missing>"))
            a = cell(actual_map.get(rid, {}).get(field, "<missing>"))
            item = {"run_id": rid, "field": field, "expected": e, "actual": a, "matched": e == a}
            comparisons.append(item)
            if e != a:
                differences.append({**item, "classification": "unresolved",
                                    "note": "Preserve original evidence before diagnosing implementation, expectation or input."})
    write_csv(run_dir / f"{name}_fields.csv", ["run_id", "field", "expected", "actual", "matched"], comparisons)
    write_json(run_dir / f"{name}_differences.json", differences)
    report = {"passed": not differences, "rows": len(actual), "conditions": len(actual),
              "field_comparisons": len(comparisons), "differences": len(differences)}
    write_json(run_dir / f"{name}.json", report)
    return report


def action(event):
    return {k: event[k] for k in ("global_call", "scenario", "document_id", "source_page_key", "page_id",
                                 "method", "attempt_n", "response_id", "raw_response", "validation",
                                 "new_progress", "input_state_hash")}


def audit(engine, records, isolation):
    issues, fairness = [], {}
    counts = {"main_conditions": len(records), "events_checked": 0, "states_checked": 0,
              "C_prefixes": 0, "C1_prefixes": 0, "full_B_continuations": 0,
              "C1_C_pairs": 0, "shadow_runs": 0}
    for rid, rec in records.items():
        main = rec["main_state"]
        sequence = rec["main_events"] + rec["shadow_events"]
        if rec["shadow_state"] is not None:
            counts["shadow_runs"] += 1
        seen_pages = set()
        for e in sequence:
            counts["events_checked"] += 1
            seen_pages.add(e["page_id"])
            if set(e["attempted_pages_after"]) != seen_pages or e["coverage_complete_after"] != (len(seen_pages) == 4):
                issues.append({"check": "actual_attempt_coverage", "run_id": rid, "call": e["global_call"]})
            key = (e["scenario"], e["document_id"], e["page_id"], e["method"], e["attempt_n"])
            value = canonical(e["raw_response"])
            if key in fairness and fairness[key] != value:
                issues.append({"check": "same_fault_key_same_response_across_orders", "run_id": rid})
            fairness.setdefault(key, value)
            frozen = engine.fixture.get(e["scenario"], e["page_id"], e["method"], e["attempt_n"])
            if value != canonical(frozen["response"]) or e["response_id"] != frozen["response_id"]:
                issues.append({"check": "frozen_fault_match", "run_id": rid})
            if e["global_call"] > main["budget"] or (main["strategy"] != "A" and e["attempt_n"] > 4):
                issues.append({"check": "budget_or_method_limit", "run_id": rid})
        if [e["global_call"] for e in sequence] != list(range(1, len(sequence) + 1)):
            issues.append({"check": "sequential_calls", "run_id": rid})
        for state in (main, rec["shadow_state"]):
            if state is None:
                continue
            counts["states_checked"] += 1
            visited = [p for p in engine.pages if sum(state["attempts"][p].values()) > 0]
            if visited != state["attempted_pages"] or state["coverage_complete"] != (len(visited) == 4):
                issues.append({"check": "state_coverage", "run_id": rid})
            if sum(sum(v.values()) for v in state["attempts"].values()) != state["calls"]:
                issues.append({"check": "attempt_accounting", "run_id": rid})
            if state["no_progress_calls"] != state["calls"] - len(state["accepted"]):
                issues.append({"check": "no_progress_accounting", "run_id": rid})
            if state["protected_network_failures"] > 2 or state["E"] < 0:
                issues.append({"check": "network_credit_limit", "run_id": rid})
        if main["strategy"] == "C1" and main["stop_reason"] == "stagnation" and not main["coverage_complete"]:
            issues.append({"check": "C1_no_unattempted_at_stagnation", "run_id": rid})
        if main["strategy"] in ("C", "C1"):
            baseline_id = f'{main["scenario"]}_o{rec["order_id"]}_L{main["budget"]}_B'
            b = records[baseline_id]
            b_actions = [action(e) for e in b["main_events"]]
            main_actions = [action(e) for e in rec["main_events"]]
            counts[main["strategy"] + "_prefixes"] += 1
            counts["full_B_continuations"] += 1
            if main_actions != b_actions[:len(main_actions)]:
                issues.append({"check": "guard_is_B_prefix", "run_id": rid})
            if [action(e) for e in sequence] != b_actions:
                issues.append({"check": "guard_plus_shadow_equals_B", "run_id": rid})
        if main["strategy"] == "C1":
            c_id = rid.replace("_C1_", "_C_")
            c_state = records[c_id]["main_state"]
            counts["C1_C_pairs"] += 1
            if main["calls"] < c_state["calls"] or not set(c_state["accepted"]).issubset(main["accepted"]):
                issues.append({"check": "C1_monotone_prefix_extension", "run_id": rid})
    issues.extend({"check": "main_shadow_isolation", "run_id": c["run_id"]}
                  for c in isolation if not c["passed"])
    return {"passed": not issues, "issues": issues, "counts": counts,
            "unique_fault_keys_seen": len(fairness), "snapshot_isolation": isolation}


def run_grid(root, run_dir, manifest):
    extension = read_json(root / "inputs" / "extension_protocol.json")
    orders = read_json(root / "inputs" / "orders.json")
    protocol, fixture, validator, document = load_inputs(root)
    engine = ReplayEngine(protocol, fixture, validator, document)
    rows, records, isolation = [], {}, []
    current_scenario = None
    for scenario, order, budget, strategy, k in configurations(extension, orders):
        if scenario != current_scenario:
            if current_scenario is not None:
                print(json.dumps({"scenario_finished": current_scenario, "conditions_saved": len(rows)}), flush=True)
            current_scenario = scenario
        rid = f'{scenario}_o{order["order_id"]}_L{budget}_{strategy}' + (f"_k{k}" if k is not None else "")
        state = engine.initial_state(scenario, strategy, budget, k, initial_order=order["queue"])
        folder = run_dir / "conditions" / rid
        main_dir = folder / "main"
        main_events = execute(engine, state, main_dir)
        row = main_row(rid, order["order_id"], state, main_events, engine)
        row["main_trace_path"] = rel(main_dir / "events.jsonl", run_dir)
        main_summary = {**row, "stop_info": engine.stop_info(state),
                        "accepted_page_ids": sorted(state["accepted"]),
                        "unfinished_page_ids": [p for p in engine.pages if p not in state["accepted"]],
                        "initial_order": order["queue"], "phase": "main"}
        write_json(main_dir / "summary.json", main_summary)
        sealed = {rel(main_dir / name, run_dir): sha(main_dir / name)
                  for name in ("events.jsonl", "final_state.json", "summary.json")}
        frozen_state = canonical(state)
        write_json(main_dir / "sealed_manifest.json", sealed)
        shadow_state, shadow_events = None, []
        if strategy in ("C", "C1") and state["stop_reason"] == "stagnation":
            shadow_state = copy.deepcopy(state)
            shadow_state["guard_enabled"] = False
            shadow_dir = folder / "shadow"
            shadow_events = execute(engine, shadow_state, shadow_dir)
            row.update({"shadow_extra_calls": shadow_state["calls"] - state["calls"],
                        "shadow_final_pages": len(shadow_state["accepted"]),
                        "shadow_total_calls": shadow_state["calls"],
                        "lost_pages": len(shadow_state["accepted"]) - len(state["accepted"]),
                        "false_stop": len(shadow_state["accepted"]) == len(engine.pages),
                        "shadow_trace_path": rel(shadow_dir / "events.jsonl", run_dir)})
            write_json(shadow_dir / "summary.json", {
                "run_id": rid, "phase": "shadow", "policy": "B", "original_budget": budget,
                "main_pages": len(state["accepted"]), "main_calls": state["calls"],
                "extra_calls": row["shadow_extra_calls"], "final_pages": row["shadow_final_pages"],
                "total_calls": row["shadow_total_calls"], "lost_pages": row["lost_pages"],
                "recovered_page_ids": sorted(set(shadow_state["accepted"]) - set(state["accepted"])),
                "false_stop": row["false_stop"], "stop_info": engine.stop_info(shadow_state)})
        unchanged = canonical(state) == frozen_state and all(sha(run_dir / p) == h for p, h in sealed.items())
        check = {"run_id": rid, "shadow_executed": shadow_state is not None, "passed": unchanged,
                 "sealed_files": sealed}
        write_json(folder / "isolation_check.json", check)
        isolation.append(check)
        if not unchanged:
            raise AssertionError(f"Main state changed during shadow: {rid}")
        rows.append(row)
        records[rid] = {"order_id": order["order_id"], "main_state": state, "main_events": main_events,
                        "shadow_state": shadow_state, "shadow_events": shadow_events}
    print(json.dumps({"scenario_finished": current_scenario, "conditions_saved": len(rows)}), flush=True)
    if len(rows) != manifest["expected_unique_conditions"]:
        raise AssertionError("Unexpected unique condition count")
    write_csv(run_dir / "summary.csv", EXPECTED + EXTRA, rows)
    canonical_rows = [r for r in rows if r["order_id"] == extension["canonical_order_id"]]
    permutation_rows = [r for r in rows if r["strategy"] != "A"]
    write_csv(run_dir / "canonical_summary.csv", EXPECTED + EXTRA, canonical_rows)
    write_csv(run_dir / "permutation_summary.csv", EXPECTED + EXTRA, permutation_rows)
    # Read pre-saved expectations only now, after all actual executions.
    with (root / "inputs" / "expected_all_conditions.csv").open(encoding="utf-8-sig", newline="") as f:
        expected = list(csv.DictReader(f))
    comparison = compare_tables(run_dir, "comparison_summary", expected, rows, EXPECTED)
    baseline_path = root / "inputs" / "baseline_summary.csv"
    with baseline_path.open(encoding="utf-8-sig", newline="") as f:
        old = list(csv.DictReader(f))
    old_fields = list(old[0].keys())
    regression_fields = [f for f in old_fields if not f.endswith("trace_path")]
    migrated = []
    for row in canonical_rows:
        if row["strategy"] == "C1":
            continue
        r = dict(row)
        r["run_id"] = f'{r["scenario"]}_L{r["budget"]}_{r["strategy"]}' + (f'_k{r["k"]}' if r["k"] != "" else "")
        migrated.append(r)
    regression = compare_tables(run_dir, "regression_v1_70", old, migrated, regression_fields)
    result_audit = audit(engine, records, isolation)
    write_json(run_dir / "audit.json", result_audit)
    return rows, comparison, regression, result_audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    root = parser.parse_args().root.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = root / "runs" / f"{stamp}_{uuid.uuid4().hex[:12]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "run_meta.json", {"started_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_kind": "controlled_deterministic_C1_and_order_replay", "python": sys.version,
        "executable": sys.executable, "platform": platform.platform(),
        "run_directory": str(run_dir), "expected_answers_used_for_policy": False})
    try:
        manifest, sources = freeze_and_sources(root, run_dir)
        verify_baseline(root, run_dir, "before")
        rows, comparison, regression, result_audit = run_grid(root, run_dir, manifest)
        verify_baseline(root, run_dir, "after")
        changed_sources = [e["path"] for e in sources if sha(root / e["path"]) != e["sha256"]]
        changed_inputs = [e["path"] for e in manifest["input_files"] if sha(root / e["path"]) != e["sha256"]]
        passed = comparison["passed"] and regression["passed"] and result_audit["passed"] and not changed_sources and not changed_inputs
        result = {"status": "passed" if passed else "failed", "unique_main_conditions": len(rows),
            "canonical_conditions": sum(r["order_id"] == "1234" for r in rows),
            "permutation_conditions": sum(r["strategy"] != "A" for r in rows),
            "comparison_differences": comparison["differences"], "v1_regression_passed": regression["passed"],
            "audit_passed": result_audit["passed"], "source_changes": changed_sources, "input_changes": changed_inputs,
            "main_calls": sum(r["calls"] for r in rows),
            "shadow_calls": sum(r["shadow_extra_calls"] for r in rows if r["shadow_extra_calls"] != ""),
            "shadow_runs": sum(r["shadow_extra_calls"] != "" for r in rows),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(), "run_directory": str(run_dir)}
        write_json(run_dir / "run_outcome.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if passed else 1
    except Exception as error:
        detail = {"status": "error", "error_type": type(error).__name__, "message": str(error),
                  "traceback": traceback.format_exc(), "evidence_preserved": True,
                  "run_directory": str(run_dir)}
        write_json(run_dir / "error.json", detail)
        write_json(run_dir / "run_outcome.json", detail)
        print(json.dumps(detail, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
