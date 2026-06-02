"""RTT 客户端。

连接 OpenOCD 开启的 RTT TCP 端口，读取实时日志。
"""

from __future__ import annotations

import socket
import threading
import time
from typing import ClassVar

RTT_DEFAULT_PORT = 8888
RTT_CONNECT_TIMEOUT = 5
RTT_READ_TIMEOUT = 1
RTT_BUFFER_SIZE = 4096


class RTTClient:
    """RTT TCP 客户端。

    通过 TCP 连接 OpenOCD 的 RTT 服务器，按行缓冲读取日志。
    线程安全。
    """

    _line_buffer: list[str]
    _buffer_lock: threading.Lock
    _reader_thread: threading.Thread | None
    _running: bool

    def __init__(self) -> None:
        self._socket: socket.socket | None = None
        self._line_buffer = []
        self._buffer_lock = threading.Lock()
        self._reader_thread = None
        self._running = False
        self._partial_line = ""

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._socket is not None and self._running

    def connect(self, host: str = "127.0.0.1", port: int = RTT_DEFAULT_PORT) -> None:
        """连接到 OpenOCD RTT 服务器。"""
        if self.is_connected:
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(RTT_CONNECT_TIMEOUT)
        try:
            sock.connect((host, port))
        except (socket.timeout, ConnectionRefusedError, OSError) as error:
            sock.close()
            raise RuntimeError(f"RTT connection failed: {error}") from error

        sock.settimeout(RTT_READ_TIMEOUT)
        self._socket = sock
        self._running = True
        self._partial_line = ""

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def read_lines(self, max_lines: int = 10) -> list[str]:
        """读取最多 max_lines 行 RTT 日志。"""
        if not self.is_connected:
            raise RuntimeError("RTT not connected.")

        with self._buffer_lock:
            available = len(self._line_buffer)
            count = min(max_lines, available)
            if count == 0:
                return []
            lines = self._line_buffer[:count]
            self._line_buffer = self._line_buffer[count:]
            return lines

    def close(self) -> None:
        """关闭 RTT 连接。"""
        self._running = False
        if self._reader_thread:
            self._reader_thread.join(timeout=2)
            self._reader_thread = None
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """后台线程：持续从 socket 读取数据，按行缓冲。"""
        sock = self._socket
        if not sock:
            return

        while self._running:
            try:
                data = sock.recv(RTT_BUFFER_SIZE)
                if not data:
                    # 连接关闭
                    self._running = False
                    break
                self._feed_data(data)
            except socket.timeout:
                # 超时是正常的，继续循环
                continue
            except OSError:
                self._running = False
                break

    def _feed_data(self, data: bytes) -> None:
        """将原始字节按行拆分并加入缓冲区。"""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("ascii", errors="replace")

        # 将数据与前一行遗留的未完成部分合并
        full_text = self._partial_line + text

        # 按行拆分
        if "\n" in full_text:
            lines = full_text.split("\n")
            # 最后一行可能不完整
            self._partial_line = lines[-1] if not full_text.endswith("\n") else ""
            complete_lines = lines[:-1] if not full_text.endswith("\n") else lines
            # 去除末尾的 \r
            complete_lines = [l.rstrip("\r") for l in complete_lines if l.strip()]
            if complete_lines:
                with self._buffer_lock:
                    self._line_buffer.extend(complete_lines)
        else:
            # 没有换行符，累积
            self._partial_line = full_text
