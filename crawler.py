"""Crawl4AI REST API 客户端 — 通过 HTTP 调用抓取网页

Docker 部署自带 Chromium 浏览器，无需额外依赖。
"""

import time
from logging import Logger
from typing import Optional
from urllib.parse import urlparse

import httpx

from .models import FetchResult


class Crawl4AIClient:
    """Crawl4AI 网页抓取客户端，通过 REST API 抓取并提取 Markdown。

    端点：
    - POST /md      — 轻量 Markdown 提取（无代理时）
    - POST /crawl   — 完整抓取（有代理时通过 browser_config 传入）
    - GET  /health  — 健康检查
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 30,
        max_content_length: int = 8000,
        filter_mode: str = "fit",
        proxy: Optional[str] = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
        api_token: Optional[str] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._timeout = timeout
        self._max_content_length = max_content_length
        self._filter_mode = filter_mode
        self._proxy = proxy.strip() if proxy else None
        self._proxy_username = proxy_username
        self._proxy_password = proxy_password
        self._api_token = api_token.strip() if api_token else None
        self._logger = logger

        # crawl4ai 设了 CRAWL4AI_API_TOKEN 后会绑 0.0.0.0 并要求 Bearer 鉴权；
        # 未设 token 时容器只绑回环、外部根本连不通，所以这里必须带上 token 头
        headers = {"User-Agent": "MaiBot-WebRetriever/1.0"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers=headers,
        )

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def fetch(self, url: str) -> FetchResult:
        """抓取网页，返回 Markdown。"""
        t0 = time.monotonic()

        try:
            if self._proxy:
                result = await self._fetch_via_crawl(url)
            else:
                result = await self._fetch_via_md(url)
            result.fetch_time_ms = (time.monotonic() - t0) * 1000
            return result

        except httpx.TimeoutException:
            if self._logger:
                self._logger.warning("Crawl4AI API 超时: %s", url)
            return FetchResult(
                url=url, error_message="网页抓取超时，请检查 Crawl4AI 服务",
                fetch_time_ms=(time.monotonic() - t0) * 1000,
            )
        except httpx.ConnectError:
            if self._logger:
                self._logger.warning("Crawl4AI API 不可达: %s", self._base_url)
            return FetchResult(
                url=url,
                error_message=f"无法连接 Crawl4AI ({self._base_url})，请检查服务是否启动",
                fetch_time_ms=(time.monotonic() - t0) * 1000,
            )
        except httpx.HTTPStatusError as e:
            if self._logger:
                self._logger.error("Crawl4AI HTTP %s: %s", e.response.status_code, url)
            return FetchResult(
                url=url, status_code=e.response.status_code,
                error_message=f"抓取失败 (HTTP {e.response.status_code})",
                fetch_time_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as e:
            if self._logger:
                self._logger.error("Crawl4AI 异常: %s", e)
            return FetchResult(
                url=url, error_message=f"抓取失败: {e}",
                fetch_time_ms=(time.monotonic() - t0) * 1000,
            )

    async def health_check(self) -> bool:
        """检查 Crawl4AI 服务是否可用"""
        try:
            resp = await self._http.get("/health")
            return resp.json().get("status") == "ok"
        except Exception:
            return False

    async def close(self) -> None:
        """关闭 HTTP 客户端"""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _fetch_via_md(self, url: str) -> FetchResult:
        """POST /md — 无代理时的轻量端点"""
        payload = {"url": url, "f": self._filter_mode}
        resp = await self._http.post("/md", json=payload)
        resp.raise_for_status()
        data = resp.json()

        markdown = self._ensure_str(data.get("markdown", ""))
        markdown = self._truncate_content(markdown)

        if not markdown:
            return FetchResult(url=url, error_message="网页内容为空或无法解析")

        return FetchResult(
            url=url,
            title=self._extract_title(markdown, url),
            markdown_content=markdown,
            status_code=200 if data.get("success", True) else 500,
        )

    async def _fetch_via_crawl(self, url: str) -> FetchResult:
        """POST /crawl — 有代理时使用"""
        browser_config = self._build_proxy_config()
        payload = {"urls": [url], "browser_config": browser_config}
        resp = await self._http.post("/crawl", json=payload)
        resp.raise_for_status()
        data = resp.json()

        item = (data.get("results") or [{}])[0] if "results" in data else data

        content = self._ensure_str(
            item.get("markdown")
            or item.get("extracted_content")
            or item.get("fit_html")
            or item.get("cleaned_html")
            or ""
        )
        content = self._truncate_content(content)
        title = item.get("title", "")
        error = item.get("error_message", "")

        if error and not content:
            return FetchResult(url=url, error_message=f"抓取失败: {error}")
        if not content:
            return FetchResult(url=url, error_message="网页内容为空或无法解析")

        return FetchResult(
            url=url,
            title=title or self._extract_title(content, url),
            markdown_content=content,
            status_code=item.get("status_code", 200),
        )

    def _build_proxy_config(self) -> dict:
        if not self._proxy:
            return {}
        if self._proxy_username and self._proxy_password:
            return {
                "proxy_config": {
                    "server": self._proxy,
                    "username": self._proxy_username,
                    "password": self._proxy_password,
                }
            }
        return {"proxy": self._proxy}

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _truncate_content(self, content: str) -> str:
        content = self._ensure_str(content)
        if len(content) <= self._max_content_length:
            return content
        t = content[: self._max_content_length]
        return t + f"\n\n---\n[已截断：原 {len(content)} 字符]"

    @staticmethod
    def _ensure_str(value) -> str:
        """确保值为字符串，避免 Crawl4AI 返回非预期类型时崩溃"""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            # 尝试从 dict 中提取可用的字符串字段
            for key in ("markdown", "content", "text", "raw_markdown", "fit_markdown"):
                if key in value and isinstance(value[key], str):
                    return value[key]
        return str(value) if value else ""

    @staticmethod
    def _extract_title(content: str, url: str) -> str:
        content = Crawl4AIClient._ensure_str(content)
        if content:
            for line in content.split("\n"):
                s = line.strip()
                if s.startswith("# ") and len(s) > 2:
                    return s[2:].strip()
        try:
            return urlparse(url).netloc or url
        except Exception:
            return url
