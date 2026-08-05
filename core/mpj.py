"""mpj 打包格式与完整性校验码。

mpj 是一个 zip 压缩包，包含笔记本的 jsonl 源文件、embeddings.npy 与 index.meta
（不含 cache），外加一个 `checksum.sha256` 校验文件（对 3 个数据文件按文件名排序
拼接后取 SHA-256）。导入时重算比对，用于发现"文件被第三方修改/校验码被删"。
注意：这是完整性校验（防篡改提示），不是加密签名。
"""

import hashlib
import zipfile
from pathlib import Path
from typing import Any

_CHECKSUM_NAME = "checksum.sha256"
_CHECKSUM_HEADER = "mai-mpj-checksum-v1"


def _hash_data_files(files: list[Path]) -> str:
    """对数据文件按文件名排序拼接取 SHA-256。"""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.name):
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                digest.update(chunk)
    return digest.hexdigest()


def build_checksum_text(files: list[Path]) -> str:
    """生成校验文件文本。"""
    return f"{_CHECKSUM_HEADER}\n{_hash_data_files(files)}\n"


def verify_checksum_text(text: str, files: list[Path]) -> bool:
    """校验校验文件文本与数据文件是否一致。"""
    lines = str(text or "").strip().splitlines()
    if not lines or lines[0].strip() != _CHECKSUM_HEADER:
        return False
    expected = lines[1].strip() if len(lines) > 1 else ""
    return bool(expected) and expected == _hash_data_files(files)


def pack_mpj(zip_path: Path, files: dict[str, Path]) -> None:
    """把 {归档名: 本地路径} 打包为 mpj zip，并写入校验码。"""
    data_paths = [p for p in files.values() if p.exists()]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in files.items():
            if path.exists():
                zf.write(path, arcname)
        zf.writestr(_CHECKSUM_NAME, build_checksum_text(data_paths))


def unpack_mpj(zip_path: Path, dest_dir: Path) -> dict[str, Any]:
    """解压 mpj 到 dest_dir，返回结构：

    {name, jsonl, npy, meta, checksum_ok}
      checksum_ok: True 一致 / False 不一致或缺失校验码 / None 无校验码文件。
    缺关键文件时返回 {"error": ...}。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            root_names = [n for n in names if "/" not in n and not n.startswith("__MACOSX")]
            jsonl_name = next(
                (n for n in root_names if n.endswith(".jsonl") and not n.endswith(".cache.jsonl")), None
            )
            npy_name = next((n for n in root_names if n.endswith(".embeddings.npy")), None)
            meta_name = next((n for n in root_names if n.endswith(".index.meta")), None)
            checksum_name = _CHECKSUM_NAME if _CHECKSUM_NAME in root_names else None
            if not jsonl_name or not npy_name:
                return {"error": "mpj 缺少 .jsonl 或 .embeddings.npy 文件"}
            name = jsonl_name[: -len(".jsonl")]
            jsonl_path = dest_dir / jsonl_name
            npy_path = dest_dir / npy_name
            meta_path = dest_dir / (meta_name or f"{name}.index.meta")
            checksum_path = dest_dir / _CHECKSUM_NAME
            zf.extract(jsonl_name, dest_dir)
            zf.extract(npy_name, dest_dir)
            if meta_name:
                zf.extract(meta_name, dest_dir)
            if checksum_name:
                zf.extract(checksum_name, dest_dir)
    except zipfile.BadZipFile:
        return {"error": "文件不是有效的 mpj 压缩包"}

    checksum_ok: bool | None = None
    if checksum_path.exists():
        data_files = [jsonl_path, npy_path] + ([meta_path] if meta_path.exists() else [])
        try:
            checksum_ok = verify_checksum_text(checksum_path.read_text(encoding="utf-8"), data_files)
        except Exception:
            checksum_ok = False

    return {
        "name": name,
        "jsonl": jsonl_path,
        "npy": npy_path,
        "meta": meta_path,
        "checksum_ok": checksum_ok,
    }
