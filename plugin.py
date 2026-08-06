"""麦麦的绘图笔记本 — AI 绘画提示词经验记录与语义检索。

入口模块：声明插件主类（生命周期 / 工具 / 指令 / 配置迁移），
业务逻辑拆分在 core/ 包（配置模型、笔记本存储、WebUI、LLM agent、搜索）。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    HomeCard,
    MaiBotPlugin,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .core.backup_mixin import BackupMixin
from .core.config import PromptJournalConfig
from .core.constants import _AIDRAW_PROMPT_GEN_TOOL_NAME, _WRITE_TOOL_NAMES
from .core.export_import_mixin import ExportImportMixin
from .core.notebook import Notebook, scramble_id
from .core.organize_mixin import OrganizeMixin
from .core.search_mixin import SearchMixin
from .core.webui_mixin import WebUIMixin

class PromptJournalPlugin(MaiBotPlugin, WebUIMixin, OrganizeMixin, SearchMixin, BackupMixin, ExportImportMixin):
    """麦麦的绘图笔记本插件。"""

    config_model = PromptJournalConfig

    def normalize_plugin_config(self, config_data: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """配置归一化：把旧版字段值迁移到『高级』配置节，并清理已废弃的配置节。

        迁移对（旧节.旧字段 → 高级节.新字段）：
          dedup_merge.system_prompt        → advanced.dedup_merge_system_prompt
          organize_db.system_prompt        → advanced.organize_db_system_prompt
          organize_db.batch_import_prompt  → advanced.batch_import_prompt
          journal.dedup_scan_block         → advanced.dedup_scan_block

        v2.4.0 起废弃整节（重试配置已迁移到各 WebUI 页面，按任务配置）：
          [txt_import]  /  [file_io]

        仅当新字段仍为默认值时迁移旧值（避免覆盖用户已在高级节设置的值），
        随后删除旧键，再交给基类做默认补齐与校验。
        """
        config = dict(config_data) if isinstance(config_data, Mapping) else {}
        changed = False

        migration_pairs = [
            ("dedup_merge", "system_prompt", "dedup_merge_system_prompt"),
            ("organize_db", "system_prompt", "organize_db_system_prompt"),
            ("organize_db", "batch_import_prompt", "batch_import_prompt"),
            ("journal", "dedup_scan_block", "dedup_scan_block"),
        ]
        removed_sections = ["txt_import", "file_io"]

        if config:
            default_advanced: dict[str, Any] = {}
            try:
                defaults = type(self).build_default_config()
                default_advanced = defaults.get("advanced", {}) if isinstance(defaults, Mapping) else {}
            except Exception:
                default_advanced = {}

            advanced = config.get("advanced")
            if not isinstance(advanced, dict):
                advanced = None

            for src_section, src_key, dst_key in migration_pairs:
                src = config.get(src_section)
                if not isinstance(src, dict) or src_key not in src:
                    continue
                old_value = src.pop(src_key)
                changed = True
                if old_value is None or isinstance(old_value, (dict, list)):
                    continue
                if advanced is None:
                    advanced = config["advanced"] = {}
                # 新字段仍是默认值时才覆盖，避免覆盖用户已手动设置的值
                if dst_key not in advanced or advanced.get(dst_key) == default_advanced.get(dst_key):
                    advanced[dst_key] = old_value

            # 删除已废弃的整节（重试配置已迁移到各 WebUI 页面）
            for section in removed_sections:
                if section in config:
                    del config[section]
                    changed = True

        normalized, norm_changed = super().normalize_plugin_config(config)

        # 基类 merge 会把废弃旧字段补回默认值，这里再次移除，保证写回文件时不含废弃键
        for src_section, src_key, _dst_key in migration_pairs:
            src = normalized.get(src_section)
            if isinstance(src, dict) and src_key in src:
                del src[src_key]
                changed = True

        # 基类重建时可能把废弃节补回，这里再次移除
        for section in removed_sections:
            if section in normalized:
                del normalized[section]
                changed = True

        return normalized, changed or norm_changed

    async def on_load(self) -> None:
        self._data_dir: Path = self.ctx.paths.data_dir
        self._imports_dir: Path = self._data_dir / "imports"
        self._lock: asyncio.Lock = asyncio.Lock()
        self._notebooks: dict[str, Notebook] = {}
        self._web_task: asyncio.Task | None = None
        self._web_runner: Any = None
        self._organize_sessions: dict[str, dict[str, Any]] = {}
        self._organize_tasks: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        # /mpj add/modify 检测到重复时待用户确认的写入操作（内存态，重启即失效）
        self._pending_confirms: dict[str, dict[str, Any]] = {}

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._imports_dir.mkdir(parents=True, exist_ok=True)

        # 批量导入临时笔记本（固定名 tmp，独立目录，不参与发现逻辑）
        self._tmp_import_dir: Path = self._data_dir / "tmp_import"
        self._tmp_import_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_nb: Notebook = Notebook("tmp", self._data_dir, custom_dir=self._tmp_import_dir)
        self._tmp_log_path: Path = self._tmp_import_dir / "import.log"
        self._tmp_failed_path: Path = self._tmp_import_dir / "import.failed.json"
        self._tmp_finished_path: Path = self._tmp_import_dir / ".finished"

        # 迁移旧格式
        self._migrate_legacy()

        # 发现笔记本
        self._notebooks = self._discover_notebooks()

        # 笔记本启用/禁用状态（持久化在数据目录的 disabled_notebooks.json）
        self._disabled_path: Path = self._data_dir / "disabled_notebooks.json"
        self._disabled_notebooks: set[str] = self._load_disabled_notebooks()

        # 从磁盘恢复被中断的长程任务（断点续跑）
        self._restore_interrupted_tasks()

        notebook_names = ", ".join(sorted(self._notebooks.keys())) or "(无)"
        self.ctx.logger.info(f"麦麦的绘图笔记本已加载，发现笔记本: {notebook_names}")

        # 启动 WebUI
        if self.config.web.enabled:
            self._web_task = asyncio.create_task(self._run_web_server())

        # 按配置应用 LLM 工具开关（allow_write / aidraw_prompt_gen_enabled）
        await self._apply_tool_states()

    async def on_unload(self) -> None:
        if self._web_task is not None:
            self._web_task.cancel()
            try:
                await self._web_task
            except asyncio.CancelledError:
                pass
            self._web_task = None

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        # 自身配置热更新：同步写入工具开关
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self._apply_tool_states()

    async def _apply_tool_states(self) -> None:
        """按配置启用/禁用 LLM 工具。

        allow_write 控制 add/modify/delete 三个写入工具；
        aidraw_prompt_gen_enabled 控制 aidraw_prompt_generate 子代理工具（关闭时规划器看不到该工具）。
        """
        allow_write = bool(getattr(self.config.journal, "allow_write", True))
        for name in _WRITE_TOOL_NAMES:
            try:
                if allow_write:
                    await self.ctx.component.enable_component(name, "tool", scope="global")
                else:
                    await self.ctx.component.disable_component(name, "tool", scope="global")
            except Exception as exc:
                self.ctx.logger.warning(f"{'启用' if allow_write else '禁用'}工具 {name} 失败: {exc}")

        prompt_gen_enabled = bool(getattr(self.config.journal, "aidraw_prompt_gen_enabled", False))
        try:
            if prompt_gen_enabled:
                await self.ctx.component.enable_component(_AIDRAW_PROMPT_GEN_TOOL_NAME, "tool", scope="global")
            else:
                await self.ctx.component.disable_component(_AIDRAW_PROMPT_GEN_TOOL_NAME, "tool", scope="global")
        except Exception as exc:
            self.ctx.logger.warning(f"{'启用' if prompt_gen_enabled else '禁用'}工具 {_AIDRAW_PROMPT_GEN_TOOL_NAME} 失败: {exc}")

    def _migrate_legacy(self) -> None:
        """将旧版 notes.jsonl 迁移为 default.jsonl 并补生成 note_id。"""
        old_notes = self._data_dir / "notes.jsonl"
        new_notes = self._data_dir / "default.jsonl"
        if not old_notes.exists() or new_notes.exists():
            return

        # 读取旧条目，补生成 id
        entries: list[dict[str, Any]] = []
        base_ts_ms = int(time.time() * 1000)
        with old_notes.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if not obj.get("id"):
                    obj["id"] = scramble_id(base_ts_ms + i)
                if "ts" not in obj:
                    obj["ts"] = 0.0
                entries.append(obj)

        # 写入新文件
        with new_notes.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 迁移配套文件
        for old_name, new_name in [
            ("notes.cache.jsonl", "default.cache.jsonl"),
            ("embeddings.npy", "default.embeddings.npy"),
            ("index.meta", "default.index.meta"),
        ]:
            old_path = self._data_dir / old_name
            new_path = self._data_dir / new_name
            if old_path.exists() and not new_path.exists():
                old_path.rename(new_path)

        old_notes.unlink()
        self.ctx.logger.info(f"迁移完成: notes.jsonl -> default.jsonl ({len(entries)} 条)")

    def _discover_notebooks(self) -> dict[str, Notebook]:
        """扫描文件系统，发现所有笔记本。"""
        notebooks: dict[str, Notebook] = {}

        # default 始终注册
        notebooks["default"] = Notebook("default", self._data_dir)

        # imports/ 下的第三方笔记本
        if self._imports_dir.exists():
            for path in sorted(self._imports_dir.iterdir()):
                if not path.is_file():
                    continue
                if not path.name.endswith(".jsonl"):
                    continue
                if path.name.endswith(".cache.jsonl"):
                    continue
                name = path.stem
                if name and name != "default":
                    notebooks[name] = Notebook(name, self._data_dir)

        return notebooks

    def _get_notebook(self, name: str) -> Notebook | None:
        """按名称获取笔记本，不存在返回 None。tmp 为批量导入临时笔记本。"""
        clean = str(name or "").strip() or "default"
        if clean == "tmp":
            return self._tmp_nb
        return self._notebooks.get(clean)

    def _list_notebook_names(self) -> str:
        return ", ".join(sorted(self._notebooks.keys()))

    def _load_disabled_notebooks(self) -> set[str]:
        """从数据目录加载被禁用的笔记本名集合。

        容错：文件缺失/损坏/字段非 list 返回空集。
        防御性剔除：default、tmp、以及当前已不存在的笔记本名（笔记本被删后自清理残留）。
        """
        if not self._disabled_path.exists():
            return set()
        try:
            with self._disabled_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return set()
        raw = data.get("disabled") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return set()
        names = {str(n).strip() for n in raw if isinstance(n, str) and str(n).strip()}
        names.discard("default")
        names.discard("tmp")
        return {n for n in names if n in self._notebooks}

    def _save_disabled_notebooks(self) -> None:
        """原子写入禁用笔记本集合到磁盘（.tmp → 改名，对齐项目原子写惯例）。"""
        payload = {"version": 1, "disabled": sorted(self._disabled_notebooks)}
        tmp = self._disabled_path.with_name(self._disabled_path.name + ".tmp")
        self._disabled_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
        tmp.replace(self._disabled_path)

    def _is_notebook_disabled(self, name: str) -> bool:
        """笔记本是否被禁用（default 恒为启用）。"""
        if name == "default":
            return False
        return name in self._disabled_notebooks

    def _set_notebook_disabled(self, name: str, disabled: bool) -> tuple[bool, str]:
        """切换笔记本启用/禁用并落盘。返回 (ok, message)。命令与 WebUI 共用入口。"""
        name = str(name or "").strip()
        if not name:
            return False, "笔记本名称不能为空"
        if name == "default":
            return False, "default 笔记本不可禁用"
        if name == "tmp":
            return False, "tmp 临时笔记本不可禁用"
        if self._get_notebook(name) is None:
            return False, f"笔记本 '{name}' 不存在"
        if self._is_notebook_disabled(name) == disabled:
            return True, f"笔记本 '{name}' 已是{'禁用' if disabled else '启用'}状态"
        if disabled:
            self._disabled_notebooks.add(name)
        else:
            self._disabled_notebooks.discard(name)
        self._save_disabled_notebooks()
        self.ctx.logger.info(f"笔记本 '{name}' 已{'禁用' if disabled else '启用'}")
        return True, "ok"

    def _get_notebook_for_bot(self, name: str) -> Notebook | None:
        """机器人侧笔记本解析：禁用本与不存在者一律返回 None（对机器人不可见）。"""
        nb = self._get_notebook(name)
        if nb is None or self._is_notebook_disabled(name):
            return None
        return nb

    def _list_enabled_notebook_names(self) -> str:
        """机器人侧错误消息用：仅列出启用笔记本名。"""
        return ", ".join(sorted(n for n in self._notebooks if not self._is_notebook_disabled(n)))

    @Tool(
        "add_aidraw_notes",
        brief_description="记录 AI 绘画提示词笔记（优先 tag 组合），支持一次写入多条",
        detailed_description=(
            "将一组 AI 绘画标签经验保存到绘图笔记本。"
            "每条包含英文标签(en)、中文释义(zh)和可选备注(note)。"
            "当用户分享了好的提示词组合，或你总结了绘图经验时，调用此工具记录下来。"
            "记录时优先保存有意义的 tag 组合或搭配（如形象设计、表情动作、背景、氛围、服装与配饰等彼此搭配形成的完整特征，一般由 3~10 个 tag 构成），"
            "不要只记录孤立、通用的单一 tag；若关键描述本身是自然语言写成的，也可直接以自然语言记录。"
            "并在 note 中简要总结该组合的作用与使用场景。"
        ),
        parameters=[
            ToolParameterInfo(
                name="notes",
                param_type=ToolParamType.ARRAY,
                description=(
                    '要记录的笔记数组，每条是 {"en": "英文标签", "zh": "中文释义", "note": "备注(可选)"}'
                ),
                required=True,
                items_schema={
                    "type": "object",
                    "properties": {
                        "en": {"type": "string", "description": "英文提示词标签"},
                        "zh": {"type": "string", "description": "中文释义"},
                        "note": {"type": "string", "description": "使用备注或经验（可选）"},
                    },
                    "required": ["en", "zh"],
                },
            ),
            ToolParameterInfo(
                name="notebook",
                param_type=ToolParamType.STRING,
                description="笔记本名称，不填默认写入 default 自带笔记本",
                default=None,
            ),
        ],
    )
    async def handle_add_notes(
        self, notes: list | None = None, notebook: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        if not notes or not isinstance(notes, list):
            return {"name": "add_aidraw_notes", "content": "参数 notes 不能为空，且必须是数组"}

        nb_name = str(notebook or "").strip() or "default"
        nb = self._get_notebook(nb_name)
        if nb is None:
            return {
                "name": "add_aidraw_notes",
                "content": f"笔记本 '{nb_name}' 不存在。可用笔记本: {self._list_notebook_names()}",
            }

        async with self._lock:
            if not nb.check_consistency():
                return {
                    "name": "add_aidraw_notes",
                    "content": f"写入失败：笔记本 '{nb_name}' 索引已失效，请联系管理员执行 /mpj rebuild",
                }

            valid_entries: list[dict[str, Any]] = []
            skipped = 0
            base_ts_ms = int(time.time() * 1000)
            now = time.time()
            for idx, item in enumerate(notes):
                if not isinstance(item, dict):
                    skipped += 1
                    continue
                en = str(item.get("en", "") or "").strip()
                zh = str(item.get("zh", "") or "").strip()
                note = str(item.get("note", "") or "").strip()
                if not en or not zh:
                    skipped += 1
                    self.ctx.logger.warning(f"add_aidraw_notes 跳过第 {idx} 条：en 或 zh 为空")
                    continue
                valid_entries.append(
                    {
                        "id": scramble_id(base_ts_ms + idx),
                        "en": en,
                        "zh": zh,
                        "note": note,
                        "ts": now,
                    }
                )

            if not valid_entries:
                return {"name": "add_aidraw_notes", "content": "没有有效的笔记条目（en 和 zh 不能为空）"}

            embedding_texts = [self._build_embedding_text(e["en"], e["zh"], e["note"]) for e in valid_entries]
            embeddings = await self._embed_batch(embedding_texts)
            if embeddings is None:
                return {"name": "add_aidraw_notes", "content": "写入失败：embedding 服务不可用"}

            # 写入去重检测：命中重复的条目拒绝写入，其余照常写入
            rejected: list[dict[str, Any]] = []
            accepted_indices: list[int] = []
            if self.config.journal.dedup_check_enabled:
                notebooks_to_check = self._pick_dedup_notebooks(nb)
                threshold = float(self.config.journal.dedup_check_threshold)
                for idx, entry in enumerate(valid_entries):
                    matches = await self._find_duplicate_matches(embeddings[idx], notebooks_to_check, threshold)
                    if matches:
                        rejected.append(
                            {
                                "en": entry["en"],
                                "zh": entry["zh"],
                                "note": entry["note"],
                                "matches": matches,
                            }
                        )
                        self.ctx.logger.warning(
                            f"add_aidraw_notes 拒绝写入重复条目: {entry['en']} / {entry['zh']} "
                            f"(匹配 {len(matches)} 条)"
                        )
                    else:
                        accepted_indices.append(idx)
            else:
                accepted_indices = list(range(len(valid_entries)))

            accepted_entries: list[dict[str, Any]] = []
            if accepted_indices:
                accepted_entries = [valid_entries[i] for i in accepted_indices]
                accepted_emb = embeddings[accepted_indices]
                nb.append_entries(accepted_entries, accepted_emb)
                nb.update_md5()
                self._create_backup(nb)

            count = nb.count_notes()
            parts: list[str] = []
            if accepted_entries:
                parts.append(f"成功写入 {len(accepted_entries)} 条笔记到 {nb_name}")
                if skipped:
                    parts.append(f"（跳过 {skipped} 条无效数据）")
                parts.append(f"，{nb_name} 当前共 {count} 条")
                for e in accepted_entries:
                    full = f"{e['en']} / {e['zh']}" + (f" — {e['note']}" if e["note"] else "")
                    shown = full if len(full) <= 25 else full[:25] + "…"
                    parts.append(f"\n- ID: {e['id']} | 内容: {shown}")
            if rejected:
                parts.append(f"\n以下 {len(rejected)} 条笔记因与已有笔记重复度过高被拒绝写入：")
                for r in rejected:
                    parts.append(f"- {r['en']} / {r['zh']} 匹配到:")
                    for m in r["matches"]:
                        parts.append(
                            f"    [{m['notebook']}/{m['id']}] {m['en']} / {m['zh']}"
                            f" (相似度 {m['score']:.2f})"
                        )
            if not accepted_entries:
                parts.append("本次没有写入任何笔记")
            msg = "".join(parts)
            self.ctx.logger.info(msg)
            return {
                "name": "add_aidraw_notes",
                "content": msg,
                "results": [
                    {"id": e["id"], "en": e["en"], "zh": e["zh"], "note": e["note"]} for e in accepted_entries
                ],
                "rejected": rejected,
            }

    @Tool(
        "read_aidraw_notes",
        brief_description="从绘图笔记本中搜索相关提示词经验",
        detailed_description=(
            "根据绘图关键词语义搜索之前记录的提示词笔记。"
            "当你需要参考过去的绘图经验、查找合适的标签组合时调用此工具。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="搜索关键词，可以是中文或英文，如 '猫娘' 或 'school uniform'",
                required=True,
            ),
            ToolParameterInfo(
                name="limit",
                param_type=ToolParamType.INTEGER,
                description="返回笔记数量上限",
                default=None,
            ),
            ToolParameterInfo(
                name="notebook",
                param_type=ToolParamType.STRING,
                description="笔记本名称（如 default/alice），或 all 搜索全部笔记本。不填默认 default",
                default=None,
            ),
        ],
    )
    async def handle_read_notes(
        self, query: str = "", limit: int | None = None, notebook: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"name": "read_aidraw_notes", "content": "搜索关键词不能为空"}

        nb_name = str(notebook or "").strip() or "default"
        top_k = limit if (isinstance(limit, int) and limit > 0) else self.config.journal.search_limit
        min_score = float(self.config.journal.min_score)

        async with self._lock:
            query_vec = await self._embed_single(query)
            if query_vec is None:
                return {"name": "read_aidraw_notes", "content": "搜索失败：embedding 服务不可用"}

            if nb_name == "all":
                results = await self._search_all_notebooks(query, query_vec, top_k, min_score)
            else:
                nb = self._get_notebook(nb_name)
                if nb is None:
                    return {
                        "name": "read_aidraw_notes",
                        "content": f"笔记本 '{nb_name}' 不存在。可用笔记本: {self._list_notebook_names()}",
                    }
                results = await self._search_single_notebook(nb, query, query_vec, top_k, min_score)

            if not results:
                return {"name": "read_aidraw_notes", "content": "未找到相关笔记"}

            lines = [f"找到 {len(results)} 条相关笔记："]
            for i, r in enumerate(results, 1):
                note_part = f" — {r['note']}" if r["note"] else ""
                lines.append(
                    f'{i}. 来自笔记本:{r["notebook"]} 笔记ID:{r["id"]} '
                    f'{r["en"]} / {r["zh"]}{note_part} (相似度: {r["score"]:.2f})'
                )

            return {
                "name": "read_aidraw_notes",
                "content": "\n".join(lines),
                "results": results,
            }

    @Tool(
        "aidraw_prompt_generate",
        brief_description="传入绘图要求（人物形象/服饰，动作，背景环境等），根据笔记本内容自动输出成品提示词。若需构建提示词请优先使用此工具而非read_aidraw_notes。",
        detailed_description=(
            "当需要画图时调用。传入绘图要求（人物形象/服饰、标签动作、背景环境等），"
            "本工具会释放一个子代理自行检索绘图笔记本中相关的提示词经验，"
            "最终只返回一段成品英文提示词和简短中文说明，不占用你的上下文。"
            "适合需要综合多条经验的场景，比逐条 read_aidraw_notes 更省上下文。"
        ),
        parameters=[
            ToolParameterInfo(
                name="requirement",
                param_type=ToolParamType.STRING,
                description="绘图要求，尽量简明扼要",
                required=True,
            ),
        ],
    )
    async def handle_aidraw_prompt_generate(self, requirement: str = "", **kwargs: Any) -> dict[str, Any]:
        requirement = str(requirement or "").strip()
        if not requirement:
            return {"name": _AIDRAW_PROMPT_GEN_TOOL_NAME, "content": "绘图要求不能为空"}

        if not bool(getattr(self.config.journal, "aidraw_prompt_gen_enabled", False)):
            return {
                "name": _AIDRAW_PROMPT_GEN_TOOL_NAME,
                "content": "aidraw_prompt_generate 工具已禁用，请使用 read_aidraw_notes 自行检索",
            }

        result_text, error = await self._run_aidraw_prompt_gen(requirement)
        if error is not None:
            return {
                "name": _AIDRAW_PROMPT_GEN_TOOL_NAME,
                "content": f"{error}。可改用 read_aidraw_notes 自行检索绘图经验。",
            }
        return {"name": _AIDRAW_PROMPT_GEN_TOOL_NAME, "content": result_text}

    @Tool(
        "modify_aidraw_note",
        brief_description="修改绘图笔记中的指定条目",
        detailed_description=(
            "根据笔记 ID 修改指定笔记本中的一条笔记。"
            "note_id 必填，notebook 不填默认为 default。"
            "en/zh/note 只需要传要修改的字段，未传的保持原值。"
            "修改后该条会自动重新计算向量。"
        ),
        parameters=[
            ToolParameterInfo(
                name="note_id",
                param_type=ToolParamType.STRING,
                description="要修改的笔记 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="notebook",
                param_type=ToolParamType.STRING,
                description="笔记本名称，不填默认为 default",
                default=None,
            ),
            ToolParameterInfo(
                name="en",
                param_type=ToolParamType.STRING,
                description="新的英文标签（可选，不填则不修改）",
                default=None,
            ),
            ToolParameterInfo(
                name="zh",
                param_type=ToolParamType.STRING,
                description="新的中文释义（可选，不填则不修改）",
                default=None,
            ),
            ToolParameterInfo(
                name="note",
                param_type=ToolParamType.STRING,
                description="新的备注（可选，不填则不修改）",
                default=None,
            ),
        ],
    )
    async def handle_modify_note(
        self,
        note_id: str = "",
        notebook: str | None = None,
        en: str | None = None,
        zh: str | None = None,
        note: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        clean_id = str(note_id or "").strip()
        if not clean_id:
            return {"name": "modify_aidraw_note", "content": "note_id 不能为空"}

        nb_name = str(notebook or "").strip() or "default"
        nb = self._get_notebook(nb_name)
        if nb is None:
            return {
                "name": "modify_aidraw_note",
                "content": f"笔记本 '{nb_name}' 不存在。可用笔记本: {self._list_notebook_names()}",
            }

        async with self._lock:
            if not nb.check_consistency():
                return {
                    "name": "modify_aidraw_note",
                    "content": f"修改失败：笔记本 '{nb_name}' 索引已失效，请联系管理员执行 /mpj rebuild",
                }

            entries = nb.load_notes()
            target_idx = None
            for i, entry in enumerate(entries):
                if entry.get("id") == clean_id:
                    target_idx = i
                    break

            if target_idx is None:
                return {
                    "name": "modify_aidraw_note",
                    "content": f"在笔记本 '{nb_name}' 中未找到笔记 ID: {clean_id}",
                }

            entry = entries[target_idx]
            old_hash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])

            # 部分更新
            changed = False
            if en is not None:
                new_en = str(en).strip()
                if new_en:
                    entry["en"] = new_en
                    changed = True
            if zh is not None:
                new_zh = str(zh).strip()
                if new_zh:
                    entry["zh"] = new_zh
                    changed = True
            if note is not None:
                entry["note"] = str(note).strip()
                changed = True

            if not changed:
                return {"name": "modify_aidraw_note", "content": "未提供任何要修改的字段"}

            entries[target_idx] = entry
            new_hash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])

            # 加载向量
            embeddings = nb.load_embeddings()

            # 内容变化时重新 embed（同时用于去重检测与向量更新）
            if embeddings is not None and old_hash != new_hash and len(embeddings) > target_idx:
                emb_text = self._build_embedding_text(entry["en"], entry["zh"], entry["note"])
                new_vec = await self._embed_single(emb_text)
                if new_vec is None:
                    return {"name": "modify_aidraw_note", "content": "修改失败：embedding 服务不可用"}

                # 写入去重检测：新内容与已有笔记重复则拒绝修改（排除自身）
                if self.config.journal.dedup_check_enabled:
                    notebooks_to_check = self._pick_dedup_notebooks(nb)
                    threshold = float(self.config.journal.dedup_check_threshold)
                    matches = await self._find_duplicate_matches(
                        new_vec, notebooks_to_check, threshold, exclude_id=clean_id
                    )
                    if matches:
                        lines = [
                            f"修改被拒绝：新内容与已有笔记重复度过高（匹配 {len(matches)} 条），"
                            "未写入。匹配笔记："
                        ]
                        lines.append(self._format_matches(matches))
                        return {
                            "name": "modify_aidraw_note",
                            "content": "\n".join(lines),
                        }

                emb_f16 = embeddings.astype(np.float16)
                if emb_f16.shape[1] == len(new_vec):
                    emb_f16[target_idx] = new_vec.astype(np.float16)
                    embeddings = emb_f16
                else:
                    # 维度不匹配，需要重建
                    self.ctx.logger.warning(
                        f"向量维度不匹配 (旧={emb_f16.shape[1]}, 新={len(new_vec)})，"
                        f"修改后需要执行 /mpj rebuild"
                    )

            nb.rewrite_all(entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

            self.ctx.logger.info(f"修改笔记成功: notebook={nb_name} id={clean_id}")
            return {
                "name": "modify_aidraw_note",
                "content": f"已修改笔记 {clean_id}（笔记本: {nb_name}）",
            }

    @Tool(
        "delete_aidraw_note",
        brief_description="删除绘图笔记中的指定条目",
        detailed_description=(
            "根据笔记 ID 删除指定笔记本中的一条笔记。"
            "note_id 必填，notebook 不填默认为 default。"
        ),
        parameters=[
            ToolParameterInfo(
                name="note_id",
                param_type=ToolParamType.STRING,
                description="要删除的笔记 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="notebook",
                param_type=ToolParamType.STRING,
                description="笔记本名称，不填默认为 default",
                default=None,
            ),
        ],
    )
    async def handle_delete_note(
        self, note_id: str = "", notebook: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        clean_id = str(note_id or "").strip()
        if not clean_id:
            return {"name": "delete_aidraw_note", "content": "note_id 不能为空"}

        nb_name = str(notebook or "").strip() or "default"
        nb = self._get_notebook(nb_name)
        if nb is None:
            return {
                "name": "delete_aidraw_note",
                "content": f"笔记本 '{nb_name}' 不存在。可用笔记本: {self._list_notebook_names()}",
            }

        async with self._lock:
            if not nb.check_consistency():
                return {
                    "name": "delete_aidraw_note",
                    "content": f"删除失败：笔记本 '{nb_name}' 索引已失效，请联系管理员执行 /mpj rebuild",
                }

            entries = nb.load_notes()
            target_idx = None
            for i, entry in enumerate(entries):
                if entry.get("id") == clean_id:
                    target_idx = i
                    break

            if target_idx is None:
                return {
                    "name": "delete_aidraw_note",
                    "content": f"在笔记本 '{nb_name}' 中未找到笔记 ID: {clean_id}",
                }

            del entries[target_idx]

            embeddings = nb.load_embeddings()
            if embeddings is not None and len(embeddings) > target_idx:
                embeddings = np.delete(embeddings, target_idx, axis=0)

            nb.rewrite_all(entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

            count = nb.count_notes()
            self.ctx.logger.info(f"删除笔记成功: notebook={nb_name} id={clean_id} 剩余={count}")
            return {
                "name": "delete_aidraw_note",
                "content": f"已删除笔记 {clean_id}（笔记本: {nb_name}），剩余 {count} 条",
            }

    @Command(
        "mpj_refresh",
        description="重载绘图笔记本并查看状态",
        pattern=r"^/mpj\s+refresh$",
    )
    async def handle_refresh(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return False, "无权限：仅管理员可执行此操作", True

        async with self._lock:
            # 重新发现笔记本
            self._notebooks = self._discover_notebooks()

            # 构建状态报告
            lines = ["📋 绘图笔记本状态："]
            for name in sorted(self._notebooks.keys()):
                nb = self._notebooks[name]
                count = nb.count_notes()
                if not nb.has_source:
                    status = "空"
                elif not nb.has_index:
                    status = "未建索引 ✗"
                elif nb.check_consistency():
                    status = "索引有效 ✓"
                else:
                    status = "索引失效 ✗"
                lines.append(f"- {name}: {count} 条, {status}")
            lines.append("")
            lines.append("如需重建索引，请执行 /mpj rebuild")

        msg = "\n".join(lines)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @Command(
        "mpj_rebuild",
        description="重建绘图笔记本向量索引，支持 --full 全量重建",
        pattern=r"^/mpj\s+rebuild(?:\s+(?P<full>--full))?$",
    )
    async def handle_rebuild(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return False, "无权限：仅管理员可执行此操作", True

        force_full = bool((kwargs.get("matched_groups", {}) or {}).get("full"))

        async with self._lock:
            # 重新发现笔记本（确保新文件被检测到）
            self._notebooks = self._discover_notebooks()

            mode_label = "全量重建" if force_full else "增量重建"
            lines = [f"索引{mode_label}结果："]
            has_error = False
            for name in sorted(self._notebooks.keys()):
                nb = self._notebooks[name]
                if not nb.has_source:
                    lines.append(f"- {name}: 跳过（无源文件）")
                    continue
                try:
                    stats = await self._rebuild_notebook(nb, force_full=force_full)
                    lines.append(
                        f"- {name}: 共 {stats['total']} 条，"
                        f"复用 {stats['reused']} 条，新建 {stats['rebuilt']} 条"
                    )
                except Exception as exc:
                    has_error = True
                    lines.append(f"- {name}: 失败 — {exc}")
                    self.ctx.logger.error(f"笔记本 {name} 重建失败: {exc}", exc_info=True)

        msg = "\n".join(lines)
        self.ctx.logger.info(msg)
        await self.ctx.send.text(msg, stream_id)
        if has_error:
            return False, msg, True
        return True, msg, True

    @Command(
        "mpj_help",
        description="查看绘图笔记本指令帮助",
        pattern=r"^/mpj\s+help$",
    )
    async def handle_cmd_help(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        lines = [
            "📒 麦麦的绘图笔记本 — 指令帮助",
            "",
            "📝 笔记操作：",
            "  /mpj add 英文|中文[|备注] [-n 笔记本]",
            "    添加一条笔记，笔记本默认 default",
            "  /mpj search 关键词 [-n 笔记本或all]",
            "    语义搜索笔记，默认搜 default",
            "  /mpj modify ID 字段=值 [...] [-n 笔记本]",
            "    修改笔记，字段可选 en/zh/note",
            "  /mpj delete ID [-n 笔记本]",
            "    删除指定笔记",
            "",
            "🔧 索引管理：",
            "  /mpj refresh",
            "    重载笔记本并查看状态",
            "  /mpj rebuild",
            "    增量重建所有笔记本的向量索引",
            "  /mpj rebuild --full",
            "    全量重建（忽略缓存，全部重新计算向量，换模型后使用）",
            "",
            "📦 备份：",
            "  /mpj backup list [-n 笔记本]",
            "    查看笔记本备份",
            "  /mpj backup restore 时间戳 [-n 笔记本]",
            "    恢复备份（恢复前自动备份当前状态）",
            "  /mpj backup delete 时间戳 [-n 笔记本]",
            "    删除备份",
            "",
            "  /mpj help",
            "    显示此帮助信息",
        ]
        msg = "\n".join(lines)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @Command(
        "mpj_add",
        description="添加绘图笔记",
        pattern=r"^/mpj\s+add\s+(?P<content>.+)$",
    )
    async def handle_cmd_add(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("content", "") or "").strip()
        if not raw:
            await self.ctx.send.text("用法: /mpj add 英文|中文[|备注] [-n 笔记本]", stream_id)
            return True, "", True

        content, nb_name = self._parse_notebook_flag(raw)
        parts = content.split("|", 2)
        if len(parts) < 2:
            await self.ctx.send.text("格式错误，至少需要 英文|中文，用 | 分隔", stream_id)
            return True, "", True

        en = parts[0].strip()
        zh = parts[1].strip()
        note = parts[2].strip() if len(parts) > 2 else ""
        if not en or not zh:
            await self.ctx.send.text("英文标签和中文释义不能为空", stream_id)
            return True, "", True

        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
            return True, "", True

        async with self._lock:
            if not nb.check_consistency():
                await self.ctx.send.text(f"笔记本 '{nb_name}' 索引失效，请执行 /mpj rebuild", stream_id)
                return True, "", True

            base_ts_ms = int(time.time() * 1000)
            now = time.time()
            entry = {"id": scramble_id(base_ts_ms), "en": en, "zh": zh, "note": note, "ts": now}
            emb_text = self._build_embedding_text(en, zh, note)
            emb = await self._embed_single(emb_text)
            if emb is None:
                await self.ctx.send.text("添加失败：embedding 服务不可用", stream_id)
                return True, "", True

            # 写入去重检测：命中重复则不写入，转为待确认
            if self.config.journal.dedup_check_enabled:
                notebooks_to_check = self._pick_dedup_notebooks(nb)
                threshold = float(self.config.journal.dedup_check_threshold)
                matches = await self._find_duplicate_matches(emb, notebooks_to_check, threshold)
                if matches:
                    token = self._store_pending_confirm(
                        {"type": "add", "notebook": nb_name, "en": en, "zh": zh, "note": note}
                    )
                    lines = [
                        "检测到与已有笔记重复度过高，本次未写入。匹配笔记：",
                        self._format_matches(matches),
                        "",
                        f"如确认要写入，请发送：/mpj confirm {token}",
                    ]
                    msg = "\n".join(lines)
                    await self.ctx.send.text(msg, stream_id)
                    return True, msg, True

            nb.append_entries([entry], emb.reshape(1, -1))
            nb.update_md5()
            self._create_backup(nb)

        count = nb.count_notes()
        msg = f"已添加到 {nb_name}（当前共 {count} 条）：{en} / {zh}"
        if note:
            msg += f" — {note}"
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @Command(
        "mpj_search",
        description="搜索绘图笔记",
        pattern=r"^/mpj\s+search\s+(?P<query>.+)$",
    )
    async def handle_cmd_search(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("query", "") or "").strip()
        if not raw:
            await self.ctx.send.text("用法: /mpj search 关键词 [-n 笔记本或all]", stream_id)
            return True, "", True

        query, nb_name = self._parse_notebook_flag(raw)
        query = query.strip()
        if not query:
            await self.ctx.send.text("搜索关键词不能为空", stream_id)
            return True, "", True

        top_k = int(self.config.journal.search_limit)
        min_score = float(self.config.journal.min_score)

        async with self._lock:
            query_vec = await self._embed_single(query)
            if query_vec is None:
                await self.ctx.send.text("搜索失败：embedding 服务不可用", stream_id)
                return True, "", True

            if nb_name == "all":
                results = await self._search_all_notebooks(query, query_vec, top_k, min_score)
            else:
                nb = self._get_notebook(nb_name)
                if nb is None:
                    await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
                    return True, "", True
                results = await self._search_single_notebook(nb, query, query_vec, top_k, min_score)

        if not results:
            await self.ctx.send.text("未找到相关笔记", stream_id)
            return True, "", True

        lines = [f"找到 {len(results)} 条相关笔记："]
        for i, r in enumerate(results, 1):
            note_part = f" — {r['note']}" if r.get("note") else ""
            nb_label = r.get("notebook", nb_name)
            lines.append(f'{i}. [{nb_label}/{r["id"]}] {r["en"]} / {r["zh"]}{note_part} ({r["score"]:.2f})')
        msg = "\n".join(lines)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @Command(
        "mpj_modify",
        description="修改绘图笔记",
        pattern=r"^/mpj\s+modify\s+(?P<content>.+)$",
    )
    async def handle_cmd_modify(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("content", "") or "").strip()
        if not raw:
            await self.ctx.send.text("用法: /mpj modify ID 字段=值 [...] [-n 笔记本]", stream_id)
            return True, "", True

        content, nb_name = self._parse_notebook_flag(raw)
        tokens = content.split()
        if len(tokens) < 2:
            await self.ctx.send.text("格式: /mpj modify ID 字段=值 [...] [-n 笔记本]", stream_id)
            return True, "", True

        note_id = tokens[0].strip()
        updates: dict[str, str] = {}
        for token in tokens[1:]:
            if "=" in token:
                key, val = token.split("=", 1)
                key = key.strip().lower()
                val = val.strip()
                if key in ("en", "zh", "note") and val:
                    updates[key] = val

        if not updates:
            await self.ctx.send.text("请指定要修改的字段: en=值 zh=值 note=值", stream_id)
            return True, "", True

        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
            return True, "", True

        async with self._lock:
            if not nb.check_consistency():
                await self.ctx.send.text(f"笔记本 '{nb_name}' 索引失效，请执行 /mpj rebuild", stream_id)
                return True, "", True

            entries = nb.load_notes()
            target_idx = None
            for i, entry in enumerate(entries):
                if entry.get("id") == note_id:
                    target_idx = i
                    break
            if target_idx is None:
                await self.ctx.send.text(f"未找到笔记 ID: {note_id}（笔记本: {nb_name}）", stream_id)
                return True, "", True

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
                    # 写入去重检测：新内容与已有笔记重复则转为待确认（排除自身）
                    if self.config.journal.dedup_check_enabled:
                        notebooks_to_check = self._pick_dedup_notebooks(nb)
                        threshold = float(self.config.journal.dedup_check_threshold)
                        matches = await self._find_duplicate_matches(
                            new_vec, notebooks_to_check, threshold, exclude_id=note_id
                        )
                        if matches:
                            token = self._store_pending_confirm(
                                {
                                    "type": "modify",
                                    "notebook": nb_name,
                                    "note_id": note_id,
                                    "updates": updates,
                                }
                            )
                            lines = [
                                "检测到修改后的内容与已有笔记重复度过高，本次未修改。匹配笔记：",
                                self._format_matches(matches),
                                "",
                                f"如确认要修改，请发送：/mpj confirm {token}",
                            ]
                            msg = "\n".join(lines)
                            await self.ctx.send.text(msg, stream_id)
                            return True, msg, True
                    emb_f16 = embeddings.astype(np.float16)
                    if emb_f16.shape[1] == len(new_vec):
                        emb_f16[target_idx] = new_vec.astype(np.float16)
                        embeddings = emb_f16

            nb.rewrite_all(entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

        await self.ctx.send.text(f"已修改笔记 {note_id}（笔记本: {nb_name}）", stream_id)
        return True, "", True

    @Command(
        "mpj_delete",
        description="删除绘图笔记",
        pattern=r"^/mpj\s+delete\s+(?P<content>.+)$",
    )
    async def handle_cmd_delete(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("content", "") or "").strip()
        if not raw:
            await self.ctx.send.text("用法: /mpj delete ID [-n 笔记本]", stream_id)
            return True, "", True

        content, nb_name = self._parse_notebook_flag(raw)
        note_id = content.strip()
        if not note_id:
            await self.ctx.send.text("笔记 ID 不能为空", stream_id)
            return True, "", True

        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
            return True, "", True

        async with self._lock:
            if not nb.check_consistency():
                await self.ctx.send.text(f"笔记本 '{nb_name}' 索引失效，请执行 /mpj rebuild", stream_id)
                return True, "", True

            entries = nb.load_notes()
            target_idx = None
            for i, entry in enumerate(entries):
                if entry.get("id") == note_id:
                    target_idx = i
                    break
            if target_idx is None:
                await self.ctx.send.text(f"未找到笔记 ID: {note_id}（笔记本: {nb_name}）", stream_id)
                return True, "", True

            del entries[target_idx]
            embeddings = nb.load_embeddings()
            if embeddings is not None and len(embeddings) > target_idx:
                embeddings = np.delete(embeddings, target_idx, axis=0)
            nb.rewrite_all(entries, embeddings)
            nb.update_md5()
            self._create_backup(nb)

        count = nb.count_notes()
        await self.ctx.send.text(f"已删除笔记 {note_id}（笔记本: {nb_name}），剩余 {count} 条", stream_id)
        return True, "", True

    @Command(
        "mpj_confirm",
        description="确认写入被去重检测拦截的笔记",
        pattern=r"^/mpj\s+confirm\s+(?P<token>.+)$",
    )
    async def handle_cmd_confirm(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        token = str(matched_groups.get("token", "") or "").strip()
        if not token:
            await self.ctx.send.text("用法: /mpj confirm <确认码>", stream_id)
            return True, "", True

        self._evict_pending_confirms()
        pending = self._pending_confirms.pop(token, None)
        if pending is None:
            await self.ctx.send.text("确认码无效或已过期，请重新执行原命令", stream_id)
            return True, "", True

        nb_name = str(pending.get("notebook", "") or "").strip() or "default"
        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在", stream_id)
            return True, "", True

        async with self._lock:
            if not nb.check_consistency():
                await self.ctx.send.text(f"笔记本 '{nb_name}' 索引失效，请执行 /mpj rebuild", stream_id)
                return True, "", True

            if pending["type"] == "add":
                base_ts_ms = int(time.time() * 1000)
                now = time.time()
                en = str(pending.get("en", "") or "").strip()
                zh = str(pending.get("zh", "") or "").strip()
                note = str(pending.get("note", "") or "").strip()
                if not en or not zh:
                    await self.ctx.send.text("确认的操作数据无效（en/zh 为空）", stream_id)
                    return True, "", True
                entry = {"id": scramble_id(base_ts_ms), "en": en, "zh": zh, "note": note, "ts": now}
                emb_text = self._build_embedding_text(en, zh, note)
                emb = await self._embed_single(emb_text)
                if emb is None:
                    await self.ctx.send.text("写入失败：embedding 服务不可用", stream_id)
                    return True, "", True
                nb.append_entries([entry], emb.reshape(1, -1))
                nb.update_md5()
                self._create_backup(nb)
                count = nb.count_notes()
                msg = f"已确认写入 {nb_name}（当前共 {count} 条）：{en} / {zh}"
                if note:
                    msg += f" — {note}"
                await self.ctx.send.text(msg, stream_id)
                return True, msg, True

            if pending["type"] == "modify":
                note_id = str(pending.get("note_id", "") or "").strip()
                updates = pending.get("updates") or {}
                entries = nb.load_notes()
                target_idx = None
                for i, entry in enumerate(entries):
                    if entry.get("id") == note_id:
                        target_idx = i
                        break
                if target_idx is None:
                    await self.ctx.send.text(f"未找到笔记 ID: {note_id}（笔记本: {nb_name}）", stream_id)
                    return True, "", True
                entry = entries[target_idx]
                old_hash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])
                for key in ("en", "zh", "note"):
                    if key in updates:
                        entry[key] = str(updates[key] or "").strip()
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
                await self.ctx.send.text(f"已确认修改笔记 {note_id}（笔记本: {nb_name}）", stream_id)
                return True, "", True

        await self.ctx.send.text("确认的操作类型无效", stream_id)
        return True, "", True

    @Command(
        "mpj_new",
        description="创建空白笔记本",
        pattern=r"^/mpj\s+new\s+(?P<name>.+)$",
    )
    async def handle_cmd_new(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        name = str(matched_groups.get("name", "") or "").strip()
        if not name:
            await self.ctx.send.text("用法: /mpj new <笔记本名>", stream_id)
            return True, "", True

        ok, result = await self._create_blank_notebook(name)
        if not ok:
            await self.ctx.send.text(result, stream_id)
            return True, "", True

        msg = f"已创建空白笔记本 {result}，可开始添加笔记（如 /mpj add 英文|中文 -n {result}）"
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    async def _create_blank_notebook(self, name: str) -> tuple[bool, str]:
        """创建空白笔记本并建好空索引，供 /mpj new 命令与 WebUI 笔记本管理页共用。

        返回 (ok, name) 或 (False, 错误信息)。
        """
        import re as _re

        name = str(name or "").strip()
        if not name:
            return False, "笔记本名称不能为空"
        if name == "default" or name == "tmp":
            return False, f"笔记本名 '{name}' 不可用"
        if not _re.match(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$", name):
            return False, "笔记本名称只能包含中文/字母/数字/下划线/连字符"
        if self._get_notebook(name) is not None:
            return False, f"笔记本 '{name}' 已存在"

        async with self._lock:
            nb = Notebook(name, self._data_dir)
            nb.notes_path.parent.mkdir(parents=True, exist_ok=True)
            nb.notes_path.write_text("", encoding="utf-8")
            try:
                await self._rebuild_notebook(nb)
            except Exception as exc:
                self.ctx.logger.error(f"笔记本 {name} 初始化失败: {exc}", exc_info=True)
                return False, f"创建失败：{exc}"
            self._notebooks = self._discover_notebooks()

        return True, name

    # ============================================================
    # 管理员命令：/mpj backup
    # ============================================================

    @Command(
        "mpj_backup_list",
        description="查看笔记本备份列表",
        pattern=r"^/mpj\s+backup\s+list(?:\s+(?P<rest>.+))?$",
    )
    async def handle_cmd_backup_list(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("rest", "") or "").strip()
        _content, nb_name = self._parse_notebook_flag(raw)

        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
            return True, "", True

        backups = self._list_backups(nb)
        if not backups:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 暂无备份", stream_id)
            return True, "", True

        lines = [f"📦 笔记本 '{nb_name}' 备份（{len(backups)} 份）："]
        for b in backups:
            lines.append(f"- {b['timestamp']} | {b['count']} 条 | {b['size']} B")
        msg = "\n".join(lines)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @Command(
        "mpj_backup_restore",
        description="恢复笔记本备份",
        pattern=r"^/mpj\s+backup\s+restore\s+(?P<rest>.+)$",
    )
    async def handle_cmd_backup_restore(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("rest", "") or "").strip()
        content, nb_name = self._parse_notebook_flag(raw)
        timestamp = content.strip()
        if not timestamp:
            await self.ctx.send.text("用法: /mpj backup restore <时间戳> [-n 笔记本]", stream_id)
            return True, "", True

        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
            return True, "", True

        async with self._lock:
            ok, msg = await self._restore_backup(nb, timestamp)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @Command(
        "mpj_backup_delete",
        description="删除笔记本备份",
        pattern=r"^/mpj\s+backup\s+delete\s+(?P<rest>.+)$",
    )
    async def handle_cmd_backup_delete(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get("rest", "") or "").strip()
        content, nb_name = self._parse_notebook_flag(raw)
        timestamp = content.strip()
        if not timestamp:
            await self.ctx.send.text("用法: /mpj backup delete <时间戳> [-n 笔记本]", stream_id)
            return True, "", True

        nb = self._get_notebook(nb_name)
        if nb is None:
            await self.ctx.send.text(f"笔记本 '{nb_name}' 不存在。可用: {self._list_notebook_names()}", stream_id)
            return True, "", True

        async with self._lock:
            ok, msg = self._delete_backup(nb, timestamp)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    @staticmethod
    def _parse_notebook_flag(raw: str) -> tuple[str, str]:
        """从命令参数尾部提取 -n 笔记本名，返回 (剩余参数, 笔记本名)。"""
        import re

        match = re.search(r"\s+-n\s+(\S+)\s*$", raw)
        if match:
            notebook = match.group(1).strip()
            remaining = raw[: match.start()].strip()
            return remaining, notebook
        return raw.strip(), "default"

    @HomeCard(
        "mpj_status_card",
        title="麦麦的绘图笔记本",
        description="AI 绘画提示词经验记录与向量语义检索",
        content=[
            {
                "type": "markdown",
                "content": "记录 AI 绘画标签经验，通过向量语义搜索历史笔记。支持多笔记本管理和增量索引重建。",
            },
            {
                "type": "actions",
                "actions": [
                    {"label": "插件配置", "url": "/plugin-config?plugin=mai.prompt-journal"},
                ],
            },
        ],
        width="medium",
        order=140,
    )
    async def home_status_card(self) -> None:
        return None

    def _is_admin(self, user_id: str) -> bool:
        admin_users = self.config.admin.users or []
        if not admin_users:
            return False
        return user_id in admin_users

    def _store_pending_confirm(self, data: dict[str, Any]) -> str:
        """登记一条待确认的写入操作，返回确认码（token）。"""
        self._evict_pending_confirms()
        token = uuid.uuid4().hex
        data["created_at"] = time.time()
        self._pending_confirms[token] = data
        return token

    def _evict_pending_confirms(self) -> None:
        """待确认操作 TTL 600s 且上限 50，防止内存膨胀。"""
        ttl = 600.0
        limit = 50
        now = time.time()
        stale = [t for t, d in self._pending_confirms.items() if now - d["created_at"] > ttl]
        for t in stale:
            self._pending_confirms.pop(t, None)
        if len(self._pending_confirms) > limit:
            oldest = sorted(self._pending_confirms.items(), key=lambda kv: kv[1]["created_at"])
            for t, _ in oldest[: len(self._pending_confirms) - limit]:
                self._pending_confirms.pop(t, None)


def create_plugin() -> PromptJournalPlugin:
    """创建麦麦的绘图笔记本插件实例。"""
    return PromptJournalPlugin()
