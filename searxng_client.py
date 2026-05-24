"""SearXNG HTTP 客户端 — 封装搜索 API 调用"""

import hashlib
from logging import Logger
from typing import Optional

import httpx

from .models import SearchResponse, SearchResult


class SearxNGClient:
    """SearXNG 搜索引擎 HTTP 客户端。

    通过 JSON API (/search?format=json) 进行搜索，
    支持类别过滤、安全搜索等参数。
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 10,
        logger: Optional[Logger] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._logger = logger
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "MaiBot-WebRetriever/1.0"},
        )

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        categories: list[str] | None = None,
        max_results: int = 5,
        safe_search: int = 0,
    ) -> SearchResponse:
        """执行搜索，返回结构化结果。categories 为类别列表如 ["general", "news"]。"""
        params = self._build_params(query, categories, max_results, safe_search)

        try:
            response = await self._client.get("/search", params=params)
            response.raise_for_status()
            data = response.json()
            return self._parse_response(data, query, max_results=max_results)
        except httpx.TimeoutException:
            if self._logger:
                self._logger.warning("SearXNG 搜索超时: %s", query)
            return SearchResponse(query=query)
        except httpx.HTTPStatusError as e:
            if self._logger:
                self._logger.error("SearXNG HTTP 错误: %s", e)
            return SearchResponse(query=query)
        except Exception as e:
            if self._logger:
                self._logger.error("SearXNG 请求异常: %s", e)
            return SearchResponse(query=query)

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_params(
        self,
        query: str,
        categories: list[str] | None,
        max_results: int,
        safe_search: int,
    ) -> dict:
        """构造 SearXNG API 查询参数"""
        params: dict = {
            "format": "json",
            "q": query,
            "safesearch": safe_search,
            "pageno": 1,
        }
        if categories:
            params["categories"] = ",".join(categories)
        return params

    def _parse_response(
        self,
        data: dict,
        query: str,
        *,
        max_results: int = 50,
    ) -> SearchResponse:
        """解析 SearXNG JSON 响应"""
        raw_results = data.get("results", [])
        results = []
        for item in raw_results[: max(0, max_results)]:
            results.append(SearchResult(
                url=item.get("url", ""),
                title=item.get("title", ""),
                snippet=item.get("content", ""),
                engine=item.get("engine", ""),
                category=item.get("category", "general"),
                score=item.get("score", 0.0),
                published_date=item.get("publishedDate"),
            ))

        return SearchResponse(
            query=query,
            results=results,
            total_count=data.get("number_of_results", len(results)),
            unresponsive_engines=data.get("unresponsive_engines", []),
        )


def make_search_cache_key(
    query: str, categories: str, max_results: int, show_references: bool = True
) -> str:
    """生成搜索缓存键"""
    raw = f"{query}|{categories}|{max_results}|ref={show_references}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"search:{h}"


def make_fetch_cache_key(url: str, show_references: bool = True) -> str:
    """生成抓取缓存键"""
    raw = f"{url}|ref={show_references}"
    h = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"fetch:{h}"
