"""API 调用重试兜底。

对 embedding / LLM 直连等外部 API 调用提供"瞬时失败重试 + 超时"：
- `is_transient_error`：从错误文本判断是否属于可重试的瞬时失败
  （网络抖动、超时、限流 429、5xx、连接异常等）；业务错误（配置错/维度错/格式错）不重试。
- `run_with_retry`：对 `attempt()` 包装，指数退避重试，最后返回 `(result, error)`。

与任务级配置（`[txt_import]` / `[file_io]` 的 `max_retries` / `on_failure`）分层：
- 本模块的 `_API_RETRIES` 是"单次 API 调用"的兜底重试（硬编码，所有调用方受益）；
- 任务级 `max_retries` 是"单个条目/段"的整块重试，随后按 `on_failure` 中断或跳过。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

# 单次 API 调用的最大尝试次数（含首次），瞬时限流/5xx/网络异常会指数退避重试
_API_RETRIES = 3
# 重试基础退避（秒），指数增长：1.0 → 2.0 → 4.0
_API_BACKOFF = 1.0
# 内置 embedding 单次调用超时（秒）。现状宿主 ctx.llm.embed 无超时，宿主卡住会永久挂起，
# 这里统一用 wait_for 兜底。
_API_EMBED_TIMEOUT = 60


def scaled_batch_timeout(texts: int, concurrent: int, per_text: float = 30.0, base: float = 30.0) -> float:
    """批量请求的超时（秒）：随条数/并发缩放。

    大批次（如整库重建）若用固定小超时会被误杀；这里按"批次数"估算时长并加上限，
    既给挂起设一个上限，又不误伤大任务。concurrent 为宿主内部并发数。
    """
    batches = max(1, (int(texts) + max(1, int(concurrent)) - 1) // max(1, int(concurrent)))
    return base + per_text * batches

_TRANSIENT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\btimeout\b",
        r"timed\s*out",
        r"\b429\b",
        r"\(5\d\d\)",  # (503) 等括号内的状态码
        r"status\s*[=:\s]+\s*5\d\d",  # status=503 / status: 503
        r"\b5\d\d\b\s*[,:]",  # 503, message=...（aiohttp 异常文本）
        r"\bconnection\b",
        r"\breset\b",
        r"\brefused\b",
        r"network",
        r"unavailable",
        r"server\s*(disconnect|down|unreachable)",
        r"rate\s*limit",
        r"too\s*many\s*requests",
        r"overloaded",
        r"busy",
        r"超时",
        r"网络",
        r"连接",
        r"限流",
        r"繁忙",
        r"暂时",
    )
]


def is_transient_error(text: Any) -> bool:
    """判断错误文本是否属于可重试的瞬时失败。"""
    t = str(text or "")
    return any(p.search(t) for p in _TRANSIENT_PATTERNS)


async def run_with_retry(
    attempt: Callable[[], Awaitable[tuple[Any, str | None]]],
    max_retries: int = _API_RETRIES,
    base_backoff: float = _API_BACKOFF,
    timeout: float | None = None,
    label: str = "",
    logger: Any = None,
) -> tuple[Any, str | None]:
    """对 attempt() 做指数退避重试。

    attempt() 必须返回 `(result, error)`：error 为 None 视为成功。
    超时/异常会自动折叠成 `(None, 错误文本)`；仅瞬时失败会重试（最多 max_retries 次）。
    返回最后一次 `(result, error)`，不抛异常。
    """
    max_retries = max(1, int(max_retries or 1))
    last: tuple[Any, str | None] = (None, None)
    for i in range(max_retries):
        try:
            if timeout:
                last = await asyncio.wait_for(attempt(), timeout=timeout)
            else:
                last = await attempt()
        except asyncio.TimeoutError as exc:
            last = (None, f"{label}请求超时（{timeout}s）：{exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = (None, f"{label}请求异常：{exc}")
        result, error = last
        if error is None:
            return last
        if i < max_retries - 1 and is_transient_error(error):
            delay = base_backoff * (2 ** i)
            if logger is not None:
                logger.warning(
                    f"{label}调用失败（{str(error)[:120]}），{delay:.1f}s 后重试（{i + 1}/{max_retries - 1}）"
                )
            await asyncio.sleep(delay)
        else:
            return last
    return last


async def run_task_item(
    factory: Callable[[], Awaitable[tuple[Any, str | None]]],
    max_retries: int = 3,
    base_backoff: float = 1.0,
    label: str = "",
    logger: Any = None,
) -> tuple[Any, str | None]:
    """任务条目级重试：对单个条目（txt 段 / 导入导出条目）整体重试。

    factory() 必须返回 `(result, error)`：error 为 None 视为成功。
    与 `run_with_retry` 不同，这里**不论是否瞬时**都重试 max_retries 次（含首次），
    对应 `[txt_import]` / `[file_io]` 的 `max_retries` 配置（0 表示只尝试一次）。
    返回最后一次 `(result, error)`，不抛异常。
    """
    max_retries = max(1, int(max_retries or 1))
    last: tuple[Any, str | None] = (None, "未知错误")
    for i in range(max_retries):
        try:
            last = await factory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last = (None, str(exc))
        result, error = last
        if error is None:
            return last
        if i < max_retries - 1:
            delay = base_backoff * (2 ** i)
            if logger is not None:
                logger.warning(
                    f"{label}失败（{str(error)[:120]}），{delay:.1f}s 后重试（{i + 1}/{max_retries - 1}）"
                )
            await asyncio.sleep(delay)
    return last
