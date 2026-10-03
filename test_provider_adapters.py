"""Provider adapter contract: Antigravity (EXPERIMENTAL, live NOT_TESTED), mocks, failure classification."""

import pytest

import autonomy_adapters as aa
import autonomy_controller as ctl
import model_router as mr
import provider_adapters as pa


def test_antigravity_without_a_cli_is_unavailable_and_not_tested():
    detected = pa.AntigravityAdapter(which=lambda name: None).detect()
    assert detected["status"] == "UNAVAILABLE" and detected["live_integration"] == "NOT_TESTED"
    assert detected["trust"] == "EXPERIMENTAL" and detected["verified_capabilities"] == []
    assert "code_edit" in detected["declared_capabilities"]


def test_antigravity_with_a_cli_present_is_still_not_runnable_until_live_verified():
    adapter = pa.AntigravityAdapter(which=lambda name: "/opt/agy" if name == "agy" else None)
    detected = adapter.detect()
    assert detected["status"] == "FOUND" and detected["executable"] == "/opt/agy"
    assert "NOT_TESTED" in adapter.preflight({"profile_id": "ANTIGRAVITY_FREE"})


def test_antigravity_never_dispatches_and_fails_over_cleanly():
    with pytest.raises(ctl.ExecutorFailure) as info:
        pa.AntigravityAdapter().invoke({}, {}, {})
    assert info.value.dispatched is False and info.value.failure_class == "UNAVAILABLE"


def test_the_shipped_antigravity_profile_is_unavailable_so_nothing_routes_to_it_by_accident():
    runtime, reason = aa.resolve_runtime({"profile_id": "ANTIGRAVITY_FREE"})
    assert runtime is None and "NOT_TESTED" in reason
    report = pa.status_report()["providers"]["antigravity"]
    assert report["live_integration"] == "NOT_TESTED"


def test_adapter_telemetry_defaults_to_unknown_and_never_invents_a_percent():
    snapshot = pa.AntigravityAdapter().quota_snapshot()
    view = mr.quota_view(snapshot, model_id=None, now=mr.parse_time("2026-10-03T12:00:00+00:00"),
                         policy=mr.normalize_policy())
    assert view["certainty"] == "UNKNOWN" and view["percent"] is None


def test_scripted_adapter_replays_outcomes_and_reports_verified_capabilities():
    adapter = pa.ScriptedProviderAdapter("mock", outcomes=[RuntimeError("boom"), {"summary": "ok"}])
    assert adapter.detect()["live_integration"] == "VERIFIED" and adapter.preflight({}) is None
    with pytest.raises(RuntimeError):
        adapter.invoke({}, {"profile_id": "P"}, {})
    assert adapter.invoke({}, {"profile_id": "P"}, {}) == {"summary": "ok"}
    assert len(adapter.calls) == 2


def test_registered_adapters_feed_the_router_telemetry():
    adapter = pa.ScriptedProviderAdapter("mockprov", telemetry={"limits": [
        {"window": "daily", "certainty": "EXACT", "remaining_percent": 7}]})
    pa.register_provider_adapter(adapter)
    try:
        assert pa.collect_telemetry()["pools"]["mockprov"]["limits"][0]["remaining_percent"] == 7
    finally:
        pa.unregister_provider_adapter("mockprov")


@pytest.mark.parametrize("rc,out,err,expected", [
    (124, "", "\nPROCESS_TIMEOUT", ("TIMEOUT", None)),
    (1, "", "Error: 429 Too Many Requests; retry-after: 90 seconds", ("RATE_LIMIT", 1.5)),
    (1, "", "You have hit your usage limit", ("RATE_LIMIT", None)),
    (1, "", "401 Unauthorized: please log in", ("AUTH", None)),
    (1, "", "not logged in", ("AUTH", None)),
    (1, "4010 files checked", "boom", (None, None)),
])
def test_provider_failures_are_classified_as_hard_signals(rc, out, err, expected):
    assert aa.classify_failure(rc, out, err) == expected
