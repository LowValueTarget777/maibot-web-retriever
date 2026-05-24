"""Map-reduce summarization helpers."""

import asyncio
from logging import Logger
from typing import Optional


MAP_PROMPT = """请基于以下网页内容，总结与用户问题相关的关键信息。
只使用网页内容，不要编造。如果内容完全无关，回复“无关”。

用户问题：
{query}

网页内容：
{content}

相关摘要（带来源标记）："""


REDUCE_PROMPT = """根据以下多个来源的摘要，回答用户问题。

要求：
1. 先用一句话给出结论。
2. 再列出关键要点，使用编号。
3. 每个要点末尾标注来源编号，如 [1]、[2]。
4. 对不确定的信息要明确说明。
5. 控制在 1000 字以内。

用户问题：
{query}

各来源摘要：
{summaries}

最终答案："""


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
        result = await llm_generate(prompt=prompt, temperature=0.3)
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
        result = await llm_generate(prompt=prompt, temperature=0.3)
        if result.get("success") and result.get("response"):
            return result["response"].strip()
    except Exception as exc:
        if logger:
            logger.warning("Reduce summary failed: %s", exc)
    return ""
