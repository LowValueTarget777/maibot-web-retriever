# MaiBot Web Retriever — 网页搜索+抓取插件

> 搜索-决策-抓取：SearXNG 搜网页 → AI 选 URL → Crawl4AI 抓全文 Markdown。
> 搜索结果自动注入 AI 上下文，支持参考来源 URL 展示。

## ⚡ 快速开始（Docker 部署后端）

插件依赖两个后端服务，推荐 Docker 一键部署：

### 1. 部署 SearXNG（元搜索引擎）

```bash
mkdir searxng && cd searxng
export SEARXNG_SECRET=$(openssl rand -hex 32)

docker run -d --name searxng \
  -p 8800:8080 \
  -e "SEARXNG_SECRET=$SEARXNG_SECRET" \
  searxng/searxng:latest
```

📖 官方教程：https://docs.searxng.org/admin/installation-docker.html

### 2. 部署 Crawl4AI（网页抓取，镜像自带 Chromium）

```bash
docker run -d --name crawl4ai \
  -p 11235:11235 \
  unclecode/crawl4ai:latest
```

📖 官方教程：https://docs.crawl4ai.com/

### 3. 插件配置

```toml
[search]
searxng_base_url = "http://你的IP:8800"

[fetch]
crawl4ai_base_url = "http://你的IP:11235"
```

## 🔧 完整配置

```toml
[plugin]
enabled = true
show_progress = false          # 是否显示"正在搜索..."进度
show_references = true          # URL 是否提供给 AI 作参考
send_references_to_user = true  # 是否在回复末尾向用户展示参考链接

[search]
searxng_base_url = "http://192.168.1.9:8800"
search_general = true           # 通用网页
search_news = true              # 新闻
search_science = false          # 学术
search_it = false               # IT 技术
max_results = 5
search_timeout = 10
safe_search = 0

[fetch]
crawl4ai_base_url = "http://192.168.1.9:11235"
fetch_timeout = 30
max_content_length = 8000
filter_mode = "fit"             # fit/raw/bm25/llm
proxy = ""
proxy_username = ""
proxy_password = ""

[cache]
enable_cache = true
cache_ttl = 3600
max_cache_entries = 100
```
