# astrbot_plugin_magic_context_adapter

把 [Magic Context](https://github.com/cortexkit/magic-context)（下称 mc）的上下文管理能力接进 [AstrBot](https://github.com/AstrBotDevs/AstrBot)。

行为目标与 OpenCode / Pi 上的 `@cortexkit/opencode-magic-context`、`pi-magic-context` 对齐：
**每次 model call 前把上下文交给 ck-mc 做 cache-stability transform**，由 mc 决定历史如何降档、什么可以丢弃、何时重建基线。
本仓库只负责翻译与转发，不实现任何压缩策略。

## 状态

| 部分 | 状态 |
|---|---|
| Rust adapter（HTTP） | ✅ 已跑通 `/health`、`/v1/transform` |
| OpenAI ↔ CK 映射 | ✅ system / user / assistant+tool_calls / tool 全保真 |
| Python provider 插件 | ✅ 已实现，等待在 WebUI 注册 |
| historian（真正的历史压缩） | ⬜ 未接入（`historian_active=false`，m[0] 目前为空） |
| `ctx_*` 工具（reduce/expand/search） | ⬜ 未注册 |
| 自动更新流水线 | ⬜ 未实现 |

## 架构

```
QQ / 平台消息
   │
   ▼
AstrBot 内核（管道 / persona / 工具 / 会话存储）
   │
   ▼
ToolLoopAgentRunner  ← 一次用户消息内最多 128 圈
   │  每圈调用 provider.text_chat / text_chat_stream
   ▼
MCProvider（本仓库 plugin/）        ← 注册为 type=mc_openai
   │  HTTP POST /v1/transform
   ▼
mc-adapter（本仓库 adapter/，Rust 常驻）
   │  进程内调用 mc_module::transform::transform
   ▼
ck-mc 核心（上游 crates/mc-core、mc-store）
   │
   ▼
真正的上游 Provider（deepseek / openai / …）
```

关键点：**每圈 model call 都经过 provider**，所以接入点选 provider 而不是 `on_llm_request`
（后者在一次用户消息的整个工具循环里只触发一次）。

## 目录

```
.
├── adapter/                      # Rust HTTP 适配层
│   ├── Cargo.toml
│   └── src/main.rs
├── plugin/                       # AstrBot 插件（复制到 data/plugins/ 使用）
│   ├── main.py                   # provider 适配器注册
│   ├── metadata.yaml
│   └── mc_client.py              # OpenAI ↔ CK 映射 + HTTP 客户端
├── scripts/
│   └── bootstrap.sh              # 拉取上游 + 编译 adapter
├── magic-context/                # 上游仓库（不提交，由 bootstrap.sh 拉取）
└── plan.md                       # 设计与决策记录
```

## 快速开始

```bash
# 1. 拉取上游并编译 adapter
./scripts/bootstrap.sh

# 2. 启动 adapter
cd adapter && MC_STORE_DIR=/var/lib/mc-adapter ./target/release/mc-adapter

# 3. 安装插件
cp -r plugin /path/to/AstrBot/data/plugins/astrbot_plugin_magic_context
```

然后在 AstrBot WebUI 新建 provider：

| 字段 | 值 |
|---|---|
| 类型 | `mc_openai` |
| api_base | 上游真实地址，例如 `https://api.deepseek.com/v1` |
| key | 上游 key |
| model | 上游模型名 |
| mc_adapter_url | `http://127.0.0.1:8317`（默认） |

## 配置项

adapter：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MC_ADAPTER_PORT` | `8317` | 监听端口（仅 127.0.0.1） |
| `MC_STORE_DIR` | `./data` | store.db / context.db 所在目录，需持久化 |
| `RUST_LOG` | `info` | 日志级别 |

插件（provider 配置里）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `mc_enabled` | `true` | 关掉即退化为普通 OpenAI provider |
| `mc_adapter_url` | `http://127.0.0.1:8317` | adapter 地址 |
| `mc_timeout` | `30` | transform 超时（秒） |

## 设计约束（重要）

1. **不改 `system_prompt`**：AstrBot 官方文档明确，向 `system_prompt` 追加每轮变化的内容会破坏
   provider 缓存、成本上涨 7–20 倍。mc 的 `m[0]/m[1]` 作为合成消息插在 `contexts` 中。
2. **`system` 必须留在首位**：mc 会把 `m[0]/m[1]` 插到最前，插件回填时统一把 system 提到顶部。
3. **工具结果要并进 contexts**：AstrBot 的工具结果走 `tool_calls_result` 参数、由 provider 内部拼接；
   插件在 transform 前先合并并移除该参数，否则 mc 看不到工具输出，轮内清理不会发生。
4. **失败降级**：adapter 不可用时插件回退原始上下文，绝不阻断对话。
5. **ordinal 必须跨轮稳定**：mc 要求消息 ordinal 单调递增且跨请求不变；插件用
   内容指纹 → ordinal 的持久化映射（`data/plugin_data/.../mc_ordinals.json`）。

## 更新策略

上游每天可能变动，但 mc 的 store **不支持跨单库迁移降级**。

`bootstrap.sh` 只负责拉取与编译；生产更新请遵循：

1. `git fetch` 后判断是否有新 commit；
2. **先备份** `store.db` 与 `context.db`；
3. 编译到新目录；
4. 编译成功才切换软链并重启 adapter，失败保留旧版本；
5. adapter 启动时做版本握手，不匹配直接拒绝启动。

## 许可证

本仓库代码为 AGPL-3.0（与 AstrBot 一致）。上游 Magic Context 同样为 AGPL-3.0，使用前请阅读其许可。
