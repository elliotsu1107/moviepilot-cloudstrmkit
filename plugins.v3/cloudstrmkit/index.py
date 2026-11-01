"""
STRM 索引与附属文件索引。

职责：维护「115 文件路径 ↔ 本地 STRM 路径 ↔ pickcode ↔ 附属文件」的持久映射。
存储为插件数据目录下的 JSON 文件，写入使用临时文件 + 原子替换，进程内加锁。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.sdk.logging import logger

# 索引文件版本，结构变更时递增
INDEX_VERSION = 1
# 索引文件名
INDEX_FILENAME = "index.json"


@dataclass
class AttachmentEntry:
    """附属文件记录（由插件从 115 复制到本地）。"""

    pan_path: str
    local_path: str
    size: int = 0
    mtime: float = 0
    source: str = "plugin"


@dataclass
class IndexEntry:
    """单个 STRM 条目的索引记录。"""

    id: str
    pan_storage: str
    pan_path: str
    pickcode: str
    strm_path: str
    size: int = 0
    mtime: float = 0
    media_type: str = ""
    created_at: float = 0
    attachments: List[AttachmentEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为可 JSON 序列化的字典。

        :return: 字典
        """
        data = asdict(self)
        data["attachments"] = [asdict(item) for item in self.attachments]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IndexEntry":
        """
        从字典构造索引记录。

        :param data: 索引字典
        :return: 索引记录
        """
        raw_attachments = data.get("attachments") or []
        return cls(
            id=str(data.get("id") or ""),
            pan_storage=str(data.get("pan_storage") or ""),
            pan_path=str(data.get("pan_path") or ""),
            pickcode=str(data.get("pickcode") or ""),
            strm_path=str(data.get("strm_path") or ""),
            size=int(data.get("size") or 0),
            mtime=float(data.get("mtime") or 0),
            media_type=str(data.get("media_type") or ""),
            created_at=float(data.get("created_at") or 0),
            attachments=[
                AttachmentEntry(
                    pan_path=str(item.get("pan_path") or ""),
                    local_path=str(item.get("local_path") or ""),
                    size=int(item.get("size") or 0),
                    mtime=float(item.get("mtime") or 0),
                    source=str(item.get("source") or "plugin"),
                )
                for item in raw_attachments
                if isinstance(item, dict)
            ],
        )


class StrmIndex:
    """STRM 索引：线程安全的 JSON 持久化容器。"""

    def __init__(self, data_dir: Path) -> None:
        """
        初始化索引并从磁盘加载。

        :param data_dir: 插件数据目录
        """
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / INDEX_FILENAME
        self._lock = threading.RLock()
        self._items: Dict[str, IndexEntry] = {}
        self._dir_attachments: Dict[str, List[AttachmentEntry]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        """
        返回索引文件路径。

        :return: 路径
        """
        return self._path

    @staticmethod
    def make_id(pan_path: str) -> str:
        """
        根据 115 路径生成稳定索引 ID（8 位十六进制）。

        :param pan_path: 115 文件完整路径
        :return: 索引 ID
        """
        digest = hashlib.sha1((pan_path or "").encode("utf-8")).hexdigest()
        return digest[:8]

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> None:
        """
        从磁盘加载索引；文件缺失或损坏时初始化空索引。
        """
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
            items = payload.get("items") or {}
            self._items = {
                key: IndexEntry.from_dict(value)
                for key, value in items.items()
                if isinstance(value, dict)
            }
            raw_dirs = payload.get("dir_attachments") or {}
            self._dir_attachments = {
                key: [
                    AttachmentEntry(
                        pan_path=str(item.get("pan_path") or ""),
                        local_path=str(item.get("local_path") or ""),
                        size=int(item.get("size") or 0),
                        mtime=float(item.get("mtime") or 0),
                        source=str(item.get("source") or "plugin"),
                    )
                    for item in value
                    if isinstance(item, dict)
                ]
                for key, value in raw_dirs.items()
                if isinstance(value, list)
            }
            logger.info(f"【CloudStrmKit】索引加载完成：{len(self._items)} 条")
        except Exception as err:  # noqa: BLE001 - 索引损坏需要兜底而不是阻断插件
            backup = self._path.with_suffix(".json.bak")
            try:
                if self._path.exists():
                    os.replace(self._path, backup)
                logger.error(f"【CloudStrmKit】索引损坏，已备份为 {backup.name}：{err}")
            except Exception as backup_err:  # noqa: BLE001
                logger.error(f"【CloudStrmKit】索引备份失败：{backup_err}")
            self._items = {}
            self._dir_attachments = {}

    def _save_locked(self) -> None:
        """
        原子写入索引（调用方需持有锁）。
        """
        payload = {
            "version": INDEX_VERSION,
            "updated_at": time.time(),
            "items": {key: item.to_dict() for key, item in self._items.items()},
            "dir_attachments": {
                key: [asdict(item) for item in value]
                for key, value in self._dir_attachments.items()
            },
        }
        self._data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=1)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, self._path)

    def save(self) -> None:
        """
        线程安全地保存索引。
        """
        with self._lock:
            self._save_locked()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, entry_id: str) -> Optional[IndexEntry]:
        """
        按 ID 查询条目。

        :param entry_id: 索引 ID
        :return: 索引记录或 None
        """
        with self._lock:
            return self._items.get(entry_id)

    def get_by_pan_path(self, pan_path: str) -> Optional[IndexEntry]:
        """
        按 115 路径查询条目。

        :param pan_path: 115 文件路径
        :return: 索引记录或 None
        """
        with self._lock:
            for entry in self._items.values():
                if entry.pan_path == pan_path:
                    return entry
        return None

    def all_items(self) -> List[IndexEntry]:
        """
        返回全部索引条目的快照。

        :return: 条目列表
        """
        with self._lock:
            return list(self._items.values())

    def __iter__(self) -> Iterator[IndexEntry]:
        """
        迭代全部索引条目。

        :return: 迭代器
        """
        return iter(self.all_items())

    def __len__(self) -> int:
        """
        返回条目数量。

        :return: 数量
        """
        with self._lock:
            return len(self._items)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def upsert(self, entry: IndexEntry) -> IndexEntry:
        """
        新增或更新索引条目（按 115 路径去重）。

        :param entry: 索引记录
        :return: 实际写入的记录
        """
        with self._lock:
            existing = None
            for item in self._items.values():
                if item.pan_path == entry.pan_path:
                    existing = item
                    break
            if existing is not None:
                entry.id = existing.id
                entry.created_at = existing.created_at or entry.created_at
                self._items[entry.id] = entry
            else:
                if not entry.id:
                    entry.id = self.make_id(entry.pan_path)
                if not entry.created_at:
                    entry.created_at = time.time()
                self._items[entry.id] = entry
            self._save_locked()
            return entry

    def add_many(self, entries: List[IndexEntry]) -> int:
        """
        批量写入索引条目（一次落盘，避免逐条保存）。

        :param entries: 索引记录列表
        :return: 写入条数
        """
        with self._lock:
            now = time.time()
            for entry in entries:
                if not entry.id:
                    entry.id = self.make_id(entry.pan_path)
                if not entry.created_at:
                    entry.created_at = now
                self._items[entry.id] = entry
            if entries:
                self._save_locked()
            return len(entries)

    def remove(self, entry_id: str) -> Optional[IndexEntry]:
        """
        按 ID 删除索引条目。

        :param entry_id: 索引 ID
        :return: 被删除的记录或 None
        """
        with self._lock:
            entry = self._items.pop(entry_id, None)
            if entry is not None:
                self._save_locked()
            return entry

    def remove_by_pan_path(self, pan_path: str) -> Optional[IndexEntry]:
        """
        按 115 路径删除索引条目。

        :param pan_path: 115 文件路径
        :return: 被删除的记录或 None
        """
        with self._lock:
            target_id = None
            for item in self._items.values():
                if item.pan_path == pan_path:
                    target_id = item.id
                    break
            if target_id is None:
                return None
            entry = self._items.pop(target_id, None)
            self._save_locked()
            return entry

    # ------------------------------------------------------------------
    # 目录级附属文件
    # ------------------------------------------------------------------
    def get_dir_attachments(self, dir_path: str) -> List[AttachmentEntry]:
        """
        查询目录级附属文件记录。

        :param dir_path: 115 目录路径
        :return: 附属文件列表
        """
        with self._lock:
            return list(self._dir_attachments.get(dir_path) or [])

    def set_dir_attachments(self, dir_path: str, items: List[AttachmentEntry]) -> None:
        """
        写入目录级附属文件记录。

        :param dir_path: 115 目录路径
        :param items: 附属文件列表
        """
        with self._lock:
            if items:
                self._dir_attachments[dir_path] = items
            else:
                self._dir_attachments.pop(dir_path, None)
            self._save_locked()

    def remove_dir_attachments(self, dir_path: str) -> Optional[List[AttachmentEntry]]:
        """
        删除目录级附属文件记录。

        :param dir_path: 115 目录路径
        :return: 被删除的记录或 None
        """
        with self._lock:
            items = self._dir_attachments.pop(dir_path, None)
            if items is not None:
                self._save_locked()
            return items

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """
        返回索引统计信息（供详情页展示）。

        :return: 统计字典
        """
        with self._lock:
            total = len(self._items)
            missing_pickcode = sum(1 for item in self._items.values() if not item.pickcode)
            attachment_count = sum(len(item.attachments) for item in self._items.values())
            dir_attachment_count = sum(
                len(items) for items in self._dir_attachments.values()
            )
            return {
                "total": total,
                "missing_pickcode": missing_pickcode,
                "attachments": attachment_count,
                "dir_attachments": dir_attachment_count,
                "index_path": str(self._path),
            }
