"""WebUI 服务器、全部 API 处理器与后台任务中心（mixin）。"""

import asyncio
import json
import time
import uuid
from typing import Any

import numpy as np

from .constants import _DEDUP_SCAN_BLOCK, _WEBUI_SESSION_TTL, _WEBUI_WARNING_HTML, _WEB_DIR
from .embedding_client import load_embedding_profile, save_embedding_profile
from .notebook import Notebook, _split_txt, scramble_id
from .resume import (
    STATE_INTERRUPTED,
    _RESUME_FILE,
    _TXT_IMPORT_STATE_FILE,
    load_json,
)

class WebUIMixin:

    async def _run_web_server(self) -> None:
        """启动嵌入式 aiohttp WebUI 服务器。"""
        try:
            from aiohttp import web
        except ImportError:
            self.ctx.logger.error("aiohttp 未安装，WebUI 无法启动")
            return

        port = int(self.config.web.port)
        password = str(self.config.web.password or "").strip()
        bind = str(self.config.web.bind or "").strip() or "127.0.0.1"

        unsafe = not self._is_loopback_bind(bind) and not password

        app = web.Application(client_max_size=256 * 1024 * 1024)

        if unsafe:
            # 安全警告模式：绑定非回环地址且未设置密码，所有请求一律返回警告页
            self.ctx.logger.error(
                f"WebUI 处于安全警告模式：bind={bind} 且未设置 [web] password。"
                f"所有请求将返回警告页。请将 bind 改为 127.0.0.1 或设置密码后重启插件。"
            )

            @web.middleware
            async def warning_middleware(request: Any, handler: Any) -> Any:
                return web.Response(text=_WEBUI_WARNING_HTML, content_type="text/html", status=403)

            app.middlewares.append(warning_middleware)
        else:
            app.router.add_get("/", self._web_index)
            # 静态资源（多页面的 css/js/html）
            web_dir = _WEB_DIR
            if web_dir.is_dir():
                app.router.add_static("/web/", web_dir)
            app.router.add_post("/api/login", self._web_login)
            app.router.add_post("/api/logout", self._web_logout)
            app.router.add_get("/api/status", self._web_status)
            app.router.add_get("/api/notes", self._web_notes)
            app.router.add_get("/api/search", self._web_search)
            app.router.add_post("/api/add", self._web_add)
            app.router.add_post("/api/modify", self._web_modify)
            app.router.add_post("/api/delete", self._web_delete)
            app.router.add_post("/api/refresh", self._web_refresh)
            app.router.add_post("/api/rebuild", self._web_rebuild)
            app.router.add_get("/api/tasks", self._web_tasks)
            app.router.add_post("/api/task/resume", self._web_task_resume)
            app.router.add_post("/api/task/cancel", self._web_task_cancel)
            app.router.add_post("/api/import/preview", self._web_import_preview)
            app.router.add_post("/api/import/start", self._web_import_start)
            app.router.add_get("/api/import/status", self._web_import_status)
            app.router.add_get("/api/import/tmp_notes", self._web_import_tmp_notes)
            app.router.add_get("/api/import/log", self._web_import_log)
            app.router.add_post("/api/import/resolve", self._web_import_resolve)
            app.router.add_post("/api/import/cancel", self._web_import_cancel)
            app.router.add_get("/api/import/state", self._web_import_state)
            app.router.add_post("/api/notebooks/delete", self._web_delete_notebook)
            app.router.add_post("/api/notebooks/create", self._web_create_notebook)
            app.router.add_get("/api/dedup/scan", self._web_dedup_scan)
            app.router.add_post("/api/dedup/resolve", self._web_dedup_resolve)
            app.router.add_post("/api/dedup/organize_preview", self._web_organize_preview)
            app.router.add_post("/api/organize_db/plan", self._web_organize_db_plan)
            app.router.add_get("/api/organize_db/plan_status", self._web_organize_db_plan_status)
            app.router.add_post("/api/organize_db/apply", self._web_organize_db_apply)
            app.router.add_get("/api/backups", self._web_backups_list)
            app.router.add_post("/api/backups/restore", self._web_backups_restore)
            app.router.add_post("/api/backups/delete", self._web_backups_delete)
            app.router.add_get("/api/embedding_profile", self._web_embedding_profile_get)
            app.router.add_post("/api/embedding_profile", self._web_embedding_profile_save)
            app.router.add_post("/api/export/start", self._web_export_start)
            app.router.add_get("/api/export/download", self._web_export_download)
            app.router.add_get("/api/transfer/state", self._web_transfer_state)
            app.router.add_post("/api/transfer/clear", self._web_transfer_clear)
            app.router.add_post("/api/transfer/cancel", self._web_transfer_cancel)
            app.router.add_post("/api/transfer/resume", self._web_transfer_resume)
            app.router.add_post("/api/import/file", self._web_import_file)
            app.router.add_get("/api/import/file_preview", self._web_import_file_preview)
            app.router.add_post("/api/import/file_commit", self._web_import_file_commit)

        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, bind, port)
        await site.start()
        auth_note = f"，密码保护已启用" if password else ""
        mode_note = "（安全警告模式）" if unsafe else ""
        self.ctx.logger.info(f"WebUI 已启动: http://{bind}:{port}{auth_note}{mode_note}")

        try:
            await asyncio.Event().wait()
        finally:
            # 任务取消（插件重载/卸载）时释放端口，否则旧服务器会占住端口、新配置不生效
            try:
                await self._web_runner.cleanup()
            except Exception:
                pass
            self._web_runner = None

    @staticmethod
    def _is_loopback_bind(bind: str) -> bool:
        """判断绑定地址是否为回环地址。"""
        return bind.strip().lower() in {"127.0.0.1", "localhost", "::1"}

    def _web_check_auth(self, request: Any) -> bool:
        """检查 WebUI 请求的密码认证。

        优先校验 HttpOnly cookie（浏览器登录），兼容 Authorization: Bearer（脚本/API 客户端）。
        """
        password = str(self.config.web.password or "").strip()
        if not password:
            return True
        cookie_token = request.cookies.get("mpj_auth") or ""
        if cookie_token and cookie_token == password:
            return True
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip()
        return bool(token) and token == password

    async def _web_index(self, request: Any) -> Any:
        """返回 WebUI 首页 HTML，始终返回 HTML，认证由 API 端点处理。"""
        from aiohttp import web

        html_path = _WEB_DIR / "index.html"
        if html_path.exists():
            html = html_path.read_text(encoding="utf-8")
        else:
            html = "<html><body><h1>web/index.html 未找到</h1></body></html>"
        return web.Response(text=html, content_type="text/html")

    async def _web_login(self, request: Any) -> Any:
        """校验密码并下发 HttpOnly 登录 cookie。"""
        from aiohttp import web

        if self._web_check_auth(request):
            return web.json_response({"success": True})
        body = await self._web_read_body(request)
        password = str(body.get("password", "") or "").strip()
        expected = str(self.config.web.password or "").strip()
        if not expected:
            return web.json_response({"success": True})
        if password != expected:
            return web.json_response({"error": "密码错误"}, status=401)
        resp = web.json_response({"success": True})
        resp.set_cookie(
            "mpj_auth",
            password,
            max_age=_WEBUI_SESSION_TTL,
            httponly=True,
            samesite="Strict",
            path="/",
        )
        return resp

    async def _web_logout(self, request: Any) -> Any:
        """清除 WebUI 登录 cookie。"""
        from aiohttp import web

        resp = web.json_response({"success": True})
        resp.set_cookie("mpj_auth", "", max_age=0, httponly=True, samesite="Strict", path="/")
        return resp

    async def _web_json_response(self, request: Any, data: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(data)

    async def _web_status(self, request: Any) -> Any:
        notebooks_info = []
        for name in sorted(self._notebooks.keys()):
            nb = self._notebooks[name]
            count = nb.count_notes()
            if not nb.has_source:
                status = "empty"
            elif not nb.has_index:
                status = "no_index"
            elif nb.check_consistency():
                status = "ok"
            else:
                status = "stale"
            notebooks_info.append({"name": name, "count": count, "status": status})
        return await self._web_json_response(request, {"notebooks": notebooks_info})

    async def _web_notes(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        nb_name = request.query.get("notebook", "default")
        page = max(1, int(request.query.get("page", 1)))
        size = max(1, min(100, int(request.query.get("size", 20))))
        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)
        entries = nb.load_notes()
        total = len(entries)
        start = (page - 1) * size
        end = start + size
        page_entries = entries[start:end]
        return web.json_response(
            {
                "notebook": nb_name,
                "total": total,
                "page": page,
                "size": size,
                "pages": (total + size - 1) // size if size > 0 else 1,
                "notes": page_entries,
            }
        )

    async def _web_search(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        query = request.query.get("q", "").strip()
        nb_name = request.query.get("notebook", "default")
        limit = max(1, min(50, int(request.query.get("limit", 10))))
        if not query:
            return web.json_response({"error": "搜索关键词不能为空"}, status=400)

        top_k = limit
        min_score = float(self.config.journal.min_score)

        async with self._lock:
            query_vec = await self._embed_single(query)
            if query_vec is None:
                return web.json_response({"error": "embedding 服务不可用"}, status=503)

            if nb_name == "all":
                results = await self._search_all_notebooks(query, query_vec, top_k, min_score)
            else:
                nb = self._get_notebook(nb_name)
                if nb is None:
                    return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)
                results = await self._search_single_notebook(nb, query, query_vec, top_k, min_score)

        return web.json_response({"query": query, "count": len(results), "results": results})

    async def _web_read_body(self, request: Any) -> dict:
        import json as _json

        raw = await request.read()
        if not raw:
            return {}
        try:
            return _json.loads(raw)
        except Exception:
            return {}

    async def _web_add(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        notes_raw = body.get("notes", [])
        if not isinstance(notes_raw, list) or not notes_raw:
            return web.json_response({"error": "notes 不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        valid_entries: list[dict[str, Any]] = []
        base_ts_ms = int(time.time() * 1000)
        now = time.time()
        for idx, item in enumerate(notes_raw):
            if not isinstance(item, dict):
                continue
            en = str(item.get("en", "") or "").strip()
            zh = str(item.get("zh", "") or "").strip()
            note = str(item.get("note", "") or "").strip()
            if not en or not zh:
                continue
            valid_entries.append(
                {"id": scramble_id(base_ts_ms + idx), "en": en, "zh": zh, "note": note, "ts": now}
            )
        if not valid_entries:
            return web.json_response({"error": "没有有效条目"}, status=400)

        embedding_texts = [self._build_embedding_text(e["en"], e["zh"], e["note"]) for e in valid_entries]
        embeddings = await self._embed_batch(embedding_texts)
        if embeddings is None:
            return web.json_response({"error": "embedding 服务不可用"}, status=503)

        async with self._lock:
            nb.append_entries(valid_entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

        return web.json_response({"success": True, "added": len(valid_entries), "notebook": nb_name})

    async def _web_modify(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        note_id = str(body.get("note_id", "") or "").strip()
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        if not note_id:
            return web.json_response({"error": "note_id 不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        updates = {}
        for key in ("en", "zh", "note"):
            val = body.get(key)
            if val is not None and str(val).strip():
                updates[key] = str(val).strip()

        async with self._lock:
            entries = nb.load_notes()
            target_idx = None
            for i, entry in enumerate(entries):
                if entry.get("id") == note_id:
                    target_idx = i
                    break
            if target_idx is None:
                return web.json_response({"error": "笔记不存在"}, status=404)

            entry = entries[target_idx]
            old_hash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])
            for key in ("en", "zh", "note"):
                if key in updates:
                    entry[key] = updates[key]
            entries[target_idx] = entry
            new_hash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])

            embeddings = nb.load_embeddings()
            if embeddings is not None and old_hash != new_hash and len(embeddings) > target_idx:
                emb_text = self._build_embedding_text(entry["en"], entry["zh"], entry["note"])
                new_vec = await self._embed_single(emb_text)
                if new_vec is not None:
                    emb_f16 = embeddings.astype(np.float16)
                    if emb_f16.shape[1] == len(new_vec):
                        emb_f16[target_idx] = new_vec.astype(np.float16)
                        embeddings = emb_f16

            nb.rewrite_all(entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

        return web.json_response({"success": True})

    async def _web_delete(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        note_id = str(body.get("note_id", "") or "").strip()
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        if not note_id:
            return web.json_response({"error": "note_id 不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        async with self._lock:
            entries = nb.load_notes()
            target_idx = None
            for i, entry in enumerate(entries):
                if entry.get("id") == note_id:
                    target_idx = i
                    break
            if target_idx is None:
                return web.json_response({"error": "笔记不存在"}, status=404)

            del entries[target_idx]
            embeddings = nb.load_embeddings()
            if embeddings is not None and len(embeddings) > target_idx:
                embeddings = np.delete(embeddings, target_idx, axis=0)
            nb.rewrite_all(entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

        return web.json_response({"success": True, "remaining": nb.count_notes()})

    async def _web_refresh(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        async with self._lock:
            self._notebooks = self._discover_notebooks()
        return await self._web_status(request)

    async def _web_rebuild(self, request: Any) -> Any:
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        force_full = bool(body.get("force", False))
        label = "全量重构索引" if force_full else "重建索引"

        task_id = self._start_task("rebuild", label)
        if task_id is None:
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
        handle = asyncio.create_task(self._run_rebuild_task(task_id, force_full))
        if self._tasks.get(task_id) is not None:
            self._tasks[task_id]["handle"] = handle
        return web.json_response({"task_id": task_id})

    async def _run_rebuild_task(self, task_id: str, force_full: bool) -> None:
        """后台执行索引重建，逐笔记本汇报进度。"""
        try:
            async with self._lock:
                self._notebooks = self._discover_notebooks()
                names = [n for n in sorted(self._notebooks.keys()) if self._notebooks[n].has_source]
                total = len(names)
                results: list[dict[str, Any]] = []
                for done, name in enumerate(names, 1):
                    nb = self._notebooks[name]
                    task = self._tasks.get(task_id)
                    if task is not None:
                        task["progress"] = {"total": total, "done": done, "current": name}
                    try:
                        stats = await self._rebuild_notebook(nb, force_full=force_full)
                        results.append({"notebook": name, **stats})
                    except Exception as exc:
                        self.ctx.logger.error(f"笔记本 {name} 重建失败: {exc}", exc_info=True)
                        results.append({"notebook": name, "error": str(exc)})
                self._finish_task(task_id, {"results": results})
        except Exception as exc:
            self.ctx.logger.error(f"索引重建后台任务异常: {exc}", exc_info=True)
            self._fail_task(task_id, exc)
        finally:
            self._evict_tasks()

    async def _web_dedup_scan(self, request: Any) -> Any:
        """扫描指定笔记本中的语义重复组。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        nb_name = str(request.query.get("notebook", "default") or "default").strip()
        threshold = 0.85
        try:
            threshold = float(request.query.get("threshold", 0.85))
        except (ValueError, TypeError):
            pass
        threshold = max(0.5, min(0.99, threshold))

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        async with self._lock:
            if not nb.check_consistency():
                return web.json_response({"error": f"笔记本 '{nb_name}' 索引失效，请先 /mpj rebuild"}, status=400)

            entries = nb.load_notes()
            embeddings = nb.load_embeddings()

        if not entries or embeddings is None or len(embeddings) != len(entries):
            return web.json_response({"error": "无数据或索引不一致"}, status=400)

        groups = self._scan_duplicates(entries, embeddings, threshold)
        return web.json_response({"notebook": nb_name, "threshold": threshold, "groups": groups, "total": len(groups)})

    def _scan_duplicates(
        self,
        entries: list[dict[str, Any]], embeddings: np.ndarray, threshold: float
    ) -> list[dict[str, Any]]:
        """按余弦相似度对条目做贪心聚类，返回重复组列表。

        相似度分块计算（每块 [advanced].dedup_scan_block 行，B×N 用完即弃），
        内存峰值从 N×N 降到 B×N，大笔记本扫描不会一次性吃满内存；
        只读右上三角 j>i（不含自身），结果与一次性全矩阵计算完全一致。
        """
        block = int(getattr(self.config.advanced, "dedup_scan_block", 0) or _DEDUP_SCAN_BLOCK)
        block = max(16, min(4096, block))

        emb_f32 = embeddings.astype(np.float32)
        norms = np.linalg.norm(emb_f32, axis=1, keepdims=True)
        normalized = emb_f32 / np.where(norms > 1e-8, norms, 1.0)
        n = normalized.shape[0]

        # 贪心聚类：相似度 >= threshold 的条目归入同组（逐块取行，不驻留全矩阵）
        visited: set[int] = set()
        groups: list[dict[str, Any]] = []
        for i0 in range(0, n, block):
            i1 = min(i0 + block, n)
            block_matrix = normalized[i0:i1] @ normalized.T
            for i_local in range(i1 - i0):
                i = i0 + i_local
                if i in visited:
                    continue
                row = block_matrix[i_local]
                group_indices = [i]
                for j in range(i + 1, n):
                    if j in visited:
                        continue
                    if row[j] >= threshold:
                        group_indices.append(j)
                        visited.add(j)
                if len(group_indices) > 1:
                    visited.add(i)
                    group_entries = []
                    for idx in group_indices:
                        e = entries[idx]
                        group_entries.append(
                            {
                                "id": e["id"],
                                "en": e["en"],
                                "zh": e["zh"],
                                "note": e["note"],
                            }
                        )
                    groups.append({"entries": group_entries})

        return groups

    async def _web_organize_preview(self, request: Any) -> Any:
        """调用 LLM 整理一组重复笔记，返回预览结果（不修改数据）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        entries_raw = body.get("entries", [])
        requirement = str(body.get("requirement", "") or "").strip()

        if not isinstance(entries_raw, list) or not entries_raw:
            return web.json_response({"error": "entries 不能为空"}, status=400)
        entries: list[dict[str, Any]] = [
            e for e in entries_raw if isinstance(e, dict)
        ]
        if not entries:
            return web.json_response({"error": "没有有效的条目"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        result = await self._organize_with_llm(entries, requirement)
        if result.get("_error"):
            return web.json_response({"error": result.get("message") or "LLM 整理失败"}, status=502)

        return web.json_response({"reason": result["reason"], "entries": result["entries"]})

    async def _web_dedup_resolve(self, request: Any) -> Any:
        """执行去重处理：LLM 整理（organize）模式，处理后重建索引并重扫。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        threshold = 0.85
        try:
            threshold = float(body.get("threshold", 0.85))
        except (ValueError, TypeError):
            pass
        threshold = max(0.5, min(0.99, threshold))

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        async with self._lock:
            if not nb.check_consistency():
                return web.json_response({"error": f"笔记本 '{nb_name}' 索引失效"}, status=400)

            entries = nb.load_notes()
            id_set = {e["id"] for e in entries}

            # LLM 整理：删除整组原条目，写入整理后的新条目
            delete_ids_raw = body.get("delete_ids", [])
            if not isinstance(delete_ids_raw, list) or not delete_ids_raw:
                return web.json_response({"error": "delete_ids 不能为空"}, status=400)
            delete_ids = {str(d or "").strip() for d in delete_ids_raw if str(d or "").strip()}
            if not delete_ids or not delete_ids <= id_set:
                return web.json_response({"error": "delete_ids 中存在不存在的条目"}, status=400)

            new_entries_raw = body.get("new_entries", [])
            if not isinstance(new_entries_raw, list) or not new_entries_raw:
                return web.json_response({"error": "new_entries 不能为空"}, status=400)
            new_entries: list[dict[str, Any]] = []
            for item in new_entries_raw:
                if not isinstance(item, dict):
                    continue
                en = str(item.get("en", "") or "").strip()
                zh = str(item.get("zh", "") or "").strip()
                note = str(item.get("note", "") or "").strip()
                if not en or not zh:
                    continue
                new_entries.append({"en": en, "zh": zh, "note": note})
            if not new_entries:
                return web.json_response({"error": "new_entries 没有有效条目（en/zh 必填）"}, status=400)

            base_ts_ms = int(time.time() * 1000)
            now = time.time()
            fresh_entries = [
                {"id": scramble_id(base_ts_ms + idx), "en": e["en"], "zh": e["zh"], "note": e["note"], "ts": now}
                for idx, e in enumerate(new_entries)
            ]

            kept_entries = [e for e in entries if e.get("id") not in delete_ids]
            final_entries = kept_entries + fresh_entries

            # 只写源文件，不动 cache/embeddings；随后增量重建索引（会自动补嵌变更条目）
            json_str = "\n".join(json.dumps(e, ensure_ascii=False) for e in final_entries)
            if json_str:
                json_str += "\n"
            nb.notes_path.parent.mkdir(parents=True, exist_ok=True)
            nb.notes_path.write_text(json_str, encoding="utf-8")

            try:
                rebuild_stats = await self._rebuild_notebook(nb)
            except Exception as exc:
                self.ctx.logger.error(f"去重处理后索引重建失败: {exc}", exc_info=True)
                return web.json_response(
                    {"error": "处理已写入源文件，但索引重建失败，请执行 /mpj rebuild"}, status=500
                )
            self._create_backup(nb)

            groups = self._scan_duplicates(nb.load_notes(), nb.load_embeddings(), threshold)

        return web.json_response(
            {
                "success": True,
                "remaining": nb.count_notes(),
                "rebuild": rebuild_stats,
                "threshold": threshold,
                "groups": groups,
            }
        )

    async def _web_organize_db_plan(self, request: Any) -> Any:
        """启动 LLM 操作笔记本的后台任务，立即返回 task_id（进度由 plan_status 轮询）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        requirement = str(body.get("requirement", "") or "").strip()
        session_id = str(body.get("session_id", "") or "").strip()

        # 首轮（无 session_id）必须提供操作要求，防止空要求空跑 LLM
        if not session_id and not requirement:
            return web.json_response({"error": "操作要求不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        async with self._lock:
            if not nb.check_consistency():
                return web.json_response({"error": f"笔记本 '{nb_name}' 索引失效，请先 /mpj rebuild"}, status=400)

        task_id = uuid.uuid4().hex
        asyncio.create_task(self._organize_db_task(task_id, nb_name, requirement, session_id))
        return web.json_response({"task_id": task_id})

    async def _web_organize_db_plan_status(self, request: Any) -> Any:
        """查询操作数据库后台任务的进度与结果。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        task_id = str(request.query.get("task_id", "") or "").strip()
        task = self._organize_tasks.get(task_id)
        if task is None:
            return web.json_response({"error": "任务不存在或已过期"}, status=404)

        if task["status"] == "running":
            return web.json_response({"status": "running", "searches": task["searches"]})
        if task["status"] == "done":
            return web.json_response({"status": "done", "plan": task["plan"]})
        return web.json_response({"status": "error", "error": task.get("error", "")})

    async def _organize_db_task(
        self, task_id: str, nb_name: str, requirement: str, session_id: str
    ) -> None:
        """后台执行操作数据库方案生成，更新进度。"""
        progress: dict[str, Any] = {"status": "running", "searches": [], "created_at": time.time()}
        self._organize_tasks[task_id] = progress
        try:
            async with self._lock:
                result = await self._organize_db_plan(nb_name, requirement, session_id, progress=progress)

            if result is None:
                progress["status"] = "error"
                progress["error"] = "LLM 操作失败，请重试或检查 LLM 配置"
            elif result.get("_error") == "expired":
                progress["status"] = "error"
                progress["error"] = "会话已过期，请重新生成操作方案"
            elif result.get("_error") == "empty_requirement":
                progress["status"] = "error"
                progress["error"] = "补充要求不能为空"
            elif result.get("_error") == "llm":
                progress["status"] = "error"
                progress["error"] = result.get("message") or "LLM 操作失败"
            else:
                # 为 update/delete 附带当前值，供前端展示 旧值→新值
                nb = self._get_notebook(nb_name)
                entries = nb.load_notes() if nb is not None else []
                by_id = {e["id"]: e for e in entries}
                enriched_ops: list[dict[str, Any]] = []
                for op in result["operations"]:
                    enriched = dict(op)
                    if enriched.get("type") in ("update", "delete"):
                        current = by_id.get(str(op.get("id", "") or "").strip())
                        if current is not None:
                            enriched["_old"] = {"en": current["en"], "zh": current["zh"], "note": current["note"]}
                    enriched_ops.append(enriched)
                progress["status"] = "done"
                progress["plan"] = {
                    "session_id": result["session_id"],
                    "reason": result["reason"],
                    "operations": enriched_ops,
                }
        except Exception as exc:
            self.ctx.logger.error(f"操作数据库后台任务异常: {exc}", exc_info=True)
            progress["status"] = "error"
            progress["error"] = str(exc)
        finally:
            self._evict_organize_tasks()

    def _evict_organize_tasks(self) -> None:
        """任务结果保留 TTL 并限制数量，防止内存膨胀。"""
        ttl = 300.0
        limit = 50
        now = time.time()
        stale = [tid for tid, t in self._organize_tasks.items() if now - t["created_at"] > ttl]
        for tid in stale:
            self._organize_tasks.pop(tid, None)
        if len(self._organize_tasks) > limit:
            oldest = sorted(self._organize_tasks.items(), key=lambda kv: kv[1]["created_at"])
            for tid, _ in oldest[: len(self._organize_tasks) - limit]:
                self._organize_tasks.pop(tid, None)

    def _task_busy(self) -> bool:
        """是否存在进行中的后台任务。"""
        return any(t.get("status") == "running" for t in self._tasks.values())

    def _start_task(self, task_type: str, label: str) -> str | None:
        """登记一个后台任务，返回 task_id；已有任务进行中/存在待处置的中断任务时返回 None。"""
        if self._task_busy():
            return None
        # 存在待处置（再次尝试/取消）的中断任务时不接受新任务，避免缓存互相覆盖
        if any(t.get("status") == STATE_INTERRUPTED for t in self._tasks.values()):
            return None
        task_id = uuid.uuid4().hex
        self._tasks[task_id] = {
            "id": task_id,
            "type": task_type,
            "label": label,
            "status": "running",
            "progress": {},
            "created_at": time.time(),
            "finished_at": None,
            "result": None,
            "error": None,
            "handle": None,
        }
        return task_id

    def _cancel_running_task(self, task_type: str | None = None) -> bool:
        """取消当前进行中的后台任务，可按 task_type 过滤；返回是否取消成功。

        task_type 为 None 时取消任意正在运行的任务（保留旧语义，供传输状态机使用）；
        指定时只取消匹配类型的任务，避免 txt 导入页误取消重建/导出等无关任务。
        """
        cancelled = False
        for tid, task in list(self._tasks.items()):
            if task.get("status") == "running":
                if task_type is not None and task.get("type") != task_type:
                    continue
                handle = task.get("handle")
                if handle is not None and not handle.done():
                    handle.cancel()
                task["status"] = "error"
                task["error"] = "任务已取消"
                task["finished_at"] = time.time()
                cancelled = True
        return cancelled

    def _finish_task(self, task_id: str, result: Any) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["status"] = "done"
        task["result"] = result
        task["finished_at"] = time.time()

    def _fail_task(self, task_id: str, error: Any) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["status"] = "error"
        task["error"] = str(error)
        task["finished_at"] = time.time()

    def _mark_task_interrupted(self, task_id: str, error: Any) -> None:
        """把任务标记为『中断』（断点续跑前状态，不随 TTL 淘汰）。"""
        task = self._tasks.get(task_id)
        if task is None:
            return
        task["status"] = STATE_INTERRUPTED
        task["error"] = str(error)
        task["finished_at"] = time.time()

    def _clear_task(self, task_id: str) -> None:
        """移除任务（取消 handle 并删除记录）。"""
        task = self._tasks.pop(task_id, None)
        if task is None:
            return
        handle = task.get("handle")
        if handle is not None and not handle.done():
            handle.cancel()

    def _evict_tasks(self) -> None:
        """通用任务结果保留 TTL 并限制数量。

        进行中（running）与已中断（interrupted，等待再次尝试/取消）的任务**不淘汰**，
        只淘汰已完成的 done/error 任务，保证断点续跑可用。
        """
        ttl = 300.0
        limit = 5
        now = time.time()
        stale = [
            tid
            for tid, t in self._tasks.items()
            if t.get("status") in ("done", "error") and now - t.get("created_at", 0) > ttl
        ]
        for tid in stale:
            self._tasks.pop(tid, None)
        # 只对"可淘汰"任务计数做上限约束，不计算 running/interrupted
        evictable = {
            tid: t
            for tid, t in self._tasks.items()
            if t.get("status") in ("done", "error")
        }
        if len(evictable) > limit:
            oldest = sorted(evictable.items(), key=lambda kv: kv[1].get("created_at", 0))
            for tid, _ in oldest[: len(evictable) - limit]:
                self._tasks.pop(tid, None)

    async def _web_tasks(self, request: Any) -> Any:
        """返回所有后台任务（进行中的在前），供"当前活跃任务"面板轮询。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        self._evict_tasks()
        tasks = sorted(self._tasks.values(), key=lambda t: (t["status"] != "running", t["created_at"]))
        # handle 是内部 asyncio.Task 对象，不可 JSON 序列化，对外剔除
        clean = [{k: v for k, v in t.items() if k != "handle"} for t in tasks]
        return web.json_response({"tasks": clean})

    def _restore_interrupted_tasks(self) -> None:
        """插件重载后从磁盘恢复被中断的任务（当前支持 txt 批量导入），供『再次尝试/取消』。"""
        state_path = self._tmp_import_dir / _TXT_IMPORT_STATE_FILE
        state = load_json(state_path)
        if not state or not state.get("task_id"):
            return
        if self._tmp_finished_path.exists():
            return  # 已完成，忽略陈旧状态
        if state.get("status") not in (STATE_INTERRUPTED, "running"):
            return
        task_id = str(state.get("task_id", "") or "")
        if not task_id or task_id in self._tasks:
            return
        segments = state.get("segments")
        if not isinstance(segments, list) or not segments:
            return
        current_index = int(state.get("current_index", 0))
        self._tasks[task_id] = {
            "id": task_id,
            "type": "import",
            "label": "txt 批量导入",
            "status": STATE_INTERRUPTED,
            "progress": {
                "total": len(segments),
                "done": current_index,
                "current_index": current_index + 1,
                "failed_count": len(state.get("failed") or []),
            },
            "created_at": time.time(),
            "finished_at": time.time(),
            "result": None,
            "error": f"任务已中断（已处理 {current_index}/{len(segments)} 段）",
            "handle": None,
            "resume": {"kind": "txt_import", "state": state},
        }
        self.ctx.logger.info(f"已从磁盘恢复中断的 txt 批量导入任务 {task_id}")

    async def _web_task_resume(self, request: Any) -> Any:
        """再次尝试（续跑）被中断的后台任务（当前支持 txt 批量导入）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        task_id = str(body.get("task_id", "") or "").strip()
        task = self._tasks.get(task_id)
        if task is None:
            return web.json_response({"error": "任务不存在或已过期"}, status=404)
        if task.get("status") != STATE_INTERRUPTED:
            return web.json_response({"error": "任务未处于中断状态"}, status=400)
        if self._task_busy():
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)

        resume = task.get("resume") or {}
        kind = resume.get("kind")
        if kind == "import_commit" or kind == "export_mpj":
            return web.json_response(
                {"error": "请在『传输状态』面板选择再次尝试（此任务不支持在任务栏续跑）"}, status=400
            )
        if kind != "txt_import":
            return web.json_response({"error": "不支持续跑该类型任务"}, status=400)
        state = resume.get("state") or {}
        segments = state.get("segments")
        if not isinstance(segments, list) or not segments:
            return web.json_response({"error": "续跑数据缺失，请取消后重新导入"}, status=400)

        task["status"] = "running"
        task["error"] = None
        task["finished_at"] = None
        handle = asyncio.create_task(
            self._run_import_task(
                task_id,
                segments,
                str(state.get("mode_prompt", "") or ""),
                [str(n or "").strip() for n in (state.get("ref_names") or []) if str(n or "").strip()],
                resume_state=state,
            )
        )
        task["handle"] = handle
        return web.json_response({"success": True, "task_id": task_id})

    async def _web_task_cancel(self, request: Any) -> Any:
        """取消被中断的后台任务（再次尝试之前放弃），并清理对应缓存。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        task_id = str(body.get("task_id", "") or "").strip()
        task = self._tasks.get(task_id)
        if task is None:
            return web.json_response({"error": "任务不存在或已过期"}, status=404)
        resume = task.get("resume") or {}
        kind = resume.get("kind")
        task_type = task.get("type")
        self._clear_task(task_id)
        if kind == "txt_import" or task_type == "import":
            async with self._lock:
                self._reset_tmp_import()
        elif kind in ("import_commit", "export_mpj") or task_type in ("import_file_commit", "export"):
            self._reset_io()
        return web.json_response({"success": True})

    async def _web_import_preview(self, request: Any) -> Any:
        """上传 txt → 切分 → 返回段落列表（不落盘）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        text = str(body.get("text", "") or "")
        if not text.strip():
            return web.json_response({"error": "文本不能为空"}, status=400)
        segments = _split_txt(text)
        if not segments:
            return web.json_response({"error": "没有可导入的段落"}, status=400)
        return web.json_response({"segments": segments, "count": len(segments)})

    async def _web_import_start(self, request: Any) -> Any:
        """启动批量导入后台任务，立即返回 task_id。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        text = str(body.get("text", "") or "")
        mode_prompt = str(body.get("mode_prompt", "") or "").strip()
        ref_raw = body.get("ref_notebooks", [])
        if not isinstance(ref_raw, list):
            ref_raw = []
        ref_names = [str(n or "").strip() for n in ref_raw if str(n or "").strip()]
        max_retries, on_failure = self._normalize_retry_params(
            body.get("max_retries"), body.get("on_failure")
        )

        segments = _split_txt(text)
        if not segments:
            return web.json_response({"error": "没有可导入的段落"}, status=400)

        # 双重校验：预设或自定义都必须有附加提示词
        if not mode_prompt:
            return web.json_response({"error": "附加提示词不能为空"}, status=400)

        # 校验引用笔记本存在
        for name in ref_names:
            if self._get_notebook(name) is None:
                return web.json_response({"error": f"引用笔记本 '{name}' 不存在"}, status=400)

        # LLM 直连配置完整
        llm_cfg = self.config.llm
        if not (llm_cfg.base_url and llm_cfg.api_key and llm_cfg.model):
            return web.json_response(
                {"error": "LLM 直连配置不完整，请填写 [llm] 的 base_url / api_key / model"}, status=400
            )

        task_id = self._start_task("import", "txt 批量导入")
        if task_id is None:
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
        handle = asyncio.create_task(
            self._run_import_task(
                task_id, segments, mode_prompt, ref_names, max_retries=max_retries, on_failure=on_failure
            )
        )
        if self._tasks.get(task_id) is not None:
            self._tasks[task_id]["handle"] = handle
        return web.json_response({"task_id": task_id, "count": len(segments)})

    async def _web_import_status(self, request: Any) -> Any:
        """查询导入任务状态（含失败汇总 / 中断续跑）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        task_id = str(request.query.get("task_id", "") or "").strip()
        task = self._tasks.get(task_id)
        if task is None:
            return web.json_response({"error": "任务不存在或已过期"}, status=404)
        if task["status"] == "running":
            return web.json_response({"status": "running", "progress": task["progress"]})
        if task["status"] == "done":
            return web.json_response({"status": "done", "result": task["result"]})
        if task["status"] == STATE_INTERRUPTED:
            return web.json_response(
                {"status": "interrupted", "error": task.get("error", "任务已中断"), "progress": task.get("progress")}
            )
        return web.json_response({"status": "error", "error": task.get("error", "")})

    async def _web_import_tmp_notes(self, request: Any) -> Any:
        """查看临时笔记本全部条目。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        entries = self._tmp_nb.load_notes()
        return web.json_response({"notebook": "tmp", "notes": entries, "count": len(entries)})

    async def _web_import_log(self, request: Any) -> Any:
        """下载导入日志。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not self._tmp_log_path.exists():
            return web.json_response({"error": "日志不存在"}, status=404)
        text = self._tmp_log_path.read_text(encoding="utf-8", errors="replace")
        return web.Response(
            text=text,
            content_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="import.log"'},
        )

    async def _web_import_resolve(self, request: Any) -> Any:
        """处置临时笔记本：merge 合并入已有 / create 新建 / discard 丢弃。"""
        import shutil

        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        action = str(body.get("action", "") or "").strip()
        if action not in ("merge", "create", "discard"):
            return web.json_response({"error": "action 必须是 merge/create/discard"}, status=400)

        async with self._lock:
            entries = self._tmp_nb.load_notes()
            if action == "discard":
                # 丢弃：清空缓存目录（含 .finished），回到未开始
                self._reset_tmp_import()
                return web.json_response({"success": True, "action": "discard"})

            if not entries:
                return web.json_response({"error": "临时笔记本为空，无需处置"}, status=400)

            if action == "merge":
                target = str(body.get("target_notebook", "") or "").strip() or "default"
                nb = self._get_notebook(target)
                if nb is None:
                    return web.json_response({"error": f"目标笔记本 '{target}' 不存在"}, status=404)
                if not nb.check_consistency():
                    return web.json_response(
                        {"error": f"笔记本 '{target}' 索引失效，请先 /mpj rebuild"}, status=400
                    )
                # 复用 tmp 已有向量（embedding 文本相同，维度一致），直接追加
                embeddings = self._tmp_nb.load_embeddings()
                if embeddings is None or len(embeddings) != len(entries):
                    return web.json_response({"error": "临时笔记本向量不完整，无法合并"}, status=400)
                nb.append_entries(entries, embeddings)
                nb.update_md5()
                self._create_backup(nb)
                # 标记导入完成（抗刷新/抗重启）
                self._tmp_finished_path.write_text("", encoding="utf-8")
                return web.json_response(
                    {"success": True, "action": "merge", "target": target, "count": len(entries)}
                )

            # create：新建笔记本，复制 tmp 四个文件为 {new_name}
            new_name = str(body.get("new_name", "") or "").strip()
            if not new_name:
                return web.json_response({"error": "新建笔记本必须填写名称"}, status=400)
            if new_name == "default" or self._get_notebook(new_name) is not None:
                return web.json_response({"error": f"笔记本 '{new_name}' 已存在"}, status=400)
            import re as _re

            if not _re.match(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$", new_name):
                return web.json_response(
                    {"error": "笔记本名称只能包含中文/字母/数字/下划线/连字符"}, status=400
                )

            base = self._data_dir / "imports"
            base.mkdir(parents=True, exist_ok=True)
            new_nb = Notebook(new_name, self._data_dir)
            for suffix in (".jsonl", ".cache.jsonl", ".embeddings.npy", ".index.meta"):
                src = self._tmp_import_dir / f"tmp{suffix}"
                dst = new_nb._dir / f"{new_name}{suffix}"
                if src.exists():
                    shutil.copyfile(src, dst)
            self._notebooks = self._discover_notebooks()
            # 标记导入完成（抗刷新/抗重启）
            self._tmp_finished_path.write_text("", encoding="utf-8")
            return web.json_response(
                {"success": True, "action": "create", "name": new_name, "count": len(entries)}
            )

    async def _web_import_cancel(self, request: Any) -> Any:
        """取消进行中或被中断的导入任务并清空缓存目录。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        cancelled = self._cancel_running_task("import")
        # 同时清理被中断（等待再次尝试/取消）的导入任务
        for tid, t in list(self._tasks.items()):
            if t.get("status") == STATE_INTERRUPTED and (t.get("type") == "import" or (t.get("resume") or {}).get("kind") == "txt_import"):
                self._clear_task(tid)
                cancelled = True
        async with self._lock:
            self._reset_tmp_import()
        return web.json_response({"success": True, "cancelled": cancelled})

    async def _web_import_state(self, request: Any) -> Any:
        """返回批量导入当前状态（抗刷新/抗重启）：none / building / ready / done。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        # 构建中：仅认 txt 批量导入任务（type=="import"），避免把重建/导出等
        # 其他任务误报为"笔记本构建中"并让"取消导入"误杀它们
        running = [t for t in self._tasks.values() if t.get("status") == "running" and t.get("type") == "import"]
        if running:
            t = running[0]
            progress = dict(t.get("progress", {}))
            return web.json_response(
                {
                    "state": "building",
                    "progress": progress,
                    "failed_count": progress.get("failed_count", 0),
                }
            )

        # 中断：等待再次尝试或取消（断点续跑，可跨插件重载恢复）
        interrupted = [
            t
            for t in self._tasks.values()
            if t.get("status") == STATE_INTERRUPTED and t.get("type") == "import"
        ]
        if interrupted:
            t = interrupted[0]
            progress = dict(t.get("progress", {}))
            return web.json_response(
                {
                    "state": "interrupted",
                    "task_id": t.get("id", ""),
                    "error": t.get("error", "任务已中断"),
                    "progress": progress,
                }
            )

        # 已完成：存在 .finished 标记
        if self._tmp_finished_path.exists():
            return web.json_response({"state": "done"})

        # 等待导入：tmp 有内容
        entries = self._tmp_nb.load_notes()
        if entries:
            failed: list[dict[str, Any]] = []
            if self._tmp_failed_path.exists():
                try:
                    failed = json.loads(self._tmp_failed_path.read_text(encoding="utf-8")) or []
                except Exception:
                    failed = []
            return web.json_response(
                {
                    "state": "ready",
                    "total": len(entries),
                    "failed_count": len(failed),
                    "failed": failed,
                }
            )

        # 未开始
        return web.json_response({"state": "none"})

    async def _web_delete_notebook(self, request: Any) -> Any:
        """删除一个已有的笔记本（default 与临时笔记本 tmp 不可删除）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        name = str(body.get("name", "") or "").strip()
        if not name:
            return web.json_response({"error": "笔记本名称不能为空"}, status=400)
        if name == "default":
            return web.json_response({"error": "default 笔记本不可删除"}, status=400)
        if name == "tmp":
            return web.json_response({"error": "临时笔记本不可在此删除"}, status=400)

        nb = self._get_notebook(name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{name}' 不存在"}, status=404)

        async with self._lock:
            removed = 0
            for path in (nb.notes_path, nb.cache_path, nb.embeddings_path, nb.meta_path):
                if path.exists():
                    try:
                        path.unlink()
                        removed += 1
                    except Exception as exc:
                        self.ctx.logger.error(f"删除文件失败 {path}: {exc}")
            self._notebooks = self._discover_notebooks()

        self.ctx.logger.info(f"已删除笔记本: {name}（删除 {removed} 个文件）")
        return web.json_response({"success": True, "name": name, "removed_files": removed})

    async def _web_create_notebook(self, request: Any) -> Any:
        """新建一个空白笔记本（自动建好空索引）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        name = str(body.get("name", "") or "").strip()

        ok, result = await self._create_blank_notebook(name)
        if not ok:
            return web.json_response({"error": result}, status=400)

        self.ctx.logger.info(f"已创建空白笔记本: {result}")
        return web.json_response({"success": True, "name": result})

    async def _web_backups_list(self, request: Any) -> Any:
        """列出笔记本的备份。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        nb_name = str(request.query.get("notebook", "default") or "default").strip()
        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)
        backups = self._list_backups(nb)
        return web.json_response({"notebook": nb_name, "backups": backups, "total": len(backups)})

    async def _web_backups_restore(self, request: Any) -> Any:
        """恢复笔记本到指定备份。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        timestamp = str(body.get("timestamp", "") or "").strip()
        if not timestamp:
            return web.json_response({"error": "timestamp 不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        async with self._lock:
            ok, msg = await self._restore_backup(nb, timestamp)
        if not ok:
            return web.json_response({"error": msg}, status=400)
        return web.json_response({"success": True, "message": msg})

    async def _web_backups_delete(self, request: Any) -> Any:
        """删除一个备份。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        timestamp = str(body.get("timestamp", "") or "").strip()
        if not timestamp:
            return web.json_response({"error": "timestamp 不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        ok, msg = self._delete_backup(nb, timestamp)
        if not ok:
            return web.json_response({"error": msg}, status=400)
        return web.json_response({"success": True, "message": msg})

    async def _web_embedding_profile_get(self, request: Any) -> Any:
        """读取已保存的第三方 embedding 配置。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        profile = load_embedding_profile(self._data_dir)
        # 不回传 api_key 明文，前端留空表示沿用已保存值
        return web.json_response({"profile": {k: v for k, v in profile.items() if k != "api_key"}})

    async def _web_embedding_profile_save(self, request: Any) -> Any:
        """保存第三方 embedding 配置。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        profile = load_embedding_profile(self._data_dir)
        base_url = str(body.get("base_url", "") or "").strip()
        api_key = str(body.get("api_key", "") or "").strip()
        model = str(body.get("model", "") or "").strip()
        # api_key 留空则沿用已保存值
        if base_url:
            profile["base_url"] = base_url
        if api_key:
            profile["api_key"] = api_key
        if model:
            profile["model"] = model
        try:
            timeout = int(body.get("timeout", profile.get("timeout", 60)) or 60)
            profile["timeout"] = max(5, min(600, timeout))
        except (TypeError, ValueError):
            pass
        try:
            concurrent = int(body.get("concurrent", profile.get("concurrent", 4)) or 4)
            profile["concurrent"] = max(1, min(16, concurrent))
        except (TypeError, ValueError):
            pass
        dim = body.get("dim")
        if dim is not None:
            try:
                dim = int(dim) or 0
            except (TypeError, ValueError):
                dim = 0
            profile["dim"] = max(0, dim)
        if not profile.get("base_url") or not profile.get("api_key") or not profile.get("model"):
            return web.json_response({"error": "base_url / api_key / model 均不能为空"}, status=400)
        save_embedding_profile(self._data_dir, profile)
        self.ctx.logger.info("已保存第三方 embedding 配置")
        return web.json_response({"success": True})

    async def _web_export_start(self, request: Any) -> Any:
        """启动导出后台任务（jsonl / mpj 直接 / 重新生成索引导出）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        fmt = str(body.get("format", "") or "").strip() or "jsonl"
        mode = str(body.get("mode", "") or "").strip() or "direct"
        filename = str(body.get("filename", "") or "").strip()
        max_retries, on_failure = self._normalize_retry_params(
            body.get("max_retries"), body.get("on_failure")
        )
        if "/" in filename or "\\" in filename or not filename:
            filename = f"{nb_name}.{fmt}"

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        task_id = self._start_task("export", "笔记本导出")
        if task_id is None:
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
        self._reset_io()
        self._set_io("export", "building")
        handle = asyncio.create_task(
            self._run_export_task(
                task_id, nb_name, fmt, mode, filename, max_retries=max_retries, on_failure=on_failure
            )
        )
        if self._tasks.get(task_id) is not None:
            self._tasks[task_id]["handle"] = handle
        return web.json_response({"task_id": task_id, "kind": "export", "state": "building"})

    async def _web_export_download(self, request: Any) -> Any:
        """下载已完成的导出产物。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self._get_io_kind() != "export" or self._get_io_state() != "done":
            return web.json_response({"error": "当前没有可下载的导出"}, status=400)
        result = self._read_io_result()
        filename = str(result.get("filename", "") or "").strip()
        path = self._artifact_path(filename)
        if not path.exists():
            return web.json_response({"error": "导出产物不存在"}, status=404)
        ctype = result.get("ctype") or ("application/zip" if filename.endswith(".mpj") else "text/plain")
        return web.Response(
            body=path.read_bytes(),
            content_type=ctype,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _effective_io_state(self) -> str:
        """传输状态机的"对外有效状态"。

        正常情况下返回 file_io/state；但若存储状态是 importing/building 且存在
        resume.json、又没有正在运行的任务，说明是进程被强杀/断电后的残留 →
        对外呈现为 interrupted（续跑上下文在磁盘上可恢复），前端据此提供
        「再次尝试/取消」。
        """
        state = self._get_io_state()
        if state in ("importing", "building"):
            resume = load_json(self._io_dir() / _RESUME_FILE)
            if resume and not self._task_busy():
                return STATE_INTERRUPTED
        return state

    async def _web_transfer_state(self, request: Any) -> Any:
        """查询统一传输状态（导入/导出，抗刷新/抗重启）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        kind = self._get_io_kind()
        state = self._effective_io_state()
        resp: dict[str, Any] = {"kind": kind, "state": state}
        resp["progress"] = self._read_io_progress() or None
        if state == "ready":
            meta, _entries = self._read_io_preview()
            resp["preview"] = meta
        elif state in ("done", "error"):
            resp["result"] = self._read_io_result()
        elif state == STATE_INTERRUPTED:
            # 断点续跑：回传续跑上下文与已完成的 embedding 进度
            resume = load_json(self._io_dir() / _RESUME_FILE)
            resp["resume"] = resume
            _done, _vecs = self._load_io_resume_emb()
            resp["resume_done"] = len(_done)
        return web.json_response(resp)

    async def _web_transfer_clear(self, request: Any) -> Any:
        """清空当前导入/导出状态（清除预览，重新开始）。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        self._reset_io()
        return web.json_response({"success": True, "state": "none"})

    async def _web_transfer_cancel(self, request: Any) -> Any:
        """取消进行中或被中断的导入/导出后台任务并清空状态。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        cancelled = self._cancel_running_task()
        # 清理被中断的传输任务（type import_file_commit / export）
        for tid, t in list(self._tasks.items()):
            if t.get("status") == STATE_INTERRUPTED and (t.get("resume") or {}).get("kind") in (
                "import_commit",
                "export_mpj",
            ):
                self._clear_task(tid)
                cancelled = True
        self._reset_io()
        return web.json_response({"success": True, "cancelled": cancelled, "state": "none"})

    async def _web_transfer_resume(self, request: Any) -> Any:
        """再次尝试（续跑）被中断的笔记本导入/导出任务。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self._effective_io_state() != STATE_INTERRUPTED:
            return web.json_response({"error": "当前没有可续跑的传输任务"}, status=400)
        if self._task_busy():
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)

        resume = load_json(self._io_dir() / _RESUME_FILE)
        kind = resume.get("kind")
        # 清理残留的中断任务记录
        for tid, t in list(self._tasks.items()):
            if t.get("status") == STATE_INTERRUPTED and (t.get("resume") or {}).get("kind") == kind:
                self._clear_task(tid)

        if kind == "import_commit":
            task_id = self._start_task("import_file_commit", "笔记本导入（续跑）")
            if task_id is None:
                return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
            self._set_io("import", "importing")
            handle = asyncio.create_task(
                self._run_file_commit_task(
                    task_id,
                    str(resume.get("target_name", "") or ""),
                    str(resume.get("mode", "") or ""),
                    str(resume.get("merge_target", "") or ""),
                    resume_ctx=resume,
                )
            )
            if self._tasks.get(task_id) is not None:
                self._tasks[task_id]["handle"] = handle
            return web.json_response({"success": True, "task_id": task_id, "kind": "import", "state": "importing"})

        if kind == "export_mpj":
            task_id = self._start_task("export", "笔记本导出（续跑）")
            if task_id is None:
                return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
            # 注意：续跑不能 _reset_io()，否则会清掉 partial_emb.npz / resume.json 缓存
            self._set_io("export", "building")
            handle = asyncio.create_task(
                self._run_export_task(
                    task_id,
                    str(resume.get("notebook", "") or ""),
                    str(resume.get("format", "") or ""),
                    str(resume.get("mode", "") or ""),
                    str(resume.get("filename", "") or ""),
                    resume_ctx=resume,
                )
            )
            if self._tasks.get(task_id) is not None:
                self._tasks[task_id]["handle"] = handle
            return web.json_response({"success": True, "task_id": task_id, "kind": "export", "state": "building"})

        return web.json_response({"error": "续跑上下文缺失"}, status=400)

    async def _web_import_file(self, request: Any) -> Any:
        """上传 jsonl/mpj 文件并启动后台校验任务。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        filename = ""
        source = b""
        sample_n = 25
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                filename = part.filename or ""
                source = await part.read()
            elif part.name == "sample":
                try:
                    sample_n = max(0, min(200, int((await part.read()).decode())))
                except Exception:
                    pass
        if not source or not filename:
            return web.json_response({"error": "未收到文件"}, status=400)

        task_id = self._start_task("import_file", "笔记本导入校验")
        if task_id is None:
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
        self._reset_io()
        self._set_io("import", "validating")
        handle = asyncio.create_task(self._run_file_validation_task(task_id, source, filename, sample_n))
        if self._tasks.get(task_id) is not None:
            self._tasks[task_id]["handle"] = handle
        return web.json_response({"task_id": task_id, "kind": "import", "state": "validating"})

    async def _web_import_file_preview(self, request: Any) -> Any:
        """分页返回导入预览条目。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self._get_io_kind() != "import" or self._get_io_state() != "ready":
            return web.json_response({"error": "当前没有可预览的导入"}, status=400)
        page = max(1, int(request.query.get("page", 1)))
        size = max(1, min(200, int(request.query.get("size", 20))))
        _meta, entries = self._read_io_preview()
        total = len(entries)
        start = (page - 1) * size
        end = start + size
        return web.json_response(
            {
                "total": total,
                "page": page,
                "size": size,
                "pages": (total + size - 1) // size if size > 0 else 1,
                "entries": entries[start:end],
            }
        )

    async def _web_import_file_commit(self, request: Any) -> Any:
        """确认并启动后台导入任务。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self._get_io_kind() != "import" or self._get_io_state() != "ready":
            return web.json_response({"error": "当前没有可提交的导入预览"}, status=400)

        body = await self._web_read_body(request)
        target_name = str(body.get("target_name", "") or "").strip()
        mode = str(body.get("mode", "") or "").strip() or "rebuild"
        merge_target = str(body.get("merge_target", "") or "").strip()
        max_retries, on_failure = self._normalize_retry_params(
            body.get("max_retries"), body.get("on_failure")
        )

        if merge_target:
            target_name = merge_target
        elif not target_name:
            return web.json_response({"error": "请输入新笔记本名称或选择合并目标"}, status=400)

        task_id = self._start_task("import_file_commit", "笔记本导入")
        if task_id is None:
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
        self._set_io("import", "importing")
        handle = asyncio.create_task(
            self._run_file_commit_task(
                task_id, target_name, mode, merge_target, max_retries=max_retries, on_failure=on_failure
            )
        )
        if self._tasks.get(task_id) is not None:
            self._tasks[task_id]["handle"] = handle
        return web.json_response({"task_id": task_id, "kind": "import", "state": "importing"})

    async def _web_organize_db_apply(self, request: Any) -> Any:
        """确认并执行 LLM 修改方案，重建索引。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        raw_ops = body.get("operations", [])
        session_id = str(body.get("session_id", "") or "").strip()
        if not isinstance(raw_ops, list) or not raw_ops:
            return web.json_response({"error": "operations 不能为空"}, status=400)

        nb = self._get_notebook(nb_name)
        if nb is None:
            return web.json_response({"error": f"笔记本 '{nb_name}' 不存在"}, status=404)

        async with self._lock:
            if not nb.check_consistency():
                return web.json_response({"error": f"笔记本 '{nb_name}' 索引失效"}, status=400)

            entries = nb.load_notes()
            id_set = {e["id"] for e in entries}
            operations = [o for o in raw_ops if isinstance(o, dict)]

            validate_error = self._validate_organize_operations(operations, id_set)
            if validate_error:
                return web.json_response({"error": f"方案校验失败：{validate_error}"}, status=400)

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
                        return web.json_response({"error": f"update 操作 id 不存在: {op.get('id')}"}, status=400)
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

            try:
                rebuild_stats = await self._rebuild_notebook(nb)
            except Exception as exc:
                self.ctx.logger.error(f"操作数据库应用后索引重建失败: {exc}", exc_info=True)
                return web.json_response(
                    {"error": "方案已写入源文件，但索引重建失败，请执行 /mpj rebuild"}, status=500
                )
            self._create_backup(nb)

            # 应用成功后清除该会话，防止继续追加产生过期方案
            if session_id:
                self._organize_sessions.pop(session_id, None)

        return web.json_response(
            {
                "success": True,
                "remaining": nb.count_notes(),
                "rebuild": rebuild_stats,
            }
        )
