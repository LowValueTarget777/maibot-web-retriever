import asyncio
import importlib.util
from pathlib import Path
import sys


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


class _FakeLLM:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs.get("model", "")
        return dict(self._responses[model])


def test_generate_with_preferred_task_fallback_stops_after_success():
    module = _load_plugin_module()
    fake_llm = _FakeLLM(
        {
            "utils": {"success": False, "response": "", "error": "utils failed"},
            "planner": {"success": True, "response": "planner ok"},
            "replyer": {"success": True, "response": "replyer ok"},
        }
    )

    result = asyncio.run(module._generate_with_preferred_task_fallback(fake_llm, prompt="hello", temperature=0.3))

    assert result["success"] is True
    assert result["response"] == "planner ok"
    assert [call["model"] for call in fake_llm.calls] == ["utils", "planner"]


def test_generate_with_preferred_task_fallback_returns_last_error():
    module = _load_plugin_module()
    fake_llm = _FakeLLM(
        {
            "utils": {"success": False, "response": "", "error": "utils failed"},
            "planner": {"success": False, "response": "", "error": "planner failed"},
            "replyer": {"success": False, "response": "", "error": "replyer failed"},
        }
    )

    result = asyncio.run(module._generate_with_preferred_task_fallback(fake_llm, prompt="hello", temperature=0.3))

    assert result["success"] is False
    assert result["error"] == "replyer failed"
    assert [call["model"] for call in fake_llm.calls] == ["utils", "planner", "replyer"]
