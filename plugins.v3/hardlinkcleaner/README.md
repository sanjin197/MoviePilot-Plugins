# HardlinkCleaner 硬链媒体库清理

一个 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件（**V2 / V3 双版本**）：同步硬链接媒体库中的所有剧集与电影，
按**剧集/电影维度**统一清理下载的视频文件——不仅删除媒体库中的链接文件和下载目录中的源文件，
还同步删除对应的**整理记录（转移历史）**、**订阅记录**，并联动下载器处理做种任务。

| 宿主版本 | 插件合同 | 目录 | 市场索引 |
|---|---|---|---|
| MoviePilot V3（3.x） | `app.sdk.*` + `app.db.oper.*` | `plugins.v3/hardlinkcleaner/` | `package.v3.json` |
| MoviePilot V2（2.x） | 旧版 `_PluginBase` 合同 | `plugins.v2/hardlinkcleaner/` | `package.v2.json` |

> V3 版按 **media_source + media_id** 归组，V2 版按 tmdbid/豆瓣 id 归组；两版功能一致，除下面「V3 差异」列出的点。

## 功能特性

- **媒体库同步清单**：扫描媒体库（硬链接）目录下所有媒体文件，结合整理历史按 剧集（按季分组）/电影 生成清理清单；无整理记录的文件按目录结构（`Season xx` / `SxxExx`）猜测归属并标记「未匹配」。
- **一键清理（两步确认）**：点击卡片上的「预览清理」生成计划 → 页面顶部红色横幅列出将删除的全部内容 → 点击「确认执行」才真正删除。
- **删除范围可选**：
  - 链接文件（媒体库/硬链接目录中的文件；删除后自底向上清理空目录，蓝光 BDMV 目录整体删除）
  - 源文件（下载目录中的文件，受目录白名单约束）
  - 整理记录（TransferHistory，即媒体整理/转移历史）
  - 订阅记录（Subscribe，按媒体来源 + 媒体 ID 匹配，并按季重叠过滤）
- **下载器联动**（三种模式）：
  - `smart`（默认）：删除源文件后统计该种子在插件清单中剩余的源文件，**未删完 → 暂停做种任务；已删完 → 删除任务（不删文件）**；
  - `follow`：发送官方 `downloadfile.deleted` 事件，由宿主自动删除下载任务；
  - `none`：不动下载器。
- **定时同步**：支持 crontab 定时刷新清单；支持「立即同步一次」。
- **安全防护**：源文件目录白名单、路径层级保护（拒绝删除两级以上父目录）、两步确认、删除结果与错误明细写入日志并通知。

## V3 差异（相对 V2）

- 数据访问走 `app.db.oper.transferhistory.TransferHistoryOper` / `app.db.oper.subscribe.SubscribeOper`，按 `media_source + media_id` 匹配；旧版 `tmdbid/doubanid` 列在 V3 已不存在。
- 删除文件统一走 `StorageChain().delete_media_file()`（`del_empty_dirs` 关闭时改用 `delete_file()`）。
- V3 宿主裁剪了下载历史（DownloadHistory）的删除接口，因此 **V3 版不删除下载历史记录**；整理记录、订阅记录、源文件与链接文件仍按需删除，下载器联动会暂停/删除任务。
- V3 宿主 `settings` 已无 `RMT_MEDIAEXT`，插件内置同等媒体后缀清单。

## 安装

### 方式一：自建插件市场（推荐）

1. Fork [MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)（或自建同结构仓库）；
2. 将对应版本的插件目录放入仓库根目录：
   - V3：`plugins.v3/hardlinkcleaner/`
   - V2：`plugins.v2/hardlinkcleaner/`
3. 将 `package.v3.json`（或 `package.v2.json`）中的 `HardlinkCleaner` 条目合并进仓库根目录同名文件；
4. 在 MoviePilot「设定 → 服务 → 插件市场」中添加你的仓库地址，然后在插件市场搜索「硬链媒体库清理」安装。

> V3 宿主只读取 `package.v3.json`；只提交 `package.v2.json` 会搜不到插件。

### 方式二：手动安装

将 `hardlinkcleaner` 目录复制到 MoviePilot 的插件目录（默认 `/config/plugins/hardlinkcleaner`），重启 MoviePilot 后在「插件 → 已安装」中启用。

## 使用

1. **配置目录**：插件设置中可勾选「自动同步系统目录配置中的媒体库」（读取 MoviePilot「设定 → 目录」中的媒体库目录），或在 `媒体库/硬链接目录` 中手动填写（每行一个）；`源文件目录白名单` 填写下载目录（留空则自动使用系统目录配置中的下载目录）。
2. **同步**：点击插件页面上的「同步媒体库」，生成清单（也可配置 crontab 定时同步）。
3. **清理**：在卡片上点击「预览清理」→ 检查页面顶部的待删除清单 → 「确认执行」。筛选按钮可按 全部/电视剧/电影/有订阅/未匹配 过滤清单。

## 注意事项

- **删除不可恢复**：文件删除、记录删除均不可撤销，请谨慎确认；建议先在测试环境验证目录配置。
- 源文件只会删除**白名单目录**内的文件；层级保护会拒绝删除挂载点/根目录级别路径。
- MoviePilot 核心不含媒体服务器刷新机制，删除文件后请依赖 Emby/Jellyfin/Plex 自身的媒体库监控，或手动扫描。
- 「未匹配」项没有整理记录支撑，仅能删除链接文件本身，不会误删其它数据。
- 硬链接场景下删除链接文件不影响源文件；反之删除源文件不影响其余硬链接。

## 目录结构

```
hardlinkcleaner/
├── plugins.v3/
│   └── hardlinkcleaner/
│       ├── __init__.py     # 插件主文件（V3 合同：app.sdk / app.db.oper）
│       └── README.md       # 插件说明（市场详情页读取）
├── plugins.v2/
│   └── hardlinkcleaner/
│       └── __init__.py     # 插件主文件（V2 合同：旧版 _PluginBase）
├── package.v3.json         # V3 插件市场索引（含 HardlinkCleaner 条目）
├── package.v2.json         # V2 插件市场索引条目
└── README.md
```
