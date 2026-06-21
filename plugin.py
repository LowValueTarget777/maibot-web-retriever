"""MaiBot 网页检索插件 — 搜索 → 抓取 → 清洗 → 分块 → Map-Reduce 摘要"""

import os as _os
import sys as _sys

# 确保插件目录在 sys.path 中，解决目录名含连字符的导入问题
_plugin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _plugin_dir not in _sys.path:
    _sys.path.insert(0, _plugin_dir)

import asyncio
import re
from typing import Any, Literal

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

from .cache import MemoryCache
from .crawler import Crawl4AIClient
from .searxng_client import (
    SearxNGClient,
    make_fetch_cache_key,
    make_search_cache_key,
)

# 只用 fast non-thinking 任务，避免 replyer（可能是 thinking 模型）撞 ~30s RPC 硬超时
PREFERRED_LLM_TASKS: tuple[str, str] = ("utils", "planner")
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
DISCLOSURE_MODE_DESCRIPTIONS = {
    "按需发送": "默认不发链接；有人追问来源/可信度时，麦麦才用「查来源」工具取来源并说出对应那条",
    "主动发送": "每次联网后都在末尾附上全部来源链接",
    "从不发送": "永不向用户展示链接，来源只作为事实参考给麦麦",
}
INJECTION_ACTION_DESCRIPTIONS = {
    "删除注入行": "默认。只删掉网页里疑似注入的那几行，其余正常内容照用",
    "拒绝整页": "更严：一旦发现注入就丢弃整个网页内容、不交给麦麦",
    "仅记录不删": "只在日志里记录命中、内容照常使用（用于观察/调词，不拦）",
    "关闭": "完全不做注入检测",
}
# 危险/下载类文件扩展名：这类链接不该「读」，也是恶意常见载体，直接拦
_DANGEROUS_FILE_EXTS = {
    ".exe", ".msi", ".apk", ".dmg", ".pkg", ".deb", ".rpm", ".scr", ".bat",
    ".cmd", ".com", ".pif", ".vbs", ".vbe", ".ps1", ".jar", ".sh", ".dll",
    ".iso", ".img", ".bin", ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2",
}
# 由扩展名集合生成的匹配正则：扩展名后须接分隔符或结尾，覆盖 path 与 query（如 ?file=x.exe）
_DANGEROUS_EXT_RE = re.compile(
    r"\.(?:" + "|".join(sorted((e[1:] for e in _DANGEROUS_FILE_EXTS), key=len, reverse=True)) + r")(?=[?#/&=\s]|$)",
    re.IGNORECASE,
)


async def _generate_with_preferred_task_fallback(
    llm_capability,
    *,
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 800,
    logger=None,
) -> dict:
    """按插件约定顺序调用 LLM：utils -> planner。带 max_tokens 上限以压低单次延迟。"""
    last_result: dict | None = None

    for task_name in PREFERRED_LLM_TASKS:
        result = await llm_capability.generate(
            prompt=prompt,
            model=task_name,
            temperature=temperature,
            max_tokens=max_tokens,
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
        default="1.1.0",
        description="用于兼容旧配置，通常不需要手动修改",
        json_schema_extra={"label": "配置版本", "x-widget": "input", "advanced": True},
    )
    show_progress: bool = Field(
        default=False,
        description="开启后联网时先发一条「搜索中」提示。默认关闭，让麦麦自然地回应即可",
        json_schema_extra={"label": "发送过程提示", "x-widget": "switch"},
    )
    disclosure_mode: Literal["按需发送", "主动发送", "从不发送"] = Field(
        default="按需发送",
        description="什么时候把来源链接发给用户。按需发送=有人追问来源/可信度时麦麦才取来源",
        json_schema_extra={
            "label": "来源链接披露方式",
            "x-widget": "select",
            "x-option-descriptions": DISCLOSURE_MODE_DESCRIPTIONS,
        },
    )
    inject_max_chars: int = Field(
        default=1500,
        description="注入给麦麦当参考的检索内容上限字数。太长会稀释人格、让麦麦像念稿；建议 1000-2000",
        json_schema_extra={"label": "注入参考内容上限", "x-widget": "input", "advanced": True},
    )


class SearchConfig(PluginConfigBase):
    """SearXNG 搜索配置"""
    __ui_label__ = "搜索服务"
    __ui_icon__ = "search"
    __ui_order__ = 1

    searxng_base_url: str = Field(
        default="http://127.0.0.1:8800",
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
        default="http://127.0.0.1:11235",
        description="填你的 Crawl4AI 地址，例如 http://127.0.0.1:11235",
        json_schema_extra={"label": "网页抓取服务地址", "x-widget": "input"},
    )
    crawl4ai_token: str = Field(
        default="",
        description="Crawl4AI 的 API Token（容器设了 CRAWL4AI_API_TOKEN 才需要；否则容器只绑回环连不通）",
        json_schema_extra={"label": "抓取服务 Token", "x-widget": "password", "advanced": True},
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
    fetch_full_text: bool = Field(
        default=True,
        description="开启=抓取网页全文再总结（信息全、稍慢）；关闭=只用搜索结果摘要（快、轻问题足够，如查天气/新闻标题）",
        json_schema_extra={"label": "抓取网页全文", "x-widget": "switch"},
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


class PermissionConfig(PluginConfigBase):
    """权限控制"""
    __ui_label__ = "权限控制"
    __ui_icon__ = "shield"
    __ui_order__ = 4

    mode: Literal["所有人", "白名单", "黑名单"] = Field(
        default="黑名单",
        description="所有人=不限制；白名单=仅名单内可用；黑名单=名单内禁用、其余放行（名单留空=所有人可用）",
        json_schema_extra={"label": "权限模式", "x-widget": "select"},
    )
    admin_qq_list: list[str] = Field(
        default_factory=list,
        description="管理员 QQ；管理员永远放行，不受模式与名单限制",
        json_schema_extra={"label": "管理员 QQ 列表"},
    )
    user_qq_list: list[str] = Field(
        default_factory=list,
        description="名单作用的用户 QQ：白名单模式=只有这些人能用；黑名单模式=这些人被禁用",
        json_schema_extra={"label": "用户 QQ 名单"},
    )
    restrict_tool: bool = Field(
        default=False,
        description="开启后，麦麦自己的自动联网搜索也走同一套权限（默认关闭，即麦麦任何场景都能联网）",
        json_schema_extra={"label": "限制麦麦自动联网搜索", "x-widget": "switch", "advanced": True},
    )
    reply_on_deny: bool = Field(
        default=True,
        description="命令被拒时回一条友好提示；关闭则静默拦截（无论如何都会记日志）",
        json_schema_extra={"label": "命令被拒时给出提示", "x-widget": "switch", "advanced": True},
    )


class SecurityConfig(PluginConfigBase):
    """安全拦截（在内网拦截之外，针对用户发来的链接）"""
    __ui_label__ = "安全拦截"
    __ui_icon__ = "shield-alert"
    __ui_order__ = 5

    # —— 危险文件链接 ——
    block_download_links: bool = Field(
        default=True,
        description="拦掉指向 .exe/.apk/.zip/.msi 等可执行/下载文件的链接（这类不该读，也是恶意常见载体）",
        json_schema_extra={"label": "拦截危险文件下载链接", "x-widget": "switch"},
    )
    extra_blocked_extensions: list[str] = Field(
        default_factory=list,
        description="在内置危险后缀之外再额外拦截的文件后缀，如 .torrent、.lnk（带不带点都行）",
        json_schema_extra={"label": "额外拦截的文件后缀", "advanced": True},
    )

    # —— 域名黑/白名单 ——
    blocked_domains: list[str] = Field(
        default_factory=list,
        description="手动域名黑名单：命中（含子域名）就拒绝读取。填域名即可，如 evil.com",
        json_schema_extra={"label": "域名黑名单"},
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="域名白名单：列在这里的域名（含子域名）豁免黑名单与恶意域名库（仍受内网/下载文件拦截）。用于纠正误判",
        json_schema_extra={"label": "域名白名单（豁免误判）", "advanced": True},
    )

    # —— 公开恶意域名库 ——
    threat_feed_enabled: bool = Field(
        default=True,
        description="接入公开恶意域名库，自动拦截已知钓鱼/恶意域名（需联网拉取，拉取失败则不拦、只记日志）",
        json_schema_extra={"label": "接入公开恶意域名库", "x-widget": "switch"},
    )
    threat_feed_urls: list[str] = Field(
        default_factory=lambda: ["https://urlhaus.abuse.ch/downloads/hostfile/"],
        description="恶意域名库地址，可填多个（hosts 或纯域名格式）。拉不到可换/加可达的镜像地址",
        json_schema_extra={"label": "恶意域名库地址（可多个）", "advanced": True},
    )
    threat_feed_refresh_hours: int = Field(
        default=24,
        description="多久刷新一次恶意域名库（小时）",
        json_schema_extra={"label": "恶意域名库刷新间隔（小时）", "x-widget": "input", "advanced": True},
    )

    # —— 提示词注入 ——
    injection_action: Literal["删除注入行", "拒绝整页", "仅记录不删", "关闭"] = Field(
        default="删除注入行",
        description="抓到的网页里出现疑似「提示词注入」（如『忽略上述指令』『你现在是…』）时怎么处理",
        json_schema_extra={
            "label": "提示词注入处理方式",
            "x-widget": "select",
            "x-option-descriptions": INJECTION_ACTION_DESCRIPTIONS,
        },
    )
    injection_extra_keywords: list[str] = Field(
        default_factory=list,
        description="在内置注入模式之外的额外触发词：网页某行含这些词就按上面方式处理",
        json_schema_extra={"label": "额外注入触发词", "advanced": True},
    )


class WebRetrieverConfig(PluginConfigBase):
    """网页检索插件总配置"""
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)


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
            api_token=self.config.fetch.crawl4ai_token or None,
            logger=self.ctx.logger,
        )

        # 按会话(stream_id)暂存「本轮待注入内容/待发来源」，供 after_response 钩子消费
        # 键=stream_id，值={"content": str, "refs": list}；按 stream 隔离避免多会话并发串台
        self._pending_inject: dict[str, dict] = {}
        # 按会话留存「最近一次检索来源」，供 query_sources 工具 / /出处 命令事后调取
        # 键=stream_id，值=sources 列表；最多保留 _MAX_SOURCE_STREAMS 个会话
        self._recent_sources_by_stream: dict[str, list[dict]] = {}
        self._MAX_SOURCE_STREAMS = 50

        # 公开恶意域名库（后台拉取、定时刷新；拉不到则为空集=不拦该层、只记日志）
        self._threat_domains: set[str] = set()
        self._threat_task = None
        self._threat_feed_sig: tuple = ()
        await self._start_threat_feed()

        self.ctx.logger.info("网页检索插件已加载")

    async def on_unload(self) -> None:
        """卸载时清理所有资源"""
        task = getattr(self, "_threat_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.ctx.logger.warning("取消恶意域名库任务异常: %s", exc)
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
            api_token=self.config.fetch.crawl4ai_token or None,
            logger=self.ctx.logger,
        )
        await old_crawl.close()

        # 清空缓存（配置变更后旧缓存可能无效）
        if hasattr(self, "_cache"):
            self._cache.clear()
            self._cache.max_entries = self.config.cache.max_cache_entries

        # 仅当恶意域名库相关配置变化时才重启任务（避免无关配置保存就重拉 urlhaus）
        if self._threat_feed_signature() != getattr(self, "_threat_feed_sig", ()):
            await self._start_threat_feed()

    def get_webui_config_schema(self, **kwargs):
        """把配置页渲染成「一节一个标签页」（基础/搜索/网页内容/缓存/权限/安全）。
        SDK 默认 layout=auto 是单页长条；这里改成 tabs（CLAUDE.md §13 验证模式）。"""
        try:
            schema = super().get_webui_config_schema(**kwargs)
        except Exception:
            return {}  # 永不让配置页 500
        if isinstance(schema, dict) and isinstance(schema.get("sections"), dict):
            sections = schema["sections"]
            ordered = sorted(sections.items(), key=lambda kv: (kv[1] or {}).get("order", 0))
            schema["layout"] = {
                "type": "tabs",
                "tabs": [
                    {
                        "id": name,
                        "title": (sec or {}).get("title") or name,
                        "icon": (sec or {}).get("icon"),
                        "order": (sec or {}).get("order", 0),
                        "sections": [name],
                    }
                    for name, sec in ordered
                ],
            }
        return schema


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
        cache_key = make_fetch_cache_key(url=url)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.ctx.logger.info("Fetch cache hit: url=%s", url)
            return cached

        result = await self._crawler.fetch(url)
        if not result.error_message:
            self._cache_set(cache_key, result)
        return result

    # ========== 权限控制 ==========

    @staticmethod
    def _norm_qq(raw) -> str:
        """提取纯数字 QQ；空 / 全 0 / 非数字 → 空串（避免 qq=0 误匹配）。"""
        digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
        return digits if digits and digits.strip("0") else ""

    def _check_permission(self, user_id: str) -> tuple[bool, str]:
        """按 [permission] 配置判定 user_id 是否放行，返回 (是否允许, 拒绝原因)。"""
        perm = self.config.permission
        qq = self._norm_qq(user_id)

        admins = {self._norm_qq(x) for x in perm.admin_qq_list}
        admins.discard("")
        if qq and qq in admins:
            return True, ""

        users = {self._norm_qq(x) for x in perm.user_qq_list}
        users.discard("")
        mode = perm.mode
        if mode == "所有人":
            return True, ""
        if mode == "白名单":
            return (True, "") if qq in users else (False, "不在白名单")
        if mode == "黑名单":
            return (False, "在黑名单") if qq in users else (True, "")
        # Literal 已约束，理论不可达；保守拒并记日志
        self.ctx.logger.warning("[权限] 未知模式 mode=%s，保守拒绝", mode)
        return False, "未知权限模式"

    async def _deny_command(self, cmd: str, user_id: str, stream_id: str, reason: str):
        """命令被权限拒绝：必记日志；按配置回友好提示；拦截原消息。"""
        self.ctx.logger.info(
            "[权限] 拒绝 %s：user=%s 原因=%s", cmd, self._norm_qq(user_id) or "未知", reason
        )
        if self.config.permission.reply_on_deny:
            await self.ctx.send.text(f"你暂时没有使用 {cmd} 的权限（{reason}）", stream_id)
        return True, f"{cmd} denied: {reason}", 1

    def _check_tool_permission(self, kwargs: dict) -> tuple[bool, str]:
        """工具级权限：restrict_tool 关闭则放行；开启则从 message 解析发言者 QQ 判定。"""
        if not self.config.permission.restrict_tool:
            return True, ""
        uinfo = ((kwargs.get("message") or {}).get("message_info") or {}).get("user_info") or {}
        return self._check_permission(str(uinfo.get("user_id") or ""))

    @staticmethod
    def _is_internal_url(url: str) -> bool:
        """是否为内网/本机地址（裸私有 IP、整数编码 IP 或 localhost）。拦截 SSRF：
        公网 IP 与普通域名放行；私有/回环/链路本地 IP 及 localhost 拦截。
        注：不做 DNS 解析，故解析到内网的「域名」不在此拦截范围（属已知局限）。"""
        from urllib.parse import urlparse
        import ipaddress
        host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
        if not host or host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            return not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        # 整数编码的 IP（十进制 2130706433 / 十六进制 0x7f000001 / 八进制 0177…），inet_aton 会解析
        try:
            if host.startswith(("0x", "0X")):
                n = int(host, 16)
            elif host.startswith("0") and len(host) > 1 and host[1:].isdigit():
                n = int(host, 8)
            elif host.isdigit():
                n = int(host)
            else:
                n = None
            if n is not None and 0 <= n <= 0xFFFFFFFF:
                return not ipaddress.ip_address(n).is_global
        except (ValueError, TypeError):
            pass
        return False

    def _is_dangerous_file_url(self, url: str) -> bool:
        """URL 是否指向可执行/下载类文件：查 path 与 query（unquote 防 %20/%00 绕过），含用户自定义后缀。"""
        from urllib.parse import urlparse, unquote
        p = urlparse(url)
        hay = (unquote(p.path) + "?" + unquote(p.query)).lower()
        if _DANGEROUS_EXT_RE.search(hay):
            return True
        for ext in self.config.security.extra_blocked_extensions:
            e = str(ext).strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            if re.search(re.escape(e) + r"(?=[?#/&=\s]|$)", hay):
                return True
        return False

    @staticmethod
    def _host_and_parents(host: str) -> set:
        """host 及其父域名集合，供域名黑名单按子域匹配。a.b.com → {a.b.com, b.com}。"""
        host = (host or "").strip().lower().rstrip(".")
        parts = host.split(".") if host else []
        out = {".".join(parts[i:]) for i in range(len(parts) - 1)}
        if host:
            out.add(host)
        return out

    @staticmethod
    def _domain_set(items) -> set:
        """规范化域名列表为集合：去一层 *. / . 前缀与尾点、转小写。"""
        out = set()
        for d in items or []:
            s = str(d).strip().lower().rstrip(".")
            if s.startswith("*."):
                s = s[2:]
            elif s.startswith("."):
                s = s[1:]
            if s:
                out.add(s)
        return out

    def _url_block_reason(self, url: str) -> str | None:
        """综合安全判断：命中则返回拦截原因（短语），放行返回 None。"""
        from urllib.parse import urlparse
        sec = self.config.security
        if self._is_internal_url(url):
            return "内网/本机地址"
        if sec.block_download_links and self._is_dangerous_file_url(url):
            return "可执行/下载文件链接"
        host = (urlparse(url).hostname or "").strip().lower()
        if host:
            hosts = self._host_and_parents(host)
            if hosts & self._domain_set(sec.allowed_domains):
                return None   # 白名单豁免黑名单/恶意库（内网/下载已在上面拦过）
            if hosts & self._domain_set(sec.blocked_domains):
                return "命中域名黑名单"
            if hosts & self._threat_domains:
                return "命中恶意域名库"
        return None

    def _apply_injection_guard(self, content: str, where: str) -> tuple[str, bool]:
        """按 injection_action 处理网页内容里的疑似提示词注入。
        返回 (处理后内容, 是否应拒绝整页)。命中才动手、才记日志。"""
        from .content_cleaner import redact_injection
        sec = self.config.security
        action = sec.injection_action
        if action == "关闭" or not content:
            return content, False
        cleaned, n = redact_injection(content, sec.injection_extra_keywords)
        if not n:
            return content, False
        if action == "拒绝整页":
            self.ctx.logger.warning("%s 检测到疑似注入 %d 行，按配置拒绝整页", where, n)
            return "", True
        if action == "仅记录不删":
            self.ctx.logger.warning("%s 检测到疑似注入 %d 行（仅记录、未删）", where, n)
            return content, False
        # 删除注入行（默认）
        self.ctx.logger.warning("%s 移除疑似注入 %d 行", where, n)
        return cleaned, False

    # ========== 恶意域名库（后台拉取） ==========

    def _threat_feed_signature(self) -> tuple:
        """恶意域名库相关配置的签名，用于判断是否需要重启任务。"""
        sec = self.config.security
        return (sec.threat_feed_enabled, tuple(sec.threat_feed_urls), sec.threat_feed_refresh_hours)

    async def _start_threat_feed(self) -> None:
        """按配置启动/重启恶意域名库后台刷新任务（等待旧任务取消完成，避免双跑）。"""
        old = getattr(self, "_threat_task", None)
        if old is not None and not old.done():
            old.cancel()
            try:
                await old
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.ctx.logger.warning("取消旧恶意域名库任务异常: %s", exc)
        self._threat_task = None
        self._threat_feed_sig = self._threat_feed_signature()
        if self.config.security.threat_feed_enabled:
            self._threat_task = asyncio.create_task(self._threat_feed_loop())

    async def _threat_feed_loop(self) -> None:
        """定时拉取恶意域名库；拉取失败 fail-open（保留旧集/空集），记日志。"""
        try:
            while True:
                await self._load_threat_feed()
                hours = max(1, int(self.config.security.threat_feed_refresh_hours))
                await asyncio.sleep(hours * 3600)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.error("恶意域名库刷新循环异常: %s", exc, exc_info=True)

    async def _load_threat_feed(self) -> None:
        """拉取并解析（可多个）恶意域名库到 self._threat_domains；全部失败则保留旧集、记 warning。"""
        import httpx
        import ipaddress
        urls = [str(u).strip() for u in self.config.security.threat_feed_urls if str(u).strip()]
        if not urls:
            return
        domains: set[str] = set()
        any_ok = False
        truncated = False
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    text = resp.text
            except Exception as exc:
                self.ctx.logger.warning("恶意域名库拉取失败 url=%s err=%s", url[:80], exc)
                continue
            any_ok = True
            for line in text.splitlines():
                line = line.strip()
                if not line or line[0] in "#!;":
                    continue
                # 兼容 hosts 格式（"0.0.0.0 domain"）与纯域名列表
                parts = line.split()
                cand = (parts[-1] if len(parts) >= 2 else parts[0]).strip().lower().rstrip(".")
                if not cand or "." not in cand or cand == "localhost":
                    continue
                try:
                    ipaddress.ip_address(cand)  # 是 IP（如 hosts 的 sink 0.0.0.0）→ 非域名，跳过
                    continue
                except ValueError:
                    pass
                domains.add(cand)
                if len(domains) >= 1_000_000:  # 上限保护，避免异常大表撑爆内存
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            self.ctx.logger.warning("恶意域名库超过 100 万条已截断，其余未载入")
        if domains:
            self._threat_domains = domains
            self.ctx.logger.info("恶意域名库已加载 %d 条（来自 %d 个源）", len(domains), len(urls))
        elif any_ok:
            self.ctx.logger.warning("恶意域名库为空或解析不到域名")
        # 全部源都失败 → 不更新（保留旧集，fail-open），每个失败已记 warning

    # ========== 来源留存 ==========

    def _store_recent_sources(self, stream_id: str, sources: list[dict]) -> None:
        """按会话留存最近一次检索来源；超出上限时淘汰最旧的会话。"""
        if not stream_id or not sources:
            return
        self._recent_sources_by_stream[stream_id] = sources
        # 简单容量控制：超出则按插入顺序淘汰最旧的（dict 在 py3.7+ 保序）
        while len(self._recent_sources_by_stream) > self._MAX_SOURCE_STREAMS:
            oldest = next(iter(self._recent_sources_by_stream))
            self._recent_sources_by_stream.pop(oldest, None)

    @staticmethod
    def _format_source_lines(sources: list[dict]) -> list[str]:
        """把来源列表渲染成「- 标题\\n  链接」行，去重 URL。"""
        seen: set[str] = set()
        lines: list[str] = []
        for r in sources:
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                title = (r.get("title") or "").replace("\n", " ").strip()[:80]
                lines.append(f"- {title}\n  {url}")
        return lines

    @Tool(
        "web_search",
        brief_description="联网搜索，返回相关事实素材（供你用自己的话回答）",
        detailed_description=(
            "需要最新/外部信息时调用：联网搜索相关网页并整理成干净的事实素材返回。\n"
            "返回的是供你参考的事实，不是成品答案——请用你自己平时的口吻自然作答，不要照搬、不要列编号。\n"
            "参数：\n"
            "- query：string，必填。搜索关键词。\n"
            "- max_results：int，可选。参考的结果数，默认 3。\n"
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
        """流水线：搜索 →（按配置抓取全文/仅用摘要）→ 把干净事实素材交给麦麦人格"""
        import asyncio, time as _time
        from urllib.parse import urlparse

        from .content_cleaner import clean_markdown
        from .content_chunker import chunk_content
        from .summarizer import map_summarize, reduce_summarize
        from .models import PageContent

        t0 = _time.monotonic()
        debug: dict = {}
        stream_id = kwargs.get("stream_id", "")

        # ── 工具级权限（默认 restrict_tool=False 不限制，麦麦任何场景可联网）──
        ok, reason = self._check_tool_permission(kwargs)
        if not ok:
            self.ctx.logger.info("[权限] web_search 被拦截 原因=%s", reason)
            return {"success": False, "content": "（当前会话没有联网搜索权限，请用你自己的话委婉告诉用户这点）"}

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
            self.ctx.logger.warning(
                "web_search 无结果 query=%s 引擎不可用=%s",
                query, search_resp.unresponsive_engines,
            )
            return {"success": False, "content": "（这次没查到相关网页，请用你自己的话自然地告诉用户没查到）"}

        # ── 安全：丢弃命中拦截的结果 URL（让 web_search 抓取也走 fetch_url/fetch 同一道闸门）──
        kept = []
        for r in search_resp.results:
            reason = self._url_block_reason(r.url) if r.url else "无效URL"
            if reason:
                self.ctx.logger.warning("web_search 丢弃被拦结果 reason=%s url=%s", reason, (r.url or "")[:60])
                continue
            kept.append(r)
        search_resp.results = kept
        if not search_resp.results:
            self.ctx.logger.warning("web_search 结果全部被安全拦截 query=%s", query)
            return {"success": False, "content": "（搜到的网页都被安全过滤了，请用你自己的话告诉用户没有可用结果）"}

        # 搜索结果级来源（标题/链接/域名）——轻量路径与抓取全失败降级时复用
        search_sources = [
            {"id": i + 1, "title": r.title, "url": r.url, "domain": urlparse(r.url).netloc}
            for i, r in enumerate(search_resp.results) if r.url
        ]

        def _snippet_facts() -> str:
            facts = []
            for r in search_resp.results:
                snip = (r.snippet or "").replace("\n", " ").strip()
                if snip:
                    facts.append(f"- {r.title}：{snip}")
            content = "\n".join(facts)
            # snippet/title 是页面 meta（攻击者可控）→ 同样过注入处理
            content, _reject = self._apply_injection_guard(content, f"web_search摘要[{query[:30]}]")
            return "" if _reject else content

        # ── 轻量路径：只用搜索摘要、不抓全文（fetch_full_text=False）──
        if not self.config.fetch.fetch_full_text:
            content = _snippet_facts()
            if not content:
                self.ctx.logger.warning("轻量路径无可用摘要 query=%s", query)
                return {"success": False, "content": "（搜到了网页但没有可用摘要，请用你自己的话告诉用户没查到细节）"}
            self._set_disclosure(stream_id, content, search_sources)
            self.ctx.logger.info("web_search 轻量路径完成 query=%s", query)
            return {"success": True, "content": content, "query": query}

        # ── Step 2&3: 并行抓取全文 ──
        urls = [(r.url, r.title) for r in search_resp.results if r.url]
        if self.config.plugin.show_progress:
            await self.ctx.send.text(f"正在帮你查「{query}」，稍等一下～", stream_id)

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

        # ── Step 4: 清洗（+ 按 injection_action 处理疑似提示词注入）──
        for p in pages:
            if p.cleaned_md:
                p.cleaned_md = clean_markdown(
                    p.cleaned_md,
                    max_content_chars=self.config.fetch.max_content_length,
                )
                p.cleaned_md, _reject = self._apply_injection_guard(p.cleaned_md, f"web_search[{p.url[:50]}]")
                if _reject:
                    p.cleaned_md = ""  # 拒绝整页 → 丢弃该页内容
        valid_pages = [p for p in pages if p.cleaned_md]
        if not valid_pages:
            # 已验证降级：抓取失败真实会发生（见上方抓取日志），降级用搜索摘要、记日志
            self.ctx.logger.warning("所有页面抓取失败，降级到搜索摘要 query=%s 共%d个URL", query, len(pages))
            content = _snippet_facts()
            if not content:
                return {"success": False, "content": "（网页都没抓到内容，请用你自己的话告诉用户这次没查到）"}
            self._set_disclosure(stream_id, content, search_sources)
            return {"success": True, "content": content, "query": query}

        # 抓取成功的来源（去重）
        sources, seen = [], set()
        for p in valid_pages:
            if p.url not in seen:
                seen.add(p.url)
                sources.append({"id": p.source_id, "title": p.title, "url": p.url,
                                "domain": urlparse(p.url).netloc})

        # ── Step 5: 分块 ──
        all_chunks = []
        for p in valid_pages:
            p.chunks = chunk_content(p.cleaned_md, p.source_id, p.title, p.url)
            all_chunks.extend(p.chunks)
        debug["chunks"] = len(all_chunks)

        # 不做摘要 → 直接把清洗后的正文当事实素材
        if not self.config.fetch.summary_enabled:
            content = "\n\n".join(f"【{p.title}】\n{p.cleaned_md[:2000]}" for p in valid_pages)
            self._set_disclosure(stream_id, content, sources)
            return {"success": True, "content": content, "query": query}

        # ── Map-Reduce 摘要 ──
        async def _llm(prompt: str, temperature: float = 0.3, max_tokens: int = 800) -> dict:
            return await _generate_with_preferred_task_fallback(
                self.ctx.llm,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                logger=self.ctx.logger,
            )

        self.ctx.logger.info("Map 阶段: %d 个分块", len(all_chunks))
        summaries = await map_summarize(
            _llm, query, all_chunks, self.ctx.logger,
            max_concurrency=MAP_SUMMARY_MAX_CONCURRENCY,
        )
        debug["map_summaries"] = len(summaries)
        if not summaries:
            # 已验证降级：Map 全判无关时真实会发生，降级用清洗正文、记日志
            self.ctx.logger.warning("Map 全部无相关摘要，降级到正文 query=%s", query)
            content = "\n\n".join(f"【{p.title}】\n{p.cleaned_md[:1500]}" for p in valid_pages)
            self._set_disclosure(stream_id, content, sources)
            return {"success": True, "content": content, "query": query}

        # 单条摘要无需 Reduce，省一次 LLM 调用
        if len(summaries) == 1:
            answer = summaries[0]
            self.ctx.logger.info("仅 1 条摘要，跳过 Reduce")
        else:
            self.ctx.logger.info("Reduce 阶段: %d 条摘要", len(summaries))
            answer = await reduce_summarize(_llm, query, summaries, self.ctx.logger)
            if not answer:
                answer = "\n\n".join(summaries)
        debug["reduce_ms"] = (_time.monotonic() - t0) * 1000

        self._set_disclosure(stream_id, answer, sources)
        return {"success": True, "content": answer, "query": query, "sources": sources, "debug": debug}

    def _set_disclosure(self, stream_id: str, content: str, sources: list[dict]) -> None:
        """统一收尾：留存来源、并按会话(stream_id)暂存待注入内容/待发链接，供钩子消费。"""
        self._store_recent_sources(stream_id, sources)
        if not stream_id:
            self.ctx.logger.warning("检索结果缺少 stream_id，无法注入本轮回复")
            return
        refs = sources if self.config.plugin.disclosure_mode == "主动发送" else []
        self._pending_inject[stream_id] = {"content": content, "refs": refs}
        # 容量控制：与来源留存一致，避免无界增长
        while len(self._pending_inject) > self._MAX_SOURCE_STREAMS:
            self._pending_inject.pop(next(iter(self._pending_inject)), None)

    @Tool(
        "query_sources",
        brief_description="查询最近一次联网检索的网页来源（标题+链接）",
        detailed_description=(
            "当用户追问某条信息的来源、出处、链接，或问「这哪来的 / 可信吗 / 真的假的 / 求证 / 在哪看的」时，"
            "调用本工具拿到最近一次联网检索的网页来源列表（每条含标题和链接）。"
            "拿到后用你自己的话告诉用户对应来源；如果用户只问其中某一条，就只说那一条的链接，不要把全部链接一股脑甩出来。"
        ),
    )
    async def handle_query_sources(self, **kwargs):
        """供麦麦在用户追问来源时调用，返回最近检索来源供其挑选引用。"""
        stream_id = kwargs.get("stream_id", "")
        if self.config.plugin.disclosure_mode == "从不发送":
            self.ctx.logger.info("[来源] query_sources 被「从不发送」模式拒绝 stream=%s", stream_id)
            return {"success": False, "content": "（当前设置为不向用户展示来源链接，请用你自己的话委婉说明）"}
        sources = self._recent_sources_by_stream.get(stream_id) or []
        if not sources:
            return {"success": True, "content": "（最近没有可提供的检索来源）"}
        lines = self._format_source_lines(sources)
        return {"success": True, "content": "最近一次联网检索的来源（请挑用户问的那条说）：\n" + "\n".join(lines)}

    @Tool(
        "fetch_url",
        brief_description="读取/查看一个网页链接的内容（用户发来链接、或让你看某个链接时用）",
        detailed_description=(
            "当用户发来一个网页链接、或让你打开/看看/查一下某个链接是什么时，调用本工具读取该网页正文。\n"
            "返回的是网页事实内容供你参考——请用你自己的话自然地告诉用户这个网页讲的是什么，不要照搬、不要列编号。\n"
            "参数：\n"
            "- url：string，必填。要读取的完整网址（http:// 或 https:// 开头）。"
        ),
        parameters=[
            ToolParameterInfo(
                name="url",
                param_type=ToolParamType.STRING,
                description="要读取的网页完整链接",
                required=True,
            ),
        ],
    )
    async def handle_fetch_url(self, url: str = "", **kwargs):
        """读取用户给出的单个网页链接，返回正文事实素材供麦麦用自己的话描述。"""
        from urllib.parse import urlparse
        from .content_cleaner import clean_markdown

        stream_id = kwargs.get("stream_id", "")

        # 工具级权限（与 web_search 同一套）
        ok, reason = self._check_tool_permission(kwargs)
        if not ok:
            self.ctx.logger.info("[权限] fetch_url 被拦截 原因=%s", reason)
            return {"success": False, "content": "（当前会话没有读取网页的权限，请用你自己的话委婉告诉用户这点）"}

        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            self.ctx.logger.warning("fetch_url 非法链接 url=%s", url[:80])
            return {"success": False, "content": "（没拿到有效的网页链接，请用你自己的话让用户把完整链接发出来）"}

        # 综合安全拦截（内网 / 下载文件链接 / 域名黑名单 / 恶意域名库）
        block = self._url_block_reason(url)
        if block:
            self.ctx.logger.warning("fetch_url 安全拦截 reason=%s url=%s", block, url[:80])
            return {"success": False, "content": f"（这个链接被安全拦截了：{block}，我不能读取，请用你自己的话告诉用户）"}

        result = await self._fetch_with_cache(url)
        if result.error_message:
            self.ctx.logger.warning("fetch_url 抓取失败 url=%s err=%s", url[:80], result.error_message)
            return {"success": False, "content": "（这个网页没打开成功，可能它挡了抓取或超时了，请用你自己的话告诉用户没读到）"}

        content = clean_markdown(result.markdown_content, max_content_chars=self.config.fetch.max_content_length)
        if not content:
            self.ctx.logger.warning("fetch_url 正文为空 url=%s", url[:80])
            return {"success": False, "content": "（网页打开了但没读到正文，请用你自己的话告诉用户）"}

        content, _reject = self._apply_injection_guard(content, f"fetch_url[{url[:50]}]")
        if _reject:
            return {"success": False, "content": "（这个网页含可疑的指令注入内容，出于安全我没读它，请用你自己的话告诉用户）"}
        if not content:
            return {"success": False, "content": "（网页处理后没剩下有效内容，请用你自己的话告诉用户）"}

        title = result.title or urlparse(url).netloc
        material = f"【{title}】\n{content[:4000]}"
        source = [{"id": 1, "title": title, "url": url, "domain": urlparse(url).netloc}]
        self._set_disclosure(stream_id, material, source)
        self.ctx.logger.info("fetch_url 完成 url=%s len=%d", url[:60], len(content))
        return {"success": True, "content": material, "url": url, "title": title}

    # ========== Command 组件 ==========

    @Command("search", pattern=r"^/search\s+(?P<query>.+)")
    async def handle_search_command(self, **kwargs):
        """处理 /search 命令"""
        # 宿主注入的是命名捕获组 dict（键名 matched_groups，非 match_groups）；
        # 取不到时回退到 text 自行解析（参考 hello_world_plugin 写法）
        query = ((kwargs.get("matched_groups") or {}).get("query") or "").strip()
        if not query:
            m = re.match(r"^/search\s+(.+)", kwargs.get("text") or "")
            query = m.group(1).strip() if m else ""
        stream_id = kwargs.get("stream_id", "")
        user_id = kwargs.get("user_id", "")
        self.ctx.logger.info("/search 命令: query=%s", query)

        ok, reason = self._check_permission(user_id)
        if not ok:
            return await self._deny_command("/search", user_id, stream_id, reason)

        if not query:
            await self.ctx.send.text("用法: /search <关键词>", stream_id)
            return True, "", 0

        if self.config.plugin.show_progress:
            await self.ctx.send.text(f"正在搜索：{query}…", stream_id)

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

    @Command("fetch", pattern=r"^/fetch\s+(?P<url>.+)")
    async def handle_fetch_command(self, **kwargs):
        """处理 /fetch 命令"""
        # 同上：命名捕获组 dict + text 兜底
        url = ((kwargs.get("matched_groups") or {}).get("url") or "").strip()
        if not url:
            m = re.match(r"^/fetch\s+(.+)", kwargs.get("text") or "")
            url = m.group(1).strip() if m else ""
        stream_id = kwargs.get("stream_id", "")
        user_id = kwargs.get("user_id", "")
        self.ctx.logger.info("/fetch 命令: url=%s", url)

        ok, reason = self._check_permission(user_id)
        if not ok:
            return await self._deny_command("/fetch", user_id, stream_id, reason)

        if not url.startswith(("http://", "https://")):
            await self.ctx.send.text(
                "URL 要以 http:// 或 https:// 开头\n用法: /fetch <网址>",
                stream_id,
            )
            return True, "无效 URL", 0

        block = self._url_block_reason(url)
        if block:
            self.ctx.logger.warning("/fetch 安全拦截 reason=%s url=%s", block, url[:80])
            await self.ctx.send.text(f"这个链接被安全拦截了（{block}），不能抓取～", stream_id)
            return True, f"安全拦截:{block}", 0

        if self.config.plugin.show_progress:
            await self.ctx.send.text(f"正在抓取：{url}…", stream_id)

        result = await self._fetch_with_cache(url)

        if result.error_message:
            await self.ctx.send.text(
                f"这个网页没抓成功：{result.error_message}", stream_id
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

    @Command("sources", pattern=r"^/(?:sources|出处|来源)\s*$")
    async def handle_sources_command(self, **kwargs):
        """事后手动调取本会话最近一次检索的来源链接。"""
        stream_id = kwargs.get("stream_id", "")
        if self.config.plugin.disclosure_mode == "从不发送":
            await self.ctx.send.text("当前设置为不展示来源链接～", stream_id)
            return True, "出处: 已禁用", 0
        sources = self._recent_sources_by_stream.get(stream_id) or []
        if not sources:
            await self.ctx.send.text("最近没有可展示的检索来源～", stream_id)
            return True, "出处: 无", 0
        lines = self._format_source_lines(sources)
        await self.ctx.send.text("📎 最近检索来源\n" + "\n".join(lines), stream_id)
        return True, "出处: 已展示", 0

    # ========== HookHandler 组件 ==========

    @HookHandler(
        "maisaka.planner.after_response",
        mode=HookMode.BLOCKING,
    )
    async def inject_search_into_reply(self, **kwargs: Any) -> dict[str, Any]:
        """注入检索结果到 reply.reference_info，并可选地向用户单独发送参考链接。"""
        # 用本轮 session_id 精确取本会话待注入内容（宿主 chat_loop_service.py:995 传 session_id），
        # 按 stream 隔离，避免多会话并发时单槽串台
        stream_id = str(kwargs.get("session_id") or "")
        pending = self._pending_inject.pop(stream_id, None) if stream_id else None
        if not pending:
            # 本会话本轮没有检索结果 → 正常放行（预期内早退，不记日志以免刷屏）
            return {"modified_kwargs": kwargs}
        fetch_content = pending.get("content") or ""
        ref_urls = pending.get("refs") or []

        tool_calls = kwargs.get("tool_calls")
        if not isinstance(tool_calls, list):
            self.ctx.logger.warning(
                "after_response 注入跳过：tool_calls 非 list type=%s",
                type(tool_calls).__name__,
            )
            await self._send_reference_links(ref_urls, stream_id)
            return {"modified_kwargs": kwargs}

        injected = False
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

            # 注入检索结果到 reply 的 reference_info（reply 工具已验证的内部透传参数）
            if fetch_content:
                content = fetch_content
                limit = max(200, int(self.config.plugin.inject_max_chars))
                if len(content) > limit:
                    content = content[:limit] + "\n…（更多内容略）"
                existing = str(args.get("reference_info", "") or "").strip()
                # 元指令：让人格层用自己的口吻自然作答，而非照搬素材（去机器味的关键）
                injection = (
                    "[这是我刚联网查到的资料，仅供你参考。请用你自己平时说话的口吻自然地回答，"
                    "不要照搬下面的措辞、不要列编号、不要标来源编号]\n" + content
                )
                args["reference_info"] = (
                    f"{existing}\n{injection}" if existing else injection
                )
                func["arguments"] = args
                tc["function"] = func
                self.ctx.logger.info("检索结果已注入 reply.reference_info")
            injected = True
            break

        if fetch_content and not injected:
            self.ctx.logger.warning("after_response 注入：未找到 reply 工具调用，检索结果未注入")

        # 参考链接改用 send.text 单独发（reply 工具无 message/content 参数可改写）
        await self._send_reference_links(ref_urls, stream_id)
        return {"modified_kwargs": kwargs}

    async def _send_reference_links(self, ref_urls: list, stream_id: str) -> None:
        """把来源链接作为单独一条消息发给用户（仅「主动发送」模式会传入非空 ref_urls）。"""
        if not ref_urls:
            return
        if not stream_id:
            self.ctx.logger.warning("发参考链接失败：本轮缺少 stream_id，跳过")
            return
        lines = self._format_source_lines(ref_urls)
        if lines:
            await self.ctx.send.text("📎 参考来源\n" + "\n".join(lines), stream_id)
            self.ctx.logger.info("参考链接已主动发送 count=%d", len(lines))


def create_plugin():
    """插件工厂函数，MaiBot 通过此函数创建插件实例"""
    return WebRetrieverPlugin()
