"""
CloudStrmKit 插件配置模型。

职责：把插件配置字典解析为带默认值与校验的 Pydantic 模型，
供入口初始化、配置页默认值和后续各模块读取。
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field, field_validator

# 默认视频扩展名（小写、逗号分隔）
DEFAULT_VIDEO_EXTS = "mkv,mp4,ts,m2ts,avi,mov,wmv"
# 默认附属文件扩展名（NFO/海报/字幕）
DEFAULT_COPY_EXTS = "nfo,jpg,jpeg,png,webp,srt,ass,ssa,sub"
# 默认媒体类型
DEFAULT_MEDIA_TYPES = ["movie", "tv"]


class CloudStrmKitConfig(BaseModel):
    """插件全部配置项。"""

    # 基础开关
    enabled: bool = Field(default=False, description="启用插件")
    # 115 存储名称（MoviePilot 存储页显示的名称，如 115网盘）
    source_storage: str = Field(default="115网盘", description="115 存储名称")
    # 飞牛影视实例名（MoviePilot 媒体服务器配置中的名称）
    fnos_instance: str = Field(default="", description="飞牛媒体服务器实例名")
    # 对外基础地址（STRM 内容前缀，如 https://mp.example.com，无尾斜杠）
    base_url: str = Field(default="", description="对外基础地址")

    # STRM 路径
    strm_root_local: str = Field(default="/strm_test", description="容器内 STRM 根目录")
    strm_root_fnos: str = Field(default="", description="飞牛视角 STRM 根目录")
    # 路径映射：每行「115前缀 => 容器内根」
    path_mappings: str = Field(default="", description="115 路径到容器内 STRM 根的映射")

    # 扫描过滤
    video_exts: str = Field(default=DEFAULT_VIDEO_EXTS, description="视频扩展名")
    min_size_mb: int = Field(default=100, ge=0, description="最小文件大小（MB）")
    media_types: List[str] = Field(
        default_factory=lambda: list(DEFAULT_MEDIA_TYPES), description="处理的媒体类型"
    )
    library_roots: str = Field(default="", description="对账用的 115 媒体库根路径，多行")

    # 302 与限频
    cache_ttl: int = Field(
        default=7200, ge=30, description="直链缓存秒数（自动受直链有效期约束）"
    )
    qps: float = Field(default=2.0, gt=0, description="115 换链限频（每秒请求数）")
    cooldown_seconds: int = Field(default=60, ge=0, description="风控冷却秒数")
    secret: str = Field(default="", description="STRM 签名密钥")

    # 附属文件
    copy_exts: str = Field(default=DEFAULT_COPY_EXTS, description="附属文件扩展名白名单")
    attachment_max_mb: int = Field(default=10, ge=1, description="附属文件大小上限（MB）")
    attachment_conflict: str = Field(default="skip", description="附属文件冲突策略")

    # 对账与同步
    reconcile_cron: str = Field(default="30 4 * * *", description="对账 Cron 表达式")
    reconcile_mode: str = Field(
        default="history",
        description="对账模式：history 基于整理历史（快）/ scan 扫描 115 全库（慢）",
    )
    scan_cron: str = Field(
        default="", description="定期一致性扫描 Cron（scan 模式，留空关闭）"
    )
    reconcile_sync_attachments: bool = Field(
        default=False, description="对账时同步附属文件（首次全量建议关闭）"
    )
    delete_sync: bool = Field(default=True, description="115 删除后同步删除本地文件")
    delete_sync_verify: bool = Field(
        default=True, description="删除前逐条确认 115 文件确实不存在（防误删）"
    )
    delete_sync_batch_limit: int = Field(
        default=20, ge=1, description="单次最多自动删除条数，超过需人工确认"
    )
    delete_sync_max_ratio: float = Field(
        default=0.2, ge=0.0, le=1.0, description="单次删除占索引比例上限，超过需人工确认"
    )
    debug: bool = Field(default=False, description="调试日志")

    # 通知
    notify_enabled: bool = Field(default=True, description="启用通知（对账完成/播放失败）")

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        """
        去掉基础地址两侧空白与尾部斜杠。

        :param value: 原始配置值
        :return: 规范化后的地址
        """
        return (value or "").strip().rstrip("/")

    @field_validator("strm_root_local", "strm_root_fnos")
    @classmethod
    def _normalize_root(cls, value: str) -> str:
        """
        统一 STRM 根目录格式：去空白、反斜杠转正斜杠。

        :param value: 原始配置值
        :return: 规范化后的目录
        """
        return (value or "").strip().replace("\\", "/")

    @field_validator("attachment_conflict")
    @classmethod
    def _validate_conflict(cls, value: str) -> str:
        """
        校验附属文件冲突策略。

        :param value: 原始配置值
        :return: skip 或 overwrite
        """
        value = (value or "skip").strip().lower()
        return value if value in {"skip", "overwrite"} else "skip"

    @field_validator("reconcile_mode")
    @classmethod
    def _validate_reconcile_mode(cls, value: str) -> str:
        """
        校验对账模式。

        :param value: 原始配置值
        :return: history 或 scan
        """
        value = (value or "history").strip().lower()
        return value if value in {"history", "scan"} else "history"

    @property
    def video_ext_set(self) -> set[str]:
        """
        返回小写视频扩展名集合（不含点）。

        :return: 扩展名集合
        """
        return {
            ext.strip().lstrip(".").lower()
            for ext in self.video_exts.split(",")
            if ext.strip()
        }

    @property
    def copy_ext_set(self) -> set[str]:
        """
        返回小写附属文件扩展名集合（不含点）。

        :return: 扩展名集合
        """
        return {
            ext.strip().lstrip(".").lower()
            for ext in self.copy_exts.split(",")
            if ext.strip()
        }

    @property
    def min_size_bytes(self) -> int:
        """
        返回最小文件大小的字节数。

        :return: 字节数
        """
        return max(0, self.min_size_mb) * 1024 * 1024

    @property
    def attachment_max_bytes(self) -> int:
        """
        返回附属文件大小上限的字节数。

        :return: 字节数
        """
        return max(1, self.attachment_max_mb) * 1024 * 1024
