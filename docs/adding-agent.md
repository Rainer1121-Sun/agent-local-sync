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
        """写入会话（增量），返回 {"imported": N, "updated": N, ...}"""
        ...
```

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
            "role": "user" | "assistant" | "tool",
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
3. **实现 write_sessions**：canonical 格式 → 写入 agent 的原生格式
4. **处理写入约束**：
   - 需要关闭进程？→ 在 sync.py 的 process_alive 检查中加入
   - 需要重启才显示？→ 文档说明
   - 有索引/缓存需同步更新？→ 在 write 中一并处理
5. **注册到 sync.py**：在 main() 中加入读取和写入调用

## 已有适配器参考

agentctxsync 项目（MIT）已实现 7 个 agent 的适配器：
- WorkBuddy、DSH、Hermes、OpenCode、Reasonix、OpenClaw、Oh My Pi

仓库：github.com/westsource/agentctxsync → `mcp/adapters/`

如果你的 agent 已在其中，直接引用即可。否则参照 `adapters/_template.py` 实现。

## 注意事项

- 时间戳统一为 Unix 秒（float），毫秒需 /1000
- role=tool 的消息在写入不支持 tool 的 agent 时，折叠为 assistant
- ID 如果跨 agent 不一致，需要维护映射表（参考 qoder_id_map.json）
- 自动化会话通过标题模式过滤，不参与同步
