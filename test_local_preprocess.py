"""Bounded stdlib tests for AAW local preprocessing policy and retention."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_preprocess as p


class PreprocessTests(unittest.TestCase):
    def run_prep(self, node: str, package: dict, **kwargs: object) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(p.local_llm, "precheck_soft", return_value={"ok": True, "state": "AVAILABLE"}), patch.object(p.local_llm, "chat_json", return_value={"json": {"goal": "x", "uncertainty": []}, "content": '{"goal":"x","uncertainty":[]}', "usage": {"input_tokens": 4, "output_tokens": 2}}):
                result = p.preprocess_for_node(run_id="RUN", node_id="N01", node_type=node, package=package, artifact_root=root, **kwargs)
                result["saved"] = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
                return result

    def test_short_task_and_structured_handoff_skip(self) -> None:
        short = self.run_prep("PLAN", {"GOAL": "clear short task"})
        one = self.run_prep("REVIEW", {"SUBTASK_RESULTS": [{"outcome": "PASS"}], "GIT_DIFF_BASELINE_TO_HEAD": "small"})
        self.assertEqual(short["status"], "SKIPPED_NOT_USEFUL")
        self.assertEqual(one["status"], "SKIPPED_NOT_USEFUL")

    def test_long_task_summary_diff_and_log_run(self) -> None:
        self.assertEqual(self.run_prep("PLAN", {"GOAL": "messy " * 900})["preprocess_type"], "LOCAL_QWEN_TASK_NORMALIZE")
        self.assertEqual(self.run_prep("REVIEW", {"SUBTASK_RESULTS": [{}, {}, {}]})["preprocess_type"], "LOCAL_QWEN_SUMMARY")
        self.assertEqual(self.run_prep("REVIEW", {"COMMIT_LIST": ["a", "b"], "GIT_DIFF": "x" * 17000})["preprocess_type"], "LOCAL_QWEN_DIFF_TRIAGE")
        self.assertEqual(self.run_prep("REPAIR", {"RAW_LOG": "Traceback\n" * 2000})["preprocess_type"], "LOCAL_QWEN_LOG_TRIAGE")

    def test_findings_delta_source_retention_and_compact_package(self) -> None:
        result = self.run_prep("REPAIR", {"OPEN_ISSUES": [{"finding_id": str(i)} for i in range(6)]})
        self.assertEqual(result["preprocess_type"], "LOCAL_QWEN_FINDINGS_PREP")
        delta = self.run_prep("DELTA_REVIEW", {"REPAIR_DIFF": "x" * 17000})
        self.assertEqual(delta["preprocess_type"], "LOCAL_QWEN_DELTA_ASSIST")
        self.assertTrue(result["saved"]["source_paths"] and result["saved"]["source_hashes"])
        compact = p.compact_package({"GIT_DIFF": "large", "GOAL": "x"}, result)
        self.assertNotIn("GIT_DIFF", compact); self.assertIn("LOCAL_PREPROCESS", compact)

    def test_unavailable_auto_skips_and_required_manual_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.object(p.local_llm, "precheck_soft", return_value={"ok": False, "state": "LOCAL_LLM_UNSAFE_BIND", "reason": "unsafe"}):
            arguments = dict(run_id="RUN", node_id="N", node_type="PLAN", package={"GOAL": "x" * 5000}, artifact_root=Path(temp))
            self.assertEqual(p.preprocess_for_node(policy="AUTO_SAFE", **arguments)["status"], "SKIPPED_LOCAL_UNAVAILABLE")
            self.assertEqual(p.preprocess_for_node(policy="MANUAL", requested_type="LOCAL_QWEN_TASK_NORMALIZE", required=True, **arguments)["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
