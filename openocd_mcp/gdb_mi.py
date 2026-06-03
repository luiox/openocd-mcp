"""GDB/MI 异步控制器。

使用 GDB --interpreter=mi2 模式，通过事件驱动方式与 GDB 交互。
核心优势：
- 发送 -exec-continue 后立即返回，GDB 持续响应其他命令
- -exec-interrupt 优先通过 MI 协议完成
- GDB 主动推送 *stopped/*running 事件，无需轮询提示符

平台注意：
- Unix: MI -exec-interrupt 可靠，直接使用
- Windows: 管道 -exec-interrupt 可能超时，自动 fallback 到 OpenOCD telnet halt
"""

from __future__ import annotations

import os
import queue
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import Future
from typing import Any

GDB_DEFAULT_PORT = 3333
GDB_COMMAND_TIMEOUT = 30

# ---------------------------------------------------------------------------
# MI 协议解析
# ---------------------------------------------------------------------------


class MIParseError(Exception):
    """MI 协议解析错误。"""
    pass


def _unescape(s: str) -> str:
    """展开 C 风格转义序列。"""
    chars: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            esc = s[i + 1]
            table = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\', '0': '\0'}
            chars.append(table.get(esc, esc))
            i += 2
        else:
            chars.append(s[i])
            i += 1
    return ''.join(chars)


def parse_mi_line(line: str) -> dict:
    """解析单行 MI 输出。

    返回结构:
        {"type": "result|exec|notify|status|console|log|target|unknown",
         "token": int, "class": str, "results": dict, "content": str, "raw": str}
    """
    if not line:
        return {"type": "unknown", "raw": line}

    # --- 流式记录: ~"console", &"log", @"target" ---
    if line[0] in ('~', '&', '@'):
        type_map = {'~': 'console', '&': 'log', '@': 'target'}
        content_raw = line[1:]
        content = ""
        if content_raw.startswith('"') and content_raw.endswith('"'):
            try:
                content = _unescape(content_raw[1:-1])
            except Exception:
                content = content_raw[1:-1]
        elif content_raw.startswith('"'):
            try:
                end = content_raw.rfind('"')
                if end > 0:
                    content = _unescape(content_raw[1:end])
            except Exception:
                content = content_raw
        return {"type": type_map[line[0]], "content": content, "raw": line}

    # --- 提取前导 token ---
    token = 0
    rest = line
    m = re.match(r'^(\d+)', line)
    if m:
        token = int(m.group(1))
        rest = line[m.end():]

    if not rest:
        return {"type": "unknown", "raw": line}

    c = rest[0]
    body = rest[1:]

    # --- 结果 / 异步 / 通知 / 状态 记录 ---
    rt_map: dict[str, str] = {'^': 'result', '*': 'exec', '=': 'notify', '+': 'status'}
    record_type = rt_map.get(c, 'unknown')
    if record_type == 'unknown':
        return {"type": "unknown", "raw": line}

    # 解析 class 名称
    comma_pos = _find_comma_outside_quotes(body)
    if comma_pos == -1:
        klass = body.strip()
        params: dict[str, Any] = {}
    else:
        klass = body[:comma_pos].strip()
        params = _parse_params(body[comma_pos + 1:])

    return {
        "type": record_type,
        "token": token,
        "class": klass,
        "results": params,
        "raw": line,
    }


def _find_comma_outside_quotes(text: str) -> int:
    """找到不在引号/括号内的第一个逗号位置。"""
    depth = 0
    in_str = False
    escaped = False
    for i, c in enumerate(text):
        if escaped:
            escaped = False
            continue
        if c == '\\':
            escaped = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c in '({[':
                depth += 1
            elif c in ')}]':
                depth -= 1
            elif c == ',' and depth == 0:
                return i
    return -1


def _parse_params(text: str) -> dict[str, Any]:
    """解析逗号分隔的 key=value 参数列表。"""
    result: dict[str, Any] = {}
    pos = 0
    length = len(text)
    while pos < length:
        while pos < length and text[pos] in ' \t\r\n':
            pos += 1
        if pos >= length:
            break
        eq = text.find('=', pos)
        if eq == -1 or eq >= length - 1:
            break
        key = text[pos:eq].strip()
        pos = eq + 1
        value, pos = _parse_value(text, pos)
        result[key] = value
        while pos < length and text[pos] in ' \t\r\n':
            pos += 1
        if pos < length and text[pos] == ',':
            pos += 1
    return result


def _parse_value(text: str, pos: int) -> tuple[Any, int]:
    """解析单个 MI 值（字符串/元组/列表/裸值）。"""
    if pos >= len(text):
        return None, pos

    c = text[pos]

    # ---- 字符串 ----
    if c == '"':
        pos += 1
        chars: list[str] = []
        while pos < len(text):
            if text[pos] == '"':
                return ''.join(chars), pos + 1
            if text[pos] == '\\' and pos + 1 < len(text):
                esc = text[pos + 1]
                tbl = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\', '0': '\0'}
                chars.append(tbl.get(esc, esc))
                pos += 2
            else:
                chars.append(text[pos])
                pos += 1
        return ''.join(chars), pos

    # ---- 元组 {key=value,...} 或 {value,...} ----
    if c == '{':
        pos += 1
        result_dict: dict[str, Any] = {}
        result_list: list[Any] = []
        is_dict: bool | None = None

        while pos < len(text):
            while pos < len(text) and text[pos] in ' \t\r\n':
                pos += 1
            if pos >= len(text):
                break
            if text[pos] == '}':
                return (result_dict if is_dict is None or is_dict else result_list), pos + 1

            if is_dict is None:
                eq2 = text.find('=', pos, min(pos + 200, len(text)))
                is_dict = eq2 != -1 and eq2 < len(text) - 1

            if is_dict:
                eq2 = text.find('=', pos)
                if eq2 == -1:
                    break
                k = text[pos:eq2].strip()
                pos = eq2 + 1
                v, pos = _parse_value(text, pos)
                result_dict[k] = v
            else:
                v, pos = _parse_value(text, pos)
                result_list.append(v)

            while pos < len(text) and text[pos] in ' \t\r\n':
                pos += 1
            if pos < len(text) and text[pos] == ',':
                pos += 1
        return result_dict if is_dict is None or is_dict else result_list, pos

    # ---- 列表 [value,...] ----
    if c == '[':
        pos += 1
        lst: list[Any] = []
        while pos < len(text):
            while pos < len(text) and text[pos] in ' \t\r\n':
                pos += 1
            if pos >= len(text):
                break
            if text[pos] == ']':
                return lst, pos + 1
            v, pos = _parse_value(text, pos)
            lst.append(v)
            while pos < len(text) and text[pos] in ' \t\r\n':
                pos += 1
            if pos < len(text) and text[pos] == ',':
                pos += 1
        return lst, pos

    # ---- 裸值（无引号）— 读到逗号/括号/结束 ----
    start = pos
    depth2 = 0
    while pos < len(text):
        ch = text[pos]
        if ch == ',' and depth2 == 0:
            break
        if ch in ')}]':
            if depth2 == 0:
                break
            depth2 -= 1
        if ch in '({[':
            depth2 += 1
        pos += 1
    raw = text[start:pos].strip()
    return raw, pos


# ---------------------------------------------------------------------------
# GDB/MI 会话
# ---------------------------------------------------------------------------

class GDBMISession:
    """GDB/MI 异步会话。

    通过 --interpreter=mi2 启动 GDB，使用 MI 协议异步通信。
    线程安全，支持并发命令，通过 Future 同步结果。
    """

    def __init__(self, gdb_path: str) -> None:
        self._gdb_path = gdb_path
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._token_counter = 0

        # 待完成命令: token -> Future[dict]
        self._pending: dict[int, Future[dict]] = {}

        # 异步事件队列 (*running, *stopped)
        self._event_queue: queue.Queue[dict] = queue.Queue()

        # 目标状态
        self._target_state: str = "unknown"  # "running" | "stopped" | "unknown"
        self._last_stop_reason: str | None = None

        # 控制台输出缓存（累积在命令结果之间）
        self._console_buffer: list[str] = []

        # 启动是否完成
        self._started = threading.Event()

    @property
    def process(self) -> subprocess.Popen[str]:
        if not self._process:
            raise RuntimeError("GDB process is not running.")
        return self._process

    @property
    def target_state(self) -> str:
        return self._target_state

    @property
    def last_stop_reason(self) -> str | None:
        return self._last_stop_reason

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self, firmware_path: str, gdb_port: int = GDB_DEFAULT_PORT) -> None:
        """启动 GDB MI 会话: 连接远程目标并下载固件。"""
        try:
            kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 0,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            self._process = subprocess.Popen(
                [self._gdb_path, "--interpreter=mi2", firmware_path],
                **kwargs,
            )
        except OSError as error:
            raise RuntimeError(f"GDB failed to start: {error}") from error

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # 等待 GDB 准备就绪（发送一个无害命令并等待响应）
        self.send_mi("-gdb-set pagination off")

        # 连接远程目标
        result = self.send_mi(f"-target-select remote :{gdb_port}")
        if result.get("class") == "error":
            err = result.get("results", {}).get("msg", "connection failed")
            raise RuntimeError(f"GDB connection failed: {err}")

        # 下载固件
        result = self.send_mi("-target-download", timeout=120)
        if result.get("class") == "error":
            err = result.get("results", {}).get("msg", "download failed")
            raise RuntimeError(f"GDB load failed: {err}")

        self._started.set()

    def attach(self, firmware_path: str, gdb_port: int = GDB_DEFAULT_PORT) -> None:
        """附加到正在运行的目标: 连接远程 GDB 服务器但不下载固件、不复位。

        与 start() 的区别：
          - 跳过 -target-download（避免触发目标复位）
          - 不 halt 目标，保持原有运行状态
          - RTT 等后续配置在运行状态下完成
        """
        try:
            kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 0,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            self._process = subprocess.Popen(
                [self._gdb_path, "--interpreter=mi2", firmware_path],
                **kwargs,
            )
        except OSError as error:
            raise RuntimeError(f"GDB failed to start: {error}") from error

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # 等待 GDB 准备就绪
        self.send_mi("-gdb-set pagination off")

        # 连接远程目标（与 start() 相同）
        result = self.send_mi(f"-target-select remote :{gdb_port}")
        if result.get("class") == "error":
            err = result.get("results", {}).get("msg", "connection failed")
            raise RuntimeError(f"GDB connection failed: {err}")

        # ⚠️ 注意：跳过 -target-download，避免触发目标复位！
        # 目标保持当前运行状态不变，不做 halt。
        # RTT 配置等操作在目标运行状态下即可完成。

        self._started.set()

    def stop(self) -> None:
        """停止 GDB 进程。"""
        if not self._process:
            return

        if self._process.poll() is None:
            try:
                # 先中断目标（如仍在运行），再优雅退出
                if self._target_state == "running":
                    self.send_mi("-exec-interrupt", timeout=5)
                self._process.stdin.write("quit\n")
                self._process.stdin.flush()
            except Exception:
                pass
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)

        self._process = None
        self._target_state = "unknown"

    def wait_started(self, timeout: float = 10) -> None:
        """等待 start() 完成。"""
        if not self._started.wait(timeout=timeout):
            raise RuntimeError("GDB session did not start in time.")

    # ------------------------------------------------------------------
    # 命令发送
    # ------------------------------------------------------------------

    def _next_token(self) -> int:
        with self._lock:
            self._token_counter += 1
            return self._token_counter

    def send_mi(self, mi_command: str, timeout: float = GDB_COMMAND_TIMEOUT) -> dict:
        """发送 MI 命令并等待结果。

        Args:
            mi_command: MI 命令字符串，如 "-exec-continue"。
            timeout: 超时秒数。

        Returns:
            解析后的结果记录 dict。
        """
        process = self.process
        if process.stdin is None:
            raise RuntimeError("GDB stdin is unavailable.")

        token = self._next_token()
        future: Future[dict] = Future()
        with self._lock:
            self._pending[token] = future

        process.stdin.write(f"{token}{mi_command}\n")
        process.stdin.flush()

        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            with self._lock:
                self._pending.pop(token, None)
            raise RuntimeError(
                f"GDB command timed out after {timeout}s: {mi_command}"
            )

    def send_cli(self, cli_command: str, timeout: float = GDB_COMMAND_TIMEOUT) -> str:
        """发送 CLI 命令（封装为 -interpreter-exec console）。

        Args:
            cli_command: GDB CLI 命令，如 "print x"。
            timeout: 超时秒数。

        Returns:
            命令的控制台输出文本。
        """
        escaped = cli_command.replace('\\', '\\\\').replace('"', '\\"')
        result = self.send_mi(f'-interpreter-exec console "{escaped}"', timeout=timeout)
        console_out = result.get("console_output", "")
        if isinstance(console_out, list):
            return "\n".join(console_out)
        return str(console_out)

    def exec_continue(self) -> str:
        """继续执行目标（异步，立即返回）。"""
        result = self.send_mi("-exec-continue")
        # ^running 或 ^error
        if result.get("class") == "running":
            return "(target is running)"
        err = result.get("results", {}).get("msg", "unknown error")
        raise RuntimeError(f"Failed to continue: {err}")

    def exec_interrupt(self) -> str:
        """中断目标执行。

        平台策略：
        - Unix: 优先 MI -exec-interrupt
        - Windows: 优先 OpenOCD telnet halt（管道中断不可靠），MI 为降级
        """
        if os.name == "nt":
            return self._exec_interrupt_windows()
        return self._exec_interrupt_unix()

    def _exec_interrupt_unix(self) -> str:
        """Unix 中断：MI → OpenOCD telnet fallback。"""
        try:
            return self._exec_interrupt_mi()
        except RuntimeError:
            if self._interrupt_via_openocd():
                self._wait_for_stop_quiet(3)
                return "Target interrupted (via OpenOCD telnet)."
            raise

    def _exec_interrupt_windows(self) -> str:
        """Windows 中断：OpenOCD telnet halt → MI fallback。"""
        if self._interrupt_via_openocd():
            self._wait_for_stop_quiet(5)
            return "Target interrupted (via OpenOCD telnet)."
        return self._exec_interrupt_mi()

    def _exec_interrupt_mi(self) -> str:
        """通过 GDB/MI -exec-interrupt 中断。"""
        result = self.send_mi("-exec-interrupt")
        if result.get("class") == "error":
            msg = result.get("results", {}).get("msg", "interrupt failed")
            raise RuntimeError(f"Failed to interrupt target: {msg}")
        self._wait_for_stop_quiet(3)
        return "Target interrupted."

    def _wait_for_stop_quiet(self, timeout: float) -> None:
        """安静地等待目标停止，忽略超时。"""
        try:
            self.wait_for_stop(timeout=timeout)
        except RuntimeError:
            pass

    def wait_for_stop(self, timeout: float = 10) -> dict:
        """等待目标停止（*stopped 事件），可用于 continue 后等待断点。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self._event_queue.get(timeout=0.5)
                if event.get("class") == "stopped":
                    return event
            except queue.Empty:
                if self._target_state == "stopped":
                    return {"class": "stopped", "reason": self._last_stop_reason}
                continue
        raise RuntimeError(f"Target did not stop within {timeout}s.")

    def get_state(self) -> dict:
        """获取当前目标状态。"""
        return {
            "state": self._target_state,
            "reason": self._last_stop_reason,
        }

    # ------------------------------------------------------------------
    # MI 记录处理
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """持续读取 GDB stdout 并解析 MI 记录。"""
        if not self._process or not self._process.stdout:
            return

        for line in self._process.stdout:
            line = line.rstrip('\r\n')
            if not line:
                continue
            self._dispatch_line(line)

    def _dispatch_line(self, line: str) -> None:
        """分发一行 MI 输出到对应的处理器。"""
        record = parse_mi_line(line)
        rtype = record.get("type")

        if rtype == "result":
            self._handle_result(record)
        elif rtype == "exec":
            self._handle_exec_event(record)
        elif rtype == "notify":
            self._handle_notify(record)
        elif rtype == "console":
            self._console_buffer.append(record.get("content", ""))
        elif rtype == "status":
            # 状态记录（如 +download），通常可忽略
            pass
        # "log", "target", "unknown" — 忽略

    def _handle_result(self, record: dict) -> None:
        """处理结果记录: token^class,params。

        找到匹配的 Future 并解析它，附带累积的控制台输出。
        """
        token = record.get("token", 0)
        if token == 0:
            return  # 没有 token 的结果记录通常来自早期连接

        with self._lock:
            future = self._pending.pop(token, None)

        if future is not None:
            # 附带累积的控制台输出
            record["console_output"] = list(self._console_buffer)
            self._console_buffer.clear()
            future.set_result(record)

    def _handle_exec_event(self, record: dict) -> None:
        """处理异步执行事件: *running, *stopped。"""
        klass = record.get("class")

        if klass == "running":
            self._target_state = "running"
            self._event_queue.put(record)

        elif klass == "stopped":
            self._target_state = "stopped"
            reason = record.get("results", {}).get("reason", "unknown")
            self._last_stop_reason = reason
            self._event_queue.put(record)

    def _handle_notify(self, record: dict) -> None:
        """处理通知记录 (=thread-group-started, =thread-created 等)。"""
        # 这些只是信息性通知，不改变目标状态
        pass

    # ------------------------------------------------------------------
    # 中断 fallback（Windows 管道中断不可靠时使用）
    # ------------------------------------------------------------------

    def _interrupt_via_openocd(self) -> bool:
        """fallback: 通过 OpenOCD telnet (port 4444) halt 目标。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(("127.0.0.1", 4444))
                sock.sendall(b"halt\n")
                time.sleep(0.5)
            return True
        except Exception:
            return False
