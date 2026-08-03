# 麦麦的绘图笔记本（mai-prompt-journal）开发指南

> **⚠️ 本文件已脱敏（不含本地绝对路径）。** 本插件的源码位于麦麦的插件目录，主程序源码位于麦麦的部署根目录下（`MaiBot-main/`）。若开发时确需访问这些目录，请先向使用者询问这两个目录的具体路径，不要自行猜测。

## 目录速查

| 用途 | 路径 |
|------|------|
| 本插件源码 | 麦麦插件目录中的 `mai-prompt-journal/` |
| 主程序源码 | 麦麦部署根目录下的 `MaiBot-main/` |
| 插件开发文档 | 麦麦的 `docs-main/zh/plugin/`（推荐先读 `manifest.md`、`tools.md`、`commands.md`、`api-reference.md`） |
| SDK 官方指南 | https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md |
| 能力清单（权威） | 主程序源码 `src/plugin_runtime/capabilities/registry.py` |
| 权限校验实现 | 主程序源码 `src/plugin_runtime/host/authorization.py` |
| 参考插件示例 | 主程序源码 `plugins/hello_world_plugin/plugin.py` |
| 配置 Schema 参考（json_schema_extra 用法） | 其他插件的 `core/config.py`（如 maimai-drawpic-plugin） |

## 插件概览

本插件为 AI 提供"绘图提示词笔记"能力：AI 或管理员把绘图标签经验（英文 tag + 中文释义 + 备注）写入多本"笔记本"，通过向量语义检索回忆历史经验。

- 4 个 LLM 工具：`add_aidraw_notes` / `read_aidraw_notes` / `modify_aidraw_note` / `delete_aidraw_note`
- 7 个管理员指令：`/mpj add|search|modify|delete|refresh|rebuild|help`
- 1 个嵌入式 WebUI（aiohttp 独立端口，默认 8010，可选密码）
- WebUI 去重功能：语义扫描 → LLM 整理（可配置任务名与系统提示词）
- 1 个 WebUI 首页 HomeCard

## 核心文件

| 文件 | 职责 |
|------|------|
| `_manifest.json` | 插件元信息 + 能力声明 + 依赖声明 |
| `plugin.py` | 全部业务逻辑（~3100 行） |
| `web/index.html` | WebUI 首页（状态栏 + 搜索/浏览 + 添加 + 索引管理） |
| `web/dedup.html` | WebUI 去重页 |
| `web/organize.html` | WebUI 操作数据库页 |
| `web/import.html` | WebUI txt 批量导入页 |
| `web/notebooks.html` | WebUI 删除笔记本页 |
| `web/app.js` | WebUI 共享逻辑（api/esc/登录/loadStatus/导航注入） |
| `web/style.css` | WebUI 共享样式 |
| `config.toml` | 运行时配置 |

WebUI 为多页面静态站点（无构建步骤）：`/` 返回 `web/index.html`，`/web/` 目录由 `_run_web_server` 中 `app.router.add_static("/web/", web_dir)` 提供服务。新增功能页 = 新建 `web/*.html` + 在 `app.js` 的 `NAV_ITEMS` 加导航项，并在页面底部调用 `injectNav('<id>')` + `loadStatus()`。

## 重要架构约束（先读，避免踩坑）

### 1. 插件运行在独立 Runner 进程
插件与宿主（主进程）通过 `self.ctx.*` RPC 代理通信，**不能直接 import 宿主内部模块**、不能访问宿主内存。所有宿主能力都通过 `ctx` 代理调用。

### 2. 能力声明必须精确匹配（E_CAPABILITY_DENIED 坑）
任何 `self.ctx.*` 调用都必须在 `_manifest.json` 的 `capabilities` 数组里声明对应能力字符串，**精确匹配，无映射/归一化**。未声明的能力调用会报 `E_CAPABILITY_DENIED`。

本插件当前用到：
- `llm.embed`（embedding，搜索/检索都需要）
- `send.text`（`/mpj` 指令回复）
- `component.disable` / `component.enable`（`allow_write` 开关控制 add/modify/delete 三个写入工具的启用/禁用）

注意：**LLM 文本生成（去重整理、操作数据库）不再走麦麦的 `ctx.llm.generate`/`generate_with_tools`**，而是通过插件直连的 OpenAI 兼容 API（`_direct_chat`，配置见 `[llm]` 节）。因此 manifest 不声明 `llm.generate` 能力。若将来回退到麦麦管线，需把能力加回 manifest 并重载插件（能力令牌在插件加载时注册）。

常用能力速查：
- LLM：`llm.generate` / `llm.embed` / `llm.transcribe_audio`
- 发送：`send.text` / `send.image` / `send.forward` / `send.hybrid`
- 配置：`config.get` / `config.get_plugin`
- 数据库：`database.query` / `database.save` 等

新增 `ctx.*` 调用时，**先查 registry.py 确认能力字符串，再同步更新 manifest**。

### 3. 依赖声明
第三方 Python 依赖在 `_manifest.json` 的 `dependencies` 声明（`type: "python_package"`），插件系统自动安装。当前依赖：`numpy`、`aiohttp`。

## 数据模型

### 笔记条目格式（JSONL 每行一个）
```json
{"id": "2d6vo4gwlc", "en": "cat ears", "zh": "猫耳", "note": "画猫娘必备", "ts": 1722500000.0}
```
- `id`：毫秒时间戳经"模乘置换 + base36 编码"生成的唯一 ID，**严格双射可逆**（见 `scramble_id()`）。相邻时间戳的 ID 完全无序，长度 7~10 字符。
- `en` / `zh`：必填；`note`：可空；`ts`：创建时间戳

### 笔记本文件族
每个笔记本由 4 个文件组成，`{name}` 为笔记本名：

| 文件 | 说明 |
|------|------|
| `{name}.jsonl` | 人类可编辑的笔记源文件（用户可直接改） |
| `{name}.cache.jsonl` | 与向量索引对齐的内部快照（增量重建比对用） |
| `{name}.embeddings.npy` | float16 向量矩阵，行号 = jsonl 行号 |
| `{name}.index.meta` | 元信息：`{md5, count, built_at}` |

- `default` 笔记本文件在插件 data_dir 根目录
- 第三方笔记本文件在 `data_dir/imports/{name}.jsonl`（用户投放，`/mpj refresh` 后被发现）
- 数据目录：`self.ctx.paths.data_dir`（实际在 `data/plugins/mai.prompt-journal/`）

## 一致性机制（核心规则，勿破坏）

### MD5 校验
- 每次读/写操作前，计算 `{name}.jsonl` 的 MD5，与 `index.meta` 里的 `md5` 比对
- 用户手动编辑 jsonl → MD5 失配 → 工具返回失败，提示管理员执行 `/mpj rebuild`
- 程序自身写入后调用 `update_md5()` 刷新缓存
- 首次运行（无文件）视为合法空状态

### 增量重建
`/mpj rebuild` 对每个笔记本：
1. 读 `{name}.jsonl`（当前）和 `{name}.cache.jsonl`（快照）
2. 每条计算 `content_hash = md5(en \x00 zh \x00 note)`
3. content_hash 相同的条目**直接复用已有向量**（0 次 API 调用）
4. 仅对新增/变更条目批量 embed
5. 全部缓存命中时做一次探测 embed 校验向量维度（模型切换维度变化会强制全量重建）

## 搜索逻辑

### `_cosine_topk_boosted`（当前唯一搜索入口）
流程：向量余弦 → 放宽阈值扩大候选池（min_score×0.5，top_k×3）→ 文本匹配加分 → 重排 → 应用原始阈值 → top_k

### 加分规则（`_compute_text_boost`，优先级递减取最高）
| 规则 | 条件 | 加分 |
|------|------|------|
| Rule 1 | query == en 或 query == zh | +0.30 |
| Rule 2 | en/zh（≥2字符）是 query 的子串 | +0.25 |
| Rule 3 | query（≥2字符）是 en/zh 的子串 | +0.15 |
| Rule 4 | 英文词重叠 + 中文字符重叠 | +0.05×数，封顶 +0.15 |

Rule 2 处理多关键词和中文长句（如 query "我想画猫耳女孩" 命中 zh="猫耳"）。

### 调用链约束
修改搜索必须同步更新参数传递：
- `_search_single_notebook(nb, query_text, query_vec, top_k, min_score)`
- `_search_all_notebooks(query_text, query_vec, top_k, min_score)`
- 所有调用方：`handle_read_notes`（工具）、`handle_cmd_search`（命令）、`_web_search`（WebUI）都要把 query 文本传入

## 去重与 LLM 整理（WebUI 功能）

### 扫描
- `GET /api/dedup/scan?notebook=&threshold=`：L2 归一化 → N×N 余弦相似度矩阵 → 贪心聚类（`_scan_duplicates`）。
- 阈值范围 0.5~0.99（后端钳制 `max(0.5, min(0.99, ...))`），WebUI 滑块 min 同步为 0.5。

### 处理方式（`POST /api/dedup/resolve`）
- **organize LLM 整理**（唯一模式）：删除整组原条目，写入前端回传的 `new_entries`（各生成新 `scramble_id` + `ts`）。
  - 预览端点 `POST /api/dedup/organize_preview`：组内条目 + 每组一个"整理意见"输入框（`requirement`，前端提交该组自己的框内容）→ LLM 返回 `{reason, entries:[...]}` → 展示给用户确认后再执行。
  - LLM 可能把一组整理成 1~N 条（不只 1 条），并返回整理理由。
- 组内每条目带**修改 / 删除**按钮（`startDedupEdit`/`saveDedupEdit`/`doDedupDelete`），走既有 `/api/modify`、`/api/delete`，保存/删除后调 `doDedupScan()` 重新扫描刷新列表。

### 关键：合并后必须重建索引 + 重扫（勿回退）
`_web_dedup_resolve` 处理流程（全程在 `self._lock` 内）：
1. 计算 `final_entries`（保留未删条目 + LLM 整理的新条目）
2. **只写 `notes_path`（源文件），不动 cache/embeddings**
3. 调 `_rebuild_notebook(nb)` 增量重建（未变条目按 content_hash 复用向量，变更/新增自动补嵌）
4. `_scan_duplicates(nb.load_notes(), nb.load_embeddings(), threshold)` 重扫，返回新 groups
5. 前端用返回的 groups 整体重渲染

**为什么必须"只写源文件 + 重建"，而不是手工改向量**：合并/整理会改动条目内容，若用旧逻辑 `rewrite_all` 重写 cache+embeddings，保留条目的 note 已变但向量仍是旧的 → embedding 与内容失配。只有"notes 先行、缓存/向量交给增量重建对齐"才能保证索引与源文件一致，也顺带满足"每次合并后索引必须刷新（合并可能影响其他组的归属）"的需求。

### LLM 生成统一走直连 API（`[llm]` 节，重要）
去重整理与整理数据库的**所有 LLM 文本生成都通过插件直连的 OpenAI 兼容 API**（`_direct_chat`），不再走麦麦的 `ctx.llm.generate`/`generate_with_tools`。

**为什么**：麦麦的消息管线（`Message`/`MessageBuilder`）不承载 `reasoning_content`，thinking 模式的模型在多轮工具调用时要求 assistant 消息**原样回传 `reasoning_content`**，否则 API 返回 400（`The reasoning_content in the thinking mode must be passed back to the API`）。直连后可完全控制请求载荷，规避该问题。

- 配置节 `[llm]`：`enabled` / `base_url`（OpenAI 兼容地址）/ `api_key` / `model`（具体模型名）/ `temperature` / `max_tokens` / `timeout` / `extra_params`（JSON 透传请求体，如 `{"enable_thinking": false}`）。**无回落机制**：`base_url`/`api_key`/`model` 任一为空即报错返回，不会退回麦麦。
- `_direct_chat(messages, tools=None)`：aiohttp 非流式 POST `{base_url}/chat/completions`（`base_url` 以 `/v1` 结尾时直接拼 `/chat/completions`），返回 `{success, content, reasoning_content, tool_calls}`。
- **`tool_calls[].function.arguments` 在原始 API 里是 JSON 字符串**，`_direct_chat` 已 `json.loads` 成 dict。
- 多轮工具循环里，**assistant 消息必须带上 `reasoning_content`**（`{"role":"assistant","content","reasoning_content","tool_calls"}`），否则 thinking 模型下一轮 400。
- **回传 assistant 消息时 `tool_calls` 必须还原为 API 格式**：`function.arguments` 要 `json.dumps` 回 JSON **字符串**（dict 会被网关 serde 拒绝，报 `invalid type: map, expected a string`），并补 `"type": "function"`（`_direct_chat` 归一化时丢弃了该字段）。执行工具用解析后的 dict，回传用字符串，两者分开。
- `extra_params` 里若提供商支持 `{"enable_thinking": false}` 可关闭思考，从根上避免该问题。
- embedding 仍走 `ctx.llm.embed`（保证与笔记本向量维度一致），不参与直连。

### 去重整理（`config.toml` `[dedup_merge]`，重启生效）
`enabled` / `system_prompt`（空 = 内置默认）。模型参数由 `[llm]` 节统一配置。

- 调用 `_direct_chat(messages)`，`messages = [system, user]`，系统提示词放在 message list 里。
- 内置系统提示词要点：修正过度安全拒绝（绘图标签可能敏感，禁止拒绝/省略/软化）、限定输出严格 JSON（`{"reason","entries":[{en,zh,note}]}`）、明确 entries 允许 1~N 条（防止模型误以为只能输出 1 条）、禁止强行合并丢信息。
- 解析用 `_extract_json`（剥代码围栏 + 括号配对取首个 JSON 对象），失败返回 502 让前端重试。

### 操作数据库（`config.toml` `[organize_db]`，多轮会话 + 后台任务）
`enabled` / `max_iterations` / `search_limit` / `system_prompt`（空 = 内置默认）。功能名用"操作"而非"整理"（可 create/update/delete，含导入新内容）。

- **多轮会话**：`self._organize_sessions`（内存，上限 20 按 `created_at` 淘汰，`_evict_organize_sessions`）。
- **后台任务 + 进度轮询**：`POST /api/organize_db/plan` 只做快速校验（笔记本存在/索引有效），然后 `asyncio.create_task` 跑 `_organize_db_task` 并立即返回 `{task_id}`；`GET /api/organize_db/plan_status?task_id=` 轮询进度。
  - 任务进度存在 `self._organize_tasks[task_id]`，`_run_organize_db_round` 每执行一次 search_notes 就往 `progress["searches"]` 追加 `{keyword, notebook}`（前端显示"正在检索：'x'（第 N 次）"）
  - 完成 → `{"status":"done","plan":{session_id, reason, operations}}`（operations 已在任务内富化 `_old` 当前值）；失败 → `{"status":"error","error":具体错误}`；`_evict_organize_tasks` 保留 300s 且上限 50
- **请求体** `{notebook, requirement?, session_id?}`：
  - 无 `session_id` → 新建会话跑初始轮；有 → 校验会话/notebook/非空补充后追加 user 消息重跑**覆盖**上一轮
  - `session_id` 不存在/不匹配 → error"会话已过期"；补充要求为空 → error"补充要求不能为空"
- **错误透传**：`_run_organize_db_round` 返回 `(plan, messages, error)`，`_organize_db_plan` 失败返回 `{"_error":"llm","message":具体错误}`（含 `_direct_chat` 的 HTTP 详情），任务把具体错误放进 status，前端 `data.error` 直接展示（不再只显示"操作失败"）。
- **模式（学习描述方式/导入人物形象/提取动作模板/无附加提示词）是纯前端**：`web/organize.html` 单选 `organizeDbMode`（默认 `none`），`doOrganizeDbPlan` 按模式把常量 `ORGANIZE_DB_MODE_PROMPTS` 前置拼进 `requirement` 提交（后端无 mode 字段）；只影响首轮，补充轮不附加。输入框上方有 `updateOrganizeDbModePrompt()` 驱动的只读展示区，实时显示当前模式附加的提示词全文（`none` 显示"无"）。
- `POST /api/organize_db/apply` 成功后清除对应 `session_id` 会话。
- 前端对话框内：方案预览 + [补充要求输入框 + 追加要求] + [确认执行] + [清除]，每轮刷新只显示最新方案。

## txt 批量导入（WebUI 功能）

把一份 txt 按段落批量交给 LLM 处理，写入临时笔记本，完成后可查看/编辑/处置。

- **切分**：`_split_txt`（模块级函数）按两个及以上连续换行（`\n{2,}`）切分，段首尾 strip，忽略空段，单段也能导入。
- **临时笔记本**：固定名 `tmp`，文件在 `data_dir/tmp_import/`（`tmp.jsonl` / `tmp.cache.jsonl` / `tmp.embeddings.npy` / `tmp.index.meta` / `import.log`），用 `Notebook("tmp", data_dir, custom_dir=tmp_import_dir)` 构造，**不参与** `_discover_notebooks`（但 `_get_notebook("tmp")` 返回它，供 `/api/modify`、`/api/delete` 编辑临时条目）。一轮完成后**不清理**；下一轮导入开始前 `_reset_tmp_import()` 清空。
- **一段一完整循环**：`_run_import_segment` 对每段独立跑 agent 循环（多轮 search_notes），搜索范围 = 用户选择的引用笔记本 + 临时笔记本（`_execute_search_notes_multi`）；系统提示词 = `organize_db.system_prompt` + `\n` + `batch_import_prompt`（`{temp-journal}` 替换为 `tmp`，约束 LLM 只能改临时笔记本）；输出完整 create/update/delete，`_apply_ops_to_tmp` 应用后 `_rebuild_notebook(tmp)` 增量重建，下一段可见。
- **模式（附加提示词）是纯前端**：`web/import.html` 四模式单选（学习描述方式/导入人物形象/提取动作模板/自定义），预设直接发对应 `IMPORT_MODE_PROMPTS` 文本，自定义弹窗输入；`POST /api/import/start` 的 `mode_prompt` 字段后端仅校验非空（双重校验）。
- **失败处理**：某段 LLM 调用/解析/写入失败 → 跳过该段，记录到 result.failed，继续下一段；导入完成后把失败汇总追加到 `import.log` 末尾。
- **日志**：`import.log` 记录每段时间、用户输入、附加提示词、LLM 决定与理由（reason + operations）、成功/失败。
- **处置**（`POST /api/import/resolve`）：`merge` 合并入已有笔记本（复用 tmp 向量直接追加）、`create` 新建笔记本（复制 tmp 四文件到 `imports/{new_name}.jsonl`，`_discover_notebooks` 自动发现）、`discard` 丢弃（仅清空状态，文件留给下一轮清理）。
- **API**：`POST /api/import/preview`（切分预览）/ `POST /api/import/start` / `GET /api/import/status` / `GET /api/import/tmp_notes` / `GET /api/import/log` / `POST /api/import/resolve`。
- 导入走通用任务中心 `_start_task("import", ...)`，与 rebuild 互斥（进行中拒绝新任务，409）；进度在导入页 + 顶部任务栏同步显示。

## 命令规范

- 所有 `/mpj *` 指令**必须管理员校验**：`self._is_admin(user_id)` 检查 `config.admin.users` 列表
- **非管理员静默无视**：`return True, "", False`（不发消息、不报错）
- 笔记本名称统一用 `-n xxx` 后缀语法，`_parse_notebook_flag()` 解析
- `search` 支持 `-n all` 跨笔记本搜索

## WebUI 规范

### 架构
- 插件内启动 aiohttp server（`on_load` 中 `create_task`，`on_unload` 中 cancel）
- 配置：`config.web.enabled` / `port`（默认 8010）/ `password`（空 = 无鉴权）
- 端口需在 Docker 中额外映射

### 坑：WebUI 服务器卸载时必须 `runner.cleanup()`，否则重载不生效
`_run_web_server` 末尾是 `await asyncio.Event().wait()`。**必须**在其 `finally` 里调用 `await self._web_runner.cleanup()` 释放端口：
- 插件重载/卸载是**同一进程内** `on_unload` + `on_load`，若旧 server 的 socket 不释放，新 server `TCPSite.start()` 会因端口占用失败，旧 server（旧配置，如旧密码）继续服务 → 表现为"重载插件后配置（尤其密码）不生效，只有重启麦麦才行"
- 密码本身是 `_web_check_auth` 每请求读取 `self.config.web.password`，配置热更新后立即生效；bind/port/enabled 变更需重启 server

### 认证（关键）
- `_web_index` **必须始终返回 HTML**，鉴权由 API 端点负责（历史教训：曾因在 index 处拦截返回纯文本 401，导致用户看不到登录页）
- 登录走 `POST /api/login`：校验密码后 `set_cookie("mpj_auth", 密码, httponly=True, samesite="Strict", path="/", max_age=7天)`；`POST /api/logout` 清 cookie
- `_web_check_auth`：优先校验 HttpOnly cookie `mpj_auth`，再兼容 `Authorization: Bearer` header（脚本/API 客户端）；与 `config.web.password` 比对
- 前端不再存 `localStorage`、不再手动加 Bearer header（cookie 由浏览器自动携带）；API 401 时弹出登录框
- 登录/登出端点**不能**被安全警告模式的中间件放行（警告模式本就该全拦截）

### 前端踩坑记录
- `.hidden { display: none !important; }` **必须有 `!important`**（历史教训：`.login-overlay` 的 `display:flex` 会覆盖无 `!important` 的 `.hidden`，导致登录遮罩永显）
- 新增 API 路由需在 `_run_web_server` 中注册

## 插件配置页（宿主 WebUI 配置编辑器）Schema 描述

### 坑：字段描述不显示、标签显示英文字段名
宿主配置页前端 `dashboard/src/routes/plugin-config.tsx` 的 `FieldRenderer` **只读取 schema 的 `label` 和 `hint` 两个字段，从不读取 pydantic 的 `description`**。而 SDK 生成 schema（`maibot_sdk/config.py` 的 `_build_field_schema`）时：
- `label = json_schema_extra["label"] or 字段名`（不写就显示英文字段名）
- `hint = json_schema_extra["hint"]`（不写就为空 → 没有说明文字）

所以只写 `Field(description=...)` 的字段在配置页会显示"英文字段名 + 无说明"（文档 config.md 声称 description 会显示，与实际前端不符）。

### 坑：manifest 的 `i18n` 是宿主必填字段，不能整体删除
宿主 `PluginManifest` 校验要求 `i18n` 字段必须存在（否则插件**加载失败**，配置页会回退到无描述的兜底 schema，表现为"配置描述全消失"）。但 `i18n` 内部只有 `default_locale` 必填，`locales_path` / `supported_locales` 可选。
- **正确写法**：`"i18n": {"default_locale": "zh-CN"}`（不声明 `locales_path`，就不会有"缺失 `_locales` 目录"的问题）
- 千万别整块删掉 `i18n`（宿主报 `缺少必需字段: i18n`）。插件中心 CONTRIBUTING 建议的"移除 i18n 字段"与宿主校验矛盾，以宿主为准。

### 正解：每个字段都要 `json_schema_extra`
```python
Field(
    default=...,
    description="...",   # 兼容其他消费方，仍需保留
    json_schema_extra={
        "label": "中文显示名",   # 必填
        "hint": "字段说明文字",  # 必填（配置页唯一展示位置）
        "order": 0,             # 组内排序
    },
)
```
参考成熟插件：其他插件的 `core/config.py`（如 maimai-drawpic-plugin，每个字段都带 label/hint/order）。

### 其他 json_schema_extra 用法
- `Optional` 类型（`float | None` / `int | None`）会被 SDK `_map_field_type` 映射为 `string` → 渲染成文本输入框。要保留"可留空"语义又想显示数字输入框，用 `"x-widget": "number"` 强制。
- 长文本（如系统提示词）用 `"x-widget": "textarea", "rows": 8`。

### 配置节标题与描述
- 节标题 = `__ui_label__`，节描述 = 类 docstring（`_build_section_schema` 用 `config_class.__doc__`）。给每个配置节类写好中文 docstring。

### 验证 Schema 是否生成正确
宿主 `python3`（3.10）无法 import 容器 SDK，用已装好依赖的 uv 虚拟环境实测（执行环境：麦麦部署根目录下 `MaiBot-main/.venv/bin/python`）：
```python
import sys
sys.path.insert(0, "<插件目录>")
import plugin as m
from maibot_sdk.config import generate_plugin_config_schema
schema = generate_plugin_config_schema(m.PromptJournalConfig)
# 逐字段检查 label/hint/ui_type
```

## 代码规范（沿用父项目）

- 首选简体中文：注释、日志、WebUI 展示、工具描述
- import 顺序：标准库/第三方用 `from ... import` 在前、`import ...` 在后，按字母序；本地模块放最后
- 保留原注释和类型注解；复杂函数补充注释
- 不随意用 fallback 掩盖错误，让异常完整暴露
- 不要改父项目源码；插件改动只在本插件目录

## 验证方法

无 pytest。改完必须：
1. `python3 -m py_compile plugin.py` 验证语法
2. 用 AST 检查确认 Tool/Command/HomeCard/路由注册无遗漏
3. 若改 `_compute_text_boost`，用独立脚本跑加分规则用例
4. 若改 WebUI，用脚本检查 HTML div 配对、关键元素存在
5. 若改配置模型，用 uv 虚拟环境（`MaiBot-main/.venv/bin/python`）跑 `generate_plugin_config_schema` 实测 label/hint/ui_type（见上文"验证 Schema"）

## 维护提示

- 修改 ID 生成逻辑会影响历史数据（`scramble_id` 是双射，ID 不可随意改格式）
- 修改 JSONL 格式要同步迁移逻辑（`_migrate_legacy` 负责旧 `notes.jsonl` → `default.jsonl`）
- 新增笔记本操作时，检查 `rewrite_all` / `append_entries` / `update_md5` 是否都调用了
- 修改搜索、去重、重建逻辑时注意 `self._lock` 并发保护
- **`[journal] allow_write`**：只读模式开关。`_apply_write_tools_state()` 用 `ctx.component.disable/enable_component(name, "tool", scope="global")` 控制 `_WRITE_TOOL_NAMES`（add/modify/delete）三个工具的启停；`on_load` 与 `on_config_update(scope="self")` 都会应用（WebUI 改配置即时生效）。manifest 需声明 `component.disable`/`component.enable`。只影响 LLM 工具，管理员 `/mpj` 命令与 WebUI 不受影响
- **去重 resolve 不要改回"手工改向量"**：一律走"只写源文件 → `_rebuild_notebook` → `_scan_duplicates`"（见"去重与 LLM 整理"）
- **LLM 生成走 `_direct_chat`，不要改回 `ctx.llm.generate`**；新增 `ctx.*` 调用记得同步 `_manifest.json` 能力声明（当前仅 `llm.embed` + `send.text`）
- 修改 `_direct_chat` / agent 循环时注意 **`reasoning_content` 回传** 与 **`tool_calls[].function.arguments` 是 JSON 字符串需 `json.loads`** 两个要点（见"LLM 生成统一走直连 API"）
- 改配置模型字段时，若只是加 `json_schema_extra` 元数据，**无需 bump `config_version`**（config.toml 里值不变）
- `[dedup_merge]` / `[organize_db]` / `[llm]` 配置改动需重启插件生效（`self.config` 加载时读取，无热更新）
- 改 `_manifest.json` capabilities 后必须**重载插件**才会重新注册能力令牌（否则 E_CAPABILITY_DENIED）
