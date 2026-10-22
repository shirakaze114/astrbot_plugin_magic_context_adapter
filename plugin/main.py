"""Magic Context provider adapter for AstrBot.

把每一次 model call 的 contexts 交给本地 mc-adapter（Rust）过一遍
ck-mc 的 cache-stability transform，再把结果原样送给上游 provider。

策略（丢什么、降档到什么级别、何时重建基线）全部在 ck-mc 内部，
本插件只做翻译与转发。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

try:  # 包内导入（AstrBot 插件）
    from .mc_client import MCClient
except ImportError:  # 兜底
    from mc_client import MCClient  # type: ignore

PLUGIN_NAME = "astrbot_plugin_magic_context"
DEFAULT_ADAPTER_URL = "http://127.0.0.1:8317"
# 手动更新脚本（只在 /mc update 时执行，没有任何定时任务）
_UPDATE_SCRIPT = Path(
    os.environ.get("MC_UPDATE_SCRIPT", "/root/data/mc-adapter/scripts/update.sh")
)


def _state_file() -> Path:
    try:
        base = Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:
        base = Path("data") / "plugin_data" / PLUGIN_NAME
    return base / "mc_ordinals.json"


_SHARED_CLIENT: MCClient | None = None
_PATCH_INSTALLED = False
# AstrBot 的 conversation_id 与 UMO 的映射：patch 只能拿到前者，
# 但对话隔离要以 UMO 为准（/reset 后 conversation_id 会变，UMO 不变）。
_CONV_TO_UMO: dict[str, str] = {}
# 熔断：连续失败到阈值就暂停 transform 一段时间，保证永远不拖住正常对话
_MC_FAILS = 0
_MC_OFF_UNTIL = 0.0
_MC_FAIL_THRESHOLD = 3
_MC_COOLDOWN_SECONDS = 120.0

# 插件配置面板（_conf_schema.json）的运行时快照
_CFG: dict[str, Any] = {
    "enabled": True,
    "adapter_url": DEFAULT_ADAPTER_URL,
    "historian_model": "",
}

# historian 的凭据落盘位置：adapter 每次 tick 会重新读取（改配置无需重启服务）
_LLM_CONFIG_PATH = Path(
    os.environ.get("MC_LLM_CONFIG", "/root/data/mc-adapter/data/llm.json")
)


def _write_llm_config(chain: dict) -> None:
    """把三个角色（historian / dreamer / embedding）的模型写盘，交给 adapter。

    结构：
        {"historian": {base_url, api_key, model},
         "dreamer":   {...},
         "embedding": {...}}

    adapter 每次 historian tick 会重新读这个文件，改配置无需重启服务。
    """
    try:
        _LLM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        roles: dict[str, dict] = {}
        for role, cfg in (chain or {}).items():
            if not isinstance(cfg, dict):
                continue
            roles[role] = {
                "base_url": cfg.get("base_url", ""),
                "api_key": cfg.get("api_key", ""),
                "model": cfg.get("model", ""),
            }
        tmp = _LLM_CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(roles, ensure_ascii=False), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _LLM_CONFIG_PATH)
    except Exception as exc:
        logger.warning("[mc] 写入模型配置失败: %s", exc)


def _shared_client() -> MCClient:
    """工具与 patch 共用的 client（与 provider 同一 adapter 地址）。"""
    global _SHARED_CLIENT
    if _SHARED_CLIENT is None:
        _SHARED_CLIENT = MCClient(
            base_url=os.environ.get("MC_ADAPTER_URL", DEFAULT_ADAPTER_URL),
            state_path=_state_file(),
        )
    return _SHARED_CLIENT


def _trace_patch_call(owner, kwargs: dict) -> None:
    """排查用：记录 patch 是否真的被调用（滚动保留最后 20 行）。"""
    try:
        import time as _t

        ctx = kwargs.get("contexts") or []
        line = (
            f"{_t.strftime('%H:%M:%S')} cls={type(owner).__name__} "
            f"ctx={len(ctx)} conv={kwargs.get('conversation_id')} "
            f"sid={kwargs.get('session_id')}\n"
        )
        p = Path("/tmp/mc_trace.log")
        with p.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
    except Exception:
        pass


def _reset_failures() -> None:
    global _MC_FAILS
    _MC_FAILS = 0


def _note_failure(exc: Exception) -> None:
    """记一次失败；连续失败到阈值就熔断一段时间，避免拖住正常对话。"""
    global _MC_FAILS, _MC_OFF_UNTIL
    _MC_FAILS += 1
    if _MC_FAILS >= _MC_FAIL_THRESHOLD:
        _MC_OFF_UNTIL = time.time() + _MC_COOLDOWN_SECONDS
        logger.warning(
            "[mc] 连续失败 %d 次，暂停 transform %.0fs（对话不受影响）: %s",
            _MC_FAILS,
            _MC_COOLDOWN_SECONDS,
            exc,
        )
        _MC_FAILS = 0


async def _mc_wrap_payload(owner, kwargs: dict) -> dict:
    """把一次 model call 的 contexts 交给 mc 过一遍，失败则原样返回。"""
    if time.time() < _MC_OFF_UNTIL:
        return kwargs
    _trace_patch_call(owner, kwargs)
    if getattr(owner, "_mc_patch_disabled", False):
        return kwargs
    contexts = kwargs.get("contexts")
    if not contexts:
        return kwargs

    # 统一 key = session_id（AstrBot 的 UMO）：它在 patch 侧始终存在，
    # 且跨 /reset 稳定；conversation_id 只作兜底。
    session_id = str(
        kwargs.get("session_id") or kwargs.get("conversation_id") or "astrbot-default"
    )
    try:
        raw = owner._ensure_message_to_dicts(list(contexts))
    except Exception:
        raw = [m for m in contexts if isinstance(m, dict)]
    if not raw:
        return kwargs

    # 工具结果不在 contexts 里，先并进来，否则 mc 看不到工具输出
    tcr = kwargs.get("tool_calls_result")
    merged = False
    if tcr:
        items = tcr if isinstance(tcr, list) else [tcr]
        try:
            for item in items:
                raw.extend(item.to_openai_messages())
            merged = True
        except Exception:
            merged = False

    try:
        transformed = await asyncio.wait_for(
            _shared_client().transform(session_id, raw), timeout=8
        )
        _reset_failures()
    except Exception as exc:
        _note_failure(exc)
        _trace_patch_call(owner, {"contexts": raw, "session_id": f"ERROR {session_id}: {exc}"})
        text = str(exc)
        # 锚点错乱自愈：mc 里存的 compartment 边界与本地 ordinal 对不上时，
        # 丢掉该会话的 mc 状态与本地映射，下一轮从干净状态重建。
        if "boundary ordinal mismatch" in text or "ordinals not strictly increasing" in text:
            try:
                await _shared_client().reset_mc_session(session_id)
                logger.warning("[mc] 会话 %s 锚点错乱，已重置状态自愈", session_id)
            except Exception as reset_exc:
                logger.warning("[mc] 重置会话失败: %s", reset_exc)
        else:
            logger.warning("[mc] transform 失败，回退原始上下文: %s", exc)
        return kwargs
    if not transformed:
        return kwargs

    kwargs = dict(kwargs)
    kwargs["contexts"] = transformed
    if merged:
        kwargs.pop("tool_calls_result", None)
    return kwargs


def install_provider_patch() -> bool:
    """把 mc 的 transform 挂到所有 OpenAI 兼容 provider 上。

    这样不需要在 WebUI 新建 provider 类型，现有模型直接生效。
    """
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return True
    try:
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

        # 把“最初的真实方法”挂在类上，模块重载也不会丢；
        # 这样每次重载都重新包装最新逻辑，而不是复用旧闭包。
        if not hasattr(ProviderOpenAIOfficial, "_mc_orig_text_chat"):
            ProviderOpenAIOfficial._mc_orig_text_chat = (
                ProviderOpenAIOfficial.text_chat
            )
            ProviderOpenAIOfficial._mc_orig_text_chat_stream = (
                ProviderOpenAIOfficial.text_chat_stream
            )

        orig_chat = ProviderOpenAIOfficial._mc_orig_text_chat
        orig_stream = ProviderOpenAIOfficial._mc_orig_text_chat_stream

        async def text_chat(self, **kwargs):
            kwargs = await _mc_wrap_payload(self, kwargs)
            return await orig_chat(self, **kwargs)

        async def text_chat_stream(self, **kwargs):
            kwargs = await _mc_wrap_payload(self, kwargs)
            async for chunk in orig_stream(self, **kwargs):
                yield chunk

        text_chat._mc_patched = True  # type: ignore[attr-defined]
        text_chat_stream._mc_patched = True  # type: ignore[attr-defined]

        ProviderOpenAIOfficial.text_chat = text_chat
        ProviderOpenAIOfficial.text_chat_stream = text_chat_stream
        _PATCH_INSTALLED = True
        _write_patch_status({"installed": True, "error": None})
        logger.info("[mc] provider patch installed (all OpenAI-compatible providers)")
        return True
    except Exception as exc:
        _write_patch_status({"installed": False, "error": str(exc)})
        logger.error("[mc] 安装 provider patch 失败: %s", exc)
        return False


def _write_patch_status(payload: dict) -> None:
    """给运维排查用：确认插件 __init__ 真的跑了、patch 是否成功。"""
    try:
        import time as _t

        data = dict(payload)
        data["ts"] = _t.time()
        Path("/tmp/mc_patch_status.json").write_text(
            json.dumps(data, ensure_ascii=False), "utf-8"
        )
    except Exception:
        pass


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


MC_DEFAULT_CONFIG = {
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
}


class MCProvider(ProviderOpenAIOfficial):
    """包在 OpenAI provider 外面的 transform 层。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        cfg = provider_config or {}
        # 这个类自己会 transform，避免被全局 patch 再包一层
        self._mc_patch_disabled = True
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


def _register_provider_type() -> None:
    """幂等注册 mc_openai。

    AstrBot 重载插件时模块会被重新 import，装饰器二次执行会撞上
    “适配器已注册”的保护，所以这里先摘掉旧登记再注册。
    """
    try:
        from astrbot.core.provider import register as _reg

        _reg.provider_cls_map.pop("mc_openai", None)
        try:
            _reg.provider_registry[:] = [
                m
                for m in _reg.provider_registry
                if getattr(m, "type", None) != "mc_openai"
            ]
        except Exception:
            pass
        decorator = _reg.register_provider_adapter(
            "mc_openai",
            "OpenAI 兼容 + Magic Context 上下文管理（每轮 model call 走 ck-mc transform）",
            default_config_tmpl=dict(MC_DEFAULT_CONFIG),
        )
        decorator(MCProvider)
    except Exception as exc:
        logger.warning("[mc] 注册 provider 类型失败（通常可忽略，patch 才是主路径）: %s", exc)


@register(PLUGIN_NAME, "柳烟", "Magic Context 上下文管理（provider 层）", "0.1.0", "")
class MagicContextPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config=config)
        self.config = config or {}
        self._state_file = _state_file()
        # 从插件配置面板读取（_conf_schema.json）
        self._apply_plugin_config()
        # 覆盖现有 provider：不需要在 WebUI 新建类型，所有 OpenAI 兼容 provider 直接生效
        install_provider_patch()
        # 同时注册可选的新类型（幂等，避免重载时撞“已注册”保护）
        _register_provider_type()
        # 注册 Magic Context 的运维工具（facade 形式的 ctx_* 需要 thalamus 网关，
        # 本部署没有，所以只暴露 method 形式可用的能力）
        try:
            self.context.add_llm_tools(MCWrapupTool(), MCStatusTool())
        except Exception as exc:
            logger.warning("[mc] 注册工具失败: %s", exc)
        logger.info(
            "[mc] 插件已加载；enabled=%s historian_model=%s adapter=%s",
            _CFG.get("enabled"),
            _CFG.get("historian_model") or "(跟随会话)",
            _CFG.get("adapter_url"),
        )

    def _apply_plugin_config(self) -> None:
        """读取 _conf_schema.json 的配置面板值，并同步给 adapter。"""
        global _SHARED_CLIENT
        cfg = self.config or {}
        _CFG["enabled"] = bool(cfg.get("enabled", True))
        url = str(cfg.get("mc_adapter_url") or DEFAULT_ADAPTER_URL)
        _CFG["adapter_url"] = url
        timeout = float(cfg.get("mc_timeout") or 8)

        # 重建共享 client，使地址/超时改动立即生效
        _SHARED_CLIENT = MCClient(url, _state_file(), timeout=timeout)

        chain: dict[str, dict] = {}

        hist = self._resolve_provider(str(cfg.get("historian_provider_id") or "").strip())
        if hist:
            chain["historian"] = hist
            _CFG["historian_model"] = hist.get("model") or ""
        else:
            _CFG["historian_model"] = ""

        # dreamer 留空则沿用 historian
        dreamer_pid = str(cfg.get("dreamer_provider_id") or "").strip()
        dreamer = self._resolve_provider(dreamer_pid) if dreamer_pid else hist
        if dreamer:
            chain["dreamer"] = dreamer

        emb_pid = str(cfg.get("embedding_provider_id") or "").strip()
        emb = self._resolve_provider(emb_pid) if emb_pid else None
        if emb:
            chain["embedding"] = emb

        if chain:
            _write_llm_config(chain)
        _CFG["models"] = {
            k: v.get("model") or "" for k, v in chain.items()
        }

    def _resolve_provider(self, provider_id: str) -> dict | None:
        """从 AstrBot 配置里解析所选模型的 base_url / key / model。"""
        if not provider_id:
            return None
        try:
            conf = self.context.get_config()
            providers = conf.get("provider", []) or []
            sources = conf.get("provider_sources", []) or []
            item = next(
                (p for p in providers if isinstance(p, dict) and p.get("id") == provider_id),
                None,
            )
            if not item:
                logger.warning("[mc] 找不到 provider: %s", provider_id)
                return None
            src_id = item.get("provider_source_id")
            src = next(
                (s for s in sources if isinstance(s, dict) and s.get("id") == src_id),
                {},
            )
            key = src.get("key") or ""
            if isinstance(key, list):
                key = key[0] if key else ""
            return {
                "base_url": str(src.get("api_base") or ""),
                "api_key": str(key),
                "model": str(item.get("model") or ""),
            }
        except Exception as exc:
            logger.warning("[mc] 解析 provider 失败: %s", exc)
            return None

    @filter.on_llm_request()
    async def on_llm_request_mc(self, event, req) -> None:
        """记录 conversation → UMO 映射；patch 没挂上时兜底做一次 transform。"""
        try:
            conv = getattr(req, "conversation", None)
            conv_id = getattr(conv, "cid", None) if conv is not None else None
            req_sid = getattr(req, "session_id", None)
            # patch 侧实际能拿到的是 payload 的 conversation_id / session_id，
            # 这里把两者都映射到 UMO（UMO 跨 /reset 稳定，是首选 key）
            for key in (conv_id, req_sid):
                if key:
                    _CONV_TO_UMO[str(key)] = event.unified_msg_origin

            if _PATCH_INSTALLED:
                # patch 会在每次 model call 前处理（含工具循环内的每一圈）
                return

            contexts = getattr(req, "contexts", None)
            if not contexts:
                return
            raw = [m if isinstance(m, dict) else getattr(m, "model_dump", lambda: {})()
                   for m in contexts]
            if not raw:
                return
            _trace_patch_call(self, {"contexts": raw, "session_id": event.unified_msg_origin})
            transformed = await _shared_client().transform(
                event.unified_msg_origin, raw
            )
            if transformed:
                req.contexts = transformed
        except Exception as exc:
            logger.warning("[mc] on_llm_request 处理失败，跳过: %s", exc)

    @filter.command("mc")
    async def mc_command(self, event: AstrMessageEvent, sub: str = ""):
        """Magic Context 运维指令：/mc update | /mc status | /mc help

        更新完全手动：不做任何定时拉取，只有这条指令才碰上游仓库。
        """
        sub = (sub or "").strip().lower()

        if sub in ("", "help"):
            yield event.plain_result(
                "Magic Context\n"
                "/mc status  查看当前会话压缩状态\n"
                "/mc update  手动更新 ck-mc（拉源码→编译→备份→切换→重启）"
            )
            return

        if sub == "status":
            try:
                res = await _shared_client().status(event.unified_msg_origin)
                yield event.plain_result(
                    f"compartments={res.get('compartment_count')} "
                    f"tokens={res.get('compartment_tokens')} "
                    f"boundary={res.get('boundary_present')}"
                )
            except Exception as exc:
                yield event.plain_result(f"mc status 失败: {exc}")
            return

        if sub in ("update", "up"):
            yield event.plain_result("开始手动更新 ck-mc，可能要几分钟…")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bash",
                    str(_UPDATE_SCRIPT),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
                text = (out or b"").decode("utf-8", "replace").strip()
                tail = text[-1500:] if len(text) > 1500 else text
                yield event.plain_result(
                    f"更新结束 (exit={proc.returncode})\n{tail}"
                )
            except asyncio.TimeoutError:
                yield event.plain_result("更新超时（>15 分钟），请查后台日志")
            except Exception as exc:
                yield event.plain_result(f"更新失败: {exc}")
            return

        yield event.plain_result(f"未知子命令：{sub}（可用：status / update）")

    async def terminate(self) -> None:
        logger.info("[mc] 插件已卸载")
