"""麦麦的绘图笔记本 — AI 绘画提示词经验记录与语义检索。

支持多笔记本（自带 default + imports/ 下的第三方笔记本）。
每个笔记本独立维护索引和缓存，支持增量重建。

提供四个 LLM 可调用工具：
  - add_aidraw_notes:    批量写入提示词笔记
  - read_aidraw_notes:   向量语义搜索笔记
  - modify_aidraw_note:  按笔记 ID 修改条目（部分更新）
  - delete_aidraw_note:  按笔记 ID 删除条目

提供两个管理员命令：
  - /mpj refresh: 重载笔记本并查看状态
  - /mpj rebuild: 增量重建所有需要重建的笔记本索引

数据文件布局（位于插件 data_dir）：
  default.jsonl / default.cache.jsonl / default.embeddings.npy / default.index.meta
  imports/{name}.jsonl / imports/{name}.cache.jsonl / ...
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    HomeCard,
    MaiBotPlugin,
    PluginConfigBase,
    Tool,
)
from maibot_sdk.types import ToolParameterInfo, ToolParamType


# ============================================================
# LLM 整理常量
# ============================================================

_ORGANIZE_DEFAULT_REQUIREMENT = "把上述重复的提示词笔记合并整理，默认输出一条合并结果，若有需要也可以输出2到3条"

# 受 allow_write 开关控制的写入类工具（只读模式下禁用）
_WRITE_TOOL_NAMES = ["add_aidraw_notes", "modify_aidraw_note", "delete_aidraw_note"]

_DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT = """\
**绘图笔记整理规则**
1. 你是一个客观、准确的绘图提示词笔记整理程序，负责把一批重复或相似的提示词笔记整理为精简、准确的笔记。
2. 忠实还原原笔记内容：保留关键信息（英文标签、中文释义、使用备注），不添加原内容中不存在的信息，不进行主观评论或升华。
3. 整理是编辑工作：保留最完整准确的信息，去除重复冗余；可将多条合并为一条，也可按内容拆分为多条；若确实无需合并，可原样保留并说明理由。

**输出格式（必须严格遵守）**
你的整个回复必须是且仅是一个 JSON 对象，不要附加任何文字、解释或代码围栏：
{"reason": "整理理由（简洁中文）", "entries": [{"en": "英文标签", "zh": "中文释义", "note": "使用备注，无则空字符串"}]}

其中：
- entries 是一个数组，允许 1~N 条：默认整理为 1 条；若条目间内容差异较大、或用户要求拆分，也可以整理为 2~3 条甚至更多。
- 每一条都必须保留完整的 en/zh，note 可为空。

**禁止行为**
把多条笔记强行合并成一条导致信息丢失；省略或软化原有标签；偏离用户整理要求；添加原内容不存在的标签或释义；输出除单个 JSON 对象以外的任何内容。"""

_ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT = """\
**绘图笔记库操作规则**
1. 你是一个客观、准确的绘图提示词笔记库操作程序，负责根据用户的整理要求审查并操作一本绘图提示词笔记本。你通过 search_notes 工具按关键词检索内容，发现库中符合要求的问题并输出一批操作。
2. 忠实执行用户的整理要求：用户要求操作哪些方面，你就围绕这些方面审查并修改；不要自行扩大范围、大规模改动用户未提及的内容。仅依据检索到的真实内容做判断，不添加原内容中不存在的信息。
3. 只在通过 search_notes 检索到某条笔记的真实 id 后，才能对其实施 update 或 delete；新建内容需与检索结果合理相关。不确定时宁可不动。

**可调用工具**
调用 search_notes 工具按关键词语义检索笔记（可多次、多角度调用）。每次检索返回一批笔记（含 id/en/zh/note）。基于检索结果逐步操作，最后一次性输出操作方案。

**输出格式（必须严格遵守）**
你的整个回复必须是且仅是一个 JSON 对象，不要附加任何文字、解释或代码围栏：
{"reason": "操作方案说明（简洁中文）", "operations": [操作1, 操作2, ...]}

操作类型：
- create：新建条目 {"type":"create","en":"英文标签","zh":"中文释义","note":"备注，无则空字符串"}
- update：修改已有条目 {"type":"update","id":"笔记ID","en":可选,"zh":可选,"note":可选}（未提供的字段保持不变；note 传空字符串表示清空）
- delete：删除已有条目 {"type":"delete","id":"笔记ID"}

其中：
- update/delete 的 id 必须是 search_notes 检索结果中出现过的真实 id。
- operations 可以为空数组，表示无需修改。

**禁止行为**
捏造或猜测不存在的笔记 id；仅凭直觉批量删除不确定的内容；偏离用户整理要求；添加原内容不存在的标签或释义；输出除单个 JSON 对象以外的任何内容。"""

_ORGANIZE_DB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_notes",
        "description": (
            "按关键词在绘图笔记库中做语义检索，返回最相关的笔记（含 id/en/zh/note）。"
            "可多次调用以检索不同关键词。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "检索关键词，中文或英文"},
                "notebook": {"type": "string", "description": "笔记本名称，留空为 default"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 10"},
            },
            "required": ["keyword"],
        },
    },
}


# ============================================================
# 配置模型
# ============================================================


class PluginSectionConfig(PluginConfigBase):
    """插件开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件", "hint": "是否启用本插件", "order": 0},
    )
    config_version: str = Field(
        default="2.1.0",
        description="配置版本",
        json_schema_extra={"label": "配置版本", "hint": "当前配置的版本号，一般无需修改", "order": 1},
    )


class JournalConfig(PluginConfigBase):
    """笔记本参数。"""

    __ui_label__ = "笔记本"
    __ui_icon__ = "book-open"
    __ui_order__ = 1

    search_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="read_aidraw_notes 默认返回条数",
        json_schema_extra={"label": "搜索默认返回条数", "hint": "read_aidraw_notes 默认返回的笔记条数", "order": 0},
    )
    min_score: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="最低相似度阈值",
        json_schema_extra={"label": "最低相似度阈值", "hint": "相似度低于该值的笔记不会出现在搜索结果中", "order": 1},
    )
    embed_max_concurrent: int = Field(
        default=4,
        ge=1,
        le=16,
        description="批量 embedding 最大并发数",
        json_schema_extra={"label": "批量 embedding 并发数", "hint": "批量 embedding 的最大并发数", "order": 2},
    )
    allow_write: bool = Field(
        default=True,
        description="是否允许 LLM 写入笔记本（add/modify/delete 工具）",
        json_schema_extra={
            "label": "允许 LLM 写入",
            "hint": "关闭后禁用 add/modify/delete 三个写入工具，麦麦只读笔记本；管理员 /mpj 命令与 WebUI 不受影响",
            "order": 3,
        },
    )


class AdminConfig(PluginConfigBase):
    """管理员权限。"""

    __ui_label__ = "管理员"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    users: list[str] = Field(
        default_factory=list,
        description="能执行 /mpj 命令的 QQ 号列表",
        json_schema_extra={"label": "管理员 QQ 号", "hint": "能执行 /mpj 命令的 QQ 号列表，每行一个", "order": 0},
    )


class WebConfig(PluginConfigBase):
    """WebUI 配置。"""

    __ui_label__ = "WebUI"
    __ui_icon__ = "globe"
    __ui_order__ = 3

    enabled: bool = Field(
        default=False,
        description="是否启用 WebUI（建议开启，很多功能只在WebUI可用）",
        json_schema_extra={"label": "启用 WebUI", "hint": "是否启用插件自带 WebUI（建议开启，去重、操作数据库等很多功能只在 WebUI 可用）", "order": 0},
    )
    port: int = Field(
        default=8010,
        ge=1,
        le=65535,
        description="WebUI 端口",
        json_schema_extra={"label": "WebUI 端口", "hint": "插件 WebUI 监听的端口，Docker 部署时需额外映射", "order": 1},
    )
    password: str = Field(
        default="",
        description="访问密码，留空则无密码保护",
        json_schema_extra={"label": "WebUI 访问密码", "hint": "访问密码，留空则无密码保护", "order": 2},
    )


class DedupMergeConfig(PluginConfigBase):
    """去重 LLM 整理参数。"""

    __ui_label__ = "去重整理"
    __ui_icon__ = "wand"
    __ui_order__ = 4

    enabled: bool = Field(
        default=True,
        description="是否启用 LLM 整理",
        json_schema_extra={"label": "启用 LLM 整理", "hint": "是否在去重扫描中启用 LLM 整理功能", "order": 0},
    )
    system_prompt: str = Field(
        default="",
        description="整理系统提示词，留空用内置默认",
        json_schema_extra={
            "label": "整理系统提示词",
            "hint": "LLM 整理使用的系统提示词，留空使用内置默认（LLM 由 [llm] 节配置）",
            "order": 1,
            "x-widget": "textarea",
            "rows": 8,
        },
    )


class OrganizeDbConfig(PluginConfigBase):
    """LLM 操作数据库参数。"""

    __ui_label__ = "操作数据库"
    __ui_icon__ = "database"
    __ui_order__ = 5

    enabled: bool = Field(
        default=True,
        description="是否启用 LLM 操作数据库",
        json_schema_extra={"label": "启用 LLM 操作数据库", "hint": "是否启用 WebUI 中的 LLM 操作数据库功能", "order": 0},
    )
    max_iterations: int = Field(
        default=8,
        ge=1,
        le=30,
        description="LLM 检索循环的最大轮数",
        json_schema_extra={"label": "最大检索轮数", "hint": "LLM 调用 search_notes 工具循环的最大轮数，防止死循环", "order": 1, "x-widget": "number"},
    )
    search_limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="search_notes 工具单次返回条数",
        json_schema_extra={"label": "search_notes 返回条数", "hint": "LLM 调用 search_notes 工具时单次返回的笔记条数", "order": 2, "x-widget": "number"},
    )
    system_prompt: str = Field(
        default="",
        description="操作数据库系统提示词，留空用内置默认",
        json_schema_extra={
            "label": "操作数据库系统提示词",
            "hint": "LLM 操作数据库使用的系统提示词，留空使用内置默认（LLM 由 [llm] 节配置）",
            "order": 3,
            "x-widget": "textarea",
            "rows": 8,
        },
    )


class DirectLlmConfig(PluginConfigBase):
    """直连 LLM API 配置（独立于麦麦自带 LLM，必填）。"""

    __ui_label__ = "LLM 直连"
    __ui_icon__ = "api"
    __ui_order__ = 6

    base_url: str = Field(
        default="",
        description="OpenAI 兼容 API 地址",
        json_schema_extra={"label": "API 地址", "hint": "OpenAI 兼容 API 地址，如 https://api.example.com/v1（必填）", "order": 0},
    )
    api_key: str = Field(
        default="",
        description="API 密钥",
        json_schema_extra={"label": "API 密钥", "hint": "调用 LLM API 的密钥（必填）", "order": 1, "x-widget": "password"},
    )
    model: str = Field(
        default="",
        description="具体模型名",
        json_schema_extra={"label": "模型名", "hint": "调用 LLM API 使用的具体模型名，如 deepseek-v4-flash（必填）", "order": 2},
    )
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="生成温度",
        json_schema_extra={"label": "生成温度", "hint": "LLM 生成的温度参数", "order": 3, "x-widget": "number"},
    )
    max_tokens: int = Field(
        default=10000,
        ge=1,
        le=65536,
        description="最大输出 token",
        json_schema_extra={"label": "最大输出 token", "hint": "LLM 生成的最大输出 token 数，防止长输出被截断", "order": 4, "x-widget": "number"},
    )
    timeout: int = Field(
        default=120,
        ge=5,
        le=600,
        description="请求超时秒数",
        json_schema_extra={"label": "请求超时（秒）", "hint": "单次 LLM API 请求的超时时间", "order": 5, "x-widget": "number"},
    )
    extra_params: str = Field(
        default="",
        description="额外 JSON 参数",
        json_schema_extra={
            "label": "额外 JSON 参数",
            "hint": "透传到请求体的额外 JSON，如 {\"enable_thinking\": false}",
            "order": 6,
            "x-widget": "textarea",
            "rows": 3,
        },
    )


class PromptJournalConfig(PluginConfigBase):
    """麦麦的绘图笔记本配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    dedup_merge: DedupMergeConfig = Field(default_factory=DedupMergeConfig)
    organize_db: OrganizeDbConfig = Field(default_factory=OrganizeDbConfig)
    llm: DirectLlmConfig = Field(default_factory=DirectLlmConfig)


# ============================================================
# 笔记 ID 生成：模乘置换 + Base36 编码
# ============================================================

# 黄金比例 (φ-1) × 2^64，低 48 位为奇数，保证 [0, 2^48) 上的严格双射
_MULTIPLIER = 0x9E3779B97F4A7C15
_MASK_48 = (1 << 48) - 1
_BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to_base36(n: int) -> str:
    """将非负整数编码为 base36 字符串。"""
    if n == 0:
        return "0"
    chars: list[str] = []
    while n > 0:
        n, remainder = divmod(n, 36)
        chars.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(chars))


def scramble_id(ts_ms: int) -> str:
    """将毫秒时间戳映射为无序化的 base36 ID（双射，可逆）。

    相邻时间戳的输出在约一半比特位上不同，视觉上无规律。
    """
    scrambled = (ts_ms * _MULTIPLIER) & _MASK_48
    return _to_base36(scrambled)


# ============================================================
# Notebook：单个笔记本的数据操作
# ============================================================


class Notebook:
    """封装单个笔记本的路径解析和文件读写。

    每个笔记本由 4 个文件组成：
      {name}.jsonl        人类可编辑的笔记源文件
      {name}.cache.jsonl  与向量索引对齐的内部快照
      {name}.embeddings.npy  float16 向量矩阵
      {name}.index.meta   索引元信息（md5、条目数、构建时间）
    """

    def __init__(self, name: str, base_dir: Path) -> None:
        self.name = name
        if name == "default":
            self._dir = base_dir
        else:
            self._dir = base_dir / "imports"

    # ---------- 路径 ----------

    @property
    def notes_path(self) -> Path:
        return self._dir / f"{self.name}.jsonl"

    @property
    def cache_path(self) -> Path:
        return self._dir / f"{self.name}.cache.jsonl"

    @property
    def embeddings_path(self) -> Path:
        return self._dir / f"{self.name}.embeddings.npy"

    @property
    def meta_path(self) -> Path:
        return self._dir / f"{self.name}.index.meta"

    @property
    def has_source(self) -> bool:
        return self.notes_path.exists()

    @property
    def has_index(self) -> bool:
        return self.embeddings_path.exists() and self.meta_path.exists()

    # ---------- JSONL 读取 ----------

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                entries.append(
                    {
                        "id": str(obj.get("id", "") or "").strip(),
                        "en": str(obj.get("en", "") or "").strip(),
                        "zh": str(obj.get("zh", "") or "").strip(),
                        "note": str(obj.get("note", "") or "").strip(),
                        "ts": float(obj.get("ts", 0) or 0),
                    }
                )
        return entries

    def load_notes(self) -> list[dict[str, Any]]:
        return self._load_jsonl(self.notes_path)

    def load_cache_notes(self) -> list[dict[str, Any]]:
        return self._load_jsonl(self.cache_path)

    def count_notes(self) -> int:
        if not self.notes_path.exists():
            return 0
        count = 0
        with self.notes_path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                if raw_line.strip():
                    count += 1
        return count

    # ---------- 向量读取 ----------

    def load_embeddings(self) -> np.ndarray | None:
        if not self.embeddings_path.exists():
            return None
        try:
            arr = np.load(self.embeddings_path)
        except Exception:
            return None
        if arr.ndim != 2 or arr.shape[0] == 0:
            return None
        return arr.astype(np.float32)

    # ---------- 一致性校验 ----------

    def compute_file_md5(self) -> str:
        if not self.notes_path.exists():
            return ""
        h = hashlib.md5()
        with self.notes_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def load_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        try:
            with self.meta_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_meta(self, meta: dict[str, Any]) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        with self.meta_path.open("w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def check_consistency(self) -> bool:
        """检查 notes.jsonl 的 MD5 是否与索引元信息一致。"""
        if not self.notes_path.exists():
            return True
        current_md5 = self.compute_file_md5()
        meta = self.load_meta()
        return meta.get("md5", "") == current_md5

    def update_md5(self) -> None:
        """写入完成后更新 MD5 缓存。"""
        meta = self.load_meta()
        meta["md5"] = self.compute_file_md5()
        meta["count"] = self.count_notes()
        meta["updated_at"] = time.time()
        self.save_meta(meta)

    # ---------- 写入操作 ----------

    def append_entries(self, entries: list[dict[str, Any]], embeddings: np.ndarray) -> None:
        """追加笔记和向量。"""
        emb_f16 = embeddings.astype(np.float16)

        # 追加 jsonl（notes 和 cache 内容一致）
        for path in (self.notes_path, self.cache_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")

        # 追加 npy
        if self.embeddings_path.exists():
            try:
                existing = np.load(self.embeddings_path)
                if existing.ndim == 2 and existing.shape[0] > 0 and existing.shape[1] == emb_f16.shape[1]:
                    combined = np.vstack([existing, emb_f16])
                else:
                    combined = emb_f16
            except Exception:
                combined = emb_f16
        else:
            combined = emb_f16
        np.save(self.embeddings_path, combined)

    def rewrite_all(self, entries: list[dict[str, Any]], embeddings: np.ndarray | None) -> None:
        """全量重写笔记和向量（用于 modify/delete）。"""
        # 重写 jsonl
        json_str = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
        if json_str:
            json_str += "\n"
        self.notes_path.parent.mkdir(parents=True, exist_ok=True)
        self.notes_path.write_text(json_str, encoding="utf-8")
        self.cache_path.write_text(json_str, encoding="utf-8")

        # 重写 npy
        if embeddings is not None:
            np.save(self.embeddings_path, embeddings)
        elif self.embeddings_path.exists():
            self.embeddings_path.unlink()

    def rewrite_cache(self, entries: list[dict[str, Any]]) -> None:
        """重建后重写 cache jsonl。"""
        json_str = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)
        if json_str:
            json_str += "\n"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json_str, encoding="utf-8")


# ============================================================
# 插件主类
# ============================================================


class PromptJournalPlugin(MaiBotPlugin):
    """麦麦的绘图笔记本插件。"""

    config_model = PromptJournalConfig

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._data_dir: Path = self.ctx.paths.data_dir
        self._imports_dir: Path = self._data_dir / "imports"
        self._lock: asyncio.Lock = asyncio.Lock()
        self._notebooks: dict[str, Notebook] = {}
        self._web_task: asyncio.Task | None = None
        self._web_runner: Any = None
        self._organize_sessions: dict[str, dict[str, Any]] = {}
        self._organize_tasks: dict[str, dict[str, Any]] = {}

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._imports_dir.mkdir(parents=True, exist_ok=True)

        # 迁移旧格式
        self._migrate_legacy()

        # 发现笔记本
        self._notebooks = self._discover_notebooks()

        notebook_names = ", ".join(sorted(self._notebooks.keys())) or "(无)"
        self.ctx.logger.info(f"麦麦的绘图笔记本已加载，发现笔记本: {notebook_names}")

        # 启动 WebUI
        if self.config.web.enabled:
            self._web_task = asyncio.create_task(self._run_web_server())

        # 按 allow_write 应用写入工具状态
        await self._apply_write_tools_state()

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
            await self._apply_write_tools_state()

    async def _apply_write_tools_state(self) -> None:
        """根据 allow_write 启用/禁用写入类 LLM 工具。"""
        allow_write = bool(getattr(self.config.journal, "allow_write", True))
        for name in _WRITE_TOOL_NAMES:
            try:
                if allow_write:
                    await self.ctx.component.enable_component(name, "tool", scope="global")
                else:
                    await self.ctx.component.disable_component(name, "tool", scope="global")
            except Exception as exc:
                self.ctx.logger.warning(f"{'启用' if allow_write else '禁用'}工具 {name} 失败: {exc}")

    # ============================================================
    # 迁移与发现
    # ============================================================

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
        """按名称获取笔记本，不存在返回 None。"""
        clean = str(name or "").strip() or "default"
        return self._notebooks.get(clean)

    def _list_notebook_names(self) -> str:
        return ", ".join(sorted(self._notebooks.keys()))

    # ============================================================
    # 工具一：add_aidraw_notes
    # ============================================================

    @Tool(
        "add_aidraw_notes",
        brief_description="记录 AI 绘画提示词笔记（优先 tag 组合），支持一次写入多条",
        detailed_description=(
            "将一组 AI 绘画标签经验保存到绘图笔记本。"
            "每条包含英文标签(en)、中文释义(zh)和可选备注(note)。"
            "当用户分享了好的提示词组合，或你总结了绘图经验时，调用此工具记录下来。"
            "记录时优先保存有意义的 tag 组合或搭配（如形象设计、表情动作、背景、氛围、服装与配饰等彼此搭配形成的完整特征，一般由 3~6 个 tag 构成），"
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

            nb.append_entries(valid_entries, embeddings)
            nb.update_md5()

            count = nb.count_notes()
            parts = [f"成功写入 {len(valid_entries)} 条笔记到 {nb_name}"]
            if skipped:
                parts.append(f"（跳过 {skipped} 条无效数据）")
            parts.append(f"，{nb_name} 当前共 {count} 条")
            for e in valid_entries:
                full = f"{e['en']} / {e['zh']}" + (f" — {e['note']}" if e["note"] else "")
                shown = full if len(full) <= 25 else full[:25] + "…"
                parts.append(f"\n- ID: {e['id']} | 内容: {shown}")
            msg = "".join(parts)
            self.ctx.logger.info(msg)
            return {
                "name": "add_aidraw_notes",
                "content": msg,
                "results": [
                    {"id": e["id"], "en": e["en"], "zh": e["zh"], "note": e["note"]} for e in valid_entries
                ],
            }

    # ============================================================
    # 工具二：read_aidraw_notes
    # ============================================================

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

    # ============================================================
    # 工具三：modify_aidraw_note
    # ============================================================

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

            # 内容变化时重新 embed
            if embeddings is not None and old_hash != new_hash and len(embeddings) > target_idx:
                emb_text = self._build_embedding_text(entry["en"], entry["zh"], entry["note"])
                new_vec = await self._embed_single(emb_text)
                if new_vec is not None:
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
                else:
                    return {"name": "modify_aidraw_note", "content": "修改失败：embedding 服务不可用"}

            nb.rewrite_all(entries, embeddings)
            nb.update_md5()

            self.ctx.logger.info(f"修改笔记成功: notebook={nb_name} id={clean_id}")
            return {
                "name": "modify_aidraw_note",
                "content": f"已修改笔记 {clean_id}（笔记本: {nb_name}）",
            }

    # ============================================================
    # 工具四：delete_aidraw_note
    # ============================================================

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

            count = nb.count_notes()
            self.ctx.logger.info(f"删除笔记成功: notebook={nb_name} id={clean_id} 剩余={count}")
            return {
                "name": "delete_aidraw_note",
                "content": f"已删除笔记 {clean_id}（笔记本: {nb_name}），剩余 {count} 条",
            }

    # ============================================================
    # 管理员命令：/mpj refresh
    # ============================================================

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

    # ============================================================
    # 管理员命令：/mpj rebuild
    # ============================================================

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

    # ============================================================
    # 管理员命令：/mpj help
    # ============================================================

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
            "  /mpj help",
            "    显示此帮助信息",
        ]
        msg = "\n".join(lines)
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    # ============================================================
    # 管理员命令：/mpj add
    # ============================================================

    @Command(
        "mpj_add",
        description="添加绘图笔记",
        pattern=r"^/mpj\s+add\s+(.+)$",
    )
    async def handle_cmd_add(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get(1, "") or "").strip()
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

            nb.append_entries([entry], emb.reshape(1, -1))
            nb.update_md5()

        count = nb.count_notes()
        msg = f"已添加到 {nb_name}（当前共 {count} 条）：{en} / {zh}"
        if note:
            msg += f" — {note}"
        await self.ctx.send.text(msg, stream_id)
        return True, msg, True

    # ============================================================
    # 管理员命令：/mpj search
    # ============================================================

    @Command(
        "mpj_search",
        description="搜索绘图笔记",
        pattern=r"^/mpj\s+search\s+(.+)$",
    )
    async def handle_cmd_search(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get(1, "") or "").strip()
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

    # ============================================================
    # 管理员命令：/mpj modify
    # ============================================================

    @Command(
        "mpj_modify",
        description="修改绘图笔记",
        pattern=r"^/mpj\s+modify\s+(.+)$",
    )
    async def handle_cmd_modify(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get(1, "") or "").strip()
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
                    emb_f16 = embeddings.astype(np.float16)
                    if emb_f16.shape[1] == len(new_vec):
                        emb_f16[target_idx] = new_vec.astype(np.float16)
                        embeddings = emb_f16

            nb.rewrite_all(entries, embeddings)
            nb.update_md5()

        await self.ctx.send.text(f"已修改笔记 {note_id}（笔记本: {nb_name}）", stream_id)
        return True, "", True

    # ============================================================
    # 管理员命令：/mpj delete
    # ============================================================

    @Command(
        "mpj_delete",
        description="删除绘图笔记",
        pattern=r"^/mpj\s+delete\s+(.+)$",
    )
    async def handle_cmd_delete(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, bool]:
        user_id = str(kwargs.get("user_id", "") or "").strip()
        if not self._is_admin(user_id):
            return True, "", False

        matched_groups = kwargs.get("matched_groups", {})
        raw = str(matched_groups.get(1, "") or "").strip()
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

        count = nb.count_notes()
        await self.ctx.send.text(f"已删除笔记 {note_id}（笔记本: {nb_name}），剩余 {count} 条", stream_id)
        return True, "", True

    # ============================================================
    # 指令参数解析
    # ============================================================

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

    # ============================================================
    # WebUI 服务器
    # ============================================================

    async def _run_web_server(self) -> None:
        """启动嵌入式 aiohttp WebUI 服务器。"""
        try:
            from aiohttp import web
        except ImportError:
            self.ctx.logger.error("aiohttp 未安装，WebUI 无法启动")
            return

        port = int(self.config.web.port)
        password = str(self.config.web.password or "").strip()

        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_get("/", self._web_index)
        app.router.add_get("/api/status", self._web_status)
        app.router.add_get("/api/notes", self._web_notes)
        app.router.add_get("/api/search", self._web_search)
        app.router.add_post("/api/add", self._web_add)
        app.router.add_post("/api/modify", self._web_modify)
        app.router.add_post("/api/delete", self._web_delete)
        app.router.add_post("/api/refresh", self._web_refresh)
        app.router.add_post("/api/rebuild", self._web_rebuild)
        app.router.add_get("/api/dedup/scan", self._web_dedup_scan)
        app.router.add_post("/api/dedup/resolve", self._web_dedup_resolve)
        app.router.add_post("/api/dedup/organize_preview", self._web_organize_preview)
        app.router.add_post("/api/organize_db/plan", self._web_organize_db_plan)
        app.router.add_get("/api/organize_db/plan_status", self._web_organize_db_plan_status)
        app.router.add_post("/api/organize_db/apply", self._web_organize_db_apply)

        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, "0.0.0.0", port)
        await site.start()
        auth_note = f"，密码保护已启用" if password else ""
        self.ctx.logger.info(f"WebUI 已启动: http://0.0.0.0:{port}{auth_note}")

        await asyncio.Event().wait()

    def _web_check_auth(self, request: Any) -> bool:
        """检查 WebUI 请求的密码认证。"""
        password = str(self.config.web.password or "").strip()
        if not password:
            return True
        token = request.query.get("token") or ""
        if not token:
            auth_header = request.headers.get("Authorization", "")
            token = auth_header.replace("Bearer ", "").strip()
        return token == password

    async def _web_index(self, request: Any) -> Any:
        """返回 WebUI HTML 页面，始终返回 HTML，认证由 API 端点处理。"""
        from aiohttp import web

        html_path = Path(__file__).parent / "webui.html"
        if html_path.exists():
            html = html_path.read_text(encoding="utf-8")
        else:
            html = "<html><body><h1>webui.html 未找到</h1></body></html>"
        return web.Response(text=html, content_type="text/html")

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
        results: list[dict[str, Any]] = []
        async with self._lock:
            self._notebooks = self._discover_notebooks()
            for name in sorted(self._notebooks.keys()):
                nb = self._notebooks[name]
                if not nb.has_source:
                    continue
                try:
                    stats = await self._rebuild_notebook(nb, force_full=force_full)
                    results.append({"notebook": name, **stats})
                except Exception as exc:
                    results.append({"notebook": name, "error": str(exc)})
        return web.json_response({"results": results})

    # ============================================================
    # WebUI 去重 API
    # ============================================================

    async def _web_dedup_scan(self, request: Any) -> Any:
        """扫描指定笔记本中的语义重复组。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        nb_name = str(request.query.get("notebook", "default") or "default").strip()
        threshold = 0.92
        try:
            threshold = float(request.query.get("threshold", 0.92))
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

    @staticmethod
    def _scan_duplicates(
        entries: list[dict[str, Any]], embeddings: np.ndarray, threshold: float
    ) -> list[dict[str, Any]]:
        """按余弦相似度对条目做贪心聚类，返回重复组列表。"""
        # L2 归一化后计算 N×N 相似度矩阵
        emb_f32 = embeddings.astype(np.float32)
        norms = np.linalg.norm(emb_f32, axis=1, keepdims=True)
        normalized = emb_f32 / np.where(norms > 1e-8, norms, 1.0)
        sim_matrix = normalized @ normalized.T
        np.fill_diagonal(sim_matrix, 0.0)

        # 贪心聚类：相似度 >= threshold 的条目归入同组
        visited: set[int] = set()
        groups: list[dict[str, Any]] = []
        for i in range(len(entries)):
            if i in visited:
                continue
            group_indices = [i]
            for j in range(i + 1, len(entries)):
                if j in visited:
                    continue
                if sim_matrix[i][j] >= threshold:
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
        """执行去重处理：direct 直接合并 / organize LLM 整理，处理后重建索引并重扫。"""
        from aiohttp import web

        if not self._web_check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        body = await self._web_read_body(request)
        nb_name = str(body.get("notebook", "") or "").strip() or "default"
        mode = str(body.get("mode", "direct") or "").strip() or "direct"
        threshold = 0.92
        try:
            threshold = float(body.get("threshold", 0.92))
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

            if mode == "organize":
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
            else:
                # direct：保留指定条目，合并其余备注，删除其余
                keep_id = str(body.get("keep_id", "") or "").strip()
                delete_ids_raw = body.get("delete_ids", [])
                if not isinstance(delete_ids_raw, list):
                    delete_ids_raw = []
                delete_ids = {str(d or "").strip() for d in delete_ids_raw if str(d or "").strip()}
                if not keep_id or not delete_ids:
                    return web.json_response({"error": "keep_id 和 delete_ids 不能为空"}, status=400)

                merged_notes: list[str] = []
                indices_to_delete: set[int] = set()
                keep_idx = None
                for i, entry in enumerate(entries):
                    if entry.get("id") == keep_id:
                        keep_idx = i
                    elif entry.get("id") in delete_ids:
                        indices_to_delete.add(i)
                        if entry.get("note"):
                            merged_notes.append(entry["note"])
                if keep_idx is None:
                    return web.json_response({"error": "未找到 keep_id 对应的条目"}, status=404)
                if not indices_to_delete:
                    return web.json_response({"error": "未找到要删除的条目"}, status=400)

                if merged_notes:
                    existing_note = entries[keep_idx].get("note", "")
                    parts = [p for p in [existing_note] + merged_notes if p.strip()]
                    entries[keep_idx]["note"] = " | ".join(parts)

                final_entries = [e for i, e in enumerate(entries) if i not in indices_to_delete]

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

    # ============================================================
    # HomeCard 组件
    # ============================================================

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

    # ============================================================
    # 向量搜索
    # ============================================================

    async def _search_single_notebook(
        self,
        nb: Notebook,
        query_text: str,
        query_vec: np.ndarray,
        top_k: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """搜索单个笔记本。"""
        if not nb.check_consistency():
            self.ctx.logger.warning(f"笔记本 '{nb.name}' 索引失效，已跳过")
            return []

        entries = nb.load_notes()
        if not entries:
            return []

        embeddings = nb.load_embeddings()
        if embeddings is None or len(embeddings) != len(entries):
            self.ctx.logger.warning(f"笔记本 '{nb.name}' 向量数量与条目不一致，已跳过")
            return []

        results = self._cosine_topk_boosted(query_text, query_vec, embeddings, entries, top_k, min_score)
        for r in results:
            r["notebook"] = nb.name
        return results

    async def _search_all_notebooks(
        self,
        query_text: str,
        query_vec: np.ndarray,
        top_k: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """搜索所有一致的笔记本，合并结果。"""
        all_results: list[dict[str, Any]] = []

        for name in sorted(self._notebooks.keys()):
            nb = self._notebooks[name]
            if not nb.has_source:
                continue
            if not nb.check_consistency():
                continue
            results = await self._search_single_notebook(nb, query_text, query_vec, top_k, min_score)
            all_results.extend(results)

        all_results.sort(key=lambda x: x["score"], reverse=True)
        return all_results[:top_k]

    @staticmethod
    def _compute_text_boost(query_lower: str, en_lower: str, zh_lower: str) -> float:
        """根据文本匹配规则计算加分（0~0.30）。

        优先级递减，取最高一条：
          Rule 1  query == en 或 query == zh          → +0.30
          Rule 2  en/zh（≥2字符）是 query 的子串       → +0.25
          Rule 3  query（≥2字符）是 en/zh 的子串       → +0.15
          Rule 4  英文词重叠 + 中文字符重叠             → +0.05×数（上限+0.15）
        """
        if not query_lower:
            return 0.0

        # Rule 1: 精确匹配
        if query_lower == en_lower or query_lower == zh_lower:
            return 0.30

        # Rule 2: 标签名出现在查询中（处理多关键词 / 长句）
        if en_lower and len(en_lower) >= 2 and en_lower in query_lower:
            return 0.25
        if zh_lower and len(zh_lower) >= 2 and zh_lower in query_lower:
            return 0.25

        # Rule 3: 查询出现在标签名中（处理部分关键词）
        if len(query_lower) >= 2 and (query_lower in en_lower or query_lower in zh_lower):
            return 0.15

        # Rule 4: token 级重叠（英文词 + 中文字符）
        query_words = set(query_lower.split())
        en_words = set(en_lower.split())
        word_overlap = len(query_words & en_words)

        query_cjk = {c for c in query_lower if "\u4e00" <= c <= "\u9fff"}
        zh_cjk = {c for c in zh_lower if "\u4e00" <= c <= "\u9fff"}
        cjk_overlap = len(query_cjk & zh_cjk)

        total_overlap = word_overlap + cjk_overlap
        if total_overlap > 0:
            return min(0.15, 0.05 * total_overlap)

        return 0.0

    def _cosine_topk_boosted(
        self,
        query_text: str,
        query_vec: np.ndarray,
        embeddings: np.ndarray,
        entries: list[dict[str, Any]],
        top_k: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        """余弦相似度搜索 + 精确匹配加分，返回 top-k 结果。

        1. 向量搜索取候选（放宽阈值，扩大候选池）
        2. 对候选用 query 做本地文本匹配加分
        3. 重新排序 → 应用原始阈值 → 取 top_k
        """
        query_lower = query_text.lower().strip()

        emb_f32 = embeddings.astype(np.float32)
        norms = np.linalg.norm(emb_f32, axis=1)
        query_norm = np.linalg.norm(query_vec)

        safe_norms = np.where(norms > 1e-8, norms, 1.0)
        safe_query_norm = query_norm if query_norm > 1e-8 else 1.0

        base_scores = (emb_f32 @ query_vec) / (safe_norms * safe_query_norm)

        # 放宽阈值扩大候选池
        relaxed_threshold = min_score * 0.5
        valid_mask = base_scores >= relaxed_threshold
        if not np.any(valid_mask):
            return []

        valid_indices = np.where(valid_mask)[0]

        # 加分 + 重排
        scored: list[tuple[int, float]] = []
        for idx in valid_indices:
            i = int(idx)
            base = float(base_scores[i])
            entry = entries[i]
            boost = self._compute_text_boost(
                query_lower,
                entry["en"].lower().strip(),
                entry["zh"].lower().strip(),
            )
            final_score = min(1.0, base + boost)
            if final_score >= min_score:
                scored.append((i, final_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        scored = scored[:top_k]

        results: list[dict[str, Any]] = []
        for i, score in scored:
            entry = entries[i]
            results.append(
                {
                    "id": entry["id"],
                    "en": entry["en"],
                    "zh": entry["zh"],
                    "note": entry["note"],
                    "score": round(score, 4),
                }
            )
        return results

    # ============================================================
    # 索引重建（增量）
    # ============================================================

    async def _rebuild_notebook(self, nb: Notebook, force_full: bool = False) -> dict[str, int]:
        """对单个笔记本执行增量重建；force_full=True 时忽略缓存全量重嵌。"""
        current_entries = nb.load_notes()
        cache_entries = nb.load_cache_notes()
        existing_embeddings = nb.load_embeddings()

        reuse_vectors: list[np.ndarray] = []
        need_build_indices: list[int] = []

        if force_full:
            need_build_indices = list(range(len(current_entries)))
            reuse_vectors = [np.zeros(0, dtype=np.float32)] * len(current_entries)
        else:
            # 构建缓存映射: content_hash → 向量
            cache_map: dict[str, np.ndarray] = {}
            if cache_entries and existing_embeddings is not None:
                for i, entry in enumerate(cache_entries):
                    if i >= len(existing_embeddings):
                        break
                    chash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])
                    cache_map[chash] = existing_embeddings[i].astype(np.float32)

            # 遍历当前条目，区分复用和待建
            for i, entry in enumerate(current_entries):
                chash = self._compute_content_hash(entry["en"], entry["zh"], entry["note"])
                if chash in cache_map:
                    reuse_vectors.append(cache_map[chash])
                else:
                    reuse_vectors.append(np.zeros(0, dtype=np.float32))
                    need_build_indices.append(i)

            # 维度安全检查：如果全部命中缓存，做一次探测 embed 验证维度
            if not need_build_indices and reuse_vectors:
                test_text = self._build_embedding_text(
                    current_entries[0]["en"], current_entries[0]["zh"], current_entries[0]["note"]
                )
                test_vec = await self._embed_single(test_text)
                if test_vec is not None and len(test_vec) != len(reuse_vectors[0]):
                    self.ctx.logger.warning(
                        f"笔记本 '{nb.name}' 向量维度已变更 "
                        f"(旧={len(reuse_vectors[0])}, 新={len(test_vec)})，强制全量重建"
                    )
                    need_build_indices = list(range(len(current_entries)))
                    reuse_vectors = [np.zeros(0, dtype=np.float32)] * len(current_entries)

        # 批量 embed 待建条目
        rebuilt_count = 0
        if need_build_indices:
            build_texts = [
                self._build_embedding_text(
                    current_entries[i]["en"], current_entries[i]["zh"], current_entries[i]["note"]
                )
                for i in need_build_indices
            ]
            new_embeddings = await self._embed_batch(build_texts)
            if new_embeddings is None:
                raise RuntimeError("embedding 服务不可用，无法完成重建")

            for j, idx in enumerate(need_build_indices):
                reuse_vectors[idx] = new_embeddings[j]
            rebuilt_count = len(need_build_indices)

        # 组装最终矩阵
        if reuse_vectors:
            dim = len(reuse_vectors[0])
            final_matrix = np.zeros((len(reuse_vectors), dim), dtype=np.float16)
            for i, vec in enumerate(reuse_vectors):
                final_matrix[i] = vec.astype(np.float16)
        else:
            final_matrix = np.zeros((0, 1), dtype=np.float16)

        np.save(nb.embeddings_path, final_matrix)

        # 重写 cache jsonl
        nb.rewrite_cache(current_entries)

        # 更新 meta
        nb.save_meta(
            {
                "md5": nb.compute_file_md5(),
                "count": len(current_entries),
                "built_at": time.time(),
            }
        )

        reused_count = len(current_entries) - rebuilt_count
        return {
            "total": len(current_entries),
            "reused": reused_count,
            "rebuilt": rebuilt_count,
        }

    # ============================================================
    # 辅助方法
    # ============================================================

    def _is_admin(self, user_id: str) -> bool:
        admin_users = self.config.admin.users or []
        if not admin_users:
            return False
        return user_id in admin_users

    @staticmethod
    def _build_embedding_text(en: str, zh: str, note: str) -> str:
        """拼接用于 embedding 的完整文本。"""
        parts = [en, zh]
        if note.strip():
            parts.append(note)
        return " ".join(parts)

    @staticmethod
    def _compute_content_hash(en: str, zh: str, note: str) -> str:
        """计算笔记内容哈希，用于增量重建比对。"""
        raw = f"{en}\x00{zh}\x00{note}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    async def _embed_single(self, text: str) -> np.ndarray | None:
        """对单条文本调用 embedding。"""
        try:
            result = await self.ctx.llm.embed(text=text)
        except Exception as exc:
            self.ctx.logger.error(f"embedding 调用失败: {exc}")
            return None
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error", "unknown") if isinstance(result, dict) else result
            self.ctx.logger.warning(f"embedding 返回失败: {error}")
            return None
        vec = result.get("embedding")
        if not isinstance(vec, list) or not vec:
            return None
        return np.asarray(vec, dtype=np.float32)

    async def _embed_batch(self, texts: list[str]) -> np.ndarray | None:
        """对多条文本批量调用 embedding。"""
        max_concurrent = int(self.config.journal.embed_max_concurrent)
        try:
            result = await self.ctx.llm.embed(texts=texts, max_concurrent=max_concurrent)
        except Exception as exc:
            self.ctx.logger.error(f"批量 embedding 调用失败: {exc}")
            return None
        if not isinstance(result, dict) or not result.get("success"):
            error = result.get("error", "unknown") if isinstance(result, dict) else result
            self.ctx.logger.warning(f"批量 embedding 返回失败: {error}")
            return None
        items = result.get("results")
        if not isinstance(items, list) or len(items) != len(texts):
            actual = len(items) if isinstance(items, list) else 0
            self.ctx.logger.warning(f"批量 embedding 结果数量不匹配: 期望 {len(texts)}，实际 {actual}")
            return None
        vectors = []
        for item in items:
            vec = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vec, list) or not vec:
                return None
            vectors.append(vec)
        return np.asarray(vectors, dtype=np.float32)

    # ============================================================
    # LLM 整理
    # ============================================================

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

        try:
            timeout = ClientTimeout(total=max(5, int(cfg.timeout if cfg.timeout is not None else 120)))
            async with ClientSession(timeout=timeout) as session:
                async with session.post(url, json=request_body, headers=headers) as resp:
                    status = resp.status
                    resp_body = await resp.json()
        except aiohttp.ClientError as exc:
            self.ctx.logger.error(f"LLM 直连 HTTP 请求失败: {exc}")
            return {"success": False, "error": f"LLM API 请求失败: {exc}"}
        except Exception as exc:
            self.ctx.logger.error(f"LLM 直连请求异常: {exc}", exc_info=True)
            return {"success": False, "error": f"LLM API 请求异常: {exc}"}

        if status != 200:
            self.ctx.logger.error(f"LLM API 返回错误: status={status} body={str(resp_body)[:300]}")
            return {"success": False, "error": f"LLM API 返回错误({status}): {resp_body}"}

        try:
            choice = resp_body["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            self.ctx.logger.error(f"LLM API 响应缺少 choices: {str(resp_body)[:300]}")
            return {"success": False, "error": "LLM API 响应格式异常"}

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
        }

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

        system_prompt = str(cfg.system_prompt or "").strip() or _DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT
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

        payload = self._extract_json(response_text)
        if payload is None:
            self.ctx.logger.warning(f"LLM 整理返回无法解析的 JSON: {response_text[:200]}")
            return {"_error": "llm", "message": f"LLM 返回内容无法解析为 JSON，完整输出：\n{response_text}"}

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

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """从 LLM 回复中提取第一个 JSON 对象。"""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[len("json") :].lstrip()
            if stripped.startswith("{"):
                stripped = stripped.strip("`")
        start = stripped.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(stripped[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    # ============================================================
    # LLM 整理数据库（agent 循环 + search_notes 工具）
    # ============================================================

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
            payload = self._extract_json(response_text)
            if payload is None:
                self.ctx.logger.warning(f"LLM 操作数据库返回无法解析的 JSON: {response_text[:200]}")
                return None, messages, f"LLM 返回内容无法解析为 JSON，完整输出：\n{response_text}"
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
            system_prompt = str(cfg.system_prompt or "").strip() or _ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT
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


# ============================================================
# 工厂函数
# ============================================================


def create_plugin() -> PromptJournalPlugin:
    """创建麦麦的绘图笔记本插件实例。"""
    return PromptJournalPlugin()
