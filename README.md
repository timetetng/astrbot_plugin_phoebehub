# 啾比表情包插件

Astrbot 插件。从 [phoebehub.top](https://phoebehub.top) 获取菲比表情包，支持随机获取、模糊搜索、上传图片并自动提交 PR。

## 安装

```bash
# 在 Astrbot 插件目录下
git clone https://github.com/timetetng/astrbot_plugin_phoebehub
cd astrbot_plugin_phoebehub
uv pip install -r requirements.txt
```

依赖：`rapidfuzz`、`zhconv`、`jieba`。缺装时自动降级为 difflib（效果较差）。

## 命令

| 命令 | 说明 | 权限 |
|------|------|------|
| `啾比` | 随机发送一张表情包 | 所有人 |
| `/搜比 <关键词> [数量]` | 模糊搜索表情包 | 所有人 |
| `/啾比帮助` | 显示本插件所有命令用法 | 所有人 |
| `/传比列表` | 查看 staging 中待提交的图片 | 所有人 |
| `/传比 [名字]` | 上传图片到 staging | 管理员 |
| `/传比改 <序号> [名字=xxx] [描述=xxx]` | 修改 staging 中图片的名字/描述 | 管理员 |
| `/删比 <序号>` | 从 staging 删除图片及其元数据 | 管理员 |
| `/pr提交` | 提交 staging 中所有图片到 GitHub PR | 管理员 |
| `/pr状态` | 查看已提交 PR 的合并/关闭状态 | 管理员 |

### 上传图片工作流

```
/传比 开心     ← 发送图片，名字为「开心」
/传比列表      ← 查看 staging 中的图片
/传比改 1 名字=暴爽菲比 描述=超级开心  ← 修改识别结果
/删比 2        ← 删除不需要的图片（可选）
/pr提交       ← 自动 fork → sync → commit → PR 到上游仓库
/pr状态       ← 检查 PR 是否已合并
```

上传图片支持回复引用图片（回复一条带图消息再发命令）。

### PR 冲突保护

`/pr提交` 会自动检查是否已有来自同一 fork 的未关闭 PR。若存在，则拒绝提交新 PR，避免并发 PR 导致合并冲突。每次提交前会自动 sync fork 的 main 分支，确保基于最新源码。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cache_ttl` | int | 300 | memes.json 缓存时间（秒），0 关闭 |
| `trigger_keywords` | list | `["啾比"]` | 随机触发关键词 |
| `proxy` | str | "" | HTTP 代理地址 |
| `cache_max_hours` | int | 24 | 本地图片缓存保留时长（小时），0 不清理 |
| `vision_provider_id` | str | "" | 视觉模型 provider，用于 /传比 自动命名+描述 |
| `github_token` | str | "" | GitHub PAT，用于 /pr提交 自动推图+PR |
| `github_target_owner` | str | `timetetng` | 测试时推送到指定 fork；留空则自动 fork 上游 |
| `upload_auth` | str | `admin` | `admin`=仅管理员可上传/改/PR；`everyone`=所有人可用 |

## License

MIT
