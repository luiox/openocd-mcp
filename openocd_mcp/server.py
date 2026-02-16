from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP


GDB_DEFAULT_PORT = 3333
OPENOCD_START_TIMEOUT_SECONDS = 15
OPENOCD_FLASH_TIMEOUT_SECONDS = 180
GDB_COMMAND_TIMEOUT_SECONDS = 30


@dataclass
class GlobalConfig:
    openocd_path: str
    gdb_path: str
    openocd_scripts: str


@dataclass
class DebugConfig:
    name: str
    executable: str | None
    config_files: list[str]
    run_to_entry_point: str | None
    cwd: str | None
    raw: dict[str, Any]


@dataclass
class DebugSession:
    config_name: str
    firmware_path: str
    project_dir: str
    openocd_process: subprocess.Popen[str]
    gdb_controller: "GDBController"


class ProjectConfigManager:
    def __init__(self) -> None:
        self._project_dir: str | None = None
        self._configs: dict[str, DebugConfig] = {}

    @property
    def project_dir(self) -> str | None:
        return self._project_dir

    def set_project(self, project_dir: str) -> tuple[str, list[str]]:
        normalized = os.path.abspath(os.path.normpath(project_dir))
        if not os.path.isdir(normalized):
            raise RuntimeError(f"Project directory {normalized} does not exist.")

        self._project_dir = normalized
        self._configs = self._load_configs(normalized)
        return normalized, list(self._configs.keys())

    def refresh(self) -> list[str]:
        if not self._project_dir:
            raise RuntimeError("No project set. Please call set_project first.")

        self._configs = self._load_configs(self._project_dir)
        return list(self._configs.keys())

    def get_config(self, name: str) -> DebugConfig:
        config = self._configs.get(name)
        if not config:
            raise RuntimeError(f"Config '{name}' not found in current project.")
        return config

    def all_config_names(self) -> list[str]:
        return list(self._configs.keys())

    def _load_configs(self, project_dir: str) -> dict[str, DebugConfig]:
        launch_path = os.path.join(project_dir, ".vscode", "launch.json")
        if not os.path.isfile(launch_path):
            raise RuntimeError("Could not find .vscode/launch.json in the project directory.")

        try:
            content = Path(launch_path).read_text(encoding="utf-8")
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError("Failed to parse launch.json: invalid JSON format.") from error
        except OSError as error:
            raise RuntimeError("launch.json not found or unreadable.") from error

        configurations = parsed.get("configurations")
        if not isinstance(configurations, list):
            raise RuntimeError("Failed to parse launch.json: missing configurations list.")

        loaded: dict[str, DebugConfig] = {}
        for item in configurations:
            if not isinstance(item, dict):
                continue

            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue

            cwd_value = item.get("cwd")
            resolved_cwd = None
            if isinstance(cwd_value, str) and cwd_value.strip():
                substituted_cwd = self._substitute_workspace_vars(cwd_value, project_dir)
                resolved_cwd = self._resolve_path(substituted_cwd, project_dir)

            executable_value = item.get("executable") or item.get("program")
            resolved_executable = None
            if isinstance(executable_value, str) and executable_value.strip():
                substituted_executable = self._substitute_workspace_vars(executable_value, project_dir)
                executable_base = resolved_cwd if resolved_cwd else project_dir
                resolved_executable = self._resolve_path(substituted_executable, executable_base)

            config_files_raw = item.get("configFiles", [])
            if isinstance(config_files_raw, str):
                config_files = [config_files_raw]
            elif isinstance(config_files_raw, list):
                config_files = [value for value in config_files_raw if isinstance(value, str) and value.strip()]
            else:
                config_files = []

            config_files = [self._substitute_workspace_vars(value, project_dir) for value in config_files]
            run_to_entry = item.get("runToEntryPoint")
            run_to_entry_point = run_to_entry if isinstance(run_to_entry, str) and run_to_entry.strip() else None

            loaded[name] = DebugConfig(
                name=name,
                executable=resolved_executable,
                config_files=config_files,
                run_to_entry_point=run_to_entry_point,
                cwd=resolved_cwd,
                raw=item,
            )

        return loaded

    @staticmethod
    def _substitute_workspace_vars(text: str, project_dir: str) -> str:
        return text.replace("${workspaceRoot}", project_dir).replace("${workspaceFolder}", project_dir)

    @staticmethod
    def _resolve_path(path_text: str, base_dir: str) -> str:
        candidate = os.path.expandvars(path_text)
        if os.path.isabs(candidate):
            return os.path.abspath(os.path.normpath(candidate))
        return os.path.abspath(os.path.normpath(os.path.join(base_dir, candidate)))


class OpenOCDController:
    def __init__(self, config: GlobalConfig) -> None:
        self._config = config

    def flash(self, project_dir: str, config_files: list[str], firmware_path: str) -> str:
        command = self._build_base_command(config_files)
        command.extend(["-c", f'program "{firmware_path}" verify reset exit'])

        try:
            completed = subprocess.run(
                command,
                cwd=project_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=OPENOCD_FLASH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("OpenOCD execution failed: timeout / no device found.") from error
        except OSError as error:
            raise RuntimeError(f"OpenOCD execution failed: {error}") from error

        output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        output = output.strip()

        if completed.returncode != 0:
            detail = output if output else f"exit code {completed.returncode}"
            raise RuntimeError(f"OpenOCD execution failed: {detail}")

        return output

    def start_server(self, project_dir: str, config_files: list[str]) -> subprocess.Popen[str]:
        command = self._build_base_command(config_files)

        try:
            process = subprocess.Popen(
                command,
                cwd=project_dir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as error:
            raise RuntimeError(f"OpenOCD failed to start: {error}") from error

        if not self._wait_for_port("127.0.0.1", GDB_DEFAULT_PORT, OPENOCD_START_TIMEOUT_SECONDS, process):
            startup_log = ""
            try:
                startup_log = self._read_process_output(process, timeout_seconds=1)
            except Exception:
                startup_log = ""

            self.stop_server(process)
            reason = startup_log.strip() if startup_log else "target remote :3333 timeout."
            raise RuntimeError(f"OpenOCD failed to start: {reason}")

        return process

    @staticmethod
    def stop_server(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _build_base_command(self, config_files: list[str]) -> list[str]:
        command = [self._config.openocd_path]
        if self._config.openocd_scripts:
            command.extend(["-s", self._config.openocd_scripts])

        for config_file in config_files:
            command.extend(["-f", config_file])

        return command

    @staticmethod
    def _wait_for_port(host: str, port: int, timeout_seconds: int, process: subprocess.Popen[str]) -> bool:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((host, port)) == 0:
                    return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _read_process_output(process: subprocess.Popen[str], timeout_seconds: int) -> str:
        if process.stdout is None:
            return ""

        start = time.time()
        parts: list[str] = []
        while time.time() - start < timeout_seconds:
            chunk = process.stdout.read(1)
            if not chunk:
                break
            parts.append(chunk)
        return "".join(parts)


class GDBController:
    def __init__(self, gdb_path: str) -> None:
        self._gdb_path = gdb_path
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._char_queue: queue.Queue[str] = queue.Queue()
        self._pending_output = ""

    @property
    def process(self) -> subprocess.Popen[str]:
        if not self._process:
            raise RuntimeError("GDB process is not running.")
        return self._process

    def start(self, firmware_path: str, gdb_port: int = GDB_DEFAULT_PORT) -> None:
        try:
            self._process = subprocess.Popen(
                [self._gdb_path, "--quiet", firmware_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=0,
            )
        except OSError as error:
            raise RuntimeError(f"GDB failed to start: {error}") from error

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self._wait_for_prompt(timeout_seconds=GDB_COMMAND_TIMEOUT_SECONDS)
        self.send_command("set pagination off")

        connect_output = self.send_command(f"target remote :{gdb_port}")
        if self._looks_like_error(connect_output):
            raise RuntimeError(f"GDB connection failed: {connect_output.strip() or 'target remote timeout.'}")

        load_output = self.send_command("load", timeout_seconds=120)
        if self._looks_like_error(load_output):
            raise RuntimeError(f"GDB load failed: {load_output.strip()}")

    def send_command(self, command: str, timeout_seconds: int = GDB_COMMAND_TIMEOUT_SECONDS) -> str:
        process = self.process
        if process.stdin is None:
            raise RuntimeError("GDB stdin is unavailable.")

        process.stdin.write(command + "\n")
        process.stdin.flush()

        output = self._wait_for_prompt(timeout_seconds=timeout_seconds).strip()
        return output

    def stop(self) -> None:
        if not self._process:
            return

        if self._process.poll() is None:
            try:
                self.send_command("quit", timeout_seconds=3)
            except Exception:
                pass
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)

        self._process = None

    def _reader_loop(self) -> None:
        if not self._process or not self._process.stdout:
            return

        while True:
            char = self._process.stdout.read(1)
            if not char:
                break
            self._char_queue.put(char)

    def _wait_for_prompt(self, timeout_seconds: int) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            self._drain_char_queue()

            marker_index = self._pending_output.rfind("(gdb)")
            if marker_index != -1:
                output = self._pending_output[:marker_index]
                self._pending_output = self._pending_output[marker_index + len("(gdb)") :].lstrip(" \r\n")
                return output

            if self._process and self._process.poll() is not None:
                self._drain_char_queue()
                raise RuntimeError("GDB process exited unexpectedly.")

            time.sleep(0.05)

        raise RuntimeError("GDB command timeout.")

    def _drain_char_queue(self) -> None:
        while True:
            try:
                self._pending_output += self._char_queue.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _looks_like_error(output: str) -> bool:
        lowered = output.lower()
        patterns = [
            "error",
            "failed",
            "cannot",
            "connection timed out",
            "no such file",
            "no executable file",
        ]
        return any(pattern in lowered for pattern in patterns)


class DebugSessionManager:
    def __init__(self, project_manager: ProjectConfigManager, global_config: GlobalConfig) -> None:
        self._project_manager = project_manager
        self._openocd_controller = OpenOCDController(global_config)
        self._global_config = global_config
        self._current_session: DebugSession | None = None
        self._lock = threading.Lock()

    def set_global_config(self, global_config: GlobalConfig) -> None:
        with self._lock:
            self._global_config = global_config
            self._openocd_controller = OpenOCDController(global_config)

    def stop_session(self) -> bool:
        with self._lock:
            if not self._current_session:
                return False

            session = self._current_session
            self._current_session = None

        try:
            session.gdb_controller.stop()
        finally:
            OpenOCDController.stop_server(session.openocd_process)
        return True

    def start_session(self, config_name: str, firmware_path_override: str | None) -> DebugSession:
        self.stop_session()

        project_dir = self._project_manager.project_dir
        if not project_dir:
            raise RuntimeError("No project set. Please call set_project first.")

        config = self._project_manager.get_config(config_name)
        firmware_path = firmware_path_override or config.executable
        if not firmware_path:
            raise RuntimeError("Firmware file missing.")

        firmware_path = os.path.abspath(os.path.normpath(firmware_path))
        if not os.path.isfile(firmware_path):
            raise RuntimeError(f"Firmware file {firmware_path} does not exist.")

        if not config.config_files:
            raise RuntimeError(f"Config '{config_name}' has no configFiles for OpenOCD.")

        openocd_process = self._openocd_controller.start_server(project_dir=project_dir, config_files=config.config_files)
        gdb_controller = GDBController(self._global_config.gdb_path)

        try:
            gdb_controller.start(firmware_path=firmware_path)
            if config.run_to_entry_point:
                gdb_controller.send_command(f"tbreak {config.run_to_entry_point}")
                gdb_controller.send_command("continue", timeout_seconds=120)
        except Exception as error:
            gdb_controller.stop()
            OpenOCDController.stop_server(openocd_process)
            raise RuntimeError(str(error)) from error

        session = DebugSession(
            config_name=config_name,
            firmware_path=firmware_path,
            project_dir=project_dir,
            openocd_process=openocd_process,
            gdb_controller=gdb_controller,
        )

        with self._lock:
            self._current_session = session

        return session

    def execute_gdb_command(self, command: str) -> str:
        with self._lock:
            if not self._current_session:
                raise RuntimeError("No active debug session. Call debug_start first.")
            gdb_controller = self._current_session.gdb_controller

        output = gdb_controller.send_command(command)
        if self._looks_like_gdb_failure(output):
            raise RuntimeError(f"GDB command failed: (gdb) {command}\n{output.strip()}")

        return output.strip()

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._current_session

        base: dict[str, Any] = {
            "session_active": bool(session),
            "project_dir": self._project_manager.project_dir,
            "available_configs": self._project_manager.all_config_names(),
        }

        if not session:
            return base

        base.update(
            {
                "config_name": session.config_name,
                "firmware": session.firmware_path,
                "openocd_pid": session.openocd_process.pid,
                "gdb_pid": session.gdb_controller.process.pid,
            }
        )
        return base

    def flash_download(self, config_name: str, firmware_path_override: str | None) -> tuple[str, str, str]:
        project_dir = self._project_manager.project_dir
        if not project_dir:
            raise RuntimeError("No project set. Please call set_project first.")

        config = self._project_manager.get_config(config_name)
        firmware_path = firmware_path_override or config.executable
        if not firmware_path:
            raise RuntimeError("Firmware file missing.")

        firmware_path = os.path.abspath(os.path.normpath(firmware_path))
        if not os.path.isfile(firmware_path):
            raise RuntimeError(f"Firmware file {firmware_path} does not exist.")

        if not config.config_files:
            raise RuntimeError(f"Config '{config_name}' has no configFiles for OpenOCD.")

        output = self._openocd_controller.flash(
            project_dir=project_dir,
            config_files=config.config_files,
            firmware_path=firmware_path,
        )
        return firmware_path, config_name, output

    @staticmethod
    def _looks_like_gdb_failure(output: str) -> bool:
        lowered = output.lower()
        patterns = [
            "no symbol",
            "undefined",
            "error",
            "cannot",
            "not defined",
            "unknown",
        ]
        return any(pattern in lowered for pattern in patterns)


mcp = FastMCP("openocd-mcp")
_project_manager = ProjectConfigManager()
_global_config = GlobalConfig(
    openocd_path=os.environ.get("OPENOCD_PATH", "openocd"),
    gdb_path=os.environ.get("GDB_PATH", "arm-none-eabi-gdb"),
    openocd_scripts=os.environ.get("OPENOCD_SCRIPTS", ""),
)
_session_manager = DebugSessionManager(_project_manager, _global_config)


def _ok_or_error(handler: callable[..., str], *args: Any, **kwargs: Any) -> str:
    try:
        return handler(*args, **kwargs)
    except Exception as error:
        return f"Error: {error}"


@mcp.tool()
def set_project(project_dir: str) -> str:
    def _inner() -> str:
        _session_manager.stop_session()
        normalized_dir, names = _project_manager.set_project(project_dir)
        lines = [f"Project set to {normalized_dir}"]
        lines.append(f"Loaded {len(names)} debug configurations:")
        lines.extend(f"- {name}" for name in names)
        return "\n".join(lines)

    return _ok_or_error(_inner)


@mcp.tool()
def refresh_debug_targets() -> str:
    def _inner() -> str:
        names = _project_manager.refresh()
        lines = ["Refreshed debug targets. Available configurations:"]
        lines.extend(f"- {name}" for name in names)
        return "\n".join(lines)

    return _ok_or_error(_inner)


@mcp.tool()
def flash_download(config_name: str, firmware_path: str | None = None) -> str:
    def _inner() -> str:
        resolved_firmware, resolved_config, output = _session_manager.flash_download(config_name, firmware_path)
        lines = [f"Flashing firmware {resolved_firmware} using config '{resolved_config}'...", "OpenOCD output:"]
        lines.append(output if output else "(no output)")
        lines.append("Flash done.")
        return "\n".join(lines)

    return _ok_or_error(_inner)


@mcp.tool()
def debug_start(config_name: str, firmware_path: str | None = None) -> str:
    def _inner() -> str:
        session = _session_manager.start_session(config_name, firmware_path)

        lines = [f"Debug session started with config '{session.config_name}'"]
        lines.append(f"OpenOCD PID: {session.openocd_process.pid}")
        lines.append(f"GDB PID: {session.gdb_controller.process.pid}")
        lines.append(f"Loaded firmware {session.firmware_path}")

        config = _project_manager.get_config(session.config_name)
        if config.run_to_entry_point:
            lines.append(f"Running to {config.run_to_entry_point}...")
            lines.append(f"Stopped at {config.run_to_entry_point} (breakpoint hit)")

        lines.append("Ready for debug commands.")
        return "\n".join(lines)

    return _ok_or_error(_inner)


@mcp.tool()
def debug_stop() -> str:
    def _inner() -> str:
        stopped = _session_manager.stop_session()
        if not stopped:
            raise RuntimeError("No active debug session to stop.")
        return "Debug session terminated."

    return _ok_or_error(_inner)


@mcp.tool()
def debug_command(command: str) -> str:
    return _ok_or_error(_session_manager.execute_gdb_command, command)


@mcp.tool()
def debug_status() -> str:
    return _ok_or_error(lambda: json.dumps(_session_manager.status(), ensure_ascii=False, indent=2))


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="openocd-mcp server")
    parser.add_argument("--openocd-path", default=os.environ.get("OPENOCD_PATH", "openocd"))
    parser.add_argument("--gdb-path", default=os.environ.get("GDB_PATH", "arm-none-eabi-gdb"))
    parser.add_argument("--openocd-scripts", default=os.environ.get("OPENOCD_SCRIPTS", ""))
    return parser.parse_args()


def main() -> None:
    global _global_config

    args = _parse_cli_args()
    _global_config = GlobalConfig(
        openocd_path=args.openocd_path,
        gdb_path=args.gdb_path,
        openocd_scripts=args.openocd_scripts,
    )
    _session_manager.set_global_config(_global_config)

    mcp.run()
