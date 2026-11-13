# 适配器接口说明

本案例的适配器来自 agentctxsync 项目（MIT 许可）。

## 获取方式

```bash
git clone https://github.com/westsource/agentctxsync.git
```

将 `mcp/adapters/` 目录放到本项目的 `adapters/` 下，或在 config.json 中指定 `adapters_path`。

## 本案例使用的适配器

- `adapters/workbuddy.py` — WorkBuddyAdapter（read_sessions / write_sessions）
- `adapters/dsh.py` — DshAdapter（read_sessions / write_sessions，含 v4 事件生成 + zstd）
- `adapters/base.py` — 公共基类（canonical 格式、ID 工具、去重）

## Qoder 适配器

Qoder 的读写直接内嵌在 sync.py 中（read_qoder_sessions / write_qoder_sessions），
因为 Qoder 的存储是标准 SQLite，无需独立适配器文件。

如果你的 agent 也是 SQLite 存储，参照 sync.py 中的 Qoder 实现即可。

## 接口契约

```python
adapter.discover() -> str          # 存储根目录
adapter.read_sessions() -> list    # canonical 会话列表
adapter.write_sessions(list) -> dict  # {"imported": N, "updated": N, "duplicates": N}
```

详见 docs/adding-agent.md。
