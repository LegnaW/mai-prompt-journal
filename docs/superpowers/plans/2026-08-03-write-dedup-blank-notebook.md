# 写入去重检测 + 新建空白笔记本 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 或 subagent-driven-development 按任务执行。步骤用 `- [ ]` 跟踪。

**Goal:** 为 add/modify 写入增加可配置的去重拦截（LLM 工具直接拒绝、/mpj 指令走 /mpj confirm 确认），WebUI 去重默认阈值改为 0.85，并新增 `/mpj new <name>` 创建空白笔记本。

**Architecture:** 在 plugin.py 单文件内新增纯余弦重复检测助手（`_find_duplicate_matches`，复用写入时已计算的 embedding，与 `_scan_duplicates` 口径一致），在各写入入口调用；/mpj 指令的确认通过内存 pending 表 + `/mpj confirm` 实现。

**Tech Stack:** Python 3.10+ / aiohttp / numpy / maibot_sdk。无 pytest，验证走 AGENTS.md 的 py_compile + AST + 独立脚本 + uv 虚拟环境 schema 生成。

## Global Constraints

- 插件运行在独立 Runner 进程，只能通过 `self.ctx.*` 调用宿主能力；新增能力调用必须同步 `_manifest.json`。本计划**不新增** `ctx.*` 能力。
- 搜索/去重口径统一：纯向量余弦（float32），阈值钳制 `max(0.5, min(0.99, t))`。
- LLM 生成一律走 `_direct_chat`（本计划不改动该区域）。
- 写入前必须 `check_consistency()`；写入后 `update_md5()`。
- 代码注释/日志/UI 文案用简体中文；不改父项目源码。
- 版本号 bump：`_manifest.json` version 与 `config_version` 均改为 `2.3.0`。

---

### Task 1: WebUI 去重默认阈值 0.92 → 0.85

**Files:**
- Modify: `plugin.py:2059,2061,2164,2166`
- Modify: `web/dedup.html:23-24`
- Modify: `web/dedup_guide.md:11`

- [ ] 后端 scan/resolve 默认值改为 `0.85`
- [ ] 前端滑块 `value="0.92"` 与显示 `0.92` 改为 `0.85`
- [ ] 指南文案 "一般保持在 0.92 附近即可" → 0.85
- [ ] 验证：`grep -rn "0\.92" .` 应无残留（logs/ 除外）

### Task 2: JournalConfig 新增去重检测配置 + config_version

**Files:**
- Modify: `plugin.py:185-189`（config_version）、`plugin.py:220-228` 后新增 3 字段

- [ ] `config_version` 默认值 `"2.2.1"` → `"2.3.0"`
- [ ] 新增 `dedup_check_enabled`(bool, default True)、`dedup_check_all_notebooks`(bool, default False)、`dedup_check_threshold`(float, default 0.85, ge=0.5, le=0.99)，均带 `json_schema_extra`(label/hint/order)
- [ ] 验证：`.venv/bin/python` 跑 `generate_plugin_config_schema` 检查新字段

### Task 3: 去重检测助手 + pending 确认存储

**Files:**
- Modify: `plugin.py`（`on_load` 初始化、新增 3 个方法）

- [ ] `on_load` 初始化 `self._pending_confirms: dict[str, dict] = {}`
- [ ] `_pick_dedup_notebooks(target_nb)`：all_notebooks 时返回 `self._notebooks.values()`，否则 `[target_nb]`
- [ ] `_find_duplicate_matches(query_vec, notebooks, threshold, exclude_id="")`：返回 `[{notebook,id,en,zh,note,score}]` 降序；跳过不一致笔记本、跳过 exclude_id
- [ ] `_format_matches(matches)`：格式化匹配行为多行字符串
- [ ] `_evict_pending_confirms()`：TTL 600s、上限 50

### Task 4: add_aidraw_notes 工具去重拦截

**Files:**
- Modify: `plugin.py` `handle_add_notes`（~875-930）

- [ ] embed 后按 `dedup_check_enabled` 逐条 `_find_duplicate_matches`，命中 → 拒写该条，否则收进 accepted
- [ ] 仅写入 accepted 条目（`embeddings[accepted_indices]`），rejected 不进库
- [ ] 返回 `content` 列出被拒条目 + 匹配笔记；结构化字段新增 `rejected`
- [ ] 全部被拒时返回被拒列表

### Task 5: modify_aidraw_note 工具去重拦截

**Files:**
- Modify: `plugin.py` `handle_modify_note`（~1115-1145）

- [ ] 内容变化且 `dedup_check_enabled` 时，用新内容 embedding 调 `_find_duplicate_matches(exclude_id=clean_id)`
- [ ] 命中 → 整次拒绝修改，返回匹配信息；未命中 → 复用该向量更新（避免重复 embed）

### Task 6: /mpj add/modify 指令确认流 + /mpj confirm

**Files:**
- Modify: `plugin.py` `handle_cmd_add` / `handle_cmd_modify`
- Add: `handle_cmd_confirm`（@Command "mpj_confirm"）

- [ ] `/mpj add`、`/mpj modify` 检测到重复 → 不写入，存 pending，回复匹配信息 + `/mpj confirm <token>`
- [ ] `/mpj confirm <token>`：管理员校验 → 查 pending → 一致性校验 → 执行原 add/modify → 清除 pending
- [ ] pending 无效/过期 → 提示重发

### Task 7: /mpj new 新建空白笔记本

**Files:**
- Add: `plugin.py` `handle_cmd_new`（@Command "mpj_new"）

- [ ] 校验名称正则 `^[A-Za-z0-9_\-\u4e00-\u9fff]+$`，拒绝 default/tmp/重名
- [ ] 建空 `imports/{name}.jsonl`，调 `_rebuild_notebook`（空库无 embed 调用）
- [ ] `_discover_notebooks()` 刷新后回复成功

### Task 8: manifest / README / 版本号

**Files:**
- Modify: `_manifest.json:3`（version 2.3.0）
- Modify: `README.md`（更新记录 + 配置表 + 命令列表 + 去重检测说明）

- [ ] manifest version → 2.3.0
- [ ] README 增补 changelog v2.3.0、`[journal]` 新配置、`/mpj confirm` `/mpj new`、去重检测说明

### Task 9: 整体验证

- [ ] `python3 -m py_compile plugin.py`
- [ ] AST 检查 Tool/Command/HomeCard/路由注册无遗漏
- [ ] 独立脚本跑 `_find_duplicate_matches` 余弦用例（命中/未命中/排除自身/钳制）
- [ ] `.venv` 生成 schema 验证 label/hint
