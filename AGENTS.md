# openocd-mcp — AI Agent Guide

## What This Project Does

An MCP server (via `fastmcp`) that exposes OpenOCD flashing & debugging workflows through natural-language-callable tools. It reuses the project's `.vscode/launch.json` as the source of debug targets — no extra config needed.

## Quick Start

```bash
# Install dependencies
uv sync

# Run with custom tool paths
uv run openocd-mcp --openocd-path openocd --gdb-path arm-none-eabi-gdb

# Run in SSE/HTTP mode (for local AI clients)
uv run openocd-mcp -sse --host 127.0.0.1 --port 9000
```

Parameter priority: CLI args > env vars > `config.json` > built-in defaults.

## Key Architecture

| Module | Responsibility |
|---|---|
| `ProjectConfigManager` | Parses `.vscode/launch.json`, resolves `${workspaceFolder}`, caches configs |
| `OpenOCDController` | Spawns OpenOCD for flash (`program`) or as GDB server (target remote :3333) |
| `GDBMISession` | GDB/MI 异步会话，协议解析、事件驱动、无需轮询提示符 |
| `DebugSessionManager` | Single active session; coordinates OpenOCD + GDB lifecycle |

- All tools return strings. Failures are prefixed with `"Error: "`.
- `launch.json` supports JSON with C-style comments and trailing commas (custom parser in `_parse_launch_content`).

## MCP Tools

| Tool | Purpose |
|---|---|
| `set_project(project_dir)` | Load `.vscode/launch.json` from a project |
| `refresh_debug_targets()` | Reload launch.json configs |
| `flash_download(config_name, firmware_path?)` | One-shot flash via OpenOCD `program` |
| `debug_start(config_name, firmware_path?)` | Start OpenOCD + GDB, load firmware, optionally run to entry point |
| `debug_stop()` | Kill OpenOCD & GDB |
| `debug_command(command)` | Send arbitrary GDB command to active session |
| `debug_continue()` | Continue target execution (async, returns immediately via MI `^running`) |
| `debug_interrupt()` | Interrupt/pause running target (MI `-exec-interrupt`, Windows fallback to telnet halt) |
| `debug_state()` | Get target execution state (running/stopped) and last stop reason |
| `debug_status()` | JSON with session state, PIDs, available configs |
| `get_runtime_config()` | Show effective OpenOCD/GDB paths and their sources |
| `read_rtt(max_lines)` | Read RTT log from active debug session |
| `shutdown()` | Gracefully shut down MCP server |

## Essential Conventions

- **Config source**: `config.json` in CWD — supports `openocd_path`, `gdb_path`, `openocd_scripts`, `armToolchainPath`, `rtt_port`, `adapter_speed`.
- **Env vars**: `OPENOCD_PATH`, `GDB_PATH`, `OPENOCD_SCRIPTS`, `RTT_PORT`.
- **Project requirement**: Target project must have `.vscode/launch.json` with `configFiles` (OpenOCD scripts) and `executable`/`program` (firmware ELF).
- **Session model**: At most **one** active debug session. `debug_start` auto-stops any previous session.
- **GDB timeout**: Default command timeout is 30s; `load` uses 120s; flash uses 180s.
- **Run → Inspect → Run loop**: Use `debug_continue()` to resume the target asynchronously — it returns immediately via MI `^running`. Use `debug_interrupt()` to pause the target via MI `-exec-interrupt` at any time. No polling or timeout issues.
- **Interrupt mechanism (`debug_interrupt`)**: Uses **GDB/MI `-exec-interrupt`** as primary. On Windows, falls back to **OpenOCD telnet halt** (port 4444) since Windows pipe interrupt is unreliable.

## RTT (Implemented)

RTT real-time logging is implemented. Use `read_rtt(max_lines=10)` during an active debug session to read logs from the MCU. RTT is automatically started on `debug_start` if the firmware has SEGGER RTT support compiled in.

- `read_rtt(max_lines)` — Read up to `max_lines` lines of RTT log output
- RTT status is shown in `debug_start` output and `debug_status` JSON (`rtt_connected`)
- RTT connection is automatically cleaned up on `debug_stop`
- Default RTT port: 8888 (configurable via `--rtt-port` CLI arg, `RTT_PORT` env var, or `config.json` `rtt_port` field)
- RTT is non-fatal: if the firmware doesn't support RTT, the debug session continues without it

## Detailed Docs

- [Architecture Design](架构设计.md) — component diagram, data flows, module responsibilities
- [API Reference](接口定义.md) — input/output schemas, error examples for every tool
- [RTT Feature Design](RTT特性.md) — future RTT logging capability

## Common Pitfalls

- Windows paths use `\\` or `/` — OpenOCD expects forward slashes for `program` paths (already handled via `.replace("\\", "/")`).
- `launch.json` may contain comments/trailing commas — the custom parser strips them.
- OpenOCD startup waits for port `:3333` with a 15s timeout. If it fails, the process's stderr is captured as the error reason.
- GDB error detection checks for keywords: `error`, `failed`, `cannot`, `connection timed out`, etc.
