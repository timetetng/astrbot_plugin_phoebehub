# 菲比表情包

Astrbot 插件。从 [phoebehub.top](https://phoebehub.top) 获取菲比表情包，支持随机获取和模糊搜索。

## 安装

```bash
# 在 Astrbot 插件目录下
git clone https://github.com/xj/astrbot_plugin_phoebehub
cd astrbot_plugin_phoebehub
uv pip install -r requirements.txt
```

依赖：`rapidfuzz`、`zhconv`、`jieba`。缺装时自动降级为 difflib（效果较差）。

## 用法

### 随机表情包

配置 `trigger_keywords`（默认 `啾比`、`jiubi`、`jbi`），精确匹配关键词时随机发送一张表情包。

### 搜索表情包

```
/搜比 <关键词> [数量]
```

示例：

| 命令 | 效果 |
|------|------|
| `/搜比 好馋` | 精确匹配直接返回，否则模糊匹配 top 3 |
| `/搜比 开心 5` | 返回最多 5 个结果 |
| `/搜比 吵` | 模糊搜索相似标题 |

搜索特性：
- 简繁自动转换（`新年快乐` → `新年快樂`）
- 字符模糊匹配（`哭` → `你已急哭`）
- 同义词辅助（`饿` → `吃吃菲比`、`伤心` → `你已急哭`）
- 结果带图片预览

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cache_ttl` | int | 300 | memes.json 缓存时间（秒），0 关闭 |
| `trigger_keywords` | list | `["啾比","jiubi","jbi"]` | 随机触发关键词 |
| `proxy` | str | "" | HTTP 代理地址 |
| `cache_max_hours` | int | 24 | 本地图片缓存保留时长（小时），0 不清理 |

## License

MIT
