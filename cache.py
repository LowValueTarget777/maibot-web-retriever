"""内存 TTL 缓存 — 零外部依赖，基于 OrderedDict + 时间戳"""

import time
from collections import OrderedDict
from typing import Any, Optional

from .models import CacheEntry


class MemoryCache:
    """简单的内存缓存，支持 TTL 过期和容量上限。

    使用 OrderedDict 维护插入顺序，惰性淘汰过期条目。
    超出 max_entries 时优先淘汰最旧的条目。
    """

    def __init__(self, max_entries: int = 100) -> None:
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self.max_entries = max_entries

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值，自动检查过期。返回 None 表示未命中或已过期。"""
        entry = self._store.get(key)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._store[key]
            return None
        # 命中后移到末尾（LRU-ish）
        self._store.move_to_end(key)
        return entry.data

    def set(self, key: str, value: Any, ttl: int) -> None:
        """存入缓存。ttl 单位为秒。"""
        self._store[key] = CacheEntry(
            data=value,
            created_at=time.monotonic(),
            ttl=ttl,
        )
        self._store.move_to_end(key)
        self._evict_if_needed()

    def invalidate(self, key: str) -> None:
        """主动失效指定 key"""
        self._store.pop(key, None)

    def clear(self) -> None:
        """清空所有缓存"""
        self._store.clear()

    @property
    def size(self) -> int:
        """当前缓存条目数（含未过期条目）"""
        self._evict_expired()
        return len(self._store)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _is_expired(entry: CacheEntry) -> bool:
        return (time.monotonic() - entry.created_at) > entry.ttl

    def _evict_expired(self) -> None:
        """淘汰所有过期条目"""
        expired = [k for k, v in self._store.items() if self._is_expired(v)]
        for k in expired:
            del self._store[k]

    def _evict_if_needed(self) -> None:
        """超出容量上限时淘汰最旧条目"""
        self._evict_expired()
        while len(self._store) > self.max_entries:
            # OrderedDict 第一个是最旧的
            self._store.popitem(last=False)
