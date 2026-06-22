import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def load_plugin_module():
    package_name = "maibot_web_retriever_under_test"
    plugin_dir = Path(__file__).resolve().parents[1]
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(
        f"{package_name}.plugin",
        plugin_dir / "plugin.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


plugin_module = load_plugin_module()
SecurityConfig = plugin_module.SecurityConfig
WebRetrieverConfig = plugin_module.WebRetrieverConfig
WebRetrieverPlugin = plugin_module.WebRetrieverPlugin
_generate_with_preferred_task_fallback = plugin_module._generate_with_preferred_task_fallback


class DummyLLM:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[len(self.calls) - 1]


class DummyLogger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


def make_plugin(config: WebRetrieverConfig | None = None) -> WebRetrieverPlugin:
    plugin = object.__new__(WebRetrieverPlugin)
    plugin._plugin_config_instance = config or WebRetrieverConfig()
    plugin._ctx = type("Ctx", (), {"logger": DummyLogger()})()
    return plugin


def test_llm_fallback_uses_utils_planner_replyer_order():
    llm = DummyLLM(
        [
            {"success": False, "error": "utils failed"},
            {"success": False, "error": "planner failed"},
            {"success": True, "response": "replyer ok"},
        ]
    )

    result = asyncio.run(
        _generate_with_preferred_task_fallback(
            llm,
            prompt="summarize",
            temperature=0.2,
            max_tokens=123,
            logger=DummyLogger(),
        )
    )

    assert result == {"success": True, "response": "replyer ok"}
    assert [call["model"] for call in llm.calls] == ["utils", "planner", "replyer"]
    assert all(call["max_tokens"] == 123 for call in llm.calls)


def test_restrict_tool_rejects_blacklisted_top_level_user_id():
    config = WebRetrieverConfig()
    config.permission.restrict_tool = True
    config.permission.mode = "黑名单"
    config.permission.user_qq_list = ["12345"]
    plugin = make_plugin(config)

    ok, reason = plugin._check_tool_permission({"user_id": "12345"})

    assert ok is False
    assert reason == "在黑名单"


def test_restrict_tool_rejects_unidentified_user():
    config = WebRetrieverConfig()
    config.permission.restrict_tool = True
    config.permission.mode = "黑名单"
    config.permission.user_qq_list = []
    plugin = make_plugin(config)

    ok, reason = plugin._check_tool_permission({})

    assert ok is False
    assert reason == "无法识别用户"


def test_disabling_threat_feed_clears_loaded_domains_and_stops_blocking():
    config = WebRetrieverConfig()
    config.security = SecurityConfig(threat_feed_enabled=False)
    plugin = make_plugin(config)
    plugin._threat_task = None
    plugin._threat_domains = {"bad.example"}

    asyncio.run(plugin._start_threat_feed())

    assert plugin._threat_domains == set()
    assert plugin._url_block_reason("https://bad.example/path") is None
