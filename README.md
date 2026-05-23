# MaiBot Web Retriever 插件

基于 SearXNG + Crawl4AI 的 MaiBot 网页检索插件，为 LLM 提供网页搜索与内容抓取能力。

## 功能

- **网页搜索** (`web_search` Tool)：通过 SearXNG 搜索引擎检索网页，返回相关结果摘要
- **网页抓取** (`web_fetch` Tool)：通过 Crawl4AI 抓取并解析指定 URL 的网页内容
- **斜杠命令**：`/search <关键词>` 和 `/fetch <URL>` 快速触发

## 安装

### 前置条件

- Python >= 3.11
- uv（推荐）或 pip
- 可访问的 SearXNG 实例
- Crawl4AI 运行环境

### 安装步骤

```bash
# 1. 将插件放入 MaiBot 的 plugins/ 目录
cp -r maibot-web-retriever /path/to/maibot/plugins/

# 2. 安装依赖（uv 方式）
cd /path/to/maibot/plugins/maibot-web-retriever
uv sync

# 3. 启动 MaiBot，插件会自动加载
```

## 配置

编辑 `config.toml`：

```toml
# 是否启用插件
enabled = true

# SearXNG 实例地址
searxng_base_url = "http://localhost:8080"

# 最大搜索结果数
max_search_results = 5

# 网页抓取超时时间（秒）
fetch_timeout = 10

# 是否启用网页内容缓存
enable_cache = true

# 缓存过期时间（秒）
cache_ttl = 3600
```

## 命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `/search <关键词>` | 搜索网页 | `/search Python 教程` |
| `/fetch <URL>` | 抓取网页内容 | `/fetch https://example.com` |

## LLM 工具

| 工具名 | 说明 |
|--------|------|
| `web_search` | 搜索网页，LLM 自动调用 |
| `web_fetch` | 抓取指定网页内容，LLM 自动调用 |

## 能力声明

- `send_message`：发送消息到聊天流

## 开发

```bash
# 安装开发依赖
uv sync

# 运行测试
uv run pytest

# 代码检查
uv run ruff check .
```

## 故障排查

1. **插件未加载**：检查 `_manifest.json` 格式是否正确，生命周期方法是否完整
2. **搜索无结果**：确认 SearXNG 实例可访问，检查 `searxng_base_url` 配置
3. **抓取超时**：增大 `fetch_timeout`，或检查目标网站是否可达
4. **依赖缺失**：运行 `uv sync` 确保所有依赖已安装

## 许可

MIT
