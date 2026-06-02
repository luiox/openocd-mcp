## RTT 特性设计

本文档描述 openocd-mcp 中与 RTT（Real-Time Transfer）相关的接口与架构设计。

当前状态：**已实现**（对应 2026-06-02 版本）。

---

### 1. 适用范围与启用时机

- 当前状态：**不纳入 MVP**。
- 启用目标：在现有烧录/调试能力稳定后，引入实时日志读取能力。
- 设计原则：不破坏现有工具语义，RTT 仅作为调试会话的可选增强。

---

### 2. 全局配置（RTT 相关）

当 RTT 特性启用时，增加以下全局参数：

| 参数         | 类型    | 默认值 | 说明                       |
| ------------ | ------- | ------ | -------------------------- |
| `--rtt-port` | integer | `8888` | RTT 服务器监听的端口号     |

---

### 3. 工具接口增量

#### 3.1 `read_rtt`
- **描述**：从当前调试会话的 RTT 连接中读取日志。最多返回指定行数，默认 10 行。会话必须已启动且 RTT 已连接。
- **输入**：
  ```json
  {
    "type": "object",
    "properties": {
      "max_lines": {
        "type": "integer",
        "description": "最多返回的行数，默认 10",
        "default": 10
      }
    }
  }
  ```
- **成功返回示例**：
  ```
  RTT log:
  Hello from MCU!
  Counter: 1
  Counter: 2
  ```
- **失败返回示例**：
  ```
  Error: No active debug session or RTT not enabled.
  ```
  ```
  Error: RTT connection lost.
  ```

#### 3.2 `debug_start`（RTT 增强）
- 现有 `debug_start` 在启用 RTT 时，额外执行：
  - OpenOCD 端启用 RTT 相关配置；
  - 建立 RTT TCP 连接；
  - 返回信息中附带 RTT 连接状态。

启用 RTT 时可在返回中包含：
```
RTT connected on port 8888
```

#### 3.3 `debug_stop`（RTT 增强）
- 启用 RTT 时，`debug_stop` 除关闭 OpenOCD 与 GDB 外，还需关闭 RTT 连接。

#### 3.4 `debug_status`（RTT 增强）
- 启用 RTT 时，状态 JSON 可包含字段：
  - `rtt_connected: boolean`

示例：
```json
{
  "session_active": true,
  "config_name": "Debug mbootloader",
  "firmware": "/home/user/myproject/build/artifacts/mbootloader.elf",
  "openocd_pid": 12345,
  "gdb_pid": 12346,
  "rtt_connected": true,
  "project_dir": "/home/user/myproject",
  "available_configs": ["Debug i2c_master_demo", "Debug i2c_slave_demo", "Debug mbootloader"]
}
```

---

### 4. 架构增量设计

#### 4.1 新增模块：RTT 客户端
- **职责**：连接 OpenOCD 开启的 RTT TCP 端口，读取实时日志。
  - **连接**：`connect(host, port)`
  - **读取**：`read_lines(max_lines)`
  - **关闭**：`close()`
- **实现建议**：使用 `socket` 或 `asyncio.open_connection`，按行缓冲并支持断连检测。

#### 4.2 调试会话管理器扩展
- `start_session(...)`：在 OpenOCD/GDB 启动后尝试连接 RTT。
- `stop_session()`：关闭 RTT socket 并清理状态。
- `get_status()`：暴露 RTT 连接状态。
- 会话状态中新增：
  - `rtt_socket`
  - `rtt_port`

#### 4.3 OpenOCD 控制器扩展
- `start_server(config_files, enable_rtt)`：RTT 启用时加载 RTT 配置（如额外脚本或命令）。
- 保持工作目录为项目目录，确保相对脚本路径可解析。

---

### 5. 数据流（RTT 启用时）

1. AI 调用 `debug_start`。
2. 调试会话管理器启动 OpenOCD 与 GDB。
3. 调试会话管理器连接 RTT 端口并保存连接状态。
4. AI 调用 `read_rtt` 获取最新日志。
5. AI 调用 `debug_stop`，会话管理器关闭 RTT 与子进程。

---

### 6. 与 MVP 文档关系

- `接口定义.md`：仅保留 MVP 能力（不含 RTT）。
- `架构设计.md`：仅保留 MVP 模块与流程。
- 本文档：承载 RTT 全部设计，供后续版本启用时回填。
