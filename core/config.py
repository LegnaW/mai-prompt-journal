"""插件配置模型。"""

from typing import Literal

from maibot_sdk import Field, PluginConfigBase

from .constants import (
    _BATCH_IMPORT_DEFAULT_PROMPT,
    _DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT,
    _DEDUP_SCAN_BLOCK,
    _ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT,
)

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
        default="2.3.1",
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
    dedup_check_enabled: bool = Field(
        default=True,
        description="add/modify 写入前是否做重复检测",
        json_schema_extra={
            "label": "写入去重检测",
            "hint": "add/modify 写入前检测是否与已有笔记重复，重复则拒绝写入（LLM 工具直接拒绝；/mpj 指令需 /mpj confirm 确认）",
            "order": 4,
        },
    )
    dedup_check_all_notebooks: bool = Field(
        default=False,
        description="去重检测是否检测所有笔记本",
        json_schema_extra={
            "label": "检测所有笔记本",
            "hint": "开启则跨所有笔记本检测重复；关闭只检测目标笔记本",
            "order": 5,
        },
    )
    dedup_check_threshold: float = Field(
        default=0.85,
        ge=0.5,
        le=0.99,
        description="写入去重检测相似度阈值",
        json_schema_extra={
            "label": "去重检测阈值",
            "hint": "相似度超过该值的笔记会被判为重复（与 WebUI 去重阈值口径一致）",
            "order": 6,
        },
    )
    dedup_scan_block: int = Field(
        default=_DEDUP_SCAN_BLOCK,
        ge=16,
        le=4096,
        description="去重扫描相似度矩阵计算的分块行数（已迁移至 [advanced]，此字段仅用于旧配置迁移）",
        json_schema_extra={
            "label": "去重扫描分块大小（已废弃）",
            "hint": "已迁移到『高级』分类下的『去重扫描分块大小』，此字段仅供旧配置自动迁移，请勿直接填写",
            "order": 7,
            "hidden": True,
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
        json_schema_extra={"label": "WebUI 访问密码", "hint": "访问密码，留空则无密码保护；绑定非回环地址时必须设置", "order": 2},
    )
    bind: str = Field(
        default="127.0.0.1",
        description="监听地址",
        json_schema_extra={
            "label": "监听地址",
            "hint": "默认 127.0.0.1 仅本机可访问；改为 0.0.0.0 对外暴露时必须设置密码，否则所有请求返回安全警告页",
            "order": 3,
        },
    )

class DedupMergeConfig(PluginConfigBase):
    """去重 LLM 整理参数。"""

    __ui_label__ = "去重整理"
    __ui_icon__ = "wand"
    __ui_order__ = 5

    enabled: bool = Field(
        default=True,
        description="是否启用 LLM 整理",
        json_schema_extra={"label": "启用 LLM 整理", "hint": "是否在去重扫描中启用 LLM 整理功能", "order": 0},
    )
    system_prompt: str = Field(
        default=_DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT,
        description="整理系统提示词，留空用内置默认（已迁移至 [advanced]，此字段仅用于旧配置迁移）",
        json_schema_extra={
            "label": "整理系统提示词（已废弃）",
            "hint": "已迁移到『高级』分类下的『去重整理系统提示词』，此字段仅供旧配置自动迁移，请勿直接填写",
            "order": 1,
            "hidden": True,
            "x-widget": "textarea",
            "rows": 8,
        },
    )

class OrganizeDbConfig(PluginConfigBase):
    """LLM 操作数据库参数。"""

    __ui_label__ = "操作数据库"
    __ui_icon__ = "database"
    __ui_order__ = 6

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
        default=_ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT,
        description="操作数据库系统提示词，留空用内置默认（已迁移至 [advanced]，此字段仅用于旧配置迁移）",
        json_schema_extra={
            "label": "操作数据库系统提示词（已废弃）",
            "hint": "已迁移到『高级』分类下的『操作数据库系统提示词』，此字段仅供旧配置自动迁移，请勿直接填写",
            "order": 3,
            "hidden": True,
            "x-widget": "textarea",
            "rows": 8,
        },
    )
    batch_import_prompt: str = Field(
        default=_BATCH_IMPORT_DEFAULT_PROMPT,
        description="txt 批量写入追加提示词（已迁移至 [advanced]，此字段仅用于旧配置迁移）",
        json_schema_extra={
            "label": "txt 批量写入追加提示词（已废弃）",
            "hint": "已迁移到『高级』分类下的『txt 批量写入追加提示词』，此字段仅供旧配置自动迁移，请勿直接填写",
            "order": 4,
            "hidden": True,
            "x-widget": "textarea",
            "rows": 4,
        },
    )

class DirectLlmConfig(PluginConfigBase):
    """直连 LLM API 配置（独立于麦麦自带 LLM，必填）。"""

    __ui_label__ = "LLM 直连"
    __ui_icon__ = "api"
    __ui_order__ = 4

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

class AdvancedConfig(PluginConfigBase):
    """高级配置：默认情况下不建议修改，请确认理解用途后再调整。"""

    __ui_label__ = "高级"
    __ui_icon__ = "settings"
    __ui_order__ = 7

    dedup_merge_system_prompt: str = Field(
        default=_DEDUP_MERGE_DEFAULT_SYSTEM_PROMPT,
        description="去重整理系统提示词，留空用内置默认",
        json_schema_extra={
            "label": "去重整理系统提示词",
            "hint": "WebUI 去重『LLM 整理』使用的系统提示词，留空使用内置默认（LLM 由 [llm] 节配置）。默认不建议修改",
            "order": 0,
            "x-widget": "textarea",
            "rows": 8,
        },
    )
    organize_db_system_prompt: str = Field(
        default=_ORGANIZE_DB_DEFAULT_SYSTEM_PROMPT,
        description="操作数据库系统提示词，留空用内置默认",
        json_schema_extra={
            "label": "操作数据库系统提示词",
            "hint": "WebUI 『操作数据库』使用的系统提示词，留空使用内置默认（LLM 由 [llm] 节配置）。默认不建议修改",
            "order": 1,
            "x-widget": "textarea",
            "rows": 8,
        },
    )
    batch_import_prompt: str = Field(
        default=_BATCH_IMPORT_DEFAULT_PROMPT,
        description="txt 批量写入追加提示词（追加在操作数据库系统提示词之后）",
        json_schema_extra={
            "label": "txt 批量写入追加提示词",
            "hint": "txt 批量写入时追加在系统提示词后的约束文本；{temp-journal} 会被替换为临时笔记本名。一般情况下请勿乱动此项目。",
            "order": 2,
            "x-widget": "textarea",
            "rows": 4,
        },
    )
    dedup_scan_block: int = Field(
        default=_DEDUP_SCAN_BLOCK,
        ge=16,
        le=4096,
        description="去重扫描相似度矩阵计算的分块行数",
        json_schema_extra={
            "label": "去重扫描分块大小",
            "hint": "去重扫描相似度计算的分块行数，影响内存占用（越小越省内存），一般不需要修改",
            "order": 3,
        },
    )


class BackupConfig(PluginConfigBase):
    """笔记本自动备份配置。"""

    __ui_label__ = "备份"
    __ui_icon__ = "archive"
    __ui_order__ = 8

    enabled: bool = Field(
        default=True,
        description="每次修改笔记本后自动创建备份",
        json_schema_extra={
            "label": "启用自动备份",
            "hint": "每次笔记本被修改后按时间戳创建一份 jsonl 备份，可在『备份』页或 /mpj backup 指令查看/恢复/删除",
            "order": 0,
        },
    )
    max_per_notebook: int = Field(
        default=6,
        ge=1,
        le=200,
        description="每个笔记本保留的备份上限",
        json_schema_extra={
            "label": "备份上限",
            "hint": "每个笔记本最多保留的备份份数，超出后自动删除最旧备份",
            "order": 1,
            "x-widget": "number",
        },
    )


class TxtImportConfig(PluginConfigBase):
    """txt 批量写入的重试与失败处理。"""

    __ui_label__ = "txt 批量写入"
    __ui_icon__ = "file-text"
    __ui_order__ = 9

    max_retries: int = Field(
        default=3,
        ge=0,
        le=20,
        description="每个段失败后的最大重试次数",
        json_schema_extra={
            "label": "失败最大重试",
            "hint": "单个段处理失败后最多重试几次（含 API 层的瞬时失败兜底），0 表示不额外重试",
            "order": 0,
            "x-widget": "number",
        },
    )
    on_failure: Literal["interrupt", "skip"] = Field(
        default="interrupt",
        description="段重试仍失败后的行为",
        json_schema_extra={
            "label": "失败后行为",
            "hint": "interrupt=中断整个导入并缓存进度，可在导入页选择再次尝试或取消；skip=跳过该段并记录到失败列表，继续处理下一段",
            "order": 1,
        },
    )


class FileIOConfig(PluginConfigBase):
    """笔记本导入/导出（jsonl / mpj）的重试与失败处理。"""

    __ui_label__ = "导入 / 导出"
    __ui_icon__ = "file-import"
    __ui_order__ = 10

    max_retries: int = Field(
        default=3,
        ge=0,
        le=20,
        description="每个条目失败后的最大重试次数",
        json_schema_extra={
            "label": "失败最大重试",
            "hint": "单个条目 embed 失败后最多重试几次（含 API 层的瞬时失败兜底），0 表示不额外重试",
            "order": 0,
            "x-widget": "number",
        },
    )
    on_failure: Literal["interrupt", "skip"] = Field(
        default="interrupt",
        description="条目重试仍失败后的行为",
        json_schema_extra={
            "label": "失败后行为",
            "hint": "interrupt=中断整个导入/导出并缓存进度，可在传输状态区选择再次尝试或取消；skip=跳过失败条目继续（导出 mpj 时失败条目会被丢弃，仅导出成功子集）",
            "order": 1,
        },
    )


class PromptJournalConfig(PluginConfigBase):
    """麦麦的绘图笔记本配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    llm: DirectLlmConfig = Field(default_factory=DirectLlmConfig)
    dedup_merge: DedupMergeConfig = Field(default_factory=DedupMergeConfig)
    organize_db: OrganizeDbConfig = Field(default_factory=OrganizeDbConfig)
    advanced: AdvancedConfig = Field(default_factory=AdvancedConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    txt_import: TxtImportConfig = Field(default_factory=TxtImportConfig)
    file_io: FileIOConfig = Field(default_factory=FileIOConfig)
