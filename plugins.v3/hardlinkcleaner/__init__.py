import hashlib
import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.application.directory import DirectoryHelper
from app.chain.storage import StorageChain
from app.db.oper.subscribe import SubscribeOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.sdk.config import settings
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas import FileItem
from app.schemas.types import EventType, MessageType

# 媒体文件后缀（V3 宿主的 settings 不再提供媒体后缀清单，插件内置同等清单）
MEDIA_EXTS = [
    ".mp4", ".mkv", ".ts", ".iso", ".rmvb", ".avi", ".mov", ".mpeg", ".mpg",
    ".wmv", ".3gp", ".asf", ".m4v", ".flv", ".m2ts", ".strm", ".tp", ".f4v",
]


def _to_bool(v: Any) -> bool:
    return str(v).lower() in ("1", "true", "yes", "y", "on")


def _format_size(size: float) -> str:
    size = float(size or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}PB"


def _clean_title_year(raw: str) -> Tuple[str, str]:
    """
    从目录名中拆出标题与年份，如 `The.Movie.(2020)` / `The.Movie.2020.1080p`。
    """
    s = str(raw).strip()
    year = ""
    m = re.search(r"[\(\[\s._-]((?:19|20)\d{2})[\)\]\s._-]*$", s)
    if m:
        year = m.group(1)
        s = s[: m.start()].strip(" ._-()[]")
    s = s.replace(".", " ").replace("_", " ").strip()
    return (s or str(raw).strip()), year


def _col(md: int, *children) -> dict:
    return {"component": "VCol", "props": {"cols": 12, "md": md}, "content": list(children)}


def _switch(model: str, title: str) -> dict:
    return {"component": "VSwitch", "props": {"model": model, "label": title}}


def _textfield(model: str, title: str, placeholder: str = "") -> dict:
    props = {"model": model, "label": title}
    if placeholder:
        props["placeholder"] = placeholder
    return {"component": "VTextField", "props": props}


def _textarea(model: str, title: str, placeholder: str = "", rows: int = 3) -> dict:
    props = {"model": model, "label": title, "rows": rows, "auto-grow": True}
    if placeholder:
        props["placeholder"] = placeholder
    return {"component": "VTextarea", "props": props}


def _select(model: str, title: str, items: List[dict]) -> dict:
    return {"component": "VSelect", "props": {"model": model, "label": title, "items": items}}


class HardlinkCleaner(_PluginBase):
    """
    硬链媒体库清理（V3）：同步硬链接媒体库中的所有剧集与电影，按剧集/电影维度一键清理
    链接文件、源文件、整理记录、订阅记录，并联动下载器处理做种任务。

    基于 V3 稳定契约：app.sdk.plugin / app.sdk.config / app.sdk.events / app.sdk.logging，
    数据访问走 app.db.oper.*（身份合同：media_source + media_id）。
    """

    plugin_name = "硬链媒体库清理"
    plugin_desc = "同步硬链接媒体库的剧集与电影，按剧集维度一键清理：链接文件、源文件、整理记录、订阅记录，支持下载器联动与两步确认。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/clean.png"
    plugin_version = "3.0.0"
    plugin_author = "sanjin197"
    author_url = "https://github.com/sanjin197"
    plugin_config_prefix = "hardlinkcleaner_"
    plugin_order = 20
    auth_level = 2

    _enabled = False
    _onlyonce = False
    _notify = True
    _use_system_dirs = True
    _library_paths = ""
    _download_paths = ""
    _cron = ""
    _del_links = True
    _del_source = True
    _del_history = True
    _del_subscribe = True
    _del_empty_dirs = True
    _torrent_mode = "smart"
    # 类级锁与调度器句柄：跨实例共享，且实例未初始化时可安全访问
    _lock = threading.Lock()
    _scheduler = None
    # Oper 实例在 init_plugin 中创建（不定义 __init__，交给 _PluginBase 初始化 chain/plugindata 等）
    _transferhis: Optional[TransferHistoryOper] = None
    _subscribehis: Optional[SubscribeOper] = None

    def init_plugin(self, config: dict = None):
        # 数据操作实例（_PluginBase.__init__ 已创建 self.chain/self.plugindata/self.eventmanager）
        self._transferhis = TransferHistoryOper()
        self._subscribehis = SubscribeOper()
        config = config or {}
        self._enabled = _to_bool(config.get("enabled"))
        self._onlyonce = _to_bool(config.get("onlyonce"))
        self._notify = _to_bool(config.get("notify", True))
        self._use_system_dirs = _to_bool(config.get("use_system_dirs", True))
        self._library_paths = str(config.get("library_paths") or "")
        self._download_paths = str(config.get("download_paths") or "")
        self._cron = str(config.get("cron") or "")
        self._del_links = _to_bool(config.get("del_links", True))
        self._del_source = _to_bool(config.get("del_source", True))
        self._del_history = _to_bool(config.get("del_history", True))
        self._del_subscribe = _to_bool(config.get("del_subscribe", True))
        self._del_empty_dirs = _to_bool(config.get("del_empty_dirs", True))
        self._torrent_mode = str(config.get("torrent_mode") or "smart")
        if self._torrent_mode not in ("follow", "smart", "none"):
            self._torrent_mode = "smart"

        # 立即运行一次
        if self._enabled and self._onlyonce:
            self._onlyonce = False
            self.__update_config()
            try:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.__sync_library,
                    trigger="date",
                    run_date=time.time() + 3,
                    name="硬链媒体库立即同步",
                )
                self._scheduler.start()
                logger.info("硬链媒体库立即同步任务已启动")
            except Exception as e:
                logger.error(f"启动立即同步任务失败: {e}")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "use_system_dirs": self._use_system_dirs,
            "library_paths": self._library_paths,
            "download_paths": self._download_paths,
            "cron": self._cron,
            "del_links": self._del_links,
            "del_source": self._del_source,
            "del_history": self._del_history,
            "del_subscribe": self._del_subscribe,
            "del_empty_dirs": self._del_empty_dirs,
            "torrent_mode": self._torrent_mode,
        })

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass

    @staticmethod
    def get_command() -> List[dict]:
        pass

    def get_service(self) -> List[dict]:
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
            return [{
                "id": "HardlinkCleanerSync",
                "name": "硬链媒体库同步",
                "trigger": trigger,
                "func": self.__sync_library,
                "kwargs": {},
            }]
        except Exception as e:
            logger.error(f"硬链媒体库同步定时任务配置错误: {e}")
            return []

    def get_api(self) -> List[dict]:
        return [
            {"path": "/sync", "endpoint": self.__api_sync, "methods": ["GET"], "summary": "同步媒体库目录"},
            {"path": "/preview", "endpoint": self.__api_preview, "methods": ["GET"], "summary": "预览清理计划"},
            {"path": "/clean", "endpoint": self.__api_clean, "methods": ["GET"], "summary": "执行清理"},
            {"path": "/cancel", "endpoint": self.__api_cancel, "methods": ["GET"], "summary": "取消待确认清理"},
            {"path": "/filter", "endpoint": self.__api_filter, "methods": ["GET"], "summary": "切换清单筛选"},
            {"path": "/catalog", "endpoint": self.__api_catalog, "methods": ["GET"], "summary": "获取媒体清单"},
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        form = [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "class": "mb-3"},
                "text": "同步硬链接媒体库中的所有剧集和电影，按剧集/电影维度统一清理：删除媒体库中的链接文件、下载目录中的源文件，"
                        "并可选删除对应的整理记录、订阅记录，联动下载器暂停/删除做种任务。所有清理均需两步确认。",
            },
            {"component": "VRow", "content": [
                _col(6, _switch("enabled", "启用插件")),
                _col(6, _switch("use_system_dirs", "自动同步系统目录配置中的媒体库")),
                _col(6, _textfield("cron", "定时同步周期", "0 5 * * *")),
                _col(6, _switch("onlyonce", "立即同步一次")),
                _col(12, _textarea("library_paths", "媒体库/硬链接目录（每行一个）",
                                   "/media/library\n/volume1/library", 3)),
                _col(12, _textarea("download_paths", "源文件目录白名单（每行一个，留空使用系统下载目录）",
                                   "/media/downloads\n/volume1/downloads", 3)),
                _col(4, _select("torrent_mode", "下载器联动", [
                    {"title": "智能联动（未删完暂停、删完删除）", "value": "smart"},
                    {"title": "跟随官方（发送事件由宿主删种）", "value": "follow"},
                    {"title": "不联动", "value": "none"},
                ])),
                _col(4, _switch("del_links", "删除链接文件")),
                _col(4, _switch("del_source", "删除源文件")),
                _col(4, _switch("del_history", "删除整理记录")),
                _col(4, _switch("del_subscribe", "删除订阅记录")),
                _col(4, _switch("del_empty_dirs", "自动清理空目录")),
                _col(6, _switch("notify", "发送清理通知")),
            ]},
        ]
        defaults = {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "use_system_dirs": True,
            "library_paths": "",
            "download_paths": "",
            "cron": "",
            "del_links": True,
            "del_source": True,
            "del_history": True,
            "del_subscribe": True,
            "del_empty_dirs": True,
            "torrent_mode": "smart",
        }
        return form, defaults

    def get_page(self) -> Optional[List[dict]]:
        catalog = self.get_data("catalog") or {}
        ui = self.get_data("ui") or {"filter": "all"}
        pending = self.get_data("pending")
        last_result = self.get_data("last_result")
        current_filter = str(ui.get("filter") or "all")
        api_key = settings.API_TOKEN
        components: List[dict] = []

        stats = catalog.get("stats") or {}
        last_sync = str(catalog.get("last_sync") or "")
        if last_sync:
            last_sync = f"（{last_sync}）"

        # 顶部操作栏
        header_children = [
            {"component": "span", "props": {"class": "text-subtitle-1"},
             "text": f"共 {stats.get('series', 0)} 部剧集 / {stats.get('movies', 0)} 部电影，"
                     f"{stats.get('files', 0)} 个文件，{_format_size(stats.get('size', 0))} {last_sync}"},
            {"component": "VBtn", "props": {"color": "primary", "size": "small"}, "text": "同步媒体库",
             "events": {"click": {"api": "plugin/HardlinkCleaner/sync", "method": "get",
                                  "params": {"apikey": api_key}}}},
        ]
        filters = [("all", "全部"), ("tv", "电视剧"), ("movie", "电影"), ("sub", "有订阅"), ("unknown", "未匹配")]
        for value, label in filters:
            header_children.append({
                "component": "VBtn",
                "props": {"size": "small", "variant": "tonal" if value == current_filter else "text",
                          "color": "primary" if value == current_filter else ""},
                "text": label,
                "events": {"click": {"api": "plugin/HardlinkCleaner/filter", "method": "get",
                                     "params": {"apikey": api_key, "f": value}}},
            })
        components.append({
            "component": "VCard", "props": {"variant": "flat", "class": "mb-3"},
            "content": [{"component": "VCardText", "content": [
                {"component": "div", "props": {"class": "d-flex align-center flex-wrap ga-2"},
                 "content": header_children},
                {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"},
                 "text": "清理流程：点击卡片上的「预览清理」生成计划 → 页面顶部确认要删除的内容 → 点击「确认执行」。"},
            ]},
        ]})

        # 上次清理结果
        if last_result:
            r = last_result
            result_text = (
                f"{r.get('ts', '')} 清理「{r.get('title', '')}」：链接文件 {r.get('link_deleted', 0)} 个、"
                f"源文件 {r.get('src_deleted', 0)} 个、整理记录 {r.get('history_deleted', 0)} 条、"
                f"订阅 {r.get('subscribe_deleted', 0)} 条；"
                f"下载器暂停 {len(r.get('torrents_stopped') or [])} 个、删除 {len(r.get('torrents_removed') or [])} 个任务"
            )
            if r.get("errors"):
                result_text += f"；失败 {len(r.get('errors'))} 项（详见日志）"
            components.append({
                "component": "VAlert", "props": {"type": "success", "variant": "tonal", "class": "mb-3"},
                "text": result_text,
            })

        # 待确认清理计划
        if pending:
            options = pending.get("options") or {}
            actions = pending.get("actions") or {}
            opt_names = [name for name, flag in [
                ("链接文件", options.get("del_links")),
                ("源文件", options.get("del_source")),
                ("整理记录", options.get("del_history")),
                ("订阅记录", options.get("del_subscribe")),
            ] if flag]
            pending_children = [
                {"component": "div", "props": {"class": "text-subtitle-2"},
                 "text": f"确认清理「{pending.get('title', '')}」"},
                {"component": "div", "props": {"class": "text-caption mt-1"},
                 "text": f"将删除：链接文件 {len(actions.get('link_files') or [])} 个、"
                         f"源文件 {len(actions.get('src_files') or [])} 个、"
                         f"整理记录 {len(actions.get('history_ids') or [])} 条、"
                         f"订阅 {len(actions.get('subscribe_ids') or [])} 条"
                         f"（范围：{'、'.join(opt_names) or '无'}；下载器联动：{options.get('torrent_mode', 'smart')}）"},
            ]
            for line in (actions.get("link_files") or [])[:6]:
                pending_children.append({"component": "div",
                                         "props": {"class": "text-caption text-error"},
                                         "text": f"[链接] {line}"})
            if len(actions.get("link_files") or []) > 6:
                pending_children.append({"component": "div", "props": {"class": "text-caption"},
                                         "text": f"... 等共 {len(actions.get('link_files'))} 个链接文件"})
            for line in (actions.get("src_files") or [])[:6]:
                pending_children.append({"component": "div",
                                         "props": {"class": "text-caption text-error"},
                                         "text": f"[源文件] {line}"})
            if len(actions.get("src_files") or []) > 6:
                pending_children.append({"component": "div", "props": {"class": "text-caption"},
                                         "text": f"... 等共 {len(actions.get('src_files'))} 个源文件"})
            pending_children.append({
                "component": "div", "props": {"class": "d-flex ga-2 mt-2"}, "content": [
                    {"component": "VBtn", "props": {"color": "error", "size": "small"}, "text": "确认执行",
                     "events": {"click": {"api": "plugin/HardlinkCleaner/clean", "method": "get",
                                          "params": {"apikey": api_key, "key": pending.get("key", ""),
                                                     "confirm": "1"}}}},
                    {"component": "VBtn", "props": {"size": "small", "variant": "text"}, "text": "取消",
                     "events": {"click": {"api": "plugin/HardlinkCleaner/cancel", "method": "get",
                                          "params": {"apikey": api_key, "key": pending.get("key", "")}}}},
                ]})
            components.append({
                "component": "VAlert", "props": {"type": "error", "variant": "tonal", "class": "mb-3"},
                "content": pending_children,
            })

        # 清单卡片
        items = catalog.get("items") or []
        if current_filter == "tv":
            items = [i for i in items if i.get("type") == "电视剧"]
        elif current_filter == "movie":
            items = [i for i in items if i.get("type") == "电影"]
        elif current_filter == "sub":
            items = [i for i in items if i.get("subscribe_ids")]
        elif current_filter == "unknown":
            items = [i for i in items if i.get("unknown")]

        if not items:
            components.append({
                "component": "VCard", "props": {"variant": "flat"},
                "content": [{"component": "VCardText", "content": [
                    {"component": "div", "props": {"class": "text-center text-medium-emphasis pa-4"},
                     "text": "暂无数据，请先点击「同步媒体库」。"},
                ]},
            ]})
        else:
            total = len(items)
            items = sorted(
                items,
                key=lambda x: (0 if x.get("subscribe_ids") else 1, 0 if not x.get("unknown") else 1,
                               str(x.get("title") or "")),
            )
            shown = items[:200]
            cards = [self.__render_item_card(item, api_key) for item in shown]
            components.append({
                "component": "VRow", "content": cards,
            })
            if total > 200:
                components.append({
                    "component": "div", "props": {"class": "text-caption text-medium-emphasis mt-2"},
                    "text": f"仅显示前 200 项（共 {total} 项），可使用筛选缩小范围。",
                })
        return components

    def __render_item_card(self, item: dict, api_key: str) -> dict:
        seasons = item.get("seasons") or []
        season_text = ",".join(seasons) if seasons else ""
        meta_parts = []
        if season_text:
            meta_parts.append(season_text)
        meta_parts.append(f"{len(item.get('link_files') or [])} 个文件")
        meta_parts.append(_format_size(item.get("total_size") or 0))
        if item.get("src_files"):
            meta_parts.append(f"源 {len(item['src_files'])} 个")
        chips = []
        if item.get("unknown"):
            chips.append({"component": "VChip", "props": {"size": "x-small", "color": "orange"},
                          "text": "未匹配记录"})
        if item.get("subscribe_ids"):
            chips.append({"component": "VChip", "props": {"size": "x-small", "color": "red"},
                          "text": f"订阅 {len(item['subscribe_ids'])}"})
        title_text = str(item.get("title") or "未知")
        if item.get("year"):
            title_text += f" ({item['year']})"
        return {
            "component": "VCol", "props": {"cols": 12, "md": 6, "lg": 4}, "content": [
                {"component": "VCard", "props": {"variant": "outlined", "class": "h-100"}, "content": [
                    {"component": "VCardText", "content": [
                        {"component": "div", "props": {"class": "d-flex align-center ga-2"}, "content": [
                            {"component": "span", "props": {"class": "text-subtitle-2 flex-grow-1",
                             "style": "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"},
                             "text": title_text},
                        ] + chips},
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"},
                         "text": " · ".join(meta_parts)},
                        {"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                         "text": item.get("type") or ""},
                        {"component": "div", "props": {"class": "d-flex ga-2 mt-2"}, "content": [
                            {"component": "VBtn", "props": {"color": "error", "size": "x-small",
                             "variant": "tonal"}, "text": "预览清理",
                             "events": {"click": {"api": "plugin/HardlinkCleaner/preview", "method": "get",
                                                  "params": {"apikey": api_key, "key": item.get("key", "")}}}},
                        ]},
                    ]},
                ]},
            ]}

    # ============================= 对外 API =============================

    def __check_auth(self, apikey: str) -> Optional[dict]:
        if not settings.API_TOKEN or apikey != settings.API_TOKEN:
            return {"success": False, "message": "认证失败"}
        return None

    def __api_sync(self, apikey: str = "") -> dict:
        auth_fail = self.__check_auth(apikey)
        if auth_fail:
            return auth_fail
        result = self.__sync_library()
        return {"success": True, "message": result.get("message", "同步完成"), "data": {
            "stats": result.get("stats"),
        }}

    def __api_preview(self, apikey: str = "", key: str = "",
                      del_links: str = "", del_source: str = "", del_history: str = "",
                      del_subscribe: str = "", del_empty_dirs: str = "", torrent_mode: str = "") -> dict:
        auth_fail = self.__check_auth(apikey)
        if auth_fail:
            return auth_fail
        options = self.__resolve_options(del_links, del_source, del_history, del_subscribe,
                                         del_empty_dirs, torrent_mode)
        return self.__preview(key, options)

    def __api_clean(self, apikey: str = "", key: str = "", confirm: str = "") -> dict:
        auth_fail = self.__check_auth(apikey)
        if auth_fail:
            return auth_fail
        if not _to_bool(confirm):
            return {"success": False, "message": "请先预览并确认后再执行清理"}
        return self.__clean(key)

    def __api_cancel(self, apikey: str = "", key: str = "") -> dict:
        auth_fail = self.__check_auth(apikey)
        if auth_fail:
            return auth_fail
        self.del_data("pending")
        return {"success": True, "message": "已取消待确认清理"}

    def __api_filter(self, apikey: str = "", f: str = "all") -> dict:
        auth_fail = self.__check_auth(apikey)
        if auth_fail:
            return auth_fail
        if f not in ("all", "tv", "movie", "sub", "unknown"):
            f = "all"
        self.save_data("ui", {"filter": f})
        return {"success": True}

    def __api_catalog(self, apikey: str = "") -> dict:
        auth_fail = self.__check_auth(apikey)
        if auth_fail:
            return auth_fail
        return {"success": True, "data": self.get_data("catalog") or {}}

    # ============================= 核心逻辑 =============================

    def __resolve_options(self, del_links: str, del_source: str, del_history: str,
                          del_subscribe: str, del_empty_dirs: str, torrent_mode: str) -> dict:
        """清理选项：API 参数 > 插件配置默认值。"""

        def pick(param: str, default: bool) -> bool:
            return _to_bool(param) if param != "" else default

        mode = torrent_mode if torrent_mode in ("follow", "smart", "none") else self._torrent_mode
        return {
            "del_links": pick(del_links, self._del_links),
            "del_source": pick(del_source, self._del_source),
            "del_history": pick(del_history, self._del_history),
            "del_subscribe": pick(del_subscribe, self._del_subscribe),
            "del_empty_dirs": pick(del_empty_dirs, self._del_empty_dirs),
            "torrent_mode": mode,
        }

    def __get_library_dirs(self) -> List[Path]:
        return self.__collect_dirs(self._use_system_dirs, self._library_paths, "library_path", "媒体库")

    def __get_download_dirs(self) -> List[Path]:
        return self.__collect_dirs(self._use_system_dirs, self._download_paths, "download_path", "下载")

    def __collect_dirs(self, use_system: bool, extra: str, attr: str, tag: str) -> List[Path]:
        dirs: List[Path] = []
        if use_system:
            try:
                for conf in DirectoryHelper().get_dirs() or []:
                    p = getattr(conf, attr, None)
                    if p:
                        dirs.append(Path(str(p)))
            except Exception as e:
                logger.warning(f"{self.plugin_name} 读取系统{tag}目录配置失败: {e}")
        for line in (extra or "").splitlines():
            line = line.strip()
            if line:
                dirs.append(Path(line))
        result: List[Path] = []
        seen = set()
        for d in dirs:
            posix = d.as_posix()
            if not posix or posix in seen:
                continue
            seen.add(posix)
            try:
                if len(d.parts) <= 2:
                    logger.warning(f"{self.plugin_name} 目录层级过浅，跳过: {posix}")
                    continue
                if not d.exists() or not d.is_dir():
                    logger.warning(f"{self.plugin_name} {tag}目录不存在，跳过: {posix}")
                    continue
            except Exception as e:
                logger.warning(f"{self.plugin_name} {tag}目录不可访问，跳过 {posix}: {e}")
                continue
            result.append(d)
        return result

    @staticmethod
    def __under_any(path: Path, roots: List[Path]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    @staticmethod
    def __list_media_files(directory: Path) -> List[Path]:
        """递归扫描目录下的媒体文件（v3 无 SystemUtils.list_files 依赖，本地实现）。"""
        result: List[Path] = []
        exts = {e.lower() for e in MEDIA_EXTS}
        try:
            for root, _dirs, files in os.walk(directory):
                for name in files:
                    if os.path.splitext(name)[1].lower() in exts:
                        result.append(Path(root) / name)
        except Exception as e:
            logger.warning(f"扫描目录失败 {directory}: {e}")
            return []
        result.sort(key=lambda p: p.as_posix())
        return result

    def __sync_library(self) -> dict:
        """
        扫描媒体库目录，结合整理历史生成按剧集/电影分组的清理清单。
        """
        with self._lock:
            try:
                libs = self.__get_library_dirs()
                dl_roots = self.__get_download_dirs()
                if not libs:
                    message = "未配置媒体库目录，请检查插件设置或系统目录配置"
                    logger.warning(f"{self.plugin_name} {message}")
                    return {"success": False, "message": message}

                # 待确认计划已过期
                self.del_data("pending")
                self.del_data("last_result")

                # 全量订阅
                try:
                    subscribes = self._subscribehis.list() or []
                except Exception as e:
                    logger.warning(f"{self.plugin_name} 读取订阅列表失败: {e}")
                    subscribes = []

                items: Dict[str, dict] = {}
                file_count = 0
                for lib in libs:
                    files = self.__list_media_files(lib)
                    for f in files:
                        file_count += 1
                        try:
                            self.__collect_file_item(f, lib, items)
                        except Exception as e:
                            logger.warning(f"{self.plugin_name} 处理文件失败 {f}: {e}")

                # 匹配订阅
                for item in items.values():
                    self.__attach_subscribes(item, subscribes)

                # 集合转列表（save_data 走 JSON 序列化，set 无法序列化）
                for item in items.values():
                    item["seasons"] = sorted(item.get("seasons") or [])
                    item["download_hashes"] = sorted(item.get("download_hashes") or [])

                item_list = list(items.values())
                stats = {
                    "series": sum(1 for i in item_list if i.get("type") == "电视剧"),
                    "movies": sum(1 for i in item_list if i.get("type") == "电影"),
                    "files": file_count,
                    "size": sum(i.get("total_size") or 0 for i in item_list),
                }
                catalog = {
                    "last_sync": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    "library_dirs": [d.as_posix() for d in libs],
                    "download_dirs": [d.as_posix() for d in dl_roots],
                    "stats": stats,
                    "items": item_list,
                }
                self.save_data("catalog", catalog)
                message = (f"同步完成：{stats['series']} 部剧集 / {stats['movies']} 部电影，"
                           f"{stats['files']} 个文件，{_format_size(stats['size'])}")
                logger.info(f"{self.plugin_name} {message}")
                if self._notify:
                    self.post_message(mtype=MessageType.Plugin,
                                      title=f"{self.plugin_name} 同步完成", text=message)
                return {"success": True, "message": message, "stats": stats}
            except Exception as e:
                logger.error(f"{self.plugin_name} 同步媒体库出错: {e}\n{traceback.format_exc()}")
                return {"success": False, "message": f"同步出错：{e}"}

    def __collect_file_item(self, f: Path, lib: Path, items: Dict[str, dict]):
        posix = f.as_posix()
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        # 按媒体库文件路径（dest）反查整理记录（v3 Oper 合同：get_by_dest）
        rec = None
        try:
            rec = self._transferhis.get_by_dest(dest=posix)
        except Exception as e:
            logger.warning(f"{self.plugin_name} 查询整理记录失败 {posix}: {e}")
        if rec:
            mtype = str(getattr(rec, "type", "") or "电视剧")
            if mtype not in ("电视剧", "电影"):
                mtype = "电视剧"
            media_source = str(getattr(rec, "media_source", "") or "")
            media_id = str(getattr(rec, "media_id", "") or "")
            # v3 身份合同：media_source + media_id；缺身份时退回标题+年份分组
            if media_source and media_id:
                ident = f"media:{media_source}:{media_id}"
            else:
                ident = f"title:{getattr(rec, 'title', '')}:{getattr(rec, 'year', '')}"
            key = f"{'tv' if mtype == '电视剧' else 'movie'}_{ident}"
            item = items.get(key) or {
                "key": key,
                "type": mtype,
                "title": str(getattr(rec, "title", "") or "未知"),
                "year": str(getattr(rec, "year", "") or ""),
                "media_source": media_source,
                "media_id": media_id,
                "seasons": set(),
                "link_files": [],
                "src_files": [],
                "src_hashes": {},
                "history_ids": [],
                "subscribe_ids": [],
                "subscribe_names": [],
                "download_hashes": set(),
                "unknown": False,
                "total_size": 0,
            }
            seasons = str(getattr(rec, "seasons", "") or "")
            if seasons:
                for s in seasons.split("-"):
                    s = s.strip().upper()
                    if re.match(r"^S\d{1,2}$", s):
                        item["seasons"].add(f"S{int(s[1:]):02d}")
            history_id = getattr(rec, "id", None)
            if history_id and history_id not in item["history_ids"]:
                item["history_ids"].append(history_id)
            download_hash = str(getattr(rec, "download_hash", "") or "")
            src = str(getattr(rec, "src", "") or "")
            if src:
                src_posix = Path(src).as_posix()
                try:
                    src_size = Path(src).stat().st_size
                except OSError:
                    src_size = 0
                if src_posix not in [x["path"] for x in item["src_files"]]:
                    item["src_files"].append({"path": src_posix, "size": src_size})
                if download_hash:
                    item["src_hashes"][src_posix] = download_hash
            if download_hash:
                item["download_hashes"].add(download_hash)
            downloader = str(getattr(rec, "downloader", "") or "")
            if downloader:
                item.setdefault("downloaders", {})[download_hash or "*"] = downloader
        else:
            mtype, title, year, seasons, unknown = self.__guess_from_path(f, lib)
            raw_key = f"{mtype}|{title.lower()}|{year}"
            key = f"{'tv' if mtype == '电视剧' else 'movie'}_dir_{hashlib.md5(raw_key.encode('utf-8')).hexdigest()[:12]}"
            item = items.get(key) or {
                "key": key,
                "type": mtype,
                "title": title,
                "year": year,
                "media_source": "",
                "media_id": "",
                "seasons": set(),
                "link_files": [],
                "src_files": [],
                "src_hashes": {},
                "history_ids": [],
                "subscribe_ids": [],
                "subscribe_names": [],
                "download_hashes": set(),
                "unknown": True,
                "total_size": 0,
            }
            for s in seasons:
                item["seasons"].add(s)

        item["link_files"].append({"path": posix, "size": size})
        item["total_size"] += size
        items[key] = item

    @staticmethod
    def __guess_from_path(f: Path, lib: Path) -> Tuple[str, str, str, List[str], bool]:
        """
        无整理记录时按目录结构猜测剧集/电影信息。
        返回 (类型, 标题, 年份, 季列表, 是否未匹配)。
        """
        try:
            rel_parts = f.relative_to(lib).parts[:-1]
        except ValueError:
            rel_parts = ()
        # 季目录识别
        for i, part in enumerate(rel_parts):
            name = part.strip()
            m = re.match(r"(?i)^season[\s._-]?(\d{1,2})$", name) or re.match(r"(?i)^s(\d{1,2})$", name)
            if m:
                season = f"S{int(m.group(1)):02d}"
                if i >= 1:
                    raw_title = rel_parts[i - 1]
                elif len(rel_parts) == 1:
                    raw_title = rel_parts[0]
                else:
                    raw_title = lib.name
                title, year = _clean_title_year(raw_title)
                return "电视剧", title, year, [season], True
        # 文件名含 SxxExx 视为剧集
        m = re.search(r"(?i)[sS](\d{1,2})[eE](\d{1,3})", f.stem)
        if m:
            raw_title = rel_parts[-1] if rel_parts else lib.name
            title, year = _clean_title_year(raw_title)
            return "电视剧", title, year, [f"S{int(m.group(1)):02d}"], True
        # 电影
        raw_title = rel_parts[-1] if rel_parts else f.stem
        title, year = _clean_title_year(raw_title)
        return "电影", title, year, [], True

    def __attach_subscribes(self, item: dict, subscribes: List[Any]):
        for sub in subscribes:
            try:
                if self.__match_subscribe(item, sub):
                    sid = getattr(sub, "id", None)
                    name = str(getattr(sub, "name", "") or "")
                    if sid and sid not in item["subscribe_ids"]:
                        item["subscribe_ids"].append(sid)
                        if name and name not in item["subscribe_names"]:
                            item["subscribe_names"].append(name)
            except Exception as e:
                logger.warning(f"{self.plugin_name} 匹配订阅失败: {e}")

    @staticmethod
    def __season_overlap(item: dict, sub: Any) -> bool:
        seasons = item.get("seasons") or []
        try:
            sub_season = getattr(sub, "season", None)
        except Exception:
            sub_season = None
        if not seasons or sub_season in (None, "", 0):
            return True
        try:
            return f"S{int(sub_season):02d}" in seasons
        except (TypeError, ValueError):
            return True

    def __match_subscribe(self, item: dict, sub: Any) -> bool:
        # v3 订阅身份合同：media_source + media_id（Subscribe 模型无 tmdbid/doubanid 列）
        if str(item.get("type") or "") != str(getattr(sub, "type", "") or ""):
            return False
        sub_source = str(getattr(sub, "media_source", "") or "")
        sub_mid = str(getattr(sub, "media_id", "") or "")
        item_source = str(item.get("media_source") or "")
        item_mid = str(item.get("media_id") or "")
        if item_source and item_mid and sub_source and sub_mid:
            if item_source == sub_source and item_mid == sub_mid:
                return self.__season_overlap(item, sub)
            return False
        # 身份不全（如未匹配项）退回名称+年份匹配
        name = str(getattr(sub, "name", "") or "")
        year = str(getattr(sub, "year", "") or "")
        if (item.get("title") and name
                and item["title"].strip().upper() == name.strip().upper()
                and str(item.get("year") or "") == year):
            return self.__season_overlap(item, sub)
        return False

    def __find_item(self, key: str) -> Tuple[Optional[dict], Optional[dict]]:
        catalog = self.get_data("catalog") or {}
        for item in catalog.get("items") or []:
            if item.get("key") == key:
                return catalog, item
        return None, None

    def __preview(self, key: str, options: dict) -> dict:
        _, item = self.__find_item(key)
        if not item:
            return {"success": False, "message": "清单中没有该项，请先同步媒体库"}
        dl_roots = self.__get_download_dirs()
        actions = {
            "link_files": [],
            "src_files": [],
            "history_ids": [],
            "subscribe_ids": [],
            "download_hashes": list(item.get("download_hashes") or []),
        }
        if options.get("del_links"):
            for f in item.get("link_files") or []:
                if Path(f["path"]).exists():
                    actions["link_files"].append(f["path"])
        if options.get("del_source"):
            for f in item.get("src_files") or []:
                p = Path(f["path"])
                if not p.exists():
                    continue
                if dl_roots and not self.__under_any(p, dl_roots):
                    logger.info(f"{self.plugin_name} 源文件不在白名单内，跳过: {p}")
                    continue
                actions["src_files"].append(f["path"])
        if options.get("del_history"):
            actions["history_ids"] = list(item.get("history_ids") or [])
        if options.get("del_subscribe"):
            actions["subscribe_ids"] = list(item.get("subscribe_ids") or [])
        title = item.get("title") or "未知"
        if item.get("year"):
            title += f" ({item['year']})"
        pending = {
            "key": key,
            "title": title,
            "options": options,
            "actions": actions,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        self.save_data("pending", pending)
        logger.info(f"{self.plugin_name} 生成清理计划「{title}」: 链接 {len(actions['link_files'])} / "
                    f"源 {len(actions['src_files'])} / 记录 {len(actions['history_ids'])} / "
                    f"订阅 {len(actions['subscribe_ids'])}")
        return {"success": True, "message": "清理计划已生成，请在页面上确认后执行", "data": pending}

    def __clean(self, key: str) -> dict:
        with self._lock:
            pending = self.get_data("pending")
            if not pending or pending.get("key") != key:
                return {"success": False, "message": "没有待执行的清理计划，请先预览"}
            options = pending.get("options") or {}
            actions = pending.get("actions") or {}
            catalog, item = self.__find_item(key)
            if not item:
                return {"success": False, "message": "清单中没有该项，请先同步媒体库"}

            result = {
                "title": pending.get("title", ""),
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "link_deleted": 0,
                "src_deleted": 0,
                "history_deleted": 0,
                "subscribe_deleted": 0,
                "torrents_stopped": [],
                "torrents_removed": [],
                "errors": [],
            }
            storage = StorageChain()

            # 1. 删除链接文件
            if options.get("del_links"):
                for path_str in actions.get("link_files") or []:
                    if self.__delete_file(storage, path_str, options.get("del_empty_dirs", True), result):
                        result["link_deleted"] += 1

            # 2. 删除源文件（按 hash 分组，便于下载器联动；仅对删除成功的文件联动）
            hash_map: Dict[str, dict] = {}
            if options.get("del_source"):
                for path_str in actions.get("src_files") or []:
                    deleted = self.__delete_file(storage, path_str, options.get("del_empty_dirs", True), result)
                    if deleted:
                        result["src_deleted"] += 1
                    h = (item.get("src_hashes") or {}).get(Path(path_str).as_posix(), "")
                    if deleted and h:
                        group = hash_map.setdefault(h, {"srcs": [], "downloader": ""})
                        group["srcs"].append(path_str)
                        if not group["downloader"]:
                            group["downloader"] = (item.get("downloaders") or {}).get(h, "")
                # 下载器联动
                for h, group in hash_map.items():
                    self.__handle_torrent(h, group["downloader"], group["srcs"],
                                          options.get("torrent_mode", "smart"), result)

            # 3. 删除整理记录（v3 Oper：仅删除未绑定整理任务的记录，由宿主状态机管理的记录跳过）
            if options.get("del_history"):
                for hid in actions.get("history_ids") or []:
                    try:
                        self._transferhis.delete(hid)
                        result["history_deleted"] += 1
                    except Exception as e:
                        result["errors"].append(f"删除整理记录 #{hid} 失败: {e}")

            # 4. 删除订阅记录
            if options.get("del_subscribe"):
                for sid in actions.get("subscribe_ids") or []:
                    try:
                        self._subscribehis.delete(sid)
                        result["subscribe_deleted"] += 1
                    except Exception as e:
                        result["errors"].append(f"删除订阅 #{sid} 失败: {e}")

            # 5. 更新清单：移除已清理项（或保留仍存在的文件）
            try:
                item["link_files"] = [f for f in item.get("link_files") or []
                                      if Path(f["path"]).exists()]
                item["history_ids"] = [] if options.get("del_history") else item.get("history_ids")
                item["subscribe_ids"] = [] if options.get("del_subscribe") else item.get("subscribe_ids")
                if not item["link_files"]:
                    catalog["items"] = [i for i in catalog.get("items") or [] if i.get("key") != key]
                else:
                    item["total_size"] = sum(f.get("size") or 0 for f in item["link_files"])
                catalog["stats"] = {
                    "series": sum(1 for i in catalog["items"] if i.get("type") == "电视剧"),
                    "movies": sum(1 for i in catalog["items"] if i.get("type") == "电影"),
                    "files": sum(len(i.get("link_files") or []) for i in catalog["items"]),
                    "size": sum(i.get("total_size") or 0 for i in catalog["items"]),
                }
                self.save_data("catalog", catalog)
            except Exception as e:
                logger.warning(f"{self.plugin_name} 更新清单失败: {e}")

            self.del_data("pending")
            self.save_data("last_result", result)

            # 通知
            if self._notify:
                try:
                    text = (
                        f"媒体：{result['title']}\n"
                        f"链接文件：删除 {result['link_deleted']} 个\n"
                        f"源文件：删除 {result['src_deleted']} 个\n"
                        f"整理记录：删除 {result['history_deleted']} 条\n"
                        f"订阅：删除 {result['subscribe_deleted']} 条\n"
                        f"下载器：暂停 {len(result['torrents_stopped'])} 个 / "
                        f"删除 {len(result['torrents_removed'])} 个任务"
                    )
                    if result["errors"]:
                        text += f"\n失败 {len(result['errors'])} 项（详见日志）"
                    self.post_message(mtype=MessageType.Plugin,
                                      title=f"{self.plugin_name} 清理完成", text=text)
                except Exception as e:
                    logger.warning(f"{self.plugin_name} 发送通知失败: {e}")

            logger.info(f"{self.plugin_name} 清理完成「{result['title']}」: 链接 {result['link_deleted']} / "
                        f"源 {result['src_deleted']} / 记录 {result['history_deleted']} / "
                        f"订阅 {result['subscribe_deleted']}，失败 {len(result['errors'])}")
            return {"success": True, "message": "清理完成", "data": result}

    def __delete_file(self, storage: StorageChain, path_str: str,
                      clean_empty: bool, result: dict) -> bool:
        try:
            p = Path(path_str)
            if len(p.parts) <= 2:
                result["errors"].append(f"路径层级过浅，拒绝删除: {path_str}")
                return False
            if not p.exists():
                return False
            fileitem = FileItem(storage="local",
                                type="file" if p.is_file() else "dir",
                                path=p.as_posix(),
                                name=p.name)
            if clean_empty:
                ok = storage.delete_media_file(fileitem)
            else:
                ok = storage.delete_file(fileitem)
            if ok:
                logger.info(f"{self.plugin_name} 已删除: {path_str}")
                return True
            result["errors"].append(f"删除失败: {path_str}")
            return False
        except Exception as e:
            logger.error(f"{self.plugin_name} 删除文件出错 {path_str}: {e}\n{traceback.format_exc()}")
            result["errors"].append(f"删除出错: {path_str}")
            return False

    def __handle_torrent(self, torrent_hash: str, downloader: str,
                         srcs: List[str], mode: str, result: dict):
        if mode == "none" or not torrent_hash:
            return
        try:
            if mode == "follow":
                # 官方行为：发送下载文件删除事件（载荷 {src, hash}），由宿主联动删除下载任务（不删下载器文件）
                for src in srcs:
                    try:
                        eventmanager.send_event(EventType.DownloadFileDeleted,
                                                {"src": Path(src).as_posix(), "hash": torrent_hash})
                    except Exception as e:
                        logger.warning(f"{self.plugin_name} 发送下载文件删除事件失败 {src}: {e}")
                result["torrents_removed"].append(torrent_hash)
                logger.info(f"{self.plugin_name} 已发送下载文件删除事件: {torrent_hash[:8]}")
                return
            # smart：依据插件清单判断该种子是否还有未删除的源文件（未删完暂停，删完删除）
            remaining = self.__count_remaining_srcs(torrent_hash, srcs)
            if remaining > 0:
                self.chain.stop_torrents(hashs=[torrent_hash], downloader=downloader or None)
                result["torrents_stopped"].append(torrent_hash)
                logger.info(f"{self.plugin_name} 种子 {torrent_hash[:8]} 仍有 {remaining} 个源文件，已暂停任务")
            else:
                self.chain.remove_torrents(hashs=[torrent_hash], delete_file=False,
                                           downloader=downloader or None)
                result["torrents_removed"].append(torrent_hash)
                logger.info(f"{self.plugin_name} 种子 {torrent_hash[:8]} 源文件已全部删除，已删除任务")
        except Exception as e:
            logger.error(f"{self.plugin_name} 下载器联动失败 {torrent_hash[:8]}: {e}\n{traceback.format_exc()}")
            result["errors"].append(f"下载器联动失败: {torrent_hash[:8]}")

    def __count_remaining_srcs(self, torrent_hash: str, deleted_srcs: List[str]) -> int:
        """
        统计清单中同一种子仍有多少源文件未删除（磁盘上仍存在且不在本次删除范围内）。
        """
        deleted_set = {Path(s).as_posix() for s in deleted_srcs or []}
        remaining = 0
        try:
            catalog = self.get_data("catalog") or {}
            for it in catalog.get("items") or []:
                for sp, sh in (it.get("src_hashes") or {}).items():
                    if sh != torrent_hash or sp in deleted_set:
                        continue
                    try:
                        if Path(sp).exists():
                            remaining += 1
                    except OSError:
                        continue
        except Exception as e:
            logger.warning(f"{self.plugin_name} 统计种子剩余文件失败 {torrent_hash[:8]}: {e}")
        return remaining

    def get_dashboard(self, key: str = None, **kwargs) -> Optional[dict]:
        pass
