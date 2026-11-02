"""Tests for AAW MCP Client Configurator (aaw_mcp_config.py)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib

import pytest

import aaw_mcp_config
from aaw_mcp_config import (
    format_codex_toml,
    generate_config,
    generate_config_snippet,
    get_aaw_command,
    get_antigravity_config_path,
    get_claude_code_config_path,
    get_claude_desktop_config_path,
    get_codex_config_path,
    install_config,
    main,
    merge_codex_toml,
)


class TestCommandDetection:
    """Test detection of how AAW is run (frozen executable vs source checkout)."""

    def test_detection_source_checkout(self, monkeypatch, tmp_path):
        """When running from source without AAW.exe, command is sys.executable with AAW.py."""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        cmd, args = get_aaw_command(aaw_root=tmp_path)
        assert cmd == sys.executable
        assert args == [str((tmp_path / "AAW.py").resolve()), "--mcp"]

    def test_detection_frozen_executable(self, monkeypatch, tmp_path):
        """When running as frozen app (sys.frozen), command is AAW.exe with args ['--mcp']."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        fake_exe = tmp_path / "AAW.exe"
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        cmd, args = get_aaw_command(aaw_root=tmp_path)
        assert cmd.lower().endswith("aaw.exe")
        assert args == ["--mcp"]

    def test_detection_repo_exe_exists(self, monkeypatch, tmp_path):
        """When AAW.exe exists in root, command is Path to AAW.exe even if sys.frozen is False."""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        fake_exe = tmp_path / "AAW.exe"
        fake_exe.write_text("fake binary")
        cmd, args = get_aaw_command(aaw_root=tmp_path)
        assert cmd == str(fake_exe.resolve())
        assert args == ["--mcp"]


class TestConfigSnippetGeneration:
    """Test config snippet generation for all 3 targets (antigravity, claude, codex)."""

    def test_antigravity_snippet(self):
        snippet = generate_config_snippet("antigravity")
        assert isinstance(snippet, str)
        parsed = json.loads(snippet)
        assert "mcpServers" in parsed
        assert "aaw" in parsed["mcpServers"]
        server = parsed["mcpServers"]["aaw"]
        assert "command" in server
        assert "--mcp" in server["args"]

    def test_claude_snippet(self):
        snippet = generate_config_snippet("claude")
        assert isinstance(snippet, str)
        parsed = json.loads(snippet)
        assert "mcpServers" in parsed
        assert "aaw" in parsed["mcpServers"]
        server = parsed["mcpServers"]["aaw"]
        assert "command" in server
        assert "--mcp" in server["args"]

    def test_codex_snippet(self):
        snippet = generate_config_snippet("codex")
        assert isinstance(snippet, str)
        assert "[mcp_servers.aaw]" in snippet
        assert "command =" in snippet
        assert 'args = [' in snippet
        parsed = tomllib.loads(snippet)
        assert "mcp_servers" in parsed
        assert "aaw" in parsed["mcp_servers"]
        assert "--mcp" in parsed["mcp_servers"]["aaw"]["args"]

    def test_all_snippet(self):
        snippet = generate_config_snippet("all")
        assert "Antigravity" in snippet
        assert "Claude" in snippet
        assert "Codex" in snippet
        assert "[mcp_servers.aaw]" in snippet

    def test_generate_config_dict(self):
        cfg_anti = generate_config("antigravity")
        assert "mcpServers" in cfg_anti
        assert "aaw" in cfg_anti["mcpServers"]

        cfg_claude = generate_config("claude")
        assert "mcpServers" in cfg_claude
        assert "aaw" in cfg_claude["mcpServers"]

        cfg_codex = generate_config("codex")
        assert "mcp_servers" in cfg_codex
        assert "aaw" in cfg_codex["mcp_servers"]


class TestTomlSerializationAndFormatting:
    """Test TOML serialization and formatting for codex without external toml library."""

    def test_format_codex_toml_escaping(self):
        # Paths with backslashes (Windows) and special characters
        cmd = r"C:\Program Files\AAW Engine\AAW.exe"
        args = ["--mcp", '--extra="quoted"']
        toml_str = format_codex_toml(cmd, args)

        parsed = tomllib.loads(toml_str)
        assert parsed["mcp_servers"]["aaw"]["command"] == cmd
        assert parsed["mcp_servers"]["aaw"]["args"] == args

    def test_merge_codex_toml_empty_file(self):
        merged = merge_codex_toml("", "AAW.exe", ["--mcp"])
        parsed = tomllib.loads(merged)
        assert parsed["mcp_servers"]["aaw"]["command"] == "AAW.exe"
        assert parsed["mcp_servers"]["aaw"]["args"] == ["--mcp"]

    def test_merge_codex_toml_preserves_other_tables_and_keys(self):
        existing = (
            '# User configuration\n'
            'model = "o3-mini"\n'
            'temperature = 0.2\n\n'
            '[mcp_servers.filesystem]\n'
            'command = "npx"\n'
            'args = ["-y", "@modelcontextprotocol/server-filesystem"]\n\n'
            '[other_section]\n'
            'flag = true\n'
        )
        merged = merge_codex_toml(existing, r"C:\AAW\AAW.exe", ["--mcp"])
        parsed = tomllib.loads(merged)

        # Original top-level keys preserved
        assert parsed["model"] == "o3-mini"
        assert parsed["temperature"] == 0.2

        # Original other MCP server preserved
        assert parsed["mcp_servers"]["filesystem"]["command"] == "npx"
        assert parsed["mcp_servers"]["filesystem"]["args"] == [
            "-y",
            "@modelcontextprotocol/server-filesystem",
        ]

        # Newly merged aaw server present
        assert parsed["mcp_servers"]["aaw"]["command"] == r"C:\AAW\AAW.exe"
        assert parsed["mcp_servers"]["aaw"]["args"] == ["--mcp"]

        # Trailing section preserved
        assert parsed["other_section"]["flag"] is True

    def test_merge_codex_toml_replaces_existing_aaw_section(self):
        existing = (
            '[mcp_servers.aaw]\n'
            'command = "old_aaw"\n'
            'args = ["--old"]\n\n'
            '[mcp_servers.other]\n'
            'command = "other"\n'
            'args = []\n'
        )
        merged = merge_codex_toml(existing, "new_aaw.exe", ["--mcp"])
        parsed = tomllib.loads(merged)

        assert parsed["mcp_servers"]["aaw"]["command"] == "new_aaw.exe"
        assert parsed["mcp_servers"]["aaw"]["args"] == ["--mcp"]
        assert parsed["mcp_servers"]["other"]["command"] == "other"
        # Ensure only one [mcp_servers.aaw] header exists
        assert merged.count("[mcp_servers.aaw]") == 1


class TestInstallConfig:
    """Test install_config with custom_home (tmp_path)."""

    def test_fresh_antigravity_installation(self, tmp_path):
        res = install_config("antigravity", custom_home=tmp_path)
        expected_path = get_antigravity_config_path(tmp_path)
        assert expected_path.is_file()
        assert res["status"] == "created"
        assert res["backup"] is None

        # Verify no .bak file was created
        assert not expected_path.with_name(expected_path.name + ".bak").exists()

        data = json.loads(expected_path.read_text(encoding="utf-8"))
        assert "aaw" in data["mcpServers"]

    def test_fresh_claude_installation(self, tmp_path):
        res = install_config("claude", custom_home=tmp_path)
        desktop_path = get_claude_desktop_config_path(tmp_path)
        code_path = get_claude_code_config_path(tmp_path)

        assert desktop_path.is_file()
        assert code_path.is_file()
        assert res["status"] == "created"

        desk_data = json.loads(desktop_path.read_text(encoding="utf-8"))
        assert "aaw" in desk_data["mcpServers"]

        code_data = json.loads(code_path.read_text(encoding="utf-8"))
        assert "aaw" in code_data["mcpServers"]

    def test_fresh_codex_installation(self, tmp_path):
        res = install_config("codex", custom_home=tmp_path)
        codex_path = get_codex_config_path(tmp_path)
        assert codex_path.is_file()
        assert res["status"] == "created"
        assert res["backup"] is None

        parsed = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        assert parsed["mcp_servers"]["aaw"]["args"][-1] == "--mcp"

    def test_merge_with_existing_config_containing_other_servers(self, tmp_path):
        # 1. Antigravity JSON merge
        anti_path = get_antigravity_config_path(tmp_path)
        anti_path.parent.mkdir(parents=True, exist_ok=True)
        initial_anti = {
            "mcpServers": {
                "preexisting_srv": {"command": "node", "args": ["index.js"]}
            },
            "customSetting": "active",
        }
        anti_path.write_text(json.dumps(initial_anti, indent=2), encoding="utf-8")

        res_anti = install_config("antigravity", custom_home=tmp_path)
        assert res_anti["status"] == "merged"
        anti_data = json.loads(anti_path.read_text(encoding="utf-8"))
        assert "preexisting_srv" in anti_data["mcpServers"]
        assert "aaw" in anti_data["mcpServers"]
        assert anti_data["customSetting"] == "active"

        # 2. Codex TOML merge
        codex_path = get_codex_config_path(tmp_path)
        codex_path.parent.mkdir(parents=True, exist_ok=True)
        initial_codex = (
            'theme = "dark"\n\n'
            '[mcp_servers.preexisting]\n'
            'command = "python"\n'
            'args = ["-m", "srv"]\n'
        )
        codex_path.write_text(initial_codex, encoding="utf-8")

        res_codex = install_config("codex", custom_home=tmp_path)
        assert res_codex["status"] == "merged"
        codex_data = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        assert codex_data["theme"] == "dark"
        assert codex_data["mcp_servers"]["preexisting"]["command"] == "python"
        assert "aaw" in codex_data["mcp_servers"]

    def test_backup_file_creation(self, tmp_path):
        anti_path = get_antigravity_config_path(tmp_path)
        anti_path.parent.mkdir(parents=True, exist_ok=True)
        original_content = json.dumps({"mcpServers": {"old": {"command": "x", "args": []}}})
        anti_path.write_text(original_content, encoding="utf-8")

        res = install_config("antigravity", custom_home=tmp_path)
        backup_path = anti_path.with_name(anti_path.name + ".bak")

        assert backup_path.is_file()
        assert res["backup"] == str(backup_path)
        assert backup_path.read_text(encoding="utf-8") == original_content

    def test_idempotent_reinstallation(self, tmp_path):
        # 1. First install
        res1 = install_config("all", custom_home=tmp_path)
        assert res1["status"] == "created"

        anti_path = get_antigravity_config_path(tmp_path)
        codex_path = get_codex_config_path(tmp_path)

        anti_content_1 = anti_path.read_text(encoding="utf-8")
        codex_content_1 = codex_path.read_text(encoding="utf-8")

        # 2. Re-install without changes
        res2 = install_config("all", custom_home=tmp_path)
        for item in res2["installed"]:
            assert item["status"] == "unchanged"

        anti_content_2 = anti_path.read_text(encoding="utf-8")
        codex_content_2 = codex_path.read_text(encoding="utf-8")

        assert anti_content_1 == anti_content_2
        assert codex_content_1 == codex_content_2


class TestCliEntryPoint:
    """Test CLI main() argument handling and execution."""

    def test_main_print_default(self, capsys):
        code = main([])
        assert code == 0
        captured = capsys.readouterr().out
        assert "Antigravity" in captured
        assert "Claude" in captured
        assert "Codex" in captured

    def test_main_print_target(self, capsys):
        code = main(["--target", "codex", "--print"])
        assert code == 0
        captured = capsys.readouterr().out
        assert "[mcp_servers.aaw]" in captured

    def test_main_install(self, tmp_path, capsys):
        code = main(["--install", "--target", "antigravity", "--home", str(tmp_path)])
        assert code == 0
        captured = capsys.readouterr().out
        assert "[antigravity] created:" in captured
        assert get_antigravity_config_path(tmp_path).is_file()

    def test_invalid_target_raises(self):
        with pytest.raises(ValueError, match="Unknown target"):
            install_config("unknown_client")
