# MaiBot Web Retriever Agent Rules

你正在维护 `plugins/maibot-web-retriever/` 这个 MaiBot 第三方插件。

## 强制边界

- 所有改动只能发生在当前插件目录内。
- 不要修改 `src/`、`dashboard/`、`config/`、仓库根目录 `.gitignore` 或任何 MaiBot 主程序文件。
- 如果需求必须依赖主程序改动，先停止并说明原因、影响面和替代方案。
- 插件入口固定为 `plugin.py`。
- 工厂函数固定为 `create_plugin()`。
- 元信息文件固定为 `_manifest.json`。
- 运行配置文件固定为 `config.toml`。

## 开发约束

- 新功能优先使用 `@Tool`、`@Command`、`@HookHandler`、`@EventHandler`、`@API`、`@MessageGateway`。
- 不要给新代码使用 `@Action`。
- 修改前先阅读 `_manifest.json`、`plugin.py`、`config.toml`、`README.md`，沿用现有风格。
- 不要重构无关代码，不要顺手整理全仓库格式。
- 所有用户可见文本优先使用简体中文。

## 安全约束

- 不要硬编码 token、cookie、密钥、绝对路径、个人 QQ 号、群号或私有 URL。
- 默认配置应使用通用值，例如 `127.0.0.1` 或空值，不要提交作者本地私网地址。
- 不要提交日志、临时脚本、实验数据、数据库文件、虚拟环境和本地调试配置。
- 所有网络请求都要带超时和异常处理。
- 所有后台任务、连接和文件句柄都要能在 `on_unload()` 里清理。

参考文档：

- <https://docs.mai-mai.org/develop/plugin-dev/vibe-coding>
