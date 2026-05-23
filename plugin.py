"""MaiBot 网页检索插件 — 基于 SearXNG + Crawl4AI

提供 web_search 和 web_fetch 两个 Tool，以及 /search、/fetch 命令。
搜索结果自动注入 Reply 上下文。
"""

import os as _os
import sys as _sys

# 确保插件目录在 sys.path 中，解决目录名含连字符的导入问题
_plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _plugin_dir not in _sys.path:
    _sys.path.insert(0, _plugin_dir)

from typing import Any

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .cache import MemoryCache
from .crawler import Crawl4AIClient
from .models import (
    fetch_error_dict,
    fetch_to_llm_dict,
    search_error_dict,
    search_to_llm_dict,
)
from .searxng_client import (
    SearxNGClient,
    make_fetch_cache_key,
    make_search_cache_key,
)


# ============================================================
# 配置模型
# ============================================================

class PluginSectionConfig(PluginConfigBase):
    """插件基础配置"""
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class SearchConfig(PluginConfigBase):
    """SearXNG 搜索配置"""
    __ui_label__ = "搜索"
    __ui_icon__ = "search"
    __ui_order__ = 1

    searxng_base_url: str = Field(
        default="http://192.168.1.9:8800",
        description="SearXNG 实例地址",
        json_schema_extra={"label": "SearXNG 地址"},
    )
    search_general: bool = Field(
        default=True,
        description="通用网页搜索",
        json_schema_extra={"label": "通用网页"},
    )
    search_news: bool = Field(
        default=True,
        description="新闻搜索",
        json_schema_extra={"label": "新闻"},
    )
    search_science: bool = Field(
        default=False,
        description="学术搜索",
        json_schema_extra={"label": "学术"},
    )
    search_it: bool = Field(
        default=False,
        description="IT 技术搜索",
        json_schema_extra={"label": "IT 技术"},
    )
    max_results: int = Field(
        default=5,
        description="单次搜索最大返回条数",
        json_schema_extra={"label": "最大结果数"},
    )
    search_timeout: int = Field(
        default=10,
        description="搜索超时（秒）",
        json_schema_extra={"label": "搜索超时"},
    )
    safe_search: int = Field(
        default=0,
        description="安全搜索: 0=关闭, 1=中等, 2=严格",
        json_schema_extra={"label": "安全搜索"},
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
    __ui_label__ = "抓取"
    __ui_icon__ = "download"
    __ui_order__ = 2

    crawl4ai_base_url: str = Field(
        default="http://192.168.1.9:11235",
        description="Crawl4AI REST API 地址（留空则尝试本地 SDK）",
        json_schema_extra={"label": "Crawl4AI 地址"},
    )
    fetch_timeout: int = Field(
        default=30,
        description="抓取超时（秒）",
        json_schema_extra={"label": "抓取超时"},
    )
    max_content_length: int = Field(
        default=8000,
        description="返回内容最大字符数（超出截断）",
        json_schema_extra={"label": "内容长度上限"},
    )
    filter_mode: str = Field(
        default="fit",
        description="内容提取模式: fit(Readability)/raw/bm25/llm",
        json_schema_extra={"label": "提取模式"},
    )
    proxy: str = Field(
        default="",
        description="代理地址，如 http://proxy:8080，留空则不使用",
        json_schema_extra={"label": "代理地址"},
    )
    proxy_username: str = Field(
        default="",
        description="代理用户名（可选）",
        json_schema_extra={"label": "代理用户名"},
    )
    proxy_password: str = Field(
        default="",
        description="代理密码（可选）",
        json_schema_extra={"label": "代理密码"},
    )


class CacheConfig(PluginConfigBase):
    """缓存配置"""
    __ui_label__ = "缓存"
    __ui_icon__ = "database"
    __ui_order__ = 3

    enable_cache: bool = Field(
        default=True,
        description="是否启用内存缓存",
        json_schema_extra={"label": "启用缓存"},
    )
    cache_ttl: int = Field(
        default=3600,
        description="缓存过期时间（秒）",
        json_schema_extra={"label": "缓存有效期"},
    )
    max_cache_entries: int = Field(
        default=100,
        description="最大缓存条目数",
        json_schema_extra={"label": "最大缓存数"},
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

    @Tool(
        "web_search",
        brief_description="搜索网页内容",
        detailed_description=(
            "通过 SearXNG 元搜索引擎在互联网上搜索指定关键词，返回相关网页的"
            "URL、标题和内容摘要。适合需要获取最新信息、查找资料或验证事实"
            "的场景。\n"
            "参数说明：\n"
            "- query：string，必填。搜索关键词。\n"
            "- max_results：int，可选。最大返回结果数，默认 5。\n"
            "- categories：string，可选。搜索类别，如 general/news 等，默认 general。"
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
                description="最大返回结果数，默认 5",
                required=False,
            ),
            ToolParameterInfo(
                name="categories",
                param_type=ToolParamType.STRING,
                description="搜索类别，如 general/news，默认 general",
                required=False,
            ),
        ],
    )
    async def handle_web_search(
        self,
        query: str,
        max_results: int = 5,
        categories: str = "",
        **kwargs,
    ):
        """处理网页搜索工具调用"""
        self.ctx.logger.info("搜索请求: query=%s, max_results=%d", query, max_results)
        stream_id = kwargs.get("stream_id", "")

        # 类别：优先用参数，否则用配置
        if categories:
            cat_list = [c.strip() for c in categories.split(",") if c.strip()]
        else:
            cat_list = self.config.search.get_enabled_categories()

        # 1. 查缓存
        cache_key = make_search_cache_key(query, ",".join(cat_list), max_results)
        if self.config.cache.enable_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self.ctx.logger.info("搜索命中缓存: %s", query)
                return cached

        # 2. 发送进度提示
        await self.ctx.send.text(f"🔍 正在搜索：{query}...", stream_id)

        # 3. 执行搜索
        response = await self._searxng.search(
            query=query,
            categories=cat_list,
            max_results=max_results,
            safe_search=self.config.search.safe_search,
        )

        # 4. 结果截断
        response.results = response.results[:max_results]

        if not response.results:
            await self.ctx.send.text(
                f"未找到与「{query}」相关的结果", stream_id
            )
            return search_to_llm_dict(response)

        # 5. 写缓存 & 记录搜索内容
        result_dict = search_to_llm_dict(response)
        if self.config.cache.enable_cache:
            self._cache.set(cache_key, result_dict, self.config.cache.cache_ttl)
        self._last_search_content = result_dict.get("content", "")

        return result_dict

    @Tool(
        "web_fetch",
        brief_description="抓取指定网页内容",
        detailed_description=(
            "抓取并解析指定 URL 的网页内容，返回 Markdown 格式的正文。"
            "适合在搜索后进一步获取网页全文、阅读文章详情等场景。\n"
            "参数说明：\n"
            "- url：string，必填。要抓取的网页 URL（需以 http:// 或 https:// 开头）。\n"
            "- filter_mode：string，可选。内容提取模式：fit(Readability)/raw/bm25/llm，默认 fit。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="要抓取的网页 URL",
                required=True,
            ),
            ToolParameterInfo(
                name="filter_mode",
                param_type=ToolParamType.STRING,
                description="内容提取模式: fit/raw/bm25/llm，默认 fit",
                required=False,
            ),
        ],
    )
    async def handle_web_fetch(
        self,
        url: str,
        filter_mode: str = "fit",
        **kwargs,
    ):
        """处理网页抓取工具调用"""
        self.ctx.logger.info("抓取请求: url=%s, filter_mode=%s", url, filter_mode)
        stream_id = kwargs.get("stream_id", "")

        # 1. URL 校验
        if not url.startswith(("http://", "https://")):
            return fetch_error_dict(url, "URL 必须以 http:// 或 https:// 开头")

        # 2. 查缓存
        cache_key = make_fetch_cache_key(url)
        if self.config.cache.enable_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self.ctx.logger.info("抓取命中缓存: %s", url)
                return cached

        # 3. 发送进度提示
        await self.ctx.send.text(f"📄 正在抓取网页：{url}...", stream_id)

        # 4. 执行抓取
        result = await self._crawler.fetch(url)

        # 5. 写缓存 & 返回
        result_dict = fetch_to_llm_dict(result)
        if self.config.cache.enable_cache and result.error_message == "":
            self._cache.set(cache_key, result_dict, self.config.cache.cache_ttl)
        if result.error_message == "":
            self._last_fetch_content = result_dict.get("content", "")

        return result_dict

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

        response = await self._searxng.search(
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

        result = await self._crawler.fetch(url)

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
        """LLM 调用 reply 时，自动将最近搜索/抓取结果注入 reference_info。"""
        search_content = self._last_search_content
        fetch_content = self._last_fetch_content
        self._last_search_content = ""
        self._last_fetch_content = ""

        content = search_content or fetch_content
        if not content:
            return {"modified_kwargs": kwargs}

        tag = "[搜索结果]" if search_content else "[网页内容]"

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
            existing = str(args.get("reference_info", "") or "").strip()
            injection = f"{tag}\n{content}"
            args["reference_info"] = (
                f"{existing}\n{injection}" if existing else injection
            )
            func["arguments"] = args
            tc["function"] = func
            self.ctx.logger.info("搜索结果已注入 Reply 上下文")
            break

        return {"modified_kwargs": kwargs}


def create_plugin():
    """插件工厂函数，MaiBot 通过此函数创建插件实例"""
    return WebRetrieverPlugin()
