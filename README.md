# openocd-mcp

基于 `fastmcp` 的 OpenOCD 调试 MCP 服务，复用项目中的 `.vscode/launch.json` 作为调试目标来源。

## 当前阶段（MVP）

- 已实现：项目切换、调试目标刷新、一次性烧录、调试会话启动/停止、GDB 命令执行、状态查询。
- 未实现：RTT 相关能力（已拆分到独立文档）。

## 使用 uv 运行

```bash
uv sync
uv run openocd-mcp --openocd-path openocd --gdb-path arm-none-eabi-gdb
```

也可通过环境变量配置：

- `OPENOCD_PATH`
- `GDB_PATH`
- `OPENOCD_SCRIPTS`

## 工具列表（MVP）

- `set_project(project_dir)`
- `refresh_debug_targets()`
- `flash_download(config_name, firmware_path?)`
- `debug_start(config_name, firmware_path?)`
- `debug_stop()`
- `debug_command(command)`
- `debug_status()`

## 文档

- 接口定义：`接口定义.md`
- 架构设计：`架构设计.md`
- RTT 特性（非 MVP）：`RTT特性.md`



