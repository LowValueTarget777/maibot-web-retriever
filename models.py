"""数据结构定义 — 搜索/抓取结果的标准化模型"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SearchResult:
    """单条搜索结果"""
    url: str
    title: str
    snippet: str
    engine: str = ""
    category: str = "general"
    score: float = 0.0
    published_date: Optional[str] = None


@dataclass
class SearchResponse:
    """SearXNG 搜索响应"""
    query: str
    results: list[SearchResult] = field(default_factory=list)
    total_count: int = 0
    unresponsive_engines: list[list[str]] = field(default_factory=list)


@dataclass
class FetchResult:
    """Crawl4AI 抓取结果"""
    url: str
    title: str = ""
    markdown_content: str = ""
    status_code: int = 0
    fetch_time_ms: float = 0.0
    error_message: str = ""


@dataclass
class CacheEntry:
    """缓存条目"""
    data: Any
    created_at: float
    ttl: int  # 秒


@dataclass
class PageContent:
    """单页抓取+清洗后的内容"""
    source_id: int
    url: str
    title: str
    cleaned_md: str
    chunks: list = field(default_factory=list)
    summary: str = ""
