# Agent Local Sync — 多 AI Agent 对话记录本机同步方法

## 一句话

轮流使用多个 AI agent 时，对话记录自动跟着走。纯本地、零服务端、断网可跑、关闭 agent 时触发同步。

## 痛点

你同时用 WorkBuddy、DSH、Qoder（或更多 agent）处理任务。每次切换 agent，新 agent 里没有上一个的对话历史。手动导出/导入太麻烦，云同步方案（如 agentctxsync 服务端）对单机场景过度设计。

## 核心方法

```
┌────────────────────────────────────────────────────────┐
│  sync.py（调度器，无服务端，无网络）                     │
│                                                        │
│  1. 读取各 agent 的本地存储（适配器模式）               │
│  2. 按 canonical ID 去重，取并集                       │
│  3. 只写增量（不删只加）                               │
│  4. 权威源优先（冲突时以指定 agent 为准）               │
│                                                        │
│  触发方式：进程退出事件（watcher 检测）                 │
│  面板：localhost WebUI 查看同步状态                     │
└────────────────────────────────────────────────────────┘
```

### 关键设计决策

| 决策 | 理由 |
|---|---|
| 不用服务端/数据库 | 单机场景，多一个服务多一个挂的环节 |
| 适配器模式 | 每个 agent 存储格式不同，统一接口 read_sessions / write_sessions |
| 事件驱动（进程退出）| 不是定时轮询；你关掉 A 打开 B 时，B 里已有 A 的对话 |
| 不删只加 | 安全；本地删除不当删除信号 |
| 权威源 | 指定一个 agent 为基准，冲突时它的版本胜出 |
| 过滤自动化 | 签到/巡检等机器产生的会话不参与同步 |
| ID 映射 | 同一对话在不同 agent 中可能有不同 ID，用映射表去重 |

### 各 agent 存储格式（已验证）

| Agent | 存储位置 | 格式 | 写入约束 |
|---|---|---|---|
| WorkBuddy | `~/.workbuddy/projects/<slug>/*.jsonl` + `workbuddy.db` | JSONL + SQLite | append-only；需重启显示 |
| DSH | `<home>/sessions/--<slug>--/<id>/session.v4.jsonl.zstd` | zstd 压缩事件日志 | 需关闭；含 workspace.json + projcache |
| Qoder | `%APPDATA%/com.qodercn.app.stable/main.sqlite` | SQLite (WAL) | 运行中可写；需 payload_hash + schema_version |
| 其他 agent | 见 docs/adding-agent.md | 自行实现适配器 | — |

## 文件说明

```
agent-local-sync/
├── README.md              ← 你正在看的
├── sync.py                ← 三方同步器（案例实现）
├── sync_watcher.py        ← 进程退出检测 → 自动触发
├── panel.py               ← WebUI 面板 (localhost:19528)
├── config.example.json    ← 路径配置模板
├── qoder_id_map.json      ← ID 映射示例
├── docs/
│   ├── architecture.md    ← 架构设计详解
│   ├── adding-agent.md    ← 如何添加新 agent 适配器
│   └── IMPORT-RULES.md    ← Qoder「WorkBuddy 数据导入」校验规则逆向
└── adapters/              ← 适配器接口说明（引用 agentctxsync MIT）
    └── INTERFACE.md
```

## 快速开始（以 WB + DSH + Qoder 为例）

1. 安装依赖：`pip install zstandard`
2. 获取适配器：`git clone https://github.com/westsource/agentctxsync`（MIT），将 `mcp/adapters/` 放到本目录
3. 编辑 `config.example.json` → `config.json`，填入你的路径
4. 关闭 WB 和 DSH
5. `python sync.py --apply`
6. 打开 WB / DSH / Qoder 验证
7. 可选：`python panel.py` 打开面板；`python sync_watcher.py` 后台常驻

## 扩展到更多 agent

见 `docs/adding-agent.md`。核心：实现一个适配器类，提供 `read_sessions()` 和 `write_sessions()`，返回统一的 canonical 格式。sync.py 的调度逻辑不需要改。

## 许可

方法本身无限制。代码中引用的 agentctxsync 适配器为 MIT 许可。
