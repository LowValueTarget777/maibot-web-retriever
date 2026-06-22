import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_plugin_module():
    plugin_path = Path(__file__).resolve().parents[1] / "plugin.py"
    spec = importlib.util.spec_from_file_location(
        "maibot_web_retriever_plugin.plugin",
        plugin_path,
        submodule_search_locations=[str(plugin_path.parent)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_module(module_name: str, file_name: str):
    base_dir = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        f"maibot_web_retriever_plugin.{module_name}",
        base_dir / file_name,
        submodule_search_locations=[str(base_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _FakeSend:
    def __init__(self):
        self.messages = []

    async def text(self, message, stream_id=""):
        self.messages.append((message, stream_id))


class _FakeSearchClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeCrawler:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        return self.result


def test_clean_markdown_removes_noise_lines_but_keeps_real_content():
    cleaner = _load_module("content_cleaner", "content_cleaner.py")

    raw = (
        "Share\n"
        "\n"
        "This is the main body content that should stay after cleaning.\n"
        "\n"
        "Another paragraph with useful information for the summary.\n"
    )

    cleaned = cleaner.clean_markdown(raw, max_content_chars=1000)

    assert "Share" not in cleaned
    assert "main body content" in cleaned
    assert "Another paragraph" in cleaned


def test_handle_web_search_uses_cache_for_repeated_requests():
    module = _load_plugin_module()
    plugin = module.WebRetrieverPlugin()
    config = plugin.get_default_config()
    config["search"]["safe_search"] = 0
    config["fetch"]["max_content_length"] = 1000
    config["fetch"]["summary_enabled"] = False
    config["plugin"]["show_progress"] = False
    config["plugin"]["send_references_to_user"] = False
    config["cache"]["enable_cache"] = True
    config["cache"]["cache_ttl"] = 3600
    config["cache"]["max_cache_entries"] = 100
    plugin.set_plugin_config(config)
    plugin._ctx = SimpleNamespace(logger=_FakeLogger(), send=_FakeSend(), llm=None)
    plugin._cache = module.MemoryCache(max_entries=100)
    plugin._last_search_content = ""
    plugin._last_fetch_content = ""
    plugin._last_reference_urls = []

    search_response = SimpleNamespace(
        results=[SimpleNamespace(url="https://example.com/post", title="Example Title")],
        total_count=1,
    )
    fetch_result = SimpleNamespace(
        error_message="",
        markdown_content="# Example Title\n\nThis is useful fetched content.",
    )

    plugin._searxng = _FakeSearchClient(search_response)
    plugin._crawler = _FakeCrawler(fetch_result)

    first = asyncio.run(plugin.handle_web_search(query="cache me", max_results=1))
    second = asyncio.run(plugin.handle_web_search(query="cache me", max_results=1))

    assert first["success"] is True
    assert second["success"] is True
    assert len(plugin._searxng.calls) == 1
    assert len(plugin._crawler.calls) == 1
    assert first["content"] == second["content"]


def test_map_summarize_respects_max_concurrency_limit():
    summarizer = _load_module("summarizer", "summarizer.py")
    chunk_module = _load_module("content_chunker", "content_chunker.py")

    active = 0
    peak = 0

    async def fake_llm_generate(*, prompt, temperature=0.3):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"success": True, "response": "summary"}

    chunks = [
        chunk_module.ContentChunk(
            source_id=1,
            title="Page",
            url="https://example.com",
            chunk_index=index,
            text=f"chunk {index}",
        )
        for index in range(6)
    ]

    results = asyncio.run(
        summarizer.map_summarize(
            fake_llm_generate,
            "question",
            chunks,
            max_concurrency=2,
        )
    )

    assert len(results) == 6
    assert peak <= 2
