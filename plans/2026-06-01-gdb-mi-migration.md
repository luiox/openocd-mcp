# GDB/MI 异步架构迁移规划

日期: 2026-06-01
状态: 规划中

---

## 1. 动机

### 当前架构的问题

当前 `GDBController` 使用 GDB **Console 模式**（`--quiet`），通过 stdin/stdout 管道发送 CLI 命令并等待 `(gdb)` 提示符。这个模式有根本性的缺陷：

| 问题 | 表现 | 根因 |
|------|------|------|
| **`continue` 阻塞** | 发完 `continue` 后 GDB 不再响应新输入，直到断点命中 | GDB Console 模式下 `continue` 是前台命令，阻塞 stdin |
| **中断依赖 OpenOCD 绕路** | 暂停需要通过 telnet halt 硬件层打断，再由 GDB 被动检测 | Console 模式无法在目标运行时发送 `Ctrl+C` 到管道 |
| **30 秒超时限制** | 程序运行 30 秒后命令超时返回 | `_wait_for_prompt` 有超时，无法无限等待 |
| **平台差异大** | Windows 上 `\x03` 无效，`CTRL_BREAK_EVENT` 杀死 GDB | 管道模式的中断信号处理不可靠 |

### GDB/MI 方案的优势

GDB/MI（Machine Interface）是 GDB 专门为 IDE/工具链设计的**异步机器接口**：

| 能力 | Console 模式 | MI 模式 |
|------|-------------|---------|
| `continue` 后是否响应输入 | ❌ 阻塞，不响应 | ✅ 立即返回 `^running`，继续响应命令 |
| 中断运行中的目标 | ❌ 需要绕路 OpenOCD halt | ✅ `-exec-interrupt` 随时可发 |
| 事件通知 | ❌ 需要轮询 | ✅ 异步推送 `*stopped`/`*running` 事件 |
| 命令超时 | ❌ 有 30 秒超时限制 | ✅ 无超时，事件驱动 |
| 跨平台可靠性 | ❌ Windows 不稳定 | ✅ 标准协议，跨平台一致 |

---

## 2. GDB/MI 协议速览

MI 是**行协议**，每行一个记录：

```
token^result-class[,result-param=value]...      ← 结果记录
token*async-class[,async-param=value]...         ← 异步执行记录
token=notify-class[,notify-param=value]...       ← 通知记录
~"console-stream-output\n"                        ← 控制台流
&"log-stream-output\n"                            ← 日志流
```

### 关键命令映射

| Console 命令 | MI 命令 | 说明 |
|-------------|---------|------|
| `target remote :3333` | `-target-select remote :3333` | 连接目标 |
| `load` | `-target-download` | 下载固件 |
| `continue` | `-exec-continue` | 继续执行（立即返回 `^running`） |
| `Ctrl+C` | `-exec-interrupt` | 中断执行 |
| `break <loc>` | `-break-insert <loc>` | 设置断点 |
| `tbreak <loc>` | `-break-insert -t <loc>` | 设置临时断点 |
| `delete` | `-break-delete` | 删除断点 |
| `print <expr>` | `-data-evaluate-expression <expr>` | 计算表达式 |
| `info registers` | `-data-list-register-values x` | 读取寄存器 |
| `bt` | `-stack-list-frames` | 栈回溯 |
| `next` | `-exec-next` | 单步跳过 |
| `step` | `-exec-step` | 单步进入 |
| `set pagination off` | `-gdb-set pagination off` | 设置参数 |

### 异步事件

当目标状态变化时，GDB 主动推送：

```
*running,thread-id="all"                  ← 目标开始运行
*stopped,reason="breakpoint-hit",...      ← 断点命中
*stopped,reason="signal-received",...     ← 收到信号
*stopped,reason="watchpoint-trigger",...  ← 监视点触发
```

---

## 3. 架构变更方案

### 3.1 文件结构

```
openocd_mcp/
├── __init__.py          # 导出 main
├── __main__.py          # python -m 入口（不变）
├── server.py            # MCP 工具定义 + main() 入口（精简）
├── config.py            # GlobalConfig, ProjectConfigManager（从 server.py 抽出）
├── openocd.py           # OpenOCDController（从 server.py 抽出）
├── gdb_mi.py            # 全新：GDB/MI 控制器
└── session.py           # DebugSessionManager（从 server.py 抽出）
```

### 3.2 GDB/MI 控制器设计（新核心）

```
class GDBMISession:
    """GDB/MI 会话，管理与单个 GDB 进程的异步通信。"""

    - _process: subprocess.Popen       # GDB 进程
    - _state: "stopped" | "running"   # 目标运行状态
    - _output_queue: queue.Queue       # MI 事件/输出队列
    - _reader_thread: Thread           # 持续读取 stdout 的行读取器
    - _pending: dict[int, Future]      # 未完成的命令 token → Future 映射
    - _token_counter: int              # 自增命令 token

    方法:
    + start(firmware_path, gdb_port)    # 启动 GDB，连接，下载
    + send_mi(mi_command) -> str        # 发送 MI 命令，等待结果
    + continue(target: bool)            # 继续执行（立即返回）
    + interrupt()                       # 中断执行（即时生效）
    + stop()                            # 停止 GDB
    + get_state() -> str                # 获取目标状态
    + wait_for_stop(timeout) -> str     # 等待 *stopped 事件

    内部:
    - _reader_loop()                    # 行读取+解析线程
    - _parse_line(line)                 # 解析单行 MI 输出
    - _handle_result(token, klass, params)  # 处理结果记录
    - _handle_async(klass, params)      # 处理异步记录
    - _next_token() -> int              # 生成下一个 token
```

### 3.3 命令流程对比

#### 当前（Console 模式）

```
用户: "运行"
  → debug_command("continue")
  → 写入 stdin: "continue\n"
  → _wait_for_prompt(30s)  ← 阻塞！GDB 不再响应
  → 超时 → "target is running"
  → GDB 仍然阻塞

用户: "暂停"
  → debug_interrupt()
  → _interrupt_via_openocd()  ← 绕路 OpenOCD telnet halt
  → GDB 检测到目标停止 → 恢复提示符
  → 成功
```

#### 新架构（MI 模式）

```
用户: "运行"
  → debug_continue()
  → 写入 stdin: "-exec-continue\n"
  → GDB 立即回复: "^running"
  → 解析到 ^running → 返回 "(running)"
  → GDB 继续响应其他命令  ← 关键！不阻塞

用户: "暂停"
  → debug_interrupt()
  → 写入 stdin: "-exec-interrupt\n"
  → GDB 立即处理中断 → 发送 Ctrl+C 到 OpenOCD
  → GDB 推送: "*stopped,reason='signal-received'"
  → 解析到 *stopped → 返回 "Target stopped"
  → 不再需要 OpenOCD telnet 绕路
```

### 3.4 事件驱动 vs. 轮询

```
当前架构 (同步轮询):
  send_command("continue")
    → _wait_for_prompt(timeout=30)
    → 每秒轮询 20 次检查 (gdb) 标识
    → 30 秒后超时
    → 需要主动中断才能恢复

新架构 (事件驱动):
  send_mi("-exec-continue")
    → GDB 返回 ^running → 立即返回，不等待
    → 后台行读取器持续监听 stdout
    → 当 GDB 推送 *stopped 时:
      → 存入事件队列
      → 可通过 get_last_event() 查询
      → 或通过 wait_for_stop() 主动等待
```

### 3.5 工具接口变更

| 当前工具 | 新工具 | 变更说明 |
|---------|--------|---------|
| `debug_command(command)` | `debug_command(command)` | 不变，内部改为 MI 命令 |
| `debug_interrupt()` | `debug_interrupt()` | 不再需要 OpenOCD telnet，直接 `-exec-interrupt` |
| 无 | `debug_continue()` | **新增**：专用继续运行工具，返回后立即响应 |
| 无 | `debug_state()` | **新增**：查询目标当前状态 (running/stopped) 及停止原因 |

> **兼容性策略**：`debug_command("continue")` 自动映射为 `-exec-continue`，行为不变。

---

## 4. 实施计划

### 阶段 1：创建新 GDB/MI 模块（当前 sprint）

1. 创建 `gdb_mi.py` — GDB/MI 控制器完整实现
   - 进程管理、行读取器、MI 解析器
   - 命令 token 与 Future 映射
   - 异步事件处理

2. 创建 `config.py` — 从 `server.py` 抽出配置管理器

3. 创建 `openocd.py` — 从 `server.py` 抽出 OpenOCD 控制器

4. 创建 `session.py` — 从 `server.py` 抽出会话管理器

5. 重写 `server.py` — 精简为 MCP 工具定义 + main 入口

### 阶段 2：工具层适配

6. 新增 `debug_continue()` 工具
7. 新增 `debug_state()` 工具
8. 修改 `debug_interrupt()` — 改为 MI 方式
9. 修改 `debug_command()` — 兼容 Console/MI 命令

### 阶段 3：测试与验证

10. 启动调试会话
11. 验证 continue 后立即响应
12. 验证 interrupt 即时生效
13. 验证断开点、打印变量等常规操作
14. 验证 Windows/Unix 双平台兼容性

---

## 5. 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| GDB/MI 在某些 GDB 版本上行为不一致 | 保留 Console 模式作为 fallback |
| MI 解析器遇到非标准输出 | 实现容错解析，忽略无法识别的行 |
| `-target-download` 进度指示 | MI 模式下进度通过 `+download` 事件报告，忽略即可 |
| 需要同时维护两套 GDB 通信方式 | 通过接口抽象隔离，策略模式切换 |

---

## 6. 预期效果

迁移完成后：

- **运行/暂停** 如同 VS Code 原生调试一样流畅
- `continue` 不再阻塞 AI，可以立即查询状态或设置断点
- `interrupt` 直接通过 GDB 协议完成，无需 OpenOCD telnet 绕路
- 不再需要 30 秒超时 hack
- 代码结构更清晰：模块分离、职责单一
