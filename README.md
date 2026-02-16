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

如果直接运行：

```bash
uv run openocd-mcp
```

服务会自动读取当前目录 `config.json`（若存在）中的路径配置。
参数优先级：命令行参数 > 环境变量 > `config.json` > 内置默认值。

### 启动为 SSE/HTTP（给本地其他 AI 客户端）

默认是 `stdio`。

```bash
uv run openocd-mcp -sse
```

默认监听：`http://127.0.0.1:9000/sse`

可自定义：

```bash
uv run openocd-mcp -sse --host 127.0.0.1 --port 9000 --path /sse
```

也可通过环境变量配置：

- `OPENOCD_PATH`
- `GDB_PATH`
- `OPENOCD_SCRIPTS`

## VS Code MCP 配置

已提供 `.vscode/mcp.json`，使用 `stdio + uv run openocd-mcp` 启动服务。

如需改为 SSE，可使用：

```json
{
	"servers": {
		"openocd-mcp": {
			"type": "sse",
			"url": "http://127.0.0.1:9000/sse"
		}
	},
	"inputs": []
}
```

## 工具列表（MVP）

- `set_project(project_dir)`
- `refresh_debug_targets()`
- `flash_download(config_name, firmware_path?)`
- `debug_start(config_name, firmware_path?)`
- `debug_stop()`
- `debug_command(command)`
- `debug_status()`
- `get_runtime_config()`

## 文档

- 接口定义：`接口定义.md`
- 架构设计：`架构设计.md`
- RTT 特性（非 MVP）：`RTT特性.md`



