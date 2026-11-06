"""Adaptive evidence loop: Gate decision = label (1.1); honest SHADOW evaluation + progress (1.2); never unlocks."""
import json

import pytest

import adaptive_evidence as aev
import adaptive_policy as ap
import aaw_telemetry as tel
import autonomy_contract as ac
import autonomy_controller as ctl
from test_autonomy import Harness, mandate_fixture, plan
from test_autonomy import held_run


def test_approve_writes_one_accept_label_with_hashes_and_is_idempotent(tmp_path):
    h, cand = held_run(tmp_path)
    ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    rows = aev.read_runs(h.stats)
    assert len(rows) == 1
    r = rows[0]
    assert r["schema"] == aev.SCHEMA and r["decision"] == "ACCEPT" and r["run_id"] == "RUN1"
    assert r["candidate_id"] == cand and r["mandate_hash"] and r["telemetry_sha256"].startswith("sha256:")
    assert r["calls"] > 0 and r["approver"] == "kamil"
    # scripted executors report no usage -> never counted as a real verified run
    assert r["provenance"] == aev.NO_USAGE_EVIDENCE
    state = ctl.load_state("RUN1", h.stats)
    aev.record_gate_decision("RUN1", state, tel.read_records(ctl.autonomy_dir("RUN1", h.stats) / "telemetry.jsonl"), h.stats)
    assert len(aev.read_runs(h.stats)) == 1                         # same decision_id -> no duplicate


def _measured(eid, cost=1.0):
    return {"execution_id": eid, "tokens": {"input_total": 10, "output": 1}, "cost_usd": cost, "phase": "EXECUTE"}


def test_fingerprint_covers_record_contents_not_just_ids():
    state = {"human": {"verdict": "APPROVED", "approval_id": "A1"}, "executions": [{"execution_id": "E1"}]}
    a = aev.build_record(run_id="R", state=state, records=[_measured("E1", 1.0)])
    b = aev.build_record(run_id="R", state=state, records=[_measured("E1", 999.0)])
    assert a["telemetry_sha256"] != b["telemetry_sha256"]


def test_run_with_missing_telemetry_is_never_real_measured():
    state = {"human": {"verdict": "APPROVED", "approval_id": "A1"},
             "executions": [{"execution_id": "E1"}, {"execution_id": "E2"}]}
    partial = aev.build_record(run_id="R", state=state, records=[_measured("E1")])
    assert partial["provenance"] == aev.NO_USAGE_EVIDENCE and partial["missing_telemetry_executions"] == ["E2"]
    full = aev.build_record(run_id="R", state=state, records=[_measured("E1"), _measured("E2")])
    assert full["provenance"] == aev.REAL_MEASURED and full["missing_telemetry_executions"] == []


def test_reject_writes_a_reject_label(tmp_path):
    h, _ = held_run(tmp_path)
    ctl.reject("RUN1", approver="kamil", reason="not what I meant", stats_root=h.stats)
    rows = aev.read_runs(h.stats)
    assert [r["decision"] for r in rows] == ["REJECT"]


def test_recording_never_raises_into_the_human_surface(tmp_path, monkeypatch):
    h, cand = held_run(tmp_path)
    monkeypatch.setattr(aev, "_runs_path", lambda root: (_ for _ in ()).throw(OSError("disk")))
    state = ctl.approve_promotion("RUN1", approver="kamil", candidate_id=cand, stats_root=h.stats)
    assert state["status"] == ac.HUMAN_APPROVED                       # the approval stands; the label is lost, not fatal


def _row(decision, *, real=True, diverged=False, observed=True):
    return {"schema": aev.SCHEMA, "run_id": "R", "decision": decision,
            "provenance": aev.REAL_MEASURED if real else aev.NO_USAGE_EVIDENCE,
            "shadow": {"observed": 3 if observed else 0, "diverged": diverged}}


def test_wilson_interval_is_sane():
    assert aev.wilson(0, 0) == {"n": 0, "rate": None, "low": None, "high": None}
    w = aev.wilson(8, 10)
    assert w["rate"] == 0.8 and 0.4 < w["low"] < 0.8 < w["high"] < 1.0


def test_evaluate_counts_only_real_runs_and_reports_progress():
    rows = [_row("ACCEPT")] * 6 + [_row("REJECT")] * 2 + [_row("ACCEPT", real=False)] * 5
    rep = aev.evaluate(rows=rows)
    assert rep["runs_total"] == 13 and rep["runs_real_measured"] == 8 and rep["runs_excluded_no_usage"] == 5
    assert rep["progress"] == {"have": 8, "need": 100, "fraction": 0.08}
    assert rep["accept"]["n"] == 8 and rep["accept"]["rate"] == 0.75
    assert rep["stage4_entry_met"] is False


def test_evaluate_splits_diverged_vs_aligned_and_cannot_unlock_even_at_100():
    rows = [_row("ACCEPT", diverged=True)] * 45 + [_row("REJECT", diverged=True)] * 5 \
        + [_row("ACCEPT")] * 45 + [_row("REJECT")] * 5
    rep = aev.evaluate(rows=rows)
    assert rep["runs_real_measured"] == 100
    assert rep["shadow"]["divergence_rate"]["rate"] == 0.5
    assert rep["shadow"]["accept_rate_when_diverged"]["rate"] == 0.9
    assert all(g["passed"] for g in rep["stage4_entry_gates"]) and rep["stage4_entry_met"] is True
    assert rep["unlock_allowed"] is False                              # evidence is necessary, never sufficient


def test_clearly_worse_baseline_where_policy_diverges_fails_the_gate():
    rows = [_row("ACCEPT", diverged=True)] * 5 + [_row("REJECT", diverged=True)] * 45 \
        + [_row("ACCEPT")] * 45 + [_row("REJECT")] * 5
    gate = next(g for g in aev.evaluate(rows=rows)["stage4_entry_gates"]
                if g["gate"] == "divergence_not_worse_than_aligned")
    assert gate["passed"] is False


def test_labels_do_not_unlock_controlled_adaptive(tmp_path, monkeypatch):
    monkeypatch.setenv("AAW_ADAPTIVE_MODE", "CONTROLLED_ADAPTIVE")
    with (tmp_path / aev.RUNS_FILE).open("w", encoding="utf-8") as fh:
        for r in [_row("ACCEPT")] * 120:
            fh.write(json.dumps(r) + "\n")
    assert ap.resolve_effective_mode(stats_root=tmp_path) == ap.MODE_SHADOW
    assert aev.evaluate(tmp_path)["unlock_allowed"] is False
