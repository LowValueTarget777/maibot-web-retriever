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
config_version = "1.1.0"
show_progress = false
# 来源链接披露：按需发送 / 主动发送 / 从不发送
disclosure_mode = "按需发送"
# 注入给 AI 当参考的检索内容上限字数
inject_max_chars = 1500

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
crawl4ai_token = ""
fetch_timeout = 30
max_content_length = 8000
# 抓取网页全文（关闭=只用搜索摘要，更快）
fetch_full_text = true
summary_enabled = true
filter_mode = "fit"
proxy = ""
proxy_username = ""
proxy_password = ""

[cache]
enable_cache = true
cache_ttl = 3600
max_cache_entries = 100

[permission]
# 权限模式：所有人 / 白名单 / 黑名单
mode = "黑名单"
admin_qq_list = []
user_qq_list = []
# 是否对麦麦自动联网搜索也限制
restrict_tool = false
reply_on_deny = true

[security]
# 危险文件下载链接
block_download_links = true
# 额外拦截的文件后缀（内置之外，带不带点都行），如 [".torrent", ".lnk"]
extra_blocked_extensions = []

# 域名黑名单（命中含子域名即拒绝读取），支持 *.evil.com 通配
blocked_domains = []
# 域名白名单：豁免黑名单与恶意库的误判（仍受内网/下载文件拦截）
allowed_domains = []

# 公开恶意域名库（拉取失败则该层不拦、只记日志）
threat_feed_enabled = true
# 可填多个地址（多源/镜像）
threat_feed_urls = ["https://urlhaus.abuse.ch/downloads/hostfile/"]
threat_feed_refresh_hours = 24

# 提示词注入处理方式：删除注入行 / 拒绝整页 / 仅记录不删 / 关闭
injection_action = "删除注入行"
# 额外注入触发词（行内含即按上面方式处理）
injection_extra_keywords = []
```

## 安全说明

抓取在 Crawl4AI 的 Docker 容器内进行，插件**只提取网页文字、从不下载或执行任何文件到设备**（缓存为内存）。在此之上对「读取链接」做了多层拦截（按顺序，每层都可在 `[security]` 里细调）：

1. **内网/本机地址**：私有 IP、`localhost`、以及十进制/十六进制/八进制编码的 IP（如 `http://2130706433/`）一律拒绝（防 SSRF）。
2. **危险文件下载链接**：`.exe/.apk/.msi/.zip` 等直接拒绝；查 path 与 query（防 `?file=x.exe` 绕过），可用 `extra_blocked_extensions` 加自定义后缀。
3. **域名黑/白名单**：`blocked_domains` 手动拉黑（含子域名、支持 `*.` 通配）；`allowed_domains` 白名单豁免下面 2 层的误判。
4. **公开恶意域名库**：自动拉取 urlhaus 等（可填多个 `threat_feed_urls`）恶意/钓鱼域名库拦截。
5. **提示词注入处理**：网页内容若含「忽略上述指令」「你现在是…」等注入文本，按 `injection_action` 处理（删除该行 / 拒绝整页 / 仅记录 / 关闭），可用 `injection_extra_keywords` 加自定义触发词。该层对**搜索抓取的网页、读链接、搜索摘要**三条路径都生效。

> 注：第 4 层需联网拉取域名库，拉取失败会 fail-open（该层不拦、只记日志），其余层不受影响。web_search 自动抓取的搜索结果也会先过这套 URL 拦截。

## 能力与命令

- **联网搜索**（`web_search` 工具）：麦麦需要最新/外部信息时自动联网搜索、抓取、总结。
- **读取链接**（`fetch_url` 工具）：有人在群里丢网页链接、或让麦麦"看看这个链接是什么"时，麦麦自动读取该网页正文并用自己的话描述。出于安全，内网/本机地址（私有 IP、localhost）会被拦截。
- `/search <关键词>`、`/fetch <网址>`：手动命令，受 `[permission]` 控制（黑名单为空时所有人可用）。
- `/出处`（或 `/sources`、`/来源`）：调取本会话最近一次检索的来源链接。
- 「按需发送」模式下，平时不发链接；有人追问"来源/可信吗/哪来的"时，麦麦会调用 `query_sources` 工具拿到来源、只说对应那条。

## 安装与启用

1. 把本插件目录放进 MaiBot 的 `plugins/` 下。
2. 部署两个后端（见上方「快速开始」）：SearXNG（搜索）与 Crawl4AI（抓取）。
3. 启动 / 重载 MaiBot；在 WebUI「插件管理」里启用本插件。
4. 在 WebUI 插件配置页把 `[search].searxng_base_url`、`[fetch].crawl4ai_base_url` 改成你的后端地址；Crawl4AI 若设了 `CRAWL4AI_API_TOKEN`，把同样的值填进 `[fetch].crawl4ai_token`。

## 常见问题

- **麦麦不联网/不读链接**：确认插件已启用；`[permission]` 没把当前用户拉黑；SearXNG/Crawl4AI 后端可达（`/health`、`/search?format=json`）。
- **抓取一直失败**：Crawl4AI 0.9+ 默认开 SSRF 防护会拦所有 URL；容器需设 `CRAWL4AI_API_TOKEN`（否则只绑回环）。详见「快速开始」。
- **SearXNG 返回 403/空**：SearXNG 需在 `settings.yml` 开启 `formats: [html, json]` 并关闭 `limiter`，否则 JSON API 被拦。
- **联网很慢**：`[fetch].fetch_full_text=false` 可只用搜索摘要、跳过抓全文，轻问题（天气/新闻标题）更快。
- **来源链接太频繁/太少**：调 `[plugin].disclosure_mode`（按需发送 / 主动发送 / 从不发送）。
- **正常网页内容被误删**：把 `[security].injection_action` 调成「仅记录不删」观察日志，或用 `allowed_domains` 白名单豁免。
- **恶意域名库没生效**：它需联网拉取 `threat_feed_urls`，拉不到会 fail-open（不拦、只记日志）；可换可达的镜像地址。

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

