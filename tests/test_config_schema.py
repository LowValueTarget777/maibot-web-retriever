import importlib.util
import sys
from pathlib import Path


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


def test_common_fields_have_user_friendly_labels():
    module = _load_plugin_module()

    plugin_field = module.PluginSectionConfig.model_fields["enabled"]
    search_url_field = module.SearchConfig.model_fields["searxng_base_url"]
    fetch_url_field = module.FetchConfig.model_fields["crawl4ai_base_url"]

    assert plugin_field.json_schema_extra["label"] == "启用网页搜索"
    assert search_url_field.json_schema_extra["label"] == "搜索服务地址"
    assert fetch_url_field.json_schema_extra["label"] == "网页抓取服务地址"


def test_advanced_fields_are_marked_as_advanced():
    module = _load_plugin_module()

    safe_search = module.SearchConfig.model_fields["safe_search"]
    filter_mode = module.FetchConfig.model_fields["filter_mode"]
    cache_ttl = module.CacheConfig.model_fields["cache_ttl"]

    assert safe_search.json_schema_extra["advanced"] is True
    assert filter_mode.json_schema_extra["advanced"] is True
    assert cache_ttl.json_schema_extra["advanced"] is True


def test_common_fields_remain_visible_without_advanced_toggle():
    module = _load_plugin_module()

    max_results = module.SearchConfig.model_fields["max_results"]
    summary_enabled = module.FetchConfig.model_fields["summary_enabled"]
    enable_cache = module.CacheConfig.model_fields["enable_cache"]

    assert max_results.json_schema_extra.get("advanced") is not True
    assert summary_enabled.json_schema_extra.get("advanced") is not True
    assert enable_cache.json_schema_extra.get("advanced") is not True
