"""笔记本自动备份（mixin）。

每次笔记本被修改后，把 `{name}.jsonl` 源文件复制到 `data_dir/backups/{name}/{时间戳}.jsonl`，
超过 `[backup] max_per_notebook` 时自动删除最旧备份。备份只存源文件，恢复时写回 jsonl
并调用 `_rebuild_notebook` 增量重建索引。`tmp` 临时笔记本不参与备份。
"""

import shutil
import time
from pathlib import Path
from typing import Any

from .notebook import Notebook


class BackupMixin:
    """提供笔记本备份的创建 / 查看 / 恢复 / 删除能力（需与主类组合）。"""

    # ---------- 配置 ----------

    def _backup_enabled(self) -> bool:
        """是否启用自动备份。"""
        return bool(getattr(self.config.backup, "enabled", True))

    def _backup_max(self) -> int:
        """每个笔记本的备份上限。"""
        value = getattr(self.config.backup, "max_per_notebook", 6)
        try:
            return max(1, min(200, int(value or 6)))
        except (TypeError, ValueError):
            return 6

    # ---------- 目录 ----------

    def _backup_dir(self, name: str) -> Path:
        """笔记本备份目录：data_dir/backups/{name}/。"""
        return self._data_dir / "backups" / name

    def _backup_path(self, name: str, timestamp: str) -> Path:
        """某个备份的文件路径（timestamp 为文件名主干，不含 .jsonl）。"""
        return self._backup_dir(name) / f"{timestamp}.jsonl"

    # ---------- 创建 ----------

    def _create_backup(self, nb: Notebook) -> str | None:
        """为笔记本创建一份备份，返回时间戳文件名主干；禁用 / tmp / 无源文件时返回 None。"""
        if not self._backup_enabled():
            return None
        if nb.name == "tmp" or not nb.notes_path.exists():
            return None

        bdir = self._backup_dir(nb.name)
        bdir.mkdir(parents=True, exist_ok=True)

        base = time.strftime("%Y%m%d_%H%M%S")
        target = bdir / f"{base}.jsonl"
        suffix = 2
        while target.exists():
            target = bdir / f"{base}_{suffix}.jsonl"
            suffix += 1

        try:
            shutil.copyfile(nb.notes_path, target)
        except OSError:
            return None

        self._evict_backups(nb.name)
        return target.stem

    def _evict_backups(self, name: str) -> None:
        """超出上限时删除最旧备份。"""
        limit = self._backup_max()
        bdir = self._backup_dir(name)
        if not bdir.is_dir():
            return
        files = sorted(bdir.glob("*.jsonl"))
        while len(files) > limit:
            try:
                files[0].unlink()
            except OSError:
                pass
            files = files[1:]

    # ---------- 查看 ----------

    def _list_backups(self, nb: Notebook) -> list[dict[str, Any]]:
        """列出笔记本全部备份（新→旧）：[{timestamp, count, size}]。"""
        bdir = self._backup_dir(nb.name)
        if not bdir.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in sorted(bdir.glob("*.jsonl"), reverse=True):
            result.append(
                {
                    "timestamp": path.stem,
                    "count": self._count_backup_lines(path),
                    "size": self._safe_size(path),
                }
            )
        return result

    @staticmethod
    def _count_backup_lines(path: Path) -> int:
        """统计备份文件中的条目行数。"""
        count = 0
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
        except OSError:
            pass
        return count

    @staticmethod
    def _safe_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    # ---------- 恢复 / 删除 ----------

    async def _restore_backup(self, nb: Notebook, timestamp: str) -> tuple[bool, str]:
        """恢复备份：先备份当前状态（可撤销），再写回备份内容并重建索引。"""
        path = self._backup_path(nb.name, timestamp)
        if not path.exists():
            return False, f"备份不存在：{timestamp}"

        raw = path.read_text(encoding="utf-8")
        if raw.strip():
            if not Notebook._load_jsonl(path):
                return False, "备份内容无法解析，已取消恢复"

        # 先备份当前状态，便于撤销本次恢复
        self._create_backup(nb)

        nb.notes_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, nb.notes_path)
        try:
            await self._rebuild_notebook(nb)
        except Exception as exc:
            self.ctx.logger.error(f"恢复备份 {timestamp} 后重建索引失败: {exc}", exc_info=True)
            return False, f"已写回源文件，但索引重建失败：{exc}（请执行 /mpj rebuild）"

        count = len(Notebook._load_jsonl(path))
        return True, f"已从备份 {timestamp} 恢复（{count} 条）"

    def _delete_backup(self, nb: Notebook, timestamp: str) -> tuple[bool, str]:
        """删除一个备份。"""
        path = self._backup_path(nb.name, timestamp)
        if not path.exists():
            return False, f"备份不存在：{timestamp}"
        try:
            path.unlink()
        except OSError as exc:
            return False, f"删除失败：{exc}"
        return True, f"已删除备份 {timestamp}"
