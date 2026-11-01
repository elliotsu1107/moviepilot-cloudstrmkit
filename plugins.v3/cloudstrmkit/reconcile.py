"""
定时对账与手动触发。

职责（PRD F4）：
- 遍历配置的 115 媒体库根目录，找出漏生成 STRM 的视频并补齐；
- 清理“115 已不存在但本地仍有 STRM”的孤儿条目（受 `delete_sync` 控制）；
- 对账结果汇总并触发飞牛媒体库刷新；
- 仅在 `library_roots` 配置范围内执行删除，避免误删用户其他文件。
"""

from __future__ import annotations

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
from .u115 import U115Client, U115Error

# 目录遍历最大深度
MAX_DEPTH = 8
# 单次对账最多处理的文件数（防过量请求）
MAX_ITEMS = 20000


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
        """
        self._config = config
        self._mapper = mapper
        self._builder = builder
        self._u115 = u115
        self._index = index
        self._attachments = attachments
        self._fnos = fnos
        self._lock = threading.Lock()
        self._running = False
        self._last_run: Optional[Dict[str, Any]] = None

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
        列出 115 目录。

        :param pan_dir: 目录路径
        :return: FileItem 列表或 None
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

    def _walk(self, pan_dir: Path, results: List[Tuple[str, int]], depth: int = 0) -> bool:
        """
        递归遍历 115 目录，收集视频文件。

        :param pan_dir: 当前目录
        :param results: 结果列表（append (path, size)）
        :param depth: 当前深度
        :return: 是否成功列举（用于安全判断：列举失败时禁止清理孤儿）
        """
        if depth > MAX_DEPTH or len(results) >= MAX_ITEMS:
            return True
        items = self._list_dir(pan_dir)
        if items is None:
            return False
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
            results.append((str(item_path), size))
        return ok

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

    def run(self) -> Dict[str, Any]:
        """
        执行一次对账（同步阻塞，调用方负责放到后台线程）。

        :return: 对账结果
        """
        with self._lock:
            if self._running:
                return {"success": False, "message": "对账正在进行中", "data": self._last_run or {}}
            self._running = True
        started = time.monotonic()
        result: Dict[str, Any] = {
            "success": True,
            "generated": 0,
            "removed": 0,
            "scanned": 0,
            "failed": 0,
            "refreshed": 0,
        }
        try:
            roots = self._library_roots()
            if not roots:
                result.update(success=False, message="未配置 115 媒体库根路径（library_roots）")
                return result

            videos: List[Tuple[str, int]] = []
            root_status: List[Dict[str, Any]] = []
            scan_ok = False
            for root in roots:
                before = len(videos)
                ok = self._walk(Path(root), videos)
                root_status.append({"root": root, "ok": ok, "found": len(videos) - before})
                scan_ok = scan_ok or ok
            result["scanned"] = len(videos)
            result["root_status"] = root_status
            logger.info(
                f"【CloudStrmKit】对账开始：扫描到 {len(videos)} 个视频，"
                f"根目录状态={root_status}"
            )

            pan_paths = {path for path, _ in videos}
            refresh_dirs: List[Path] = []
            pickcode_cache: Dict[str, str] = {}

            # 1. 补漏生成
            for pan_path, _size in videos:
                if self._stop_if_needed():
                    break
                local_path = self._mapper.map(pan_path)
                if local_path is None:
                    continue
                strm_path = local_path.with_suffix(".strm")
                entry = self._index.get_by_pan_path(pan_path)
                if entry is not None and strm_path.exists():
                    continue
                try:
                    pickcode = pickcode_cache.get(pan_path)
                    if not pickcode:
                        pickcode = self._u115.get_pickcode(pan_path)
                        pickcode_cache[pan_path] = pickcode
                    entry = self._index.upsert(
                        IndexEntry(
                            id=StrmIndex.make_id(pan_path),
                            pan_storage=self._config.source_storage,
                            pan_path=pan_path,
                            pickcode=pickcode,
                            strm_path=str(strm_path),
                        )
                    )
                    write_strm(strm_path, self._builder.build_url(entry.id))
                    result["generated"] += 1
                    refresh_dirs.append(strm_path.parent)
                    if self._attachments is not None:
                        try:
                            self._attachments.sync_for_video(pan_path, local_path, entry)
                        except Exception as err:  # noqa: BLE001
                            logger.warning(f"【CloudStrmKit】对账附属文件同步失败：{pan_path}：{err}")
                except U115Error as err:
                    result["failed"] += 1
                    logger.warning(f"【CloudStrmKit】对账补漏失败：{pan_path}：{err}")
                except Exception as err:  # noqa: BLE001
                    result["failed"] += 1
                    logger.error(f"【CloudStrmKit】对账补漏异常：{pan_path}：{err}")

            # 2. 清理孤儿（仅限媒体库根内、delete_sync 开启，且扫描必须成功且有结果）
            if self._config.delete_sync and scan_ok and result["scanned"] > 0:
                for entry in self._index.all_items():
                    if not self._within_roots(entry.pan_path):
                        continue
                    if entry.pan_path in pan_paths:
                        continue
                    try:
                        remove_strm(Path(entry.strm_path))
                        self._index.remove(entry.id)
                        result["removed"] += 1
                        refresh_dirs.append(Path(entry.strm_path).parent)
                    except Exception as err:  # noqa: BLE001
                        logger.warning(f"【CloudStrmKit】清理孤儿失败：{entry.strm_path}：{err}")

            # 3. 飞牛刷新（去重）
            if self._fnos is not None:
                seen = set()
                for directory in refresh_dirs:
                    fnos_dir = PathMapper.to_fnos_path(
                        directory,
                        self._config.strm_root_local,
                        self._config.strm_root_fnos,
                    )
                    if fnos_dir is None or str(fnos_dir) in seen:
                        continue
                    seen.add(str(fnos_dir))
                    item = self._fnos.build_item(None, None, None, None, fnos_dir)
                    if item is not None:
                        self._fnos.queue(item)
                        result["refreshed"] += 1

            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            logger.info(
                f"【CloudStrmKit】对账完成：扫描 {result['scanned']}，补漏 {result['generated']}，"
                f"清理 {result['removed']}，失败 {result['failed']}，刷新 {result['refreshed']}"
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
