# MaiBot Web Retriever

基于 SearXNG 和 Crawl4AI 的网页检索插件，为 MaiBot 提供搜索、抓取、清洗和摘要能力。


## 依赖服务

插件依赖两个后端服务：

- `SearXNG`：负责网页搜索
- `Crawl4AI`：负责网页抓取和 Markdown 提取

## 快速开始

### 1. 部署 SearXNG

```bash
docker run -d --name searxng \
  -p 8800:8080 \
  searxng/searxng:latest
```

### 2. 部署 Crawl4AI

```bash
docker run -d --name crawl4ai \
  -p 11235:11235 \
  unclecode/crawl4ai:latest
```

### 3. 配置插件

默认配置使用本机地址：

```toml
[search]
searxng_base_url = "http://127.0.0.1:8800"

[fetch]
crawl4ai_base_url = "http://127.0.0.1:11235"
proxy = ""
```

如果服务运行在其他机器，请改成你自己的地址，但不要把私网地址作为默认值提交回仓库。

## 配置示例

```toml
[plugin]
enabled = true
config_version = "1.0.0"
show_progress = true
show_references = true
send_references_to_user = true

[search]
searxng_base_url = "http://127.0.0.1:8800"
search_general = true
search_news = true
search_science = false
search_it = false
max_results = 5
search_timeout = 60
safe_search = 0

[fetch]
crawl4ai_base_url = "http://127.0.0.1:11235"
fetch_timeout = 30
max_content_length = 8000
summary_enabled = true
filter_mode = "fit"
proxy = ""
proxy_username = ""
proxy_password = ""

[cache]
enable_cache = true
cache_ttl = 3600
max_cache_entries = 100
```

## AI 协作提示词

如果你使用 AI 修改这个插件，建议先给它这段上下文：

```text
你正在为 MaiBot 编写第三方插件。插件必须放在 plugins/maibot-web-retriever/ 下，不要修改 MaiBot 主程序代码，除非我明确许可。请使用 maibot-plugin-sdk，入口文件为 plugin.py，元信息文件为 _manifest.json。必须实现 on_load、on_unload、on_config_update 和 create_plugin。优先使用 @Tool、@Command、@HookHandler、@EventHandler、@API、@MessageGateway；不要给新插件使用 @Action。所有用户可见文本优先使用简体中文。请保持改动边界清晰，并给出测试方式。
```

## 测试建议

- 先确认 `_manifest.json` 是合法 JSON
- 再确认 `plugin.py` 能被 Python 正常导入
- 把插件放进 `plugins/` 后启动 MaiBot，观察加载日志
- 实际触发网页搜索、抓取和摘要链路，确认超时和错误处理符合预期

## 开发边界

本插件按 MaiBot 的 Vibe Coding 插件规范维护：

- 只允许修改 `plugins/maibot-web-retriever/` 内的文件
- 不修改 `src/`、`dashboard/`、`config/` 等主程序目录
- 不提交密钥、token、cookie、日志、数据库文件、虚拟环境和本地实验脚本
- 不在源码默认值中写作者私网地址、私有代理或个人环境信息

如果确实需要主程序改动，应先说明原因、影响面和替代方案，再单独请求许可。

参考文档：

- [MaiBot Vibe Coding 插件开发指南](https://docs.mai-mai.org/develop/plugin-dev/vibe-coding)

