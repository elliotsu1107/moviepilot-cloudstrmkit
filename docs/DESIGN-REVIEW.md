<!-- 实例化为 docs/DESIGN-REVIEW.md -->
# 设计完备性审查（DESIGN-REVIEW）

> 更新日期：2026-11-01
> 对应版本：v0.1.0
> 状态：待开工前确认
> 目的：开工前把"会导致返工"的点全部前置解决；本文结论合并进 `docs/SPEC.md` 与 `docs/PRD.md`。

---

## 一、预研结论（MoviePilot v3.2.3 源码核实）

| # | 结论 | 依据 |
|---|---|---|
| P1 | 插件本地开发：`PLUGIN_LOCAL_REPO_PATHS` 指向"插件市场仓库"目录，读取该目录下 `package.json` / `package.{gen}.json`（v3 用 `package.v3.json`）作为索引 | `app/adapters/external/plugin/client.py:516`、`:533` |
| P2 | 索引条目字段：`name/description/labels/version/icon/author/level/release/history/system_version` | 官方市场 `package.v3.json` 样例 |
| P3 | 插件安装后运行目录为 `{ROOT_PATH}/app/plugins/<id>/`；本地仓支持"同步到运行目录"（local_sync 机制） | `app/startup/initializers/plugins.py`、`app/application/plugin/install.py` |
| P4 | 插件 API：`get_api()` 声明，前缀 `{API_V1_STR}/plugin/{plugin_id}`；`allow_anonymous: True` 免鉴权；可返回任意 Response（含 302） | `app/adapters/web/plugin/routes.py`、`app/factory.py:439` |
| P5 | 事件装饰器 `@eventmanager.register(EventType.X)` 在 `app/sdk/events.py`；**支持 async handler**（事件系统有 async 安全调用） | `app/runtime/events.py:847`、`:949` |
| P6 | `TransferComplete` 载荷 `TransferResultContractData(fileitem/meta/mediainfo/transferinfo)`；`TransferInfo` 含 `target_item/target_diritem/file_list_new/success` | `app/schemas/event.py:484`、`app/schemas/transfer.py:179` |
| P7 | 115 换链最佳路径：`U115Pan()._request_api("POST","/open/ufile/downurl","data", data={"pick_code": pickcode}, headers={"User-Agent": ua})`；该方法自带 **token 自动刷新 + 下载端点独立限流 + 风控共享冷却 + 有限重试** | `app/modules/filemanager/storages/u115.py:1014-1070`、`:366-430` |
| P8 | `U115Pan.access_token` 属性会自动用 refresh_token 续期并更新请求头；插件无需自己管授权 | `u115.py:136-165` |
| P9 | 飞牛刷新：`MediaServerHelper().get_service(name, type_filter="trimemedia")` → `instance.refresh_library_by_items([RefreshMediaItem])`；`RefreshMediaItem` 字段 `title/year/type/category/target_path` | `app/sdk/services.py`、`app/schemas/mediaserver.py:150` |
| P10 | 飞牛 `refresh_library_by_items` 内部按 `target_path` 匹配媒体库目录；未认证返回 None，非管理员返回 False；匹配失败回退全库 `refresh_root_library` | `app/modules/trimemedia/trimemedia.py:459-540` |
| P11 | `MediaServerHelper.get_services(name_filters=)` / `get_service(name, type_filter=)` 返回 `ServiceInfo(name/instance/module/type/config)` | `app/application/service.py:167-201` |
| P12 | 配置页为 Vuetify 组件树（VForm/VRow/VCol/VSwitch/VTextField/VSelect/VTextarea 等），由 `get_form()` 返回 | 官方 v3 插件 `doubanrank` 实现 |
| P13 | 插件数据目录 `get_data_path()`；插件可直接读写该目录（索引文件放这里） | `app/sdk/plugin/base.py:343` |

**结论：设计中的 6 个技术假设（匿名 302 路由、UA 换链、宿主 115 授权复用、飞牛刷新、事件驱动、本地插件热加载）全部有源码支撑，无需调整架构。**

---

## 二、工程结构定稿

项目根即"本地插件市场仓库"：

```
moviepilot-cloudstrmkit/
├── package.v3.json              # 本地市场索引（唯一需要维护的元数据）
├── plugins.v3/
│   └── cloudstrmkit/
│       ├── __init__.py          # 插件入口（_PluginBase 子类）
│       ├── config.py            # 配置模型
│       ├── constants.py
│       ├── index.py             # STRM 索引
│       ├── strm.py              # 路径映射/生成/签名
│       ├── redirect.py          # 302 服务
│       ├── u115.py              # 115 换链封装
│       ├── attachments.py       # 附属文件同步
│       ├── fnos.py              # 飞牛刷新
│       ├── reconcile.py         # 对账
│       └── pyproject.toml       # 依赖声明（可为空依赖）
├── docs/
└── README.md
```

`package.v3.json` 条目：

```json
{
  "CloudStrmKit": {
    "name": "云盘STRM工具箱",
    "description": "115 网盘 STRM 生成与 302 直链播放，飞牛影视实时刷新。",
    "labels": "云盘",
    "version": "0.1.0",
    "icon": "https://.../u115.png",
    "author": "<你的名字>",
    "level": 1,
    "release": false,
    "history": {"v0.1.0": "首个版本：事件驱动 STRM、115 302、飞牛刷新、附属文件同步、对账。"},
    "system_version": ">=3.0.0"
  }
}
```

### 开发工作流（避免开工后卡住）

1. MP 容器挂载项目根，并设置环境变量：
   `PLUGIN_LOCAL_REPO_PATHS=/plugins-src`（示例），`DEV=true`（热加载/调试模式）
2. MP 插件市场 → 本地插件 → 安装 CloudStrmKit
3. 改代码后：插件详情页"重新加载"或市场"同步本地插件"（local_sync）
4. 验证路径：插件出现在已安装列表、`/api/v1/plugin/cloudstrmkit/...` 路由可访问

> Day 0 必须先跑通这一步，再写业务逻辑，避免"代码写完但装不进去"。

---

## 三、补齐的边界与规则

| # | 场景 | 规则 |
|---|---|---|
| R1 | 覆盖整理（`overwrite=latest`） | 同一 115 路径再次整理：覆盖 STRM、更新索引，不产生重复条目；旧附属文件按新目录重新同步 |
| R2 | 文件改名/移动 | 路径变化 → 索引 ID 变化 → 旧 STRM 成为孤儿；对账按"115 不存在"删除旧 STRM，新增生成 |
| R3 | 整季/批量整理 | 单事件多文件合并处理；STRM 逐文件写，飞牛刷新合并为一次（去抖 3s），避免触发飞牛 -14 重复任务 |
| R4 | 目录级附属文件生命周期 | `tvshow.nfo`/`poster.jpg` 等目录级文件只在**该目录所有视频条目都删除后**才删除，避免删某一集时误删整季海报 |
| R5 | 同目录多视频共享附属文件 | 按目录级同步一次（目录去重），不重复下载 |
| R6 | 字幕外挂 | `<视频名>.<lang>.srt/ass/ssa/sub` 命中"以视频主名开头"规则，随视频同步 |
| R7 | 签名过期 | `e` 默认 365 天；对账时检测索引中 `e` 已过期或 `base_url` 变更，自动重写该条 STRM |
| R8 | base_url 变更 | 提供"全量重写 STRM"按钮；重写只改内容，不动目录结构，不触发飞牛重新刮削（飞牛按内容变化判断） |
| R9 | 索引损坏/丢失 | 启动校验失败 → 备份损坏文件 → 从 115 目录全量重建（等价一次全量对账） |
| R10 | STRM 写盘失败（权限/磁盘满） | 记录失败 + 重试队列（3 次退避）；持续失败在插件页展示告警；不阻塞其它条目 |
| R11 | 115 换链失败 | 分类：授权失效 → 提示重新授权 115 存储；风控/限频 → 冷却 + 503；网络 → 502 重试 |
| R12 | 并发播放同一文件 | 同一 `(pickcode, UA)` 只允许一个换链在途，其余等待结果，避免重复消耗限频 |
| R13 | 插件禁用/卸载 | STRM 文件保留（避免飞牛突然缺片）；卸载前插件页提示先执行"清理本插件文件"；索引在插件数据目录，卸载即丢失，重新安装后靠对账重建 |
| R14 | MP 重启/插件重载 | 索引从磁盘加载；事件期间丢失的条目由对账补齐；线程/调度器在 `stop_service`/`close` 中清理 |
| R15 | 多实例 | v0.1 只支持"一个 115 存储 + 一个飞牛实例"；多实例列入 v0.2 |
| R16 | 覆盖整理时的旧附属文件 | 同路径覆盖整理时：先清理该视频旧的附属文件（索引记录的 + 同目录下已无对应 STRM 的同名 NFO/海报），再按新文件重新同步，避免旧 NFO 残留 |
| R17 | 历史遗留孤儿附属文件（SmartStrm 时期） | 提供手动「清理孤儿附属文件」工具：扫描本地 STRM 目录，列出"没有对应 .strm 的附属文件"清单，用户确认后删除；仅限白名单扩展名，不碰用户其他文件 |

---

## 四、返工风险清单与对策

| 风险 | 概率 | 影响 | 对策（开工前） |
|---|---|---|---|
| 插件装不进 MP（本地仓格式不对） | 中 | 高（卡住全部） | Day 0 先用空插件验证安装/同步流程 |
| 115 换链 UA 绑定与预期不符 | 中 | 高（302 不可用） | V1 实测；预留 cookie 模式/中转降级 |
| 飞牛不解析内网 STRM（模式 B 失效） | 中 | 中（无公网时受限） | V3 实测；模式 B 标注"实测确认" |
| `TransferComplete` 触发时机早于 115 可见 | 低 | 中 | 生成前校验 115 文件存在，不存在则入重试队列（R10） |
| `target_item` 不带 pickcode | 高 | 低 | 按路径查 `U115Pan.get_item` 兜底 |
| 飞牛刷新报 -14 重复任务 | 中 | 低 | 去抖合并 + 串行刷新（R3） |
| 飞牛不识别本地 NFO/海报命名 | 中 | 中 | V6 实测；预留"文件名归一化"开关 |
| 115 直链有效期短于 TTL | 中 | 中 | 按响应里的过期参数动态调 TTL（若有）；V7 实测 |
| MP 升级破坏私有 API | 中 | 中 | `u115.py`/`fnos.py` 单点封装 + 降级实现 |
| 索引并发写损坏 | 低 | 中 | 原子写 + 锁 + 启动校验 + 对账重建 |

---

## 五、验证清单（V1–V12，开工后逐项打钩）

| # | 验证项 | 方法 | 通过标准 | 阶段 |
|---|---|---|---|---|
| V1 | 115 换链 UA 绑定 | 插件内用飞牛 UA 换链，再以同 UA/异 UA 请求直链 | 同 UA 200；异 UA 403（预期） | T3 |
| V2 | 飞牛刷新生效 | 对已有媒体库调用 `refresh_library_by_items` | 飞牛日志出现扫描、媒体库出现影片 | T3/T7 |
| V3 | 飞牛播放 STRM 行为 | 局域网/公网/FN ID 各播一次，观察插件日志 | 记录请求来源与是否 302 | T11 |
| V4 | 事件载荷 | 日志打印 `target_item.storage/path/pickcode`、`file_list_new` | 能拿到 115 路径；pickcode 可缺省但能补查 | T3 |
| V5 | 路径映射 | 容器内写 `/strm_test/...`，刷新用宿主路径 | 飞牛媒体库匹配 | T7 |
| V6 | 飞牛读本地 NFO/海报 | 放入 MP 产物的 NFO/图片，飞牛刮削 | 标题/简介/海报正确 | T11 |
| V7 | 115 直链有效期 | 记录换链响应中的过期时间，间隔请求 | 得出实际有效期，校准 TTL | T3 |
| V8 | 飞牛刷新频率 | 连续整理 10 部影片 | 无 -14 错误，扫描请求合并 | T7 |
| V9 | 115 目录列表 pickcode | `U115Pan.list()` 结果检查 `FileItem.pickcode` | 有值则生成时可免二次查询 | T3 |
| V10 | 插件本地仓安装 | 空插件走本地仓安装+同步 | 插件列表可见、路由可访问 | Day 0 |
| V11 | 飞牛客户端 UA | 日志记录播放请求 UA | 得到 UA 字符串，用于换链 | T11 |
| V12 | MP 重启恢复 | 重启 MP 后检查索引/调度器/路由 | 功能自动恢复，对账可用 | T11 |

### 5.1 已实测结论（2026-11-01）

| # | 结论 | 证据 |
|---|---|---|
| V1 | **UA 绑定成立**：换链 UA = A 时，用 A 请求直链返回 206；用 B 请求同一直链返回 403。空 UA/浏览器 UA/自定义 UA 均可换链，直链各自绑定换链 UA | 插件 `/diag/u115` 实测：3 组 UA 交叉验证 |
| V10 | 本地插件仓安装、更新、路由注册均正常；更新依赖版本号递增 | 插件市场安装、`/api/v1/plugin/CloudStrmKit/ping` 返回版本 |
| 环境问题 | **宿主 `U115Pan._request_api` 在本环境换链失败（返回空）**，而插件内直连 115 开放平台（禁用代理）成功（code=0 + 有效直链）。判断为 MP 容器 `HTTP_PROXY` 影响宿主请求链路 | 同一 pickcode：宿主链路失败、直连成功 |

由此确定 T4 实现方向：**插件内自实现 115 直链请求（绕过宿主 RequestUtils/代理，使用请求方 UA 换链）**，不再依赖 `U115Pan._request_api` 换链；`U115Pan` 仅用于取 token 与按路径查 pickcode。

---

## 六、Day 0 预研任务（开工前，需主人配合 30 分钟）

1. **本地插件仓打通**（V10）：MP 容器增加挂载与 `PLUGIN_LOCAL_REPO_PATHS` 环境变量，装一个空壳插件验证。
2. **确认 115 存储名称**：MP 存储页显示的 115 存储名（插件配置用）。
3. **确认飞牛实例管理员账号**：MoviePilot 中 `MyFnos` 实例使用的是否管理员账号（非管理员刷新会失败）。
4. **确认 115 直链能否被浏览器直接打开**（V1 的粗验）：从 SmartStrm 后台复制一个 STRM 地址，用浏览器/curl 访问，看 302 后 115 是否返回 200/403（可判断 UA 限制是否存在）。

> 这 4 项完成后，代码阶段只剩实现与联调，预计返工概率显著降低。

---

## 七、现有插件评估（针对飞牛影视）

| 插件 | 作用 | 对飞牛是否有效 | 建议 |
|---|---|---|---|
| 媒体库刮削（LibraryScraper v3.1.2） | 遍历本地目录，用 MP 刮削链补 NFO/海报到本地文件 | **有效**：纯本地文件刮削，不依赖 Emby/Jellyfin API，可直接指向 STRM 目录 | 暂不启用；等 CloudStrmKit 附属文件同步跑通后，作为"115 缺 NFO 时"的兜底手段二选一 |
| 演职人员刮削（PersonMeta v3.1.3） | 读取媒体服务器 People 字段并写回中文名/角色/头像 | **无效**：代码只实现 Emby/Jellyfin/Plex 私有 API（`emby/Users/.../Items` 等），无 trimemedia 分支 | 建议卸载或禁用，避免每次整理完成产生无效请求与报错日志 |

---

## 八、环境已知问题

| 问题 | 现象 | 结论与处理 |
|---|---|---|
| Lucky WAF 拦截超长 URL | 前端插件页偶发 403；请求为 `GET /api/v1/plugin/rating?plugin_ids=<96个插件ID>`（URL 约 1357 字符）。内网直连 MP 返回 200，经 Lucky 域名返回 HTML「403 - Lucky WAF」；同一请求只传 3 个 ID 经域名则正常 200 | Lucky WAF 将逗号分隔的超长参数误判（疑似 SQL 注入/长参数规则）。处理：Lucky 该站点 WAF 为 `/api/v1/plugin/rating` 加 URL 白名单，或关闭 SQL 注入检测。仅影响插件评分星标显示，不影响其他功能 |

---

## 九、明确推迟到 v0.2 的增强

- 多 115 存储 / 多飞牛实例
- 115 文件管理页（列目录/上传/下载/重命名/删除）
- 115 全盘增量对账（life 事件）
- 插件自定义 Vue 前端页
- 飞牛 8005 Emby 兼容端点适配
- 多用户权限
