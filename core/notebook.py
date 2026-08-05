"""笔记本数据模型：笔记条目读写、向量文件与一致性校验。"""

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

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

def _split_txt(text: str) -> list[str]:
    """按两个及以上连续换行切分 txt，每段为一个块。

    \\n\\n 切分；\\n\\n\\n 也只切一次；单个 \\n 不切分。
    """
    import re

    parts = re.split(r"\n{2,}", text or "")
    return [p.strip() for p in parts if p.strip()]

class Notebook:
    """封装单个笔记本的路径解析和文件读写。

    每个笔记本由 4 个文件组成：
      {name}.jsonl        人类可编辑的笔记源文件
      {name}.cache.jsonl  与向量索引对齐的内部快照
      {name}.embeddings.npy  float16 向量矩阵
      {name}.index.meta   索引元信息（md5、条目数、构建时间）
    """

    def __init__(self, name: str, base_dir: Path, custom_dir: Path | None = None) -> None:
        self.name = name
        if custom_dir is not None:
            self._dir = custom_dir
        elif name == "default":
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
