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

## MCP Tools (MVP)

| Tool | Purpose |
|---|---|
| `set_project(project_dir)` | Load `.vscode/launch.json` from a project |
| `refresh_debug_targets()` | Reload launch.json configs |
| `flash_download(config_name, firmware_path?)` | One-shot flash via OpenOCD `program` |
| `debug_start(config_name, firmware_path?)` | Start OpenOCD + GDB, load firmware, optionally run to entry point |
| `debug_stop()` | Kill OpenOCD & GDB |
| `debug_command(command)` | Send arbitrary GDB command to active session |
| `debug_status()` | JSON with session state, PIDs, available configs |
| `get_runtime_config()` | Show effective OpenOCD/GDB paths and their sources |

## Essential Conventions

- **Config source**: `config.json` in CWD — supports `openocd_path`, `gdb_path`, `openocd_scripts`, `armToolchainPath`.
- **Env vars**: `OPENOCD_PATH`, `GDB_PATH`, `OPENOCD_SCRIPTS`.
- **Project requirement**: Target project must have `.vscode/launch.json` with `configFiles` (OpenOCD scripts) and `executable`/`program` (firmware ELF).
- **Session model**: At most **one** active debug session. `debug_start` auto-stops any previous session.
- **GDB timeout**: Default command timeout is 30s; `load` uses 120s; flash uses 180s.
- **Run → Inspect → Run loop**: Use `debug_continue()` to resume the target asynchronously — it returns immediately via MI `^running`. Use `debug_interrupt()` to pause the target via MI `-exec-interrupt` at any time. No polling or timeout issues.
- **Interrupt mechanism (`debug_interrupt`)**: Uses **GDB/MI `-exec-interrupt`** — sends the command via stdin pipe, GDB responds with `^done` and pushes `*stopped` event. Clean, reliable, cross-platform. No signal hacks or telnet fallback needed.

## RTT (Not Yet Implemented)

RTT real-time logging is **not in MVP**. Design doc: [`RTT特性.md`](RTT特性.md). Do not expose `read_rtt` or related features until explicitly implemented.

## Detailed Docs

- [Architecture Design](架构设计.md) — component diagram, data flows, module responsibilities
- [API Reference](接口定义.md) — input/output schemas, error examples for every tool
- [RTT Feature Design](RTT特性.md) — future RTT logging capability

## Common Pitfalls

- Windows paths use `\\` or `/` — OpenOCD expects forward slashes for `program` paths (already handled via `.replace("\\", "/")`).
- `launch.json` may contain comments/trailing commas — the custom parser strips them.
- OpenOCD startup waits for port `:3333` with a 15s timeout. If it fails, the process's stderr is captured as the error reason.
- GDB error detection checks for keywords: `error`, `failed`, `cannot`, `connection timed out`, etc.
