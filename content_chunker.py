"""内容分块 — 按标题/段落切分，保留元数据"""

from dataclasses import dataclass, field


@dataclass
class ContentChunk:
    """内容分块"""
    source_id: int
    title: str
    url: str
    chunk_index: int
    text: str


def chunk_content(
    text: str,
    source_id: int,
    title: str,
    url: str,
    chunk_size: int = 4000,
) -> list[ContentChunk]:
    """将清洗后的内容按标题和段落切分成块

    优先按 ## 标题切分，若无标题则按段落自然分界。
    每块控制在 chunk_size 字符左右。
    """
    if not text.strip():
        return []

    # 按 ## 标题切分
    sections = _split_by_headings(text)

    chunks: list[ContentChunk] = []
    current_block: list[str] = []
    current_len = 0
    chunk_idx = 0

    for section in sections:
        # 如果加入当前段落后不超限，就追加
        sec_len = len(section)
        if current_len + sec_len <= chunk_size:
            current_block.append(section)
            current_len += sec_len
        else:
            # 当前块满了，输出
            if current_block:
                chunks.append(ContentChunk(
                    source_id=source_id,
                    title=title,
                    url=url,
                    chunk_index=chunk_idx,
                    text="\n\n".join(current_block),
                ))
                chunk_idx += 1
            # 新段落开始
            current_block = [section]
            current_len = sec_len

    # 最后一块
    if current_block:
        chunks.append(ContentChunk(
            source_id=source_id,
            title=title,
            url=url,
            chunk_index=chunk_idx,
            text="\n\n".join(current_block),
        ))

    return chunks


def _split_by_headings(text: str) -> list[str]:
    """按 Markdown 标题切分"""
    import re

    # 在 ## 或 # 处切分
    parts = re.split(r"\n(?=#{1,3}\s)", text)
    return [p.strip() for p in parts if p.strip()]
