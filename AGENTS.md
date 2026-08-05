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

- 5 个 LLM 工具：`add_aidraw_notes` / `read_aidraw_notes` / `modify_aidraw_note` / `delete_aidraw_note` / `aidraw_prompt_generate`（SubAgent 构建提示词，默认关）
- 7 个管理员指令：`/mpj add|search|modify|delete|refresh|rebuild|help`
- 1 个嵌入式 WebUI（aiohttp 独立端口，默认 8010，可选密码）
- WebUI 去重功能：语义扫描 → LLM 整理（可配置任务名与系统提示词）
- 1 个 WebUI 首页 HomeCard

## 近期更新（dev 分支，未发布）

- **v2.4.0：新增 `aidraw_prompt_generate` SubAgent 工具**：规划器（聊天侧）传入绘图要求，插件经 `_direct_chat` 直连 LLM 释放一个**子代理**自行检索笔记本（`search_notes`，notebook 可选手动/`all`，提示词提示优先 all），最终返回一段成品英文提示词 + 简短中文说明。**子代理的中间消息与检索结果工具返回后即丢弃，不进宿主 `_chat_history`，省上下文**。开关 `[journal].aidraw_prompt_gen_enabled`（默认关，关闭时规划器看不到该工具）、轮数 `[journal].aidraw_prompt_gen_max_iterations`（默认 4，与 WebUI organize_db 独立）、系统提示词 `[advanced].aidraw_prompt_gen_system_prompt`（空=内置默认）。实现：`core/organize_mixin.py` 的 `_run_aidraw_prompt_gen` / `_execute_search_anywhere` + `plugin.py` 的 `handle_aidraw_prompt_generate`。
- **v2.4.0：txt 批量写入 / 导入导出的重试配置迁出配置文件**：删除 `[txt_import]` / `[file_io]` 配置节，`max_retries` / `on_failure` 改为**各 WebUI 页面在任务开始前按次配置**（txt 导入页与「导入/导出」页均有表单，默认 3 / interrupt）。中断续跑时从 `import.state.json` / `resume.json` 回读启动时的设置。`_run_import_task` / `_run_export_task` / `_run_file_commit_task` 改为接收参数；`_normalize_retry_params`（`core/export_import_mixin.py`）统一钳制（0–20、interrupt/skip）。
- **v2.4.0：配置节重排**：`[advanced]`（高级）移至配置页**最底部**；`config_version` 升到 `2.4.0`；`normalize_plugin_config` 新增废弃节清理（`removed_sections`），`[txt_import]`/`[file_io]` 旧键升级时自动丢弃。
- **search_notes 工具结果附带检索机会提示**：操作数据库 / SubAgent / txt 批量写入的 agent 循环里，每次 search_notes 的返回末尾追加 `_format_iteration_hint`（`core/organize_mixin.py`），告知模型当前第几轮、还剩几轮、一轮内多次检索只算 1 次机会、信息足够可提前收尾。
- **WebUI 长程任务提示条**：`web/*.html` 中涉及长程任务（操作数据库 / txt 批量写入 / 导入导出 / 去重整理）的位置新增 `.lock-hint` 提示（`web/style.css`），提醒"期间占用写入锁、机器人对话侧 add/modify/delete 暂不可用、建议空闲时使用"；全局导航重建索引按钮 tooltip 同步提示。
- **txt 批量写入页文案统一**：`web/import.html` / `web/import_guide.md` 中与「txt 批量写入」功能相关的"导入"全部改为"写入"（写入状况 / 写入配置 / 开始写入 / 取消写入 / 等待写入等），避免与笔记本导入功能混淆；任务中心标签同步为「txt 批量写入」。`导入oc设计` 预设名保留（与操作数据库页共享）。
- **长程任务重试兜底 + 断点续跑**：embedding/LLM API 增加瞬时失败重试与超时（`core/retry.py`）；txt 批量写入、笔记本导入/导出支持条目级重试，失败后可「中断（缓存进度、再次尝试/取消）」或「跳过」，新增 `interrupted` 状态与磁盘续跑缓存（`core/resume.py`）。embed 进度**每 10s 周期落盘 + 原子写 + 上一份备份**，进程被强杀/断电时最多丢最后 10s 进度，重载后通过 `_effective_io_state` 呈现为可续跑的中断状态。详见下文「重试与断点续跑」章节。
- **WebUI 活跃任务栏收起**：`injectTaskCenter` 头部新增收起/展开按钮（`toggleTaskCenter`，`web/app.js`），任务列表可折叠；`.hidden` 需 `!important` 故不受影响。
- **导出默认格式改为 mpj**：`exportFormat` 下拉默认选中 mpj，页面加载后文件名自动填 `default.mpj`。
- **第三方 embedding 配置新增「向量维度」**：`embedding_profile.json` 新增可选 `dim` 字段（WebUI 表单 `embDim` 输入并自动预填）；后端 `_web_embedding_profile_save` 支持存取；`_export_mpj_rebuild` 导出时若配置了 `dim` 会逐条核对第三方返回的向量维度，不符即报错中止，防止导出损坏的 mpj（`core/export_import_mixin.py`）。
- **导出前自动保存第三方配置**：`doExport` 在 mpj + rebuild 模式点击「开始导出」时先静默保存当前表单配置（`saveEmbeddingProfile(true)`）再启动导出，避免用旧配置导出；direct 模式不依赖该配置不触发。
- **api_key 保留语义**：placeholder 标注「留空则保留已保存的密钥」；GET 端点不回传 api_key、前端表单恒为空，后端仅当提交值非空时才覆盖 `profile["api_key"]`。
- **导入校验相似度警告**：抽样最小相似度 < 0.95 时，`renderImportPreview` 将数值红色加粗并追加红底提示「你的 embedding 极可能和文件导出者用的不一致，请重建索引导入」（阈值 0.95 仅前端）。
- **导入/导出列位置交换**：「导入 / 导出」选项卡现为**左导出、右导入**（此前左导入右导出）。
- **导出说明文案更新**：明确 jsonl 仅为数据记录（无索引、重导需重算）、mpj 为本插件专用格式（打包索引，同 embedding 模型可直接导入），以及两种导出模式的用途。
- **直接导入按钮仅 mpj 显示**：jsonl 预览不再显示「直接导入」按钮（此前空按钮出现在 jsonl 预览中）。
- **输入框样式统一**：`input[type="number"]` 补入通用输入框样式与 focus 高亮（此前数字输入框无边框/内边距，`web/style.css`）。
- **txt 批量导入取消/状态按任务类型过滤**：`_cancel_running_task(task_type)` 支持按类型取消；txt 导入页的取消与「笔记本构建中」状态只认 `type=="import"` 任务，避免误杀重建/导出等无关任务（`core/webui_mixin.py`）。
- **拖拽上传兼容 Firefox/Safari**：`web/import.html` 的 drop 处理器在 `input.files` 只读时经 `DataTransfer` 中转赋值。
- **.gitignore 忽略 `test_files/`**（本地测试用 mpj 样本不入库）。

## 核心文件

| 文件 | 职责 |
|------|------|
| `_manifest.json` | 插件元信息 + 能力声明 + 依赖声明 |
| `plugin.py` | 入口：插件主类 `PromptJournalPlugin`（生命周期 / 迁移 / 5 工具 / 9 指令 / 辅助）+ `create_plugin()` 工厂 |
| `core/constants.py` | 模块常量：LLM 系统提示词、`_WRITE_TOOL_NAMES`、WebUI 阈值与警告页等 |
| `core/config.py` | 全部配置模型（`PromptJournalConfig` 及各节） |
| `core/notebook.py` | `Notebook` 数据模型 + `scramble_id` / `_split_txt` |
| `core/search_mixin.py` | `SearchMixin`：向量搜索、索引重建、embedding 助手、写入去重检测 |
| `core/organize_mixin.py` | `OrganizeMixin`：LLM 直连、去重整理、操作数据库、txt 批量写入、SubAgent 提示词生成的 agent 循环 |
| `core/json_utils.py` | 宽容 JSON 解析（`parse_lenient_json`：多候选提取 + strict=False/去尾逗号/非法转义/单引号修复） |
| `core/backup_mixin.py` | `BackupMixin`：笔记本自动备份（创建/列表/恢复/删除/上限淘汰，`data_dir/backups/{name}/{时间戳}.jsonl`） |
| `core/export_import_mixin.py` | `ExportImportMixin`：笔记本导出（jsonl/mpj）/ 文件导入（后台校验 + 提交 + 抗刷新暂存 `data_dir/file_import/`） |
| `core/embedding_client.py` | 第三方 OpenAI 兼容 embedding 客户端 + 配置存取（`data_dir/embedding_profile.json`） |
| `core/retry.py` | API 调用重试兜底：瞬时失败判定（`is_transient_error`）、单次调用重试（`run_with_retry`）、任务条目级重试（`run_task_item`）、embedding 超时常量 |
| `core/resume.py` | 中断任务磁盘缓存与断点续跑：`TaskInterrupted`、resume.json / partial_emb.npz 读写、`STATE_INTERRUPTED` |
| `core/mpj.py` | mpj 打包/解包 + `checksum.sha256` 校验码 |
| `core/webui_mixin.py` | `WebUIMixin`：WebUI 服务器 + 全部 API + 后台任务中心 + 去重扫描 |
| `web/index.html` | WebUI 首页（状态栏 + 搜索/浏览 + 添加） |
| `web/dedup.html` | WebUI 去重页 |
| `web/organize.html` | WebUI 操作数据库页 |
| `web/import.html` | WebUI txt 批量写入页 |
| `web/notebooks.html` | WebUI 笔记本管理页（新建空白笔记本 / 删除笔记本） |
| `web/backups.html` | WebUI 备份页（查看/恢复/删除备份） |
| `web/app.js` | WebUI 共享逻辑（api/esc/登录/loadStatus/导航注入 + 全局导航右侧的刷新/重建索引/全量重构索引按钮） |
| `web/style.css` | WebUI 共享样式 |
| `config.toml` | 运行时配置 |

**架构说明**：主类 `PromptJournalPlugin(MaiBotPlugin, WebUIMixin, OrganizeMixin, SearchMixin, BackupMixin, ExportImportMixin)` 通过 mixin 拆分业务，SDK 用 `dir(instance)` 收集组件，继承方法可正常注册。加载器以 `plugin.py` 为入口（`submodule_search_locations`），`core/` 下用**相对导入**（`from .core.config import ...`，与 maimai-drawpic-plugin 同款）。新增逻辑时：配置字段加在 `core/config.py`，通用工具/存储放 `core/notebook.py` 或新增 `core/*.py`，WebUI 处理器加进 `WebUIMixin`，agent 循环加进 `OrganizeMixin`，搜索相关加进 `SearchMixin`，备份相关加进 `BackupMixin`，导入导出相关加进 `ExportImportMixin`。

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

## 主程序规划器上下文 / 工具机制（宿主管线参考）

> 理解宿主（MaiBot-main）LLM 规划器如何运作的参考，路径均为相对 `MaiBot-main/`。本插件 WebUI 的 LLM 整理 / 操作数据库 / 批量导入**全部走自研 `_direct_chat` 直连 API，不经宿主规划器**，因此不受本条目所述 tool 协议 / 折叠 / 裁切约束；但若未来回退到宿主 `ctx.llm.generate_with_tools` 管线，需按本条目理解工具如何进入上下文，以及 `reasoning_content` 回传要求（宿主消息管线不承载 `reasoning_content`，详见下文「LLM 生成统一走直连 API」）。

### 1. 规划器是三组件协同的 ReAct agent 循环
- `src/maisaka/runtime.py`（`MaisakaHeartFlowChatting`）：会话运行时，持有 `_chat_history`（上下文列表）与工具注册表。
- `src/maisaka/reasoning_engine.py`（`MaisakaReasoningEngine`）：驱动多轮循环（`run_loop` / `_run_planner_request`）。
- `src/maisaka/chat_loop_service.py`（`MaisakaChatLoopService.chat_loop_step`）：构造 messages、真正请求 LLM。
- 内部循环上限 `_max_internal_rounds=10`（`runtime.py`）。

### 2. 上下文表示
`_chat_history` 是 `list[LLMContextMessage]`（`src/maisaka/context/messages.py`），5 个子类：
- `SessionBackedMessage`（user）— 真实聊天消息
- `ComplexSessionMessage`（user）— 合并转发的摘要版
- `ReferenceMessage`（user）— 记忆 / 黑话 / tool_hint，`count_in_context=False` 不占窗口
- `AssistantMessage`（assistant）— 可携带 `tool_calls`
- `ToolResultMessage`（tool）— 工具执行结果

经 `to_llm_message()` 转成统一 `Message`；系统提示词来自 `prompts/{locale}/maisaka_chat.prompt`。

### 3. 工具调用进入上下文只有两个写入点
1. `reasoning_engine.py` 的 `_handle_planner_response_actions`：把带 `tool_calls` 的 `response.raw_message`（`AssistantMessage`）append 进 `_chat_history`。
2. `_append_tool_execution_result`：把 `ToolResultMessage(tool_call_id=tool_call.call_id)` append 进去。

二者靠 **`tool_call_id` 严格配对**，在历史里形成 `assistant(tool_calls) → tool → tool → ...`，下一轮模型即可看到工具结果继续推理。配对被裁切破坏时由 `src/maisaka/context/history.py` 的 `normalize_tool_call_result_pairs`（`drop_orphan_tool_results` / `drop_unanswered_tool_calls` / `normalize_tool_result_order`）修复。

### 4. 工具占用上下文的上限
- `ToolResultMessage.count_in_context = False`（`messages.py`）——tool 结果**不计入**滑动窗口，是"搭便车"的；带 tool_calls 的 `AssistantMessage` 计入。
- **折叠约束（最强）**：`enable_context_optimization`（默认开）下，每轮后 `_trim_assistant_history_to_latest`（`context/post_processor.py`）只保留最近 `ASSISTANT_OPTIMIZATION_KEEP_COUNT=3` 条 assistant 消息，更早的 assistant+tool 链折叠成一条普通 user 文本（`source_kind="optimized_tool_history"`），绕开 tool 协议。
- **硬裁切**：`count_in_context` 消息数 > `max_context_size×2.0` 时裁到 `×1.0`。
- **无字符/token 截断**：进上下文的 `ToolResultMessage.content` 由 `_build_tool_result_history_content` 原样返回 `result.get_history_content()`；`reasoning_engine.py:1907` 的 2000 字符截断**只用于 monitor 终端展示摘要，不作用于上下文**。

### 5. 群聊 / 私聊上下文配置的影响
`runtime.py` 的 `_max_context_size` 按会话类型二选一：群聊 `chat.max_context_size`（默认 40）、私聊 `chat.max_private_context_size`（默认 60）。该值驱动选择窗口（`×2.0` 提升 cache 命中）、裁切阈值（`×2.0`/`×1.0`）、DB 恢复量（`×0.5`）。
**因为 tool 结果不计入窗口，调大它多放的是"带 tool_calls 的 assistant"，顺带捎上配对 tool 结果；真正卡住"活工具消息"数量的是 assistant 折叠 keep=3（全局、与群/私聊配置无关）**。

### 6. 持久化
`_chat_history`（含 assistant/tool 消息）是**纯内存，重启即清**；工具调用另落库 `ToolRecord` 表（`_record_tool_execution_effects` → `database_service.store_tool_info`）用于审计。

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
- `GET /api/dedup/scan?notebook=&threshold=`：L2 归一化 → 余弦相似度 → 贪心聚类（`_scan_duplicates`）。
- **`_scan_duplicates` 分块计算相似度**：每块 `[advanced].dedup_scan_block`（默认 `_DEDUP_SCAN_BLOCK=256`）行（`block = normalized[i0:i1] @ normalized.T`，`B×N` 用完即弃），内存峰值从一次性 `N×N` 降到 `B×N`，避免大笔记本扫描占满内存；只读右上三角 `j>i`（天然不含自身，无需 `fill_diagonal`），结果与一次性全矩阵计算完全一致（无符号差异，仅浮点舍入级误差）。N=5000 实测峰值 RSS ≈74MB。分块大小可在插件配置页 `[advanced]` 的「去重扫描分块大小」修改（一般不需要动）。**后续改动别改回一次性 N×N 矩阵**。
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

### 去重整理（开关在 `[dedup_merge]`，系统提示词在 `[advanced]`，重启生效）
`enabled`（开关）；`system_prompt` 已迁移到 `[advanced].dedup_merge_system_prompt`（空 = 内置默认）。模型参数由 `[llm]` 节统一配置。

- 调用 `_direct_chat(messages)`，`messages = [system, user]`，系统提示词放在 message list 里。
- 内置系统提示词要点：修正过度安全拒绝（绘图标签可能敏感，禁止拒绝/省略/软化）、限定输出严格 JSON（`{"reason","entries":[{en,zh,note}]}`）、明确 entries 允许 1~N 条（防止模型误以为只能输出 1 条）、禁止强行合并丢信息。
- 解析用 `core/json_utils.py` 的 `parse_lenient_json`（状态机提取所有 `{…}` 候选 + 多级修复：`strict=False` 容忍字符串内真实换行、去尾逗号、非法转义、单引号→双引号兜底；返回 `(payload, reason)`，reason 区分 `no_json`/`truncated`/`parse_failed` 用于精确报错），失败返回 502 让前端重试。**不要改回旧的 `_extract_json` 单候选实现**。

### 操作数据库（开关/轮数在 `[organize_db]`，系统提示词在 `[advanced]`，多轮会话 + 后台任务）
`enabled` / `max_iterations` / `search_limit`（在 `[organize_db]`）；`system_prompt` 已迁移到 `[advanced].organize_db_system_prompt`、`batch_import_prompt` 已迁移到 `[advanced].batch_import_prompt`（空 = 内置默认）。功能名用"操作"而非"整理"（可 create/update/delete，含导入新内容）。

- **多轮会话**：`self._organize_sessions`（内存，上限 20 按 `created_at` 淘汰，`_evict_organize_sessions`）。
- **后台任务 + 进度轮询**：`POST /api/organize_db/plan` 只做快速校验（笔记本存在/索引有效），然后 `asyncio.create_task` 跑 `_organize_db_task` 并立即返回 `{task_id}`；`GET /api/organize_db/plan_status?task_id=` 轮询进度。
  - 任务进度存在 `self._organize_tasks[task_id]`，`_run_organize_db_round` 每执行一次 search_notes 就往 `progress["searches"]` 追加 `{keyword, notebook}`（前端显示"正在检索：'x'（第 N 次）"）
  - 每次 search_notes 的返回末尾追加 `_format_iteration_hint`（当前第几轮/还剩几轮/一轮内多次检索只算 1 次机会/信息足够可提前输出），让模型感知剩余轮数预算、避免撞 `max_iterations` 上限
  - 完成 → `{"status":"done","plan":{session_id, reason, operations}}`（operations 已在任务内富化 `_old` 当前值）；失败 → `{"status":"error","error":具体错误}`；`_evict_organize_tasks` 保留 300s 且上限 50
- **请求体** `{notebook, requirement?, session_id?}`：
  - 无 `session_id` → 新建会话跑初始轮；有 → 校验会话/notebook/非空补充后追加 user 消息重跑**覆盖**上一轮
  - `session_id` 不存在/不匹配 → error"会话已过期"；补充要求为空 → error"补充要求不能为空"
- **错误透传**：`_run_organize_db_round` 返回 `(plan, messages, error)`，`_organize_db_plan` 失败返回 `{"_error":"llm","message":具体错误}`（含 `_direct_chat` 的 HTTP 详情），任务把具体错误放进 status，前端 `data.error` 直接展示（不再只显示"操作失败"）。
- **模式（学习描述方式/导入oc设计/提取动作模板/提取服饰穿搭/无附加提示词）是纯前端**：`web/organize.html` 单选 `organizeDbMode`（默认 `none`），`doOrganizeDbPlan` 按模式把常量 `ORGANIZE_DB_MODE_PROMPTS` 前置拼进 `requirement` 提交（后端无 mode 字段）；只影响首轮，补充轮不附加。输入框上方有 `updateOrganizeDbModePrompt()` 驱动的只读展示区，实时显示当前模式附加的提示词全文（`none` 显示"无"）。
- `POST /api/organize_db/apply` 成功后清除对应 `session_id` 会话。
- 前端对话框内：方案预览（每条操作独立元素 + 复选框默认全选，按类型配色：新增浅绿/修改浅蓝/删除浅红）+ [补充要求输入框 + 追加要求] + [执行已选] + [全选/全不选] + [清除]，每轮刷新只显示最新方案。`renderOrganizeDbPlan` 用 `data-idx` 记录操作索引，`doOrganizeDbApply` 只提交勾选的 operations 子集。

## aidraw_prompt_generate（SubAgent 提示词生成，聊天侧工具）

给规划器用的第 5 个 LLM 工具（`plugin.py` 的 `handle_aidraw_prompt_generate`，`@Tool("aidraw_prompt_generate")`）。规划器传入绘图要求，插件释放一个**子代理**自行检索笔记本、生成成品英文提示词 + 简短中文说明返回；子代理的中间消息与检索结果**工具返回后即丢弃，不进宿主 `_chat_history`**，比规划器自己逐条 `read_aidraw_notes` 更省上下文。

- **开关**：`[journal].aidraw_prompt_gen_enabled`（默认 **false**）。关闭时经 `_apply_tool_states`（`plugin.py`，由原 `_apply_write_tools_state` 扩展）`disable_component`，**规划器根本看不到该工具**；开启时 `enable_component`。需配置 `[llm]` 直连。
- **轮数**：`[journal].aidraw_prompt_gen_max_iterations`（默认 4，ge=1 le=30），**与 WebUI organize_db 的 `max_iterations` 完全独立**。
- **系统提示词**：`[advanced].aidraw_prompt_gen_system_prompt`（空=内置默认 `_AIDRAW_PROMPT_GEN_DEFAULT_SYSTEM_PROMPT`，`core/constants.py`），强调优先 `search_notes(notebook="all")` 多角度检索、输出「提示词：… / 说明：…」格式、不臆造、客观处理敏感主题。
- **子代理循环**：`_run_aidraw_prompt_gen`（`core/organize_mixin.py`）镜像 `_run_organize_db_round` —— `for i in range(max_iterations)` 调 `_direct_chat(messages, tools=[_ORGANIZE_DB_SEARCH_TOOL])`，tool_calls 回传含 `reasoning_content` + `function.arguments` 还原为 JSON 字符串；无 tool_calls 时把最终 `content` **按纯文本返回**（非 JSON），仅剥首尾代码围栏（`_strip_code_fence`）；空内容/撞轮数上限返回错误。
- **检索调度**：`_execute_search_anywhere(keyword, notebook, limit)` —— notebook 为空或 `all` → `_search_all_notebooks`；否则单笔记本 `_search_single_notebook`。**每次调用独立持 `self._lock`**（对齐 `handle_read_notes` 粒度），`_direct_chat` 不持锁，避免子代理多轮 LLM 调用长时间阻塞其他会话。
- **返回值**：`{"name": "aidraw_prompt_generate", "content": "提示词：…\n说明：…"}`；失败返回简短错误 + "可改用 read_aidraw_notes 自行检索"。**只读**，不入 `_WRITE_TOOL_NAMES`（不受 `allow_write` 控制），不触发备份。

## txt 批量写入（WebUI 功能）

把一份 txt 按段落批量交给 LLM 处理，写入临时笔记本，完成后可查看/编辑/处置。

- **切分**：`_split_txt`（模块级函数）按两个及以上连续换行（`\n{2,}`）切分，段首尾 strip，忽略空段，单段也能导入。
- **临时笔记本**：固定名 `tmp`，文件在 `data_dir/tmp_import/`（`tmp.jsonl` / `tmp.cache.jsonl` / `tmp.embeddings.npy` / `tmp.index.meta` / `import.log`），用 `Notebook("tmp", data_dir, custom_dir=tmp_import_dir)` 构造，**不参与** `_discover_notebooks`（但 `_get_notebook("tmp")` 返回它，供 `/api/modify`、`/api/delete` 编辑临时条目）。一轮完成后**不清理**；下一轮导入开始前 `_reset_tmp_import()` 清空。
- **一段一完整循环**：`_run_import_segment` 对每段独立跑 agent 循环（多轮 search_notes），搜索范围 = 用户选择的引用笔记本 + 临时笔记本（`_execute_search_notes_multi`）；系统提示词 = `advanced.organize_db_system_prompt` + `\n` + `advanced.batch_import_prompt`（`{temp-journal}` 替换为 `tmp`，约束 LLM 只能改临时笔记本）；输出完整 create/update/delete，`_apply_ops_to_tmp` 应用后 `_rebuild_notebook(tmp)` 增量重建，下一段可见。每轮 search_notes 返回末尾同样追加 `_format_iteration_hint`。
- **模式（附加提示词）是纯前端**：`web/import.html` 五模式单选（学习描述方式/导入oc设计/提取动作模板/提取服饰穿搭/自定义），预设直接发对应 `IMPORT_MODE_PROMPTS` 文本，自定义弹窗输入；`POST /api/import/start` 的 `mode_prompt` 字段后端仅校验非空（双重校验）。
- **失败处理**：某段 LLM 调用/解析/写入失败 → 按 `max_retries` 重试；仍失败按 `on_failure`：`skip`=跳过该段记录到 result.failed 继续下一段；`interrupt`=缓存 `tmp_import/import.state.json` 并置任务为中断（导入页「再次尝试/取消」，跨插件重载可恢复）。导入完成后把失败汇总追加到 `import.log` 末尾。**`max_retries` / `on_failure` 在导入页「失败处理」表单按次配置**（默认 3 / interrupt，已从 `[txt_import]` 配置节迁出），续跑时从 state 回读启动时的设置。
- **日志**：`import.log` 记录每段时间、用户输入、附加提示词、LLM 决定与理由（reason + operations）、成功/失败。
- **处置**（`POST /api/import/resolve`）：`merge` 合并入已有笔记本（复用 tmp 向量直接追加）、`create` 新建笔记本（复制 tmp 四文件到 `imports/{new_name}.jsonl`，`_discover_notebooks` 自动发现）、`discard` 丢弃（仅清空状态，文件留给下一轮清理）。
- **API**：`POST /api/import/preview`（切分预览）/ `POST /api/import/start` / `GET /api/import/status` / `GET /api/import/tmp_notes` / `GET /api/import/log` / `POST /api/import/resolve`。
- 导入走通用任务中心 `_start_task("import", ...)`，与 rebuild 互斥（进行中拒绝新任务，409）；进度在导入页 + 顶部任务栏同步显示。

## 笔记本上传 / 下载（`web/notebooks.html` 「导入 / 导出」选项卡）

> 导入与导出共用一套**统一传输状态机**（持久化在 `data_dir/file_io/`，页面刷新不丢）：
> `kind` = import | export；`state` = none / validating / ready / importing / building / done / error。
> 由于后台任务走 `_start_task`（占用即拒绝新任务），同一时刻只会有一个导入或导出任务，一个状态槽即可。
> 前端 `web/notebooks.html` 在「导入 / 导出」选项卡下用**一个统一状态元素**渲染（左侧导出、右侧导入两列，移动端上下堆叠）。

### 导出（一律后台任务，产物写入 `file_io/artifact/`）
- 前端 `exportFormat` **默认 mpj**（jsonl 需手动切换）。
- `POST /api/export/start` `{notebook, format, mode, filename}` → `kind=export, state=building` → 后台任务。
- jsonl：仅为数据记录、无索引，直接复制源文件重命名到 `artifact/`（重新导入需重算索引）；mpj 为本插件专用格式，除数据外打包索引（`mode=direct` 打包当前索引 / `mode=rebuild` 用第三方 embedding 重新生成索引后打包，含校验码）写入 `artifact/`。
- `mode=rebuild`：第三方配置 `data_dir/embedding_profile.json`（base_url/api_key/model/timeout/**concurrent**/**dim**，WebUI 表单可保存复用；api_key 留空沿用已保存值）；embed 为**信号量并发逐条**（`Semaphore(profile.concurrent)`，默认 4）+ 逐条写进度；**若配置了 `dim`，导出时逐条核对第三方返回的向量维度**，不符即报错中止。
- **导出前自动保存配置**：前端 `doExport` 在 mpj+rebuild 模式先静默保存当前表单配置（`saveEmbeddingProfile(true)`）再启动导出，避免用旧配置导出（direct 模式不触发）。
- 完成 → `state=done`，`result.json` 存 `{filename, size, ctype}`；`GET /api/export/download` 下载产物。

### 导入（后台任务 + 抗刷新）
- 上传：`web/notebooks.html` 导入列用**点击/拖拽虚线框**（`.jsonl`/`.mpj`，`handleImportDrop`/`importFileSelected`，注意 drop handler 内不要用 `this`——普通函数调用时 `this` 非元素）。
- `POST /api/import/file`（multipart：`file` + `sample` 抽样数）→ `_reset_io()` → `kind=import, state=validating` → 起后台校验任务。
- 校验：jsonl 解析（`_parse_import_jsonl`：缺 id 补 `scramble_id`、缺 ts 补当前时间、坏行计入 skipped）；mpj 解包 → 校验码 → 维度（内置 embedding 探针）→ 条目数=向量数 → 维度一致时抽样（默认 25，前端可指定；**填 0 表示不校验相似度**，`meta.sample.skipped=true`，预览显示"未校验"）用内置 embedding 重算余弦 → 平均/最小相似度。结果写 `preview.jsonl` + `preview.json`，`state=ready`。
- **校验码**：`checksum.sha256` 缺失或对不上都视为"可能被第三方修改"，前端显示警告并要求勾选"我已了解风险"才能提交（同一警告文案）。
- **抽样相似度警告**：`renderImportPreview` 中抽样**最小相似度 < 0.95** 时数值红色加粗，并追加红底提示「你的 embedding 极可能和文件导出者用的不一致，请重建索引导入」（阈值仅前端）。
- 预览：新笔记本名称**默认取上传文件名**（去扩展名，输入框在预览面板）；目标（新建/合并）+ 按钮 **直接导入（仅 mpj 且维度/数量一致时显示）/ 重建索引导入 / 清除**（校验码异常需勾选"我已了解风险"才可点导入）。
- 提交：jsonl / mpj-rebuild → 内置 embedding 全量建索引 → **新建或合并**（合并时重生成 id 防冲突，前端提醒去重）；mpj-direct → 保留 mpj 自带索引（仅新建，需维度一致 + 条目数=向量数）。条目 embed 失败按 `max_retries` 重试，仍失败按 `on_failure`：`skip`=跳过失败条目导入成功子集；`interrupt`=缓存 `file_io/resume.json` + `partial_emb.npz` 并置传输状态为中断（「再次尝试」从断点续算，不重复 embed 已完成条目）。**`max_retries` / `on_failure` 在「导入/导出」页「失败处理」表单按次配置**（默认 3 / interrupt，已从 `[file_io]` 配置节迁出），续跑时从 `resume.json` 回读启动时的设置。
- **进度与取消**：`file_io/progress.json` 由 `_write_io_progress` 写入（phase/done/total；导入用 `_embed_with_progress` 信号量并发逐条 embed，并发取 `config.journal.embed_max_concurrent`；mpj 校验抽样逐条）；前端状态元素显示 `(xx/xx)` + **取消按钮**（`POST /api/transfer/cancel` → `_cancel_running_task()` + `_reset_io()`）；中断状态可用 `POST /api/transfer/resume` 续跑。
- 端点：`GET /api/transfer/state`（统一状态+进度，抗刷新）/ `POST /api/transfer/clear` / `POST /api/transfer/cancel` / `POST /api/transfer/resume` / `GET /api/import/file_preview?page=&size=` / `POST /api/import/file_commit`。
- `client_max_size` 已放宽到 256MB（支持大 mpj）。

### 关键约束
- 新增导出/导入逻辑放 `ExportImportMixin`；mpj 打包校验在 `core/mpj.py`；第三方 embedding 在 `core/embedding_client.py`（`EmbeddingClient.embed` 直连 OpenAI 兼容 `/embeddings`，profile 含 `concurrent` 与可选 `dim`）。
- **校验码不是加密签名**：能发现内容被改动，但防不住懂行的人重算校验码重打包。
- 导入会修改/新建笔记本：新建不触发备份，**合并会触发备份**（`_import_jsonl_entries` 里已调 `_create_backup`）。
- 备份功能已并入 `web/notebooks.html`「笔记本管理」选项卡（独立 `web/backups.html` 已删除，`/api/backups/*` 接口保留）。
- 传输状态机细节：`_reset_io()` 清空 `file_io/`（含 preview/artifact/progress）；任务互斥走 `_start_task`（占用即 409）。

## 配置节布局与版本迁移（v2.4.0）

**当前配置节顺序**（`core/config.py` 的 `__ui_order__`，`[advanced]` 始终在最底部）：

| 顺序 | 节 | 内容 |
|------|-----|------|
| 0 | `[plugin]` | 开关 / `config_version`（当前 `2.4.0`） |
| 1 | `[journal]` | 搜索条数 / 阈值 / embed 并发 / allow_write / 写入去重检测 / SubAgent 提示词开关与轮数 |
| 2 | `[admin]` | 管理员 QQ |
| 3 | `[web]` | WebUI 开关 / 端口 / 密码 / bind |
| 4 | `[llm]` | LLM 直连（base_url / api_key / model / ...） |
| 5 | `[dedup_merge]` | 去重整理开关（系统提示词已迁到 `[advanced]`） |
| 6 | `[organize_db]` | 操作数据库开关 / 轮数 / 条数 |
| 7 | `[backup]` | 备份：`enabled`（默认 true）/ `max_per_notebook`（默认 6） |
| 8 | `[advanced]` | **高级**（最后）：`dedup_merge_system_prompt` / `organize_db_system_prompt` / `batch_import_prompt` / `aidraw_prompt_gen_system_prompt` / `dedup_scan_block`（默认不建议改） |

> 注：`[txt_import]` / `[file_io]` 配置节已在本版**删除**，重试配置迁移到各 WebUI 页面按任务配置（见「重试与断点续跑」）。

**配置迁移机制（重要，勿破坏）**：
- host 在 `config_version` **升高**时，以最新默认配置为骨架重建并写回文件（runner_main `_prepare_plugin_config_for_version_update`）；键路径不变的旧值自动保留，**被删除的键值会在进入钩子前被丢弃**。
- 插件主类实现了 `normalize_plugin_config`（`plugin.py`），负责"跨节搬字段 + 删旧键"，当前迁移对：
  `dedup_merge.system_prompt → advanced.dedup_merge_system_prompt`、`organize_db.system_prompt → advanced.organize_db_system_prompt`、`organize_db.batch_import_prompt → advanced.batch_import_prompt`、`journal.dedup_scan_block → advanced.dedup_scan_block`。
- **废弃整节清理**：`normalize_plugin_config` 的 `removed_sections = ["txt_import", "file_io"]` 会在进入钩子时（以及基类重建后）把这两节从配置里删除并置 `changed=True`。
- **两阶段迁移惯例**：搬字段时，旧字段先在模型里保留并标 `json_schema_extra={"hidden": True}`（WebUI 自动隐藏），配合钩子把旧值搬进新节并删除旧键；下一版本再删掉废弃 hidden 字段并再次 bump `config_version`。
- **规则**：只加字段 → 可 bump 可不 bump；删字段 / 搬字段 / 改键路径 → **必须 bump `config_version`**，否则旧值会在重建时被静默丢弃。
- 部署升级验证：插件重载后检查 `config.toml` 是否生成 `[advanced]`、旧节字段是否清理、`[txt_import]`/`[file_io]` 是否移除、版本是否自动升到 `2.4.0`。

## 命令规范

- 所有 `/mpj *` 指令**必须管理员校验**：`self._is_admin(user_id)` 检查 `config.admin.users` 列表
- **非管理员静默无视**：`return True, "", False`（不发消息、不报错）
- 笔记本名称统一用 `-n xxx` 后缀语法，`_parse_notebook_flag()` 解析
- `search` 支持 `-n all` 跨笔记本搜索
- `/mpj backup list/restore/delete`：`backup` 子命令用命名组 `(?P<rest>.+)`（restore/delete 的时间戳在 rest 里，`_parse_notebook_flag` 再剥 `-n`）

**备份触发约定（重要）**：`[backup]` 自动备份要求在**每个成功写入源文件后的路径**调用 `self._create_backup(nb)`。当前已覆盖：工具 add/modify/delete ×3、`/mpj add/modify/delete/confirm` ×5、WebUI `_web_add/modify/delete`、`_web_dedup_resolve`、`_web_organize_db_apply`、`_web_import_resolve`（merge）——**新增写入路径必须补上**，否则该路径不会自动备份。`tmp` 临时笔记本与 `_web_create_notebook`（新建空）不备份。

### 坑：命令正则必须用**命名捕获组**
宿主 `find_command_by_text`（`src/plugin_runtime/host/component_registry.py`）匹配命令后只回传 `m.groupdict()`（命名捕获组 dict）。**位置捕获组 `(.+)` 的 group 1 取不到**，`matched_groups.get(1)` 恒为空 → 命令永远走"用法"提示分支。
- 正确写法：`pattern=r"^/mpj\s+new\s+(?P<name>.+)$"`，handler 里 `matched_groups.get("name")`。
- 参考：内置插件管理命令用 `matched_groups.get("manage_command")`；本插件 `/mpj rebuild` 的 `(?P<full>--full)` 同理。
- 新增带参数命令时，务必用命名组，并同步在此登记 group 名。

## 重试与断点续跑（长程任务健壮性）

所有依赖 embedding / LLM API 的长程 WebUI 任务（重建索引、去重整理、操作数据库、
txt 批量写入、笔记本导入/导出）共两层兜底，**改动时勿破坏分层**：

### 第 1 层：API 调用级重试 + 超时（`core/retry.py`）
- `run_with_retry`：单次 API 调用最多 `_API_RETRIES=3` 次尝试，指数退避
  （1s/2s/4s），**只对瞬时失败重试**（`is_transient_error`：超时/网络/429/5xx 等，
  5xx 需带上下文如 `(503)`/`status=503`，避免误匹配向量维度数字）；业务错误不重试。
- `run_task_item`：任务"条目级"重试，**不论是否瞬时**都重试 max_retries 次
  （对应各 WebUI 页面按次配置的 `max_retries`，0 表示只尝试一次）。
- 接线位置：
  - `_embed_single`（`search_mixin.py`）：补 `_API_EMBED_TIMEOUT=60s` 超时
    （现状宿主 `ctx.llm.embed` 无超时，宿主卡住会永久挂起）+ 瞬时重试。
  - `_embed_batch`：`scaled_batch_timeout`（按条数/并发缩放，避免大批次被误杀）+ 瞬时重试。
  - `_direct_chat`（`organize_mixin.py`）：瞬时重试（幂等单轮，安全）。
  - `EmbeddingClient.embed`（`embedding_client.py`）：瞬时重试。

### 第 2 层：任务级「中断 / 跳过」+ 断点续跑（`core/resume.py`）
- 配置：`max_retries`（默认 3）与 `on_failure`（`interrupt`/`skip`，默认 `interrupt`）
  已从配置文件迁出，改由**各 WebUI 页面在任务开始前按次配置**（txt 批量写入页 /
  「导入/导出」页的「失败处理」表单）；任务启动时经 `_normalize_retry_params`
  （`core/export_import_mixin.py`）钳制（0–20 / interrupt|skip）。
- 条目仍失败后：
  - `interrupt`：保存续跑上下文到**磁盘**，任务置 `interrupted`；用户可选「再次尝试」
    或「取消」。**磁盘缓存可跨插件重载恢复**。
  - `skip`：跳过失败条目/段并记录，任务继续（导出 mpj 时失败条目会被丢弃，
    只导出成功子集，日志会提示丢弃数量）。
- **中断缓存位置**：
  - txt 批量写入：`tmp_import/import.state.json`（segments + mode_prompt + ref_names +
    current_index + segment_status + failed + **max_retries/on_failure**，续跑沿用启动时的设置）。
  - 导入/导出：`file_io/resume.json`（任务上下文 + **max_retries/on_failure**）+
    `file_io/partial_emb.npz`（已完成条目的索引与向量）。「再次尝试」读缓存续算，
    **不重复 embed 已完成的条目**。
- **周期落盘 + 崩溃容错（重要）**：
  - embed 循环（`_embed_with_progress` / `_export_mpj_rebuild`）用
    `_run_with_periodic_flush` 每 `_PROGRESS_FLUSH_INTERVAL=10s` 把已完成进度写盘一次，
    进程被强杀/断电时**最多丢最后 10s** 的进度（`core/retry.py` 无此项，见 `core/resume.py`）。
  - `save_embed_progress` 是**原子写 + 上一份备份**：先把旧快照旋转为 `.bak`，新快照写
    `.tmp` → fsync → 原子改名；`load_embed_progress` 主快照解析失败时回退 `.bak`。
    **不要改回直接写目标文件**（中途崩溃会留撕裂 npz）。
  - `resume.json` **任务启动即写**（`_write_resume_context`），成功完成删除
    （`_clear_resume_context`）——即使强杀，磁盘上也有任务参数可续跑。
  - **强杀残留识别**：`_effective_io_state()` 把"存储状态是 importing/building 但
    resume.json 存在且无运行任务"的崩溃残留呈现为 `interrupted`（`_web_transfer_state`
    与 `_web_transfer_resume` 使用），前端无需改动即可提供再次尝试/取消。
- **状态机**：`self._tasks` 状态新增 `interrupted`（`STATE_INTERRUPTED`），
  被中断任务**不随 `_evict_tasks` 淘汰**（直到再次尝试或取消）；传输状态机
  `file_io/state` 新增 `interrupted`。
- 端点：`POST /api/task/resume` / `POST /api/task/cancel`（txt 导入，task_id 定位）、
  `POST /api/transfer/resume`（导入/导出，读 resume.json）。
- 恢复入口：`plugin.on_load` 调 `_restore_interrupted_tasks()`，从 `import.state.json`
  重建中断的 txt 导入任务（`_web_import_state`/`_web_import_status` 据此展示）。
- 前端：`web/app.js` 任务栏、`web/import.html`、`web/notebooks.html` 均渲染
  `interrupted`（再次尝试/取消按钮）。

### 关键约束
- **导出 `skip` 会丢数据**：mpj 重建导出跳过失败条目 = 同时从 jsonl 与向量剔除
  （保持 jsonl/向量数量一致），结果文件比源笔记本少条目，前端/日志需提示。
- **中断缓存是磁盘真相**：txt 导入的 state.json 与导入导出的 resume.json/partial_emb
  是唯一可续跑依据；`_reset_tmp_import()` / `_reset_io()` 会清掉它们（= 取消）。
- **存在中断任务时 `_start_task` 拒绝新任务**（返回 None → 409），要求先「再次尝试/取消」，
  避免缓存互相覆盖与任务堆积。
- 去重整理 / 操作数据库 / 重建索引**没有**第 2 层（失败直接报错、前端重试），
  只受第 1 层 API 兜底保护。

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
3. 用 ruff 查未定义名（`/root/mai/MaiBot-main/.venv/bin/ruff check --select F821 plugin.py core/*.py`）——**拆分/新增模块后必跑**，防止漏 import（曾因缺 hashlib/time/_split_txt 导致重建索引、txt 批量写入、update_md5 运行时 NameError）
4. 若改 `_compute_text_boost`，用独立脚本跑加分规则用例
5. 若改 WebUI，用脚本检查 HTML div 配对、关键元素存在
6. 若改配置模型，用 uv 虚拟环境（`MaiBot-main/.venv/bin/python`）跑 `generate_plugin_config_schema` 实测 label/hint/ui_type（见上文"验证 Schema"）

## 维护提示

- 修改 ID 生成逻辑会影响历史数据（`scramble_id` 是双射，ID 不可随意改格式）
- 修改 JSONL 格式要同步迁移逻辑（`_migrate_legacy` 负责旧 `notes.jsonl` → `default.jsonl`）
- 新增笔记本操作时，检查 `rewrite_all` / `append_entries` / `update_md5` 是否都调用了
- 修改搜索、去重、重建逻辑时注意 `self._lock` 并发保护
- **笔记 tag 组合数量（3~10）出现在三处，改动必须同步**：`add_aidraw_notes` 工具描述（`plugin.py`）、`web/organize.html` 与 `web/import.html` 的 `learn_style` 预设
- **`_scan_duplicates` 必须保持分块计算**（`_DEDUP_SCAN_BLOCK`，B×N 用完即弃），不要改回一次性 N×N 矩阵——大笔记本会 OOM
- **`[journal] allow_write` / `aidraw_prompt_gen_enabled`**：`_apply_tool_states()`（`plugin.py`，由原 `_apply_write_tools_state` 扩展）用 `ctx.component.disable/enable_component(name, "tool", scope="global")` 控制 `_WRITE_TOOL_NAMES`（add/modify/delete）与 `aidraw_prompt_generate` 的启停；`on_load` 与 `on_config_update(scope="self")` 都会应用（WebUI 改配置即时生效）。manifest 需声明 `component.disable`/`component.enable`。只影响 LLM 工具，管理员 `/mpj` 命令与 WebUI 不受影响
- **去重 resolve 不要改回"手工改向量"**：一律走"只写源文件 → `_rebuild_notebook` → `_scan_duplicates`"（见"去重与 LLM 整理"）
- **LLM 生成走 `_direct_chat`，不要改回 `ctx.llm.generate`**；新增 `ctx.*` 调用记得同步 `_manifest.json` 能力声明（当前仅 `llm.embed` + `send.text`）
- 修改 `_direct_chat` / agent 循环时注意 **`reasoning_content` 回传** 与 **`tool_calls[].function.arguments` 是 JSON 字符串需 `json.loads`** 两个要点（见"LLM 生成统一走直连 API"）
- 改配置模型字段时，若只是加 `json_schema_extra` 元数据，**无需 bump `config_version`**（config.toml 里值不变）
- `[dedup_merge]` / `[organize_db]` / `[advanced]` / `[llm]` 配置改动需重启插件生效（`self.config` 加载时读取，无热更新）；`[journal]` 的 `allow_write` / `aidraw_prompt_gen_enabled` 经 `_apply_tool_states` 在 `on_config_update(scope="self")` 即时生效，其余 `[journal]` 字段仍重启生效
- 改 `_manifest.json` capabilities 后必须**重载插件**才会重新注册能力令牌（否则 E_CAPABILITY_DENIED）
- **API 重试分层勿破坏**：第 1 层 `run_with_retry`（单次调用、仅瞬时失败）与第 2 层 `run_task_item`（条目级、任意失败都重试）语义不同；5xx 判定需带上下文（`(503)`/`status=503`），**不要改回裸 `\b5\d\d\b`**（会误匹配向量维度数字）
- **中断续跑缓存是磁盘真相**：`tmp_import/import.state.json`、`file_io/resume.json` + `file_io/partial_emb.npz`（及 `.bak`）是"再次尝试"的唯一依据；`_reset_tmp_import()` / `_reset_io()` 会清掉它们（= 取消）。改动重置逻辑时勿漏
- **`save_embed_progress` 的原子写+备份勿回退**：快照写 `.tmp`→fsync→改名，旧快照旋转为 `.bak`；加载主快照失败回退 `.bak`。别直接写目标文件（强杀会留撕裂 npz）
- **周期落盘间隔 `_PROGRESS_FLUSH_INTERVAL`**（10s，`core/resume.py`）：改小更抗崩溃但增加大笔记本 IO 开销；改大则强杀丢失的进度更多
- 新增会中断的长程任务时：置 `interrupted` 状态 + 写续跑缓存 + `_restore_interrupted_tasks` 恢复 + 前端渲染，四者缺一不可
