# openocd-mcp

基于 [fastmcp](https://github.com/jlowin/fastmcp) 的 OpenOCD 调试 MCP 服务器，将嵌入式烧录与 GDB 调试工作流封装为 AI 可调用的工具。复用项目已有的 `.vscode/launch.json` 作为调试目标来源，无需额外配置。

## 特性

- 🔧 **零配置** — 直接复用 VS Code 的 `launch.json`，无需维护额外配置文件
- ⚡ **GDB/MI 异步协议** — 基于 MI2 事件驱动，`continue` 不阻塞，`interrupt` 即时生效
- 📡 **RTT 实时日志** — 自动连接 SEGGER RTT，读取 MCU 运行时输出
- 🖥️ **跨平台** — 支持 Windows / Linux / macOS，Windows 上自动回退 OpenOCD telnet halt
- 🔄 **双模式运行** — stdio（VS Code MCP）和 SSE/HTTP（本地 AI 客户端）

## 快速开始

### 安装

```bash
# 克隆仓库
git clone https://github.com/luiox/openocd-mcp.git
cd openocd-mcp

# 安装依赖
uv sync
```

### 运行

```bash
# stdio 模式（VS Code MCP 默认）
uv run openocd-mcp

# 自定义工具路径
uv run openocd-mcp --openocd-path /usr/bin/openocd --gdb-path /usr/bin/arm-none-eabi-gdb

# SSE/HTTP 模式（给本地其他 AI 客户端）
uv run openocd-mcp -sse --host 127.0.0.1 --port 9000
```

参数优先级：命令行参数 > 环境变量 > `config.json` > 内置默认值。

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENOCD_PATH` | OpenOCD 可执行文件路径 | `openocd` |
| `GDB_PATH` | GDB 可执行文件路径 | `arm-none-eabi-gdb` |
| `OPENOCD_SCRIPTS` | OpenOCD 脚本搜索路径 | `""` |
| `RTT_PORT` | RTT 服务器端口 | `8888` |

### config.json 配置

在项目根目录创建 `config.json`（已被 `.gitignore` 忽略）：

```json
{
    "openocd_path": "D:/sdk/OpenOCD/bin/openocd.exe",
    "gdb_path": "D:/sdk/Arm GNU Toolchain/bin/arm-none-eabi-gdb.exe",
    "openocd_scripts": "D:/sdk/OpenOCD/share/openocd/scripts",
    "rtt_port": 8888,
    "adapter_speed": 0
}
```

## VS Code 集成

项目已包含 `.vscode/mcp.json`，使用 stdio 模式启动：

```json
{
    "servers": {
        "openocd-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": ["run", "openocd-mcp"],
            "cwd": "${workspaceFolder}"
        }
    }
}
```

如需 SSE 模式：

```json
{
    "servers": {
        "openocd-mcp": {
            "type": "sse",
            "url": "http://127.0.0.1:9000/sse"
        }
    }
}
```

## MCP 工具列表

### 项目与配置

| 工具 | 描述 |
|------|------|
| `set_project(project_dir)` | 加载项目 `.vscode/launch.json`，解析所有调试配置 |
| `refresh_debug_targets()` | 重新加载 launch.json 配置（修改后刷新） |
| `get_runtime_config()` | 查看当前 OpenOCD/GDB 路径及其来源 |

### 烧录与调试

| 工具 | 描述 |
|------|------|
| `flash_download(config_name, firmware_path?)` | 一次性烧录固件（不启动调试会话） |
| `debug_start(config_name, firmware_path?)` | 启动 OpenOCD + GDB 调试会话，加载固件，可选运行到入口点 |
| `debug_stop()` | 终止当前调试会话 |
| `debug_command(command)` | 执行任意 GDB 命令 |
| `debug_continue()` | 继续目标执行（异步，立即返回） |
| `debug_interrupt()` | 中断/暂停运行中的目标 |

### 状态与日志

| 工具 | 描述 |
|------|------|
| `debug_status()` | 获取调试会话状态（JSON） |
| `debug_state()` | 获取目标执行状态和停止原因 |
| `read_rtt(max_lines)` | 读取 RTT 实时日志（默认 10 行） |
| `shutdown()` | 优雅关闭 MCP 服务器 |

## 架构

```
AI 客户端 → MCP 协议 → openocd-mcp
                           ├── ProjectConfigManager (解析 launch.json)
                           ├── OpenOCDController (启动/停止 OpenOCD 进程)
                           ├── GDBMISession (MI2 异步协议通信)
                           ├── RTTClient (实时日志读取)
                           └── DebugSessionManager (协调生命周期)
```

| 模块 | 职责 |
|------|------|
| `ProjectConfigManager` | 解析 `.vscode/launch.json`，替换 `${workspaceFolder}`，缓存配置 |
| `OpenOCDController` | 启动 OpenOCD 进行烧录（`program`）或作为 GDB 服务器（`:3333`） |
| `GDBMISession` | GDB/MI 异步会话，协议解析、事件驱动、无需轮询提示符 |
| `RTTClient` | TCP 连接 RTT 端口，后台线程按行缓冲读取日志 |
| `DebugSessionManager` | 单会话模型，协调 OpenOCD + GDB + RTT 生命周期 |

## launch.json 要求

目标项目必须包含 `.vscode/launch.json`，每个配置需包含：

- `name` — 配置名称（唯一标识）
- `configFiles` — OpenOCD 脚本列表（如 `["interface/cmsis-dap.cfg", "target/stm32f1x.cfg"]`）
- `executable` — 固件 ELF 文件路径（支持 `${workspaceFolder}` 变量）
- `runToEntryPoint`（可选）— 入口点断点（如 `"main"`）

示例：

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "Debug STM32",
            "type": "cortex-debug",
            "configFiles": [
                "interface/cmsis-dap.cfg",
                "target/stm32f1x.cfg"
            ],
            "executable": "${workspaceFolder}/build/firmware.elf",
            "runToEntryPoint": "main"
        }
    ]
}
```

> 支持 JSON 中的 C 风格注释和尾随逗号（自定义解析器自动清理）。

## 关键设计

- **单会话模型**：同时最多一个调试会话，`debug_start` 自动停止之前的会话
- **异步继续**：`debug_continue()` 通过 MI `^running` 立即返回，不阻塞等待目标停止
- **中断机制**：优先使用 GDB/MI `-exec-interrupt`；Windows 上自动回退到 OpenOCD telnet halt
- **RTT 非致命**：固件不支持 RTT 时，调试会话照常运行，RTT 功能不可用但不影响其他操作
- **超时控制**：普通 GDB 命令 30 秒超时，`load` 120 秒，flash 180 秒
- **路径处理**：Windows 路径自动转换为正斜杠（OpenOCD 兼容）

## 项目结构

```
openocd_mcp/
├── __init__.py       # 包入口
├── __main__.py       # python -m 入口
├── server.py         # MCP 工具定义 + main() 入口
├── config.py         # GlobalConfig, ProjectConfigManager
├── openocd.py        # OpenOCDController
├── gdb_mi.py         # GDBMISession (MI2 异步协议)
├── rtt.py            # RTTClient (TCP 日志读取)
└── session.py        # DebugSessionManager
```

## 文档

- [接口定义](接口定义.md) — 所有 MCP 工具的输入/输出详细说明
- [架构设计](架构设计.md) — 组件图、数据流、模块职责
- [RTT 特性](RTT特性.md) — RTT 实时日志功能设计
- [AGENTS.md](AGENTS.md) — AI Agent 快速参考指南

## License

MIT



