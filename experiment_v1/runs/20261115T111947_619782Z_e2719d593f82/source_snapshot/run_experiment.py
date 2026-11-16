"""Run the frozen, controlled replay grid without reading expected answers as policy.

Standard library only. Every invocation creates a new run directory; failed runs
are retained. The frozen manual expectations are read only after all runs finish.
"""

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


EXPECTED_COLUMNS = [
    "run_id", "scenario", "strategy", "budget", "k", "completed_pages", "calls",
    "stop_reason", "shadow_extra_calls", "shadow_final_pages", "shadow_total_calls",
    "lost_pages", "false_stop",
]
EXTRA_COLUMNS = [
    "no_progress_calls", "repeated_calls", "old_page_returns", "network_failures",
    "protected_network_failures", "main_trace_path", "shadow_trace_path",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally replacing evidence from a run.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def validate_freeze(root: Path, run_dir: Path) -> dict:
    manifest_path = root / "input_freeze_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks, issues = [], []
    declared = set()
    for entry in manifest["input_files"]:
        candidate = (root / entry["path"]).resolve()
        if not candidate.is_relative_to((root / "inputs").resolve()):
            raise ValueError(f"Frozen input escapes inputs directory: {entry['path']}")
        declared.add(relative(candidate, root))
        exists = candidate.is_file()
        actual_hash = sha256_file(candidate) if exists else None
        actual_size = candidate.stat().st_size if exists else None
        ok = actual_hash == entry["sha256"] and actual_size == entry["size_bytes"]
        checks.append({"path": entry["path"], "expected_sha256": entry["sha256"],
                       "actual_sha256": actual_hash, "actual_size": actual_size,
                       "matched": ok})
        if not ok:
            issues.append({"path": entry["path"], "reason": "hash_or_size_mismatch"})
    actual_files = {relative(p, root) for p in (root / "inputs").rglob("*") if p.is_file()}
    if actual_files != declared:
        issues.append({"reason": "input_file_set_mismatch", "undeclared": sorted(actual_files - declared),
                       "missing": sorted(declared - actual_files)})
    result = {"manifest_sha256": sha256_file(manifest_path), "passed": not issues,
              "checks": checks, "issues": issues}
    write_json(run_dir / "input_integrity.json", result)
    if issues:
        raise ValueError("Input freeze verification failed; see input_integrity.json")
    shutil.copy2(manifest_path, run_dir / "input_freeze_manifest.json")
    for entry in manifest["input_files"]:
        dest = run_dir / "frozen_inputs" / entry["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / entry["path"], dest)
    return manifest


def snapshot_sources(root: Path, run_dir: Path) -> list[dict]:
    sources = [root / "run_experiment.py"]
    for folder in ("src", "tests"):
        sources.extend(sorted((root / folder).rglob("*.py")))
    snapshots = []
    for source in sources:
        rel = relative(source, root)
        dest = run_dir / "source_snapshot" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        snapshots.append({"path": rel, "sha256": sha256_file(source),
                          "size_bytes": source.stat().st_size})
    write_json(run_dir / "source_manifest.json", snapshots)
    return snapshots


def execute_streamed(engine: ReplayEngine, state: dict, folder: Path) -> list[dict]:
    """Persist each event before the next call, including on a later exception."""
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / "initial_state.json", state)
    events = []
    with (folder / "events.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        try:
            while engine.stop_info(state)["reason"] is None:
                event = engine.step(state)
                events.append(event)
                stream.write(canonical(event) + "\n")
                stream.flush()
            state["stop_reason"] = engine.stop_info(state)["reason"]
        except Exception:
            write_json(folder / "interrupted_state.json", state)
            raise
    write_json(folder / "final_state.json", state)
    return events


def main_summary(run_id: str, state: dict, engine: ReplayEngine) -> dict:
    return {
        "run_id": run_id, "scenario": state["scenario"], "strategy": state["strategy"],
        "budget": state["budget"], "k": state["k"] if state["k"] is not None else "",
        "completed_pages": len(state["accepted"]), "calls": state["calls"],
        "stop_reason": engine.stop_info(state)["reason"],
        "stop_triggers": engine.stop_info(state)["triggers"],
        "accepted_pages": sorted(state["accepted"]),
        "unfinished_pages": [p for p in engine.pages if p not in state["accepted"]],
        "remaining_budget": state["budget"] - state["calls"],
        "no_progress_calls": state["no_progress_calls"],
        "repeated_calls": state["repeated_calls"],
        "old_page_returns": state["old_page_returns"],
        "network_failures": state["network_failures"],
        "protected_network_failures": state["protected_network_failures"],
    }


def cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def compare_expected(root: Path, run_dir: Path, summaries: list[dict], expected_count: int) -> list[dict]:
    # The expected file does not influence fixture lookup, policy, or conditions.
    with (root / "inputs" / "manual_expected.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise ValueError("Unexpected manual expectation columns")
        expected_rows = list(reader)
    expected = {r["run_id"]: r for r in expected_rows}
    actual = {r["run_id"]: r for r in summaries}
    if len(expected_rows) != expected_count or len(expected) != len(expected_rows):
        raise ValueError("Manual expected row count or unique IDs do not match freeze")
    comparisons, differences = [], []
    for run_id in sorted(set(expected) | set(actual)):
        for field in EXPECTED_COLUMNS:
            expected_value = expected.get(run_id, {}).get(field, "<missing_run>")
            actual_value = cell(actual.get(run_id, {}).get(field, "<missing_run>"))
            matched = expected_value == actual_value
            row = {"run_id": run_id, "field": field, "expected": expected_value,
                   "actual": actual_value, "matched": str(matched).lower()}
            comparisons.append(row)
            if not matched:
                differences.append({**row, "classification": "unresolved",
                                    "note": "Preserve this run; diagnose implementation versus manual expectation before correction."})
    write_csv(run_dir / "comparison.csv", ["run_id", "field", "expected", "actual", "matched"], comparisons)
    write_json(run_dir / "differences.json", differences)
    write_json(run_dir / "comparison_summary.json", {
        "expected_runs": len(expected), "actual_runs": len(actual),
        "field_comparisons": len(comparisons), "differences": len(differences),
        "expected_file_sha256": sha256_file(root / "inputs" / "manual_expected.csv"),
    })
    return differences


def action_result(event: dict) -> dict:
    """Exclude C-only S/E, strategy diagnostics and stop flags from B matching."""
    return {key: event[key] for key in (
        "global_call", "scenario", "document_id", "source_page_key", "page_id", "method",
        "attempt_n", "response_id", "raw_response", "validation", "new_progress",
        "input_state_hash",
    )}


def audit_runs(engine: ReplayEngine, records: dict, snapshot_checks: list[dict]) -> dict:
    issues, fairness = [], {}
    counts = {"events_checked": 0, "C_prefixes_checked": 0, "C_full_continuations_checked": 0,
              "states_checked": 0, "fixture_matches_checked": 0}
    for run_id, record in records.items():
        main_state = record["main_state"]
        sequence = record["main_events"] + record["shadow_events"]
        for event in sequence:
            counts["events_checked"] += 1
            key = (event["scenario"], event["document_id"], event["page_id"],
                   event["method"], event["attempt_n"])
            raw = canonical(event["raw_response"])
            if key in fairness and fairness[key]["raw"] != raw:
                issues.append({"check": "same_key_same_response", "run_id": run_id,
                               "key": list(key), "first_run": fairness[key]["run_id"]})
            else:
                fairness.setdefault(key, {"raw": raw, "run_id": run_id})
            frozen = engine.fixture.get(event["scenario"], event["page_id"],
                                        event["method"], event["attempt_n"])
            counts["fixture_matches_checked"] += 1
            if raw != canonical(frozen["response"]) or event["response_id"] != frozen["response_id"]:
                issues.append({"check": "frozen_fixture_response", "run_id": run_id,
                               "global_call": event["global_call"]})
            if event["global_call"] > main_state["budget"]:
                issues.append({"check": "budget", "run_id": run_id})
            if main_state["strategy"] != "A":
                if event["attempt_n"] > engine.protocol["max_attempts_per_page_method"]:
                    issues.append({"check": "method_limit", "run_id": run_id})
            if event["post_state"]["calls"] != event["global_call"]:
                issues.append({"check": "event_call_number", "run_id": run_id})
        if [e["global_call"] for e in sequence] != list(range(1, len(sequence) + 1)):
            issues.append({"check": "sequential_calls", "run_id": run_id})
        for phase in ("main", "shadow"):
            state = record.get(f"{phase}_state")
            if state is None:
                continue
            counts["states_checked"] += 1
            if sum(sum(m.values()) for m in state["attempts"].values()) != state["calls"]:
                issues.append({"check": "attempt_accounting", "run_id": run_id, "phase": phase})
            if state["calls"] > state["budget"]:
                issues.append({"check": "state_budget", "run_id": run_id, "phase": phase})
            if state["protected_network_failures"] > engine.protocol["network_protection_per_document"]:
                issues.append({"check": "network_credit_limit", "run_id": run_id, "phase": phase})
            if state["no_progress_calls"] != state["calls"] - len(state["accepted"]):
                issues.append({"check": "no_progress_accounting", "run_id": run_id, "phase": phase})
            if main_state["strategy"] != "A" and any(
                    n > engine.protocol["max_attempts_per_page_method"]
                    for m in state["attempts"].values() for n in m.values()):
                issues.append({"check": "state_method_limit", "run_id": run_id, "phase": phase})
        if main_state["strategy"] == "C":
            b_id = f'{main_state["scenario"]}_L{main_state["budget"]}_B'
            b_actions = [action_result(e) for e in records[b_id]["main_events"]]
            main_actions = [action_result(e) for e in record["main_events"]]
            combined_actions = [action_result(e) for e in sequence]
            counts["C_prefixes_checked"] += 1
            counts["C_full_continuations_checked"] += 1
            if main_actions != b_actions[:len(main_actions)]:
                issues.append({"check": "C_is_B_prefix", "run_id": run_id})
            if combined_actions != b_actions:
                issues.append({"check": "C_plus_shadow_equals_B", "run_id": run_id})
    for check in snapshot_checks:
        if not check["passed"]:
            issues.append({"check": "main_snapshot_isolation", **check})
    return {"passed": not issues, "counts": counts, "unique_fault_keys_seen": len(fairness),
            "main_snapshot_isolation_checks": snapshot_checks, "issues": issues,
            "scope": "Deterministic controlled replay only; no real parsing, OCR or network."}


def run_grid(root: Path, run_dir: Path, manifest: dict) -> tuple[list[dict], dict]:
    protocol, fixture, validator, document = load_inputs(root)
    engine = ReplayEngine(protocol, fixture, validator, document)
    summaries, records, snapshot_checks = [], {}, []
    for scenario in protocol["scenarios"]:
        for budget in protocol["budgets"]:
            configs = [("A", None), ("B", None)] + [
                ("C", k) for k in protocol["stagnation_thresholds"]]
            for strategy, k in configs:
                run_id = f"{scenario}_L{budget}_{strategy}" + (f"_k{k}" if k is not None else "")
                state = engine.initial_state(scenario, strategy, budget, k)
                main_dir = run_dir / "conditions" / run_id / "main"
                main_events = execute_streamed(engine, state, main_dir)
                frozen_summary = main_summary(run_id, state, engine)
                write_json(main_dir / "summary.json", frozen_summary)
                # These files and the in-memory state are sealed before deepcopy.
                sealed_paths = [main_dir / name for name in ("events.jsonl", "final_state.json", "summary.json")]
                sealed_hashes = {relative(p, run_dir): sha256_file(p) for p in sealed_paths}
                main_state_before = canonical(state)
                write_json(main_dir / "sealed_manifest.json", sealed_hashes)
                shadow_events, shadow_state = [], None
                row = {column: "" for column in EXPECTED_COLUMNS + EXTRA_COLUMNS}
                row.update({column: frozen_summary[column] for column in (
                    "run_id", "scenario", "strategy", "budget", "k", "completed_pages", "calls",
                    "stop_reason", "no_progress_calls", "repeated_calls", "old_page_returns",
                    "network_failures", "protected_network_failures")})
                row["main_trace_path"] = relative(main_dir / "events.jsonl", run_dir)
                if strategy == "C" and frozen_summary["stop_reason"] == "stagnation":
                    shadow_state = copy.deepcopy(state)
                    shadow_state["guard_enabled"] = False
                    shadow_dir = main_dir.parent / "shadow"
                    shadow_events = execute_streamed(engine, shadow_state, shadow_dir)
                    extra = shadow_state["calls"] - frozen_summary["calls"]
                    lost_pages = len(shadow_state["accepted"]) - frozen_summary["completed_pages"]
                    shadow_summary = {
                        "run_id": run_id, "phase": "shadow", "shadow_policy": "B",
                        "original_budget": budget, "start_calls": frozen_summary["calls"],
                        "start_pages": frozen_summary["completed_pages"], "extra_calls": extra,
                        "final_pages": len(shadow_state["accepted"]), "total_calls": shadow_state["calls"],
                        "lost_pages": lost_pages, "false_stop": len(shadow_state["accepted"]) == len(engine.pages),
                        "stop_reason": engine.stop_info(shadow_state)["reason"],
                        "stop_triggers": engine.stop_info(shadow_state)["triggers"],
                    }
                    write_json(shadow_dir / "summary.json", shadow_summary)
                    row.update({"shadow_extra_calls": extra,
                                "shadow_final_pages": shadow_summary["final_pages"],
                                "shadow_total_calls": shadow_state["calls"], "lost_pages": lost_pages,
                                "false_stop": shadow_summary["false_stop"],
                                "shadow_trace_path": relative(shadow_dir / "events.jsonl", run_dir)})
                unchanged = (canonical(state) == main_state_before and all(
                    sha256_file(run_dir / rel) == sha for rel, sha in sealed_hashes.items()))
                check = {"run_id": run_id, "shadow_executed": shadow_state is not None,
                         "passed": unchanged, "sealed_files": sealed_hashes}
                snapshot_checks.append(check)
                write_json(main_dir.parent / "isolation_check.json", check)
                if not unchanged:
                    raise AssertionError(f"Main snapshot changed during shadow: {run_id}")
                records[run_id] = {"main_events": main_events, "main_state": state,
                                   "shadow_events": shadow_events, "shadow_state": shadow_state}
                summaries.append(row)
    write_csv(run_dir / "summary.csv", EXPECTED_COLUMNS + EXTRA_COLUMNS,
              [{k: cell(v) for k, v in r.items()} for r in summaries])
    differences = compare_expected(root, run_dir, summaries, manifest["manual_expected_rows"])
    audit = audit_runs(engine, records, snapshot_checks)
    write_json(run_dir / "audit.json", audit)
    return differences, audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="Experiment directory containing inputs and the freeze manifest")
    args = parser.parse_args()
    root = args.root.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = root / "runs" / f"{stamp}_{uuid.uuid4().hex[:12]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "run_meta.json", {
        "started_at_utc": utc_now(), "run_directory": str(run_dir), "root": str(root),
        "python": sys.version, "executable": sys.executable, "platform": platform.platform(),
        "argv": sys.argv, "experiment_kind": "controlled_deterministic_fault_replay",
        "expected_answers_used_for_policy": False,
    })
    try:
        manifest = validate_freeze(root, run_dir)
        source_manifest = snapshot_sources(root, run_dir)
        differences, audit = run_grid(root, run_dir, manifest)
        source_changes = [e["path"] for e in source_manifest
                          if sha256_file(root / e["path"]) != e["sha256"]]
        input_changes = [e["path"] for e in manifest["input_files"]
                         if sha256_file(root / e["path"]) != e["sha256"]]
        passed = not differences and audit["passed"] and not source_changes and not input_changes
        result = {"finished_at_utc": utc_now(), "status": "passed" if passed else "failed",
                  "main_conditions": manifest["manual_expected_rows"],
                  "comparison_differences": len(differences), "audit_passed": audit["passed"],
                  "source_changes_during_run": source_changes, "input_changes_during_run": input_changes,
                  "run_directory": str(run_dir)}
        write_json(run_dir / "run_outcome.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if passed else 1
    except Exception as error:
        detail = {"finished_at_utc": utc_now(), "status": "error", "type": type(error).__name__,
                  "message": str(error), "traceback": traceback.format_exc(),
                  "run_directory": str(run_dir), "evidence_preserved": True}
        write_json(run_dir / "error.json", detail)
        if not (run_dir / "run_outcome.json").exists():
            write_json(run_dir / "run_outcome.json", detail)
        print(json.dumps({"status": "error", "message": str(error), "run_directory": str(run_dir)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
