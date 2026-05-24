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


def search_to_llm_dict(response: SearchResponse, show_references: bool = True) -> dict:
    """将搜索响应序列化为 LLM 友好的格式"""
    lines = [
        f'搜索「{response.query}」共找到 {response.total_count} 条结果，'
        f'以下是前 {len(response.results)} 条：',
        "",
    ]

    for i, r in enumerate(response.results, 1):
        lines.append(f"[{i}] {r.title}")
        if show_references:
            lines.append(f"    URL: {r.url}")
        if r.snippet:
            snippet = r.snippet.strip().replace("\n", " ")
            if len(snippet) > 180:
                snippet = snippet[:177] + "..."
            lines.append(f"    摘要: {snippet}")
        lines.append("")

    if response.unresponsive_engines:
        failed = [e[0] for e in response.unresponsive_engines]
        lines.append(f"⚠️ 以下搜索引擎暂时不可用: {', '.join(failed)}")
        lines.append("")

    if show_references:
        lines.append(
            "以上信息来自网络搜索。如需了解详情，可使用 web_fetch 打开对应链接。"
        )

    return {
        "success": True,
        "content": "\n".join(lines),
        "query": response.query,
        "total_count": response.total_count,
        "results": [
            {
                "url": r.url,
                "title": r.title,
                "snippet": r.snippet,
                "engine": r.engine,
            }
            for r in response.results
        ],
    }


def search_error_dict(query: str, message: str) -> dict:
    """搜索错误的返回格式"""
    return {
        "success": False,
        "content": f"❌ 搜索失败: {message}",
        "query": query,
        "total_count": 0,
        "results": [],
        "message": message,
    }


def fetch_to_llm_dict(result: FetchResult, show_references: bool = True) -> dict:
    """将抓取结果序列化为 LLM 友好的格式"""
    if result.error_message:
        return fetch_error_dict(result.url, result.error_message)

    header = footer = ""
    if show_references:
        header = (
            f"> 📄 **来源**: {result.url}\n"
            f"> 📌 **标题**: {result.title}\n"
            f"> ✅ **状态**: HTTP {result.status_code}\n"
            f"\n---\n\n"
        )
        footer = f"\n\n---\n📎 本文来自 {result.url}"

    full_content = header + result.markdown_content + footer

    return {
        "success": True,
        "content": full_content,
        "url": result.url,
        "title": result.title,
        "status_code": result.status_code,
        "content_length": len(result.markdown_content),
    }


def fetch_error_dict(url: str, message: str) -> dict:
    """抓取错误的返回格式"""
    return {
        "success": False,
        "content": f"❌ 抓取失败 ({url}): {message}",
        "url": url,
        "message": message,
    }


@dataclass
class PipelineResult:
    """流水线最终输出"""
    success: bool
    answer: str = ""
    sources: list[dict] = field(default_factory=list)
    debug: dict = field(default_factory=dict)

    def to_llm_dict(self) -> dict:
        return {
            "success": self.success,
            "content": self.answer,
            "sources": self.sources,
            "debug": self.debug,
        }


@dataclass
class PageContent:
    """单页抓取+清洗后的内容"""
    source_id: int
    url: str
    title: str
    cleaned_md: str
    chunks: list = field(default_factory=list)
    summary: str = ""
