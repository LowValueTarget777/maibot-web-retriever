"""Crawl4AI 混合客户端 — REST API + SDK 双模抓取

策略优先级：
1. 配置了 crawl4ai_base_url → 优先 REST API（POST /md 或 /crawl）
2. REST API 不可达或未配置 → 降级为 crawl4ai SDK（AsyncWebCrawler）
3. 两种模式均支持代理
"""

import time
from logging import Logger
from typing import Optional
from urllib.parse import urlparse

import httpx

from .models import FetchResult


class Crawl4AIClient:
    """Crawl4AI 网页抓取客户端。

    混合模式：REST API 优先，SDK 兜底。
    """

    def __init__(
        self,
        base_url: str = "",
        timeout: int = 30,
        max_content_length: int = 8000,
        filter_mode: str = "fit",
        proxy: Optional[str] = None,
        proxy_username: Optional[str] = None,
        proxy_password: Optional[str] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        self._base_url = base_url.strip().rstrip("/") if base_url else ""
        self._timeout = timeout
        self._max_content_length = max_content_length
        self._filter_mode = filter_mode
        self._proxy = proxy.strip() if proxy else None
        self._proxy_username = proxy_username
        self._proxy_password = proxy_password
        self._logger = logger

        # REST API 模式：仅在有 base_url 时初始化
        self._http: Optional[httpx.AsyncClient] = None
        self._api_available: Optional[bool] = None  # None=未检测

        if self._base_url:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(timeout),
                headers={"User-Agent": "MaiBot-WebRetriever/1.0"},
            )

        # SDK 模式：延迟初始化
        self._sdk_crawler = None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def fetch(self, url: str) -> FetchResult:
        """抓取网页，自动选择可用模式。

        有 REST API 时优先 API，失败或无配置时降级 SDK。
        """
        t0 = time.monotonic()

        # ---- 先尝试 REST API ----
        if self._http is not None:
            result = await self._try_api_fetch(url)
            if result is not None:
                result.fetch_time_ms = (time.monotonic() - t0) * 1000
                return result
            # API 不可达，标记降级
            self._api_available = False
            if self._logger:
                self._logger.warning("Crawl4AI REST API 不可达，降级为本地 SDK")

        # ---- 降级为 SDK ----
        result = await self._try_sdk_fetch(url)
        result.fetch_time_ms = (time.monotonic() - t0) * 1000
        return result

    async def health_check(self) -> bool:
        """检查抓取能力是否可用（任一模式可用即返回 True）"""
        if self._http is not None:
            try:
                resp = await self._http.get("/health")
                if resp.json().get("status") == "ok":
                    self._api_available = True
                    return True
            except Exception:
                self._api_available = False
        try:
            __import__("crawl4ai")
            return True
        except ImportError:
            return False

    async def close(self) -> None:
        """清理资源"""
        if self._http is not None:
            await self._http.aclose()
        if self._sdk_crawler is not None:
            try:
                await self._sdk_crawler.__aexit__(None, None, None)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # REST API 模式
    # ------------------------------------------------------------------

    async def _try_api_fetch(self, url: str) -> Optional[FetchResult]:
        """尝试通过 REST API 抓取，失败返回 None 触发降级"""
        try:
            if self._proxy:
                return await self._api_fetch_via_crawl(url)
            else:
                return await self._api_fetch_via_md(url)
        except httpx.TimeoutException:
            if self._logger:
                self._logger.warning("Crawl4AI REST API 超时: %s", url)
        except httpx.HTTPStatusError as e:
            if self._logger:
                self._logger.warning("Crawl4AI REST API HTTP %s", e.response.status_code)
        except Exception as e:
            if self._logger:
                self._logger.warning("Crawl4AI REST API 异常: %s", e)
        return None

    async def _api_fetch_via_md(self, url: str) -> FetchResult:
        """POST /md — 无代理时的轻量端点"""
        payload = {"url": url, "f": self._filter_mode}
        resp = await self._http.post("/md", json=payload)
        resp.raise_for_status()
        data = resp.json()

        markdown = data.get("markdown") or ""
        markdown = self._truncate_content(markdown)

        return FetchResult(
            url=url,
            title=self._extract_title(markdown, url),
            markdown_content=markdown,
            status_code=200 if data.get("success", True) else 500,
        )

    async def _api_fetch_via_crawl(self, url: str) -> FetchResult:
        """POST /crawl — 有代理时使用"""
        browser_config = self._build_proxy_config()
        payload = {"urls": [url], "browser_config": browser_config}
        resp = await self._http.post("/crawl", json=payload)
        resp.raise_for_status()
        data = resp.json()

        if "results" in data and data["results"]:
            item = data["results"][0]
        else:
            item = data

        content = (
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

        return FetchResult(
            url=url,
            title=title or self._extract_title(content, url),
            markdown_content=content,
            status_code=item.get("status_code", 200),
        )

    def _build_proxy_config(self) -> dict:
        """构造 browser_config 代理配置"""
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
    # SDK 模式（降级）
    # ------------------------------------------------------------------

    async def _try_sdk_fetch(self, url: str) -> FetchResult:
        """通过 crawl4ai SDK 抓取"""
        try:
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
        except ImportError:
            return FetchResult(
                url=url,
                error_message="crawl4ai SDK 未安装，且 REST API 不可用",
            )

        try:
            crawler = await self._get_sdk_crawler(BrowserConfig)

            run_config = CrawlerRunConfig(
                word_count_threshold=self._max_content_length,
            )

            result = await crawler.arun(url=url, config=run_config)

            if not result.success:
                return FetchResult(
                    url=url,
                    error_message=result.error_message or "SDK 抓取失败",
                    status_code=result.status_code or 0,
                )

            markdown = result.markdown or result.extracted_content or ""
            markdown = self._truncate_content(markdown)

            title = ""
            if result.metadata and isinstance(result.metadata, dict):
                title = result.metadata.get("title", "")

            return FetchResult(
                url=url,
                title=title,
                markdown_content=markdown,
                status_code=result.status_code or 200,
            )

        except Exception as e:
            if self._logger:
                self._logger.error("Crawl4AI SDK 异常: %s", e)
            return FetchResult(url=url, error_message=f"SDK 抓取失败: {e}")

    async def _get_sdk_crawler(self, BrowserConfig):
        """获取或新建 SDK crawler（复用实例）"""
        if self._sdk_crawler is not None:
            return self._sdk_crawler

        browser_config = self._build_sdk_browser_config(BrowserConfig)
        self._sdk_crawler = AsyncWebCrawler(config=browser_config)
        await self._sdk_crawler.__aenter__()
        return self._sdk_crawler

    def _build_sdk_browser_config(self, BrowserConfig):
        """构建 SDK 的 BrowserConfig（含代理）"""
        kwargs = {"verbose": False}
        if self._proxy:
            if self._proxy_username and self._proxy_password:
                kwargs["proxy_config"] = {
                    "server": self._proxy,
                    "username": self._proxy_username,
                    "password": self._proxy_password,
                }
            else:
                kwargs["proxy"] = self._proxy
        return BrowserConfig(**kwargs)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _truncate_content(self, content: str) -> str:
        """截断过长内容"""
        if len(content) <= self._max_content_length:
            return content
        truncated = content[: self._max_content_length]
        return (
            truncated
            + f"\n\n---\n[内容已截断：原 {len(content)} 字符，保留"
            + f"前 {self._max_content_length} 字符]"
        )

    @staticmethod
    def _extract_title(content: str, url: str) -> str:
        """从 Markdown 提取标题，或从 URL 推断"""
        if content:
            for line in content.split("\n"):
                s = line.strip()
                if s.startswith("# ") and len(s) > 2:
                    return s[2:].strip()
        try:
            parsed = urlparse(url)
            return parsed.netloc or url
        except Exception:
            return url
