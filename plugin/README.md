# astrbot_plugin_magic_context

把 [Magic Context](https://github.com/cortexkit/magic-context)（下称 mc）的上下文管理接入 AstrBot：
**每次模型调用前，把上下文交给 ck-mc 做一次 cache-stability transform**——历史分档压缩、
可逆丢弃、稳定前缀由 mc 决定，本插件只做翻译与转发，不自研任何压缩策略。

## 它解决什么

- 长会话越聊越贵、越聊越慢：mc 把旧历史折成带重要度的 compartment，按年龄降档；
- 上下文塞不下就丢消息且找不回来：mc 的丢弃可逆，原文留在库里；
- 不同会话互相污染：按会话隔离，长期记忆可按 project 共享。

## 前置条件

本插件是**客户端**，需要本机已有可用的 CortexKit 运行时：

1. `ck` CLI 与 `ck-subc` daemon（`ck setup --with mc`）；
2. `ck-mc` 模块处于 `running`；
3. 本仓库的 `mc-adapter`（Rust）常驻在 `127.0.0.1:8317`。

检查：

```bash
ck module list          # magic-context running
curl -s 127.0.0.1:8317/health
```

## 安装

```bash
cp -r plugin /path/to/AstrBot/data/plugins/astrbot_plugin_magic_context
```

重载插件后生效。**不需要新建 provider**：插件会 patch 所有 OpenAI 兼容 provider
（`ProviderOpenAIOfficial` 及其子类）的 `text_chat` / `text_chat_stream`，
现有模型直接走 mc。

## 指令

| 指令 | 作用 |
|---|---|
| `/mc` 或 `/mc help` | 显示帮助 |
| `/mc status` | 当前会话的 compartment / token / boundary 状态 |
| `/mc update` | **手动**更新 ck-mc：拉源码 → 编译 → 备份 db → 原子切换 → 重启模块 |

更新完全手动，插件不做任何定时拉取。

## 配置

插件无独立配置面板项；通过环境变量调整：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MC_ADAPTER_URL` | `http://127.0.0.1:8317` | adapter 地址 |
| `MC_UPDATE_SCRIPT` | `/root/data/mc-adapter/scripts/update.sh` | `/mc update` 调用的脚本 |

## 行为与容错

- **超时**：transform 调用 8 秒超时，超时即回退原始上下文；
- **熔断**：连续失败 3 次暂停 transform 120 秒，期间对话完全正常，之后自动恢复；
- **幂等**：重复重载不会重复包装 provider（原始方法保存在类属性上）；
- **会话键**：使用 AstrBot 的 session_id（UMO），跨 `/reset` 保持稳定。

## 实现要点

- 接入点是 **provider 层**而不是 `on_llm_request`：后者在一轮工具循环里只触发一次，
  而 provider 每次 model call 都被调用，工具循环内的每一圈都能过一遍 mc；
- 图片按 mc 的 `MediaBlock` 透传（注意 `CkKind` 是内部标签枚举，字段与 `type` **平铺**），
  回填时还原成 `image_url`，模型仍能看到图；
- 工具结果不在 `contexts` 里（走 `tool_calls_result`），transform 前需合并，否则 mc
  看不到工具输出；
- `ordinal` 用内容指纹分配并持久化，检测到乱序时整段重排到安全区间。

## 已知限制

- facade 形式的 `ctx_*` 工具（`ctx_reduce`/`ctx_expand`/`ctx_search`/`ctx_memory`/`ctx_note`）
  需要 `thalamus` 网关解析 session，本部署未安装，暂不可用；
- 插件暴露的是 `mc_wrapup` / `mc_status` 两个基于 method 形式的工具；
- historian 需要 `historian.runner = host`（host 侧跑模型），见仓库 README。

## 许可

AGPL-3.0（与 AstrBot、Magic Context 一致）。
