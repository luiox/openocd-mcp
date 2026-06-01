"""调试会话管理器。

管理 OpenOCD + GDB/MI 会话的生命周期，协调两个子进程。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

from .config import GlobalConfig, ProjectConfigManager
from .gdb_mi import GDBMISession
from .openocd import OpenOCDController


@dataclass
class DebugSession:
    config_name: str
    firmware_path: str
    project_dir: str
    openocd_process: Any  # subprocess.Popen
    gdb_session: GDBMISession


class DebugSessionManager:
    """调试会话管理器。

    维护当前调试会话（最多一个），协调 OpenOCD 和 GDB 的生命周期。
    线程安全。
    """

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

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def start_session(self, config_name: str, firmware_path_override: str | None) -> DebugSession:
        """启动新的调试会话（自动停止旧的）。"""
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

        # 启动 OpenOCD
        openocd_process = self._openocd_controller.start_server(
            project_dir=project_dir, config_files=config.config_files
        )

        # 启动 GDB/MI 会话
        gdb_session = GDBMISession(self._global_config.gdb_path)
        try:
            gdb_session.start(firmware_path=firmware_path)
        except Exception as error:
            gdb_session.stop()
            OpenOCDController.stop_server(openocd_process)
            raise RuntimeError(str(error)) from error

        # 如果需要运行到入口点
        if config.run_to_entry_point:
            try:
                gdb_session.send_mi(f'-break-insert -t "{config.run_to_entry_point}"')
                gdb_session.exec_continue()
            except Exception as error:
                gdb_session.stop()
                OpenOCDController.stop_server(openocd_process)
                raise RuntimeError(f"Failed to run to entry point: {error}") from error

        session = DebugSession(
            config_name=config_name,
            firmware_path=firmware_path,
            project_dir=project_dir,
            openocd_process=openocd_process,
            gdb_session=gdb_session,
        )

        with self._lock:
            self._current_session = session

        return session

    def stop_session(self) -> bool:
        """停止当前调试会话。"""
        with self._lock:
            if not self._current_session:
                return False
            session = self._current_session
            self._current_session = None

        try:
            session.gdb_session.stop()
        finally:
            OpenOCDController.stop_server(session.openocd_process)
        return True

    # ------------------------------------------------------------------
    # GDB 命令
    # ------------------------------------------------------------------

    def execute_gdb_command(self, command: str) -> str:
        """执行 GDB 命令。

        自动检测命令类型：
        - "continue" → 使用 MI 的 -exec-continue（异步，立即返回）
        - "interrupt" → 使用 MI 的 -exec-interrupt
        - 以 "-" 开头 → 作为 MI 命令发送
        - 其他 → 作为 CLI 命令发送
        """
        session = self._get_session()
        gdb = session.gdb_session

        cmd_stripped = command.strip()

        # 特殊命令映射
        if cmd_stripped in ("continue", "c", "cont"):
            return gdb.exec_continue()

        if cmd_stripped in ("interrupt", "Ctrl+C", "Ctrl-C"):
            return gdb.exec_interrupt()

        # MI 命令（以 "-" 开头）
        if cmd_stripped.startswith("-"):
            result = gdb.send_mi(cmd_stripped)
            output = _format_mi_result(result)
            return output

        # CLI 命令 → 通过 -interpreter-exec console 发送
        return gdb.send_cli(cmd_stripped)

    def interrupt_target(self) -> str:
        """中断目标执行。"""
        session = self._get_session()
        return session.gdb_session.exec_interrupt()

    def exec_continue(self) -> str:
        """继续目标执行。"""
        session = self._get_session()
        return session.gdb_session.exec_continue()

    def get_target_state(self) -> dict:
        """获取目标状态。"""
        session = self._get_session()
        return session.gdb_session.get_state()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """返回当前会话状态。"""
        with self._lock:
            session = self._current_session

        base: dict[str, Any] = {
            "session_active": bool(session),
            "project_dir": self._project_manager.project_dir,
            "available_configs": self._project_manager.all_config_names(),
        }

        if session:
            base.update({
                "config_name": session.config_name,
                "firmware": session.firmware_path,
                "openocd_pid": session.openocd_process.pid,
                "gdb_pid": session.gdb_session.process.pid,
            })
            # 添加目标状态
            try:
                state_info = session.gdb_session.get_state()
                base["target_state"] = state_info["state"]
                base["last_stop_reason"] = state_info["reason"]
            except Exception:
                base["target_state"] = "unknown"

        return base

    def flash_download(self, config_name: str, firmware_path_override: str | None) -> tuple[str, str, str]:
        """一次性烧录固件（不启动调试会话）。"""
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

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_session(self) -> DebugSession:
        with self._lock:
            if not self._current_session:
                raise RuntimeError("No active debug session. Call debug_start first.")
            return self._current_session


def _format_mi_result(result: dict) -> str:
    """格式化 MI 结果记录为可读字符串。"""
    klass = result.get("class", "")
    results = result.get("results", {})

    if klass in ("running",):
        return "(target is running)"

    if klass == "done":
        # 尝试提取有用的输出
        console = result.get("console_output", [])
        if console:
            return "\n".join(console)
        return "(done)"

    if klass == "error":
        msg = results.get("msg", "unknown error")
        return f"Error: {msg}"

    if klass == "stopped":
        reason = results.get("reason", "unknown")
        return f"Target stopped (reason: {reason})"

    # 对于其他结果，尝试格式化显示
    parts = [f"({klass})"]
    console = result.get("console_output", [])
    if console:
        parts.append("\n".join(console))
    if results:
        try:
            import json
            parts.append(json.dumps(results, ensure_ascii=False))
        except Exception:
            parts.append(str(results))
    return "\n".join(parts)
