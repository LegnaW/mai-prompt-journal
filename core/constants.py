"""模块级常量：LLM 提示词、工具名、WebUI 阈值等。"""

from pathlib import Path

# 插件根目录与 WebUI 静态资源目录（本模块位于 core/ 下，需向上两级）
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_WEB_DIR = _PLUGIN_ROOT / "web"

_ORGANIZE_DEFAULT_REQUIREMENT = "把上述重复的提示词笔记合并整理，默认输出一条合并结果，若有需要也可以输出2到3条"

_WRITE_TOOL_NAMES = ["add_aidraw_notes", "modify_aidraw_note", "delete_aidraw_note"]

_WEBUI_SESSION_TTL = 7 * 24 * 3600

_DEDUP_SCAN_BLOCK = 256

_BATCH_IMPORT_DEFAULT_PROMPT = "你只能写入/删除/修改`{temp-journal}`中的内容，不要尝试动其他的笔记本。"

_WEBUI_WARNING_HTML = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WebUI 未安全配置</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f0f2f5; color: #333;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 16px; }
  .box { max-width: 640px; background: #fff; border-radius: 12px; padding: 28px;
         box-shadow: 0 2px 8px rgba(0,0,0,.1); }
  h1 { font-size: 1.3em; color: #c62828; margin-bottom: 14px; }
  p, li { line-height: 1.8; font-size: 15px; }
  code { background: #f5f5f5; padding: 2px 6px; border-radius: 4px; font-size: 14px; }
  ul { margin: 10px 0 0 20px; }
  .tip { margin-top: 16px; padding: 10px 14px; background: #fff8e1; border-radius: 8px; font-size: 14px; color: #795548; }
</style>
</head>
<body>
  <div class="box">
    <h1>⚠️ WebUI 未安全配置</h1>
    <p>当前插件的 WebUI 绑定了<strong>非回环地址</strong>（非 127.0.0.1 / localhost），且<strong>未设置访问密码</strong>。
    出于安全考虑，所有请求均被拦截，本页面之外的功能不可用。</p>
    <p>请到<strong>麦麦的插件配置界面</strong>（或直接编辑本插件的 <code>config.toml</code> 的 <code>[web]</code> 节）做以下任意一项修改：</p>
    <ul>
      <li>将 <code>bind</code> 改为 <code>"127.0.0.1"</code>（仅允许本机访问），或</li>
      <li>为 <code>password</code> 设置一个访问密码（需要对局域网/公网暴露时<strong>必须</strong>设置）。</li>
    </ul>
    <div class="tip">修改后请<strong>重启 / 重载插件</strong>，本警告页会自动消失。</div>
  </div>
</body>
</html>"""

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
