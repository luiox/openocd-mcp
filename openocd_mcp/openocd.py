"""OpenOCD 控制器。

管理 OpenOCD 进程的生命周期：启动 GDB 服务器、一次性烧录、停止。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import Any

from .config import GlobalConfig

GDB_DEFAULT_PORT = 3333
OPENOCD_START_TIMEOUT_SECONDS = 15
OPENOCD_FLASH_TIMEOUT_SECONDS = 180


class OpenOCDController:
    """封装对 OpenOCD 进程的操作。"""

    def __init__(self, config: GlobalConfig) -> None:
        self._config = config

    def flash(self, project_dir: str, config_files: list[str], firmware_path: str) -> str:
        """一次性烧录固件到目标。"""
        command = self._build_base_command(config_files)
        firmware_for_openocd = firmware_path.replace("\\", "/")
        command.extend(["-c", f'program "{firmware_for_openocd}" verify reset exit'])

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
        """启动常驻 OpenOCD GDB 服务器进程。"""
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
        """停止 OpenOCD 进程。"""
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
        # adapter speed 必须在配置文件之后，否则会被覆盖
        if self._config.adapter_speed > 0:
            command.extend(["-c", f"adapter speed {self._config.adapter_speed}"])
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
