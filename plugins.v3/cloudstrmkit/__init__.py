"""
CloudStrmKit 插件入口。

当前阶段（T6）：整理完成事件驱动 STRM 生成、飞牛媒体库刷新、302 播放、诊断接口。
后续任务：附属文件同步、定时对账（见 docs/PLAN.md）。
"""

import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.types import EventType, MediaType
from app.sdk.events import eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from fastapi import Request
from fastapi.responses import Response

from .attachments import AttachmentSyncer
from .config import CloudStrmKitConfig
from .diagnostics import (
    DiagEventLog,
    extract_transfer_fields,
    probe_u115_download,
    probe_u115_list,
)
from .fnos import FnosRefresher
from .index import IndexEntry, StrmIndex
from .redirect import PlayService
from .reconcile import Reconciler
from .strm import PathMapper, StrmContentBuilder, write_strm
from .u115 import U115Client, U115Error

# 插件标识（与 package.v3.json 的 key 一致）
PLUGIN_ID = "CloudStrmKit"

# 115 探针默认候选 UA（飞牛客户端 UA 待从播放日志确认后补充）
DEFAULT_PROBE_USER_AGENTS = [
    "",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "MoviePilot/CloudStrmKit",
]


class CloudStrmKit(_PluginBase):
    """云盘STRM工具箱：115 网盘 STRM 生成与 302 直链播放。"""

    # 插件名称
    plugin_name = "云盘STRM工具箱"
    # 插件描述
    plugin_desc = "115 网盘 STRM 生成、302 直链播放与飞牛影视实时刷新。"
    # 插件图标
    plugin_icon = (
        "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/"
        "refs/heads/v2/src/assets/images/misc/u115.png"
    )
    # 插件版本
    plugin_version = "0.6.1"
    # 插件作者
    plugin_author = "elliotsu1107"
    # 作者主页
    author_url = "https://github.com/elliotsu1107"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudstrmkit_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 运行时状态
    _config: CloudStrmKitConfig = CloudStrmKitConfig()
    _index: Optional[StrmIndex] = None
    _diag: Optional[DiagEventLog] = None
    _mapper: Optional[PathMapper] = None
    _builder: Optional[StrmContentBuilder] = None
    _u115: Optional[U115Client] = None
    _fnos: Optional[FnosRefresher] = None
    _play: Optional[PlayService] = None
    _attachments: Optional[AttachmentSyncer] = None
    _reconciler: Optional[Reconciler] = None
    _queue: Optional["queue.Queue[Dict[str, Any]]"] = None
    _worker: Optional[threading.Thread] = None
    _stop_event: threading.Event = threading.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化插件：配置、索引、映射、115 客户端、飞牛刷新器与后台工作线程。

        :param config: 插件配置字典
        """
        self._config = self._parse_config(config)
        data_dir = self.get_data_path()
        self._index = StrmIndex(data_dir)
        self._diag = DiagEventLog(data_dir)

        secret = self._config.secret or self._ensure_secret(config)
        self._mapper = PathMapper(self._config.path_mappings)
        self._builder = StrmContentBuilder(self._config.base_url, secret, PLUGIN_ID)
        self._u115 = U115Client(cache_ttl=self._config.cache_ttl, qps=self._config.qps)
        self._fnos = FnosRefresher(self._config.fnos_instance)
        self._play = PlayService(
            self._index, self._builder, self._u115, debug=self._config.debug
        )
        self._attachments = AttachmentSyncer(self._config, self._u115, self._index)
        self._reconciler = Reconciler(
            self._config,
            self._mapper,
            self._builder,
            self._u115,
            self._index,
            self._attachments,
            self._fnos,
        )

        self._queue = queue.Queue(maxsize=2000)
        self._stop_event = threading.Event()
        if self._config.enabled:
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="CloudStrmKitWorker",
                daemon=True,
            )
            self._worker.start()

        logger.info(
            f"【CloudStrmKit】插件初始化，enabled={self._config.enabled}，"
            f"索引条目={len(self._index)}，映射={len(self._mapper.mappings)} 条，"
            f"飞牛实例={self._config.fnos_instance or '未配置'}，数据目录={data_dir}"
        )

    def _ensure_secret(self, config: Optional[Dict[str, Any]]) -> str:
        """
        生成并持久化 STRM 签名密钥。

        :param config: 当前插件配置（可能为 None）
        :return: 新生成的密钥
        """
        new_secret = uuid.uuid4().hex
        merged = dict(config or {})
        merged["secret"] = new_secret
        try:
            self.update_config(merged)
            logger.info("【CloudStrmKit】已自动生成 STRM 签名密钥并写入配置")
        except Exception as err:  # noqa: BLE001 - 配置写入失败不阻断插件启动
            logger.error(f"【CloudStrmKit】写入签名密钥失败：{err}")
        return new_secret

    def get_state(self) -> bool:
        """
        返回插件运行状态。

        :return: 是否启用
        """
        return self._config.enabled

    def stop_service(self) -> None:
        """
        停止插件服务：停止后台线程与刷新器，保存索引并释放引用。
        """
        self._stop_event.set()
        if self._fnos is not None:
            self._fnos.stop()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=5)
        if self._index is not None:
            try:
                self._index.save()
            except Exception as err:  # noqa: BLE001 - 停止阶段不阻断宿主
                logger.error(f"【CloudStrmKit】停止时保存索引失败：{err}")
        self._worker = None
        self._index = None
        self._play = None
        self._queue = None
        logger.info("【CloudStrmKit】插件已停止")

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_config(config: Optional[Dict[str, Any]]) -> CloudStrmKitConfig:
        """
        把插件配置字典解析为配置模型，非法值回退默认值。

        :param config: 插件配置字典
        :return: 配置模型
        """
        if not config:
            return CloudStrmKitConfig()
        try:
            return CloudStrmKitConfig(**config)
        except Exception as err:  # noqa: BLE001 - 配置非法时不让插件崩溃
            logger.error(f"【CloudStrmKit】配置解析失败，使用默认配置：{err}")
            return CloudStrmKitConfig()

    # ------------------------------------------------------------------
    # 定时服务
    # ------------------------------------------------------------------
    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """
        注册定时对账服务。

        :return: 服务声明列表
        """
        if not (self._config.reconcile_cron or "").strip():
            return None
        return [
            {
                "id": "CloudStrmKitReconcile",
                "name": "STRM 对账",
                "trigger": self._config.reconcile_cron.strip(),
                "func": self._run_reconcile_job,
                "kwargs": {},
            }
        ]

    def _run_reconcile_job(self) -> None:
        """定时任务入口：后台线程执行对账，避免阻塞调度器。"""
        if self._reconciler is None or not self._config.enabled:
            return
        threading.Thread(
            target=self._reconciler.run,
            name="CloudStrmKitReconcile",
            daemon=True,
        ).start()

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件 API。

        :return: 接口声明列表
        """
        return [
            {
                "path": "/ping",
                "endpoint": self.ping,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "健康检查",
                "description": "返回 CloudStrmKit 运行状态与索引统计",
            },
            {
                "path": "/stats",
                "endpoint": self.api_stats,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "索引统计",
                "description": "返回 STRM 索引统计信息",
            },
            {
                "path": "/diag/event-log",
                "endpoint": self.api_event_log,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "整理事件记录",
                "description": "返回最近记录的 TransferComplete 事件载荷（T3 验证用）",
            },
            {
                "path": "/diag/u115",
                "endpoint": self.api_probe_u115,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "115 换链探针",
                "description": "按 115 路径查 pickcode，用多个 UA 换链并探测直链状态（T3 验证用）",
            },
            {
                "path": "/diag/u115-list",
                "endpoint": self.api_probe_u115_list,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "115 目录列举诊断",
                "description": "对比宿主 U115Pan.list 与目录查询结果，定位对账扫描失败原因",
            },
            {
                "path": "/diag/reconcile-source",
                "endpoint": self.api_diag_reconcile_source,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "对账数据源诊断",
                "description": "检查整理历史可读性与目标数量（不生成文件）",
            },
            {
                "path": "/diag/clear-events",
                "endpoint": self.api_clear_events,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空事件记录",
                "description": "清空 T3 诊断事件记录",
            },
            {
                "path": "/play",
                "endpoint": self.api_play,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "302 播放",
                "description": "校验签名后用请求方 UA 换取 115 直链并返回 302",
            },
            {
                "path": "/play/logs",
                "endpoint": self.api_play_logs,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "播放日志",
                "description": "返回最近的播放请求日志",
            },
            {
                "path": "/reconcile",
                "endpoint": self.api_reconcile,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动对账",
                "description": "后台执行一次 115 媒体库对账（补漏/清理孤儿）",
            },
            {
                "path": "/reconcile/status",
                "endpoint": self.api_reconcile_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "对账状态",
                "description": "返回对账运行状态与最近一次结果",
            },
            {
                "path": "/attachments/orphans",
                "endpoint": self.api_scan_orphans,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "孤儿附属文件清单",
                "description": "扫描 STRM 目录下没有对应 .strm 的附属文件（只读）",
            },
            {
                "path": "/attachments/cleanup",
                "endpoint": self.api_cleanup_orphans,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清理孤儿附属文件",
                "description": "删除用户确认的孤儿附属文件（仅限 STRM 根目录与白名单扩展名）",
            },
        ]

    def ping(self) -> Dict[str, Any]:
        """
        健康检查接口。

        :return: 统一响应结构
        """
        return {
            "success": True,
            "message": "",
            "data": {
                "plugin": PLUGIN_ID,
                "version": self.plugin_version,
                "enabled": self._config.enabled,
                "items": len(self._index) if self._index is not None else 0,
            },
        }

    def api_stats(self) -> Dict[str, Any]:
        """
        索引统计接口。

        :return: 统一响应结构
        """
        stats = self._index.stats() if self._index is not None else {}
        return {"success": True, "message": "", "data": stats}

    # ------------------------------------------------------------------
    # 整理完成事件 → 后台队列 → STRM 生成
    # ------------------------------------------------------------------
    @eventmanager.register(EventType.TransferComplete)
    async def on_transfer_complete(self, event: Any) -> None:
        """
        接收整理完成事件：记录载荷并提交后台生成任务。

        :param event: 事件对象
        """
        try:
            fields = extract_transfer_fields(event.event_data)
            if self._diag is not None:
                self._diag.record(fields)
            if not self._config.enabled or self._queue is None:
                return
            task = self._build_task(fields)
            if task is None:
                return
            logger.info(
                f"【CloudStrmKit】接收整理完成：{len(task['files'])} 个文件，"
                f"存储={task['storage']}"
            )
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                logger.warning("【CloudStrmKit】任务队列已满，丢弃本次整理事件")
        except Exception as err:  # noqa: BLE001 - 事件处理异常不能外抛
            logger.error(f"【CloudStrmKit】处理整理完成事件失败：{err}")

    def _build_task(self, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        从事件载荷构造生成任务（过滤存储与视频文件）。

        :param fields: 事件关键字段
        :return: 任务字典或 None（无需处理）
        """
        if not fields.get("success"):
            return None
        target_item = fields.get("target_item") or {}
        storage = fields.get("target_storage") or target_item.get("storage") or ""
        if not self._is_target_storage(str(storage)):
            return None
        media = fields.get("media") or {}
        raw_files = [p for p in (fields.get("file_list_new") or []) if p]
        target_path = target_item.get("path") or ""
        if not raw_files and target_path:
            raw_files = [target_path]
        files = [str(p) for p in raw_files if self._is_video_path(str(p))]
        if not files:
            return None
        return {
            "storage": str(storage),
            "files": files,
            "pickcode": str(target_item.get("pickcode") or ""),
            "media": media,
        }

    def _is_target_storage(self, storage: str) -> bool:
        """
        判断整理目标存储是否为配置的 115 存储（名称或类型）。

        :param storage: 存储名称/类型
        :return: 是否匹配
        """
        storage = (storage or "").strip()
        if not storage:
            return False
        configured = (self._config.source_storage or "").strip()
        if configured and storage == configured:
            return True
        lowered = storage.lower()
        return "115" in lowered or lowered in {"u115", "115网盘"}

    def _is_video_path(self, path: str) -> bool:
        """
        判断路径是否为配置的视频扩展名。

        :param path: 文件路径
        :return: 是否视频
        """
        ext = Path(path).suffix.lstrip(".").lower()
        return bool(ext) and ext in self._config.video_ext_set

    @staticmethod
    def _to_media_type(value: Any) -> Optional[MediaType]:
        """
        把事件载荷中的媒体类型（可能是枚举字符串或中文值）转换为 MediaType。

        :param value: 原始类型值
        :return: MediaType 或 None
        """
        if not value:
            return None
        raw = str(value)
        if "." in raw:
            raw = raw.rsplit(".", 1)[-1]
        normalized = raw.strip().lower()
        mapping = {
            "tv": MediaType.TV,
            "tvshow": MediaType.TV,
            "series": MediaType.TV,
            "电视剧": MediaType.TV,
            "movie": MediaType.MOVIE,
            "电影": MediaType.MOVIE,
        }
        return mapping.get(normalized)

    def _worker_loop(self) -> None:
        """后台任务循环：串行处理生成任务。"""
        assert self._queue is not None
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process_task(task)
            except Exception as err:  # noqa: BLE001 - 后台任务异常不能终止线程
                logger.error(f"【CloudStrmKit】处理生成任务失败：{err}", exc_info=True)
            finally:
                self._queue.task_done()

    def _process_task(self, task: Dict[str, Any]) -> None:
        """
        处理单个整理任务：生成 STRM、写入索引、提交飞牛刷新。

        :param task: 生成任务
        """
        if self._index is None or self._mapper is None or self._builder is None:
            return
        media = task.get("media") or {}
        generated = 0
        refresh_items: List[Any] = []
        for pan_path in task.get("files") or []:
            pan_path = str(pan_path)
            local_path = self._mapper.map(pan_path)
            if local_path is None:
                logger.warning(f"【CloudStrmKit】路径无映射，跳过：{pan_path}")
                continue
            strm_path = local_path.with_suffix(".strm")
            file_pickcode = str(task.get("pickcode") or "")
            if not file_pickcode and self._u115 is not None:
                try:
                    file_pickcode = self._u115.get_pickcode(pan_path)
                except U115Error as err:
                    logger.error(f"【CloudStrmKit】获取 pickcode 失败：{pan_path}：{err}")
                    continue
            if not file_pickcode:
                logger.error(f"【CloudStrmKit】缺少 pickcode，跳过：{pan_path}")
                continue
            try:
                entry = self._index.upsert(
                    IndexEntry(
                        id=StrmIndex.make_id(pan_path),
                        pan_storage=str(task.get("storage") or ""),
                        pan_path=pan_path,
                        pickcode=file_pickcode,
                        strm_path=str(strm_path),
                        media_type=str(media.get("type") or ""),
                    )
                )
                url = self._builder.build_url(entry.id)
                write_strm(strm_path, url)
                generated += 1
            except Exception as err:  # noqa: BLE001 - 单个文件失败不影响其他文件
                logger.error(f"【CloudStrmKit】生成 STRM 失败：{strm_path}：{err}")
                continue

            # 附属文件同步（NFO/海报/字幕；失败不阻断主流程）
            if self._attachments is not None:
                try:
                    self._attachments.sync_for_video(pan_path, local_path, entry)
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"【CloudStrmKit】附属文件同步失败：{pan_path}：{err}")

            # 飞牛刷新项：使用 STRM 所在影片目录的飞牛视角路径
            fnos_dir = PathMapper.to_fnos_path(
                strm_path.parent,
                self._config.strm_root_local,
                self._config.strm_root_fnos,
            )
            if fnos_dir is None or self._fnos is None:
                continue
            media_type = self._to_media_type(media.get("type"))
            item = self._fnos.build_item(
                title=media.get("title"),
                year=media.get("year"),
                media_type=media_type,
                category=media.get("category"),
                target_path=fnos_dir,
            )
            if item is not None:
                refresh_items.append(item)

        if self._fnos is not None:
            for item in refresh_items:
                self._fnos.queue(item)
        logger.info(
            f"【CloudStrmKit】生成完成：{generated} 个 STRM，刷新项 {len(refresh_items)} 个"
        )

    def api_event_log(self, limit: int = 20) -> Dict[str, Any]:
        """
        返回最近记录的整理完成事件载荷。

        :param limit: 最大条数
        :return: 统一响应结构
        """
        items = self._diag.recent(limit) if self._diag else []
        return {"success": True, "message": "", "data": {"items": items}}

    def api_clear_events(self) -> Dict[str, Any]:
        """
        清空整理完成事件记录。

        :return: 统一响应结构
        """
        if self._diag is not None:
            self._diag.clear()
        return {"success": True, "message": "已清空", "data": None}

    def api_probe_u115(self, path: str = "", uas: str = "") -> Dict[str, Any]:
        """
        115 换链探针：按路径查 pickcode，用多个 UA 换链并探测直链可访问性。

        :param path: 115 文件完整路径
        :param uas: 逗号分隔的 UA 列表；为空时使用内置候选
        :return: 统一响应结构
        """
        if not path or not path.strip():
            return {"success": False, "message": "缺少 path 参数（115 文件完整路径）", "data": None}
        agents = [item.strip() for item in uas.split(",")] if uas else list(DEFAULT_PROBE_USER_AGENTS)
        result = probe_u115_download(path.strip(), agents)
        ok = bool(result.get("ok"))
        return {
            "success": ok,
            "message": "" if ok else str(result.get("error") or "探针失败"),
            "data": result,
        }

    def api_probe_u115_list(self, path: str = "") -> Dict[str, Any]:
        """
        115 目录列举诊断。

        :param path: 115 目录路径
        :return: 统一响应结构
        """
        if not path or not path.strip():
            return {"success": False, "message": "缺少 path 参数（115 目录路径）", "data": None}
        result = probe_u115_list(path.strip())
        return {"success": bool(result.get("ok")), "message": "", "data": result}

    def api_diag_reconcile_source(self) -> Dict[str, Any]:
        """
        对账数据源诊断（整理历史可读性）。

        :return: 统一响应结构
        """
        if self._reconciler is None:
            return {"success": False, "message": "插件未初始化", "data": None}
        result = self._reconciler.probe_history()
        return {"success": bool(result.get("ok")), "message": str(result.get("error") or ""), "data": result}

    # ------------------------------------------------------------------
    # 播放（302）
    # ------------------------------------------------------------------
    def api_play(self, request: Request, f: str = "", e: int = 0, sig: str = "") -> Response:
        """
        播放请求入口：验签 → 换链 → 302。

        :param request: FastAPI 请求对象
        :param f: 索引 ID
        :param e: 过期时间戳
        :param sig: 签名
        :return: 302 或错误响应
        """
        if self._play is None:
            return Response(
                status_code=503,
                content=json.dumps({"success": False, "message": "插件未初始化"}, ensure_ascii=False),
                media_type="application/json",
            )
        return self._play.handle(request, f, e, sig)

    def api_play_logs(self, limit: int = 50) -> Dict[str, Any]:
        """
        返回最近播放日志。

        :param limit: 最大条数
        :return: 统一响应结构
        """
        logs = self._play.recent_logs(limit) if self._play else []
        return {"success": True, "message": "", "data": {"items": logs}}

    # ------------------------------------------------------------------
    # 对账
    # ------------------------------------------------------------------
    def api_reconcile(self) -> Dict[str, Any]:
        """
        手动触发一次对账（后台执行）。

        :return: 统一响应结构
        """
        if self._reconciler is None:
            return {"success": False, "message": "插件未初始化", "data": None}
        if self._reconciler.running:
            return {"success": False, "message": "对账正在进行中", "data": None}
        threading.Thread(
            target=self._reconciler.run,
            name="CloudStrmKitReconcileManual",
            daemon=True,
        ).start()
        return {"success": True, "message": "对账已开始（后台执行）", "data": None}

    def api_reconcile_status(self) -> Dict[str, Any]:
        """
        返回对账运行状态与最近一次结果。

        :return: 统一响应结构
        """
        if self._reconciler is None:
            return {"success": False, "message": "插件未初始化", "data": None}
        return {
            "success": True,
            "message": "",
            "data": {
                "running": self._reconciler.running,
                "last_run": self._reconciler.last_run,
                "cron": self._config.reconcile_cron,
                "library_roots": self._config.library_roots,
            },
        }

    def api_scan_orphans(self) -> Dict[str, Any]:
        """
        扫描本地 STRM 目录下的孤儿附属文件（只读）。

        :return: 统一响应结构
        """
        if self._attachments is None:
            return {"success": False, "message": "插件未初始化", "data": None}
        items = self._attachments.scan_orphans(self._config.strm_root_local)
        return {"success": True, "message": "", "data": {"items": items, "count": len(items)}}

    def api_cleanup_orphans(self, paths: str = "") -> Dict[str, Any]:
        """
        删除用户确认的孤儿附属文件。

        :param paths: 逗号分隔的绝对路径列表
        :return: 统一响应结构
        """
        if self._attachments is None:
            return {"success": False, "message": "插件未初始化", "data": None}
        files = [item.strip() for item in (paths or "").split(",") if item.strip()]
        if not files:
            return {
                "success": False,
                "message": "缺少 paths 参数（逗号分隔的绝对路径）",
                "data": None,
            }
        result = self._attachments.cleanup_orphans(self._config.strm_root_local, files)
        return {"success": True, "message": "", "data": result}

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        拼装插件配置页。

        :return: Vuetify 组件树与默认配置
        """
        defaults = CloudStrmKitConfig().model_dump()
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "整理完成后自动生成 STRM 并刷新飞牛媒体库；"
                                            "播放时本插件用请求方 UA 换取 115 直链并 302。"
                                            "请先配置 115 存储名称、飞牛实例、基础地址与路径映射。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 基础设置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "source_storage",
                                            "label": "115 存储名称",
                                            "placeholder": "115网盘",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fnos_instance",
                                            "label": "飞牛媒体服务器实例名",
                                            "placeholder": "如 MyFnos",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_url",
                                            "label": "对外基础地址",
                                            "placeholder": "https://mp.example.com",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM 路径
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strm_root_local",
                                            "label": "容器内 STRM 根目录",
                                            "placeholder": "/strm_test",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strm_root_fnos",
                                            "label": "飞牛视角 STRM 根目录",
                                            "placeholder": "/path/to/strm",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "video_exts",
                                            "label": "视频扩展名",
                                            "placeholder": "mkv,mp4,ts",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_size_mb",
                                            "label": "最小文件大小(MB)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射（每行：115前缀 => 容器内根目录）",
                                            "rows": 4,
                                            "placeholder": "/movie => /strm_test/movie\n/tv => /strm_test/tv",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 302 与安全
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cache_ttl",
                                            "label": "直链缓存(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "qps",
                                            "label": "换链限频(QPS)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cooldown_seconds",
                                            "label": "风控冷却(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "secret",
                                            "label": "STRM 签名密钥",
                                            "placeholder": "留空自动生成",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 附属文件
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "copy_exts",
                                            "label": "附属文件扩展名",
                                            "placeholder": "nfo,jpg,png,srt",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "attachment_max_mb",
                                            "label": "附属文件大小上限(MB)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "attachment_conflict",
                                            "label": "附属文件冲突策略",
                                            "items": [
                                                {"title": "跳过已有文件", "value": "skip"},
                                                {"title": "覆盖已有文件", "value": "overwrite"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 对账
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "reconcile_cron",
                                            "label": "对账 Cron",
                                            "placeholder": "30 4 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "reconcile_mode",
                                            "label": "对账模式",
                                            "items": [
                                                {"title": "整理历史（快）", "value": "history"},
                                                {"title": "扫描 115 全库（慢）", "value": "scan"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_sync",
                                            "label": "115 删除后同步删除本地文件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "reconcile_sync_attachments",
                                            "label": "对账时同步附属文件（首次全量建议关闭）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "debug", "label": "调试日志"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "library_roots",
                                            "label": "对账用 115 媒体库根路径（每行一个）",
                                            "rows": 3,
                                            "placeholder": "/movie/电影\n/tv/电视剧",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], defaults

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------
    def get_page(self) -> Optional[List[dict]]:
        """
        拼装插件详情页（索引统计）。

        :return: Vuetify 组件树
        """
        stats = self._index.stats() if self._index is not None else {}
        cards = [
            ("索引条目", stats.get("total", 0)),
            ("附属文件", stats.get("attachments", 0)),
            ("目录级附属文件", stats.get("dir_attachments", 0)),
            ("缺失 pickcode", stats.get("missing_pickcode", 0)),
        ]
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6, "lg": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {"class": "text-h6"},
                                                "text": str(value),
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": label,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                    for label, value in cards
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "secondary",
                                    "variant": "tonal",
                                    "text": f"索引文件：{stats.get('index_path', '未初始化')}",
                                },
                            }
                        ],
                    }
                ],
            },
        ]
