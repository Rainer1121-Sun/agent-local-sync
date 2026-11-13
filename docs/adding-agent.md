# 如何添加新 Agent 适配器

## 统一接口

每个适配器是一个 Python 类，提供两个核心方法：

```python
class MyAgentAdapter:
    def discover(self) -> str:
        """返回该 agent 的本地存储根目录"""
        ...

    def read_sessions(self) -> list[dict]:
        """读取所有会话，返回 canonical 格式列表"""
        ...

    def write_sessions(self, sessions: list[dict]) -> dict:
        """写入会话，返回 {"imported": N, "new_messages": N, ...}"""
        ...
```

`write_sessions` 有一条硬要求：**按 id 合并，而不是按 id 覆盖**。sync.py 补尾部时会构造一个只含新消息的"尾巴会话"（同 id、同 cwd、同 title，`messages` 只有新增那几条）喂给它，指望存储端把这几条追加进已有记录。如果实现成"整条替换"，尾部追加会把目标端已有的历史抹掉。

## Canonical 会话格式

```python
{
    "id": "uuid-string",           # 会话唯一 ID
    "title": "对话标题",
    "cwd": "/path/to/workspace",   # 工作目录
    "started_at": 1794331931.554,  # Unix 秒（float）
    "last_activity_at": 1794501306.808,
    "messages": [
        {
            "role": "user" | "assistant" | "tool" | "system",
            "content": "消息文本",
            "timestamp": 1794331932.0,  # Unix 秒
            # 可选：
            "tool_name": "...",
            "tool_call_id": "...",
        },
        ...
    ],
    "message_count": 42,
}
```

## 实现步骤

1. **找到存储位置**：agent 的对话存在哪里？（SQLite / JSONL / JSON / 私有格式）
2. **实现 read_sessions**：解析存储 → 转为 canonical 格式
3. **实现 write_sessions**：canonical 格式 → 写入原生格式，同 id 追加不覆盖
4. **处理写入约束**：
   - 需要关闭进程？→ 加进 sync.py 的 `running` 门禁字典（进程名比较必须转小写）
   - 运行中可写但有内存态风险？→ 像 Qoder 那样整体推迟，见 `qoder_open`
   - 需要重启才显示？→ 文档说明
   - 有索引/缓存/元数据表需同步更新？→ 在 write 中一并处理
5. **注册到 sync.py**：`build_stores()` 里读进 `stores`，`TRANSFORMS` 里给出写前变换（例如 tool 消息折叠），`ROLES_LOST` 里登记该端装不下的角色，写入分支加进 `main()` 的 apply 段
6. **确认幂等**：同一批 tail 连跑两次，第二次必须写 0 条

## 已有适配器参考

agentctxsync 项目（MIT）已实现 7 个 agent 的适配器：
- WorkBuddy、DSH、Hermes、OpenCode、Reasonix、OpenClaw、Oh My Pi

仓库：github.com/westsource/agentctxsync → `mcp/adapters/`

如果你的 agent 已在其中，直接引用即可。否则参照 `adapters/_template.py` 实现。

## 注意事项

- 时间戳统一为 Unix 秒（float），毫秒需 /1000
- role=tool 的消息在写入不支持 tool 的 agent 时，折叠为 assistant
- **去重靠内容指纹，不靠时间戳**：`(role, sha1(text)[:16])`。跨端写同一句话时时间戳会变，用时间戳判重必然重复写
- 该端表达不了的角色要登记，否则那条消息每轮都进计划，差异永远归不了零；登记后它会从计划剔除并单独上报为「物理装不下」
- ID 如果跨 agent 不一致，需要维护映射表（格式参考 qoder_id_map.json；本仓库里那份是合成示例）
- 自动化会话通过标题模式过滤，不参与同步，也不算差异
- 任何 subprocess 调用都带 `creationflags=CREATE_NO_WINDOW`：watcher/面板从 `pythonw` 启动时没有控制台，子进程会新开一个窗口闪一下
