"""
302 播放服务：验签 → 查索引 → 用请求方 UA 换 115 直链 → 返回 302。

关键约束（T3 实测）：115 直链与换链时的 User-Agent 严格绑定，
因此必须原样使用播放请求的 UA 换链，客户端 302 后沿用同一 UA 才能取流。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from app.sdk.logging import logger
from fastapi import Request
from fastapi.responses import Response

from .index import IndexEntry, StrmIndex
from .strm import StrmContentBuilder
from .u115 import U115AuthError, U115Client, U115Error, U115RateLimited

# 播放日志保留条数
PLAY_LOG_LIMIT = 200


class PlayService:
    """播放请求处理服务。"""

    def __init__(
        self,
        index: StrmIndex,
        builder: StrmContentBuilder,
        u115: U115Client,
        debug: bool = False,
        notifier: Optional[Any] = None,
    ) -> None:
        """
        初始化播放服务。

        :param index: STRM 索引
        :param builder: STRM 内容构建器（验签用）
        :param u115: 115 客户端
        :param debug: 是否输出调试日志
        :param notifier: 通知器（播放失败告警）
        """
        self._index = index
        self._builder = builder
        self._u115 = u115
        self._debug = debug
        self._notifier = notifier
        self._logs: "deque[Dict[str, Any]]" = deque(maxlen=PLAY_LOG_LIMIT)
        self._log_lock = threading.Lock()
        self._log_dedupe: Dict[str, float] = {}

    def handle(self, request: Request, entry_id: str, expires_at: int, signature: str) -> Response:
        """
        处理播放请求。

        :param request: FastAPI 请求
        :param entry_id: 索引 ID
        :param expires_at: 过期时间戳
        :param signature: 签名
        :return: 302 或错误响应
        """
        started = time.monotonic()
        user_agent = request.headers.get("user-agent") or ""
        client_ip = self._client_ip(request)

        if not entry_id or not signature:
            return self._reject(400, "缺少参数", entry_id, user_agent, client_ip, started)
        if not self._builder.verify(entry_id, expires_at, signature):
            return self._reject(403, "签名不合法", entry_id, user_agent, client_ip, started)
        if int(expires_at) < int(time.time()):
            return self._reject(403, "链接已过期", entry_id, user_agent, client_ip, started)

        entry = self._index.get(entry_id)
        if entry is None:
            return self._reject(404, "索引不存在", entry_id, user_agent, client_ip, started)

        pickcode = entry.pickcode
        if not pickcode:
            # 对账批量生成时可能缺少 pickcode，播放时按路径补查并回填
            try:
                pickcode = self._u115.get_pickcode(entry.pan_path)
            except U115Error as err:
                return self._reject(
                    502,
                    f"查询 115 文件失败：{err}",
                    entry_id,
                    user_agent,
                    client_ip,
                    started,
                )
            if pickcode:
                entry.pickcode = pickcode
                self._index.upsert(entry)

        try:
            url = self._u115.get_download_url(pickcode, user_agent)
        except U115AuthError as err:
            return self._reject(502, f"115 授权失败：{err}", entry_id, user_agent, client_ip, started)
        except U115RateLimited as err:
            return self._reject(
                503, f"115 限频：{err}", entry_id, user_agent, client_ip, started, retry_after=30
            )
        except U115Error as err:
            return self._reject(502, f"115 换链失败：{err}", entry_id, user_agent, client_ip, started)

        filename = Path(entry.pan_path).name
        headers = {
            "Location": url,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        }
        # 播放日志去重：同一片 24 小时内只记录一次（避免播放器频繁请求刷屏）
        dedupe_key = f"{entry.pan_path}|{client_ip}"
        now = time.time()
        should_record = False
        with self._log_lock:
            last = self._log_dedupe.get(dedupe_key)
            if last is None or now - last >= 86400:
                self._log_dedupe[dedupe_key] = now
                should_record = True
                # 清理超过 24 小时的去重键，避免无限增长
                expired = [key for key, ts in self._log_dedupe.items() if now - ts >= 86400]
                for key in expired:
                    self._log_dedupe.pop(key, None)
        if should_record:
            self._record(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "entry_id": entry_id,
                    "ip": client_ip,
                    "ua": user_agent[:120],
                    "pan_path": entry.pan_path,
                    "status": 302,
                    "cached": self._u115.last_cached,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                }
            )
        if self._debug:
            logger.info(f"【CloudStrmKit】302：{filename} UA={user_agent[:60]}")
        return Response(status_code=302, headers=headers)

    @staticmethod
    def _client_ip(request: Request) -> str:
        """
        获取客户端 IP（兼容反代头）。

        :param request: 请求对象
        :return: IP 字符串
        """
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def _reject(
        self,
        status: int,
        message: str,
        entry_id: str,
        user_agent: str,
        client_ip: str,
        started: float,
        retry_after: Optional[int] = None,
    ) -> Response:
        """
        返回错误响应并记录日志。

        :param status: HTTP 状态码
        :param message: 错误描述
        :param entry_id: 索引 ID
        :param user_agent: 请求 UA
        :param client_ip: 客户端 IP
        :param started: 开始时间
        :param retry_after: Retry-After 头
        :return: 响应
        """
        self._record(
            {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_id": entry_id,
                "ip": client_ip,
                "ua": (user_agent or "")[:120],
                "status": status,
                "message": message,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            }
        )
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        body = json.dumps({"success": False, "message": message}, ensure_ascii=False)
        if status in (502, 503) and self._notifier is not None:
            self._notifier.send(
                "CloudStrmKit 播放失败",
                f"播放请求失败（{status}）：{message}",
                dedupe_key=f"play_fail_{status}",
                cooldown=600,
            )
        return Response(status_code=status, content=body, headers=headers)

    def _record(self, item: Dict[str, Any]) -> None:
        """
        记录一条播放日志。

        :param item: 日志条目
        """
        self._logs.append(item)

    def recent_logs(self, limit: int = 50) -> list:
        """
        返回最近的播放日志（新到旧）。

        :param limit: 最大条数
        :return: 日志列表
        """
        return list(reversed(list(self._logs)))[: max(1, limit)]
