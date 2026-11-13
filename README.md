# Agent Local Sync — 多 AI Agent 对话记录本机同步方法

## 一句话

轮流使用多个 AI agent 时，对话记录自动跟着走。纯本地、零服务端、断网可跑，同步由「agent 关闭」这个事件触发。

## 痛点

你同时用 WorkBuddy、DSH、Qoder（或更多 agent）处理任务。每次切换 agent，新 agent 里没有上一个的对话历史。手动导出/导入太麻烦，云同步方案（如 agentctxsync 服务端）对单机场景过度设计。

更麻烦的是：同一段对话会在多个 agent 里**继续生长**。所以同步不能只做「缺失就整条搬过去」，还得处理「这条已经在了，但它后面又多了 20 句」。

## 核心方法

```
┌──────────────────────────────────────────────────────────┐
│  sync.py（调度器，无服务端，无网络）                       │
│                                                          │
│  1. 读取各 agent 的本地存储（适配器模式）                 │
│  2. 按 canonical ID 去重，取并集                         │
│  3. 该端没有这条会话 → 整条导入                          │
│     该端已有这条会话 → 只补尾部新增消息（append）        │
│  4. 只往「该 agent 已关闭」的存储写                       │
│                                                          │
│  触发：进程退出事件（watcher）+ 15 分钟心跳兜底           │
│  面板：localhost WebUI 查看与手动触发                     │
└──────────────────────────────────────────────────────────┘
```

### 两种写入语义

| 语义 | 前置条件 | 落盘方式 | 重跑 |
|---|---|---|---|
| **import** | 目标端不存在这条会话 | 适配器新建（WB jsonl+db / DSH v4 事件流 / Qoder sqlite 插行） | 不再命中（会话已存在） |
| **tail** | 目标端已有同 ID 会话 | 只追加「时间戳严格大于目标端最大值，且内容指纹不在目标端」的消息 | 写 0（幂等） |

内容指纹是 `(role, sha1(text)[:16])`，**不含时间戳**——同一句话在不同端的时间戳可能差几毫秒，靠时间戳判重会写重复。

### 关键设计决策

| 决策 | 理由 |
|---|---|
| 不用服务端/数据库 | 单机场景，多一个服务多一个挂的环节 |
| 适配器模式 | 每个 agent 存储格式不同，统一接口 read_sessions / write_sessions |
| 事件驱动（进程退出）| 不是纯轮询；你关掉 A 打开 B 时，B 里已有 A 的对话 |
| 尾部追加而非整条重写 | agent 会在原会话里继续说话；整条复制要么丢消息要么造重复 |
| 写入门禁按进程判定 | 只有目标 agent 关闭时才写它的存储；否则 app 的内存态会在下一次提交时盖掉写入结果 |
| 心跳兜底（900s）| 退出事件可能漏（崩溃、watcher 没在跑），定时全量一轮把差异补平 |
| 子进程隐藏控制台 | 从 `pythonw` 启动时 Windows 会给子进程新开一个控制台，表现为反复一闪的弹窗；所有 subprocess 调用统一加 `CREATE_NO_WINDOW` |
| 不删只加 | 安全；本地删除不当删除信号 |
| 权威源 | 指定一个 agent 为基准，冲突时它的版本胜出 |
| ID 映射 | 原生导入器会给同一对话分配新 ID，用映射表折回原 ID |
| 过滤自动化 | 签到/巡检等机器产生的会话不参与同步，也不算差异 |

### 「物理装不下」与差异口径

各端能表达的消息角色不完全一样：实测 DSH 的 v4 事件流只有 `user` / `assistant`，**没有 `system` 的位置**；WB 和 Qoder 三种角色都有。

所以 sync.py 把这类消息从 tail 计划里剔除（否则同一句话每轮都被重新计划，差异永远降不到 0），并在计划输出和面板里单独报一条 `Physically unwritable`：它不是「还没同步」，而是「这个端存不了」，消息只保留在能承载它的端。差异（drift）指标因此可以做到 0，同时不把丢失藏起来。

### 各 agent 存储格式（已验证）

| Agent | 存储位置 | 格式 | 写入约束 |
|---|---|---|---|
| WorkBuddy | `~/.workbuddy/projects/<slug>/*.jsonl` + `workbuddy.db` | JSONL + SQLite | 进程须关闭；jsonl 追加 + db 元数据同步 |
| DSH | `<home>/sessions/--<slug>--/<id>/session.v4.jsonl.zstd` | zstd 压缩事件日志 | 进程须关闭；含 workspace.json + projcache；无 `system` 角色 |
| Qoder | `%APPDATA%/com.qodercn.app.stable/main.sqlite` | SQLite (WAL) | app 运行中时**整体不写**（导入与尾部一起推迟到它退出那一次）；即便已关闭，仍跳过 N 分钟内被 app 改过的会话。写前备份主库 |
| 其他 agent | 见 docs/adding-agent.md | 自行实现适配器 | — |

## 文件说明

```
agent-local-sync/
├── README.md              ← 你正在看的
├── sync.py                ← 三方同步器（import + tail 两条语义）
├── sync_watcher.py        ← 进程退出检测 → 自动触发 + 心跳兜底
├── panel.py               ← WebUI 面板 (127.0.0.1:19528)
├── config.example.json    ← 各端路径/进程名的参考说明（脚本不读它，见下）
├── qoder_id_map.json      ← ID 映射表的格式示例（合成数据，非真实会话）
├── docs/
│   ├── architecture.md    ← 架构设计详解
│   ├── adding-agent.md    ← 如何添加新 agent 适配器
│   └── IMPORT-RULES.md    ← Qoder「WorkBuddy 数据导入」校验规则逆向
└── adapters/              ← 适配器接口说明（引用 agentctxsync MIT）
    └── INTERFACE.md
```

## 路径来自哪里

三个脚本**不读配置文件**，路径在运行时自动发现；`config.example.json` 只是把默认值和进程名列清楚给人看：

- WorkBuddy：环境变量 `WORKBUDDY_HOME`，否则跟随 app 最近一次启动的 home（一台机器可能同时装了两代 WorkBuddy）
- DSH：环境变量 `DSH_HOME`，否则 `~/.dsh/sessions`
- Qoder：`%APPDATA%/com.qodercn.app.stable/main.sqlite`
- 适配器：`./agentctxsync/mcp`，相对 sync.py 自身目录解析，不硬编码绝对路径

要换位置就改 `sync.py` 顶部常量或设环境变量。

## 快速开始

1. 安装依赖：`pip install zstandard`
2. 获取适配器：在本目录里 `git clone https://github.com/westsource/agentctxsync`（MIT），使 `./agentctxsync/mcp/adapters/` 存在
3. 关闭 WB 和 DSH。Qoder 可以开着，但这一轮不会碰它——它的导入和尾部会整体推迟到 Qoder 退出时由 watcher 落盘（避免和 app 内存里的会话竞争）
4. `python sync.py` — 干跑，打印 union、每端 new sessions / tail msgs、物理装不下清单，不写任何文件
5. `python sync.py --apply` — 先备份再写入；`--limit N` 先小批量试；`--live-guard 5` 额外跳过 5 分钟内被 app 写过的 Qoder 会话
6. 打开三端验证；再跑一次 `--apply`，tail msgs 应为 0（幂等）
7. 常驻：`python sync_watcher.py`；面板：`python panel.py [--port 19528] [--no-browser]`

### Windows 下静默自启

`python.exe` 会带一个黑框，`pythonw.exe` 不会。用启动目录里的 `.vbs` 拉起：

```vbs
' %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\AgentSyncWatcher.vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw.exe ""D:\path\to\sync_watcher.py""", 0, False
```

两个坑：`pythonw` 下 `sys.stdout` 是 `None`，sync.py / panel.py 已做兜底（落到 devnull），否则第一句 print 就崩；`.vbs` 按本地编码读取，路径里有非 ASCII 时要么保证保存编码一致，要么改用短路径。

## 面板

- 纯 stdlib `http.server`，绑 `127.0.0.1:19528`，无外部依赖、不联网
- 顶部：union、每端覆盖、待补 tail 消息数、物理装不下条数（灰色，不计入差异）
- 每行一个会话，三端点灯；30s 自动刷新 + 「立即同步」按钮（按钮内部跑 `sync.py --apply`，同样带 `CREATE_NO_WINDOW`）

## 验证过什么

三条 tail 通道都跑过完整闭环：WB 在一次性 scratch home 上追加 26 条 → 剩余 0 → 重放写 0；Qoder 在 87 MB 库副本上 15 会话 433 条 → 剩余 0 → 重放写 0；DSH 真实库收敛且重放为 0。watcher 的退出/心跳判定与 tasklist 实测一致（进程名比较不区分大小写——否则「开着」会被看成「关着」，直接写进活存储）。弹窗问题用时钟分桶的 A/B 采样复现，并确认 `CREATE_NO_WINDOW` 后为 0 次。

## 扩展到更多 agent

见 `docs/adding-agent.md`。核心：实现一个适配器类，提供 `read_sessions()` 和 `write_sessions()`，返回统一的 canonical 格式，sync.py 的调度逻辑不用改。如果新端表达不了某种消息角色，在 `ROLES_LOST` 里登记，它会从计划里剔除并单独上报，而不是永远算成待同步。

## 许可

方法本身无限制。代码中引用的 agentctxsync 适配器为 MIT 许可。
