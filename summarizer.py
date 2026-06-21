"""Map-reduce summarization helpers."""

import asyncio
from logging import Logger
from typing import Optional


MAP_PROMPT = """从下面这段网页内容里，抽取和「用户问题」直接相关的事实信息。
只罗列事实，不要写成文章，不要下结论、不要加修饰语。
严格只用网页里的内容，不要补充常识、不要编造。
如果这段内容和问题完全无关，只回复两个字：无关。

用户问题：
{query}

网页内容：
{content}

相关事实（每条一行，简短直接）："""


REDUCE_PROMPT = """下面是从多个网页里抽取的事实片段。把它们合并成一份干净的事实清单，
供别人参考后用自己的话回答用户。要求：
- 只整理事实：去重、合并相近的点；有矛盾就如实并列指出。
- 不要写成正式答案，不要开场白和总结句，不要写「综上」「总结」这类措辞，不要编号成报告。
- 保留关键的数字、时间、名称等具体信息。

用户问题：
{query}

事实片段：
{summaries}

整理后的事实清单："""


async def summarize_chunk(
    llm_generate,
    query: str,
    chunk_text: str,
    source_label: str,
    logger: Optional[Logger] = None,
) -> str:
    """Summarize one content chunk in the map stage."""
    prompt = MAP_PROMPT.format(query=query, content=chunk_text[:5000])
    try:
        result = await llm_generate(prompt=prompt, temperature=0.3, max_tokens=400)
        if result.get("success") and result.get("response"):
            summary = result["response"].strip()
            if summary == "无关":
                return ""
            return f"[{source_label}] {summary}"
    except Exception as exc:
        if logger:
            logger.warning("Map summary failed: %s", exc)
    return ""


async def map_summarize(
    llm_generate,
    query: str,
    chunks: list,
    logger: Optional[Logger] = None,
    max_concurrency: int = 3,
) -> list[str]:
    """Summarize multiple chunks with a bounded level of concurrency."""
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def _summarize_with_limit(chunk) -> str:
        label = f"S{chunk.source_id}C{chunk.chunk_index}"
        async with semaphore:
            return await summarize_chunk(
                llm_generate,
                query,
                chunk.text,
                label,
                logger,
            )

    tasks = [_summarize_with_limit(chunk) for chunk in chunks]
    results = await asyncio.gather(*tasks)
    return [result for result in results if result]


async def reduce_summarize(
    llm_generate,
    query: str,
    summaries: list[str],
    logger: Optional[Logger] = None,
) -> str:
    """Combine mapped summaries into a final answer."""
    prompt = REDUCE_PROMPT.format(
        query=query,
        summaries="\n\n---\n\n".join(summaries)[:8000],
    )
    try:
        result = await llm_generate(prompt=prompt, temperature=0.3, max_tokens=1000)
        if result.get("success") and result.get("response"):
            return result["response"].strip()
    except Exception as exc:
        if logger:
            logger.warning("Reduce summary failed: %s", exc)
    return ""
