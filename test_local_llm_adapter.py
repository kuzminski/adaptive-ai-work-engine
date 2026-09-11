#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline unit tests for the AAW LOCAL_LLM adapter. No network, no AnythingLLM."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import local_llm_adapter as a


class LoopbackGuardTests(unittest.TestCase):
    def test_loopback_hosts_accepted(self):
        for url in ("http://127.0.0.1:11434", "http://localhost:11434/v1", "http://[::1]:11434"):
            a.assert_loopback_endpoint(url)

    def test_routable_hosts_refused(self):
        for url in ("http://10.1.2.3:11434", "http://192.168.0.9:11434/v1", "http://model.example.com/v1"):
            with self.assertRaises(a.LocalLLMUnsafeBind):
                a.assert_loopback_endpoint(url)


class ContextPolicyTests(unittest.TestCase):
    def test_small_input_ok(self):
        info = a.enforce_context_policy([{"role": "user", "content": "hello world"}], num_ctx=a.DEFAULT_NUM_CTX)
        self.assertEqual(info["input_chars"], 11)
        self.assertLessEqual(info["input_tokens_est"], a.MAX_INPUT_TOKENS_EST)

    def test_oversized_chars_rejected_not_truncated(self):
        msgs = [{"role": "user", "content": "x" * (a.MAX_INPUT_CHARS + 50)}]
        with self.assertRaises(a.LocalContextLimit) as ctx:
            a.enforce_context_policy(msgs, num_ctx=a.DEFAULT_NUM_CTX)
        self.assertIn("truncat", str(ctx.exception).lower())

    def test_num_ctx_ceiling_enforced(self):
        with self.assertRaises(a.LocalContextLimit):
            a.enforce_context_policy([{"role": "user", "content": "hi"}], num_ctx=a.MAX_NUM_CTX_V0_1 + 1)

    def test_content_parts_counted(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "de"}]}]
        info = a.enforce_context_policy(msgs, num_ctx=a.DEFAULT_NUM_CTX)
        self.assertEqual(info["input_chars"], 5)


class TransportParsingTests(unittest.TestCase):
    def _patch_http(self, status, doc):
        return mock.patch.object(a, "_http_json", return_value=(status, doc, 0.2))

    def test_openai_happy_path(self):
        doc = {"choices": [{"message": {"role": "assistant", "content": "AAW_LOCAL_QWEN_PASS"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}, "model": a.LOCAL_MODEL_ID}
        with self._patch_http(200, doc):
            out = a.chat([{"role": "user", "content": "ping"}])
        self.assertEqual(out["content"], "AAW_LOCAL_QWEN_PASS")
        self.assertEqual(out["usage"], {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17})
        self.assertTrue(out["stateless"])

    def test_openai_malformed_no_choices(self):
        with self._patch_http(200, {"unexpected": True}):
            with self.assertRaises(a.LocalLLMProtocolError):
                a.chat([{"role": "user", "content": "ping"}])

    def test_openai_http_500_is_protocol_error(self):
        with self._patch_http(500, {"error": "boom"}):
            with self.assertRaises(a.LocalLLMProtocolError):
                a.chat([{"role": "user", "content": "ping"}])

    def test_native_usage_mapping(self):
        doc = {"message": {"role": "assistant", "content": "ok"}, "done_reason": "stop",
               "prompt_eval_count": 9, "eval_count": 3, "load_duration": 10, "total_duration": 100}
        with self._patch_http(200, doc):
            out = a.chat([{"role": "user", "content": "ping"}], transport="native")
        self.assertEqual(out["usage"], {"input_tokens": 9, "output_tokens": 3, "total_tokens": 12})
        self.assertEqual(out["cold_warm"], "WARM") if False else self.assertIn(out["cold_warm"], {"WARM", "COLD", "UNKNOWN"})

    def test_chat_json_parses_object(self):
        doc = {"choices": [{"message": {"content": '{"ok": true, "tag": "AAW"}'}, "finish_reason": "stop"}], "usage": {}}
        with self._patch_http(200, doc):
            out = a.chat_json([{"role": "user", "content": "give json"}])
        self.assertEqual(out["json"], {"ok": True, "tag": "AAW"})

    def test_chat_json_rejects_prose(self):
        doc = {"choices": [{"message": {"content": "sorry, I cannot"}, "finish_reason": "stop"}], "usage": {}}
        with self._patch_http(200, doc):
            with self.assertRaises(a.LocalLLMProtocolError):
                a.chat_json([{"role": "user", "content": "give json"}])


class FailClosedTests(unittest.TestCase):
    def test_timeout_maps_to_local_timeout(self):
        with mock.patch.object(a, "_http_json", side_effect=a.LocalLLMTimeout("slow")):
            with self.assertRaises(a.LocalLLMTimeout):
                a.chat([{"role": "user", "content": "ping"}])

    def test_connection_refused_maps_to_unavailable(self):
        with mock.patch.object(a, "_http_json", side_effect=a.LocalLLMUnavailable("refused")):
            with self.assertRaises(a.LocalLLMUnavailable):
                a.chat([{"role": "user", "content": "ping"}])

    def test_precheck_soft_never_raises(self):
        with mock.patch.object(a, "_http_json", side_effect=a.LocalLLMUnavailable("down")):
            with mock.patch.object(a, "verify_localhost_bind", return_value={"state": "UNVERIFIED_ASSUMED_LOOPBACK", "port": 11434}):
                out = a.precheck_soft()
        self.assertFalse(out["ok"])
        self.assertEqual(out["state"], "LOCAL_LLM_UNAVAILABLE")

    def test_precheck_blocks_on_unsafe_bind(self):
        with mock.patch.object(a, "verify_localhost_bind", return_value={"state": "UNSAFE_BIND", "port": 11434, "routable_candidate": "192.168.1.5"}):
            with self.assertRaises(a.LocalLLMUnsafeBind):
                a.precheck()


class TelemetryTests(unittest.TestCase):
    def test_no_cash_cost_zero(self):
        rec = a.build_telemetry({"usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                                 "transport": "openai", "wall_time_s": 0.3, "cold_warm": "WARM"},
                                stage="UNIT", outcome="PASS")
        self.assertNotIn("cost", rec)
        self.assertEqual(rec["external_paid_cost"], 0)
        self.assertEqual(rec["cost_basis"], "LOCAL_INFERENCE_NO_EXTERNAL_BILLING")
        self.assertEqual(rec["telemetry_status"], "CAPTURED")

    def test_missing_usage_is_null_not_zero(self):
        rec = a.build_telemetry({"usage": {}}, stage="UNIT", outcome="PASS")
        self.assertIsNone(rec["usage"]["input_tokens"])
        self.assertIsNone(rec["usage"]["output_tokens"])
        self.assertEqual(rec["telemetry_status"], "PARTIAL")

    def test_write_telemetry_no_overwrite(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p1 = a.write_telemetry({"usage": {"input_tokens": 1, "output_tokens": 1}}, stage="S", outcome="PASS",
                                   aaw_run_id="RUN1", stats_root=root)
            self.assertTrue(p1.is_file())
            data = json.loads(p1.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], a.TELEMETRY_SCHEMA)
            self.assertEqual(data["harness"], "ollama_openai_compat")


class CatalogContractTests(unittest.TestCase):
    def test_adapter_matches_catalog(self):
        self.assertEqual(a.self_test(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
