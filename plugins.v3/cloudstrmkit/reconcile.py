"""
定时对账与手动触发。

两种模式（`reconcile_mode`）：
- `history`（默认，快）：从 MoviePilot 整理历史读取 115 目标路径，补漏生成；
- `scan`（慢，兜底）：遍历 115 媒体库根目录，补漏并清理孤儿（删除受 `delete_sync` 控制）。

安全约束：
- 只有目录扫描成功且结果非空时才允许清理孤儿；
- 孤儿清理仅在 `library_roots` 配置范围内执行。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger

from .attachments import AttachmentSyncer
from .config import CloudStrmKitConfig
from .fnos import FnosRefresher
from .index import IndexEntry, StrmIndex
from .strm import PathMapper, StrmContentBuilder, remove_strm, write_strm
from .u115 import U115Client, U115Error, U115NotFound

# 目录遍历最大深度
MAX_DEPTH = 8
# 单次对账最多处理的文件数（防过量请求）
MAX_ITEMS = 20000
# 整理历史最大读取条数
HISTORY_LIMIT = 10000
# 待确认删除记录文件名
PENDING_FILENAME = "pending_deletions.json"


class Reconciler:
    """115 媒体库对账器。"""

    def __init__(
        self,
        config: CloudStrmKitConfig,
        mapper: PathMapper,
        builder: StrmContentBuilder,
        u115: U115Client,
        index: StrmIndex,
        attachments: Optional[AttachmentSyncer] = None,
        fnos: Optional[FnosRefresher] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        """
        初始化对账器。

        :param config: 插件配置
        :param mapper: 路径映射
        :param builder: STRM 内容构建器
        :param u115: 115 客户端
        :param index: STRM 索引
        :param attachments: 附属文件同步器（可选）
        :param fnos: 飞牛刷新器（可选）
        :param notifier: 通知器（可选）
        """
        self._config = config
        self._mapper = mapper
        self._builder = builder
        self._u115 = u115
        self._index = index
        self._attachments = attachments
        self._fnos = fnos
        self._notifier = notifier
        self._lock = threading.Lock()
        self._running = False
        self._last_run: Optional[Dict[str, Any]] = None
        self._pending_path = Path(index.path).parent / PENDING_FILENAME
        self._pending: List[Dict[str, Any]] = self._load_pending()

    # ------------------------------------------------------------------
    # 待确认删除
    # ------------------------------------------------------------------
    def _load_pending(self) -> List[Dict[str, Any]]:
        """
        从磁盘加载待确认删除记录。

        :return: 待删除项列表
        """
        try:
            if self._pending_path.exists():
                payload = json.loads(self._pending_path.read_text(encoding="utf-8"))
                items = payload.get("items") or []
                return [item for item in items if isinstance(item, dict)]
        except Exception as err:  # noqa: BLE001 - 损坏时忽略
            logger.warning(f"【CloudStrmKit】加载待确认删除失败：{err}")
        return []

    def _save_pending(self, entries: List[IndexEntry]) -> None:
        """
        持久化待确认删除记录。

        :param entries: 候选索引条目
        """
        self._pending = [
            {
                "id": entry.id,
                "pan_path": entry.pan_path,
                "strm_path": entry.strm_path,
            }
            for entry in entries
        ]
        try:
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            self._pending_path.write_text(
                json.dumps({"items": self._pending}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"【CloudStrmKit】保存待确认删除失败：{err}")

    def list_pending(self) -> List[Dict[str, Any]]:
        """
        返回待确认删除列表。

        :return: 待删除项列表
        """
        return list(self._pending)

    def cancel_pending(self) -> int:
        """
        清空待确认删除列表（不删除任何文件）。

        :return: 清空数量
        """
        count = len(self._pending)
        self._pending = []
        try:
            if self._pending_path.exists():
                self._pending_path.unlink()
        except Exception:  # noqa: BLE001
            pass
        return count

    def confirm_pending(self) -> Dict[str, Any]:
        """
        执行用户确认后的删除（删除本地 STRM 与索引条目）。

        :return: 删除结果
        """
        removed = 0
        failed = 0
        for item in list(self._pending):
            entry = self._index.get(str(item.get("id") or ""))
            if entry is None:
                continue
            try:
                remove_strm(Path(entry.strm_path))
                if self._index.remove(entry.id) is not None:
                    removed += 1
            except Exception as err:  # noqa: BLE001
                failed += 1
                logger.warning(f"【CloudStrmKit】确认删除失败：{entry.strm_path}：{err}")
        self._pending = []
        try:
            if self._pending_path.exists():
                self._pending_path.unlink()
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"【CloudStrmKit】确认删除完成：{removed} 条，失败 {failed}")
        return {"removed": removed, "failed": failed}

    def _delete_candidates(self, candidates: List[IndexEntry]) -> Tuple[int, bool]:
        """
        删除候选条目（可逐条确认 115 文件确实不存在，异常时中止）。

        :param candidates: 候选索引条目
        :return: (删除数量, 是否因 115 查询异常中止)
        """
        removed = 0
        for entry in candidates:
            if self._config.delete_sync_verify:
                try:
                    self._u115.get_pickcode(entry.pan_path)
                    logger.debug(
                        f"【CloudStrmKit】115 仍有文件，跳过删除：{entry.pan_path}"
                    )
                    continue
                except U115NotFound:
                    pass
                except U115Error as err:
                    logger.error(
                        f"【CloudStrmKit】删除前确认失败，中止删除：{entry.pan_path}：{err}"
                    )
                    return removed, True
            try:
                remove_strm(Path(entry.strm_path))
                if self._index.remove(entry.id) is not None:
                    removed += 1
            except Exception as err:  # noqa: BLE001
                logger.warning(f"【CloudStrmKit】清理孤儿失败：{entry.strm_path}：{err}")
        return removed, False

    @property
    def running(self) -> bool:
        """
        是否正在对账。

        :return: 运行状态
        """
        return self._running

    @property
    def last_run(self) -> Optional[Dict[str, Any]]:
        """
        返回最近一次对账结果。

        :return: 结果字典或 None
        """
        return self._last_run

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _library_roots(self) -> List[str]:
        """
        返回配置的 115 媒体库根路径列表。

        :return: 路径列表
        """
        roots: List[str] = []
        for raw in (self._config.library_roots or "").splitlines():
            value = raw.strip().rstrip("/")
            if value:
                roots.append(value)
        return roots

    def _list_dir(self, pan_dir: Path) -> Optional[List[Any]]:
        """
        列出 115 目录（v3 的 U115Pan.list 接收 FileItem）。

        :param pan_dir: 目录路径
        :return: FileItem 列表或 None（失败）
        """
        try:
            from app.modules.filemanager.storages.u115 import U115Pan

            pan = U115Pan()
            folder = pan.get_item(pan_dir)
            if folder is None:
                logger.warning(f"【CloudStrmKit】对账目录不存在：{pan_dir}")
                return None
            return list(pan.list(folder) or [])
        except Exception as err:  # noqa: BLE001 - 单个目录失败跳过
            logger.warning(f"【CloudStrmKit】对账列举目录失败：{pan_dir}：{err}")
            return None

    @staticmethod
    def _item_type(item: Any) -> str:
        """
        读取 FileItem 类型（兼容字符串/枚举）。

        :param item: 文件项
        :return: file/dir/空
        """
        value = getattr(item, "type", "") or ""
        raw = str(value)
        if "." in raw:
            raw = raw.rsplit(".", 1)[-1]
        return raw.strip().lower()

    def _within_roots(self, pan_path: str) -> bool:
        """
        判断路径是否在配置的媒体库根目录内。

        :param pan_path: 115 路径
        :return: 是否在范围内
        """
        normalized = (pan_path or "").replace("\\", "/")
        for root in self._library_roots():
            if normalized == root or normalized.startswith(root + "/"):
                return True
        return False

    def _cleanup_renamed(self, pan_path: str) -> int:
        """
        删除同一文件名但路径不同的旧索引条目及其 STRM（改名/改分类场景）。

        :param pan_path: 新文件路径
        :return: 清理数量
        """
        new_name = Path(pan_path).name
        removed = 0
        for entry in self._index.all_items():
            if entry.pan_path == pan_path:
                continue
            if Path(entry.pan_path).name != new_name:
                continue
            remove_strm(Path(entry.strm_path))
            if self._index.remove(entry.id) is not None:
                removed += 1
        return removed

    # ------------------------------------------------------------------
    # 数据源一：115 全库扫描
    # ------------------------------------------------------------------
    def _walk(
        self, pan_dir: Path, results: List[Tuple[str, int, str]], depth: int = 0
    ) -> bool:
        """
        递归遍历 115 目录，收集视频文件（含 pickcode）。

        :param pan_dir: 当前目录
        :param results: 结果列表（append (path, size, pickcode)）
        :param depth: 当前深度
        :return: 是否成功列举（用于安全判断：失败时禁止清理孤儿）
        """
        if depth > MAX_DEPTH or len(results) >= MAX_ITEMS:
            return True
        items = self._list_dir(pan_dir)
        if items is None:
            return False
        # 扫描节流：降低目录列举频率，避免触发 115 访问上限
        interval = float(getattr(self, "_scan_interval", 0) or 0)
        if interval > 0:
            time.sleep(interval)
        ok = True
        for item in items:
            item_type = self._item_type(item)
            name = str(getattr(item, "name", "") or "")
            item_path = getattr(item, "path", None)
            if item_path is None:
                item_path = pan_dir / name
            if item_type in ("dir", "folder"):
                if not self._walk(Path(item_path), results, depth + 1):
                    ok = False
                continue
            ext = Path(name).suffix.lstrip(".").lower()
            if ext not in self._config.video_ext_set:
                continue
            size = int(getattr(item, "size", 0) or 0)
            if size < self._config.min_size_bytes:
                continue
            pickcode = str(getattr(item, "pickcode", "") or "")
            results.append((str(item_path), size, pickcode))
        return ok

    def _scan_library(self) -> Tuple[List[Tuple[str, int, str]], bool]:
        """
        扫描 115 媒体库根目录。

        :return: (视频列表, 是否扫描成功)
        """
        roots = self._library_roots()
        if not roots:
            logger.warning("【CloudStrmKit】未配置 115 媒体库根路径（library_roots）")
            return [], False
        videos: List[Tuple[str, int, str]] = []
        root_status: List[Dict[str, Any]] = []
        scan_ok = False
        for root in roots:
            before = len(videos)
            ok = self._walk(Path(root), videos)
            root_status.append({"root": root, "ok": ok, "found": len(videos) - before})
            scan_ok = scan_ok or ok
            if not ok:
                # 目录列举失败（含风控/异常）：中止后续根目录，避免放大请求
                logger.warning("【CloudStrmKit】目录扫描失败，中止剩余根目录")
                break
        logger.info(
            f"【CloudStrmKit】目录扫描：{len(videos)} 个视频，根目录状态={root_status}"
        )
        return videos, scan_ok

    # ------------------------------------------------------------------
    # 数据源二：整理历史（快）
    # ------------------------------------------------------------------
    @staticmethod
    def _is_target_storage(storage: str) -> bool:
        """
        判断整理历史中的目标存储是否为 115。

        :param storage: 存储名称/类型
        :return: 是否 115
        """
        value = (storage or "").strip().lower()
        return "115" in value or value == "u115"

    def _history_targets(self) -> Optional[List[Tuple[str, int, str]]]:
        """
        从 MoviePilot 整理历史读取 115 目标视频（分页读取 SDK 快照）。

        :return: (路径, 大小, pickcode) 列表；读取失败返回 None
        """
        try:
            from app.schemas.query import QueryPageRequest, TransferHistoryFilter
            from app.sdk.queries import list_transfer_history
        except Exception as err:  # noqa: BLE001 - 宿主接口不可用时回退扫描
            logger.warning(f"【CloudStrmKit】无法导入整理历史查询接口：{err}")
            return None
        seen = set()
        targets: List[Tuple[str, int, str]] = []
        page = 1
        while page <= 50:
            try:
                result = list_transfer_history(
                    TransferHistoryFilter(),
                    QueryPageRequest(page=page, count=200),
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(f"【CloudStrmKit】读取整理历史第 {page} 页失败：{err}")
                if page == 1:
                    return None
                break
            items = list(getattr(result, "items", None) or [])
            if not items:
                break
            for row in items:
                dest = str(getattr(row, "dest", "") or "").replace("\\", "/")
                if not dest or dest in seen:
                    continue
                if not self._is_target_storage(str(getattr(row, "dest_storage", "") or "")):
                    continue
                ext = Path(dest).suffix.lstrip(".").lower()
                if ext not in self._config.video_ext_set:
                    continue
                seen.add(dest)
                pickcode = ""
                raw_item = getattr(row, "dest_fileitem", None)
                if isinstance(raw_item, dict):
                    pickcode = str(raw_item.get("pickcode") or "")
                targets.append((dest, 0, pickcode))
            total = int(getattr(result, "total", 0) or 0)
            if page * 200 >= total:
                break
            page += 1
        logger.info(f"【CloudStrmKit】整理历史对账：{len(targets)} 个 115 视频")
        return targets

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def probe_history(self) -> Dict[str, Any]:
        """
        诊断整理历史数据源（不生成任何文件）。

        :return: 诊断结果
        """
        try:
            from app.schemas.query import QueryPageRequest, TransferHistoryFilter
            from app.sdk.queries import list_transfer_history
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"导入失败：{err}"}
        try:
            first = list_transfer_history(
                TransferHistoryFilter(), QueryPageRequest(page=1, count=1)
            )
            total = int(getattr(first, "total", 0) or 0)
        except Exception as err:  # noqa: BLE001
            return {"ok": False, "error": f"查询总数失败：{err}"}
        targets = self._history_targets()
        if targets is None:
            return {"ok": False, "error": "读取整理历史失败", "total_history": total}
        return {
            "ok": True,
            "total_history": total,
            "targets": len(targets),
            "mode": self._config.reconcile_mode,
            "library_roots": self._config.library_roots,
        }

    def run(self, paths: Optional[List[str]] = None, mode: Optional[str] = None) -> Dict[str, Any]:
        """
        执行一次对账（同步阻塞，调用方负责放到后台线程）。

        :param paths: 可选，仅处理这些 115 路径前缀下的文件（精准修复单个目录）
        :param mode: 可选，覆盖配置的对账模式（history/scan）
        :return: 对账结果
        """
        effective_mode = (mode or self._config.reconcile_mode or "history").strip().lower()
        if effective_mode not in {"history", "scan"}:
            effective_mode = "history"
        self._scan_interval = 0.5 if effective_mode == "scan" else 0.0
        scope = [
            item.strip().replace("\\", "/").rstrip("/")
            for item in (paths or [])
            if item and item.strip()
        ]
        with self._lock:
            if self._running:
                return {"success": False, "message": "对账正在进行中", "data": self._last_run or {}}
            self._running = True
        started = time.monotonic()
        result: Dict[str, Any] = {
            "success": True,
            "mode": effective_mode,
            "scope": scope,
            "generated": 0,
            "removed": 0,
            "scanned": 0,
            "failed": 0,
            "refreshed": 0,
        }
        try:
            videos: List[Tuple[str, int, str]] = []
            scan_ok = False
            if effective_mode == "history":
                history = self._history_targets()
                if history is None:
                    result.update(
                        success=False,
                        message="整理历史读取失败（详见日志；可改用 scan 模式或查看 /diag/reconcile-source）",
                    )
                    return result
                videos = history
            else:
                videos, scan_ok = self._scan_library()

            if scope:
                videos = [
                    item
                    for item in videos
                    if any(
                        item[0] == prefix or item[0].startswith(prefix + "/")
                        for prefix in scope
                    )
                ]

            if not videos and not scan_ok:
                result.update(
                    success=False,
                    message="对账数据源不可用（整理历史与目录扫描均失败）",
                )
                return result

            result["scanned"] = len(videos)
            if self._attachments is not None:
                self._attachments.clear_cache()
            pan_paths = {path for path, _, _ in videos}
            refresh_dirs: List[Path] = []
            pickcode_cache: Dict[str, str] = {}

            # 1. 补漏生成（先写文件，最后批量落索引）
            new_entries: List[IndexEntry] = []
            for pan_path, _size, host_pickcode in videos:
                if self._stop_if_needed():
                    break
                local_path = self._mapper.map(pan_path)
                if local_path is None:
                    continue
                strm_path = local_path.with_suffix(".strm")
                entry = self._index.get_by_pan_path(pan_path)
                if entry is not None and strm_path.exists():
                    # 已存在时仍清理同名旧路径条目（改名/改分类残留）
                    self._cleanup_renamed(pan_path)
                    continue
                try:
                    # pickcode 缺失时不在对账阶段补查（否则数千次 115 请求极慢），
                    # 由播放时按路径补查并回填索引。
                    pickcode = host_pickcode or pickcode_cache.get(pan_path) or ""
                    entry = IndexEntry(
                        id=StrmIndex.make_id(pan_path),
                        pan_storage=self._config.source_storage,
                        pan_path=pan_path,
                        pickcode=pickcode,
                        strm_path=str(strm_path),
                    )
                    write_strm(strm_path, self._builder.build_url(entry.id))
                    self._cleanup_renamed(pan_path)
                    new_entries.append(entry)
                    result["generated"] += 1
                    refresh_dirs.append(strm_path.parent)
                    if (
                        self._attachments is not None
                        and self._config.reconcile_sync_attachments
                        and effective_mode != "scan"
                    ):
                        try:
                            self._attachments.sync_for_video(pan_path, local_path, entry)
                        except Exception as err:  # noqa: BLE001
                            logger.warning(
                                f"【CloudStrmKit】对账附属文件同步失败：{pan_path}：{err}"
                            )
                except U115Error as err:
                    result["failed"] += 1
                    logger.warning(f"【CloudStrmKit】对账补漏失败：{pan_path}：{err}")
                except Exception as err:  # noqa: BLE001
                    result["failed"] += 1
                    logger.error(f"【CloudStrmKit】对账补漏异常：{pan_path}：{err}")
            if new_entries:
                self._index.add_many(new_entries)

            # 2. 清理孤儿（仅全库扫描模式、扫描成功且有结果时执行；带批量保护）
            if (
                self._config.delete_sync
                and effective_mode == "scan"
                and scan_ok
                and result["scanned"] > 0
            ):
                candidates = [
                    entry
                    for entry in self._index.all_items()
                    if self._within_roots(entry.pan_path)
                    and entry.pan_path not in pan_paths
                ]
                if candidates:
                    total_indexed = max(1, len(self._index))
                    ratio = len(candidates) / total_indexed
                    need_confirm = (
                        len(candidates) > self._config.delete_sync_batch_limit
                        or ratio > self._config.delete_sync_max_ratio
                    )
                    if need_confirm:
                        self._save_pending(candidates)
                        result["pending_confirm"] = len(candidates)
                        result["message"] = (
                            f"检测到 {len(candidates)} 个本地 STRM 对应 115 文件不存在"
                            f"（占索引 {ratio:.1%}），超过自动删除阈值，"
                            f"已挂起等待确认（GET /delete/pending → POST /delete/confirm）"
                        )
                        logger.warning(f"【CloudStrmKit】{result['message']}")
                        if self._notifier is not None:
                            self._notifier.send(
                                "CloudStrmKit 待确认删除",
                                result["message"],
                                dedupe_key="delete_pending",
                                cooldown=600,
                            )
                    else:
                        removed, aborted = self._delete_candidates(candidates)
                        result["removed"] = removed
                        if aborted:
                            result["message"] = (
                                f"删除过程中检测到 115 查询异常，已中止"
                                f"（已删除 {removed} 条），疑似 115 故障，请检查"
                            )
                            logger.error(f"【CloudStrmKit】{result['message']}")
                            if self._notifier is not None:
                                self._notifier.send(
                                    "CloudStrmKit 删除中止",
                                    result["message"],
                                    dedupe_key="delete_abort",
                                    cooldown=600,
                                )
                        else:
                            for entry in candidates:
                                path = Path(str(entry.strm_path))
                                if path.parent.exists():
                                    refresh_dirs.append(path.parent)

            # 3. 飞牛刷新（去重）
            if self._fnos is not None:
                seen_dirs = set()
                for directory in refresh_dirs:
                    fnos_dir = PathMapper.to_fnos_path(
                        directory,
                        self._config.strm_root_local,
                        self._config.strm_root_fnos,
                    )
                    if fnos_dir is None or str(fnos_dir) in seen_dirs:
                        continue
                    seen_dirs.add(str(fnos_dir))
                    item = self._fnos.build_item(None, None, None, None, fnos_dir)
                    if item is not None:
                        self._fnos.queue(item)
                        result["refreshed"] += 1

            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            logger.info(
                f"【CloudStrmKit】对账完成（{result['mode']}）：扫描 {result['scanned']}，"
                f"补漏 {result['generated']}，清理 {result['removed']}，"
                f"失败 {result['failed']}，刷新 {result['refreshed']}"
            )
            if self._notifier is not None and (
                result["generated"] or result["removed"] or result["failed"]
            ):
                self._notifier.send(
                    "CloudStrmKit 对账完成",
                    f"模式 {result['mode']}，扫描 {result['scanned']}，"
                    f"补漏 {result['generated']}，清理 {result['removed']}，"
                    f"失败 {result['failed']}",
                    dedupe_key="reconcile",
                    cooldown=3600,
                )
            return result
        finally:
            with self._lock:
                self._running = False
                self._last_run = dict(result)

    @staticmethod
    def _stop_if_needed() -> bool:
        """
        是否中断对账（保留扩展点）。

        :return: 是否需要停止
        """
        return False
