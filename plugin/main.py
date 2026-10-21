"""Magic Context provider adapter for AstrBot.

把每一次 model call 的 contexts 交给本地 mc-adapter（Rust）过一遍
ck-mc 的 cache-stability transform，再把结果原样送给上游 provider。

策略（丢什么、降档到什么级别、何时重建基线）全部在 ck-mc 内部，
本插件只做翻译与转发。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

try:  # 包内导入（AstrBot 插件）
    from .mc_client import MCClient
except ImportError:  # 兜底
    from mc_client import MCClient  # type: ignore

PLUGIN_NAME = "astrbot_plugin_magic_context"
DEFAULT_ADAPTER_URL = "http://127.0.0.1:8317"


def _state_file() -> Path:
    try:
        base = Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        base = Path("data") / "plugin_data" / PLUGIN_NAME
    return base / "mc_ordinals.json"


_SHARED_CLIENT: MCClient | None = None


def _shared_client() -> MCClient:
    """工具用的共享 client（与 provider 同一 adapter 地址）。"""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = MCClient(
            base_url=os.environ.get("MC_ADAPTER_URL", DEFAULT_ADAPTER_URL),
            state_path=_state_file(),
        )
    return _SHARED_CLIENT


@dataclass
class MCWrapupTool(FunctionTool):
    """手动触发历史压缩。"""

    name: str = "mc_wrapup"
    description: str = (
        "手动压缩较早的对话历史（Magic Context 的 session.wrapup）。"
        "当用户说“整理一下前面聊的”“压缩一下记忆”“这段不用记了”时调用。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def run(self, event: AstrMessageEvent):
        try:
            res = await _shared_client().wrapup(event.unified_msg_origin)
        except Exception as exc:  # 不阻断对话
            return f"mc_wrapup failed: {exc}"
        return json.dumps(res, ensure_ascii=False)[:1800]


@dataclass
class MCStatusTool(FunctionTool):
    """查询压缩状态。"""

    name: str = "mc_status"
    description: str = "查看 Magic Context 当前会话的历史压缩状态（compartment 数量、覆盖率、是否已建立边界）。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "required": [],
        }
    )

    async def run(self, event: AstrMessageEvent):
        try:
            res = await _shared_client().status(event.unified_msg_origin)
        except Exception as exc:
            return f"mc_status failed: {exc}"
        keep = {
            k: res.get(k)
            for k in (
                "compartment_count",
                "compartment_tokens",
                "boundary_present",
                "coverage_ordinal",
            )
        }
        return json.dumps(keep, ensure_ascii=False)


@register_provider_adapter(
    "mc_openai",
    "OpenAI 兼容 + Magic Context 上下文管理（每轮 model call 走 ck-mc transform）",
    default_config_tmpl={
        "type": "mc_openai",
        "enable": True,
        "id": "mc_openai",
        "provider": "openai",
        "api_base": "https://api.deepseek.com/v1",
        "key": [],
        "model_config": {"model": "deepseek-chat"},
        "timeout": 120,
        "proxy": "",
        "custom_headers": {},
        "mc_adapter_url": DEFAULT_ADAPTER_URL,
        "mc_enabled": True,
    },
)
class MCProvider(ProviderOpenAIOfficial):
    """包在 OpenAI provider 外面的 transform 层。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        cfg = provider_config or {}
        self.mc_enabled = bool(cfg.get("mc_enabled", True))
        self.mc_adapter_url = str(cfg.get("mc_adapter_url") or DEFAULT_ADAPTER_URL)
        self.mc_client = MCClient(
            base_url=self.mc_adapter_url,
            state_path=_state_file(),
            timeout=float(cfg.get("mc_timeout", 30) or 30),
        )

    # ---- 核心：每次 model call 前把 contexts 过一遍 mc ----

    async def _mc_transform_payload(self, kwargs: dict) -> dict:
        if not self.mc_enabled:
            return kwargs
        contexts = kwargs.get("contexts")
        if not contexts:
            return kwargs

        session_id = str(
            kwargs.get("conversation_id")
            or kwargs.get("session_id")
            or "astrbot-default"
        )

        # providers 内部会做 _ensure_message_to_dicts，这里先做一次，保证拿到 dict
        raw = self._ensure_message_to_dicts(list(contexts))

        # 工具结果不在 contexts 里，而是走 tool_calls_result 参数；
        # 不并进来，mc 就永远看不到工具输出，无法做轮内清理。
        tcr = kwargs.get("tool_calls_result")
        merged_tcr = False
        if tcr:
            items = tcr if isinstance(tcr, list) else [tcr]
            for item in items:
                try:
                    raw.extend(item.to_openai_messages())
                    merged_tcr = True
                except Exception:
                    merged_tcr = False
                    break
        if not merged_tcr:
            raw = self._ensure_message_to_dicts(list(contexts))

        try:
            transformed = await self.mc_client.transform(session_id, raw)
        except Exception as exc:  # 失败绝不阻断对话
            logger.warning("[mc] transform 失败，回退原始上下文: %s", exc)
            return kwargs

        if not transformed:
            return kwargs
        kwargs = dict(kwargs)
        kwargs["contexts"] = transformed
        if merged_tcr:
            # 已并入 contexts，避免 provider 内部再拼一次
            kwargs.pop("tool_calls_result", None)
        return kwargs

    async def text_chat(self, **kwargs):  # type: ignore[override]
        kwargs = await self._mc_transform_payload(kwargs)
        return await super().text_chat(**kwargs)

    async def text_chat_stream(self, **kwargs):  # type: ignore[override]
        kwargs = await self._mc_transform_payload(kwargs)
        async for resp in super().text_chat_stream(**kwargs):
            yield resp


@register(PLUGIN_NAME, "柳烟", "Magic Context 上下文管理（provider 层）", "0.1.0", "")
class MagicContextPlugin(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._state_file = _state_file()
        # 注册 Magic Context 的运维工具（facade 形式的 ctx_* 需要 thalamus 网关，
        # 本部署没有，所以只暴露 method 形式可用的能力）
        try:
            self.context.add_llm_tools(MCWrapupTool(), MCStatusTool())
        except Exception as exc:
            logger.warning("[mc] 注册工具失败: %s", exc)
        logger.info(
            "[mc] 插件已加载；在 WebUI 新建 provider 时选择类型 mc_openai，"
            "adapter 默认 %s",
            os.environ.get("MC_ADAPTER_URL", DEFAULT_ADAPTER_URL),
        )

    async def terminate(self) -> None:
        logger.info("[mc] 插件已卸载")
