"""MCP 服务器入口 — 工具定义与 main 函数。

基于抽取的模块（config / openocd / gdb_mi / session）提供 MCP 工具。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP

from .config import GlobalConfig, ProjectConfigManager, load_runtime_config_from_file
from .session import DebugSessionManager

mcp = FastMCP("openocd-mcp")

_project_manager = ProjectConfigManager()
_global_config = GlobalConfig(
    openocd_path=os.environ.get("OPENOCD_PATH", "openocd"),
    gdb_path=os.environ.get("GDB_PATH", "arm-none-eabi-gdb"),
    openocd_scripts=os.environ.get("OPENOCD_SCRIPTS", ""),
    rtt_port=8888,
)
_session_manager = DebugSessionManager(_project_manager, _global_config)
_runtime_config_sources: dict[str, str] = {
    "openocd_path": "environment/default",
    "gdb_path": "environment/default",
    "openocd_scripts": "environment/default",
    "rtt_port": "default",
}
_runtime_config_file: str | None = None


def _non_empty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _ok_or_error(handler, *args, **kwargs) -> str:
    try:
        return handler(*args, **kwargs)
    except Exception as error:
        return f"Error: {error}"


def _resolve_global_config(args: argparse.Namespace) -> tuple[GlobalConfig, dict[str, str], str | None]:
    local_config_path = Path(os.getcwd()) / "config.json"
    local_values = load_runtime_config_from_file(local_config_path)

    openocd_arg = _non_empty_string(args.openocd_path)
    gdb_arg = _non_empty_string(args.gdb_path)
    scripts_arg = _non_empty_string(args.openocd_scripts)

    openocd_env = _non_empty_string(os.environ.get("OPENOCD_PATH"))
    gdb_env = _non_empty_string(os.environ.get("GDB_PATH"))
    scripts_env = _non_empty_string(os.environ.get("OPENOCD_SCRIPTS"))

    if openocd_arg:
        openocd_path = openocd_arg; openocd_source = "cli"
    elif openocd_env:
        openocd_path = openocd_env; openocd_source = "env:OPENOCD_PATH"
    elif "openocd_path" in local_values:
        openocd_path = local_values["openocd_path"]; openocd_source = "config.json"
    else:
        openocd_path = "openocd"; openocd_source = "default"

    if gdb_arg:
        gdb_path = gdb_arg; gdb_source = "cli"
    elif gdb_env:
        gdb_path = gdb_env; gdb_source = "env:GDB_PATH"
    elif "gdb_path" in local_values:
        gdb_path = local_values["gdb_path"]; gdb_source = "config.json"
    else:
        gdb_path = "arm-none-eabi-gdb"; gdb_source = "default"

    if scripts_arg:
        openocd_scripts = scripts_arg; scripts_source = "cli"
    elif scripts_env:
        openocd_scripts = scripts_env; scripts_source = "env:OPENOCD_SCRIPTS"
    elif "openocd_scripts" in local_values:
        openocd_scripts = local_values["openocd_scripts"]; scripts_source = "config.json"
    else:
        openocd_scripts = ""; scripts_source = "default"

    # RTT 端口
    rtt_port_arg = getattr(args, "rtt_port", None)
    rtt_port_env = os.environ.get("RTT_PORT")
    rtt_port_file = local_values.get("rtt_port", 8888)
    if rtt_port_arg:
        rtt_port = rtt_port_arg; rtt_port_source = "cli"
    elif rtt_port_env:
        rtt_port = int(rtt_port_env); rtt_port_source = "env:RTT_PORT"
    elif "rtt_port" in local_values:
        rtt_port = rtt_port_file; rtt_port_source = "config.json"
    else:
        rtt_port = 8888; rtt_port_source = "default"

    resolved = GlobalConfig(
        openocd_path=openocd_path,
        gdb_path=gdb_path,
        openocd_scripts=openocd_scripts,
        rtt_port=int(rtt_port),
    )
    sources = {
        "openocd_path": openocd_source,
        "gdb_path": gdb_source,
        "openocd_scripts": scripts_source,
        "rtt_port": rtt_port_source,
    }
    config_file = str(local_config_path) if local_config_path.is_file() else None
    return resolved, sources, config_file


# --- MCP 工具 ---

@mcp.tool(description="Set current project directory and load debug configurations from .vscode/launch.json.")
def set_project(project_dir: str) -> str:
    def _inner() -> str:
        _session_manager.stop_session()
        normalized_dir, names = _project_manager.set_project(project_dir)
        lines = [f"Project set to {normalized_dir}", f"Loaded {len(names)} debug configurations:"]
        lines.extend(f"- {name}" for name in names)
        return "\n".join(lines)
    return _ok_or_error(_inner)


@mcp.tool(description="Reload launch.json from current project and return available debug configurations.")
def refresh_debug_targets() -> str:
    def _inner() -> str:
        names = _project_manager.refresh()
        lines = ["Refreshed debug targets. Available configurations:"]
        lines.extend(f"- {name}" for name in names)
        return "\n".join(lines)
    return _ok_or_error(_inner)


@mcp.tool(description="Flash firmware once using specified launch configuration without starting debug session.")
def flash_download(config_name: str, firmware_path: str | None = None) -> str:
    def _inner() -> str:
        resolved_firmware, resolved_config, output = _session_manager.flash_download(config_name, firmware_path)
        return f"Flashing firmware {resolved_firmware} using config '{resolved_config}'...\nOpenOCD output:\n{output if output else '(no output)'}\nFlash done."
    return _ok_or_error(_inner)


@mcp.tool(description="Start debug session using specified launch configuration.")
def debug_start(config_name: str, firmware_path: str | None = None) -> str:
    def _inner() -> str:
        session = _session_manager.start_session(config_name, firmware_path)
        lines = [
            f"Debug session started with config '{session.config_name}'",
            f"OpenOCD PID: {session.openocd_process.pid}",
            f"GDB PID: {session.gdb_session.process.pid}",
            f"Loaded firmware {session.firmware_path}",
        ]
        if session.rtt_client and session.rtt_client.is_connected:
            lines.append(f"RTT connected on port {_global_config.rtt_port}")
        else:
            lines.append("RTT not available (firmware may not have RTT enabled)")
        config = _project_manager.get_config(session.config_name)
        if config.run_to_entry_point:
            lines.append(f"Running to {config.run_to_entry_point}...")
            lines.append(f"Stopped at {config.run_to_entry_point} (breakpoint hit)")
        lines.append("Ready for debug commands.")
        return "\n".join(lines)
    return _ok_or_error(_inner)


@mcp.tool(description="Stop current active debug session and terminate OpenOCD/GDB processes.")
def debug_stop() -> str:
    def _inner() -> str:
        stopped = _session_manager.stop_session()
        if not stopped:
            raise RuntimeError("No active debug session to stop.")
        return "Debug session terminated."
    return _ok_or_error(_inner)


@mcp.tool(description="Execute one GDB command in current active debug session. 'continue' maps to MI async, 'interrupt' maps to MI -exec-interrupt.")
def debug_command(command: str) -> str:
    return _ok_or_error(_session_manager.execute_gdb_command, command)


@mcp.tool(description="Continue target execution (asynchronous — returns immediately). GDB stays responsive after this.")
def debug_continue() -> str:
    return _ok_or_error(_session_manager.exec_continue)


@mcp.tool(description="Interrupt/pause the running target via GDB/MI -exec-interrupt with platform-specific fallback.")
def debug_interrupt() -> str:
    return _ok_or_error(_session_manager.interrupt_target)


@mcp.tool(description="Get current debug session status in JSON format.")
def debug_status() -> str:
    return _ok_or_error(lambda: json.dumps(_session_manager.status(), ensure_ascii=False, indent=2))


@mcp.tool(description="Get target execution state (running/stopped) and last stop reason.")
def debug_state() -> str:
    def _inner() -> str:
        return json.dumps(_session_manager.get_target_state(), ensure_ascii=False, indent=2)
    return _ok_or_error(_inner)


@mcp.tool(description="Return effective OpenOCD/GDB runtime configuration with value sources.")
def get_runtime_config() -> str:
    def _inner() -> str:
        cfg_file = Path(_runtime_config_file) if _runtime_config_file else (Path(os.getcwd()) / "config.json")
        return json.dumps({
            "openocd_path": _global_config.openocd_path,
            "gdb_path": _global_config.gdb_path,
            "openocd_scripts": _global_config.openocd_scripts,
            "rtt_port": _global_config.rtt_port,
            "sources": _runtime_config_sources,
            "cwd": os.getcwd(),
            "config_file": str(cfg_file),
            "config_file_exists": cfg_file.is_file(),
        }, ensure_ascii=False, indent=2)
    return _ok_or_error(_inner)


@mcp.tool(description="Read RTT log from the active debug session. Returns up to max_lines of output.")
def read_rtt(max_lines: int = 10) -> str:
    return _ok_or_error(_session_manager.read_rtt, max_lines)


# --- CLI 入口 ---

def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="openocd-mcp server")
    parser.add_argument("--openocd-path")
    parser.add_argument("--gdb-path")
    parser.add_argument("--openocd-scripts")
    parser.add_argument("--rtt-port", type=int, default=8888, help="RTT server port (default: 8888)")
    parser.add_argument("-sse", "--sse", action="store_true", help="Run MCP server in SSE/HTTP mode")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=9000, help="HTTP bind port")
    parser.add_argument("--path", default="/sse", help="HTTP endpoint path")
    return parser.parse_args()


def _normalize_http_path(path_value: str | None) -> str:
    value = (path_value or "").strip()
    if not value:
        return "/sse"
    if "://" in value:
        parsed = urlparse(value)
        value = parsed.path or "/sse"
    value = value.replace("\\", "/")
    if not value.startswith("/"):
        if re.match(r"^[A-Za-z]:/", value):
            value = "/" + value.rstrip("/").split("/")[-1]
        else:
            value = "/" + value
    return value


def main() -> None:
    global _global_config, _runtime_config_sources, _runtime_config_file
    args = _parse_cli_args()
    _global_config, _runtime_config_sources, _runtime_config_file = _resolve_global_config(args)
    _session_manager.set_global_config(_global_config)
    if args.sse:
        mcp.run(transport="sse", host=args.host, port=args.port, path=_normalize_http_path(args.path), show_banner=False)
        return
    mcp.run(show_banner=False)
