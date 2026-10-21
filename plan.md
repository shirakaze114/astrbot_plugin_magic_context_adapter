# AstrBot × Magic Context 集成方案

> 目标：让 AstrBot 用上 `cortexkit/magic-context`（mc）的上下文管理能力
> （历史降档、可逆丢弃、缓存稳定、跨会话记忆），行为对齐 OpenCode / Pi 上的
> `@cortexkit/opencode-magic-context` 与 `pi-magic-context`。
>
> 状态：**M2 完成，M3 进行中**。daemon 已在本机跑通，模块已注册。

---

## 1. 背景

### 1.1 mc 的三层结构

| 层 | 位置 | 语言 | 作用 |
|---|---|---|---|
| Host 适配 | `packages/plugin`（OpenCode）、`packages/pi-plugin`（Pi/OMP） | TypeScript | 取 messages、交给核心、写回 |
| 核心算法 | `packages/plugin` + `crates/mc-core` / `mc-store` / `mc-tokenizer` | TS / Rust | tagging、compartment、decay 渲染、m[0]/m[1] 组装 |
| 运行外壳 | `crates/mc-module` → `ck-mc` | Rust | 跑在 `subc` daemon 下，走 subc wire 提供服务 |

AstrBot 侧要写的东西，**层级等价于 `packages/pi-plugin`**：一个 host adapter。
策略（丢什么、降档到几级、何时重建基线）全部复用 mc，不自研。

### 1.2 AstrBot 侧的关键发现（来自源码，非推测）

| 发现 | 位置 | 意义 |
|---|---|---|
| `@register_provider_adapter(...)` 公开 | `core/provider/register.py` | 插件可注册新 provider 类型，无需改内核 |
| `step_until_done()` 是 `while ... < max_step` 循环 | `core/agent/runners/tool_loop_agent_runner.py` | 一次用户消息最多 128 圈 |
| 每圈调用 `provider.text_chat(**payload)` / `text_chat_stream` | 同上 | **每次 model call 必经过 provider** → 接入点选 provider |
| `on_llm_request` 只在 runner 启动前触发一次 | `.../agent_sub_stages/internal.py:307` | 单靠它无法做轮内处理 |
| 工具结果走 `tool_calls_result` 参数，不在 `contexts` 里 | provider 内部 `_prepare_chat_payload` | transform 前必须并入，否则 mc 看不到工具输出 |
| 官方建议动态内容走 `extra_user_content_parts` + `mark_as_temp()` | 官方文档 | 等价于 mc 的 `_no_save` 注入 |

---

## 2. 架构（现行：路线 B，daemon 形态）

```
QQ / 平台消息
   │
   ▼
AstrBot 内核（管道 / persona / 工具 / 会话存储）
   │
   ▼
ToolLoopAgentRunner  ← 最多 128 圈，每圈经 provider
   │
   ▼
MCProvider（astrbot_plugin_magic_context，type=mc_openai）
   │  OpenAI ↔ CK 映射
   ▼
mc-adapter（Rust，subc consumer）
   │  subc wire（loopback + HMAC 握手）
   ▼
ck-subc（守护进程，systemd --user / 前台进程）
   │  托管 / 路由 / 健康检查 / 崩溃重启
   ▼
ck-mc（magic-context 模块）
   │  进程内 transform / historian / decay / dreamer
   ▼
mc-store（store.db + context.db）
```

### 2.1 为什么用 provider 而不是 `on_llm_request`

`on_llm_request` 在整轮工具循环里只触发一次；provider 每圈都被调用。
选 provider 才能在“工具结果刚产生、下一圈模型调用前”这个时刻介入，
这是 mc 的 `ctx_reduce` 能对齐 OpenCode/Pi 的前提。

### 2.2 路线 A（备用，已验证可行）

adapter 直接 `path` 依赖 `mc-module`，进程内调
`mc_store::McStore::open()` + `mc_module::transform::transform()`，
已实测跑通（见 §5）。缺点：拿不到 `McHandler` 的 historian/dreamer，
且 `RequestCtx` 无公开构造器，`state_import` 不可用。
**仅作为 daemon 不可用时的降级路径保留。**

---

## 3. 部署现状（2026-10-21）

| 组件 | 路径 | 状态 |
|---|---|---|
| `ck` CLI | `~/.local/share/cortexkit/cmd/ck` | v0.20.83，已装 |
| `ck-subc` | `~/.local/share/cortexkit/bin/ck-subc` | v0.20.83，运行中 |
| `ck-subc-mcp` | `~/.local/share/cortexkit/bin/ck-subc-mcp` | 已装 |
| `ck-mc` | `~/.local/share/cortexkit/bin/ck-mc` | `ck-mc-alpha.22464bf2`，手动安装 |
| 配置 | `~/.config/cortexkit/subc.jsonc` | storage + modules.magic-context |
| 连接文件 | `/run/user/0/subc-connection.json` | 已生成 |
| 模块状态 | `ck module status magic-context` | running / negotiated_ver=2 / routable_provider=true |

验证命令：

```bash
export PATH="$HOME/.local/share/cortexkit/cmd:$PATH"
ck module list          # magic-context running
ck catalog              # magic-context ready, roles=1
ck health               # magic-context
```

---

### 3.1 发布版 ck-mc 实测可用接口

`ck-mc-alpha.22464bf2` 实际只开放了 slice-1（cache-stability spine）。

| 接口 | 形式 | 状态 | 备注 |
|---|---|---|---|
| `transform` | `{kind:"transform", v:2, ...}` | ✅ 可用 | 83 ms 返回 m[0]/m[1]+tail |
| `health` / `session.status` | `{method:"...", v:1}` | ✅ 可用 | 返回 epochs / compartment 统计 |
| `session.wrapup` | `{method:"session.wrapup", v:1}` | ⚠️ 可调用 | 但返回 `no_models`，见下 |
| `agent_drops.append` | `{method:"agent_drops.append", command_id, drop:"3-5"}` | ✅ 可调用 | 需真实 tag 编号，否则 `no valid tags` |
| `session.flush` | `{method:"session.flush", v:1}` | ✅ 可用 | 返回 armed |
| `guidance.get` / `manifest.get` | `{method, v:1}` | ✅ 可用 | manifest 给出 5 个 ctx_* 工具 schema |
| `historian.pending/claim/complete` | `{method:"historian.pending"}` | ❌ 未开放 | 返回 `unrecognized_request_shape` |
| `ctx_reduce/memory/expand/search/note` | facade `{name, arguments}` | ❌ 不可用 | 需 `thalamus` 网关解析 session，返回 `session_resolve_timeout` |

**historian 卡点**：`session.wrapup` 用的是「该 session 上一次 transform 携带的
`historian_model_chain`」快照（`lib.rs:7009`），而不是 wrapup 请求自身的字段；
且模型链格式/白名单尚未验证通过（当前带 `["deepseek-chat"]` 仍返回 `no_models`）。
下一步应在插件配置界面暴露 historian 模型项，用 `provider/model` 形式注入并复测。

## 4. 接口与映射

### 4.1 adapter 对外接口（兼容层）

```
POST /v1/transform
  in:  { kind:"transform", v:2, serializer_profile, session_id, render_config, messages:[CkIngressMessage] }
  out: { status, action, ck_messages, materialize_reason, ... }
GET  /health
```

### 4.2 三个必须做对的映射点

1. **消息身份稳定**：`ordinal` 必须跨轮恒定。实现方式为
   `sha256(role + content + tool_calls + tool_call_id)` 指纹 → ordinal 的持久化映射
   （`data/plugin_data/.../mc_ordinals.json`）。做错会导致 compartment 覆盖失效。
2. **工具结果形状**：assistant 的 `tool_calls[]` → `CkKind::ToolCall{id,name,input}`；
   `role:"tool"` → `CkKind::ToolResult{id,tool_name,output:{kind:{type:"text"|"json"}}}`；
   `id` 必须原样保留，否则 runner 无法把结果匹配回调用。
3. **m[0]/m[1] 落点**：`system_prompt` 保持字节不变；`m[0]`/`m[1]` 作为合成消息插入
   `contexts`，回填时把 `system` 提回首位（OpenAI 兼容端点要求）。

---

## 5. 里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | 确认 `mc-core`/`mc-store` 公开 API 可进程内调用 | ✅ |
| M1 | 进程内跑通 transform，输出 m[0]/m[1]/tail | ✅ |
| M2 | Rust adapter HTTP + Python provider 插件 + 映射往返 | ✅ |
| M3 | 切到 subc daemon 形态，adapter 作为 consumer 调 ck-mc | ✅ |
| M4 | 注册运维工具（wrapup / status） | ✅ 部分 |
| M5 | 自动更新流水线（备份 + 编译 + 原子切换） | ⬜ |

### M1 实测结果（进程内）

```
action=HARD  materialize_reason=first_render  frozen_units=2
[0] user  <session-history></session-history>              ← m[0]
[1] user  <session-history-since>(no new content ...)</>   ← m[1]
[2..] 原始 tail
```

### M3 实测结果（subc daemon）

```
GET /health        → { mode:"subc", module_id:"magic-context", harness:"astrbot" }
POST /v1/transform → 200, 83 ms, action=HARD, reason=first_render
                     ck_messages = m[0] + m[1] + 3 条原始 tail
```

adapter 已不再依赖上游源码（只依赖 `subc-client-rs` / `subc-protocol`），
编译时间从 54s 降到 13s；`mc-upstream` 的 `magic-context/` 目录不再是构建前提。

### M2 实测结果（HTTP + 映射）

```
[0] system     你是一个助手
[1] user       <session-history></session-history>
[2] user       <session-history-since>…</…>
[3] user       帮我查一下成都天气
[4] assistant  tool_calls=1 get_weather
[5] tool       成都 22 度 多云
[6] assistant  成都现在 22 度，多云。
[7] user       那明天呢
```

---

## 6. 踩坑记录

### 6.1 上游 / 环境

| 现象 | 原因 | 处理 |
|---|---|---|
| `curl` 反复 `Recv failure: Connection reset by peer` | 链路对 HTTP/2 不友好 | `~/.curlrc` 加 `http1.1`；并走代理 `192.168.1.23:8443` |
| `ck setup --with mc` 第 9 步 refused | `ck-mc --version` 输出 `0.1.0`，索引期望 `22464bf2`（上游校验 bug） | 从 release index 取直链下载，sha256 校验一致后手动 `install` 到 bin |
| `ck setup` 未启动 daemon | 因上一步中断，第 10 步未执行 | 直接前台运行 `ck-subc`（读 `subc.jsonc`） |

### 6.2 mc 进程内（路线 A）

| 现象 | 原因 | 处理 |
|---|---|---|
| `context_not_installed` | `McStore::open()` 不自动挂 `context.db` | `create_test_context_db()` + `install_context_domain()`（后者需 `test-support` feature） |
| 工具输出看不到 | 工具结果走 `tool_calls_result`，不在 `contexts` | 插件在 transform 前合并并移除该参数 |
| `system` 被挤到中间 | mc 把 m[0]/m[1] 插在最前 | 回填时把 system 提到首位 |

---

## 7. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| daemon 需常驻 | 进程死了就没有上下文管理 | systemd --user 托管；插件侧失败降级为原始上下文 |
| 上游版本漂移 | 协议/字段变化 | 版本握手 + 编译失败不切换 + 每次升级备份 store |
| store 不支持跨单库迁移降级 | 无法回滚 | 升级前强制备份 `store.db` / `context.db` |
| 消息身份不稳定 | compartment 失效、记忆错位 | §4.2 的指纹 → ordinal 映射 |
| 轮内 drop 打碎缓存 | 省 token 反而更贵 | 由 mc 的 cache-safe 队列决定，adapter 不干预 |

---

## 8. 明确不做的事

- 不修改 AstrBot 内核源码。
- 不接管 AstrBot 的工具执行（只透传 `tool_calls`）。
- 不在 adapter 里做任何策略判断（何时丢、丢什么、降档级别）。
- 不向 `system_prompt` 写入每轮变化的内容。
- 不自己重写 historian 的 prompt / 校验逻辑（用上游 pub API，或交给 ck-mc 内部跑）。

---

## 9. 下一步

1. 读 `subc-client-rs::consumer`（`SubcConsumer::connect_default` / `open_route` / `call`），
   把 adapter 的 transform 从“进程内调用”换成“走 subc wire 调 ck-mc”。
2. 打通后用 `ck health` / `ck module logs magic-context` 观察真实对话中的 pass 行为。
3. M4：注册 `ctx_reduce` / `ctx_expand` / `ctx_search` 三个工具。
4. M5：自动更新脚本（备份 → 编译 → 原子切换 → 重启模块）。
