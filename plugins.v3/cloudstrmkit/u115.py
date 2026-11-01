"""
115 直链换取与文件查询封装。

设计要点（来自 T3 实测）：
- 直链与换链时的 User-Agent 严格绑定，因此必须用请求方 UA 换链；
- 宿主 `U115Pan._request_api` 在带全局代理的 MP 容器中换链失败，故本模块用
  `urllib` 直连 115 开放平台并显式禁用环境代理；
- token 仍从宿主 115 存储实例读取（由宿主负责刷新与持久化）；
- 直链按 `(pickcode, UA)` 做进程内 TTL 缓存，并做全局 QPS 限频与错误分类。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.sdk.logging import logger

# 115 开放平台地址
OPEN_API_BASE = "https://proapi.115.com"
# 获取下载地址接口
DOWNURL_PATH = "/open/ufile/downurl"
# 未提供 UA 时的兜底 UA
DEFAULT_USER_AGENT = "W115Storage/2.0"
# 请求超时（秒）
REQUEST_TIMEOUT = 20


class U115Error(Exception):
    """115 操作基础异常。"""


class U115AuthError(U115Error):
    """115 未授权、token 失效或刷新失败。"""


class U115RateLimited(U115Error):
    """115 限频或风控冷却。"""


class U115NotFound(U115Error):
    """文件不存在或无法获取下载地址。"""


class _TTLCache:
    """带 TTL 与容量上限的线程安全 LRU 缓存。"""

    def __init__(self, maxsize: int = 1024) -> None:
        """
        初始化缓存。

        :param maxsize: 最大条目数
        """
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = max(16, maxsize)
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        """
        读取缓存（过期自动删除）。

        :param key: 缓存键
        :return: 缓存值或 None
        """
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            expire_at, value = item
            if expire_at <= time.monotonic():
                self._data.pop(key, None)
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        """
        写入缓存。

        :param key: 缓存键
        :param value: 缓存值
        :param ttl: 有效期（秒）
        """
        with self._lock:
            self._data[key] = (time.monotonic() + max(1, ttl), value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> int:
        """
        清空缓存。

        :return: 清理条目数
        """
        with self._lock:
            count = len(self._data)
            self._data.clear()
            return count

    def stats(self) -> Dict[str, int]:
        """
        返回缓存统计。

        :return: 条目数与命中计数
        """
        with self._lock:
            return {
                "size": len(self._data),
                "hits": self._hits,
                "misses": self._misses,
            }


def _cache_key(pickcode: str, user_agent: str) -> str:
    """
    生成直链缓存键（pickcode + UA 摘要）。

    :param pickcode: 文件提取码
    :param user_agent: 换链 UA
    :return: 缓存键
    """
    ua_digest = hashlib.sha1(user_agent.encode("utf-8")).hexdigest()[:16]
    return f"{pickcode}|{ua_digest}"


def _extract_first_url(download_data: Dict[str, Any]) -> Optional[str]:
    """
    从 115 downurl 响应 data 中提取第一个文件的直链。

    :param download_data: 响应 data 字段
    :return: 直链或 None
    """
    try:
        first = next(iter(download_data.values()))
        if not isinstance(first, dict):
            return None
        return (first.get("url") or {}).get("url")
    except Exception:  # noqa: BLE001 - 结构异常按无直链处理
        return None


class U115Client:
    """115 直连客户端（token 来自宿主，请求绕过环境代理）。"""

    def __init__(
        self,
        cache_ttl: int = 1200,
        qps: float = 2.0,
        cache_maxsize: int = 1024,
    ) -> None:
        """
        初始化客户端。

        :param cache_ttl: 直链缓存有效期（秒）
        :param qps: 全局请求限频（每秒请求数）
        :param cache_maxsize: 直链缓存最大条目数
        """
        self._cache = _TTLCache(cache_maxsize)
        self._ttl = max(30, int(cache_ttl))
        self._qps = max(0.1, float(qps))
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0

    # ------------------------------------------------------------------
    # 宿主与 token
    # ------------------------------------------------------------------
    @staticmethod
    def _get_pan() -> Any:
        """
        获取宿主 115 存储实例（延迟导入，避免插件加载时依赖宿主内部模块）。

        :return: U115Pan 实例
        """
        from app.modules.filemanager.storages.u115 import U115Pan

        return U115Pan()

    def get_token(self) -> str:
        """
        读取宿主 115 访问令牌（刷新由宿主负责）。

        :return: access_token
        :raises U115AuthError: 未授权或刷新失败
        """
        try:
            token = getattr(self._get_pan(), "access_token", None)
        except Exception as err:  # noqa: BLE001
            raise U115AuthError(f"读取 115 授权失败：{err}") from err
        if not token:
            raise U115AuthError("115 未授权或 token 刷新失败，请检查 MoviePilot 的 115 存储配置")
        return str(token)

    # ------------------------------------------------------------------
    # 限频
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        """按配置 QPS 串行限频。"""
        with self._throttle_lock:
            now = time.monotonic()
            wait = self._last_request_at + (1.0 / self._qps) - now
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------
    # 换链
    # ------------------------------------------------------------------
    def _request_downurl(self, pickcode: str, user_agent: str) -> Dict[str, Any]:
        """
        直连 115 开放平台获取下载地址。

        :param pickcode: 文件提取码
        :param user_agent: 换链 UA（直链将绑定该 UA）
        :return: 响应 data 字段
        :raises U115AuthError: 授权失败
        :raises U115RateLimited: 限频/风控
        :raises U115Error: 其他失败
        """
        self._throttle()
        token = self.get_token()
        payload_data = urllib.parse.urlencode({"pick_code": pickcode}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": user_agent or DEFAULT_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        request = urllib.request.Request(
            OPEN_API_BASE + DOWNURL_PATH,
            data=payload_data,
            headers=headers,
            method="POST",
        )
        # 显式空 ProxyHandler：忽略 HTTP_PROXY/HTTPS_PROXY 环境变量
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise U115AuthError(f"115 授权失败（HTTP {err.code}）") from err
            if err.code == 429:
                raise U115RateLimited("115 限频（HTTP 429）") from err
            raise U115Error(f"115 接口返回 HTTP {err.code}") from err
        except Exception as err:  # noqa: BLE001 - 网络层统一包装
            raise U115Error(f"115 接口请求失败：{err}") from err

        try:
            payload = json.loads(raw)
        except Exception as err:  # noqa: BLE001
            raise U115Error(f"115 响应不是合法 JSON：{raw[:200]}") from err

        code = payload.get("code")
        if code != 0:
            message = str(payload.get("message") or "")
            if code in (401, 403):
                raise U115AuthError(f"115 授权失败（code={code} {message}）")
            if code == 429:
                raise U115RateLimited(f"115 限频（code={code} {message}）")
            raise U115Error(f"115 业务错误（code={code} {message}）")
        return payload.get("data") or {}

    def get_download_url(self, pickcode: str, user_agent: str) -> str:
        """
        获取 115 直链（带缓存）。

        :param pickcode: 文件提取码
        :param user_agent: 请求方 UA（直链绑定此 UA）
        :return: 直链
        :raises U115NotFound: 无法获取直链
        """
        ua = (user_agent or DEFAULT_USER_AGENT).strip()
        key = _cache_key(pickcode, ua)
        cached = self._cache.get(key)
        if cached:
            return str(cached)
        data = self._request_downurl(pickcode, ua)
        url = _extract_first_url(data)
        if not url:
            raise U115NotFound(f"115 未返回下载地址（pickcode={pickcode}）")
        self._cache.set(key, url, self._ttl)
        logger.debug(f"【CloudStrmKit】115 换链成功：{pickcode} UA={ua[:40]}")
        return url

    # ------------------------------------------------------------------
    # 文件查询
    # ------------------------------------------------------------------
    def get_pickcode(self, pan_path: str) -> str:
        """
        按 115 路径查询 pickcode。

        :param pan_path: 115 文件完整路径
        :return: pickcode
        :raises U115NotFound: 文件不存在
        :raises U115Error: 查询失败
        """
        try:
            pan = self._get_pan()
            item = pan.get_item(Path(pan_path))
        except Exception as err:  # noqa: BLE001
            raise U115Error(f"查询 115 文件失败：{err}") from err
        pickcode = getattr(item, "pickcode", None) if item else None
        if not pickcode:
            raise U115NotFound(f"未找到 115 文件：{pan_path}")
        return str(pickcode)

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def clear_cache(self) -> int:
        """
        清空直链缓存。

        :return: 清理条目数
        """
        return self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        """
        返回客户端统计信息。

        :return: 缓存与限频配置
        """
        return {
            "cache": self._cache.stats(),
            "ttl": self._ttl,
            "qps": self._qps,
            "base_url": OPEN_API_BASE,
        }
