"""中断任务的磁盘缓存与断点续跑助手。

被 `on_failure="interrupt"` 中断的长程任务（txt 批量写入 / 笔记本导入导出）把
续跑所需的上下文与已完成的 embedding 进度**缓存到磁盘**（而非仅内存），
因此插件重载后仍可"再次尝试"续跑：

- txt 批量写入：`tmp_import/import.state.json`（段列表 + 当前索引 + 段状态）
- 笔记本导入/导出：`file_io/resume.json`（任务上下文）+ `file_io/partial_emb.npz`
  （已完成条目的向量与索引）

`TaskInterrupted` 由任务内部抛出，任务包装器捕获后置为 `interrupted` 状态。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np

# file_io 目录下的续跑文件
_RESUME_FILE = "resume.json"
_PARTIAL_EMB_FILE = "partial_emb.npz"
# 上一份完整快照的备份（崩溃撕裂时回退用）
_PARTIAL_EMB_BACKUP = "partial_emb.npz.bak"
# 写快照的临时文件（写完 fsync 后原子改名）
_PARTIAL_EMB_TMP = "partial_emb.npz.tmp"

# txt 批量写入目录下的状态文件
_TXT_IMPORT_STATE_FILE = "import.state.json"

# 传输状态机新增的中断状态
STATE_INTERRUPTED = "interrupted"

# 导入/导出 embed 进度的周期落盘间隔（秒）。10s 一次把已完成向量写盘，
# 即使进程被强杀/断电，也最多丢失最后 10s 的进度。
_PROGRESS_FLUSH_INTERVAL = 10


class TaskInterrupted(RuntimeError):
    """任务按 on_failure=interrupt 主动中断（已缓存续跑状态）。"""

    def __init__(self, message: str = "任务已中断") -> None:
        super().__init__(message)


def _fsync_file(path: Path) -> None:
    """对文件执行 fsync，确保数据落盘（断电/崩溃时不被丢失）。"""
    try:
        with open(path, "rb") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    """对目录执行 fsync，确保改名/新增的目录项落盘。"""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def save_json(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON（临时文件 + fsync + 改名，避免半写/断电丢数据）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _fsync_file(tmp)
    tmp.replace(path)
    _fsync_dir(path.parent)


def load_json(path: Path) -> dict[str, Any]:
    """读取 JSON，缺失/损坏返回空 dict。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_embed_progress(base_dir: Path, done_indices: list[int], vectors: np.ndarray) -> None:
    """保存已完成条目的 embedding 进度（原子写 + 上一份备份）。

    done_indices 为完成条目的原始索引（升序），vectors 为与之对齐的向量矩阵（float32）。

    崩溃容错：先把上一份完整快照旋转为 `.bak`，新快照写临时文件 → fsync → 原子改名。
    任意崩溃点（写 tmp 中 / 旋转后改名新快照前 / 改名后）磁盘上都有一份可解析的快照。
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    target = base_dir / _PARTIAL_EMB_FILE
    backup = base_dir / _PARTIAL_EMB_BACKUP
    tmp = base_dir / _PARTIAL_EMB_TMP

    # 上一份完整快照留作备份（崩溃撕裂回退用）
    if target.exists():
        try:
            os.replace(target, backup)
        except OSError:
            pass

    # 用文件句柄写入（避免 np.savez_compressed 对路径自动追加 .npz 后缀），
    # 写完 fsync 后再原子改名，保证不产生撕裂文件
    with tmp.open("wb") as f:
        np.savez_compressed(
            f,
            indices=np.asarray(done_indices, dtype=np.int64),
            vectors=np.asarray(vectors, dtype=np.float32),
        )
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    _fsync_dir(base_dir)


def load_embed_progress(base_dir: Path) -> tuple[set[int], np.ndarray]:
    """读取 embedding 进度，返回 (已完成索引集合, 按索引排序的向量矩阵)。

    先读主快照 `partial_emb.npz`；解析失败（崩溃撕裂）时回退上一份备份 `.bak`。
    """
    for name in (_PARTIAL_EMB_FILE, _PARTIAL_EMB_BACKUP):
        path = base_dir / name
        if not path.exists():
            continue
        try:
            data = np.load(path, allow_pickle=False)
            indices = np.asarray(data["indices"], dtype=np.int64)
            vectors = np.asarray(data["vectors"], dtype=np.float32)
            order = np.argsort(indices)
            indices = indices[order]
            vectors = vectors[order]
            return set(int(i) for i in indices), vectors
        except Exception:
            continue
    return set(), np.zeros((0, 0), dtype=np.float32)


def clear_embed_progress(base_dir: Path) -> None:
    """删除 embedding 进度缓存（主快照 + 备份 + 残留临时文件）。"""
    for name in (_PARTIAL_EMB_FILE, _PARTIAL_EMB_BACKUP, _PARTIAL_EMB_TMP):
        path = base_dir / name
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
