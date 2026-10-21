# AstrBot × Magic Context 集成方案

> 目标：让 AstrBot 用上 `cortexkit/magic-context` 的上下文管理能力（历史降档、可逆丢弃、缓存稳定、跨会话记忆），
> 行为对齐 OpenCode / Pi 上的 `@cortexkit/opencode-magic-context` 与 `pi-magic-context`。
>
> 状态：**方案阶段**，尚未改动 AstrBot 任何配置。

---

## 1. 背景与结论

### 1.1 为什么不是"装一个插件"

Magic Context（下称 mc）不是 AstrBot 生态的插件，它由三部分组成：

| 层 | 位置 | 语言 | 作用 |
|---|---|---|---|
| Host 适配 | `packages/plugin`（OpenCode）、`packages/pi-plugin`（Pi/OMP） | TypeScript | 从 host 拿 messages，交给核心，写回 |
| 核心算法 | `packages/plugin` 内 + `crates/mc-core`、`crates/mc-store`、`crates/mc-tokenizer` | TS / Rust | tagging、compartment、decay 渲染、m[0]/m[1] 组装 |
| 运行外壳 | `crates/mc-module` → 可执行文件 `ck-mc` | Rust | 跑在 `subc`（subconscious daemon）里，走 `subc wire` 提供服务 |

我们要写的东西，**层级上等价于 `packages/pi-plugin`**：一个 host adapter。
策略一行都不用自己发明，全部复用 mc；我们只负责协议映射。

### 1.2 已验证的事实（不是推测）

- `cargo check -p mc-module` 在本机通过（49s），依赖 `subc-protocol 0.29.1`、`subc-control 0.28.0`、
  `subc-transport 0.10.0`、`subc-client-rs 0.26.1`、`cortexkit-store 0.2.1` 全部来自公开源；
  `subc-core` 来自公开 git rev。**结论：可自行编译。**
- `ck-mc` 的 `Cargo.toml` 声明它是 harness-agnostic 的 cache-stability transform，输入输出为 CK-in / CK-out。
- `crates/mc-module/src` 中已包含 `historian.rs`、`decay_render.rs`、`m0_compose.rs`、`m1_compose.rs`、
  `injection.rs`、`boundary.rs`、`caveman.rs` —— 核心逻辑在 Rust 侧完整存在。
- `CkWireMessage` 带 `original: Option<Value>`，为 **Value 级无损透传**，未知字段不会被吞。
- `CkKind` 共 7 种：`Text / Reasoning / RedactedReasoning / ToolCall / ToolResult / Media / Opaque`。

### 1.3 AstrBot 侧的关键发现

| 发现 | 位置 | 对方案的意义 |
|---|---|---|
| `@register_provider_adapter(...)` 是公开装饰器，内置 provider 均用它注册 | `core/provider/register.py` | **插件可以注册新的 provider 类型**，无需改内核 |
| `step_until_done()` 是 `while ... step_count < max_step` 循环 | `core/agent/runners/tool_loop_agent_runner.py` | 一次用户消息内最多 128 圈 |
| 每圈执行 `self.provider.text_chat(**payload)` / `text_chat_stream(**payload)` | 同上 | **每次 model call 必然经过 provider → 我们每步都能介入** |
| `on_llm_request` 只在 runner 启动前触发一次 | `core/pipeline/.../agent_sub_stages/internal.py:307` | 单独用它**做不到轮内处理**，只能跨轮 |
| `ProviderRequest` 含 `system_prompt / contexts / prompt / func_tool / extra_user_content_parts` | `core/provider/entities.py` | 改造面足够 |
| 官方建议：动态内容走 `extra_user_content_parts`，可 `.mark_as_temp()` 不落历史 | 官方插件文档 | 等价于 mc 的 `_no_save` 注入 |

**核心结论：接入点选 Provider 层，不选 `on_llm_request`。**
Provider 每圈被调一次，与 Pi 插件的 hook 同构，轮内处理因此天然成立。

---

## 2. 总体架构

```
QQ / 平台消息
   │
   ▼
AstrBot 内核（管道、persona、工具生态、会话存储）
   │  on_llm_request（只用来补注册工具/元信息）
   ▼
ToolLoopAgentRunner.step_until_done()   ← 最多 128 圈
   │  每圈：
   ▼
【我们的 Provider 适配层】  ←─ HTTP ─→【mc-adapter（Rust，常驻）】←─→ mc-core / ck-mc
   │                                          │
   │                                          └─→ context.db（compartments / memories）
   ▼
真正的上游 Provider（deepseek / sensenai …）
```

### 2.1 组件划分

| 组件 | 语言 | 职责 | 部署 |
|---|---|---|---|
| `mc-upstream` | — | 镜像 `cortexkit/magic-context`，每日拉取 + 编译 | `/root/data/mc-adapter/magic-context` |
| `mc-adapter` | Rust | 会话映射、OpenAI↔CK 转换、状态持久化、调用 mc 核心 | 常驻，监听 `127.0.0.1:PORT` |
| `astrbot_plugin_magic_context` | Python | 注册 provider 适配器；每圈调 adapter；注册 `ctx_*` 工具 | AstrBot 插件目录 |

### 2.2 接口草案

```
POST /v1/transform
  req:  { session_id, harness:"astrbot", model, system_prompt,
          messages:[ {role, content, tool_calls?, tool_call_id?, name?} ],
          tools?: [...] }
  resp: { system_prompt, messages, stats:{ in_tokens, out_tokens, dropped, compartments } }

POST /v1/tool
  req:  { session_id, tool: "ctx_reduce|ctx_expand|ctx_search|ctx_memory", args }
  resp: { ok, result }

GET  /health   → { ok, version, store_path, sessions }
```

`session_id` 直接取 AstrBot 的 `unified_msg_origin`，一个私聊/群 = 一个 mc session。

---

## 3. 必须做对的三个映射点

### 3.1 消息身份稳定（最容易踩）

- 问题：`contexts` 每轮从数据库重建，没有跨轮稳定的 message id。
- 做法：`stable_id = hash(conversation_id + role + 内容指纹 + 首次出现序号)`，持久化在 adapter 侧。
- 后果若做错：mc 认为上下文每轮都在变 → compartment 覆盖失效、`ctx_expand` 对不上、缓存无法稳定。

### 3.2 工具结果形状

- AstrBot：assistant 带 `tool_calls`，结果单独一条 `role:"tool"`，另有 `tool_calls_result` 字段。
- mc：`CkKind::ToolCall { id, name, input }` / `CkKind::ToolResult { id, tool_name, output }`。
- 做法：`tool_calls[]` ↔ `ToolCall` 一一映射；`role:"tool"` ↔ `ToolResult`；**`id` 必须原样保留**，
  否则 runner 无法把结果匹配回调用。
- 注意：`tool_calls_result` 是 AstrBot 特有的附加字段，需确认其与 `contexts` 中 tool 消息是否重复，避免双写。

### 3.3 m[0] / m[1] 落点

- mc 期望：两条合成消息置于对话最前，`m[0]` 冻结基线、`m[1]` 增量。
- AstrBot：`system_prompt` 是独立字段，`contexts` 是消息列表。
- 做法：`system_prompt` **保持字节不变**；把 `m[0]`/`m[1]` 作为合成消息插入 `contexts` 头部。
- 禁止：向 `system_prompt` 追加任何每轮变化的内容（官方文档明确：会破坏缓存，成本上涨 7–20 倍）。

---

## 4. 路线选择

### 路线 A（推荐，先验证）
adapter 直接 `cargo add mc-core mc-store mc-tokenizer`，**进程内调用**算法。
- 优点：无 subc wire、无 daemon、调试直观。
- 风险：`mc-core` 公开 API 是否足够（M0 验证点）。
- historian 需自行触发：adapter 内部调用配置好的 LLM 端点。

### 路线 B（原味，A 不通再退）
adapter 作为 `subc-client-rs` 客户端连接 `ck-mc`。
- 优点：行为与 OpenCode/Pi 完全一致。
- 风险：需搞清 `subc-daemon` 在 Linux 的部署方式。

---

## 5. 里程碑

| 阶段 | 内容 | 产出 | 预估 |
|---|---|---|---|
| M0 | 挖 `mc-core` / `mc-store` 公开 API；确认能否进程内完成 "对话 → 压缩后对话" 往返 | 可行性结论 | 0.5 天 |
| M1 | Rust 端到端：喂假多轮对话，输出 m[0]/m[1]/tail，人工核对降档 | 可运行 demo | 1–2 天 |
| M2 | HTTP 层 + Python 最小插件（只做 transform，不注册工具），在本机真实对话跑通 | 首次可用 | 1 天 |
| M3 | 接 `ctx_reduce` / `ctx_expand` / `ctx_search` 工具，验证"丢掉的能捞回来" | 功能完整 | 1–2 天 |
| M4 | 自动更新流水线（见 §6） | 可持续运行 | 0.5 天 |

### 进度记录（2026-10-21）

- **M0 ✅**：确认路线 A 成立。官方自带 `examples/transform_replay.rs` 就是无 subc 的离线驱动：
  `McStore::open(descriptor)` + `mc_module::transform::transform(&store, &request, &ctx)`。
  `McHandler::new()` 可在无连接文件时构造（historian 退化，transform 主干可用）。
- **M1 ✅**：喂三条假消息，返回 `ck_messages` 为 `m[0] + m[1] + tail`，
  `action=HARD`、`materialize_reason=first_render`、`frozen_units=2`，与文档描述一致。
- **M2 ✅（Rust 侧 + 映射层）**：`/root/data/mc-adapter/adapter` 已编译并跑通
  `GET /health` 与 `POST /v1/transform`；`astrbot_plugin_magic_context` 的
  OpenAI ↔ CK 映射实测往返成功（system / user / assistant+tool_calls / tool / user 全部保真）。

**实测踩到的两个坑（已修）**：
1. `McStore::open()` 不会自动挂 `context.db`，transform 会报
   `context_not_installed`；需 `create_test_context_db()` + `install_context_domain()`
   （后者被 gate 在 `test-support` feature 后，adapter 已开启该 feature）。
2. AstrBot 的工具结果**不在 `contexts` 里**，而是走 `tool_calls_result` 参数、由 provider
   内部拼接；不先并入，mc 永远看不到工具输出。插件已在 transform 前合并并移除该参数。

**尚未完成**：provider 未在 WebUI 注册（需新建 type=`mc_openai` 的 provider）；
historian 未接（`historian_active=false`，所以 m[0] 目前始终为空）；`ctx_*` 工具未注册。

---

## 6. 自动更新流水线（M4）

```
每日 cron:
  1. cd magic-context && git fetch && 有更新?
  2. 备份 context.db（时间戳命名）
  3. cargo build --release -p mc-module
  4. 编译成功 → 原子切换软链 → 重启 adapter
     编译失败 → 保留旧版本，告警
```

**硬性要求**：
- mc 的 store **不支持跨单库迁移降级**（其 README 明确写明），旧二进制会拒绝打开已迁移的 store。
- 因此每次更新前必须备份，且"编译成功才切换"，绝不覆盖式更新。
- adapter 启动时与上游做一次版本握手，版本不匹配直接拒绝启动，避免静默数据损坏。

---

## 7. 已知风险

| 风险 | 影响 | 应对 |
|---|---|---|
| `mc-core` API 不够 pub | 路线 A 不成立 | 退路线 B（subc client） |
| 消息身份不稳定 | compartment 失效、记忆错位 | §3.1 的稳定 id 方案，先用真实会话验证 |
| `tool_calls_result` 与 tool 消息重复 | 上下文重复、token 浪费 | M2 阶段抓真实 payload 对齐 |
| 上游协议漂移 | adapter 编译失败 / 字段错位 | 版本钉 + 启动握手 + 编译失败不切换 |
| 每日自动更新 | store 迁移后无法回滚 | 强制备份 + 原子切换 |
| 流式响应 | 只影响输出侧 | `text_chat_stream` 同样包一层，输入侧逻辑复用 |

---

## 8. 明确不做的事

- 不修改 AstrBot 内核源码。
- 不接管 AstrBot 的工具执行（工具仍由 runner 执行，我们只透传 `tool_calls`）。
- 不在 provider 层做任何"策略判断"（何时丢、丢什么、降档级别）——全部交给 mc。
- 不向 `system_prompt` 写入每轮变化的内容。
- 不改动现有插件配置，直到 M2 验证通过。

---

## 9. 下一步

1. 执行 M0：检查 `crates/mc-core` 与 `crates/mc-store` 的公开 API，确认进程内可用。
2. 若 M0 通过 → 按路线 A 继续；否则切路线 B。
3. 仓库位置确认：当前位于 `/root/data/mc-adapter/magic-context`（持久目录，非会话工作区）。
