"""
路径映射、STRM 内容生成与签名、文件原子写入/删除。

约定：
- 容器内写入路径由 `strm_root_local` 决定（默认 `/strm_test`）；
- 飞牛刷新使用 `strm_root_fnos` 换算为宿主视角路径；
- STRM 内容为带 HMAC 签名的绝对 URL，客户端访问插件 `/play` 换取 302。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

from app.sdk.logging import logger

# 播放接口路由模板（插件 ID 由调用方提供）
PLAY_ROUTE = "/api/v1/plugin/{plugin_id}/play"
# 签名默认有效期：365 天（播放器长期持有 STRM）
DEFAULT_EXPIRE_SECONDS = 365 * 24 * 3600


class PathMapper:
    """115 路径 → 容器内 STRM 路径映射（最长前缀优先）。"""

    def __init__(self, mappings_text: str) -> None:
        """
        初始化映射表。

        :param mappings_text: 多行文本，每行格式「115前缀 => 容器内根目录」
        """
        self._mappings: List[Tuple[str, str]] = []
        self._parse(mappings_text)

    def _parse(self, text: str) -> None:
        """
        解析映射配置。

        :param text: 多行映射文本
        """
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or "=>" not in line:
                continue
            left, right = line.split("=>", 1)
            left = left.strip().replace("\\", "/").rstrip("/")
            right = right.strip().replace("\\", "/").rstrip("/")
            if not left or not right:
                continue
            self._mappings.append((left, right))
        # 最长前缀优先，避免 /movie 覆盖 /movie/动画电影
        self._mappings.sort(key=lambda item: len(item[0]), reverse=True)

    @property
    def mappings(self) -> List[Tuple[str, str]]:
        """
        返回已解析的映射列表。

        :return: (115前缀, 容器内根) 列表
        """
        return list(self._mappings)

    def map(self, pan_path: str) -> Optional[Path]:
        """
        把 115 路径映射为容器内 STRM 路径（未替换扩展名）。

        :param pan_path: 115 文件路径
        :return: 容器内路径或 None（无匹配）
        """
        normalized = (pan_path or "").replace("\\", "/")
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        for prefix, root in self._mappings:
            if normalized == prefix or normalized.startswith(prefix + "/"):
                rest = normalized[len(prefix):].lstrip("/")
                if not rest:
                    return None
                return Path(root) / rest
        return None

    @staticmethod
    def to_fnos_path(
        local_path: Path,
        strm_root_local: str,
        strm_root_fnos: str,
    ) -> Optional[Path]:
        """
        把容器内路径换算为飞牛视角路径（用于媒体库刷新）。

        :param local_path: 容器内路径
        :param strm_root_local: 容器内 STRM 根
        :param strm_root_fnos: 飞牛视角 STRM 根
        :return: 飞牛视角路径或 None（无法换算）
        """
        local_root = (strm_root_local or "").replace("\\", "/").rstrip("/")
        fnos_root = (strm_root_fnos or "").replace("\\", "/").rstrip("/")
        if not local_root or not fnos_root:
            return None
        path_str = str(local_path).replace("\\", "/")
        if path_str == local_root:
            return Path(fnos_root)
        if path_str.startswith(local_root + "/"):
            return Path(fnos_root + path_str[len(local_root):])
        return None


class StrmContentBuilder:
    """STRM 内容构建：签名 URL 生成与校验。"""

    def __init__(
        self,
        base_url: str,
        secret: str,
        plugin_id: str,
        expire_seconds: int = DEFAULT_EXPIRE_SECONDS,
    ) -> None:
        """
        初始化构建器。

        :param base_url: 对外基础地址（无尾斜杠）
        :param secret: HMAC 签名密钥
        :param plugin_id: 插件 ID（拼路由用）
        :param expire_seconds: 默认有效期（秒）
        """
        self._base_url = (base_url or "").strip().rstrip("/")
        self._secret = secret or ""
        self._plugin_id = plugin_id
        self._expire = max(3600, int(expire_seconds))

    @property
    def base_url(self) -> str:
        """
        返回基础地址。

        :return: 基础地址
        """
        return self._base_url

    @property
    def expire_seconds(self) -> int:
        """
        返回默认有效期。

        :return: 秒数
        """
        return self._expire

    def sign(self, entry_id: str, expires_at: int) -> str:
        """
        计算签名。

        :param entry_id: 索引 ID
        :param expires_at: 过期时间戳
        :return: 十六进制签名
        """
        message = f"{entry_id}:{expires_at}".encode("utf-8")
        return hmac.new(
            self._secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()[:32]

    def verify(self, entry_id: str, expires_at: int, signature: str) -> bool:
        """
        校验签名。

        :param entry_id: 索引 ID
        :param expires_at: 过期时间戳
        :param signature: 请求签名
        :return: 是否合法
        """
        if not self._secret or not signature:
            return False
        return hmac.compare_digest(self.sign(entry_id, expires_at), signature)

    def build_url(self, entry_id: str, expires_at: Optional[int] = None) -> str:
        """
        构建 STRM 内容 URL。

        :param entry_id: 索引 ID
        :param expires_at: 指定过期时间戳；为空时按默认有效期生成
        :return: 绝对 URL
        """
        expires = expires_at or int(time.time()) + self._expire
        signature = self.sign(entry_id, expires)
        route = PLAY_ROUTE.format(plugin_id=self._plugin_id)
        return f"{self._base_url}{route}?f={entry_id}&e={expires}&sig={signature}"


def write_strm(strm_path: Path, content: str) -> None:
    """
    原子写入 STRM 文件（临时文件 + 重命名）。

    :param strm_path: 目标路径
    :param content: 文件内容
    """
    strm_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = strm_path.with_name(strm_path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        fp.write(content.strip() + "\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp_path, strm_path)


def remove_strm(strm_path: Path) -> bool:
    """
    删除 STRM 文件。

    :param strm_path: 文件路径
    :return: 是否删除成功（不存在也算成功）
    """
    try:
        strm_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except Exception as err:  # noqa: BLE001 - 删除失败只记录，不阻断流程
        logger.warning(f"【CloudStrmKit】删除 STRM 失败：{strm_path}：{err}")
        return False
