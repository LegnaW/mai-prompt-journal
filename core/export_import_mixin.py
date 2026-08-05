"""笔记本上传 / 下载（mixin）。

支持两种格式：
- jsonl：仅源文件；
- mpj：jsonl + embeddings.npy + index.meta + checksum.sha256 校验码。

导入与导出共用一套"传输状态机"（持久化在 `data_dir/file_io/`，页面刷新不丢）：
  kind  = "import" | "export"
  state = none / validating / ready / importing / building / done / error
由于后台任务走 `_start_task`（占用即拒绝新任务），同一时刻只会有一个导入或导出任务，
因此一个状态槽即可。导入校验/预览在 `preview.jsonl`+`preview.json`，导出最终产物
写入 `artifact/`（jsonl 直接复制重命名，mpj 打包），完成后前端提供下载。
"""

import hashlib
import io
import json
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np

from .embedding_client import EmbeddingClient, load_embedding_profile
from .mpj import pack_mpj, unpack_mpj
from .notebook import Notebook, scramble_id

# 内置 embedding 的默认抽样条数（mpj 校验用）
_DEFAULT_VALIDATE_SAMPLE = 25

_NOTEBOOK_NAME_RE = r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$"


class ExportImportMixin:
    """笔记本导出与文件导入能力（需与主类组合）。"""

    # ============================================================
    # 统一传输状态机（file_io/，抗刷新）
    # ============================================================

    def _io_dir(self) -> Path:
        return self._data_dir / "file_io"

    def _io_kind_path(self) -> Path:
        return self._io_dir() / "kind"

    def _io_state_path(self) -> Path:
        return self._io_dir() / "state"

    def _io_preview_jsonl_path(self) -> Path:
        return self._io_dir() / "preview.jsonl"

    def _io_preview_json_path(self) -> Path:
        return self._io_dir() / "preview.json"

    def _io_result_path(self) -> Path:
        return self._io_dir() / "result.json"

    def _io_progress_path(self) -> Path:
        return self._io_dir() / "progress.json"

    def _io_artifact_dir(self) -> Path:
        return self._io_dir() / "artifact"

    def _reset_io(self) -> None:
        """清空传输暂存区（新导入/新导出前调用）。"""
        d = self._io_dir()
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)

    def _set_io(self, kind: str, state: str) -> None:
        d = self._io_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "kind").write_text(kind, encoding="utf-8")
        (d / "state").write_text(state, encoding="utf-8")

    def _get_io_kind(self) -> str:
        try:
            return self._io_kind_path().read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _get_io_state(self) -> str:
        try:
            return self._io_state_path().read_text(encoding="utf-8").strip() or "none"
        except OSError:
            return "none"

    def _write_io_preview(self, meta: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        d = self._io_dir()
        d.mkdir(parents=True, exist_ok=True)
        with self._io_preview_jsonl_path().open("w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        self._io_preview_json_path().write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _read_io_preview(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(self._io_preview_json_path().read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        entries: list[dict[str, Any]] = []
        p = self._io_preview_jsonl_path()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
        return meta, entries

    def _write_io_result(self, result: dict[str, Any]) -> None:
        d = self._io_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._io_result_path().write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    def _read_io_result(self) -> dict[str, Any]:
        try:
            data = json.loads(self._io_result_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_io_progress(self, progress: dict[str, Any]) -> None:
        d = self._io_dir()
        d.mkdir(parents=True, exist_ok=True)
        self._io_progress_path().write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")

    def _read_io_progress(self) -> dict[str, Any]:
        try:
            data = json.loads(self._io_progress_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _embed_with_progress(self, texts: list[str], chunk: int = 64) -> np.ndarray | None:
        """分批内置 embedding 并写进度到 file_io/progress.json，返回矩阵或 None。"""
        total = len(texts)
        if total == 0:
            return None
        vectors: list[np.ndarray] = []
        for i in range(0, total, chunk):
            emb = await self._embed_batch(texts[i : i + chunk])
            if emb is None:
                return None
            vectors.append(emb)
            self._write_io_progress(
                {"phase": "embedding", "done": min(i + len(emb), total), "total": total}
            )
        return np.vstack(vectors) if vectors else None

    def _save_artifact(self, data: bytes, filename: str) -> None:
        d = self._io_artifact_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_bytes(data)

    def _artifact_path(self, filename: str) -> Path:
        return self._io_artifact_dir() / filename

    # ============================================================
    # jsonl 解析与名称校验
    # ============================================================

    @staticmethod
    def _parse_import_jsonl(raw: str) -> tuple[list[dict[str, Any]], int]:
        """解析 jsonl 文本为归一化条目，返回 (条目, 跳过数)。

        id 缺失/重复自动用 scramble_id 生成；ts 缺失填当前时间；en/zh 为空的
        行与解析失败的行计入跳过数。
        """
        entries: list[dict[str, Any]] = []
        skipped = 0
        seen: set[str] = set()
        base_ts = int(time.time() * 1000)
        now = time.time()
        for i, line in enumerate(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            en = str(obj.get("en", "") or "").strip()
            zh = str(obj.get("zh", "") or "").strip()
            if not en or not zh:
                skipped += 1
                continue
            entry_id = str(obj.get("id", "") or "").strip()
            if not entry_id or entry_id in seen:
                entry_id = scramble_id(base_ts + i)
            seen.add(entry_id)
            try:
                ts = float(obj.get("ts", 0) or 0)
            except (TypeError, ValueError):
                ts = 0.0
            entries.append(
                {
                    "id": entry_id,
                    "en": en,
                    "zh": zh,
                    "note": str(obj.get("note", "") or "").strip(),
                    "ts": ts or now,
                }
            )
        return entries, skipped

    @staticmethod
    def _valid_notebook_name(name: str) -> bool:
        import re

        return bool(re.match(_NOTEBOOK_NAME_RE, name))

    # ============================================================
    # 导出（后台任务，产物写入 artifact/）
    # ============================================================

    async def _run_export_task(self, task_id: str, notebook: str, fmt: str, mode: str, filename: str) -> None:
        try:
            nb = self._get_notebook(notebook)
            if nb is None:
                raise RuntimeError(f"笔记本 '{notebook}' 不存在")

            if fmt == "jsonl":
                if not nb.notes_path.exists():
                    raise RuntimeError("笔记本无源文件")
                out_name = filename if filename.endswith(".jsonl") else f"{filename or nb.name}.jsonl"
                data = nb.notes_path.read_bytes()
                ctype = "text/plain"
            elif fmt == "mpj":
                if mode == "rebuild":
                    data = await self._export_mpj_rebuild(nb)
                else:
                    data = await self._export_mpj_direct(nb)
                out_name = filename if filename.endswith(".mpj") else f"{filename or nb.name}.mpj"
                ctype = "application/zip"
            else:
                raise RuntimeError("format 只能是 jsonl 或 mpj")

            self._save_artifact(data, out_name)
            self._set_io("export", "done")
            self._write_io_result(
                {"success": True, "message": f"导出完成：{out_name}", "filename": out_name, "size": len(data), "ctype": ctype}
            )
            self._finish_task(task_id, {"ok": True, "filename": out_name})
        except Exception as exc:
            self.ctx.logger.error(f"导出任务异常: {exc}", exc_info=True)
            self._set_io("export", "error")
            self._write_io_result({"error": f"导出失败：{exc}"})
            self._fail_task(task_id, exc)
        finally:
            self._evict_tasks()

    async def _export_jsonl(self, nb: Notebook) -> bytes:
        return nb.notes_path.read_bytes()

    async def _export_mpj_direct(self, nb: Notebook) -> bytes:
        """直接导出：打包当前 jsonl + embeddings + meta + 校验码。"""
        files: dict[str, Path] = {}
        for suffix in (".jsonl", ".embeddings.npy", ".index.meta"):
            p = nb._dir / f"{nb.name}{suffix}"
            if p.exists():
                files[p.name] = p
        if f"{nb.name}.embeddings.npy" not in files:
            raise RuntimeError("笔记本无索引，无法导出 mpj（请先重建索引）")
        return self._pack_mpj_bytes(files)

    async def _export_mpj_rebuild(self, nb: Notebook) -> bytes:
        """用第三方 embedding 重新生成索引导出。"""
        profile = load_embedding_profile(self._data_dir)
        client = EmbeddingClient(
            base_url=profile.get("base_url", ""),
            api_key=profile.get("api_key", ""),
            model=profile.get("model", ""),
            timeout=profile.get("timeout", 60),
        )
        if not client.configured:
            raise RuntimeError("第三方 embedding 配置不完整，请先在网页保存配置")
        entries = nb.load_notes()
        if not entries:
            raise RuntimeError("笔记本为空，无法导出")
        texts = [self._build_embedding_text(e["en"], e["zh"], e["note"]) for e in entries]
        total = len(texts)
        vectors: list[list[float]] = []
        for i in range(0, total, 64):
            part = await client.embed(texts[i : i + 64])
            if part is None:
                raise RuntimeError("第三方 embedding 调用失败，请检查配置或网络")
            vectors.extend(part)
            self._write_io_progress({"phase": "embedding", "done": min(i + len(part), total), "total": total})
        if len(vectors) != total:
            raise RuntimeError("第三方 embedding 返回数量不匹配")

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            jsonl_path = tmp / f"{nb.name}.jsonl"
            jsonl_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8"
            )
            npy_path = tmp / f"{nb.name}.embeddings.npy"
            np.save(npy_path, np.asarray(vectors, dtype=np.float32).astype(np.float16))
            meta_path = tmp / f"{nb.name}.index.meta"
            meta_path.write_text(
                json.dumps(
                    {
                        "md5": hashlib.md5(jsonl_path.read_bytes()).hexdigest(),
                        "count": len(entries),
                        "built_at": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return self._pack_mpj_bytes({p.name: p for p in (jsonl_path, npy_path, meta_path)})

    @staticmethod
    def _pack_mpj_bytes(files: dict[str, Path]) -> bytes:
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / "out.mpj"
            pack_mpj(zpath, files)
            buf.write(zpath.read_bytes())
        return buf.getvalue()

    # ============================================================
    # 导入校验（后台任务）
    # ============================================================

    async def _run_file_validation_task(
        self, task_id: str, source: bytes, filename: str, sample_n: int
    ) -> None:
        try:
            lower = filename.lower()
            if lower.endswith(".jsonl"):
                raw = source.decode("utf-8", errors="replace")
                entries, skipped = self._parse_import_jsonl(raw)
                meta = {
                    "format": "jsonl",
                    "source_name": None,
                    "valid_count": len(entries),
                    "skipped_count": skipped,
                    "checksum_status": None,
                }
                self._write_io_preview(meta, entries)
                self._set_io("import", "ready")
            elif lower.endswith(".mpj"):
                result = await self._validate_mpj_file(source, sample_n)
                if "error" in result:
                    self._set_io("import", "error")
                    self._write_io_result({"error": result["error"]})
                else:
                    self._write_io_preview(result["meta"], result["entries"])
                    self._set_io("import", "ready")
            else:
                self._set_io("import", "error")
                self._write_io_result({"error": "不支持的文件类型，仅支持 .jsonl 或 .mpj"})
            self._finish_task(task_id, {"ok": True})
        except Exception as exc:
            self.ctx.logger.error(f"文件导入校验任务异常: {exc}", exc_info=True)
            self._set_io("import", "error")
            self._write_io_result({"error": f"校验失败：{exc}"})
            self._fail_task(task_id, exc)
        finally:
            self._evict_tasks()

    async def _validate_mpj_file(self, source: bytes, sample_n: int) -> dict[str, Any]:
        """解压 mpj 并做校验码 / 维度 / 抽样相似度校验。"""
        unpack_dir = self._io_dir() / "unpack"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        zip_path = unpack_dir / "upload.mpj"
        zip_path.write_bytes(source)
        unpacked = unpack_mpj(zip_path, unpack_dir)
        if "error" in unpacked:
            return unpacked

        entries, skipped = self._parse_import_jsonl(unpacked["jsonl"].read_text(encoding="utf-8"))
        meta: dict[str, Any] = {
            "format": "mpj",
            "source_name": unpacked["name"],
            "valid_count": len(entries),
            "skipped_count": skipped,
        }

        # 校验码：缺失或对不上都按"可能被第三方修改"警告
        ck = unpacked.get("checksum_ok")
        meta["checksum_status"] = "ok" if ck is True else "tampered"

        try:
            emb = np.load(unpacked["npy"])
        except Exception:
            return {"error": "mpj 的 embeddings.npy 无法读取"}
        if emb.ndim != 2:
            return {"error": "mpj 的 embeddings.npy 维度非法"}
        meta["dim"] = int(emb.shape[1])
        meta["vector_count"] = int(emb.shape[0])
        meta["count_match"] = emb.shape[0] == len(entries)

        dim_match: bool | None = None
        if entries:
            probe = await self._embed_single(entries[0]["en"])
            if probe is not None:
                dim_match = len(probe) == emb.shape[1]
        meta["dim_match"] = dim_match

        sample_stats: dict[str, Any] = {"n": 0, "avg": None, "min": None}
        if sample_n <= 0:
            # 抽样条数填 0：不校验相似度
            sample_stats["skipped"] = True
        # 维度一致且 条目数=向量数 时才抽样（否则索引无法对齐，可能是被篡改）
        elif dim_match and meta["count_match"] and entries:
            n = max(1, min(int(sample_n or _DEFAULT_VALIDATE_SAMPLE), len(entries)))
            idxs = random.sample(range(len(entries)), n)
            emb_f32 = emb.astype(np.float32)
            norms = np.linalg.norm(emb_f32, axis=1)
            safe = np.where(norms > 1e-8, norms, 1.0)
            scores: list[float] = []
            for done, i in enumerate(idxs, 1):
                text = self._build_embedding_text(entries[i]["en"], entries[i]["zh"], entries[i]["note"])
                vec = await self._embed_single(text)
                self._write_io_progress({"phase": "校验抽样", "done": done, "total": n})
                if vec is None:
                    continue
                vn = float(np.linalg.norm(vec))
                if vn <= 1e-8:
                    continue
                score = float(np.dot(emb_f32[i], vec) / (safe[i] * vn))
                scores.append(max(-1.0, min(1.0, score)))
            if scores:
                sample_stats = {
                    "n": len(scores),
                    "avg": round(sum(scores) / len(scores), 4),
                    "min": round(min(scores), 4),
                }
        meta["sample"] = sample_stats
        return {"meta": meta, "entries": entries}

    # ============================================================
    # 导入提交（后台任务）
    # ============================================================

    async def _run_file_commit_task(
        self, task_id: str, target_name: str, mode: str, merge_target: str
    ) -> None:
        try:
            meta, entries = self._read_io_preview()
            fmt = meta.get("format")
            if not entries:
                raise RuntimeError("没有可导入的条目")

            if fmt == "mpj" and mode == "direct":
                if merge_target:
                    raise RuntimeError("mpj 直接导入不支持合并到已有笔记本")
                npy_path = self._io_dir() / "unpack" / f"{meta.get('source_name')}.embeddings.npy"
                ok, msg = await self._import_mpj_direct(target_name, entries, npy_path)
            else:
                ok, msg = await self._import_jsonl_entries(target_name, entries, merge_target)

            if ok:
                self._set_io("import", "done")
                self._write_io_result({"success": True, "message": msg, "notebook": target_name})
                self._finish_task(task_id, {"ok": True, "message": msg})
            else:
                self._set_io("import", "error")
                self._write_io_result({"error": msg})
                self._fail_task(task_id, msg)
        except Exception as exc:
            self.ctx.logger.error(f"文件导入提交任务异常: {exc}", exc_info=True)
            self._set_io("import", "error")
            self._write_io_result({"error": f"导入失败：{exc}"})
            self._fail_task(task_id, exc)
        finally:
            self._evict_tasks()

    async def _import_jsonl_entries(
        self, target_name: str, entries: list[dict[str, Any]], merge_target: str
    ) -> tuple[bool, str]:
        """按内置 embedding 建索引，新建或合并到已有笔记本。"""
        if merge_target:
            nb = self._get_notebook(merge_target)
            if nb is None:
                return False, f"目标笔记本 '{merge_target}' 不存在"
            async with self._lock:
                if not nb.check_consistency():
                    return False, f"笔记本 '{merge_target}' 索引失效，请先 /mpj rebuild"
                # 合并时重生成 id，避免与目标冲突
                base = int(time.time() * 1000)
                fresh = [dict(e) for e in entries]
                existing_ids = {e["id"] for e in nb.load_notes()}
                for i, e in enumerate(fresh):
                    while e["id"] in existing_ids:
                        e["id"] = scramble_id(base + i * 1000 + len(existing_ids))
                    existing_ids.add(e["id"])
                texts = [self._build_embedding_text(e["en"], e["zh"], e["note"]) for e in fresh]
                emb = await self._embed_with_progress(texts)
                if emb is None:
                    return False, "embedding 服务不可用"
                nb.append_entries(fresh, emb)
                nb.update_md5()
                self._create_backup(nb)
            return True, f"已合并 {len(fresh)} 条到笔记本 '{merge_target}'（建议到去重页扫描重复）"

        name = str(target_name or "").strip()
        if not self._valid_notebook_name(name):
            return False, "笔记本名称只能包含中文/字母/数字/下划线/连字符"
        async with self._lock:
            if self._get_notebook(name) is not None:
                return False, f"笔记本 '{name}' 已存在"
            texts = [self._build_embedding_text(e["en"], e["zh"], e["note"]) for e in entries]
            emb = await self._embed_with_progress(texts)
            if emb is None:
                return False, "embedding 服务不可用"
            nb = Notebook(name, self._data_dir)
            nb.notes_path.parent.mkdir(parents=True, exist_ok=True)
            json_str = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
            nb.notes_path.write_text(json_str, encoding="utf-8")
            np.save(nb.embeddings_path, np.asarray(emb, dtype=np.float32).astype(np.float16))
            nb.rewrite_cache(entries)
            nb.save_meta({"md5": nb.compute_file_md5(), "count": len(entries), "built_at": time.time()})
            self._notebooks = self._discover_notebooks()
        return True, f"已导入 {len(entries)} 条到新笔记本 '{name}'"

    async def _import_mpj_direct(
        self, target_name: str, entries: list[dict[str, Any]], npy_path: Path
    ) -> tuple[bool, str]:
        """mpj 直接导入：保留 mpj 自带索引（仅新建，需维度一致）。"""
        name = str(target_name or "").strip()
        if not self._valid_notebook_name(name):
            return False, "笔记本名称只能包含中文/字母/数字/下划线/连字符"
        async with self._lock:
            if self._get_notebook(name) is not None:
                return False, f"笔记本 '{name}' 已存在"
            try:
                emb = np.load(npy_path)
                if emb.ndim != 2 or emb.shape[0] != len(entries):
                    return False, "mpj 向量数量与条目数不一致"
            except Exception:
                return False, "mpj 的 embeddings.npy 无法读取"
            nb = Notebook(name, self._data_dir)
            nb.notes_path.parent.mkdir(parents=True, exist_ok=True)
            json_str = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
            nb.notes_path.write_text(json_str, encoding="utf-8")
            np.save(nb.embeddings_path, np.asarray(emb, dtype=np.float32).astype(np.float16))
            nb.rewrite_cache(entries)
            nb.save_meta({"md5": nb.compute_file_md5(), "count": len(entries), "built_at": time.time()})
            self._notebooks = self._discover_notebooks()
        return True, f"已直接导入 {len(entries)} 条到新笔记本 '{name}'（使用 mpj 自带索引）"
