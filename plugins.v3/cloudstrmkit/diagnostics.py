"""
T3 真机验证用诊断模块。

提供两项能力（后续阶段可保留为运维诊断）：
1. 整理完成事件载荷记录：把 TransferComplete 的关键字段留存，确认 target_item/storage/pickcode 是否可用。
2. 115 直链探针：按 115 路径查 pickcode，用多个 User-Agent 换链并探测直链可访问性，验证 UA 绑定假设。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.sdk.logging import logger

# 事件记录保留条数
EVENT_LOG_LIMIT = 50


class DiagEventLog:
    """整理完成事件载荷的环形记录（JSON 持久化）。"""

    def __init__(self, data_dir: Path) -> None:
        """
        初始化事件记录器并加载历史。

        :param data_dir: 插件数据目录
        """
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "diag_events.json"
        self._items: deque = deque(maxlen=EVENT_LOG_LIMIT)
        self._load()

    def _load(self) -> None:
        """从磁盘加载历史事件记录。"""
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            for item in payload.get("items") or []:
                if isinstance(item, dict):
                    self._items.append(item)
        except Exception as err:  # noqa: BLE001 - 诊断数据损坏不影响主流程
            logger.warning(f"【CloudStrmKit】诊断事件记录加载失败：{err}")

    def _save(self) -> None:
        """把事件记录写回磁盘。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as fp:
                json.dump({"items": list(self._items)}, fp, ensure_ascii=False, indent=1)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"【CloudStrmKit】诊断事件记录保存失败：{err}")

    def record(self, payload: Dict[str, Any]) -> None:
        """
        记录一条事件载荷。

        :param payload: 事件关键字段字典
        """
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **payload}
        self._items.append(record)
        self._save()

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        返回最近的记录（新到旧）。

        :param limit: 最大条数
        :return: 记录列表
        """
        return list(reversed(list(self._items)))[: max(1, limit)]

    def clear(self) -> None:
        """清空事件记录。"""
        self._items.clear()
        self._save()


def extract_transfer_fields(event_data: Any) -> Dict[str, Any]:
    """
    从 TransferComplete 事件载荷中提取关键字段（只做只读访问，字段缺失不报错）。

    :param event_data: 事件载荷对象
    :return: 关键字段字典
    """
    result: Dict[str, Any] = {}
    transferinfo = getattr(event_data, "transferinfo", None)
    if transferinfo is not None:
        result["success"] = bool(getattr(transferinfo, "success", False))
        result["transfer_type"] = getattr(transferinfo, "transfer_type", None)
        result["target_storage"] = _safe_str(getattr(transferinfo, "target_storage", None))
        result["file_list_new"] = list(getattr(transferinfo, "file_list_new", None) or [])[:20]
        result["file_count"] = getattr(transferinfo, "file_count", None)
        target_item = getattr(transferinfo, "target_item", None)
        target_diritem = getattr(transferinfo, "target_diritem", None)
        for key, item in (("target_item", target_item), ("target_diritem", target_diritem)):
            if item is None:
                result[key] = None
                continue
            result[key] = {
                "storage": _safe_str(getattr(item, "storage", None)),
                "path": _safe_str(getattr(item, "path", None)),
                "name": _safe_str(getattr(item, "name", None)),
                "size": getattr(item, "size", None),
                "pickcode": _safe_str(getattr(item, "pickcode", None)),
                "type": _safe_str(getattr(item, "type", None)),
            }
    else:
        result["transferinfo"] = None

    fileitem = getattr(event_data, "fileitem", None)
    if fileitem is not None:
        result["source_item"] = {
            "storage": _safe_str(getattr(fileitem, "storage", None)),
            "path": _safe_str(getattr(fileitem, "path", None)),
        }
    mediainfo = getattr(event_data, "mediainfo", None)
    if mediainfo is not None:
        result["media"] = {
            "title": _safe_str(getattr(mediainfo, "title", None)),
            "year": _safe_str(getattr(mediainfo, "year", None)),
            "type": _safe_str(getattr(mediainfo, "type", None)),
        }
    return result


def _safe_str(value: Any) -> Optional[str]:
    """
    把值转为字符串（None 保持 None）。

    :param value: 原始值
    :return: 字符串或 None
    """
    if value is None:
        return None
    return str(value)


def probe_u115_download(
    pan_path: str,
    user_agents: List[str],
    proxy: Optional[str] = None,
) -> Dict[str, Any]:
    """
    按 115 路径查 pickcode，遍历多个 UA 换链并探测直链可访问性。

    :param pan_path: 115 文件完整路径
    :param user_agents: 待测试的 User-Agent 列表
    :param proxy: 请求代理（如 http://127.0.0.1:7890）；为空时直连
    :return: 探测结果字典
    """
    result: Dict[str, Any] = {"ok": False, "path": pan_path, "results": []}
    try:
        from app.modules.filemanager.storages.u115 import U115Pan

        pan = U115Pan()
        item = pan.get_item(Path(pan_path))
        if not item or not getattr(item, "pickcode", None):
            result["error"] = "未找到文件或 pickcode（请确认路径与 115 存储授权）"
            return result
        pickcode = str(item.pickcode)
        result["pickcode"] = pickcode
        try:
            result["has_token"] = bool(getattr(pan, "access_token", None))
        except Exception as err:  # noqa: BLE001
            result["has_token"] = f"ERR:{err}"
    except Exception as err:  # noqa: BLE001 - 诊断接口需要把错误回给调用方
        result["error"] = f"初始化 115 存储失败：{err}"
        return result

    # 直连测试：绕过宿主 RequestUtils（及其代理配置），确认是网络层还是参数层问题
    try:
        result["direct"] = _request_115_direct(pan, pickcode)
    except Exception as err:  # noqa: BLE001
        result["direct"] = {"error": f"{err}"}

    # UA 绑定验证：每个 UA 独立换链，再用同 UA / 异 UA 请求同一直链
    result["direct_ua_tests"] = []
    for ua in user_agents:
        chain_ua = ua or "W115Storage/2.0"
        entry: Dict[str, Any] = {"ua": ua, "chain_ua": chain_ua}
        try:
            chained = _request_115_direct(pan, pickcode, chain_ua)
            entry["chain_code"] = chained.get("code")
            entry["chain_status"] = chained.get("status")
            url = chained.get("url")
            if url:
                entry["same_ua_status"] = _probe_url(url, chain_ua)
                other_ua = next(
                    (item or "W115Storage/2.0" for item in user_agents if (item or "W115Storage/2.0") != chain_ua),
                    "Mozilla/5.0",
                )
                entry["cross_ua"] = other_ua
                entry["cross_status"] = _probe_url(url, other_ua)
            else:
                entry["error"] = chained.get("message") or chained.get("body") or "换链失败"
        except Exception as err:  # noqa: BLE001
            entry["error"] = f"{err}"
        result["direct_ua_tests"].append(entry)

    for ua in user_agents:
        entry: Dict[str, Any] = {"ua": ua, "url": None, "http_status": None}
        try:
            headers = {"User-Agent": ua} if ua else {}
            data = pan._request_api(  # noqa: SLF001 - 复用宿主 115 请求引擎（含限流/重试）
                "POST",
                "/open/ufile/downurl",
                "data",
                data={"pick_code": pickcode},
                headers=headers,
            )
            if data:
                entry["url"] = _extract_first_url(data)
            if not entry["url"]:
                entry["error"] = "换链返回为空"
        except Exception as err:  # noqa: BLE001
            entry["error"] = f"换链失败：{err}"

        if entry["url"]:
            entry["http_status"] = _probe_url(entry["url"], ua, proxy)
            # 交叉验证：用另一个 UA 请求同一个直链，判断是否 UA 绑定
            other_ua = next((u for u in user_agents if u != ua), None)
            if other_ua:
                entry["cross_ua"] = other_ua
                entry["cross_status"] = _probe_url(entry["url"], other_ua, proxy)
        result["results"].append(entry)

    result["ok"] = True
    return result


def _request_115_direct(
    pan: Any, pickcode: str, user_agent: str = "W115Storage/2.0"
) -> Dict[str, Any]:
    """
    绕过宿主 RequestUtils（及其代理配置），用 token 直连 115 开放平台 downurl。

    用于区分“代理/网络层失败”与“参数/授权失败”，并支持指定换链 UA。

    :param pan: 115 存储实例
    :param pickcode: 文件提取码
    :param user_agent: 换链请求使用的 UA（直链会绑定该 UA）
    :return: 状态码、业务码、直链
    """
    token = getattr(pan, "access_token", None)
    if not token:
        return {"error": "无 access_token"}
    url = "https://proapi.115.com/open/ufile/downurl"
    data = urllib.parse.urlencode({"pick_code": pickcode}).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": user_agent or "W115Storage/2.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed: Dict[str, Any] = {"status": resp.status, "code": None, "message": "", "url": None}
            try:
                payload = json.loads(raw)
                parsed["code"] = payload.get("code")
                parsed["message"] = payload.get("message")
                first = next(iter((payload.get("data") or {}).values()), None)
                if isinstance(first, dict):
                    parsed["url"] = (first.get("url") or {}).get("url")
            except Exception:  # noqa: BLE001 - 非 JSON 响应只回摘要
                parsed["body"] = raw[:300]
            return parsed
    except urllib.error.HTTPError as err:
        body = err.read(300).decode("utf-8", errors="replace") if hasattr(err, "read") else ""
        return {"status": err.code, "body": body}
    except Exception as err:  # noqa: BLE001
        return {"error": f"{err}"}


def _extract_first_url(download_info: Dict[str, Any]) -> Optional[str]:
    """
    从 115 downurl 响应中提取第一个文件直链。

    :param download_info: 115 响应 data 字段
    :return: 直链或 None
    """
    try:
        first = next(iter(download_info.values()))
        return first.get("url", {}).get("url")
    except Exception:  # noqa: BLE001
        return None


def _probe_url(url: str, user_agent: str, proxy: Optional[str] = None) -> Any:
    """
    用 Range 请求探测直链可访问性，禁用环境代理（除非显式指定）。

    :param url: 直链
    :param user_agent: 请求 UA
    :param proxy: 代理地址
    :return: HTTP 状态码或错误字符串
    """
    headers = {"Range": "bytes=0-1"}
    if user_agent:
        headers["User-Agent"] = user_agent
    request = urllib.request.Request(url, headers=headers, method="GET")
    handlers: List[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as err:
        return err.code
    except Exception as err:  # noqa: BLE001 - 网络错误直接回传给诊断结果
        return f"ERR:{err}"
