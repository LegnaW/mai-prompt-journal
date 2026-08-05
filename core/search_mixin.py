"""向量搜索、索引重建与 embedding 助手（mixin）。"""

import hashlib
import time
from typing import Any

import numpy as np

from .notebook import Notebook
from .retry import _API_EMBED_TIMEOUT, run_with_retry, scaled_batch_timeout

class SearchMixin:

    def _pick_dedup_notebooks(self, target_nb: Notebook) -> list[Notebook]:
        """选择写入去重检测的笔记本集合。

        配置 dedup_check_all_notebooks 开启时跨所有笔记本检测（含目标笔记本）；
        关闭时只检测目标笔记本。tmp 临时笔记本不参与。
        """
        if self.config.journal.dedup_check_all_notebooks:
            return list(self._notebooks.values())
        return [target_nb]

    async def _find_duplicate_matches(
        self,
        query_vec: np.ndarray,
        notebooks: list[Notebook],
        threshold: float,
        exclude_id: str = "",
    ) -> list[dict[str, Any]]:
        """在给定笔记本中查找与 query_vec 相似度 >= threshold 的条目（纯向量余弦）。

        返回 [{notebook, id, en, zh, note, score}, ...]，按相似度降序。
        跳过索引失效/向量与条目不一致的笔记本；exclude_id 的条目不参与匹配。
        口径与 _scan_duplicates 一致：L2 归一化后的余弦相似度。
        """
        threshold = max(0.5, min(0.99, float(threshold)))
        query_f32 = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        query_norm = np.linalg.norm(query_f32)
        if query_norm <= 1e-8:
            return []
        query_normed = query_f32 / query_norm

        matches: list[dict[str, Any]] = []
        for nb in notebooks:
            if not nb.check_consistency():
                continue
            entries = nb.load_notes()
            embeddings = nb.load_embeddings()
            if embeddings is None or len(embeddings) != len(entries):
                continue
            emb_f32 = embeddings.astype(np.float32)
            norms = np.linalg.norm(emb_f32, axis=1)
            safe_norms = np.where(norms > 1e-8, norms, 1.0)
            scores = (emb_f32 @ query_normed) / safe_norms
            for i in range(len(entries)):
                if exclude_id and str(entries[i].get("id", "")) == str(exclude_id):
                    continue
                score = float(scores[i])
                if score >= threshold:
                    e = entries[i]
                    matches.append(
                        {
                            "notebook": nb.name,
                            "id": e["id"],
                            "en": e["en"],
                            "zh": e["zh"],
                            "note": e["note"],
                            "score": round(score, 4),
                        }
                    )
        matches.sort(key=lambda m: m["score"], reverse=True)
        return matches

    @staticmethod
    def _format_matches(matches: list[dict[str, Any]]) -> str:
        """把匹配到的重复笔记格式化为多行文本。"""
        lines = []
        for m in matches:
            note_part = f" — {m['note']}" if m.get("note") else ""
            lines.append(
                f'[{m["notebook"]}/{m["id"]}] {m["en"]} / {m["zh"]}{note_part} (相似度 {m["score"]:.2f})'
            )
        return "\n".join(lines)

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
        """对单条文本调用 embedding（含超时 + 瞬时失败重试）。"""
        async def attempt() -> tuple[np.ndarray | None, str | None]:
            try:
                result = await self.ctx.llm.embed(text=text)
            except Exception as exc:
                return None, f"embedding 调用失败: {exc}"
            if not isinstance(result, dict) or not result.get("success"):
                error = result.get("error", "unknown") if isinstance(result, dict) else result
                return None, f"embedding 返回失败: {error}"
            vec = result.get("embedding")
            if not isinstance(vec, list) or not vec:
                return None, "embedding 返回空向量"
            return np.asarray(vec, dtype=np.float32), None

        vec, error = await run_with_retry(
            attempt, timeout=_API_EMBED_TIMEOUT, label="embedding", logger=self.ctx.logger
        )
        if error is not None:
            self.ctx.logger.error(f"embedding 调用失败: {error}")
            return None
        return vec

    async def _embed_batch(self, texts: list[str]) -> np.ndarray | None:
        """对多条文本批量调用 embedding（含缩放超时 + 瞬时失败重试）。"""
        async def attempt() -> tuple[np.ndarray | None, str | None]:
            max_concurrent = int(self.config.journal.embed_max_concurrent)
            try:
                result = await self.ctx.llm.embed(texts=texts, max_concurrent=max_concurrent)
            except Exception as exc:
                return None, f"批量 embedding 调用失败: {exc}"
            if not isinstance(result, dict) or not result.get("success"):
                error = result.get("error", "unknown") if isinstance(result, dict) else result
                return None, f"批量 embedding 返回失败: {error}"
            items = result.get("results")
            if not isinstance(items, list) or len(items) != len(texts):
                actual = len(items) if isinstance(items, list) else 0
                return None, f"批量 embedding 结果数量不匹配: 期望 {len(texts)}，实际 {actual}"
            vectors = []
            for item in items:
                vec = item.get("embedding") if isinstance(item, dict) else None
                if not isinstance(vec, list) or not vec:
                    return None, "批量 embedding 结果含空向量"
                vectors.append(vec)
            return np.asarray(vectors, dtype=np.float32), None

        max_concurrent = max(1, int(getattr(self.config.journal, "embed_max_concurrent", 4)))
        emb, error = await run_with_retry(
            attempt,
            timeout=scaled_batch_timeout(len(texts), max_concurrent),
            label="批量 embedding",
            logger=self.ctx.logger,
        )
        if error is not None:
            self.ctx.logger.error(f"批量 embedding 调用失败: {error}")
            return None
        return emb
