import asyncio
import importlib.util
import sys
from pathlib import Path


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


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        return _FakeHTTPResponse(self.payload)

    async def aclose(self):
        return None


def test_search_trims_results_to_requested_max_results():
    module = _load_module("searxng_client", "searxng_client.py")
    client = module.SearxNGClient(base_url="https://example.com")
    client._client = _FakeHTTPClient(
        {
            "results": [
                {"url": "https://example.com/1", "title": "One", "content": "first"},
                {"url": "https://example.com/2", "title": "Two", "content": "second"},
                {"url": "https://example.com/3", "title": "Three", "content": "third"},
            ],
            "number_of_results": 3,
        }
    )

    response = asyncio.run(
        client.search(query="trim test", categories=["general"], max_results=2)
    )

    assert [result.title for result in response.results] == ["One", "Two"]
    assert response.total_count == 3
