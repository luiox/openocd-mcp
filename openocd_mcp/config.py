"""项目与全局配置管理器。

从 .vscode/launch.json 加载调试配置，管理全局 OpenOCD/GDB 路径。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class GlobalConfig:
    openocd_path: str
    gdb_path: str
    openocd_scripts: str
    rtt_port: int = 8888
    adapter_speed: int = 0


@dataclass
class DebugConfig:
    name: str
    executable: str | None
    config_files: list[str]
    run_to_entry_point: str | None
    request: str  # "launch" | "attach"
    cwd: str | None
    raw: dict[str, Any]


class ProjectConfigManager:
    """管理项目目录下的 .vscode/launch.json 配置。"""

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
            parsed = self._parse_launch_content(content)
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

            request_raw = item.get("request", "launch")
            request = request_raw if isinstance(request_raw, str) and request_raw in ("launch", "attach") else "launch"

            loaded[name] = DebugConfig(
                name=name,
                executable=resolved_executable,
                config_files=config_files,
                run_to_entry_point=run_to_entry_point,
                request=request,
                cwd=resolved_cwd,
                raw=item,
            )

        return loaded

    @staticmethod
    def _parse_launch_content(content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise json.JSONDecodeError("launch.json root must be object", content, 0)
            return parsed
        except json.JSONDecodeError:
            pass

        no_block_comments = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
        no_line_comments = re.sub(r"(^|\s)//.*$", r"\1", no_block_comments, flags=re.MULTILINE)
        no_trailing_commas = re.sub(r",\s*([}\]])", r"\1", no_line_comments)

        parsed = json.loads(no_trailing_commas)
        if not isinstance(parsed, dict):
            raise json.JSONDecodeError("launch.json root must be object", no_trailing_commas, 0)
        return parsed

    @staticmethod
    def _substitute_workspace_vars(text: str, project_dir: str) -> str:
        return text.replace("${workspaceRoot}", project_dir).replace("${workspaceFolder}", project_dir)

    @staticmethod
    def _resolve_path(path_text: str, base_dir: str) -> str:
        candidate = os.path.expandvars(path_text)
        if os.path.isabs(candidate):
            return os.path.abspath(os.path.normpath(candidate))
        return os.path.abspath(os.path.normpath(os.path.join(base_dir, candidate)))


def load_runtime_config_from_file(config_path: Path) -> dict[str, Any]:
    """从 config.json 加载运行时配置（OpenOCD/GDB 路径、adapter_speed 等）。

    返回值包含所有识别到的字段，由调用方按需读取。
    文件丢失或 JSON 解析失败返回空 dict。
    """
    if not config_path.is_file():
        return {}

    try:
        content = config_path.read_text(encoding="utf-8")
        parsed = json.loads(content)
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(parsed, dict):
        return {}

    resolved: dict[str, Any] = {}

    for key in ("openocd_path", "openocdPath"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            resolved["openocd_path"] = val.strip()
            break

    for key in ("gdb_path", "gdbPath"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            resolved["gdb_path"] = val.strip()
            break

    for key in ("openocd_scripts", "openocdScripts"):
        val = parsed.get(key)
        if isinstance(val, str) and val.strip():
            resolved["openocd_scripts"] = val.strip()
            break

    arm_toolchain_path = parsed.get("armToolchainPath")
    if "gdb_path" not in resolved and isinstance(arm_toolchain_path, str) and arm_toolchain_path.strip():
        gdb_binary = "arm-none-eabi-gdb.exe" if os.name == "nt" else "arm-none-eabi-gdb"
        resolved["gdb_path"] = os.path.abspath(os.path.normpath(os.path.join(arm_toolchain_path.strip(), gdb_binary)))

    # 数值配置
    adapter_speed = parsed.get("adapter_speed")
    if adapter_speed is not None:
        resolved["adapter_speed"] = int(adapter_speed)

    rtt_port = parsed.get("rtt_port")
    if rtt_port is not None:
        resolved["rtt_port"] = int(rtt_port)

    return resolved


def _non_empty_str(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None
