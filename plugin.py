"""MaiBot 网页检索插件 — 搜索 → 抓取 → 清洗 → 分块 → Map-Reduce 摘要"""

import os as _os
import sys as _sys

# 确保插件目录在 sys.path 中，解决目录名含连字符的导入问题
_plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _plugin_dir not in _sys.path:
    _sys.path.insert(0, _plugin_dir)

from typing import Any, Literal

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .cache import MemoryCache
from .crawler import Crawl4AIClient
from .models import (
    fetch_error_dict,
    fetch_to_llm_dict,
    search_error_dict,
)
from .searxng_client import (
    SearxNGClient,
    make_fetch_cache_key,
    make_search_cache_key,
)

PREFERRED_LLM_TASKS: tuple[str, str, str] = ("utils", "planner", "replyer")
MAP_SUMMARY_MAX_CONCURRENCY = 3
SAFE_SEARCH_OPTION_DESCRIPTIONS = {
    0: "不过滤，结果最全",
    1: "适度过滤，适合日常使用",
    2: "严格过滤，结果会更保守",
}
FILTER_MODE_OPTION_DESCRIPTIONS = {
    "fit": "推荐。尽量只保留网页正文",
    "raw": "保留更多原始内容，适合排查抓取问题",
    "bm25": "偏关键词筛选，适合目标明确的内容",
    "llm": "依赖抓取端智能提取，通常不需要手动切换",
}


async def _generate_with_preferred_task_fallback(
    llm_capability,
    *,
    prompt: str,
    temperature: float = 0.3,
    logger=None,
) -> dict:
    """按插件约定顺序调用 LLM：utils -> planner -> replyer。"""
    last_result: dict | None = None

    for task_name in PREFERRED_LLM_TASKS:
        result = await llm_capability.generate(
            prompt=prompt,
            model=task_name,
            temperature=temperature,
        )
        if result.get("success"):
            return result

        last_result = result
        if logger is not None:
            logger.warning(
                "LLM 调用失败，准备回退到下一个任务: task=%s error=%s",
                task_name,
                result.get("error", ""),
            )

    return last_result or {"success": False, "response": "", "error": "所有预设模型任务均调用失败"}


# ============================================================
# 配置模型
# ============================================================

class PluginSectionConfig(PluginConfigBase):
    """插件基础配置"""
    __ui_label__ = "基础设置"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="关闭后，这个插件不会参与搜索和网页抓取",
        json_schema_extra={"label": "启用网页搜索", "x-widget": "switch"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="用于兼容旧配置，通常不需要手动修改",
        json_schema_extra={"label": "配置版本", "x-widget": "input", "advanced": True},
    )
    show_progress: bool = Field(
        default=True,
        description='搜索中、抓取中时先发一条提示，避免看起来像卡住了',
        json_schema_extra={"label": "发送过程提示", "x-widget": "switch"},
    )
    show_references: bool = Field(
        default=True,
        description="通常保持开启；只有你不想让 AI 看到原始来源地址时再关闭",
        json_schema_extra={"label": "把来源链接也提供给 AI", "x-widget": "switch", "advanced": True},
    )
    send_references_to_user: bool = Field(
        default=True,
        description="适合需要让用户自己点开查看原网页时开启",
        json_schema_extra={"label": "在回复后附上来源链接", "x-widget": "switch"},
    )


class SearchConfig(PluginConfigBase):
    """SearXNG 搜索配置"""
    __ui_label__ = "搜索服务"
    __ui_icon__ = "search"
    __ui_order__ = 1

    searxng_base_url: str = Field(
        default="http://192.168.1.9:8800",
        description="填你的 SearXNG 地址，例如 http://127.0.0.1:8800",
        json_schema_extra={"label": "搜索服务地址", "x-widget": "input"},
    )
    search_general: bool = Field(
        default=True,
        description="通常建议开启，用来搜索普通网页内容",
        json_schema_extra={"label": "搜索普通网页", "x-widget": "switch", "advanced": True},
    )
    search_news: bool = Field(
        default=True,
        description="想让结果更偏新闻资讯时再开启或保留开启",
        json_schema_extra={"label": "搜索新闻", "x-widget": "switch", "advanced": True},
    )
    search_science: bool = Field(
        default=False,
        description="只有明确需要论文、研究资料时再开启",
        json_schema_extra={"label": "搜索学术内容", "x-widget": "switch", "advanced": True},
    )
    search_it: bool = Field(
        default=False,
        description="只有明确需要技术资料、开发文档时再开启",
        json_schema_extra={"label": "搜索技术内容", "x-widget": "switch", "advanced": True},
    )
    max_results: int = Field(
        default=5,
        description="建议 3-8。越大越慢，但参考信息更多",
        json_schema_extra={"label": "每次最多参考几条网页", "x-widget": "input"},
    )
    search_timeout: int = Field(
        default=60,
        description="搜索服务慢时可适当调大，通常不用改",
        json_schema_extra={"label": "搜索等待时间（秒）", "x-widget": "input", "advanced": True},
    )
    safe_search: Literal[0, 1, 2] = Field(
        default=0,
        description="通常保持关闭；只有你希望过滤敏感内容时再开启",
        json_schema_extra={
            "label": "过滤敏感内容",
            "x-widget": "select",
            "advanced": True,
            "x-option-descriptions": SAFE_SEARCH_OPTION_DESCRIPTIONS,
        },
    )

    def get_enabled_categories(self) -> list[str]:
        """返回当前启用的搜索类别列表"""
        mapping = {
            "search_general": "general",
            "search_news": "news",
            "search_science": "science",
            "search_it": "it",
        }
        return [v for k, v in mapping.items() if getattr(self, k, False)]


class FetchConfig(PluginConfigBase):
    """Crawl4AI 抓取配置"""
    __ui_label__ = "网页内容"
    __ui_icon__ = "download"
    __ui_order__ = 2

    crawl4ai_base_url: str = Field(
        default="http://192.168.1.9:11235",
        description="填你的 Crawl4AI 地址，例如 http://127.0.0.1:11235",
        json_schema_extra={"label": "网页抓取服务地址", "x-widget": "input"},
    )
    fetch_timeout: int = Field(
        default=30,
        description="网页抓取很慢或偶尔超时时再调大",
        json_schema_extra={"label": "抓取等待时间（秒）", "x-widget": "input", "advanced": True},
    )
    max_content_length: int = Field(
        default=8000,
        description="越大越完整，但处理更慢。普通用户一般不用改",
        json_schema_extra={"label": "单个网页最多读取多少内容", "x-widget": "input", "advanced": True},
    )
    summary_enabled: bool = Field(
        default=False,
        description="开启后，长网页会先做摘要，再交给 AI 使用。普通用户建议开启",
        json_schema_extra={"label": "网页太长时自动总结", "x-widget": "switch"},
    )
    filter_mode: Literal["fit", "raw", "bm25", "llm"] = Field(
        default="fit",
        description="决定怎么从网页里提取正文。普通用户保持默认即可",
        json_schema_extra={
            "label": "网页正文提取方式",
            "x-widget": "select",
            "advanced": True,
            "x-option-descriptions": FILTER_MODE_OPTION_DESCRIPTIONS,
        },
    )
    proxy: str = Field(
        default="",
        description="只有你的抓取服务必须走代理时才需要填写",
        json_schema_extra={"label": "代理地址", "x-widget": "input", "advanced": True},
    )
    proxy_username: str = Field(
        default="",
        description="只有代理要求登录时才需要填写",
        json_schema_extra={"label": "代理用户名", "x-widget": "input", "advanced": True},
    )
    proxy_password: str = Field(
        default="",
        description="只有代理要求登录时才需要填写",
        json_schema_extra={"label": "代理密码", "x-widget": "password", "advanced": True},
    )


class CacheConfig(PluginConfigBase):
    """缓存配置"""
    __ui_label__ = "缓存与加速"
    __ui_icon__ = "database"
    __ui_order__ = 3

    enable_cache: bool = Field(
        default=True,
        description="开启后，短时间内重复搜索相同内容会更快",
        json_schema_extra={"label": "加速重复搜索", "x-widget": "switch"},
    )
    cache_ttl: int = Field(
        default=3600,
        description="缓存结果保留多久，普通用户一般不用改",
        json_schema_extra={"label": "缓存保留时间（秒）", "x-widget": "input", "advanced": True},
    )
    max_cache_entries: int = Field(
        default=100,
        description="最多缓存多少条搜索或抓取结果，普通用户一般不用改",
        json_schema_extra={"label": "最多缓存多少条结果", "x-widget": "input", "advanced": True},
    )


class WebRetrieverConfig(PluginConfigBase):
    """网页检索插件总配置"""
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)


# ============================================================
# 插件主体
# ============================================================

class WebRetrieverPlugin(MaiBotPlugin):
    """MaiBot 网页检索插件"""

    config_model = WebRetrieverConfig

    # ---- 生命周期 ----

    async def on_load(self) -> None:
        """插件加载时初始化客户端与缓存"""
        self._cache = MemoryCache(
            max_entries=self.config.cache.max_cache_entries,
        )

        self._searxng = SearxNGClient(
            base_url=self.config.search.searxng_base_url,
            timeout=self.config.search.search_timeout,
            logger=self.ctx.logger,
        )

        self._crawler = Crawl4AIClient(
            base_url=self.config.fetch.crawl4ai_base_url,
            timeout=self.config.fetch.fetch_timeout,
            max_content_length=self.config.fetch.max_content_length,
            filter_mode=self.config.fetch.filter_mode,
            proxy=self.config.fetch.proxy or None,
            proxy_username=self.config.fetch.proxy_username or None,
            proxy_password=self.config.fetch.proxy_password or None,
            logger=self.ctx.logger,
        )

        # 记录最近一次搜索/抓取内容，用于自动注入 Reply 上下文
        self._last_search_content: str = ""
        self._last_fetch_content: str = ""
        self._last_reference_urls: list[dict[str, str]] = []

        self.ctx.logger.info("网页检索插件已加载")

    async def on_unload(self) -> None:
        """卸载时清理所有资源"""
        if hasattr(self, "_searxng"):
            await self._searxng.close()
        if hasattr(self, "_crawler"):
            await self._crawler.close()
        if hasattr(self, "_cache"):
            self._cache.clear()
        self.ctx.logger.info("网页检索插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置热重载"""
        if scope != "self":
            return
        self.ctx.logger.info("插件配置已更新: version=%s", version)

        # 重建 SearXNG client
        old_search = self._searxng
        self._searxng = SearxNGClient(
            base_url=self.config.search.searxng_base_url,
            timeout=self.config.search.search_timeout,
            logger=self.ctx.logger,
        )
        await old_search.close()

        # 重建 Crawl4AI client
        old_crawl = self._crawler
        self._crawler = Crawl4AIClient(
            base_url=self.config.fetch.crawl4ai_base_url,
            timeout=self.config.fetch.fetch_timeout,
            max_content_length=self.config.fetch.max_content_length,
            filter_mode=self.config.fetch.filter_mode,
            proxy=self.config.fetch.proxy or None,
            proxy_username=self.config.fetch.proxy_username or None,
            proxy_password=self.config.fetch.proxy_password or None,
            logger=self.ctx.logger,
        )
        await old_crawl.close()

        # 清空缓存（配置变更后旧缓存可能无效）
        if hasattr(self, "_cache"):
            self._cache.clear()
            self._cache.max_entries = self.config.cache.max_cache_entries


    # ========== Tool 组件 ==========

    def _cache_enabled(self) -> bool:
        return (
            hasattr(self, "_cache")
            and getattr(self.config.cache, "enable_cache", False)
        )

    def _cache_get(self, key: str):
        if not self._cache_enabled():
            return None
        return self._cache.get(key)

    def _cache_set(self, key: str, value) -> None:
        if not self._cache_enabled():
            return
        self._cache.set(key, value, ttl=self.config.cache.cache_ttl)

    async def _search_with_cache(
        self,
        *,
        query: str,
        categories: list[str],
        max_results: int,
        safe_search: int,
    ):
        cache_key = make_search_cache_key(
            query=query,
            categories=",".join(categories),
            max_results=max_results,
            show_references=self.config.plugin.show_references,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.ctx.logger.info("Search cache hit: query=%s", query)
            return cached

        response = await self._searxng.search(
            query=query,
            categories=categories,
            max_results=max_results,
            safe_search=safe_search,
        )
        self._cache_set(cache_key, response)
        return response

    async def _fetch_with_cache(self, url: str):
        cache_key = make_fetch_cache_key(
            url=url,
            show_references=self.config.plugin.show_references,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.ctx.logger.info("Fetch cache hit: url=%s", url)
            return cached

        result = await self._crawler.fetch(url)
        if not result.error_message:
            self._cache_set(cache_key, result)
        return result

    @Tool(
        "web_search",
        brief_description="搜索网页并返回 AI 总结后的答案",
        detailed_description=(
            "搜索 → 抓取 → 清洗 → 分块 → Map-Reduce 摘要 → 返回答案。\n"
            "参数说明：\n"
            "- query：string，必填。搜索关键词。\n"
            "- max_results：int，可选。搜索并抓取的结果数，默认 3。\n"
            "- categories：string，可选。搜索类别，默认用配置。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="搜索关键词",
                required=True,
            ),
            ToolParameterInfo(
                name="max_results",
                param_type=ToolParamType.INTEGER,
                description="搜索并抓取的结果数，默认 3",
                required=False,
            ),
            ToolParameterInfo(
                name="categories",
                param_type=ToolParamType.STRING,
                description="搜索类别，默认用配置",
                required=False,
            ),
        ],
    )
    async def handle_web_search(
        self, query: str, max_results: int = 3, categories: str = "", **kwargs,
    ):
        """流水线：搜索 → 抓取 → 清洗 → 分块 → Map-Reduce 摘要"""
        import asyncio, time as _time
        from urllib.parse import urlparse

        from .content_cleaner import clean_markdown
        from .content_chunker import chunk_content
        from .summarizer import map_summarize, reduce_summarize
        from .models import PageContent, PipelineResult

        t0 = _time.monotonic()
        debug: dict = {}
        stream_id = kwargs.get("stream_id", "")
        cat_list = (
            [c.strip() for c in categories.split(",") if c.strip()]
            if categories else self.config.search.get_enabled_categories()
        )

        # ── Step 1: 搜索 ──
        search_resp = await self._search_with_cache(
            query=query,
            categories=cat_list,
            max_results=max_results,
            safe_search=self.config.search.safe_search,
        )
        debug["search_ms"] = (_time.monotonic() - t0) * 1000
        search_resp.results = search_resp.results[:max_results]
        if not search_resp.results:
            return {"success": False, "content": f"搜索「{query}」未找到结果。"}

        # ── Step 2 & 3: 并行抓取 ──
        urls = [(r.url, r.title) for r in search_resp.results if r.url]
        if self.config.plugin.show_progress:
            await self.ctx.send.text(f"🔍 搜索到 {len(urls)} 条，抓取中...", stream_id)

        async def fetch_one(idx: int, url: str, title: str) -> PageContent:
            r = await self._fetch_with_cache(url)
            ok = r.error_message == ""
            md_len = len(r.markdown_content) if r.markdown_content else 0
            self.ctx.logger.info(
                "抓取 [%d/%d] %s ok=%s md_len=%d err=%s",
                idx, len(urls), url[:60], ok, md_len, r.error_message or "-",
            )
            return PageContent(
                source_id=idx, url=url, title=title,
                cleaned_md=r.markdown_content if ok else "",
            )

        pages: list[PageContent] = await asyncio.gather(
            *[fetch_one(i+1, u, t) for i, (u, t) in enumerate(urls)]
        )
        debug["fetch_ms"] = (_time.monotonic() - t0) * 1000 - debug.get("search_ms", 0)

        # ── Step 4: 清洗 ──
        for p in pages:
            if p.cleaned_md:
                p.cleaned_md = clean_markdown(
                    p.cleaned_md,
                    max_content_chars=self.config.fetch.max_content_length,
                )
        valid_pages = [p for p in pages if p.cleaned_md]
        if not valid_pages:
            self.ctx.logger.warning("所有页面抓取失败或无内容: %d 个URL", len(pages))
            for p in pages:
                self.ctx.logger.warning("  [%d] %s cleaned=%d", p.source_id, p.url[:80], len(p.cleaned_md))
            return {"success": False, "content": "所有网页抓取失败或内容为空。"}

        # ── Step 5: 分块 ──
        all_chunks = []
        for p in valid_pages:
            p.chunks = chunk_content(p.cleaned_md, p.source_id, p.title, p.url)
            all_chunks.extend(p.chunks)
        debug["chunks"] = len(all_chunks)

        # ── Step 6: Map-Reduce 摘要 ──
        if not self.config.fetch.summary_enabled:
            # 不摘要 → 直接返回清洗后的内容
            blocks = [f"搜索「{query}」共找到 {search_resp.total_count} 条，已抓取 {len(valid_pages)} 条：\n"]
            for p in valid_pages:
                blocks.append(f"## [{p.source_id}] {p.title}\n{p.cleaned_md[:3000]}\n")
            self._last_fetch_content = "\n".join(blocks)
            return {"success": True, "content": "\n".join(blocks), "query": query}

        # Map: 每页摘要
        async def _llm(prompt: str, temperature: float = 0.3) -> dict:
            return await _generate_with_preferred_task_fallback(
                self.ctx.llm,
                prompt=prompt,
                temperature=temperature,
                logger=self.ctx.logger,
            )

        self.ctx.logger.info("Map 阶段: %d 个分块", len(all_chunks))
        summaries = await map_summarize(
            _llm,
            query,
            all_chunks,
            self.ctx.logger,
            max_concurrency=MAP_SUMMARY_MAX_CONCURRENCY,
        )
        debug["map_summaries"] = len(summaries)

        if not summaries:
            # Map 全部失败 → 回退到直接给清洗内容
            blocks = [f"搜索「{query}」结果：\n"]
            for p in valid_pages:
                blocks.append(f"## [{p.source_id}] {p.title}\n{p.cleaned_md[:2000]}\n")
            return {"success": True, "content": "\n".join(blocks), "query": query}

        # Reduce: 合并
        self.ctx.logger.info("Reduce 阶段: %d 条摘要", len(summaries))
        answer = await reduce_summarize(_llm, query, summaries, self.ctx.logger)
        debug["reduce_ms"] = (_time.monotonic() - t0) * 1000

        if not answer:
            answer = "\n\n".join(summaries)

        # 来源
        sources = []
        seen = set()
        for p in valid_pages:
            if p.url not in seen:
                seen.add(p.url)
                domain = urlparse(p.url).netloc
                sources.append({"id": p.source_id, "title": p.title, "url": p.url, "domain": domain})

        result = PipelineResult(success=True, answer=answer, sources=sources, debug=debug)
        rv = result.to_llm_dict()
        self._last_fetch_content = answer
        if self.config.plugin.send_references_to_user:
            self._last_reference_urls = sources
        return rv

    # ========== Command 组件 ==========

    @Command("search", pattern=r"^/search\s+(.+)")
    async def handle_search_command(self, **kwargs):
        """处理 /search 命令"""
        query = kwargs.get("match_groups", [""])[0].strip()
        stream_id = kwargs.get("stream_id", "")
        self.ctx.logger.info("/search 命令: query=%s", query)

        if not query:
            await self.ctx.send.text("用法: /search <关键词>", stream_id)
            return True, "", 0

        await self.ctx.send.text(f"🔍 正在搜索：{query}...", stream_id)

        response = await self._search_with_cache(
            query=query,
            categories=self.config.search.get_enabled_categories(),
            max_results=self.config.search.max_results,
            safe_search=self.config.search.safe_search,
        )

        results = response.results[: self.config.search.max_results]
        if not results:
            await self.ctx.send.text(
                f"未找到与「{query}」相关的结果", stream_id
            )
            return True, f"搜索: {query} (无结果)", 0

        lines = [f"🔍 搜索「{query}」共 {response.total_count} 条结果:\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title}")
            lines.append(f"    {r.url}")
            snippet = r.snippet[:200].replace("\n", " ")
            if snippet:
                lines.append(f"    {snippet}")
            lines.append("")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, f"搜索: {query}", 0

    @Command("fetch", pattern=r"^/fetch\s+(.+)")
    async def handle_fetch_command(self, **kwargs):
        """处理 /fetch 命令"""
        url = kwargs.get("match_groups", [""])[0].strip()
        stream_id = kwargs.get("stream_id", "")
        self.ctx.logger.info("/fetch 命令: url=%s", url)

        if not url.startswith(("http://", "https://")):
            await self.ctx.send.text(
                "⚠️ URL 必须以 http:// 或 https:// 开头\n用法: /fetch <URL>",
                stream_id,
            )
            return True, "无效 URL", 0

        await self.ctx.send.text(f"📄 正在抓取：{url}...", stream_id)

        result = await self._fetch_with_cache(url)

        if result.error_message:
            await self.ctx.send.text(
                f"❌ 抓取失败: {result.error_message}", stream_id
            )
            return True, f"抓取失败: {url}", 0

        # 显示摘要（前 2000 字符）
        content = result.markdown_content
        if len(content) <= 2000:
            display = content
        else:
            display = content[:1500] + f"\n\n---\n⚠️ 内容已截断（共 {len(content)} 字符）"

        header = f"📄 **{result.title or url}**\n"
        await self.ctx.send.text(header + display, stream_id)
        return True, f"抓取: {url}", 0

    # ========== HookHandler 组件 ==========

    @HookHandler(
        "maisaka.planner.after_response",
        mode=HookMode.BLOCKING,
    )
    async def inject_search_into_reply(self, **kwargs: Any) -> dict[str, Any]:
        """注入搜索结果到 AI 上下文，并可选地向用户发送参考链接。"""
        search_content = self._last_search_content
        fetch_content = self._last_fetch_content
        ref_urls = self._last_reference_urls
        self._last_search_content = ""
        self._last_fetch_content = ""
        self._last_reference_urls = []

        tool_calls = kwargs.get("tool_calls")
        if not isinstance(tool_calls, list):
            return {"modified_kwargs": kwargs}

        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function", {})
            if not isinstance(func, dict):
                continue
            if func.get("name") != "reply":
                continue
            args = func.get("arguments", {})
            if not isinstance(args, dict):
                continue

            # 1. 注入搜索结果到 AI 参考上下文（限制长度防止撑爆回复）
            content = search_content or fetch_content
            if content:
                max_ref_len = 3000
                if len(content) > max_ref_len:
                    content = content[:max_ref_len] + "\n\n[内容过长已截断，完整数据已在上方工具返回中]"
                tag = "[搜索结果]" if search_content else "[网页内容]"
                existing = str(args.get("reference_info", "") or "").strip()
                injection = f"{tag}\n{content}\n（以上信息可作为回答参考）"
                args["reference_info"] = (
                    f"{existing}\n{injection}" if existing else injection
                )
                self.ctx.logger.info("搜索结果已注入 Reply 上下文")

            # 2. 向用户发送参考来源链接
            if self.config.plugin.send_references_to_user and ref_urls:
                seen: set[str] = set()
                lines = ["", "---", "📎 **参考来源**"]
                for r in ref_urls:
                    if r["url"] not in seen:
                        seen.add(r["url"])
                        title = r["title"].replace("\n", " ").strip()[:80]
                        lines.append(f"- [{title}]({r['url']})")
                ref_text = "\n".join(lines)

                msg_key = "message" if "message" in args else "content"
                if msg_key in args:
                    args[msg_key] = str(args[msg_key]) + ref_text
                self.ctx.logger.info("参考链接已追加到回复末尾")

            func["arguments"] = args
            tc["function"] = func
            break

        return {"modified_kwargs": kwargs}


def create_plugin():
    """插件工厂函数，MaiBot 通过此函数创建插件实例"""
    return WebRetrieverPlugin()
