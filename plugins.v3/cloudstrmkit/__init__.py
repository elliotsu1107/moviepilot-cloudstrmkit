"""
CloudStrmKit 插件入口。

当前阶段（T2）：配置模型、索引持久化、配置页与详情页统计。
后续任务：事件监听、STRM 生成、115 302、飞牛刷新、对账（见 docs/PLAN.md）。
"""

from typing import Any, Dict, List, Optional, Tuple

from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .config import CloudStrmKitConfig
from .index import StrmIndex

# 插件标识（与 package.v3.json 的 key 一致）
PLUGIN_ID = "CloudStrmKit"


class CloudStrmKit(_PluginBase):
    """云盘STRM工具箱：115 网盘 STRM 生成与 302 直链播放。"""

    # 插件名称
    plugin_name = "云盘STRM工具箱"
    # 插件描述
    plugin_desc = "115 网盘 STRM 生成、302 直链播放与飞牛影视实时刷新。"
    # 插件图标
    plugin_icon = (
        "https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/"
        "refs/heads/v2/src/assets/images/misc/u115.png"
    )
    # 插件版本
    plugin_version = "0.1.0"
    # 插件作者
    plugin_author = "elliotsu1107"
    # 作者主页
    author_url = "https://github.com/elliotsu1107"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudstrmkit_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 运行时状态
    _config: CloudStrmKitConfig = CloudStrmKitConfig()
    _index: Optional[StrmIndex] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化插件：解析配置并加载 STRM 索引。

        :param config: 插件配置字典
        """
        self._config = self._parse_config(config)
        self._index = StrmIndex(self.get_data_path())
        logger.info(
            f"【CloudStrmKit】插件初始化，enabled={self._config.enabled}，"
            f"索引条目={len(self._index)}，数据目录={self.get_data_path()}"
        )

    def get_state(self) -> bool:
        """
        返回插件运行状态。

        :return: 是否启用
        """
        return self._config.enabled

    def stop_service(self) -> None:
        """
        停止插件服务：保存索引并释放引用。
        """
        if self._index is not None:
            try:
                self._index.save()
            except Exception as err:  # noqa: BLE001 - 停止阶段不阻断宿主
                logger.error(f"【CloudStrmKit】停止时保存索引失败：{err}")
        self._index = None

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_config(config: Optional[Dict[str, Any]]) -> CloudStrmKitConfig:
        """
        把插件配置字典解析为配置模型，非法值回退默认值。

        :param config: 插件配置字典
        :return: 配置模型
        """
        if not config:
            return CloudStrmKitConfig()
        try:
            return CloudStrmKitConfig(**config)
        except Exception as err:  # noqa: BLE001 - 配置非法时不让插件崩溃
            logger.error(f"【CloudStrmKit】配置解析失败，使用默认配置：{err}")
            return CloudStrmKitConfig()

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件 API。

        :return: 接口声明列表
        """
        return [
            {
                "path": "/ping",
                "endpoint": self.ping,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "健康检查",
                "description": "返回 CloudStrmKit 运行状态与索引统计",
            },
            {
                "path": "/stats",
                "endpoint": self.api_stats,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "索引统计",
                "description": "返回 STRM 索引统计信息",
            },
        ]

    def ping(self) -> Dict[str, Any]:
        """
        健康检查接口。

        :return: 统一响应结构
        """
        return {
            "success": True,
            "message": "",
            "data": {
                "plugin": PLUGIN_ID,
                "version": self.plugin_version,
                "enabled": self._config.enabled,
                "items": len(self._index) if self._index else 0,
            },
        }

    def api_stats(self) -> Dict[str, Any]:
        """
        索引统计接口。

        :return: 统一响应结构
        """
        stats = self._index.stats() if self._index else {}
        return {"success": True, "message": "", "data": stats}

    # ------------------------------------------------------------------
    # 配置页
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """
        拼装插件配置页。

        :return: Vuetify 组件树与默认配置
        """
        defaults = CloudStrmKitConfig().model_dump()
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "当前阶段仅完成配置与索引（T2）。"
                                            "事件驱动 STRM 生成、302 播放、飞牛刷新将在后续任务接入。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 基础设置
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "source_storage",
                                            "label": "115 存储名称",
                                            "placeholder": "115网盘",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "fnos_instance",
                                            "label": "飞牛媒体服务器实例名",
                                            "placeholder": "如 MyFnos",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_url",
                                            "label": "对外基础地址",
                                            "placeholder": "https://mp.example.com",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # STRM 路径
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strm_root_local",
                                            "label": "容器内 STRM 根目录",
                                            "placeholder": "/strm",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strm_root_fnos",
                                            "label": "飞牛视角 STRM 根目录",
                                            "placeholder": "/path/to/strm",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "video_exts",
                                            "label": "视频扩展名",
                                            "placeholder": "mkv,mp4,ts",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_size_mb",
                                            "label": "最小文件大小(MB)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射（每行：115前缀 => 容器内根目录）",
                                            "rows": 4,
                                            "placeholder": "/movie => /strm/movie\n/tv => /strm/tv",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    # 302 与安全
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cache_ttl",
                                            "label": "直链缓存(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "qps",
                                            "label": "换链限频(QPS)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cooldown_seconds",
                                            "label": "风控冷却(秒)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "secret",
                                            "label": "STRM 签名密钥",
                                            "placeholder": "留空自动生成",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 附属文件
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "copy_exts",
                                            "label": "附属文件扩展名",
                                            "placeholder": "nfo,jpg,png,srt",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "attachment_max_mb",
                                            "label": "附属文件大小上限(MB)",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "attachment_conflict",
                                            "label": "附属文件冲突策略",
                                            "items": [
                                                {"title": "跳过已有文件", "value": "skip"},
                                                {"title": "覆盖已有文件", "value": "overwrite"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    # 对账
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "reconcile_cron",
                                            "label": "对账 Cron",
                                            "placeholder": "30 4 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_sync",
                                            "label": "115 删除后同步删除本地文件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "debug", "label": "调试日志"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "library_roots",
                                            "label": "对账用 115 媒体库根路径（每行一个）",
                                            "rows": 3,
                                            "placeholder": "/movie/电影\n/tv/电视剧",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], defaults

    # ------------------------------------------------------------------
    # 详情页
    # ------------------------------------------------------------------
    def get_page(self) -> Optional[List[dict]]:
        """
        拼装插件详情页（索引统计）。

        :return: Vuetify 组件树
        """
        stats = self._index.stats() if self._index else {}
        cards = [
            ("索引条目", stats.get("total", 0)),
            ("附属文件", stats.get("attachments", 0)),
            ("目录级附属文件", stats.get("dir_attachments", 0)),
            ("缺失 pickcode", stats.get("missing_pickcode", 0)),
        ]
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6, "lg": 3},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "props": {"class": "text-center"},
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {"class": "text-h6"},
                                                "text": str(value),
                                            },
                                            {
                                                "component": "div",
                                                "props": {"class": "text-caption"},
                                                "text": label,
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                    for label, value in cards
                ],
            },
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "secondary",
                                    "variant": "tonal",
                                    "text": f"索引文件：{stats.get('index_path', '未初始化')}",
                                },
                            }
                        ],
                    }
                ],
            },
        ]
