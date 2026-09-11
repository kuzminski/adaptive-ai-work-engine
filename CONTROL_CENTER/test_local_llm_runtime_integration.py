#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Control Center <-> LOCAL_LLM integration tests.

Kept in a separate file so the pre-existing test_model_runtime_availability.py
suite is untouched. No network: local_llm_adapter is stubbed.
"""

from __future__ import annotations

import unittest
from unittest import mock

import aaw_control_center as cc


class LocalLlmProbeTests(unittest.TestCase):
    def test_probe_available(self):
        fake = mock.Mock()
        fake.precheck_soft.return_value = {
            "ok": True, "state": "AVAILABLE", "runtime_version": "0.20.7",
            "endpoint": "http://127.0.0.1:11434", "model": "qwen3-vl:4b-instruct",
            "model_loaded": True, "bind_check": {"state": "LOOPBACK_ONLY"},
        }
        with mock.patch.object(cc, "local_llm", fake):
            row = cc.probe_local_llm_runtime()
        self.assertEqual(row["state"], "AVAILABLE")
        self.assertIn("0.20.7", row["reason"])

    def test_probe_runtime_not_running_is_not_broken(self):
        fake = mock.Mock()
        fake.precheck_soft.return_value = {"ok": False, "state": "LOCAL_LLM_UNAVAILABLE",
                                          "reason": "cannot reach", "endpoint": "http://127.0.0.1:11434",
                                          "model": "qwen3-vl:4b-instruct"}
        with mock.patch.object(cc, "local_llm", fake):
            row = cc.probe_local_llm_runtime()
        self.assertEqual(row["state"], "UNAVAILABLE")
        self.assertIn("running", row["reason"].lower())
        self.assertNotIn("broken", row["reason"].lower())
        self.assertNotIn("corrupt", row["reason"].lower())

    def test_probe_unsafe_bind_blocks(self):
        fake = mock.Mock()
        fake.precheck_soft.return_value = {"ok": False, "state": "LOCAL_LLM_UNSAFE_BIND",
                                          "reason": "non-loopback listener", "endpoint": "http://127.0.0.1:11434"}
        with mock.patch.object(cc, "local_llm", fake):
            row = cc.probe_local_llm_runtime()
        self.assertEqual(row["state"], "UNAVAILABLE")
        self.assertIn("BLOCKED", row["reason"])

    def test_probe_adapter_missing_is_not_checked(self):
        with mock.patch.object(cc, "local_llm", None):
            row = cc.probe_local_llm_runtime()
        self.assertEqual(row["state"], "NOT_CHECKED")


class LocalProfileWiringTests(unittest.TestCase):
    def setUp(self):
        self.catalog = cc.load_implementer_profiles_for_ui()

    def test_four_local_profiles_present(self):
        for pid in cc.LOCAL_LLM_PROFILE_IDS:
            self.assertIn(pid, self.catalog)
            self.assertTrue(self.catalog[pid].get("local_runtime"))

    def test_local_profiles_excluded_from_node_bindings(self):
        app = cc.ControlCenter.__new__(cc.ControlCenter)
        app.profile_catalog = self.catalog
        selectable = app._node_binding_profiles()
        for pid in cc.LOCAL_LLM_PROFILE_IDS:
            self.assertNotIn(pid, selectable)
        # non-local ones still selectable
        self.assertIn("TERRA_HIGH", selectable)

    def test_local_profiles_visible_in_settings_models(self):
        app = cc.ControlCenter.__new__(cc.ControlCenter)
        app.profile_catalog = self.catalog
        app.model_runtime = None
        lines = "\n".join(app._profile_settings_lines())
        self.assertIn("Local Qwen / fast", lines)
        self.assertIn("Local Qwen / JSON", lines)

    def test_runtime_state_resolves_local_profile_verified(self):
        runtime = {
            "snapshot_schema_version": cc.MODEL_RUNTIME_SCHEMA_VERSION,
            "profile_catalog_version": cc.profile_catalog_version(),
            "checked_at": "2026-09-06T14:00:00+02:00",
            "harnesses": {
                "ollama_openai_compat": {"state": "AVAILABLE", "reason": "Local Qwen runtime responds"},
                "codex": {"state": "AVAILABLE", "reason": "ok"},
                "claude": {"state": "AVAILABLE", "reason": "ok"},
            },
        }
        state = cc.get_runtime_profile_state("LOCAL_QWEN_JSON", self.catalog, runtime)
        self.assertEqual(state["state"], "VERIFIED")

    def test_runtime_state_local_profile_unavailable_when_runtime_down(self):
        runtime = {
            "snapshot_schema_version": cc.MODEL_RUNTIME_SCHEMA_VERSION,
            "profile_catalog_version": cc.profile_catalog_version(),
            "checked_at": "2026-09-06T14:00:00+02:00",
            "harnesses": {
                "ollama_openai_compat": {"state": "UNAVAILABLE", "reason": "Local runtime not running (start AnythingLLM Desktop)"},
                "codex": {"state": "AVAILABLE", "reason": "ok"},
                "claude": {"state": "AVAILABLE", "reason": "ok"},
            },
        }
        state = cc.get_runtime_profile_state("LOCAL_QWEN_FAST", self.catalog, runtime)
        self.assertEqual(state["state"], "UNAVAILABLE")
        self.assertIn("running", state["reason"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
