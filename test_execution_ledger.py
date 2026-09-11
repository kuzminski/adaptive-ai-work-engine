"""AAW V0.4B execution lifecycle ledger tests.

Covers the ledger primitives, the four crash windows, the at-most-once dispatch
boundary, and the integration invariants that make the ledger trustworthy:
INTENT is durable before dispatch, STARTED is an observation, and CLOSED is
never inferred from workflow state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import execution_ledger as L
import process_observation
from execution_contract import ExecutionIdentityError, allocate_execution


EXE_A = "EXE_" + "a" * 32
EXE_B = "EXE_" + "b" * 32
EXE_C = "EXE_" + "c" * 32


def ledger(tmp_path: Path, run_id: str = "RUN_1") -> L.ExecutionLedger:
    return L.ExecutionLedger.for_run(run_id, tmp_path)


def start_receipt(**overrides):
    receipt = {"start_evidence": "CHILD_PROCESS_SPAWNED", "observed_start_time": L.now(),
               "process_id": 4242, "process_creation_time": None}
    receipt.update(overrides)
    return receipt


def intent(led: L.ExecutionLedger, execution_id: str = EXE_A, **kwargs):
    params = {"node_id": "N01", "invocation_kind": "LLM"}
    params.update(kwargs)
    return led.record_execution_intent(execution_id=execution_id, **params)


# ---------------------------------------------------------------------------
# Envelope, identity, sequencing, durability
# ---------------------------------------------------------------------------

def test_event_ids_are_unique_and_not_timestamp_derived():
    ids = {L.new_event_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(value.startswith("EVT_") and len(value) == 36 for value in ids)


def test_sequence_is_monotonic_across_events_and_writer_instances(tmp_path):
    first = ledger(tmp_path)
    intent(first)
    first.record_execution_started(execution_id=EXE_A, receipt=start_receipt())
    # A second writer instance must continue the sequence, not restart it.
    second = L.ExecutionLedger(first.path, "RUN_1")
    third = second.record_execution_closed(
        execution_id=EXE_A, close_reason="COMPLETED", effect_certainty="CONFIRMED",
        observation_source="CHILD_PROCESS_EXIT", exit_code=0)
    sequences = [row["sequence"] for row in second.read().events]
    assert sequences == [1, 2, 3] and third["sequence"] == 3


def test_append_is_durable_and_does_not_rewrite_the_whole_file(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    first_bytes = led.path.read_bytes()
    led.record_execution_started(execution_id=EXE_A, receipt=start_receipt())
    second_bytes = led.path.read_bytes()
    # Earlier bytes are untouched; the file only grew.
    assert second_bytes.startswith(first_bytes)
    assert len(second_bytes) > len(first_bytes)
    # Every reported append is already on disk, not only in Python buffers.
    reread = json.loads(led.path.read_text(encoding="utf-8").splitlines()[-1])
    assert reread["event_type"] == L.EXECUTION_STARTED


def test_identical_duplicate_event_id_is_idempotent_and_incompatible_one_is_data_integrity_error(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    fixed = L.new_event_id()
    payload = {"start_evidence": "CHILD_PROCESS_SPAWNED", "observed_start_time": "2026-09-06T20:00:00+02:00",
               "process_id": 7}
    first = led.append(L.EXECUTION_STARTED, payload, execution_id=EXE_A, event_id=fixed)
    again = led.append(L.EXECUTION_STARTED, payload, execution_id=EXE_A, event_id=fixed)
    assert again["sequence"] == first["sequence"]
    assert len(led.read().by_type(L.EXECUTION_STARTED)) == 1, "one semantic observation only"

    with pytest.raises(L.LedgerIntegrityError) as caught:
        led.append(L.EXECUTION_STARTED, {**payload, "process_id": 99}, execution_id=EXE_A, event_id=fixed)
    assert caught.value.classification == "DATA_INTEGRITY_ERROR"
    # No last-write-wins: the original observation survives unchanged.
    assert led.read().by_type(L.EXECUTION_STARTED)[0]["payload"]["process_id"] == 7


def test_unknown_event_type_and_malformed_payload_are_refused(tmp_path):
    led = ledger(tmp_path)
    with pytest.raises(L.LedgerError):
        led.append("NODE_STARTED", {}, execution_id=EXE_A)
    with pytest.raises(L.LedgerError):
        led.record_execution_closed(execution_id=EXE_A, close_reason="DONE",
                                    effect_certainty="CONFIRMED", observation_source="CHILD_PROCESS_EXIT")


# ---------------------------------------------------------------------------
# Crash window D: damaged tail
# ---------------------------------------------------------------------------

def test_partial_final_line_retains_previous_events_and_is_classified(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    led.record_execution_started(execution_id=EXE_A, receipt=start_receipt())
    with open(led.path, "ab") as handle:
        handle.write(b'{"schema_version":"AAW_EXECUTION_LEDGER_V0.4B","event_id":"EVT_abc')

    read = L._scan(led.path)
    assert len(read.events) == 2, "valid preceding events are retained"
    assert read.damaged_tail is not None
    assert read.damaged_tail["kind"] == "LEDGER_DAMAGED_TAIL"
    report = L.validate_ledger(led.path, "RUN_1")
    # A damaged tail is a diagnostic about the tail, not a reason to discard.
    assert any(row["kind"] == "LEDGER_DAMAGED_TAIL" for row in report["warnings"])


def test_reading_a_damaged_ledger_never_rewrites_it(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    with open(led.path, "ab") as handle:
        handle.write(b'{"partial')
    before = led.path.read_bytes()
    L._scan(led.path)
    L.validate_ledger(led.path, "RUN_1")
    led.lifecycle()
    assert led.path.read_bytes() == before


def test_append_after_damaged_tail_seals_it_without_destroying_it(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    with open(led.path, "ab") as handle:
        handle.write(b'{"partial')
    led.record_execution_started(execution_id=EXE_A, receipt=start_receipt())
    text = led.path.read_text(encoding="utf-8")
    assert '{"partial' in text, "damaged bytes are retained verbatim"
    read = L._scan(led.path)
    assert len(read.events) == 2
    assert any(row["kind"] == "LEDGER_DAMAGED_RECORD" for row in read.diagnostics)
    assert [row["sequence"] for row in read.events] == [1, 2]


# ---------------------------------------------------------------------------
# Crash windows A/B/C: lifecycle shapes
# ---------------------------------------------------------------------------

def test_intent_only_lifecycle_means_start_is_unknown(tmp_path):
    """Window A: descriptor and intent persisted, the process never starts."""
    led = ledger(tmp_path)
    intent(led)
    assert led.intent_without_started() == [EXE_A]
    assert led.started_without_closed() == []
    assert led.lifecycle()[EXE_A]["state"] == "INTENT_ONLY"
    report = L.validate_ledger(led.path, "RUN_1")
    assert report["valid"], "an unstarted execution is unresolved evidence, not corruption"
    assert report["unresolved"]["intent_without_started"] == [EXE_A]


def test_started_without_closed_is_valid_unresolved_evidence(tmp_path):
    """Window B: the process started, the runner crashed before any result."""
    led = ledger(tmp_path)
    intent(led)
    led.record_execution_started(execution_id=EXE_A, receipt=start_receipt())
    assert led.started_without_closed() == [EXE_A]
    report = L.validate_ledger(led.path, "RUN_1")
    assert report["valid"] and report["unresolved"]["started_without_closed"] == [EXE_A]


def test_close_is_never_synthesised_and_reconciliation_is_explicit(tmp_path):
    """Window C: a terminal effect is proven later, the start observation was lost."""
    led = ledger(tmp_path)
    intent(led)
    assert led.close_status(EXE_A) is None, "no close is invented from intent"
    led.record_execution_closed(
        execution_id=EXE_A, close_reason="RECONCILED", effect_certainty="PARTIAL",
        observation_source="RECONCILIATION", detail="commit evidence proves a terminal effect")
    assert L.validate_ledger(led.path, "RUN_1")["valid"]
    assert led.lifecycle()[EXE_A]["started"] == [], "STARTED is never back-filled to tidy the sequence"
    assert led.close_status(EXE_A)["close_reason"] == "RECONCILED"


def test_close_without_start_from_a_process_adapter_is_a_validation_error(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    led.record_execution_closed(execution_id=EXE_A, close_reason="COMPLETED", effect_certainty="CONFIRMED",
                                observation_source="CHILD_PROCESS_EXIT", exit_code=0)
    report = L.validate_ledger(led.path, "RUN_1")
    assert any(row["kind"] == "CLOSED_WITHOUT_STARTED_NOT_RECONCILED" for row in report["errors"])


def test_started_for_an_unknown_execution_is_a_validation_error(tmp_path):
    led = ledger(tmp_path)
    led.record_execution_started(execution_id=EXE_B, receipt=start_receipt())
    report = L.validate_ledger(led.path, "RUN_1")
    assert any(row["kind"] == "STARTED_WITHOUT_INTENT" for row in report["errors"])


def test_contradictory_terminal_events_are_detected(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    led.record_execution_started(execution_id=EXE_A, receipt=start_receipt())
    led.record_execution_closed(execution_id=EXE_A, close_reason="COMPLETED", effect_certainty="CONFIRMED",
                                observation_source="CHILD_PROCESS_EXIT", exit_code=0)
    led.record_execution_closed(execution_id=EXE_A, close_reason="TIMEOUT", effect_certainty="PARTIAL",
                                observation_source="CHILD_PROCESS_EXIT", exit_code=124)
    report = L.validate_ledger(led.path, "RUN_1")
    assert any(row["kind"] == "CONTRADICTORY_CLOSE" for row in report["errors"])


# ---------------------------------------------------------------------------
# At-most-once dispatch boundary and failure semantics
# ---------------------------------------------------------------------------

def test_second_intent_for_one_execution_id_is_refused(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    with pytest.raises(L.LedgerDispatchError) as caught:
        intent(led)
    assert caught.value.classification == "AT_MOST_ONCE_DISPATCH_VIOLATION"
    assert len(led.read().by_type(L.EXECUTION_INTENT)) == 1
    # A deliberate retry uses a new execution ID instead.
    intent(led, EXE_B)
    assert len(led.read().by_type(L.EXECUTION_INTENT)) == 2


def test_intent_write_failure_is_fail_closed_and_launches_nothing(tmp_path, monkeypatch):
    led = ledger(tmp_path)
    launched: list[str] = []

    def dispatch_after_intent() -> None:
        """Stand-in for a runner: intent first, then and only then a dispatch."""
        intent(led)
        launched.append("process")

    # Fail at the real write boundary so the OSError conversion is exercised.
    monkeypatch.setattr(L.os, "write", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(L.LedgerWriteError) as caught:
        dispatch_after_intent()
    assert caught.value.classification == "LEDGER_WRITE_ERROR"
    assert launched == [], "no dispatch happens without durable intent"
    monkeypatch.undo()
    assert led.read().events == []


def test_started_write_failure_reports_uncertainty_and_never_relaunches(tmp_path, monkeypatch):
    led = ledger(tmp_path)
    intent(led)
    recorder = L.LifecycleRecorder(led, EXE_A)
    monkeypatch.setattr(L.ExecutionLedger, "record_execution_started",
                        lambda *a, **k: (_ for _ in ()).throw(L.LedgerWriteError("boom")))
    recorder.observe_start(start_receipt())
    assert recorder.start_status == L.STARTED_LEDGER_UNCERTAIN
    assert recorder.status()["requires_reconciliation"] is True
    monkeypatch.undo()
    # The process is not forgotten: its close carries the uncertain start.
    recorder.close(close_reason="COMPLETED", effect_certainty="PARTIAL",
                   observation_source="CHILD_PROCESS_EXIT", exit_code=0)
    payload = led.close_status(EXE_A)
    assert payload["start_record_status"] == L.STARTED_LEDGER_UNCERTAIN


def test_close_write_failure_is_uncertain_and_triggers_no_provider_retry(tmp_path, monkeypatch):
    led = ledger(tmp_path)
    intent(led)
    recorder = L.LifecycleRecorder(led, EXE_A)
    recorder.observe_start(start_receipt())
    calls: list[int] = []
    monkeypatch.setattr(L.ExecutionLedger, "record_execution_closed",
                        lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(L.LedgerWriteError("boom")))[1])
    status = recorder.close(close_reason="COMPLETED", effect_certainty="CONFIRMED",
                            observation_source="CHILD_PROCESS_EXIT", exit_code=0)
    assert status == L.CLOSE_RECORD_UNCERTAIN
    assert len(calls) == 1, "a failed close record is never retried into a second invocation"
    assert recorder.status()["requires_reconciliation"] is True


# ---------------------------------------------------------------------------
# Observed start receipts
# ---------------------------------------------------------------------------

def test_process_receipt_carries_real_pid_from_a_live_child():
    process = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        receipt = process_observation.process_receipt(process, executable=sys.executable, adapter="TEST")
    finally:
        process.communicate()
    assert receipt["start_evidence"] == "CHILD_PROCESS_SPAWNED"
    assert receipt["process_id"] == process.pid
    # Creation time is recorded when the OS exposes it and stays null otherwise;
    # it is never substituted with the runner's own clock reading.
    assert receipt["process_creation_time"] != receipt["observed_start_time"]
    if receipt["process_creation_time"] is None:
        assert receipt["process_creation_time_source"] == "UNAVAILABLE_NO_PROCESS_OWNERSHIP_API"


def test_http_receipt_does_not_fabricate_a_pid_or_claim_remote_execution():
    receipt = process_observation.http_receipt(endpoint="http://127.0.0.1:11434",
                                               adapter="ollama_openai_compat", provider="LOCAL")
    assert receipt["process_id"] is None
    assert receipt["start_evidence"] == "HTTP_REQUEST_DISPATCH_INITIATED"
    assert "server-side inference began" in receipt["does_not_prove"]


def test_only_a_marked_dispatch_reports_a_start(tmp_path):
    """A helper spawn inside the scope must not be claimed as the invocation's start."""
    led = ledger(tmp_path)
    intent(led)
    recorder = L.LifecycleRecorder(led, EXE_A)
    helper = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    real = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with process_observation.observation_scope(recorder.observe_start):
            process_observation.notify_process_start(helper, adapter="git-helper")
            assert led.read().by_type(L.EXECUTION_STARTED) == [], "an unmarked spawn is not start evidence"
            process_observation.notify_process_start(real, dispatch=True, adapter="DIRECT_CLI_CONTROL")
    finally:
        helper.communicate()
        real.communicate()
    started = led.read().by_type(L.EXECUTION_STARTED)
    assert len(started) == 1
    assert started[0]["payload"]["process_id"] == real.pid
    assert started[0]["payload"]["process_id"] != helper.pid


def test_observation_scope_routes_receipts_and_first_observation_wins(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    recorder = L.LifecycleRecorder(led, EXE_A)
    with process_observation.observation_scope(recorder.observe_start):
        process_observation.notify(start_receipt(process_id=1))
        process_observation.notify(start_receipt(process_id=2))
    started = led.read().by_type(L.EXECUTION_STARTED)
    assert len(started) == 1 and started[0]["payload"]["process_id"] == 1
    # Outside the scope nothing is recorded.
    process_observation.notify(start_receipt(process_id=3))
    assert len(led.read().by_type(L.EXECUTION_STARTED)) == 1


# ---------------------------------------------------------------------------
# COMMIT_RECORDED and HUMAN_DECISION_RECORDED
# ---------------------------------------------------------------------------

def test_commit_recorded_is_repository_scoped_and_queryable(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    led.record_commit(repository="D:/repo-a", commit_hash="abc123", producer_execution_ids=[EXE_A],
                      expected_parent="parent1", subtask_id="S1", role="SUBTASK")
    # The same hash in another repository is a different commit identity.
    led.record_commit(repository="D:/repo-b", commit_hash="abc123", producer_execution_ids=[EXE_A],
                      expected_parent="parent9", subtask_id="S1", role="SUBTASK")
    assert L.validate_ledger(led.path, "RUN_1")["valid"]
    records = led.commits_for_execution(EXE_A)
    assert {row["repository"] for row in records} == {"D:/repo-a", "D:/repo-b"}
    assert records[0]["commit_identity"] == {"repository": "D:/repo-a", "commit_hash": "abc123"}


def test_incompatible_duplicate_commit_record_is_detected(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    led.record_commit(repository="D:/repo", commit_hash="abc", producer_execution_ids=[EXE_A], role="SUBTASK")
    led.record_commit(repository="D:/repo", commit_hash="abc", producer_execution_ids=[EXE_B], role="REPAIR")
    report = L.validate_ledger(led.path, "RUN_1")
    assert any(row["kind"] == "INCOMPATIBLE_DUPLICATE_COMMIT" for row in report["errors"])


def test_human_decision_references_the_artifact_and_keeps_accept_a_release_verdict(tmp_path):
    led = ledger(tmp_path)
    artifact = tmp_path / "HDE_1.json"
    artifact.write_text(json.dumps({"verdict": "ACCEPTED"}), encoding="utf-8")
    led.record_human_decision(human_decision_id="HDE_1", candidate_id="CAN_1", verdict="ACCEPTED",
                              decision_artifact_path=artifact)
    decision = led.human_decisions()[0]
    assert decision["decision_artifact_hash"] == L.file_hash(artifact)
    assert decision["quality_assessment"] is None
    assert "never a model-quality PASS" in decision["does_not_mean"]
    assert L.validate_ledger(led.path, "RUN_1")["valid"]


def test_malformed_human_decision_payload_is_reported(tmp_path):
    led = ledger(tmp_path)
    with pytest.raises(L.LedgerError):
        led.append(L.HUMAN_DECISION_RECORDED, {"candidate_id": "CAN_1"})


# ---------------------------------------------------------------------------
# Legacy and identity-contract boundaries
# ---------------------------------------------------------------------------

def test_a_run_without_a_ledger_is_legacy_not_corrupted(tmp_path):
    led = L.ExecutionLedger.for_run("AAW_LEGACY_RUN", tmp_path)
    read = led.read()
    assert read.exists is False and read.events == []
    assert led.unresolved() == [] and led.summary()["events"] == 0
    report = L.validate_ledger(led.path, "AAW_LEGACY_RUN")
    assert report["valid"] and report["exists"] is False


def test_no_backfill_is_performed_for_a_run_without_a_ledger(tmp_path):
    led = L.ExecutionLedger.for_run("AAW_LEGACY_RUN", tmp_path)
    led.read(); led.lifecycle(); led.summary()
    assert not led.path.exists(), "reading legacy evidence never creates synthetic events"


def test_v0_4a_execution_id_collision_still_fails_closed(tmp_path, monkeypatch):
    """The V0.4B ledger does not change V0.4A identity semantics."""
    monkeypatch.setattr("execution_contract.new_execution_id", lambda: EXE_C)
    allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N01", invocation_kind="LLM")
    with pytest.raises(ExecutionIdentityError) as caught:
        allocate_execution(descriptor_root=tmp_path, run_id="R", node_id="N02", invocation_kind="LLM")
    assert caught.value.classification == "DATA_INTEGRITY_ERROR"


def test_ledger_schema_version_is_explicit_and_separate_from_identity_contract(tmp_path):
    led = ledger(tmp_path)
    event = intent(led)
    assert event["schema_version"] == "AAW_EXECUTION_LEDGER_V0.4B"
    assert event["payload"]["identity_contract"] == "AAW_EXECUTION_DESCRIPTOR_V0.4A"
    report = L.validate_ledger(led.path, "RUN_1")
    assert report["schema_version"] == "AAW_EXECUTION_LEDGER_V0.4B"


def test_unknown_schema_version_is_reported_not_interpreted(tmp_path):
    led = ledger(tmp_path)
    intent(led)
    with open(led.path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema_version": "AAW_EXECUTION_LEDGER_V9", "event_id": L.new_event_id(),
                                 "event_type": "EXECUTION_INTENT", "run_id": "RUN_1", "sequence": 2,
                                 "recorded_at": L.now(), "execution_id": EXE_B, "payload": {}}) + "\n")
    report = L.validate_ledger(led.path, "RUN_1")
    assert any(row["kind"] == "UNKNOWN_SCHEMA" for row in report["errors"])


def test_recorded_at_is_diagnostic_only_and_sequence_carries_order(tmp_path):
    led = ledger(tmp_path)
    events = [intent(led, EXE_A), intent(led, EXE_B), intent(led, EXE_C)]
    assert [row["sequence"] for row in events] == [1, 2, 3]
    # Identity never derives from time: two events sharing a timestamp still
    # have distinct IDs and distinct sequences.
    assert len({row["event_id"] for row in events}) == 3
