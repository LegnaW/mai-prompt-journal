"""LLM 直连、去重整理、操作数据库与批量导入的 agent 循环（mixin）。"""

import json
import time
import uuid
from typing import Any

import aiohttp

from .constants import (
    _DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT,
    _ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT,
    _ORGANIZE_DB_SEARCH_TOOL,
    _ORGANIZE_DEFAULT_REQUIREMENT,
)
from .json_utils import parse_lenient_json
from .notebook import scramble_id
from .resume import _TXT_IMPORT_STATE_FILE, save_json
from .retry import run_task_item, run_with_retry

_JSON_PARSE_HINTS = {
    "no_json": "LLM 返回内容中没有找到 JSON",
    "truncated": "LLM 返回内容疑似被截断（JSON 未闭合）",
    "parse_failed": "LLM 返回内容无法解析为 JSON",
}


def _format_json_parse_error(reason: str | None, response_text: str) -> str:
    """把解析失败原因格式化为给用户/日志的中文提示。"""
    hint = _JSON_PARSE_HINTS.get(reason or "", "LLM 返回内容无法解析为 JSON")
    return f"{hint}，完整输出：\n{response_text}"

class OrganizeMixin:

    async def _direct_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """直连 OpenAI 兼容 LLM API（非流式），返回 {success, content, reasoning_content, tool_calls}。

        不依赖麦麦自带 LLM 管线，可自由控制消息载荷（含 reasoning_content 回传），
        避免多轮工具调用时推理字段丢失导致 API 400。
        """
        from aiohttp import ClientSession, ClientTimeout

        cfg = self.config.llm
        base_url = str(cfg.base_url or "").strip().rstrip("/")
        api_key = str(cfg.api_key or "").strip()
        model = str(cfg.model or "").strip()
        if not base_url or not api_key or not model:
            return {"success": False, "error": "LLM 直连配置不完整，请填写 [llm] 的 base_url / api_key / model"}

        request_body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": max(0.0, min(2.0, float(cfg.temperature if cfg.temperature is not None else 0.2))),
            "max_tokens": max(1, min(65536, int(cfg.max_tokens if cfg.max_tokens is not None else 10000))),
        }
        if tools:
            request_body["tools"] = tools

        extra_params_str = str(cfg.extra_params or "").strip()
        if extra_params_str:
            try:
                extra = json.loads(extra_params_str)
            except json.JSONDecodeError as exc:
                self.ctx.logger.warning(f"llm.extra_params JSON 解析失败: {exc}")
                return {"success": False, "error": f"llm.extra_params JSON 格式无效: {exc}"}
            if isinstance(extra, dict):
                request_body.update(extra)

        url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        timeout_secs = max(5, int(cfg.timeout if cfg.timeout is not None else 120))

        async def attempt() -> tuple[dict[str, Any], str | None]:
            try:
                timeout = ClientTimeout(total=timeout_secs)
                async with ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=request_body, headers=headers) as resp:
                        status = resp.status
                        resp_body = await resp.json()
            except aiohttp.ClientError as exc:
                return {"success": False, "error": f"LLM API 请求失败: {exc}"}, f"LLM API 请求失败: {exc}"
            except Exception as exc:
                return {"success": False, "error": f"LLM API 请求异常: {exc}"}, f"LLM API 请求异常: {exc}"

            if status != 200:
                msg = f"LLM API 返回错误({status}): {str(resp_body)[:300]}"
                return {"success": False, "error": msg}, msg

            try:
                choice = resp_body["choices"][0]
                message = choice.get("message") or {}
            except (KeyError, IndexError, TypeError):
                return {"success": False, "error": "LLM API 响应格式异常"}, "LLM API 响应格式异常"

            content = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or message.get("reasoning") or "")

            tool_calls: list[dict[str, Any]] = []
            raw_tool_calls = message.get("tool_calls") or []
            for raw in raw_tool_calls:
                if not isinstance(raw, dict):
                    continue
                call_id = str(raw.get("id") or "")
                function = raw.get("function") or {}
                name = str(function.get("name") or "")
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                if call_id and name:
                    tool_calls.append({"id": call_id, "function": {"name": name, "arguments": arguments}})

            return {
                "success": True,
                "content": content,
                "reasoning_content": reasoning,
                "tool_calls": tool_calls or None,
            }, None

        result, error = await run_with_retry(attempt, label="LLM", logger=self.ctx.logger)
        if result is None and error is not None:
            self.ctx.logger.error(error)
            return {"success": False, "error": error}
        return result

    async def _organize_with_llm(
        self, entries: list[dict[str, Any]], requirement: str = ""
    ) -> dict[str, Any]:
        """将一组重复笔记交给 LLM 整理。

        成功返回 {reason, entries}；失败返回 {"_error": "llm", "message": 具体错误}。
        """
        cfg = self.config.dedup_merge
        if not cfg.enabled:
            self.ctx.logger.warning("LLM 整理已禁用（dedup_merge.enabled=false）")
            return {"_error": "llm", "message": "LLM 整理已禁用（dedup_merge.enabled=false）"}

        system_prompt = (
            str(self.config.advanced.dedup_merge_system_prompt or "").strip()
            or _DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT
        )
        requirement_text = str(requirement or "").strip() or _ORGANIZE_DEFAULT_REQUIREMENT

        entries_text = "\n".join(
            f"- en: {e.get('en', '')} | zh: {e.get('zh', '')}"
            + (f" | note: {e['note']}" if e.get("note") else "")
            for e in entries
        )
        user_content = f"用户整理要求：{requirement_text}\n\n待整理的重复笔记：\n{entries_text}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        result = await self._direct_chat(messages)
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error", "unknown") if isinstance(result, dict) else result
            self.ctx.logger.warning(f"LLM 整理调用失败: {error}")
            return {"_error": "llm", "message": f"LLM 调用失败：{error}"}

        response_text = str(result.get("content", "") or "").strip()
        if not response_text:
            self.ctx.logger.warning("LLM 整理返回空内容")
            return {"_error": "llm", "message": "LLM 返回空内容"}

        payload, parse_reason = parse_lenient_json(response_text)
        if payload is None:
            self.ctx.logger.warning(f"LLM 整理返回无法解析的 JSON: {response_text[:200]}")
            return {"_error": "llm", "message": _format_json_parse_error(parse_reason, response_text)}

        reason = str(payload.get("reason", "") or "").strip()
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list) or not raw_entries:
            self.ctx.logger.warning(f"LLM 整理返回的 entries 无效: {response_text[:200]}")
            return {"_error": "llm", "message": f"LLM 返回的 entries 无效，完整输出：\n{response_text}"}

        clean_entries: list[dict[str, Any]] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            en = str(item.get("en", "") or "").strip()
            zh = str(item.get("zh", "") or "").strip()
            note = str(item.get("note", "") or "").strip()
            if not en or not zh:
                continue
            clean_entries.append({"en": en, "zh": zh, "note": note})
        if not clean_entries:
            self.ctx.logger.warning(f"LLM 整理返回的条目缺少 en/zh: {response_text[:200]}")
            return {"_error": "llm", "message": f"LLM 返回的条目缺少 en/zh，完整输出：\n{response_text}"}

        return {"reason": reason, "entries": clean_entries}

    async def _execute_search_notes(self, keyword: str, notebook_name: str = "", limit: int = 10) -> str:
        """执行 search_notes 工具：按关键词语义检索笔记，返回文本结果。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return "检索失败：关键词不能为空"
        nb_name = str(notebook_name or "").strip() or "default"
        top_k = max(1, min(50, int(limit or 10)))

        nb = self._get_notebook(nb_name)
        if nb is None:
            return f"笔记本 '{nb_name}' 不存在"
        if not nb.check_consistency():
            return f"笔记本 '{nb_name}' 索引失效，请先 /mpj rebuild"

        query_vec = await self._embed_single(keyword)
        if query_vec is None:
            return "检索失败：embedding 服务不可用"

        results = await self._search_single_notebook(
            nb, keyword, query_vec, top_k, float(self.config.journal.min_score)
        )
        if not results:
            return "未找到相关笔记"
        lines = [f"找到 {len(results)} 条相关笔记："]
        for i, r in enumerate(results, 1):
            note_part = f" — {r['note']}" if r.get("note") else ""
            lines.append(f'{i}. id={r["id"]} {r["en"]} / {r["zh"]}{note_part} (相似度 {r["score"]:.2f})')
        return "\n".join(lines)

    async def _execute_search_notes_multi(
        self, keyword: str, notebook_names: list[str], limit: int = 10
    ) -> str:
        """跨多个引用笔记本 + 临时笔记本 tmp 语义检索，合并返回文本结果（批量导入用）。"""
        keyword = str(keyword or "").strip()
        if not keyword:
            return "检索失败：关键词不能为空"
        top_k = max(1, min(50, int(limit or 10)))

        query_vec = await self._embed_single(keyword)
        if query_vec is None:
            return "检索失败：embedding 服务不可用"

        # 笔记本集合 = 引用笔记本 + 临时笔记本
        names = list(notebook_names or [])
        if "tmp" not in names:
            names.append("tmp")

        merged: list[dict[str, Any]] = []
        for name in names:
            if name == "tmp":
                nb = self._tmp_nb
            else:
                nb = self._get_notebook(name)
            if nb is None:
                continue
            if not nb.check_consistency():
                continue
            results = await self._search_single_notebook(
                nb, keyword, query_vec, top_k, float(self.config.journal.min_score)
            )
            merged.extend(results)

        if not merged:
            return "未找到相关笔记"
        merged.sort(key=lambda x: x["score"], reverse=True)
        merged = merged[:top_k]

        lines = [f"找到 {len(merged)} 条相关笔记："]
        for i, r in enumerate(merged, 1):
            note_part = f" — {r['note']}" if r.get("note") else ""
            lines.append(
                f'{i}. [{r["notebook"]}] id={r["id"]} {r["en"]} / {r["zh"]}{note_part} '
                f"(相似度 {r['score']:.2f})"
            )
        return "\n".join(lines)

    async def _run_organize_db_round(
        self,
        messages: list[dict[str, Any]],
        cfg: Any,
        progress: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
        """跑一轮 agent 循环。

        成功返回 (plan, messages, None)；失败返回 (None, messages, 错误信息)。
        执行 search_notes 时会向 progress["searches"] 追加检索记录用于前端展示进度。
        """
        max_iterations = max(1, int(cfg.max_iterations or 8))
        for _ in range(max_iterations):
            result = await self._direct_chat(messages, tools=[_ORGANIZE_DB_SEARCH_TOOL])
            if not isinstance(result, dict) or not result.get("success"):
                error = result.get("error", "unknown") if isinstance(result, dict) else result
                self.ctx.logger.warning(f"LLM 操作数据库调用失败: {error}")
                return None, messages, f"LLM 调用失败：{error}"

            tool_calls = result.get("tool_calls")
            if tool_calls:
                # 回传 assistant 消息时，tool_calls 需还原为 API 格式：
                # function.arguments 必须是 JSON 字符串（dict 会被网关 serde 拒绝），并补 type: function
                api_tool_calls: list[dict[str, Any]] = []
                for call in tool_calls:
                    func = dict(call.get("function") or {})
                    if isinstance(func.get("arguments"), dict):
                        func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
                    api_tool_calls.append({"id": str(call.get("id") or ""), "type": "function", "function": func})

                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": str(result.get("content") or ""),
                    "tool_calls": api_tool_calls,
                }
                # thinking 模型要求 reasoning_content 原样回传，否则 API 400
                if result.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = result["reasoning_content"]
                messages.append(assistant_msg)
                for call in tool_calls:
                    call_id = str(call.get("id") or "")
                    func = call.get("function") or {}
                    name = str(func.get("name") or "")
                    args = func.get("arguments") or {}
                    if not isinstance(args, dict):
                        args = {}
                    if name == "search_notes":
                        tool_result = await self._execute_search_notes(
                            args.get("keyword", ""),
                            args.get("notebook", ""),
                            args.get("limit", cfg.search_limit),
                        )
                        if progress is not None:
                            progress["searches"].append(
                                {
                                    "keyword": str(args.get("keyword", "") or ""),
                                    "notebook": str(args.get("notebook", "") or "").strip() or "default",
                                }
                            )
                    else:
                        tool_result = f"未知工具: {name}"
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_result})
                continue

            response_text = str(result.get("content", "") or "").strip()
            if not response_text:
                self.ctx.logger.warning("LLM 操作数据库返回空内容")
                return None, messages, "LLM 返回空内容"
            payload, parse_reason = parse_lenient_json(response_text)
            if payload is None:
                self.ctx.logger.warning(f"LLM 操作数据库返回无法解析的 JSON: {response_text[:200]}")
                return None, messages, _format_json_parse_error(parse_reason, response_text)
            raw_ops = payload.get("operations")
            if not isinstance(raw_ops, list):
                self.ctx.logger.warning(f"LLM 操作数据库返回的 operations 无效: {response_text[:200]}")
                return None, messages, f"LLM 返回的 operations 无效，完整输出：\n{response_text}"
            reason = str(payload.get("reason", "") or "").strip()
            operations = [o for o in raw_ops if isinstance(o, dict)]
            return {"reason": reason, "operations": operations}, messages, None

        self.ctx.logger.warning("LLM 操作数据库达到最大迭代次数")
        return None, messages, "LLM 检索达到最大迭代次数"

    def _evict_organize_sessions(self) -> None:
        """会话数量超限时按创建时间淘汰最旧会话。"""
        limit = 20
        if len(self._organize_sessions) <= limit:
            return
        oldest = sorted(self._organize_sessions.items(), key=lambda kv: kv[1]["created_at"])
        for sid, _ in oldest[: len(self._organize_sessions) - limit]:
            self._organize_sessions.pop(sid, None)

    async def _organize_db_plan(
        self,
        notebook_name: str,
        requirement: str = "",
        session_id: str = "",
        progress: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """驱动 LLM 操作笔记本，支持多轮会话。

        无 session_id → 新建会话跑初始轮；有 session_id → 追加补充要求后重跑覆盖。
        成功返回 {session_id, reason, operations}；错误返回 {"_error": ..., "message": ...}。
        """
        cfg = self.config.organize_db
        if not cfg.enabled:
            self.ctx.logger.warning("LLM 操作数据库已禁用（organize_db.enabled=false）")
            return {"_error": "llm", "message": "LLM 操作数据库已禁用"}

        nb = self._get_notebook(notebook_name)
        if nb is None:
            return {"_error": "llm", "message": f"笔记本 '{notebook_name}' 不存在"}
        total = nb.count_notes()
        sid = str(session_id or "").strip()
        req_text = str(requirement or "").strip()

        if sid:
            session = self._organize_sessions.get(sid)
            if session is None or session["notebook"] != notebook_name:
                return {"_error": "expired"}
            if not req_text:
                return {"_error": "empty_requirement"}
            messages = session["messages"]
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"用户补充要求：{req_text}\n\n"
                        "请基于以上完整对话重新输出一份完整的操作方案，覆盖你之前给出的方案，不要只输出差异部分。"
                    ),
                }
            )
        else:
            system_prompt = (
                str(self.config.advanced.organize_db_system_prompt or "").strip()
                or _ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT
            )
            user_parts = [
                f"请操作笔记本 '{notebook_name}'（当前共 {total} 条笔记）。",
            ]
            if req_text:
                user_parts.append(f"用户操作要求：{req_text}")
            user_parts.append("你可以调用 search_notes 工具按关键词检索内容，最终输出操作方案 JSON。")
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n".join(user_parts)},
            ]
            sid = uuid.uuid4().hex
            self._organize_sessions[sid] = {
                "notebook": notebook_name,
                "messages": messages,
                "created_at": time.time(),
            }

        plan, updated_messages, error = await self._run_organize_db_round(messages, cfg, progress)
        if error is not None:
            return {"_error": "llm", "message": error}
        session = self._organize_sessions.get(sid)
        if session is not None:
            session["messages"] = updated_messages
        self._evict_organize_sessions()
        return {"session_id": sid, "reason": plan["reason"], "operations": plan["operations"]}

    @staticmethod
    def _validate_organize_operations(operations: list[dict[str, Any]], id_set: set[str]) -> str | None:
        """校验 LLM 返回的修改方案，返回错误信息或 None（通过）。"""
        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                return f"第 {i + 1} 条操作不是对象"
            op_type = str(op.get("type") or "")
            if op_type == "create":
                en = str(op.get("en", "") or "").strip()
                zh = str(op.get("zh", "") or "").strip()
                if not en or not zh:
                    return f"create 操作缺少 en 或 zh（第 {i + 1} 条）"
            elif op_type == "update":
                op_id = str(op.get("id", "") or "").strip()
                if not op_id or op_id not in id_set:
                    return f"update 操作的 id 不存在: {op_id}"
                if not any(k in op for k in ("en", "zh", "note")):
                    return f"update 操作未提供任何要修改的字段（第 {i + 1} 条）"
                if "en" in op and not str(op["en"] or "").strip():
                    return f"update 操作的 en 不能为空（第 {i + 1} 条）"
                if "zh" in op and not str(op["zh"] or "").strip():
                    return f"update 操作的 zh 不能为空（第 {i + 1} 条）"
            elif op_type == "delete":
                op_id = str(op.get("id", "") or "").strip()
                if not op_id or op_id not in id_set:
                    return f"delete 操作的 id 不存在: {op_id}"
            else:
                return f"未知操作类型: {op_type}（第 {i + 1} 条）"
        return None

    def _reset_tmp_import(self) -> None:
        """清空临时笔记本目录，重建空 tmp 笔记本（下一轮导入开始前调用）。"""
        if self._tmp_import_dir.exists():
            for p in self._tmp_import_dir.iterdir():
                if p.is_file():
                    p.unlink()
        self._tmp_import_dir.mkdir(parents=True, exist_ok=True)
        nb = self._tmp_nb
        nb.notes_path.write_text("", encoding="utf-8")
        nb.cache_path.write_text("", encoding="utf-8")
        nb.embeddings_path.write_bytes(b"")
        nb.save_meta({"md5": "", "count": 0, "built_at": time.time()})

    def _append_import_log(self, text: str) -> None:
        try:
            with self._tmp_log_path.open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception as exc:
            self.ctx.logger.warning(f"写入导入日志失败: {exc}")

    async def _apply_ops_to_tmp(
        self, operations: list[dict[str, Any]], reason: str
    ) -> tuple[bool, str]:
        """把 LLM 输出的 operations 应用到临时笔记本 tmp，重建索引。返回 (ok, error)。"""
        nb = self._tmp_nb
        try:
            entries = nb.load_notes()
            id_set = {e["id"] for e in entries}
            validate_error = self._validate_organize_operations(operations, id_set)
            if validate_error:
                return False, f"方案校验失败：{validate_error}"

            entries_by_id = {e["id"]: e for e in entries}
            base_ts_ms = int(time.time() * 1000)
            now = time.time()
            created: list[dict[str, Any]] = []
            for i, op in enumerate(operations):
                op_type = str(op.get("type") or "")
                if op_type == "create":
                    created.append(
                        {
                            "id": scramble_id(base_ts_ms + i),
                            "en": str(op.get("en", "") or "").strip(),
                            "zh": str(op.get("zh", "") or "").strip(),
                            "note": str(op.get("note", "") or "").strip(),
                            "ts": now,
                        }
                    )
                elif op_type == "update":
                    target = entries_by_id.get(str(op.get("id", "") or "").strip())
                    if target is None:
                        return False, f"update 操作 id 不存在: {op.get('id')}"
                    if "en" in op:
                        target["en"] = str(op["en"] or "").strip()
                    if "zh" in op:
                        target["zh"] = str(op["zh"] or "").strip()
                    if "note" in op:
                        target["note"] = str(op["note"] or "").strip()
                elif op_type == "delete":
                    op_id = str(op.get("id", "") or "").strip()
                    entries = [e for e in entries if e["id"] != op_id]
                    entries_by_id.pop(op_id, None)

            final_entries = entries + created
            json_str = "\n".join(json.dumps(e, ensure_ascii=False) for e in final_entries)
            if json_str:
                json_str += "\n"
            nb.notes_path.parent.mkdir(parents=True, exist_ok=True)
            nb.notes_path.write_text(json_str, encoding="utf-8")
            await self._rebuild_notebook(nb)
            return True, ""
        except Exception as exc:
            self.ctx.logger.error(f"批量导入写入 tmp 失败: {exc}", exc_info=True)
            return False, str(exc)

    async def _run_import_segment(
        self,
        segment_text: str,
        mode_prompt: str,
        cfg: Any,
        ref_names: list[str],
        progress: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """对单段文本跑一次完整 agent 循环，返回 {ok, reason, operations, error}。"""
        system_prompt = (
            str(self.config.advanced.organize_db_system_prompt or "").strip()
            or _ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT
        )
        import_prompt = str(self.config.advanced.batch_import_prompt or "").strip()
        if import_prompt:
            import_prompt = import_prompt.replace("{temp-journal}", "tmp")
            system_prompt = f"{system_prompt}\n{import_prompt}"

        user_parts: list[str] = []
        if mode_prompt:
            user_parts.append(mode_prompt)
        user_parts.append(f"以下是需要处理的一段文本：\n{segment_text}")
        user_parts.append(
            "请对临时笔记本 tmp 执行 create/update/delete 操作（只能操作 tmp，不得触碰其他笔记本），"
            "最终输出操作方案 JSON。"
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

        max_iterations = max(1, int(cfg.max_iterations or 8))
        for _ in range(max_iterations):
            result = await self._direct_chat(messages, tools=[_ORGANIZE_DB_SEARCH_TOOL])
            if not isinstance(result, dict) or not result.get("success"):
                error = result.get("error", "unknown") if isinstance(result, dict) else result
                return {"ok": False, "error": f"LLM 调用失败：{error}"}

            tool_calls = result.get("tool_calls")
            if tool_calls:
                api_tool_calls: list[dict[str, Any]] = []
                for call in tool_calls:
                    func = dict(call.get("function") or {})
                    if isinstance(func.get("arguments"), dict):
                        func["arguments"] = json.dumps(func["arguments"], ensure_ascii=False)
                    api_tool_calls.append(
                        {"id": str(call.get("id") or ""), "type": "function", "function": func}
                    )
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": str(result.get("content") or ""),
                    "tool_calls": api_tool_calls,
                }
                if result.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = result["reasoning_content"]
                messages.append(assistant_msg)
                for call in tool_calls:
                    call_id = str(call.get("id") or "")
                    func = call.get("function") or {}
                    name = str(func.get("name") or "")
                    args = func.get("arguments") or {}
                    if not isinstance(args, dict):
                        args = {}
                    if name == "search_notes":
                        tool_result = await self._execute_search_notes_multi(
                            args.get("keyword", ""), ref_names, args.get("limit", cfg.search_limit)
                        )
                        if progress is not None:
                            progress.setdefault("searches", []).append(
                                {
                                    "keyword": str(args.get("keyword", "") or ""),
                                    "notebook": "引用+tmp",
                                }
                            )
                    else:
                        tool_result = f"未知工具: {name}"
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_result})
                continue

            response_text = str(result.get("content", "") or "").strip()
            if not response_text:
                return {"ok": False, "error": "LLM 返回空内容"}
            payload, parse_reason = parse_lenient_json(response_text)
            if payload is None:
                return {"ok": False, "error": _format_json_parse_error(parse_reason, response_text)}
            raw_ops = payload.get("operations")
            if not isinstance(raw_ops, list):
                return {"ok": False, "error": f"LLM 返回的 operations 无效，完整输出：\n{response_text}"}
            reason = str(payload.get("reason", "") or "").strip()
            operations = [o for o in raw_ops if isinstance(o, dict)]
            ok, apply_error = await self._apply_ops_to_tmp(operations, reason)
            if not ok:
                return {"ok": False, "error": apply_error}
            return {"ok": True, "reason": reason, "operations": operations}

        return {"ok": False, "error": "LLM 检索达到最大迭代次数"}

    async def _run_import_task(
        self,
        task_id: str,
        segments: list[str],
        mode_prompt: str,
        ref_names: list[str],
        resume_state: dict[str, Any] | None = None,
    ) -> None:
        """txt 批量写入后台任务。

        每段失败按 `[txt_import].max_retries` 重试；仍失败按 `[txt_import].on_failure`：
        interrupt → 缓存 `tmp_import/import.state.json` 并置任务为『中断』（可再次尝试/取消，
        跨插件重载可恢复）；skip → 跳过该段记录失败，继续下一段。
        """
        cfg = self.config.organize_db
        txt_cfg = self.config.txt_import
        max_retries = int(getattr(txt_cfg, "max_retries", 3) or 0)
        on_failure = str(getattr(txt_cfg, "on_failure", "interrupt") or "interrupt")
        if on_failure not in ("interrupt", "skip"):
            on_failure = "interrupt"
        failed: list[dict[str, Any]] = list((resume_state or {}).get("failed") or [])
        seg_status = list((resume_state or {}).get("segment_status") or ["pending"] * len(segments))
        total = len(segments)
        start_idx = int((resume_state or {}).get("current_index", 0))
        state_path = self._tmp_import_dir / _TXT_IMPORT_STATE_FILE

        # current 为"下一个待处理段的索引"，随进度更新，保证中断后能准确续跑
        current = start_idx

        def _persist(status: str) -> None:
            save_json(
                state_path,
                {
                    "task_id": task_id,
                    "status": status,
                    "segments": segments,
                    "mode_prompt": mode_prompt,
                    "ref_names": ref_names,
                    "current_index": current,
                    "segment_status": seg_status,
                    "failed": failed,
                    "created_at": time.time(),
                },
            )

        try:
            async with self._lock:
                if not resume_state:
                    self._reset_tmp_import()
                _persist("running")
                for idx in range(start_idx, total):
                    seg = segments[idx]
                    task = self._tasks.get(task_id)
                    if task is not None:
                        task["progress"] = {
                            "total": total,
                            "done": idx,
                            "current_index": idx + 1,
                            "failed_count": len(failed),
                        }
                    log_head = (
                        f"\n[========== 段 {idx + 1}/{total} ==========]\n"
                        f"[时间] {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"[用户输入]\n{seg}\n"
                    )
                    self._append_import_log(log_head)
                    if mode_prompt:
                        self._append_import_log(f"[附加提示词]\n{mode_prompt}\n")

                    async def _run_segment() -> tuple[dict[str, Any] | None, str | None]:
                        result = await self._run_import_segment(seg, mode_prompt, cfg, ref_names, task)
                        if result.get("ok"):
                            return result, None
                        return None, str(result.get("error", "段处理失败"))

                    result, error = await run_task_item(
                        _run_segment,
                        max_retries,
                        label=f"导入段 {idx + 1}/{total}",
                        logger=self.ctx.logger,
                    )
                    if error is None and result is not None:
                        seg_status[idx] = "done"
                        self._append_import_log(
                            f"[LLM 决定与理由]\n{result.get('reason', '')}\n"
                            f"[操作]\n{json.dumps(result.get('operations', []), ensure_ascii=False, indent=2)}\n"
                            "[结果] 成功\n"
                        )
                    elif on_failure == "skip":
                        seg_status[idx] = "failed"
                        failed.append({"index": idx + 1, "segment": seg, "error": error})
                        self._append_import_log(f"[结果] 失败：{error}\n")
                    else:
                        # on_failure == interrupt：缓存状态并中断整个导入（从该段续跑）
                        current = idx
                        seg_status[idx] = "interrupted"
                        _persist("interrupted")
                        self._mark_task_interrupted(task_id, f"段 {idx + 1} 处理失败：{error}")
                        self.ctx.logger.warning(f"批量导入在段 {idx + 1} 中断（on_failure=interrupt）: {error}")
                        return
                    if task is not None:
                        task["progress"]["failed_count"] = len(failed)
                        task["progress"]["done"] = idx + 1
                    current = idx + 1
                    _persist("running")

            if failed:
                err_lines = ["\n[========== 失败条目汇总 ==========]\n"]
                for f in failed:
                    err_lines.append(
                        f"段 {f['index']}: {f['error']}\n--- 内容 ---\n{f['segment']}\n\n"
                    )
                self._append_import_log("".join(err_lines))

            # 失败条目落盘，保证"等待导入"状态抗重启也能展示错误列表
            try:
                with self._tmp_failed_path.open("w", encoding="utf-8") as f:
                    json.dump(failed, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                self.ctx.logger.warning(f"写入导入失败汇总失败: {exc}")

            self._tmp_finished_path.write_text("done", encoding="utf-8")
            if state_path.exists():
                try:
                    state_path.unlink()
                except OSError:
                    pass
            self._finish_task(
                task_id,
                {
                    "total": total,
                    "failed_count": len(failed),
                    "failed": failed,
                },
            )
        except Exception as exc:
            self.ctx.logger.error(f"批量导入后台任务异常: {exc}", exc_info=True)
            self._fail_task(task_id, exc)
        finally:
            self._evict_tasks()
