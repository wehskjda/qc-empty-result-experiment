"""Controlled, deterministic fault replay; no real parser, OCR, network or LLM."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def normalize_text(text: str) -> str:
    """Only normalize NFC and whitespace; retain numbers and punctuation."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def levenshtein(a: str, b: str) -> int:
    """Unit-cost character edits, with O(min(len(a), len(b))) memory."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


class PageValidator:
    """Read reference answers separately from replayed tool responses."""

    def __init__(self, references: dict[str, str], key_config: dict,
                 protocol: dict):
        self.references = {p: normalize_text(t) for p, t in references.items()}
        self.key_config = copy.deepcopy(key_config)
        validation = protocol["validation"]
        self.limit_num = validation["cer_limit_numerator"]
        self.limit_den = validation["cer_limit_denominator"]
        if self.limit_den <= 0 or any(not t for t in self.references.values()):
            raise ValueError("Nonempty reference pages and positive CER denominator required")
        number = r"([+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))"
        self.patterns = {}
        for key, definition in self.key_config["fields"].items():
            # Delimiters prevent accepting '31.8x' or an incomplete numeric prefix.
            self.patterns[key] = re.compile(
                re.escape(definition["prefix"]) + r"\s*" + number + r"\s*"
                + re.escape(definition["suffix"]) + r"(?=$|[，。；;、\s])"
            )

    def validate(self, page_id: str, response: dict) -> dict:
        reference = self.references[page_id]
        raw_text = response.get("text", "")
        text_is_string = isinstance(raw_text, str)
        text = normalize_text(raw_text) if text_is_string else ""
        distance = levenshtein(reference, text)
        cer_ok = distance * self.limit_den <= len(reference) * self.limit_num
        extracted = {name: pattern.findall(text)
                     for name, pattern in self.patterns.items()}
        occurrences = {name: text.count(definition["prefix"])
                       for name, definition in self.key_config["fields"].items()}
        checks = {}
        for name, values in extracted.items():
            expected = self.key_config["pages"][page_id][name]
            try:
                checks[name] = (occurrences[name] == 1 and len(values) == 1
                                and Decimal(values[0]) == Decimal(expected))
            except InvalidOperation:
                checks[name] = False
        reasons = []
        if not text_is_string:
            reasons.append("non_string_text")
        if not cer_ok:
            reasons.append("cer_exceeds_limit")
        reasons.extend(f"key_number_invalid:{name}" for name, ok in checks.items() if not ok)
        return {
            "accepted": text_is_string and cer_ok and all(checks.values()),
            "edit_distance": distance,
            "reference_length": len(reference),
            "cer": distance / len(reference),
            "cer_limit": self.limit_num / self.limit_den,
            "cer_ok": cer_ok,
            "extracted_numbers": extracted,
            "field_occurrences": occurrences,
            "expected_numbers": copy.deepcopy(self.key_config["pages"][page_id]),
            "number_checks": checks,
            "reasons": reasons,
            "normalized_text": text,
        }


class FixtureStore:
    """Lookup only: responses were expanded and frozen before this code existed."""

    def __init__(self, inputs_dir: Path):
        self.records = {}
        with (inputs_dir / "fault_matrix.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                record = json.loads(line)
                key = (record["scenario"], record["page_id"], record["method"],
                       record["attempt_n"])
                if key in self.records:
                    raise ValueError(f"Duplicate frozen fault key: {key}")
                response = record["response"]
                if type(response["http_status"]) is not int or not isinstance(response["text"], str):
                    raise ValueError(f"Invalid response types at {key}")
                self.records[key] = record

    def get(self, scenario: str, page_id: str, method: str, attempt_n: int) -> dict:
        record = self.records[(scenario, page_id, method, attempt_n)]
        return copy.deepcopy({"response_id": record["response_id"],
                              "response": record["response"]})


class ReplayEngine:
    def __init__(self, protocol: dict, fixture: FixtureStore,
                 validator: PageValidator, document_manifest: dict):
        self.protocol = copy.deepcopy(protocol)
        self.fixture = fixture
        self.validator = validator
        self.document = copy.deepcopy(document_manifest)
        self.pages = list(protocol["page_order"])
        self.source_hashes = {p["page_id"]: p["sha256"]
                              for p in document_manifest["pages"]}
        self.network_statuses = frozenset(protocol["protected_http_statuses"])

    def initial_state(self, scenario: str, strategy: str, budget: int,
                      k: int | None = None) -> dict:
        if strategy not in ("A", "B", "C") or budget <= 0:
            raise ValueError("Invalid strategy or budget")
        if strategy == "C" and (k is None or k <= 0):
            raise ValueError("C requires a positive stagnation threshold")
        if strategy != "C" and k is not None:
            raise ValueError("A/B have no stagnation threshold")
        return {
            "scenario": scenario, "strategy": strategy, "budget": budget, "k": k,
            "guard_enabled": strategy == "C", "calls": 0,
            "queue": list(self.pages),
            "methods": {p: self.protocol["initial_method"] for p in self.pages},
            "attempts": {p: {m: 0 for m in self.protocol["methods"]} for p in self.pages},
            "closed_methods": {p: [] for p in self.pages},
            "accepted": {}, "S": 0,
            "E": self.protocol["network_protection_per_document"],
            "history": {}, "repair_history": {p: [] for p in self.pages},
            "no_progress_calls": 0, "repeated_calls": 0,
            "old_page_returns": 0, "network_failures": 0,
            "protected_network_failures": 0, "stop_reason": None,
        }

    def stop_info(self, state: dict) -> dict:
        complete = len(state["accepted"]) == len(self.pages)
        triggers = {
            "complete": complete,
            "budget_exhausted": state["calls"] >= state["budget"],
            "methods_exhausted": not state["queue"] and not complete,
            "stagnation": bool(state["guard_enabled"] and not complete
                               and state["k"] is not None and state["S"] >= state["k"]),
        }
        reason = next((r for r in self.protocol["stopping_priority"] if triggers[r]), None)
        return {"reason": reason, "triggers": triggers}

    def _route_pending(self, state: dict, page_id: str, method: str,
                       response: dict, attempt_n: int) -> None:
        if state["strategy"] == "A":
            state["queue"].append(page_id)
            return
        limit_reached = attempt_n >= self.protocol["max_attempts_per_page_method"]
        needs_ocr = (response.get("http_status") == self.protocol["ocr_route_http_status"]
                     and response.get("code") == self.protocol["ocr_route_code"])
        if method == "T" and (limit_reached or needs_ocr):
            if "T" not in state["closed_methods"][page_id]:
                state["closed_methods"][page_id].append("T")
            state["methods"][page_id] = "O"
            state["queue"].append(page_id)
        elif method == "O" and limit_reached:
            if "O" not in state["closed_methods"][page_id]:
                state["closed_methods"][page_id].append("O")
            state["methods"][page_id] = None
        else:
            state["queue"].append(page_id)

    def step(self, state: dict) -> dict:
        if self.stop_info(state)["reason"] is not None:
            raise RuntimeError("Cannot execute a stopped state")
        page_id = state["queue"].pop(0)
        method = state["methods"][page_id]
        if state["strategy"] != "A":
            if method in state["closed_methods"][page_id]:
                raise AssertionError("Closed method was scheduled")
            if state["attempts"][page_id][method] >= self.protocol["max_attempts_per_page_method"]:
                raise AssertionError("Method attempt limit exceeded")
        s_before, e_before = state["S"], state["E"]
        state["calls"] += 1
        state["attempts"][page_id][method] += 1
        n = state["attempts"][page_id][method]
        record = self.fixture.get(state["scenario"], page_id, method, n)
        response = record["response"]
        validation = self.validator.validate(page_id, response)
        text = validation["normalized_text"]
        # Target identity comes from the request, never from claimed_page.
        new_progress = bool(validation["accepted"] and page_id not in state["accepted"])
        old_page_return = bool(not new_progress and text and any(
            value["normalized_text"] == text for value in state["accepted"].values()))
        # No alias, free-form message, claimed page or attempt counter in this fingerprint.
        # The external source input is constant; R/L repair candidates differ in real text.
        fingerprint = digest({
            "page_id": page_id, "source_input_sha256": self.source_hashes[page_id],
            "method": method, "http_status": response.get("http_status"),
            "code": response.get("code"), "normalized_text": text,
        })
        repeated = not new_progress and fingerprint in state["history"]
        prior_identical_call = state["history"].get(fingerprint)
        state["history"].setdefault(fingerprint, state["calls"])
        state["repeated_calls"] += int(repeated)
        state["old_page_returns"] += int(old_page_return)
        state["no_progress_calls"] += int(not new_progress)
        is_network = response.get("http_status") in self.network_statuses
        state["network_failures"] += int(is_network)
        protected = False
        if state["strategy"] == "C":
            if new_progress:
                state["S"] = 0
            elif is_network and state["E"] > 0:
                state["E"] -= 1
                protected = True
            else:
                state["S"] += 1
        state["protected_network_failures"] += int(protected)
        state["repair_history"][page_id].append({
            "global_call": state["calls"], "method": method, "attempt_n": n,
            "response_id": record["response_id"],
            "response_sha256": digest(response), "text_sha256": digest(text),
        })
        if new_progress:
            state["accepted"][page_id] = {
                "source_page_key": f'{self.document["document_sha256"]}:{page_id}',
                "text": response["text"], "normalized_text": text,
                "first_call": state["calls"], "method": method, "attempt_n": n,
                "validation": copy.deepcopy(validation),
            }
        if page_id not in state["accepted"]:
            self._route_pending(state, page_id, method, response, n)
        event = {
            "global_call": state["calls"], "scenario": state["scenario"],
            "document_id": self.document["document_id"],
            "source_page_key": f'{self.document["document_sha256"]}:{page_id}',
            "page_id": page_id, "method": method, "attempt_n": n,
            "response_id": record["response_id"], "raw_response": copy.deepcopy(response),
            "validation": validation, "new_progress": new_progress,
            "S_before": s_before, "S_after": state["S"],
            "E_before": e_before, "E_after": state["E"],
            "network_failure": is_network, "network_protected": protected,
            "repeated_no_progress": repeated, "repeat_of_call": prior_identical_call,
            "old_page_return": old_page_return,
            "input_state_hash": self.source_hashes[page_id],
            "result_fingerprint": fingerprint,
            "post_state": {
                "queue": copy.deepcopy(state["queue"]),
                "methods": copy.deepcopy(state["methods"]),
                "attempts": copy.deepcopy(state["attempts"]),
                "closed_methods": copy.deepcopy(state["closed_methods"]),
                "accepted_pages": [p for p in self.pages if p in state["accepted"]],
                "calls": state["calls"], "S": state["S"], "E": state["E"],
            },
        }
        event["stop_after_call"] = self.stop_info(state)
        return event

    def run(self, state: dict) -> list[dict]:
        events = []
        while self.stop_info(state)["reason"] is None:
            events.append(self.step(state))
        state["stop_reason"] = self.stop_info(state)["reason"]
        return events


def load_inputs(root: Path) -> tuple[dict, FixtureStore, PageValidator, dict]:
    inputs_dir = root / "inputs"
    protocol = json.loads((inputs_dir / "protocol.json").read_text(encoding="utf-8"))
    keys = json.loads((inputs_dir / "key_numbers.json").read_text(encoding="utf-8"))
    document = json.loads((inputs_dir / "document_manifest.json").read_text(encoding="utf-8"))
    references = {p: (inputs_dir / "reference_pages" / f"{p}.txt").read_text(encoding="utf-8")
                  for p in protocol["page_order"]}
    return protocol, FixtureStore(inputs_dir), PageValidator(references, keys, protocol), document
