"""LLM 输出 JSON 的宽容解析工具。

模型偶尔会输出不合规 JSON：字符串内真实换行/控制字符、尾逗号、单引号、
被散文或代码围栏包裹、甚至带开头文本里的假花括号。本模块用状态机提取
候选 JSON 对象，再按多级修复逐个尝试，尽量还原出合法 dict。

解析入口 ``parse_lenient_json`` 返回 ``(payload, reason)``：
  - payload 非 None：解析成功，reason 为 None；
  - 失败：reason 为 ``"no_json"`` / ``"truncated"`` / ``"parse_failed"``，
    供上层给出更精确的提示（如"疑似被截断"）。
"""

import json
from typing import Any

_JSON_ESCAPES = set('"\\/bfnrt')


def extract_json_candidates(text: str) -> list[str]:
    """返回所有"从 { 到配对 }"的候选子串（按出现顺序）。

    状态机正确跳过字符串内的 { } 与转义，因此字符串里的花括号不会干扰；
    散文里出现的假 { 会生成解析失败的候选，由调用方逐个尝试。
    若某个 { 之后没有配对的 }（如被截断），后续不可能再有平衡对象，直接停止。
    """
    candidates: list[str] = []
    start = 0
    while True:
        i = text.find("{", start)
        if i == -1:
            break
        depth = 0
        in_string = False
        escape = False
        j = i
        balanced = False
        while j < len(text):
            ch = text[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[i : j + 1])
                    balanced = True
                    break
            j += 1
        if not balanced:
            break
        start = i + 1
    return candidates


def strip_trailing_commas(sub: str) -> str:
    """去掉字符串外的尾逗号（'}' 或 ']' 前多余的逗号）。"""
    out: list[str] = []
    in_string = False
    escape = False
    for ch in sub:
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        elif ch in ("}", "]") and out and out[-1] == ",":
            out.pop()
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def fix_invalid_escapes(sub: str) -> str:
    """把字符串内的非法转义修正为合法：\\ 后跟非合法转义字符时补一个 \\。"""
    out: list[str] = []
    in_string = False
    escape = False
    for ch in sub:
        if escape:
            if ch not in _JSON_ESCAPES:
                out.append("\\")
            out.append(ch)
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
        else:
            out.append(ch)
    return "".join(out)


def fix_single_quotes(sub: str) -> str:
    """把单引号 JSON 转成双引号 JSON（最后兜底）。

    扫描时跟踪当前字符串定界符（' 或 "），把 ' 定界符换成 "，
    并把原来单引号字符串内的裸 " 转义为 \\"。对已经是合法双引号的 JSON，
    该函数基本是恒等变换（保留字符串内普通 ' 与 \\ 转义），因此作为兜底安全。
    """
    out: list[str] = []
    in_string = False
    quote = ""
    escape = False
    for ch in sub:
        if escape:
            out.append(ch)
            escape = False
            continue
        if in_string:
            if ch == "\\":
                out.append(ch)
                escape = True
                continue
            if ch == quote:
                out.append('"')
                in_string = False
                quote = ""
                continue
            if ch == '"' and quote == "'":
                out.append('\\"')
                continue
            out.append(ch)
            continue
        if ch in ('"', "'"):
            out.append('"')
            quote = ch
            in_string = True
            continue
        out.append(ch)
    return "".join(out)


def _try_loads(sub: str) -> dict[str, Any] | None:
    """对单个候选按修复级联尝试解析，返回 dict 或 None。"""
    variants = [
        sub,
        strip_trailing_commas(sub),
        fix_invalid_escapes(sub),
        fix_single_quotes(sub),
        fix_invalid_escapes(fix_single_quotes(sub)),
    ]
    for variant in variants:
        try:
            # strict=False：允许字符串内未转义的控制字符（真实换行等）
            obj = json.loads(variant, strict=False)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_lenient_json(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """宽容解析 LLM 输出的 JSON 对象。

    Args:
        text: LLM 原始回复文本。

    Returns:
        (payload, reason)：成功时 payload 为 dict、reason 为 None；
        失败时 payload 为 None，reason ∈ {"no_json", "truncated", "parse_failed"}。
    """
    stripped = text.strip()
    if "{" not in stripped:
        return None, "no_json"

    candidates = extract_json_candidates(stripped)
    if not candidates:
        # 有 { 但没有任何配对的 } → 疑似被截断
        return None, "truncated"

    for sub in candidates:
        payload = _try_loads(sub)
        if payload is not None:
            return payload, None

    return None, "parse_failed"
