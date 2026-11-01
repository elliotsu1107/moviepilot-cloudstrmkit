"""
飞牛影视媒体库刷新封装。

职责：
- 从 MoviePilot 媒体服务器实例中按名称定位飞牛（trimemedia）实例；
- 按目标路径调用 `refresh_library_by_items` 刷新对应媒体库；
- 失败时降级为全库扫描；
- 用去抖定时器合并同一批整理产生的多次刷新请求。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.sdk.logging import logger

try:  # MoviePilot v3 稳定路径；导入失败时退化为鸭子类型调用
    from app.schemas.mediaserver import RefreshMediaItem as _RefreshMediaItem
except Exception:  # noqa: BLE001
    _RefreshMediaItem = None  # type: ignore[assignment]

# 飞牛媒体服务器模块类型
FNOS_SERVICE_TYPE = "trimemedia"
# 去抖合并窗口（秒）
DEFAULT_DEBOUNCE_SECONDS = 3.0


class FnosRefresher:
    """飞牛媒体库刷新器（带去抖合并与降级）。"""

    def __init__(
        self,
        instance_name: str,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        """
        初始化刷新器。

        :param instance_name: MoviePilot 中飞牛媒体服务器实例名
        :param debounce_seconds: 去抖窗口（秒）
        """
        self._instance_name = (instance_name or "").strip()
        self._debounce = max(0.5, float(debounce_seconds))
        self._lock = threading.Lock()
        self._pending: Dict[str, Any] = {}
        self._timer: Optional[threading.Timer] = None

    @property
    def instance_name(self) -> str:
        """
        返回实例名。

        :return: 实例名
        """
        return self._instance_name

    def queue(self, item: Any) -> None:
        """
        把一个刷新请求加入去抖队列。

        :param item: RefreshMediaItem（或等价对象）
        """
        if item is None:
            return
        target_path = getattr(item, "target_path", None)
        key = str(target_path) if target_path else f"item-{id(item)}"
        with self._lock:
            self._pending[key] = item
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        """去抖计时器回调：合并后的请求一次性刷新。"""
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
            self._timer = None
        if not items:
            return
        self.refresh(items)

    def _get_instance(self) -> Optional[Any]:
        """
        获取飞牛媒体服务器实例。

        :return: 实例或 None
        """
        if not self._instance_name:
            logger.warning("【CloudStrmKit】未配置飞牛媒体服务器实例名，跳过刷新")
            return None
        try:
            from app.sdk.services import MediaServerHelper

            helper = MediaServerHelper()
            service = helper.get_service(self._instance_name, type_filter=FNOS_SERVICE_TYPE)
        except Exception as err:  # noqa: BLE001
            logger.error(f"【CloudStrmKit】获取飞牛媒体服务器实例失败：{err}")
            return None
        if not service or not getattr(service, "instance", None):
            logger.warning(
                f"【CloudStrmKit】未找到飞牛媒体服务器实例：{self._instance_name}"
                f"（类型需为 {FNOS_SERVICE_TYPE}）"
            )
            return None
        return service.instance

    def refresh(self, items: List[Any]) -> bool:
        """
        刷新指定目标路径对应的媒体库，失败时降级全库扫描。

        :param items: 刷新项列表
        :return: 是否成功提交刷新
        """
        instance = self._get_instance()
        if instance is None:
            return False

        ok = False
        if hasattr(instance, "refresh_library_by_items"):
            try:
                ok = bool(instance.refresh_library_by_items(items))
            except Exception as err:  # noqa: BLE001 - 单次刷新失败不阻断
                logger.error(f"【CloudStrmKit】飞牛按路径刷新失败：{err}")
                ok = False
        else:
            logger.warning("【CloudStrmKit】飞牛实例不支持 refresh_library_by_items")

        if ok:
            logger.info(f"【CloudStrmKit】飞牛媒体库刷新已提交（{len(items)} 项）")
            return True

        # 降级：全库扫描
        if hasattr(instance, "refresh_root_library"):
            try:
                fallback = instance.refresh_root_library()
                if fallback:
                    logger.info("【CloudStrmKit】已降级为飞牛全库扫描")
                    return True
                logger.warning("【CloudStrmKit】飞牛全库扫描失败（请确认使用管理员账号）")
            except Exception as err:  # noqa: BLE001
                logger.error(f"【CloudStrmKit】飞牛全库扫描异常：{err}")
        return False

    def build_item(
        self,
        title: Optional[str],
        year: Optional[str],
        media_type: Any,
        category: Optional[str],
        target_path: Path,
    ) -> Any:
        """
        构造 RefreshMediaItem；宿主类型不可用时返回 None。

        :param title: 标题
        :param year: 年份
        :param media_type: 媒体类型
        :param category: 分类
        :param target_path: 飞牛视角目标路径
        :return: RefreshMediaItem 或 None
        """
        if _RefreshMediaItem is None:
            logger.warning("【CloudStrmKit】当前 MoviePilot 版本缺少 RefreshMediaItem，跳过刷新")
            return None
        try:
            return _RefreshMediaItem(
                title=title,
                year=year,
                type=media_type,
                category=category,
                target_path=target_path,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(f"【CloudStrmKit】构造刷新项失败：{err}")
            return None

    def stop(self) -> None:
        """停止去抖计时器。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending.clear()
