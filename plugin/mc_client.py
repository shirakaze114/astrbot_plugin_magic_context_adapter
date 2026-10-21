"""OpenAI messages <-> Magic Context CK wire 的映射与 HTTP 客户端。

只做翻译，不做任何"丢什么/压什么"的决策——决策全部在 ck-mc 内部。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import aiohttp

SERIALIZER_PROFILE = "owned-llmrunner"


class MCClient:
    def __init__(self, base_url: str, state_path: Path, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.state_path = state_path
        self.timeout = timeout
        self._state: dict[str, Any] = self._load()
        self._lock = asyncio.Lock()

    # ---------- ordinal / 身份持久化 ----------

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _ordinal_for(self, session_id: str, fingerprint: str) -> int:
        sess = self._state.setdefault(session_id, {"next": 1, "map": {}})
        mapping: dict[str, int] = sess.setdefault("map", {})
        if fingerprint in mapping:
            return mapping[fingerprint]
        n = int(sess.get("next", 1))
        sess["next"] = n + 1
        mapping[fingerprint] = n
        # 控制状态体积：只保留最近 2000 条
        if len(mapping) > 4000:
            keep = sorted(mapping.items(), key=lambda kv: kv[1])[-2000:]
            sess["map"] = dict(keep)
        self._save()
        return n

    @staticmethod
    def _fingerprint(role: str, content: Any, tool_calls: Any, tool_call_id: Any) -> str:
        h = hashlib.sha256()
        h.update(role.encode("utf-8"))
        h.update(b"\x00")
        if isinstance(content, str):
            h.update(content.encode("utf-8"))
        else:
            h.update(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if tool_calls:
            h.update(json.dumps(tool_calls, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if tool_call_id:
            h.update(str(tool_call_id).encode("utf-8"))
        return h.hexdigest()[:40]

    # ---------- OpenAI -> CK ----------

    @staticmethod
    def _tool_output(raw: Any) -> dict[str, Any]:
        """把 OpenAI 的 tool 结果转成 CkToolOutput。"""
        if isinstance(raw, str):
            return {"kind": {"type": "text", "text": raw}}
        if raw is None:
            return {"kind": {"type": "text", "text": ""}}
        return {"kind": {"type": "json", "value": raw}}

    def to_ck_messages(self, session_id: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen: dict[str, int] = {}
        for idx, msg in enumerate(messages):
            role = str(msg.get("role") or "user")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id")

            fp = self._fingerprint(role, content, tool_calls, tool_call_id)
            # 同内容可能出现多次（例如两轮都是"你好"），加出现序号保证唯一，
            # 否则会分到同一个 ordinal，mc 会拒绝（要求严格递增）
            occ = seen.get(fp, 0)
            seen[fp] = occ + 1
            ordinal = self._ordinal_for(session_id, f"{fp}#{occ}")

            blocks: list[dict] = []
            if role == "tool":
                blocks.append(
                    {
                        "kind": {
                            "type": "tool_result",
                            "id": str(tool_call_id or ""),
                            "tool_name": str(msg.get("name") or "tool"),
                            "output": self._tool_output(content),
                        }
                    }
                )
            else:
                if isinstance(content, str):
                    if content:
                        blocks.append({"kind": {"type": "text", "text": content}})
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        ptype = part.get("type")
                        if ptype == "text":
                            blocks.append({"kind": {"type": "text", "text": part.get("text", "")}})
                        elif ptype in ("image_url", "image"):
                            # CkKind::Media 需要 kind/media_type/source。
                            # 图片按 media 透传，回填时再还原成 image_url，
                            # 这样 mc 统计 token 时看得见，模型也仍然能看图。
                            iu = part.get("image_url")
                            url = ""
                            if isinstance(iu, dict):
                                url = str(iu.get("url") or "")
                            elif isinstance(iu, str):
                                url = iu
                            if not url:
                                url = str(part.get("url") or "")

                            media_type = "image/jpeg"
                            source: dict[str, Any]
                            if url.startswith("data:"):
                                head, _, payload = url.partition(",")
                                media_type = head[5:].split(";")[0] or media_type
                                source = {"type": "data_base64", "data": payload}
                            elif url:
                                source = {"type": "url", "url": url}
                            else:
                                source = {"type": "opaque", "raw": part}

                            blocks.append(
                                {
                                    "kind": {
                                        # 内部标签枚举：MediaBlock 的字段与 tag 平铺
                                        "type": "media",
                                        "kind": "image",
                                        "media_type": media_type,
                                        "source": source,
                                    }
                                }
                            )
                if tool_calls:
                    for tc in tool_calls:
                        fn = (tc or {}).get("function") or {}
                        raw_args = fn.get("arguments")
                        if isinstance(raw_args, str):
                            try:
                                parsed = json.loads(raw_args) if raw_args.strip() else {}
                            except Exception:
                                parsed = {"__raw": raw_args}
                        else:
                            parsed = raw_args if raw_args is not None else {}
                        blocks.append(
                            {
                                "kind": {
                                    "type": "tool_call",
                                    "id": str((tc or {}).get("id") or ""),
                                    "name": str(fn.get("name") or ""),
                                    "input": parsed,
                                }
                            }
                        )
                if not blocks:
                    blocks.append({"kind": {"type": "text", "text": ""}})

            out.append(
                {
                    "mid": f"{idx}-{fp[:12]}",
                    "ordinal": ordinal,
                    "ck": {"role": role, "content": blocks},
                }
            )
        return out

    # ---------- CK -> OpenAI ----------

    @staticmethod
    def from_ck_messages(ck_messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for msg in ck_messages or []:
            role = msg.get("role") or "user"
            texts: list[str] = []
            tool_calls: list[dict] = []
            tool_name = None
            tool_call_id = None
            tool_output = None

            media_parts: list[dict] = []
            for block in msg.get("content") or []:
                kind = block.get("kind") or {}
                btype = kind.get("type")
                if btype == "text":
                    texts.append(kind.get("text") or "")
                elif btype == "reasoning":
                    continue
                elif btype == "media":
                    # CkKind 是内部标签枚举：MediaBlock 的字段与 `type` 平铺在同一层
                    media = kind
                    src = media.get("source") or {}
                    stype = src.get("type")
                    if stype == "url":
                        url = str(src.get("url") or "")
                    elif stype == "data_base64":
                        mime = str(media.get("media_type") or "image/jpeg")
                        url = f"data:{mime};base64,{src.get('data') or ''}"
                    else:
                        continue
                    if url:
                        media_parts.append(
                            {"type": "image_url", "image_url": {"url": url}}
                        )
                elif btype == "tool_call":
                    tool_calls.append(
                        {
                            "id": kind.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": kind.get("name") or "",
                                "arguments": json.dumps(
                                    kind.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
                elif btype == "tool_result":
                    tool_call_id = kind.get("id") or ""
                    tool_name = kind.get("tool_name") or "tool"
                    out_kind = (kind.get("output") or {}).get("kind") or {}
                    if out_kind.get("type") in ("text", "error_text"):
                        tool_output = out_kind.get("text") or ""
                    elif out_kind.get("type") in ("json", "error_json"):
                        tool_output = json.dumps(out_kind.get("value"), ensure_ascii=False)
                    else:
                        tool_output = json.dumps(out_kind, ensure_ascii=False)

            if role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id or "",
                        "name": tool_name or "tool",
                        "content": tool_output or "",
                    }
                )
                continue

            joined = "\n".join(t for t in texts if t)
            if media_parts:
                content: Any = []
                if joined:
                    content.append({"type": "text", "text": joined})
                content.extend(media_parts)
            else:
                content = joined

            entry: dict[str, Any] = {"role": role, "content": content}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)

        # OpenAI 兼容端点要求 system 在首位，而 mc 会把 m[0]/m[1] 插到最前面；
        # 这里把 system 消息提回顶部（保持相互顺序），避免上游把 system 当普通消息。
        systems = [m for m in out if m.get("role") == "system"]
        if systems:
            out = systems + [m for m in out if m.get("role") != "system"]
        return out

    # ---------- HTTP ----------

    async def call(self, payload: dict) -> dict:
        """通用转发：把任意 request body 交给 adapter → ck-mc。

        用于 method 形式的运维接口（session.wrapup / session.status 等）。
        facade 形式（ctx_* 工具）需要 thalamus 网关，本部署未安装，暂不可用。
        """
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                f"{self.base_url}/v1/transform", json=payload
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"mc-adapter {resp.status}: {body[:400]}")
                return json.loads(body)

    async def wrapup(
        self, session_id: str, historian_models: list[str] | None = None
    ) -> dict:
        """手动触发历史压缩（ck-mc 的 session.wrapup）。"""
        payload: dict[str, Any] = {
            "method": "session.wrapup",
            "v": 1,
            "session_id": session_id,
        }
        # 不带模型链时 ck-mc 会直接返回 no_models，压缩永远不会发生
        if historian_models:
            payload["historian_model_chain"] = historian_models
        return await self.call(payload)

    async def status(self, session_id: str) -> dict:
        """查询该会话的 compartment / 覆盖情况。"""
        return await self.call(
            {"method": "session.status", "v": 1, "session_id": session_id}
        )

    async def transform(
        self,
        session_id: str,
        messages: list[dict],
        render_config: str = "astrbot-v1",
        historian_models: list[str] | None = None,
    ) -> list[dict]:
        payload = {
            "kind": "transform",
            "v": 2,
            "serializer_profile": SERIALIZER_PROFILE,
            "session_id": session_id,
            "render_config": render_config,
            "messages": self.to_ck_messages(session_id, messages),
        }
        # historian 的模型链随请求携带；不传的话 session.wrapup 会返回 no_models
        if historian_models:
            payload["historian_model_chain"] = historian_models
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{self.base_url}/v1/transform", json=payload) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"mc-adapter {resp.status}: {body[:400]}")
                data = json.loads(body)
        if data.get("status") != "ok":
            raise RuntimeError(f"mc transform error: {data}")
        return self.from_ck_messages(data.get("ck_messages") or [])
