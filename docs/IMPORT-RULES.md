# Qoder 桌面版「WorkBuddy 数据导入」规则导出

导出时间：2026-11-13 　适用版本：Qoder CN 0.5.7（`D:/sofeware/Qoder CN/resources/app.asar`）
来源：`app.asar` → `/out/main/electronMain.js` 反混淆代码 + `sessionMigration.sqlite` 台账实测
配套脚本：同目录 `diagnose_structure.py`（校验器复现）、`repair_qids.py`（修复器）

---

## 0. 一句话结论

导入器对每条 WorkBuddy 会话做三级校验：**文件级 → 记录级 → 消息树级**，再加一道
**Git 执行位置校验**。任何一级不过就整条会话拒绝导入，并写入失败原因码。
其中把消息树判成「有环」是本次 6+2 条会话失败的真正根因。

---

## 1. 数据来源与读取路径

| 项 | 值 | 代码依据 |
|---|---|---|
| 会话正文 | `C:/Users/Rain/.workbuddy/projects/<工作区slug>/<sessionId>.jsonl` | 逐行流式读取 `Eqx()` |
| 会话元数据 | `C:/Users/Rain/.workbuddy/workbuddy.db` 的 `sessions` 表 | 候选快照 `candidate_snapshot` |
| 中间副本 | 导入前先把 jsonl **原子复制**到临时目录再解析 | `QEe()` |
| 落地台账 | `%APPDATA%/com.qodercn.app.stable/sessionMigration.sqlite` | `migration_ledger` / `import_job` / `import_job_item` |
| 目标库 | 同目录 `main.sqlite`（`chat_sessions` / `chat_session_messages` / `workspaces`） | — |

---

## 2. 文件级规则

1. 源文件必须是普通文件、非符号链接、大小 > 0 且 ≤ **512 MiB**（`qK0 = 512*1024*1024`），超限 → `WORKBUDDY_RECORD_TOO_LARGE`。
2. 复制过程中校验 inode / mtime / size / ctime 一致，读取前后各校验一次；总读出字节数必须等于源文件大小。源在导入期间被改动 → `WORKBUDDY_SOURCE_CHANGED`。
3. **单行上限 2 MiB**（`kEe = 2*1024*1024`）。单行超限或该行不是 JSON 对象 → `WORKBUDDY_RECORD_TOO_LARGE` / `WORKBUDDY_JSON_INVALID`。
4. 逐行按 UTF-8 解码；空行跳过；末行允许无换行符（读文件末尾 1 字节判断是否为 `\n`/`\r` 来决定是否保留解析错误）。
5. 整个文件 0 行，或解析后 0 条可用记录 → `WORKBUDDY_STRUCTURE_INVALID`。

---

## 3. 记录级规则

只有下列 `type` 参与导入，其余一律跳过（跳过数计入 `skippedRecordCount`）：

```
message  ·  reasoning  ·  function_call  ·  function_call_result
```

- 另有一条「清单记录」（会话标题所在记录）被单独提取为 `aiTitle`，不算业务记录。
  （该 type 的字面量同属混淆字符串，未静态取证；行为上表现为"只提标题、不进消息树"。）
- 每条记录的字段按 `id / parentId / editedUserItemId / callId` 读取，全部走 `typeof === 'string'` 后 `trim()`，非字符串一律当作空串。

| 规则 | 判定 | 失败码 |
|---|---|---|
| R1 必须有 id | 被解析类型的记录若 `id` 为空 → 拒绝 | `WORKBUDDY_STRUCTURE_INVALID` |
| R2 id 重复策略 | 同 id 再次出现时，**仅允许 message ↔ reasoning 配对**；同类型重复或与其他类型重复 → 拒绝 | `WORKBUDDY_STRUCTURE_INVALID` |
| R3 同 id 取哪条 | 内存索引表 `latest`：**新 id 入库；已有 id 只有当新记录是 `message` 时才覆盖** | — |
| R4 父指针 | `parentId` 为空时回退 `logicalParentId`；两者皆空 = 根 | — |
| R5 editedUserItemId | 必须能在 `latest` 里找到目标，且目标行的 `index` **大于**本记录行号（即编辑记录必须排在被编辑消息之后），否则拒绝 | `WORKBUDDY_STRUCTURE_INVALID` |
| R6 图片配对 | 同一 `callId` 在 `message` 与 `reasoning` 中各出现恰好 1 次才算「成对」，成对才保留其图片路径；不成对计入跳过 | — |

---

## 4. 消息树规则（本次踩坑处）

1. **作废传播**：以每条 `editedUserItemId` 指向的行为基准，沿父链把「最大编辑行号」向上传播，得到每个 id 的作废水位 `level[id]`。
2. **丢弃**：`index < level[id]` 的记录视为被编辑作废；但「编辑记录本身」不丢，`type === message` 也始终保留。
3. **保留集合**必须满足三条硬约束（遍历方式：对每条保留记录沿 `latest[id].parentId` 往上走）：

| 约束 | 违规表现 | 失败码 |
|---|---|---|
| 无悬空父 | 父 id 不在保留集合里 | `WORKBUDDY_STRUCTURE_INVALID` |
| 无环 | 单条链上重复访问同一 id | `WORKBUDDY_STRUCTURE_INVALID` |
| 根恰好 1 个 | `roots.size !== 1`（多主题、断链、空会话） | `WORKBUDDY_STRUCTURE_INVALID` |

4. 至少要有 1 条 `role = user` 的 `message`，否则 `WORKBUDDY_STRUCTURE_INVALID`。
5. 因为 R3 是「同 id 后者覆盖」，一旦 WorkBuddy 跨轮次复用同一个 `q-N` 队列 id，父链就会绕回自己 —— **判定为环**，整条会话被拒。这就是本次「CYCLE at q-1」的机理。

---

## 5. Git 执行位置校验（与结构校验独立）

导入前会验证会话记录的 Git 执行位置（原仓库 / worktree 可访问、能取到执行上下文）。
探针失败即 `WORKBUDDY_SOURCE_EXECUTION_UNAVAILABLE`，**根本走不到结构校验**。

本次根因：Qoder 主进程的 PATH 里没有 `git`，日志表现为 `git_not_found` / `ENOENT`。
修复：把 Qoder 自带 git（`C:\Users\Rain\.qoder-cn\bin\git\cmd`）加入用户 PATH 并重启主进程。

---

## 6. 错误码全表（含官方文案）

| 原因码 | 触发环节 | 官方提示（i18n） |
|---|---|---|
| `WORKBUDDY_SOURCE_UNAVAILABLE` | 源目录/文件不可达 | 无法访问 WorkBuddy 来源数据 |
| `WORKBUDDY_SOURCE_CHANGED` | 复制期间源被改动（inode/mtime/size 不一致） | WorkBuddy 来源数据在导入期间发生变化 |
| `WORKBUDDY_SOURCE_EXECUTION_UNAVAILABLE` | Git 执行位置验证失败 | 无法验证 WorkBuddy 任务记录的 Git 执行位置。请确认原仓库和 Worktree 可访问后重新检测。 |
| `WORKBUDDY_JSON_INVALID` | 单行不是合法 JSON 对象 | WorkBuddy 记录 JSON 解析失败 |
| `WORKBUDDY_STRUCTURE_INVALID` | §3 R1/R2/R5、§4 全部、空文件、无用户消息 | WorkBuddy 任务记录消息结构不完整或存在冲突，修复来源数据后重试。 |
| `WORKBUDDY_RECORD_TOO_LARGE` | 文件 > 512 MiB 或单行 > 2 MiB | WorkBuddy 记录过大 |
| `WORKBUDDY_PREPARATION_FAILED` | 准备/落库阶段兜底异常 | WorkBuddy 数据准备失败 |
| `WORKBUDDY_MEMORY_SYNC_PARTIAL_FAILURE` | 记忆同步（非会话导入）部分失败 | 记忆同步部分失败 |

注：前 6 个在导入器里由同一条白名单正则保护并向上传递——
`/^WORKBUDDY_(SOURCE_UNAVAILABLE|SOURCE_CHANGED|JSON_INVALID|STRUCTURE_INVALID|RECORD_TOO_LARGE|PREPARATION_FAILED)$/`。

---

## 7. 本次 11 条失败会话的归因

| 原因码 | 条数 | 处理 |
|---|---|---|
| `WORKBUDDY_STRUCTURE_INVALID` | 6 → 已修复并成功导入 | 重命名重复 `q-N` id，断开环 |
| `WORKBUDDY_SOURCE_EXECUTION_UNAVAILABLE` | 5 → 3 条修复 PATH 后成功 | 加 git 到 PATH + 重启 |
| └ 其中 2 条（申请重庆邮电大学教育邮箱 / 奇思妙想）解锁后转为 `STRUCTURE_INVALID` | 2 | 同一个 `q-N` 环，**已于 2026-11-13 08:03 修复**（备份 `backup/20261113-080351/`），待界面重新勾选导入 |

复现校验器对以上样本的预测与实际导入结果**完全一致**（修复过的 2 个文件结构判 OK、成功导入；未修复的 2 个判「CYCLE at q-1」、失败）。

---

## 8. 自查与修复流程

```bash
cd /d/WorkBuddy/workbuddy-migrate/qoder-sync
python diagnose_structure.py            # 复现校验器，打印 CYCLE / DANGLING / ROOT COUNT / NO user message
python repair_qids.py                   # 干跑：内存修复 + 结构复检
python repair_qids.py --apply           # 备份到 backup/<时间戳>/ 后写回
python repair_pending2.py [--apply]     # 同一算法，只作用于台账里仍失败的那批（改 TARGETS 即可复用）
```

修复算法（保持语义不变的最小改动）：

1. 只处理 `type === 'message'` 且 id 形如 `q-<数字>`（长度 ≤ 8）的复用 id。
2. 第 1 次出现保持原样；第 k 次（k ≥ 2）改名为 `<id>-r<k>`，作用域是「从该行到同 id 下一次出现之前」这个窗口。
3. 窗口内所有引用字段同步改写：`id`、`parentId`、`logicalParentId`、`messageId`（递归进 `snapshot`）。
4. 效果：把「后者覆盖」造成的回边拆开，使每个 id 唯一，父链恢复成单根无环树；行数与内容顺序不变。
5. 修完必须再跑一次 `diagnose_structure.py`，然后在 Qoder 数据导入界面重新勾选该会话。

---

## 9. 证据边界（哪些是读代码、哪些是实测）

- 常量 `qK0 = 512*1024*1024`、`kEe = 2*1024*1024`、白名单正则、各抛错点分支：**直接读自 electronMain.js**，可靠。
- 记录/父子/编辑三类字段的判定分支：读自代码结构，可靠。
- 被跳过与被判定的 **具体字符串字面量**（`"message"`、`"reasoning"`、`WORKBUDDY_*` 各抛出点文案）：字符串表为 66 210 条且带混淆密钥，静态解码未成功，因此这些字面量取自同 bundle 的 i18n 可读文案 + 台账实测值，并由 `diagnose_structure.py` 在 11 条样本上与真实导入结果 100% 对齐来反向确认。
- 若 Qoder 升级，§3/§4 的行为可能变化；升级后先跑 `diagnose_structure.py` 再决定是否照本规则修复。
