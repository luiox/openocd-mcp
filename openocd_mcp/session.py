"""调试会话管理器。

管理 OpenOCD + GDB/MI 会话的生命周期，协调两个子进程。
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Any

from .config import GlobalConfig, ProjectConfigManager
from .gdb_mi import GDBMISession
from .openocd import OpenOCDController
from .rtt import RTTClient


@dataclass
class DebugSession:
    config_name: str
    firmware_path: str
    project_dir: str
    openocd_process: Any  # subprocess.Popen
    gdb_session: GDBMISession
    rtt_client: RTTClient | None = None


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

        # 尝试启动 RTT（在入口点断点命中后进行）
        # 注意：此时 firmware 停在 main 入口，log_init() 尚未执行。
        # rtt setup 不带签名参数，仅预置地址不阻塞。
        # 后续 rtt start 开始轮询，等 log_init() 写入 "SEGGER" 签名后自动生效。
        rtt_client = self._setup_rtt(gdb_session)

        session = DebugSession(
            config_name=config_name,
            firmware_path=firmware_path,
            project_dir=project_dir,
            openocd_process=openocd_process,
            gdb_session=gdb_session,
            rtt_client=rtt_client,
        )

        with self._lock:
            self._current_session = session

        return session

    def attach_session(self, config_name: str, firmware_path_override: str | None) -> DebugSession:
        """附加到正在运行的目标（不下载固件、不复位）。

        与 start_session() 的区别：
          - 使用 gdb_session.attach() 而非 .start()，跳过 -target-download
          - 不执行 run_to_entry_point（程序已在运行）
          - RTT 配置后自动恢复目标运行
        """
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

        # 附加 GDB（跳过下载固件，不复位）
        gdb_session = GDBMISession(self._global_config.gdb_path)
        try:
            gdb_session.attach(firmware_path=firmware_path)
        except Exception as error:
            gdb_session.stop()
            OpenOCDController.stop_server(openocd_process)
            raise RuntimeError(str(error)) from error

        # 设置 RTT（在目标运行状态下执行，SWD 内存读无需 halt）
        rtt_client = self._setup_rtt(gdb_session)

        # 确保目标继续运行（_setup_rtt 可能通过 OpenOCD 短暂暂停目标）
        try:
            gdb_session.exec_continue()
        except Exception:
            pass

        session = DebugSession(
            config_name=config_name,
            firmware_path=firmware_path,
            project_dir=project_dir,
            openocd_process=openocd_process,
            gdb_session=gdb_session,
            rtt_client=rtt_client,
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
            if session.rtt_client:
                try:
                    session.rtt_client.close()
                except Exception:
                    pass
        finally:
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

    def read_rtt(self, max_lines: int = 10) -> str:
        """读取 RTT 日志。"""
        session = self._get_session()
        if not session.rtt_client or not session.rtt_client.is_connected:
            raise RuntimeError("RTT not connected.")
        lines = session.rtt_client.read_lines(max_lines=max_lines)
        if not lines:
            return "(no new RTT output)"
        return "RTT log:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _setup_rtt(self, gdb_session: GDBMISession) -> RTTClient | None:
        """尝试设置 RTT 日志通道。

        在 start_session / attach_session 中共享。
        要求目标处于 halted 状态以便查询 _SEGGER_RTT 地址。
        """
        rtt_client: RTTClient | None = None
        rtt_port = self._global_config.rtt_port
        try:
            gdb_session.send_cli(f"monitor rtt server start {rtt_port} 0", timeout=5)

            addr_str = gdb_session.send_cli("print &_SEGGER_RTT", timeout=5)
            m = re.search(r'0x([0-9a-fA-F]+)', addr_str)
            if m:
                rtt_addr = m.group(0)
                gdb_session.send_cli(f'monitor rtt setup {rtt_addr} 1024', timeout=5)
                gdb_session.send_cli("monitor rtt start", timeout=5)
            else:
                raise RuntimeError("Could not resolve _SEGGER_RTT address")

            rtt_client = RTTClient()
            rtt_client.connect(port=rtt_port)
        except Exception:
            if rtt_client:
                try:
                    rtt_client.close()
                except Exception:
                    pass
                rtt_client = None
        return rtt_client

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
                "rtt_connected": session.rtt_client is not None and session.rtt_client.is_connected,
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
