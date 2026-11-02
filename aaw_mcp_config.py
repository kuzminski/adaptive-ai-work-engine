#!/usr/bin/env python3
"""AAW MCP Client Configurator.

Configures AAW as an MCP (Model Context Protocol) server for:
  - Antigravity (~/.gemini/antigravity/mcp_config.json)
  - Claude (Claude Desktop and Claude Code ~/.claude.json / .mcp.json)
  - ChatGPT / Codex CLI (~/.codex/config.toml)

Pure standard library: json, os, sys, pathlib, shutil.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any


def get_aaw_root() -> Path:
    """Resolve the AAW repository / installation root."""
    env_root = os.environ.get("AAW_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent


def get_aaw_command(
    aaw_root: Path | None = None,
    is_frozen: bool | None = None,
) -> tuple[str, list[str]]:
    """Determine how to invoke AAW for MCP mode.

    Returns:
        (command, args)
        - If running as frozen executable (sys.frozen) or AAW.exe exists in repo root:
            command: Path to AAW.exe, args: ["--mcp"]
        - If running from source checkout:
            command: sys.executable, args: [str(Path(AAW_ROOT / "AAW.py")), "--mcp"]
    """
    root = (aaw_root or get_aaw_root()).resolve()
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen

    # Check for AAW.exe in root
    exe_in_root = root / "AAW.exe"
    if exe_in_root.is_file():
        return str(exe_in_root.resolve()), ["--mcp"]

    if frozen:
        # Running as frozen PyInstaller executable
        exe_path = Path(sys.executable).resolve()
        if exe_path.name.lower().startswith("aaw"):
            return str(exe_path), ["--mcp"]
        # Fallback to AAW.exe in root even if not yet created on disk
        return str((root / "AAW.exe").resolve()), ["--mcp"]

    # Running from source checkout
    py_path = (root / "AAW.py").resolve()
    return sys.executable, [str(py_path), "--mcp"]


# Alias for convenience
get_command = get_aaw_command


def get_antigravity_config_path(custom_home: Path | None = None) -> Path:
    home = custom_home if custom_home is not None else Path.home()
    return (home / ".gemini" / "antigravity" / "mcp_config.json").resolve()


def get_claude_desktop_config_path(custom_home: Path | None = None) -> Path:
    if custom_home is not None:
        home = custom_home.resolve()
        if sys.platform == "win32":
            return home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
        elif sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        else:
            return home / ".config" / "Claude" / "claude_desktop_config.json"

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return (base / "Claude" / "claude_desktop_config.json").resolve()
    elif sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json").resolve()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        return (base / "Claude" / "claude_desktop_config.json").resolve()


def get_claude_code_config_path(custom_home: Path | None = None) -> Path:
    home = custom_home if custom_home is not None else Path.home()
    return (home / ".claude.json").resolve()


def get_claude_project_config_path(project_root: Path | None = None) -> Path:
    root = (project_root or get_aaw_root()).resolve()
    return root / ".mcp.json"


def get_codex_config_path(custom_home: Path | None = None) -> Path:
    home = custom_home if custom_home is not None else Path.home()
    return (home / ".codex" / "config.toml").resolve()


def format_codex_toml(command: str, args: list[str]) -> str:
    """Format the [mcp_servers.aaw] section for Codex config.toml without external libraries."""
    cmd_val = json.dumps(command)
    args_val = json.dumps(args)
    return f"[mcp_servers.aaw]\ncommand = {cmd_val}\nargs = {args_val}\n"


def merge_codex_toml(existing_content: str, command: str, args: list[str]) -> str:
    """Merge [mcp_servers.aaw] into existing TOML content safely without external libraries.

    If [mcp_servers.aaw] already exists, replaces it in-place.
    If it does not exist, appends it while preserving all existing tables and comments.
    """
    new_section = format_codex_toml(command, args)
    if not existing_content.strip():
        return new_section

    lines = existing_content.splitlines(keepends=True)
    out: list[str] = []
    in_aaw_section = False
    section_replaced = False

    for line in lines:
        stripped = line.strip()
        # Detect section headers like [header]
        if stripped.startswith("[") and stripped.endswith("]"):
            header_name = stripped.strip("[]").strip()
            # Normalize possible quotes: [mcp_servers.aaw] or [mcp_servers."aaw"]
            normalized_name = header_name.replace('"', "").replace("'", "")
            if normalized_name == "mcp_servers.aaw":
                in_aaw_section = True
                section_replaced = True
                out.append(new_section)
                continue
            elif in_aaw_section:
                in_aaw_section = False
                out.append(line)
                continue

        if in_aaw_section:
            # Skip old contents of [mcp_servers.aaw]
            continue

        out.append(line)

    if not section_replaced:
        result = "".join(out)
        if not result.endswith("\n"):
            result += "\n"
        if not result.endswith("\n\n") and not result.endswith("\r\n\r\n"):
            result += "\n"
        result += new_section
        return result

    return "".join(out)


def generate_config(
    target: str,
    aaw_root: Path | None = None,
    as_string: bool = False,
) -> dict[str, Any] | str:
    """Generate configuration dictionary or snippet for target."""
    if as_string:
        return generate_config_snippet(target, aaw_root=aaw_root)

    cmd, args = get_aaw_command(aaw_root=aaw_root)
    normalized = target.strip().lower()

    if normalized in ("antigravity", "claude", "claude_desktop", "claude_code", "claude_project"):
        return {
            "mcpServers": {
                "aaw": {
                    "command": cmd,
                    "args": args,
                }
            }
        }
    elif normalized in ("codex", "chatgpt", "openai"):
        return {
            "mcp_servers": {
                "aaw": {
                    "command": cmd,
                    "args": args,
                }
            }
        }
    elif normalized == "all":
        return {
            "antigravity": generate_config("antigravity", aaw_root=aaw_root),
            "claude": generate_config("claude", aaw_root=aaw_root),
            "codex": generate_config("codex", aaw_root=aaw_root),
        }
    else:
        raise ValueError(f"Unknown target: {target}")


def generate_config_snippet(
    target: str,
    aaw_root: Path | None = None,
) -> str:
    """Generate the config snippet string for printing or inspection."""
    cmd, args = get_aaw_command(aaw_root=aaw_root)
    normalized = target.strip().lower()

    if normalized in ("antigravity", "claude", "claude_desktop", "claude_code", "claude_project"):
        cfg = {
            "mcpServers": {
                "aaw": {
                    "command": cmd,
                    "args": args,
                }
            }
        }
        return json.dumps(cfg, indent=2)
    elif normalized in ("codex", "chatgpt", "openai"):
        return format_codex_toml(cmd, args)
    elif normalized == "all":
        return (
            "# Antigravity (~/.gemini/antigravity/mcp_config.json)\n"
            + generate_config_snippet("antigravity", aaw_root=aaw_root)
            + "\n\n# Claude (~/.claude.json / Claude Desktop)\n"
            + generate_config_snippet("claude", aaw_root=aaw_root)
            + "\n\n# Codex (~/.codex/config.toml)\n"
            + generate_config_snippet("codex", aaw_root=aaw_root)
        )
    else:
        raise ValueError(f"Unknown target: {target}")


def _install_file(
    path: Path,
    file_type: str,
    command: str,
    args: list[str],
    target_name: str,
) -> dict[str, Any]:
    """Safely install/merge configuration into a single file with backup creation."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    existed = path.is_file()
    old_text = ""
    backup_path: Path | None = None

    if existed:
        old_text = path.read_text(encoding="utf-8")
        backup_path = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup_path)

    if file_type == "json":
        if existed and old_text.strip():
            data = json.loads(old_text)
        else:
            data = {}

        if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
            data["mcpServers"] = {}

        data["mcpServers"]["aaw"] = {
            "command": command,
            "args": args,
        }
        new_text = json.dumps(data, indent=2) + "\n"

    elif file_type == "toml":
        new_text = merge_codex_toml(old_text, command, args)
    else:
        raise ValueError(f"Unsupported file type: {file_type}")

    if not existed:
        status = "created"
    else:
        status = "unchanged" if old_text == new_text else "merged"

    if status != "unchanged":
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text)

    return {
        "target": target_name,
        "path": str(path),
        "status": status,
        "backup": str(backup_path) if backup_path else None,
        "command": command,
        "args": args,
    }


def install_config(
    target: str = "all",
    custom_home: Path | None = None,
    aaw_root: Path | None = None,
) -> dict[str, Any]:
    """Install or merge AAW MCP configuration for target client(s).

    Safe file writes:
      - Creates a `.bak` backup if the file already exists.
      - Merges `aaw` server definition without overwriting existing servers.
      - Returns details of what was installed and where.
    """
    cmd, args = get_aaw_command(aaw_root=aaw_root)
    normalized = target.strip().lower()

    targets_to_process: list[tuple[str, Path, str]] = []

    if normalized in ("antigravity", "all"):
        p = get_antigravity_config_path(custom_home)
        targets_to_process.append(("antigravity", p, "json"))

    if normalized in ("claude", "all"):
        p_desk = get_claude_desktop_config_path(custom_home)
        p_code = get_claude_code_config_path(custom_home)
        targets_to_process.append(("claude_desktop", p_desk, "json"))
        targets_to_process.append(("claude_code", p_code, "json"))
    elif normalized == "claude_desktop":
        p_desk = get_claude_desktop_config_path(custom_home)
        targets_to_process.append(("claude_desktop", p_desk, "json"))
    elif normalized == "claude_code":
        p_code = get_claude_code_config_path(custom_home)
        targets_to_process.append(("claude_code", p_code, "json"))
    elif normalized == "claude_project":
        p_proj = get_claude_project_config_path(aaw_root)
        targets_to_process.append(("claude_project", p_proj, "json"))

    if normalized in ("codex", "chatgpt", "openai", "all"):
        p = get_codex_config_path(custom_home)
        targets_to_process.append(("codex", p, "toml"))

    if not targets_to_process:
        raise ValueError(
            f"Unknown target: '{target}'. Must be one of: 'antigravity', 'claude', 'codex', 'all'."
        )

    installed_items = []
    for t_name, path, f_type in targets_to_process:
        info = _install_file(path, f_type, cmd, args, t_name)
        installed_items.append(info)

    res: dict[str, Any] = {
        "target": target,
        "success": True,
        "installed": installed_items,
        "files": installed_items,
    }

    # If single target file installed, expose path/status/backup directly at root
    if len(installed_items) == 1:
        res["path"] = installed_items[0]["path"]
        res["status"] = installed_items[0]["status"]
        res["backup"] = installed_items[0]["backup"]
    elif len(installed_items) > 1:
        res["paths"] = [item["path"] for item in installed_items]
        res["status"] = "created" if all(item["status"] == "created" for item in installed_items) else "merged"
        res["backup"] = [item["backup"] for item in installed_items if item["backup"]]

    return res


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for MCP Client Configurator."""
    parser = argparse.ArgumentParser(
        description="AAW MCP Client Configurator (Antigravity, Claude, ChatGPT/Codex)"
    )
    parser.add_argument(
        "--target",
        choices=["antigravity", "claude", "codex", "all"],
        default="all",
        help="Target MCP client (default: all)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Actually write/merge configuration files",
    )
    parser.add_argument(
        "--print",
        dest="print_snippet",
        action="store_true",
        help="Print the config snippet to stdout (default behavior if not --install)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="Custom home directory for configuration files (useful for testing)",
    )

    args = parser.parse_args(argv)

    if args.install:
        result = install_config(target=args.target, custom_home=args.home)
        for item in result.get("installed", []):
            status = item.get("status", "installed")
            path = item.get("path")
            backup = item.get("backup")
            msg = f"[{item.get('target', args.target)}] {status}: {path}"
            if backup:
                msg += f" (backup: {backup})"
            print(msg)
        return 0
    else:
        print(generate_config_snippet(args.target))
        return 0


if __name__ == "__main__":
    sys.exit(main())
