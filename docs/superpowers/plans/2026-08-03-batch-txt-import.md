# txt 批量导入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 txt 批量导入功能：上传 txt 切分后逐段让 LLM 处理，写入临时笔记本，完成后用户可查看/编辑/处置（合并/新建/丢弃）。

**Architecture:** 复用现有 `[organize_db]` 配置节与 `_direct_chat`/`_run_organize_db_round` 的 agent 循环能力，但搜索范围扩展为"用户选定的引用笔记本 + 临时笔记本 tmp"。临时笔记本固定名 `tmp`，文件放 `data_dir/tmp_import/`，用独立 Notebook（`custom_dir`）不参与 `_discover_notebooks`。导入任务走通用任务中心 `_start_task`，与 rebuild 互斥。前端新增独立页 `web/import.html`。

**Tech Stack:** Python 3.13 / aiohttp / numpy / 现有 plugin.py（无新依赖）

## Global Constraints

- 无 pytest，验证用 `python3 -m py_compile plugin.py` + 独立脚本 + `web/` 下 node JS 语法检查 + 真实 aiohttp 端到端脚本
- 首选简体中文注释/日志/WebUI
- 不改父项目源码；改动只在本插件目录
- LLM 生成一律走 `_direct_chat`（直连 OpenAI 兼容 API），不走 `ctx.llm.generate`
- 临时笔记本目录 `tmp_import/` 不参与 `_discover_notebooks`（不注册为笔记本）
- 一轮导入完成后**不清理** tmp_import；下一轮导入开始前清理
- 任务进行中拒绝新任务（复用 `_task_busy`，409）
- 只修改 `web/*.html`、`web/app.js`、`plugin.py`、`AGENTS.md`、`README.md`

---

### Task 1: 配置模型新增 `batch_import_prompt` 字段

**Files:**
- Modify: `plugin.py` 中 `OrganizeDbConfig` 类（约 line 320 附近，`system_prompt` 之后）

**Interfaces:**
- Consumes: 无
- Produces: `config.organize_db.batch_import_prompt: str`（默认值含 `{temp-journal}` 占位符）

- [ ] **Step 1: 在 `OrganizeDbConfig.system_prompt` 字段后新增字段**

```python
    batch_import_prompt: str = Field(
        default="你只能写入/删除/修改`{temp-journal}`中的内容，不要尝试动其他的笔记本。",
        description="批量导入追加提示词（追加在操作数据库系统提示词之后）",
        json_schema_extra={
            "label": "批量导入追加提示词",
            "hint": "txt 批量导入时追加在系统提示词后的约束文本；{temp-journal} 会被替换为临时笔记本名。一般情况下请勿乱动此项目。",
            "order": 4,
            "x-widget": "textarea",
            "rows": 4,
        },
    )
```

- [ ] **Step 2: 验证 schema 生成**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'.'); import plugin as m; from maibot_sdk.config import generate_plugin_config_schema; s=generate_plugin_config_schema(m.PromptJournalConfig); print(s['sections']['organize_db']['fields']['batch_import_prompt']['default'])"`
Expected: 输出默认提示词文本

- [ ] **Step 3: Commit**

```bash
git add plugin.py
git commit -m "feat: organize_db 配置新增批量导入追加提示词字段"
```

---

### Task 2: Notebook 支持 `custom_dir` + 临时笔记本目录初始化

**Files:**
- Modify: `plugin.py` `Notebook.__init__`（约 line 446）
- Modify: `plugin.py` `PromptJournalPlugin.on_load`（约 line 643）

**Interfaces:**
- Consumes: 无
- Produces: `Notebook(name, base_dir, custom_dir=None)` 可选参数；`self._tmp_import_dir` / `self._tmp_nb` / `self._tmp_log_path` 实例属性

- [ ] **Step 1: `Notebook.__init__` 增加 `custom_dir`**

```python
    def __init__(self, name: str, base_dir: Path, custom_dir: Path | None = None) -> None:
        self.name = name
        if custom_dir is not None:
            self._dir = custom_dir
        elif name == "default":
            self._dir = base_dir
        else:
            self._dir = base_dir / "imports"
```

- [ ] **Step 2: `on_load` 初始化临时笔记本**

在 `self._imports_dir.mkdir(...)` 之后追加：

```python
        # 批量导入临时笔记本（固定名 tmp，独立目录，不参与发现逻辑）
        self._tmp_import_dir: Path = self._data_dir / "tmp_import"
        self._tmp_import_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_nb: Notebook = Notebook("tmp", self._data_dir, custom_dir=self._tmp_import_dir)
        self._tmp_log_path: Path = self._tmp_import_dir / "import.log"
```

- [ ] **Step 3: 验证**

Run: `.venv/bin/python -c "import sys; sys.path.insert(0,'.'); import plugin as m; from pathlib import Path; nb=m.Notebook('tmp', Path('/tmp/x'), custom_dir=Path('/tmp/y')); print(nb.notes_path)"`
Expected: `/tmp/y/tmp.jsonl`

- [ ] **Step 4: Commit**

```bash
git add plugin.py
git commit -m "feat: Notebook 支持 custom_dir，初始化批量导入临时目录"
```

---

### Task 3: txt 切分函数 `_split_txt`

**Files:**
- Modify: `plugin.py`（新增模块级函数，放在 `_ORGANIZE_DB_SEARCH_TOOL` 常量之后）

**Interfaces:**
- Consumes: 无
- Produces: `_split_txt(text: str) -> list[str]`（`\n\n` 及以上的连续换行切分，段首尾 strip，忽略空段）

- [ ] **Step 1: 实现**

```python
def _split_txt(text: str) -> list[str]:
    """按两个及以上连续换行切分 txt，每段为一个块。

    \n\n 切分；\n\n\n 也只切一次；单个 \n 不切分。
    """
    parts = re.split(r"\n{2,}", text or "")
    return [p.strip() for p in parts if p.strip()]
```

（需在文件顶部确认已 `import re`；`_parse_notebook_flag` 用了局部 import，检查顶部是否有 `import re`，若没有则在 `_split_txt` 内局部 import。）

- [ ] **Step 2: 切分规则验证脚本**

Run: 独立脚本断言
```python
from plugin import _split_txt
assert _split_txt("a\n\nb") == ["a", "b"]
assert _split_txt("a\n\n\n\nb") == ["a", "b"]          # 多换行只切一次
assert _split_txt("a\nb") == ["a\nb"]                  # 单换行不切
assert _split_txt("only one") == ["only one"]           # 单段
assert _split_txt("") == []                             # 空
assert _split_txt("  a  \n\n  b  ") == ["a", "b"]       # 首尾空白
```
Expected: 全部通过

- [ ] **Step 3: Commit**

```bash
git add plugin.py
git commit -m "feat: txt 批量导入切分函数 _split_txt"
```

---

### Task 4: 多笔记本搜索 `_execute_search_notes_multi`

**Files:**
- Modify: `plugin.py`（`_execute_search_notes` 之后新增）

**Interfaces:**
- Consumes: `self._get_notebook`, `self._embed_single`, `self._search_single_notebook`, `self.config.journal.min_score`, `self._tmp_nb`, `self._tmp_nb.check_consistency/load_notes/load_embeddings`
- Produces: `_execute_search_notes_multi(self, keyword: str, notebook_names: list[str], limit: int = 10) -> str`

- [ ] **Step 1: 实现**

```python
    async def _execute_search_notes_multi(
        self, keyword: str, notebook_names: list[str], limit: int = 10
    ) -> str:
        """跨多个笔记本 + 临时笔记本 tmp 语义检索，合并返回文本结果（批量导入用）。"""
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
```

- [ ] **Step 2: 验证**

Run: `python3 -m py_compile plugin.py`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add plugin.py
git commit -m "feat: 批量导入多笔记本搜索 _execute_search_notes_multi"
```

---

### Task 5: 导入主流程（后台任务 + 逐段 agent 循环）

**Files:**
- Modify: `plugin.py`（`_web_tasks` 之后、`_web_organize_db_apply` 附近新增一组方法）

**Interfaces:**
- Consumes: `_start_task`, `_finish_task`, `_fail_task`, `_evict_tasks`, `_task_busy`, `self._tmp_nb`, `self._tmp_log_path`, `_split_txt`, `_execute_search_notes_multi`, `_direct_chat`, `_extract_json`, `_validate_organize_operations`, `_rebuild_notebook`, `_ORGANIZE_DB_SEARCH_TOOL`, `config.organize_db`
- Produces:
  - `_reset_tmp_import()`：清空 tmp_import 目录并重建空 tmp 笔记本
  - `_append_import_log(text) -> None`
  - `_apply_ops_to_tmp(operations, reason) -> tuple[bool, str]`：应用 operations 到 tmp 并重建
  - `_run_import_segment(segment_text, mode_prompt, cfg, ref_names, progress) -> dict`：一段一完整循环，返回 `{ok, reason, operations, error}`
  - `_run_import_task(task_id, segments, mode_prompt, ref_names) -> None`
  - `_web_import_preview(request)`, `_web_import_start(request)`, `_web_import_status(request)`, `_web_import_tmp_notes(request)`, `_web_import_log(request)`, `_web_import_resolve(request)`

- [ ] **Step 1: `_reset_tmp_import` 与日志**

```python
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
```

- [ ] **Step 2: `_apply_ops_to_tmp`**

```python
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
```

- [ ] **Step 3: `_run_import_segment`（一段一完整循环）**

```python
    async def _run_import_segment(
        self,
        segment_text: str,
        mode_prompt: str,
        cfg: Any,
        ref_names: list[str],
        progress: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """对单段文本跑一次完整 agent 循环，返回 {ok, reason, operations, error}。"""
        system_prompt = str(cfg.system_prompt or "").strip() or _ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT
        import_prompt = str(cfg.batch_import_prompt or "").strip()
        if import_prompt:
            import_prompt = import_prompt.replace("{temp-journal}", "tmp")
            system_prompt = f"{system_prompt}\n{import_prompt}"

        user_parts = [
            f"以下是需要处理的一段文本：\n{segment_text}",
        ]
        if mode_prompt:
            user_parts.insert(0, mode_prompt)
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
                    api_tool_calls.append({"id": str(call.get("id") or ""), "type": "function", "function": func})
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
                                {"keyword": str(args.get("keyword", "") or ""), "notebook": "引用+tmp"}
                            )
                    else:
                        tool_result = f"未知工具: {name}"
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_result})
                continue

            response_text = str(result.get("content", "") or "").strip()
            if not response_text:
                return {"ok": False, "error": "LLM 返回空内容"}
            payload = self._extract_json(response_text)
            if payload is None:
                return {"ok": False, "error": f"LLM 返回内容无法解析为 JSON，完整输出：\n{response_text}"}
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
```

- [ ] **Step 4: `_run_import_task`**

```python
    async def _run_import_task(
        self, task_id: str, segments: list[str], mode_prompt: str, ref_names: list[str]
    ) -> None:
        cfg = self.config.organize_db
        failed: list[dict[str, Any]] = []
        total = len(segments)
        try:
            async with self._lock:
                self._reset_tmp_import()
                for idx, seg in enumerate(segments, 1):
                    task = self._tasks.get(task_id)
                    if task is not None:
                        task["progress"] = {
                            "total": total, "done": idx - 1, "current_index": idx,
                        }
                    log_head = (
                        f"\n[========== 段 {idx}/{total} ==========]\n"
                        f"[时间] {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"[用户输入]\n{seg}\n"
                    )
                    self._append_import_log(log_head)
                    if mode_prompt:
                        self._append_import_log(f"[附加提示词]\n{mode_prompt}\n")
                    result = await self._run_import_segment(seg, mode_prompt, cfg, ref_names, task)
                    if result.get("ok"):
                        self._append_import_log(
                            f"[LLM 决定与理由]\n{result.get('reason', '')}\n"
                            f"[操作]\n{json.dumps(result.get('operations', []), ensure_ascii=False, indent=2)}\n"
                            "[结果] 成功\n"
                        )
                    else:
                        failed.append({"index": idx, "segment": seg, "error": result.get("error", "")})
                        self._append_import_log(f"[结果] 失败：{result.get('error', '')}\n")

            if failed:
                err_lines = ["\n[========== 失败条目汇总 ==========]\n"]
                for f in failed:
                    err_lines.append(
                        f"段 {f['index']}: {f['error']}\n--- 内容 ---\n{f['segment']}\n\n"
                    )
                self._append_import_log("".join(err_lines))

            self._finish_task(task_id, {
                "total": total,
                "failed_count": len(failed),
                "failed": failed,
            })
        except Exception as exc:
            self.ctx.logger.error(f"批量导入后台任务异常: {exc}", exc_info=True)
            self._fail_task(task_id, exc)
        finally:
            self._evict_tasks()
```

- [ ] **Step 5: Commit**

```bash
git add plugin.py
git commit -m "feat: 批量导入主流程（重置 tmp/日志/逐段 agent 循环/后台任务）"
```

---

### Task 6: 批量导入 HTTP API

**Files:**
- Modify: `plugin.py` `_run_web_server` 路由区（约 line 1626-1648）
- Modify: `plugin.py`（新增 6 个 `_web_import_*` handler，紧邻 `_run_import_task` 之后）

**Interfaces:**
- Consumes: Task 5 全部产出 + `_web_read_body` / `_web_check_auth` / `_get_notebook` / `_list_notebook_names`
- Produces: 6 个已注册路由的 HTTP 端点

- [ ] **Step 1: 注册路由**

在 `app.router.add_get("/api/tasks", self._web_tasks)` 之后追加：

```python
            app.router.add_post("/api/import/preview", self._web_import_preview)
            app.router.add_post("/api/import/start", self._web_import_start)
            app.router.add_get("/api/import/status", self._web_import_status)
            app.router.add_get("/api/import/tmp_notes", self._web_import_tmp_notes)
            app.router.add_get("/api/import/log", self._web_import_log)
            app.router.add_post("/api/import/resolve", self._web_import_resolve)
```

- [ ] **Step 2: preview / start / status**

```python
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
        mode = str(body.get("mode", "custom") or "").strip()
        custom_prompt = str(body.get("custom_prompt", "") or "").strip()
        ref_raw = body.get("ref_notebooks", [])
        if not isinstance(ref_raw, list):
            ref_raw = []
        ref_names = [str(n or "").strip() for n in ref_raw if str(n or "").strip()]

        segments = _split_txt(text)
        if not segments:
            return web.json_response({"error": "没有可导入的段落"}, status=400)

        if mode == "custom":
            if not custom_prompt:
                return web.json_response({"error": "自定义模式必须填写附加提示词"}, status=400)
            mode_prompt = custom_prompt
        else:
            mode_prompt = _MODE_PROMPTS.get(mode, "")

        # 校验引用笔记本存在
        for name in ref_names:
            if self._get_notebook(name) is None:
                return web.json_response({"error": f"引用笔记本 '{name}' 不存在"}, status=400)

        # LLM 直连配置完整
        llm_cfg = self.config.llm
        if not (llm_cfg.base_url and llm_cfg.api_key and llm_cfg.model):
            return web.json_response({"error": "LLM 直连配置不完整，请填写 [llm] 的 base_url / api_key / model"}, status=400)

        task_id = self._start_task("import", "txt 批量导入")
        if task_id is None:
            return web.json_response({"error": "已有后台任务进行中，请等待完成后再试"}, status=409)
        asyncio.create_task(self._run_import_task(task_id, segments, mode_prompt, ref_names))
        return web.json_response({"task_id": task_id, "count": len(segments)})

    async def _web_import_status(self, request: Any) -> Any:
        """查询导入任务状态（含失败汇总）。"""
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
        return web.json_response({"status": "error", "error": task.get("error", "")})
```

- [ ] **Step 3: tmp_notes / log / resolve**

```python
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
        return web.Response(text=text, content_type="text/plain",
                            headers={"Content-Disposition": 'attachment; filename="import.log"'})

    async def _web_import_resolve(self, request: Any) -> Any:
        """处置临时笔记本：merge 合并入已有 / create 新建 / discard 丢弃。"""
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
                # 丢弃：仅清空状态，文件留给下一轮导入前清理
                return web.json_response({"success": True, "action": "discard"})

            if not entries:
                return web.json_response({"error": "临时笔记本为空，无需处置"}, status=400)

            if action == "merge":
                target = str(body.get("target_notebook", "") or "").strip() or "default"
                nb = self._get_notebook(target)
                if nb is None:
                    return web.json_response({"error": f"目标笔记本 '{target}' 不存在"}, status=404)
                if not nb.check_consistency():
                    return web.json_response({"error": f"笔记本 '{target}' 索引失效，请先 /mpj rebuild"}, status=400)
                # 复用 tmp 已有向量（embedding 文本相同，维度一致），直接追加
                embeddings = self._tmp_nb.load_embeddings()
                if embeddings is None or len(embeddings) != len(entries):
                    return web.json_response({"error": "临时笔记本向量不完整，无法合并"}, status=400)
                nb.append_entries(entries, embeddings)
                nb.update_md5()
                return web.json_response({"success": True, "action": "merge", "target": target, "count": len(entries)})

            # create：新建笔记本，复制 tmp 四个文件为 {new_name}
            new_name = str(body.get("new_name", "") or "").strip()
            if not new_name:
                return web.json_response({"error": "新建笔记本必须填写名称"}, status=400)
            if new_name == "default" or self._get_notebook(new_name) is not None:
                return web.json_response({"error": f"笔记本 '{new_name}' 已存在"}, status=400)
            import re as _re
            if not _re.match(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$", new_name):
                return web.json_response({"error": "笔记本名称只能包含中文/字母/数字/下划线/连字符"}, status=400)

            base = self._data_dir / "imports"
            base.mkdir(parents=True, exist_ok=True)
            new_nb = Notebook(new_name, self._data_dir)
            for suffix in (".jsonl", ".cache.jsonl", ".embeddings.npy", ".index.meta"):
                src = self._tmp_import_dir / f"tmp{suffix}"
                dst = new_nb._dir / f"{new_name}{suffix}"
                if src.exists():
                    import shutil
                    shutil.copyfile(src, dst)
            self._notebooks = self._discover_notebooks()
            return web.json_response({"success": True, "action": "create", "name": new_name, "count": len(entries)})
```

- [ ] **Step 4: 定义 `_MODE_PROMPTS`（模块级常量，与前端四个模式一致）**

在 `_ORGANIZE_DB_SEARCH_TOOL` 之后新增：

```python
_MODE_PROMPTS = {
    "learn_style": "阅读以下文本，找出有学习价值且没有与已知数据库重复的提示词片段。应优先记录有意义的 tag 组合或搭配，不要只记录孤立、通用的单一 tag。若关键描述片段本身是用自然语言写成的，也可以直接以这段自然语言进行记录。对每个片段在note中简要总结其作用并给出使用场景。",
    "import_character": "以下为某个自定义角色形象的名称和形象设定，若笔记本内不存在这名角色的设定信息，则将其整理为1条笔记，在备注中写明人物名称。若存在重复，则更新已有的笔记。",
    "action_template": "以下为一组完整的提示词，请去除其中的所有角色形象描述（包括发型发色、外观面貌、服装饰品等等），仅保留动作描述，整理为1条笔记，在备注中除内容简要总结/使用场景外，还要额外备注\"此为动作模板，无形象描述，需添加后使用\"。",
    "none": "",
}
```

- [ ] **Step 5: 验证**

Run: `python3 -m py_compile plugin.py`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add plugin.py
git commit -m "feat: 批量导入 HTTP API（preview/start/status/tmp_notes/log/resolve）"
```

---

### Task 7: 前端 `web/import.html` + 导航

**Files:**
- Create: `web/import.html`
- Modify: `web/app.js` 的 `NAV_ITEMS`

**Interfaces:**
- Consumes: `/api/status`, `/api/import/preview`, `/api/import/start`, `/api/import/status`, `/api/import/tmp_notes`, `/api/import/log`, `/api/import/resolve`, `loadStatus()`, `pollTasks()`, `esc()`, `api()`
- Produces: 无（纯前端）

- [ ] **Step 1: `NAV_ITEMS` 增加一项**

```js
  { id: 'import', href: '/web/import.html', label: '📥 批量导入' },
```
放在 organize 之后。

- [ ] **Step 2: 创建 `web/import.html`**

骨架（完整实现见下）：
- 顶部：`<div id="nav"></div>` + 卡片
- **上传预览区**：`<input type="file" accept=".txt">` + "解析预览"按钮 + 段落列表（`#previewSegments`，每段可展开）
- **配置区**（解析出段落后才显示）：模式单选（learn_style / import_character / action_template / custom，默认 custom）+ "自定义"时弹窗输入框 + 引用笔记本多选复选框（来自 `/api/status` 的 notebooks，可全不选）+ "开始导入"按钮
- **进度区**：`#importProgress`（显示"段 X/Y"）
- **结果区**（完成后显示）：临时笔记本条目列表（每条目内联编辑/删除）+ 失败段列表 + 下载 log 按钮 + 处置按钮（合并入已有[下拉] / 新建[输入名] / 丢弃）
- 页面底部：`injectNav('import'); loadStatus();`

JS 关键函数：
- `doPreviewImport()`：读文件 → `POST /api/import/preview` → 渲染段落列表 → 显示配置区
- `collectRefNotebooks()`：读取勾选的引用笔记本
- `doStartImport()`：校验模式/自定义提示词 → `POST /api/import/start` → 存 task_id → `pollImportStatus()`
- `pollImportStatus()`：轮询 `/api/import/status`，running 时显示进度，done 时渲染结果区并 `loadStatus()`
- `renderImportResult()`：`GET /api/import/tmp_notes` + result.failed → 渲染条目/失败列表
- `saveImportEdit(id)` / `delImportEntry(id)`：复用 `/api/modify`、`/api/delete`（notebook="tmp"），完成后重新渲染
- `downloadImportLog()`：跳转 `/api/import/log`
- `doResolveImport(action)`：调用 `/api/import/resolve`，成功后 `loadStatus()`

- [ ] **Step 3: JS 语法 + HTML 配对验证**

Run: `cd web && node -e "..."`（复用现有检查脚本）+ div 配对脚本
Expected: 全部 OK

- [ ] **Step 4: Commit**

```bash
git add web/import.html web/app.js
git commit -m "feat: 批量导入前端页面 web/import.html + 导航"
```

---

### Task 8: 端到端验证 + 文档

**Files:**
- Modify: `AGENTS.md`（核心文件表 + 新增"批量导入"段落）
- Modify: `README.md`（近期更新 + 功能特性）
- Create: `/tmp/opencode/import_e2e.py`（临时端到端测试，不提交）

**Interfaces:**
- Consumes: 全部前述产出

- [ ] **Step 1: 端到端测试脚本**

Run 独立 aiohttp 脚本（mock 插件实例，参考此前 rebuild 测试），覆盖：
1. `_split_txt` 规则
2. `/api/import/preview` 切分返回
3. `/api/import/start` 返回 task_id；有任务时 409
4. 模拟一段处理（mock `_run_import_segment` 立即成功）→ status done → tmp_notes 有内容 → log 有记录
5. `merge`/`create`/`discard` 三态（create 后 `_discover_notebooks` 能发现新笔记本）
6. 失败段跳过并记入 log

Expected: 全部断言通过

- [ ] **Step 2: AGENTS.md 更新**

- 核心文件表加 `web/import.html` 一行
- 新增小节"批量导入（txt）"：切分规则、临时目录 tmp_import/、一段一循环、搜索范围=引用+tmp、处置三态、配置字段 batch_import_prompt

- [ ] **Step 3: README.md 更新**

- 近期更新内容顶部加"新增 txt 批量导入功能"
- 功能特性表加一行
- 使用方式加"批量导入"小节（简要）

- [ ] **Step 4: 完整回归**

Run: `python3 -m py_compile plugin.py` + `cd web && node`（四页 JS）+ div 配对 + `.venv/bin/python` schema 生成
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md
git commit -m "docs: 批量导入功能文档与近期更新"
```

---

## Self-Review

- **Spec 覆盖**：切分（Task 3）✓；模式四选 + 自定义弹窗（Task 6/7）✓；引用笔记本多选/可不选（Task 7）✓；tmp_import 独立目录 + 固定名 tmp + 每轮覆盖（Task 2/5）✓；搜索范围=引用+tmp（Task 4）✓；一段一完整循环（Task 5）✓；三种操作仅能作用于 tmp（追加提示词 Task 5 Step 3）✓；部分失败跳过 + log 末尾附加错误（Task 5 Step 4）✓；完成后查看/编辑/失败项/log 下载/处置三态（Task 6/7）✓；进度显示在导入页 + 顶部任务栏（Task 5 `_start_task` + Task 7 + 既有 TaskCenter）✓；下一轮开始前清理（Task 5 Step 1 `_reset_tmp_import`）✓
- **占位符**：无 TBD/TODO
- **类型一致**：`_split_txt` → `list[str]`；`_run_import_segment` → `{ok, reason, operations, error}`；`_execute_search_notes_multi(keyword, notebook_names, limit)` 与 Task 5 Step 3 调用一致；`_start_task("import", ...)` 与既有任务中心一致
- **待确认点**：`_run_import_task` 中 `_reset_tmp_import` 在锁内调用（与 rebuild 一致）；`import_character`/`action_template` 模式提示词与前端 `web/organize.html` 的 `ORGANIZE_DB_MODE_PROMPTS` 文案保持同步（Task 6 Step 4 已对齐）
