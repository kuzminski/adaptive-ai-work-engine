import json
from pathlib import Path

import aaw_run_v0_1 as launcher


def test_direct_single_task_reserves_and_exposes_execution_id(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "STATS_ROOT", tmp_path / "03_STATS")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"C:/fixture/{name}.exe")
    stdout = "\n".join([
        json.dumps({"type":"thread.started","thread_id":"provider-session-same"}),
        json.dumps({"type":"turn.completed","usage":{"input_tokens":3,"cached_input_tokens":0,"output_tokens":2,"reasoning_output_tokens":1}}),
    ])
    monkeypatch.setattr(launcher, "run_cmd", lambda *args, **kwargs: (0, stdout, ""))
    monkeypatch.setattr(launcher, "worker_prompt", lambda *args, **kwargs: "frozen prompt")
    ctx = launcher.make_ctx("identity fixture")
    binding = {"provider":"OPENAI","harness":"codex","runtime_model_id":"gpt-fixture","effort":"high","profile":"FIXTURE","binding_source":"TEST"}
    receipt = launcher.launch_direct("identity fixture", ctx, {"pipeline":"P04"}, {"role":"CODE_IMPLEMENTER"}, binding, tmp_path, "N01")
    execution_id = receipt["execution_id"]
    assert execution_id.startswith("EXE_")
    assert receipt["provider_session_id"] == "provider-session-same"
    telemetry = json.loads(Path(receipt["telemetry_path"]).read_text(encoding="utf-8"))
    descriptor = json.loads((tmp_path / "03_STATS" / ctx.run_id / "EXECUTIONS" / f"{execution_id}.json").read_text(encoding="utf-8"))
    assert telemetry["execution_id"] == execution_id
    assert descriptor["execution_id"] == execution_id
    assert descriptor["provider_session_id"] == "provider-session-same"
    assert descriptor["provider_session_id"] != descriptor["execution_id"]
