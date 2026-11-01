"""
MoviePilot 通知封装。

职责：用宿主已配置的通知服务发送插件事件通知（对账完成、播放失败等）。
未配置通知服务时静默降级为日志；同一类告警支持冷却去重，避免刷屏。
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from app.sdk.logging import logger


class Notifier:
    """通知发送器（基于宿主通知服务）。"""

    def __init__(self, enabled: bool = True) -> None:
        """
        初始化通知器。

        :param enabled: 是否启用通知
        """
        self._enabled = enabled
        self._last_sent: Dict[str, float] = {}
        self._last_error: Optional[str] = None

    @property
    def last_error(self) -> Optional[str]:
        """
        返回最近一次发送失败的原因。

        :return: 错误信息或 None
        """
        return self._last_error

    @property
    def enabled(self) -> bool:
        """
        返回通知开关状态。

        :return: 是否启用
        """
        return self._enabled

    def send(
        self,
        title: str,
        text: str,
        dedupe_key: Optional[str] = None,
        cooldown: int = 300,
    ) -> bool:
        """
        发送一条通知。

        :param title: 通知标题
        :param text: 通知正文
        :param dedupe_key: 去重键（同类告警冷却）
        :param cooldown: 冷却秒数
        :return: 是否成功发送
        """
        if not self._enabled:
            return False
        if dedupe_key:
            now = time.monotonic()
            if now - self._last_sent.get(dedupe_key, 0.0) < cooldown:
                return False
            self._last_sent[dedupe_key] = now
        try:
            from app.schemas.message import Message
            from app.sdk.services import NotificationHelper

            services = NotificationHelper().get_services()
            if not services:
                logger.debug("【CloudStrmKit】未配置通知服务，跳过通知")
                self._last_error = "未配置通知服务"
                return False
            service = next(iter(services.values()))
            # post_message 在通知模块类上（由模块按配置分发到各实例），优先用它
            module = getattr(service, "module", None)
            target = module if module is not None and hasattr(module, "post_message") else None
            if target is None:
                instance = getattr(service, "instance", None)
                target = instance if instance is not None and hasattr(instance, "post_message") else None
            if target is None:
                self._last_error = "通知服务不支持 post_message"
                return False
            target.post_message(Message(title=title, text=text))
            self._last_error = None
            return True
        except Exception as err:  # noqa: BLE001 - 通知失败不影响主流程
            self._last_error = f"{err}"
            logger.warning(f"【CloudStrmKit】发送通知失败：{err}")
            return False
