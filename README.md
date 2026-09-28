# 关键词自动回复插件

面向 MaiBot 0.12.2，直接使用框架原生插件接口，不依赖 SDK。插件监听群消息，按规则顺序检查群范围与正则，发送预设文本或本地图片。

## 群白名单

在 `keyword_auto_reply` 配置节中维护 `default_groups`，大多数规则选择“继承默认”即可共用名单，特殊规则单独覆盖。

| 配置项 | 类型 | 含义 |
| --- | --- | --- |
| `enable` | 布尔值 | 插件回复总开关 |
| `default_groups` | 群号字符串列表 | 默认白名单，空列表表示继承它的规则不在任何群触发 |
| `rules[].group_scope` | 字符串 | `继承默认`、`指定群聊`、`所有群聊` 三选一 |
| `rules[].groups` | 字符串 | 仅指定群聊模式使用，英文逗号分隔；空字符串表示该规则不在任何群触发 |

- **继承默认**：使用 `default_groups`，忽略本规则 `groups` 的内容。
- **指定群聊**：仅使用本规则 `groups`，可以指定默认白名单外的群；不与默认名单取交集或并集。
- **所有群聊**：允许所有群，不受默认名单限制；仍不处理私聊。
- `groups` 在未选“指定群聊”时可以保留文本，但类型仍必须是字符串。
- 默认配置使用空的默认白名单，示例规则继承默认，因此首次加载不会自动回复。

新字段均须填写，包括停用规则的 `group_scope` 和 `groups`。配置加载时校验，错误会包含字段路径和规则下标（从 0 开始），并使本次插件加载失败。

群号只能是非空 ASCII 数字字符串，例如 `"123456"`。默认名单不能填写整数。指定群名单只能用英文逗号分隔，不能包含空格、换行、中文逗号、连续逗号或尾随逗号；插件不会自动清洗这些输入。完整空字符串是合法的空名单。

## 配置示例

框架首次加载时根据 schema 生成插件目录下的 `config.toml`。可以通过 WebUI 修改，或将已有配置编辑为以下结构；群号仅为示例，需要替换。

```toml
[keyword_auto_reply]
enable = true
default_groups = ["123456", "234567"]

[[keyword_auto_reply.rules]]
enabled = "启用"
group_scope = "继承默认"
groups = ""
reply_type = "文本"
pattern = '你好|hello'
reply_content = "你好。"
description = "默认群的问候回复"

[[keyword_auto_reply.rules]]
enabled = "启用"
group_scope = "指定群聊"
groups = "345678,456789"
reply_type = "图片"
pattern = '^测试图片$'
reply_content = 'D:\images\reply.png'
description = "只在指定群发送本地图片"

[[keyword_auto_reply.rules]]
enabled = "启用"
group_scope = "所有群聊"
groups = ""
reply_type = "文本"
pattern = '^帮助$'
reply_content = "可发送你好或测试图片。"
description = "所有群可用的帮助回复"
```

图片路径相对于机器人进程的工作目录解析，建议填写绝对路径。正则使用 `re.search`，默认区分大小写，不展开回复内容中的捕获组。

群范围不符合时继续检查下一条规则。成功发送后停止匹配；非法正则或图片读取失败会跳过当前规则；发送接口返回失败则结束本次匹配。插件回复不会阻止框架后续生成自己的回复。

## 消息处理开销

加载时计算所有启用规则的覆盖群集合，并编译正则。每条群消息先查询覆盖集合：没有任何候选规则的群直接返回，不遍历规则，也不清洗消息文本。存在“所有群聊”规则时不会进行整体排除，仍按各规则范围顺序匹配。

无候选群的判断成本不随规则数量增加；有候选规则但未命中的消息仍需遍历规则。正则编译结果由插件持有，避免规则数超过 Python 标准库缓存容量后逐消息重新编译。非法正则仅保存错误说明，仍在对应群消息处理时记录并跳过。

合成基准命令：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/benchmark_dispatch.py
```

基准使用不同的简单正则和固定短消息，模拟配置读取、关闭日志、不发送消息，不包含框架事件分发或消息深拷贝成本。它不能代表任意复杂正则或真实发送的耗时。

## 配置更新与旧配置

保存后必须重新加载插件或重启 MaiBot。有效群集合在注册组件时生成，配置文件保存不会直接改变运行中的规则缓存。

旧字段 `enabled_groups` 已移除，不提供兼容或自动迁移。旧配置需要单独转换：默认名单使用 `default_groups`，每条旧规则显式补齐 `group_scope = "继承默认"` 和 `groups = ""`，再按需要调整个别规则。

旧名单为空曾表示所有群；转换后空默认名单表示继承规则不触发。需要全群生效的规则必须明确选择“所有群聊”。

## 验证

测试仅依赖 Python 标准库，默认从相邻的 `../MaiBot` 读取实际框架配置类型及序列化方法；框架位于其他位置时设置 `MAIBOT_ROOT`。使用支持 `tomllib` 的 Python 运行（本项目环境为 Python 3.14）：

```powershell
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -v
```

测试隔离插件注册、日志和发送接口，不启动机器人或发送消息；配置文件写入通过内存句柄模拟，再使用 `tomllib` 解析真实序列化结果。覆盖范围选择、输入校验、缓存重建、发送顺序及框架 TOML 生成和保存。

## 实现依据

- 本插件：`plugin.py` 中的 schema、`get_plugin_components()` 和 `KeywordAutoReplyHandler.execute()`。
- MaiBot 0.12.2：`src/plugin_system/base/config_types.py`、`src/plugin_system/base/plugin_base.py`、`src/plugin_system/base/base_events_handler.py`。
- WebUI 保存与重载提示：MaiBot 的 `src/webui/plugin_routes.py`。
