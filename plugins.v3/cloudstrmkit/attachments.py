"""
附属文件同步：把 115 同目录的白名单附属文件（NFO/海报/字幕）复制到本地 STRM 目录。

规则（见 PRD F8）：
- 仅同步配置 `copy_exts` 白名单内的扩展名；
- 仅同步与该视频相关的文件：同名前缀（`<视频名>.nfo`、`<视频名>.zh.srt`）或目录级
  通用海报（`poster.jpg`、`fanart.jpg`、`banner.jpg`、`logo.png` 等）；
- 单文件大小不超过 `attachment_max_mb`；
- 冲突策略 `skip`（默认）或 `overwrite`；
- 同步结果写入索引（目录级附件记录），供覆盖整理时清理。
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from app.sdk.logging import logger

from .config import CloudStrmKitConfig
from .index import AttachmentEntry, IndexEntry, StrmIndex
from .u115 import U115Client, U115Error

# 换链与下载统一使用的 UA（直链绑定该 UA）
DOWNLOAD_USER_AGENT = "W115Storage/2.0"

# 目录级通用图片名（不含扩展名）
COMMON_NAMES: Set[str] = {
    "poster",
    "fanart",
    "banner",
    "logo",
    "thumb",
    "clearart",
    "clearlogo",
    "backdrop",
    "backdrops",
    "folder",
    "cover",
}


def _is_common_attachment(name: str) -> bool:
    """
    判断是否为目录级通用附属文件（如 poster.jpg）。

    :param name: 文件名
    :return: 是否通用附属文件
    """
    stem = Path(name).stem.lower()
    return stem in COMMON_NAMES


def _is_related_to_video(name: str, video_stem: str) -> bool:
    """
    判断附属文件是否与视频相关（同名前缀或目录级通用名）。

    :param name: 附属文件名
    :param video_stem: 视频文件名（不含扩展名）
    :return: 是否相关
    """
    if _is_common_attachment(name):
        return True
    attachment_stem = Path(name).stem
    if attachment_stem == video_stem:
        return True
    # 同名前缀：<视频名>.zh.srt、<视频名>.nfo 等
    return attachment_stem.startswith(video_stem + ".") or attachment_stem.startswith(video_stem + " ")


class AttachmentSyncer:
    """附属文件同步器。"""

    def __init__(
        self,
        config: CloudStrmKitConfig,
        u115: U115Client,
        index: StrmIndex,
    ) -> None:
        """
        初始化同步器。

        :param config: 插件配置
        :param u115: 115 客户端
        :param index: STRM 索引
        """
        self._config = config
        self._u115 = u115
        self._index = index
        self._dir_cache: Dict[str, List[Any]] = {}

    def clear_cache(self) -> None:
        """清空目录列举缓存（对账前调用，避免使用过期目录数据）。"""
        self._dir_cache.clear()

    def _list_pan_dir(self, pan_dir: str) -> Optional[List[Any]]:
        """
        列出 115 目录下的文件项。

        :param pan_dir: 115 目录路径
        :return: FileItem 列表或 None（失败）
        """
        if pan_dir in self._dir_cache:
            return self._dir_cache[pan_dir]
        try:
            from app.modules.filemanager.storages.u115 import U115Pan

            pan = U115Pan()
            folder = pan.get_item(Path(pan_dir))
            if folder is None:
                logger.warning(f"【CloudStrmKit】附属文件目录不存在：{pan_dir}")
                return None
            items = pan.list(folder)
        except Exception as err:  # noqa: BLE001 - 目录列举失败不阻断主流程
            logger.warning(f"【CloudStrmKit】列举 115 目录失败：{pan_dir}：{err}")
            return None
        result = list(items or [])
        self._dir_cache[pan_dir] = result
        return result

    def _download(self, pickcode: str, target: Path) -> bool:
        """
        把 115 文件下载到本地（原子写入，禁用环境代理）。

        :param pickcode: 文件提取码
        :param target: 本地目标路径
        :return: 是否成功
        """
        try:
            url = self._u115.get_download_url(pickcode, DOWNLOAD_USER_AGENT)
        except U115Error as err:
            logger.warning(f"【CloudStrmKit】附属文件换链失败：{target.name}：{err}")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(target.name + ".tmp")
        request = urllib.request.Request(
            url, headers={"User-Agent": DOWNLOAD_USER_AGENT}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=60) as response, tmp_path.open("wb") as fp:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, target)
            return True
        except urllib.error.HTTPError as err:
            logger.warning(f"【CloudStrmKit】附属文件下载失败（HTTP {err.code}）：{target.name}")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"【CloudStrmKit】附属文件下载异常：{target.name}：{err}")
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:  # noqa: BLE001
                pass
        return False

    def sync_for_video(
        self,
        pan_video_path: str,
        local_video_path: Path,
        entry: Optional[IndexEntry] = None,
    ) -> List[AttachmentEntry]:
        """
        同步某个视频同目录的附属文件到本地 STRM 目录。

        :param pan_video_path: 115 视频路径
        :param local_video_path: 本地视频路径（.mkv 等，用于定位 STRM 目录与同名前缀）
        :param entry: 对应索引条目（可选，用于记录）
        :return: 同步成功的附属文件记录
        """
        if not self._config.copy_ext_set:
            return []
        pan_dir = str(Path(pan_video_path).parent)
        local_dir = local_video_path.parent
        video_stem = Path(pan_video_path).stem
        items = self._list_pan_dir(pan_dir)
        if items is None:
            return []

        synced: List[AttachmentEntry] = []
        for item in items:
            name = str(getattr(item, "name", "") or "")
            if not name:
                continue
            ext = Path(name).suffix.lstrip(".").lower()
            if ext not in self._config.copy_ext_set:
                continue
            if not _is_related_to_video(name, video_stem):
                continue
            size = int(getattr(item, "size", 0) or 0)
            if size > self._config.attachment_max_bytes:
                logger.warning(
                    f"【CloudStrmKit】附属文件超过大小上限，跳过：{name}"
                    f"（{size} > {self._config.attachment_max_bytes}）"
                )
                continue
            pickcode = getattr(item, "pickcode", None)
            if not pickcode:
                logger.warning(f"【CloudStrmKit】附属文件缺少 pickcode，跳过：{name}")
                continue
            target = local_dir / name
            record = AttachmentEntry(
                pan_path=str(Path(pan_dir) / name),
                local_path=str(target),
                size=size,
                source="plugin",
            )
            if target.exists():
                if self._config.attachment_conflict == "skip":
                    synced.append(record)
                    continue
                # overwrite：继续下载覆盖
            if self._download(str(pickcode), target):
                synced.append(record)

        if synced:
            self._index.set_dir_attachments(pan_dir, synced)
            logger.info(f"【CloudStrmKit】附属文件同步 {len(synced)} 个：{local_dir}")
        if entry is not None:
            entry.attachments = synced
        return synced

    def _has_related_attachments(self, local_dir: Path, video_stem: str) -> bool:
        """
        判断本地目录是否已有该视频相关的附属文件。

        :param local_dir: 本地 STRM 目录
        :param video_stem: 视频文件名（不含扩展名）
        :return: 是否已有附件
        """
        try:
            for path in local_dir.iterdir():
                if not path.is_file():
                    continue
                if path.suffix.lstrip(".").lower() not in self._config.copy_ext_set:
                    continue
                if _is_related_to_video(path.name, video_stem):
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    def backfill(self, limit: int = 50, interval: float = 0.5) -> Dict[str, Any]:
        """
        补全缺失附件：遍历索引，对本地缺少相关附件的条目逐个从 115 同步。

        :param limit: 本次最多处理的条目数
        :param interval: 每处理一个条目的间隔秒数（节流，防 115 风控）
        :return: 统计结果
        """
        logger.info(f"【CloudStrmKit】附件补全开始：limit={limit}，索引条目={len(self._index)}")
        processed = 0
        synced_videos = 0
        synced_files = 0
        skipped = 0
        failed = 0
        for entry in self._index.all_items():
            if processed >= limit:
                break
            local_dir = Path(entry.strm_path).parent
            if not local_dir.exists():
                continue
            video_stem = Path(entry.pan_path).stem
            if self._has_related_attachments(local_dir, video_stem):
                skipped += 1
                continue
            processed += 1
            try:
                records = self.sync_for_video(
                    entry.pan_path,
                    local_dir / f"{video_stem}.mkv",
                    entry,
                )
                if records:
                    synced_videos += 1
                    synced_files += len(records)
            except Exception as err:  # noqa: BLE001
                failed += 1
                logger.warning(f"【CloudStrmKit】附件补全失败：{entry.pan_path}：{err}")
            if processed % 10 == 0:
                logger.info(
                    f"【CloudStrmKit】附件补全进度：处理 {processed}，"
                    f"同步视频 {synced_videos}，文件 {synced_files}，跳过 {skipped}"
                )
            if interval > 0:
                time.sleep(interval)
        logger.info(
            f"【CloudStrmKit】附件补全完成：处理 {processed}，同步视频 {synced_videos}，"
            f"同步文件 {synced_files}，跳过 {skipped}，失败 {failed}"
        )
        return {
            "processed": processed,
            "synced_videos": synced_videos,
            "synced_files": synced_files,
            "skipped": skipped,
            "failed": failed,
        }

    def cleanup_for_video(self, pan_video_path: str, local_video_path: Path) -> int:
        """
        覆盖整理时清理旧附属文件（仅清理索引记录的、白名单扩展名的文件）。

        :param pan_video_path: 115 视频路径
        :param local_video_path: 本地视频路径
        :return: 清理文件数
        """
        pan_dir = str(Path(pan_video_path).parent)
        local_dir = local_video_path.parent
        video_stem = Path(pan_video_path).stem
        records = self._index.get_dir_attachments(pan_dir)
        removed = 0
        for record in records:
            target = Path(record.local_path)
            try:
                target.relative_to(local_dir)
            except ValueError:
                continue
            if not _is_related_to_video(target.name, video_stem):
                continue
            if target.suffix.lstrip(".").lower() not in self._config.copy_ext_set:
                continue
            try:
                if target.exists():
                    target.unlink()
                    removed += 1
            except Exception as err:  # noqa: BLE001
                logger.warning(f"【CloudStrmKit】清理旧附属文件失败：{target}：{err}")
        if removed:
            self._index.remove_dir_attachments(pan_dir)
        return removed

    def scan_orphans(self, strm_root: str) -> List[Dict[str, Any]]:
        """
        扫描本地 STRM 目录下没有对应 .strm 的附属文件（孤儿）。

        :param strm_root: 本地 STRM 根目录（容器内路径）
        :return: 孤儿文件列表（path/name/size）
        """
        root = Path(strm_root or "")
        if not strm_root or not root.is_dir():
            return []
        orphans: List[Dict[str, Any]] = []
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            ext = path.suffix.lstrip(".").lower()
            if ext not in self._config.copy_ext_set:
                continue
            if path.with_suffix(".strm").exists():
                continue
            if _is_common_attachment(path.name):
                # 目录级海报：目录内存在任意 .strm 时视为有效
                if any(path.parent.glob("*.strm")):
                    continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            orphans.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "size": size,
                }
            )
        return orphans

    def cleanup_orphans(self, strm_root: str, files: List[str]) -> Dict[str, Any]:
        """
        删除用户确认的孤儿附属文件（仅限 STRM 根目录内、白名单扩展名）。

        :param strm_root: 本地 STRM 根目录
        :param files: 待删除文件路径列表
        :return: 处理结果统计
        """
        root = Path(strm_root or "")
        if not strm_root or not root.is_dir():
            return {"removed": 0, "skipped": 0, "errors": ["STRM 根目录不存在"]}
        try:
            root_str = str(root.resolve()).rstrip("/")
        except OSError:
            root_str = str(root).rstrip("/")
        removed = 0
        skipped = 0
        errors: List[str] = []
        for raw in files or []:
            target = Path(raw)
            try:
                target_str = str(target.resolve())
            except OSError:
                target_str = str(target)
            if not (target_str == root_str or target_str.startswith(root_str + "/")):
                skipped += 1
                errors.append(f"不在 STRM 根目录内：{raw}")
                continue
            if target.suffix.lstrip(".").lower() not in self._config.copy_ext_set:
                skipped += 1
                errors.append(f"扩展名不在白名单：{raw}")
                continue
            try:
                if target.exists():
                    target.unlink()
                    removed += 1
                else:
                    skipped += 1
            except Exception as err:  # noqa: BLE001
                errors.append(f"{target}: {err}")
        return {"removed": removed, "skipped": skipped, "errors": errors}
