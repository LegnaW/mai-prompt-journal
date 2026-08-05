"""第三方 OpenAI 兼容 embedding 客户端与配置存取。

用于 mpj「重新生成索引导出」：直接调用第三方 embeddings API（/embeddings），
与插件内置 embedding（`ctx.llm.embed`）相互独立。配置保存在
`data_dir/embedding_profile.json`（服务器端，WebUI 可读写复用）。
"""

import json
from pathlib import Path
from typing import Any

import aiohttp

_EMBEDDING_PROFILE_FILENAME = "embedding_profile.json"


class EmbeddingClient:
    """直连 OpenAI 兼容 embeddings API。"""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 60) -> None:
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self._model = str(model or "").strip()
        self._timeout = max(5, int(timeout or 60))

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key and self._model)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """批量生成向量，失败返回 None。"""
        if not self.configured or not texts:
            return None
        url = (
            f"{self._base_url}/embeddings"
            if self._base_url.endswith("/v1")
            else f"{self._base_url}/v1/embeddings"
        )
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        body = {"model": self._model, "input": list(texts)}
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    status = resp.status
                    data = await resp.json()
        except Exception:
            return None
        if status != 200:
            return None
        items = (data or {}).get("data") or []
        if not isinstance(items, list) or len(items) != len(texts):
            return None
        vectors: list[list[float]] = []
        for item in sorted(items, key=lambda x: x.get("index", 0) if isinstance(x, dict) else 0):
            if not isinstance(item, dict):
                return None
            vec = item.get("embedding")
            if not isinstance(vec, list):
                return None
            vectors.append([float(v) for v in vec])
        if len(vectors) != len(texts):
            return None
        return vectors


def load_embedding_profile(data_dir: Path) -> dict[str, Any]:
    """读取已保存的第三方 embedding 配置，缺失返回空 dict。"""
    path = data_dir / _EMBEDDING_PROFILE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_embedding_profile(data_dir: Path, profile: dict[str, Any]) -> None:
    """保存第三方 embedding 配置。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / _EMBEDDING_PROFILE_FILENAME
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
