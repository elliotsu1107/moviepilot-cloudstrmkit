<!-- 实例化为 docs/SPEC.md -->
# CloudStrmKit 技术规格（SPEC）

> 更新日期：2026-11-01
> 对应版本：v0.1.0
> 状态：草稿（待主人确认）
> 依赖需求：`docs/PRD.md`

---

## 一、范围

- **本规格覆盖**：MoviePilot v3 插件（单插件）、115 直链换取、STRM 生成、飞牛影视刷新、定时对账、插件配置页与运维接口。
- **本规格不覆盖**：Emby/Jellyfin/Plex、飞牛 8005 Emby 兼容端点、115 全盘事件监听、转码与代理中转、刮削增强。

### 1.1 已核实的宿主事实（本方案的地基）

| 能力 | 宿主位置 | 结论 |
|---|---|---|
| 插件基类 | `app/sdk/plugin/base.py` `_PluginBase` | v2 风格生命周期：`init_plugin/get_state/get_api/get_form/get_page/get_service` |
| 插件动态路由 | `app/adapters/web/plugin/routes.py`、`app/factory.py` | 前缀 `{API_V1_STR}/plugin/{plugin_id}`；声明 `allow_anonymous: True` 可免鉴权；可返回任意 `Response`（含 `RedirectResponse`） |
| 整理完成事件 | `EventType.TransferComplete`（`app/chain/transfer/settlement.py`） | 载荷 `TransferResultContractData`：`fileitem/meta/mediainfo/transferinfo` |
| 整理拦截事件 | `ChainEventType.TransferIntercept` | 写盘前可取消并自行处理（本版不使用，保留为后续优化点） |
| 115 存储 | `app/modules/filemanager/storages/u115.py` `U115Pan` | WeakSingleton，`https://proapi.115.com`，`access_token` 属性 + `_request_api`；`/open/ufile/downurl` 用 `pick_code` 换直链；`FileItem.pickcode` 可用 |
| 飞牛影视 | `app/modules/trimemedia/` | 登录/签名/媒体库/`refresh_library_by_items`/`refresh_root_library` 均已实现 |
| 媒体服务器服务发现 | `app/sdk/services.py` `MediaServerHelper` | `get_services(name_filters=)` / `get_service(name, type_filter=)` 返回 `ServiceInfo(name/instance/type/config)` |
| 插件本地开发 | `PLUGIN_LOCAL_REPO_PATHS` 环境变量 | 可挂载本地插件仓库目录（`plugins` / `plugins.v2` / `plugins.v3`）热加载 |
| Python 版本 | MP v3 镜像 3.14.7 | 与插件 `requires-python >=3.14` 一致 |

---

## 二、总体方案

### 2.1 技术选型

| 项 | 选择 | 理由 | 备选与否决原因 |
|---|---|---|---|
| 运行形态 | MoviePilot v3 插件（同进程） | 零新增服务；复用宿主 115 授权、飞牛客户端、事件总线、HTTP 端口与现成反代 | 独立服务：多一个容器/端口，与"零外部服务"目标冲突 |
| 语言/运行时 | Python 3.14 | 宿主环境 | Go 独立二进制：违背插件形态 |
| 115 直链 | **插件内直连 115 开放平台**：从宿主 115 存储取 token，用 `urllib` 禁用环境代理请求 `/open/ufile/downurl`，以请求方 UA 换链 | V1 实测：宿主 `U115Pan._request_api` 受 MP 容器 `HTTP_PROXY` 影响换链失败，直连成功；且直链绑定换链 UA | p115client：重依赖；复用宿主 RequestUtils：代理环境不可靠 |
| 数据存储 | 插件数据目录 JSON 索引（原子写 + 线程锁） | 数据量小（< 10k 条）、无外部依赖、可读可备份 | SQLite：可用插件 get_database，但 JSON 足够；预留升级 |
| 直链缓存 | 进程内 TTL 缓存 + LRU 上限 | 限频/防风控；无需持久化（重启可重建） | 磁盘缓存：收益低 |
| HTTP 客户端 | 标准库 `urllib`（显式 `ProxyHandler({})` 禁代理） | 规避 MP 容器全局代理；无第三方依赖 | 宿主 RequestUtils：受代理配置影响（实测换链失败） |
| 前端配置页 | Vuetify JSON 组件树（宿主原生） | 插件标准做法 | Vue 自定义页：重 |

### 2.2 模块划分

```
plugins.v3/cloudstrmkit/
├── __init__.py        # 插件入口：生命周期、配置、事件、API、定时服务、配置页
├── config.py          # Pydantic 配置模型、默认值、校验
├── constants.py       # 常量（路由、默认值、事件键）
├── index.py           # STRM 索引：加载/保存/增删改查/锁
├── strm.py            # 路径映射、STRM 内容生成、原子写入/删除
├── redirect.py        # /play 实现：验签、UA 换链、缓存、限频、锁、302 响应
├── u115.py            # 115 直链换取（宿主 token + 直连 API + 请求方 UA 绑定）与 pickcode 查询
├── attachments.py     # 刮削附属文件同步（115 → 本地 NFO/海报/字幕）
├── fnos.py            # 飞牛刷新封装（去抖合并、路径映射、降级）
├── reconcile.py       # 对账：115 目录遍历、差异计算、执行
└── pyproject.toml     # 插件元数据（无第三方依赖）
```

数据流：

```
TransferComplete ──> strm.py（映射+写文件） ──> index.py（落库）
                        ├──> attachments.py（115 目录白名单文件 → 本地）
                        └──> fnos.py（去抖） ──> 飞牛 mdb_scan
播放请求 ──> redirect.py ──> index.py（查 pickcode）──> u115.py（换链）──> 302
定时任务 ──> reconcile.py ──> index.py + strm.py + attachments.py + u115.list
```

---

## 三、数据模型

### 3.1 索引文件

位置：`get_data_path() / "index.json"`；结构：

```json
{
  "version": 1,
  "items": {
    "a1b2c3d4": {
      "id": "a1b2c3d4",
      "storage": "u115",
      "pan_path": "/电影/流浪地球2 (2023)/流浪地球2.mkv",
      "pickcode": "abcdefgh12345678x",
      "strm_path": "/vol2/1000/Media/Strm/电影/流浪地球2 (2023)/流浪地球2.strm",
      "size": 123456789,
      "mtime": 1762000000,
      "media_type": "movie",
      "created_at": 1762000000,
      "attachments": [
        {
          "pan_path": "/电影/流浪地球2 (2023)/流浪地球2.nfo",
          "local_path": "/vol2/1000/Media/Strm/电影/流浪地球2 (2023)/流浪地球2.nfo",
          "size": 4096,
          "mtime": 1762000000,
          "source": "plugin"
        }
      ]
    }
  }
}
```

- `attachments`：本插件从 115 复制到本地的附属文件清单（`source=plugin`）。删除/清理只针对该清单内的本地文件；用户手动放入的文件不会记录、不会被删除。
- 目录级文件（`tvshow.nfo`、`poster.jpg` 等）归属到该目录下**首个**视频条目，或单独维护 `dir_attachments` 节（实现时二选一，优先单独节避免重复记录）。

- `id`：`sha1(pan_path)` 前 8 位 + 冲突序号，保证稳定、可放进 URL。
- 写入：`tmp` 文件 + `os.replace` 原子替换；进程内 `threading.RLock` 串行化。
- 启动时加载；损坏时备份为 `index.json.bak` 并重建（对账可恢复）。

### 3.2 直链缓存（内存）

```python
key = (pickcode, ua_hash)     # ua_hash = sha1(UA)[:16]
value = {"url": ..., "expire_at": monotonic+ttl, "fetched_at": ...}
```

- TTL 默认 1200 秒，可配；上限 2048 条 LRU。
- 同一 key 换链使用 `threading.Lock` 去重（并发合并）。

---

## 四、模块设计

### 4.1 插件入口 `__init__.py`

继承 `app.sdk.plugin._PluginBase`，元信息：

| 字段 | 值 |
|---|---|
| `plugin_name` | 飞牛STRM302 |
| `plugin_id` / 目录 | `cloudstrmkit` |
| `plugin_version` | 与 `version.py` 同步 |
| `plugin_config_prefix` | `cloudstrmkit_` |
| `auth_level` | 1 |
| `plugin_order` | 99 |

生命周期：

- `init_plugin(config)`：加载配置 → 初始化索引 → 注册事件处理器与定时任务（宿主根据 `get_service` 管理）→ 构建飞牛刷新队列。
- `get_state()`：返回配置中的 `enabled`。
- `get_api()`：返回 `/play`（匿名 GET/HEAD）、`/stats`、`/resync`、`/clear_cache`（bear）。
- `get_service()`：返回对账任务（cron）与去抖刷新任务（interval）。
- `stop_service()`/`close()`：停止线程、保存索引、清理路由由宿主处理。

⚠️ 宿主事件处理器为 async；处理体必须 `run_in_threadpool`/专用队列执行，禁止阻塞事件循环。

### 4.2 事件处理（F1）

```python
@eventmanager.register(EventType.TransferComplete)
async def on_transfer_complete(event) -> None:
    data = event.event_data            # TransferResultContractData
    info = data.transferinfo           # TransferInfo
    if not info or not info.success:
        return
    target = info.target_item          # FileItem（含 storage/path）
    if target.storage != config.source_storage:
        return
    files = [p for p in (info.file_list_new or []) if is_video(p)]
    queue.submit(files, info)          # 交给后台单线程队列
```

后台队列执行：

1. 逐文件路径映射 → `strm_path`
2. 取 `pickcode`：优先 `info.target_item.pickcode`；缺失则 `u115.get_pickcode(path)`（走 `U115Pan.get_item`）
3. 写 STRM（原子）
4. 写索引
5. 合并到飞牛刷新批次（去抖 3 秒）

### 4.3 路径映射与双路径（`strm.py`）

插件运行在 MoviePilot 容器内，**写文件用容器内路径，飞牛刷新用 NAS 宿主路径**，两者通过配置解耦：

| 配置 | 含义 | 示例 |
|---|---|---|
| `strm_root_local` | 容器内 STRM 根目录（插件写入用） | `/strm_test` |
| `strm_root_fnos` | 飞牛视角 STRM 根目录（刷新时传给飞牛） | `/path/to/strm` |
| `path_mappings` | 115 前缀 => 容器内 STRM 根，每行一条 | `/movie => /strm_test/movie`<br>`/tv => /strm_test/tv` |

流程：

1. **生成**：115 路径 → 容器内路径（`path_mappings` 最长前缀匹配，剩余子路径保留，扩展名替换为 `.strm`），写文件。
2. **刷新**：容器内路径 → 飞牛路径（用 `strm_root_fnos` 替换 `strm_root_local` 前缀）→ 作为 `refresh_library_by_items(target_path)` 的入参，飞牛据此匹配媒体库目录。
3. 未匹配任何映射时记录告警并跳过，不猜测；容器内路径无法换算为飞牛路径时，刷新降级为全库扫描并告警。
4. 路径分隔符统一 `/`；非法字符按 `PathUtils` 规则清洗。

### 4.4 STRM 内容与签名（`strm.py`）

```
{base_url}/api/v1/plugin/cloudstrmkit/play?f={id}&e={expire_ts}&sig={hmac}
hmac = HMAC_SHA256(secret, f"{id}:{expire_ts}")[:32]
```

- `e` 默认 30 天有效期（播放器会长期持有 URL）；签名密钥 32 字节随机，配置页可重置（重置后旧 STRM 需重生成，提供"全量重写"按钮）。
- `base_url` 为 MP 公网地址（如 `https://media.example.com`），必须不含尾部斜杠。

### 4.5 302 服务（`redirect.py`）

```
GET/HEAD /api/v1/plugin/<id>/play?f=&e=&sig=
  1. 验签、验过期 → 403
  2. 查索引 → 无 → 404（写日志）
  3. 缓存命中（键 = pickcode + UA）→ 302
  4. 换链：u115.get_download_url(pickcode, ua=request.headers["user-agent"])
     - 直连 proapi.115.com，显式禁用环境代理
     - 直链与换链 UA 绑定（V1 实测），因此必须原样使用请求方 UA
  5. 失败分类：401/403 授权失效 → 502 + 告警；429/风控 → 503 + 冷却
  6. 302 + Content-Disposition
```

> 实测：直链绑定换链 UA（同 UA 206、异 UA 403）；因此 302 后客户端会沿用同一 UA 请求 115 CDN，能正常播放。

- 限频：全局 `threading.Semaphore` + 令牌桶（默认 2 QPS）。
- 冷却：风控触发后 `cooldown_seconds`（默认 60s）内直接 503，避免放大风控。
- 日志：`deque(maxlen=200)` 内存环形缓冲，插件页面读最近 50 条。

### 4.6 飞牛刷新（`fnos.py`）

```python
helper = MediaServerHelper()
svc = helper.get_service(name=config.fnos_instance, type_filter="trimemedia")
if svc and hasattr(svc.instance, "refresh_library_by_items"):
    svc.instance.refresh_library_by_items(items)   # items: list[RefreshMediaItem]
```

- `RefreshMediaItem` 字段：`title/year/type/category/target_path`（`target_path` 为**飞牛视角**路径）。
- 路径换算：用 `strm_root_fnos` 替换容器内 `strm_root_local` 前缀，得到飞牛媒体库路径；无映射则全库扫描兜底。
- 去抖：`deque` 收集 → 定时器 3 秒后合并成一次调用。
- 识别媒体信息：从事件 `mediainfo` 快照取 title/year/type，避免额外识别请求。
- 降级：`refresh_library_by_items` 返回 None/False → 调 `refresh_root_library()`；再失败记录告警。

### 4.7 对账（`reconcile.py`）

- 触发：cron（默认 `30 4 * * *`）+ 手动按钮。
- 遍历：`U115Pan.list` 递归配置的媒体库根（限速、分批、跳过 `#recycle` 等回收站）。
- 差异：以 `pan_path` 为键比对索引；新增/删除/大小或 mtime 变化分别处理。
- 删除同步：默认开启；删除 STRM 后清理索引。
- 并发保护：与事件处理共用索引锁；对同一文件加路径级锁，避免边写边删。
- 报告：`对账完成：新增 N / 更新 N / 删除 N / 失败 N`，并记录到日志。

### 4.8 配置页（`get_form`）

Vuetify 表单分三组：

1. **基础**：启用开关、115 存储名、飞牛实例名、基础地址、签名密钥（只读+重置按钮）
2. **STRM**：STRM 根目录、路径映射（多行文本）、视频扩展、最小大小、媒体类型、媒体库根路径
3. **高级**：直链缓存 TTL、换链 QPS、冷却时间、对账 cron、删除同步开关、调试日志

`get_page`：展示最近 50 条播放日志 + 索引统计 + 手动操作按钮。

### 4.9 刮削附属文件同步（`attachments.py`）

**目的**：MP 整理时把 NFO/海报上传到了 115，而飞牛扫描的是本地 STRM 目录；飞牛会读取本地 NFO，因此必须把附属文件复制到本地。

**同步范围（按目录而非按文件）**：

1. 对每个视频，列出其 115 所在目录（一次 `U115Pan.list`，结果按目录缓存，TTL 60 秒）。
2. 命中以下任一条件的文件被复制到本地同相对目录：
   - 扩展名 ∈ `copy_exts`（默认 `nfo,jpg,jpeg,png,webp,srt,ass,ssa,sub`）
   - 文件名主体（stem）∈ 固定刮削名集合：`poster/fanart/backdrop/banner/logo/thumb/landscape/clearart/disc/tvshow/season/season01..season99`
   - 文件名以该视频文件主名开头（覆盖 MP 的 `<视频名>.nfo`、`<视频名>-poster.jpg`、`<视频名>.zh-CN.srt` 等）
3. 过滤：大小 > `attachment_max_mb`（默认 10MB）跳过；隐藏文件/临时文件（`.tmp`、`*.part`）跳过。
4. 冲突策略：`skip`（默认，本地已存在则跳过）/ `overwrite`（对比 115 mtime 后覆盖）。
5. 复制后写入索引 `attachments`（或 `dir_attachments`），标记 `source=plugin`。

**删除规则**：

- 视频从 115 删除 → 删除该条目的 STRM + 索引中 `source=plugin` 的附属文件；不删除未记录文件。
- 对账时附属文件在 115 已不存在 → 删除本地记录过的副本并移除索引。

**限频与性能**：

- 附属文件下载与直链换链共用全局限频（默认 2 QPS），小文件下载不提高并发。
- 同一目录的附属文件只同步一次（目录级去重），一季剧集不会重复拉取 `tvshow.nfo`/`poster.jpg`。
- 同步失败不阻塞 STRM 生成：STRM 先生效，附属文件在下次对账重试。

**飞牛侧兼容性（待真机验证）**：

- 飞牛读取本地 NFO 的命名兼容性：MP 产物为 `<视频名>.nfo`、`tvshow.nfo`、`season.nfo`，图片为 `<视频名>-poster.jpg` / `poster.jpg` 等，需实测飞牛是否全部识别；若不识别，SPEC 预留"文件名归一化"开关（如统一改名为 `poster.jpg`/`fanart.jpg`）。

### 4.10 运维 API

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET/HEAD | `/play` | 匿名 + 签名 | 302 播放 |
| GET | `/stats` | bear | 索引条数、缓存命中率、最近错误 |
| POST | `/resync` | bear | 立即全量对账 |
| POST | `/rewrite_strm` | bear | 按当前 base_url/密钥全量重写 STRM |
| POST | `/clear_cache` | bear | 清空直链缓存 |

---

## 五、配置项

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | false | 插件总开关 |
| `source_storage` | string | `u115` | 目标存储类型 |
| `fnos_instance` | string | 空 | MoviePilot 中飞牛媒体服务器实例名 |
| `base_url` | string | 空 | MP 对外地址（公网域名，无尾斜杠） |
| `strm_root_local` | string | `/strm_test` | 容器内 STRM 根目录（写入用） |
| `strm_root_fnos` | string | 空 | 飞牛视角 STRM 根目录（刷新用，NAS 宿主路径） |
| `path_mappings` | text | 空 | `115前缀 => 容器内根`，每行一条 |
| `video_exts` | string | `mkv,mp4,ts,m2ts,avi,mov,wmv` | 视频扩展名 |
| `min_size_mb` | int | 100 | 最小文件大小 |
| `media_types` | list | `movie,tv` | 处理类型 |
| `library_roots` | text | 空 | 对账用的 115 媒体库根路径，每行一个 |
| `cache_ttl` | int | 1200 | 直链缓存秒数 |
| `qps` | float | 2.0 | 换链限频 |
| `cooldown_seconds` | int | 60 | 风控冷却 |
| `reconcile_cron` | string | `30 4 * * *` | 对账计划 |
| `copy_exts` | string | `nfo,jpg,jpeg,png,webp,srt,ass,ssa,sub` | 附属文件扩展名白名单 |
| `attachment_max_mb` | int | 10 | 附属文件大小上限 |
| `attachment_conflict` | string | `skip` | 附属文件冲突策略：skip/overwrite |
| `delete_sync` | bool | true | 删除同步 |
| `debug` | bool | false | 调试日志 |
| `secret` | string | 随机生成 | 签名密钥 |

---

## 六、部署方式

1. **插件目录**：开发期用 `PLUGIN_LOCAL_REPO_PATHS` 挂载本地仓库（目录结构 `plugins.v3/cloudstrmkit/`），MP 自动发现热加载。
2. **容器挂载**（必须）：
   - 宿主 STRM 目录 → 容器内固定路径，推荐容器内统一为 `/strm_test`（插件 `strm_root_local` 默认即此）
     示例：`- /path/to/strm:/strm_test`
   - 插件数据目录（默认在 MP 的 `config/plugin_data`）
3. **路径对照**：`strm_root_fnos` 填宿主/飞牛视角路径（如 `/path/to/strm`），用于飞牛刷新时传 `target_path`；容器内路径由插件用 `strm_root_local` 换算。
4. **MP 容器端口**：复用 3001 + 现有 Lucky 反代域名；无需新增端口。
5. **飞牛侧**：媒体库指向宿主 STRM 目录；0.9.1+；用管理员账号配置到 MoviePilot。
6. **对齐项**：`base_url` 必须能从飞牛客户端访问；局域网访问 MP 域名需 NAT 回环或内网 DNS 分流。
7. **目录规划建议**：见 README「目录规划」章节——不绑定 SmartStrm 目录名，迁移时用插件「全量重写 STRM」即可。

### 6.1 两种运行模式（公网 / 无公网）

| 模式 | `base_url` | 可达路径 | 前提 |
|---|---|---|---|
| A 公网直连（默认） | 公网域名（MP 反代入口） | 局域网/外网客户端直接请求 | 有公网入口（IPv4/IPv6 + Lucky 反代） |
| B 内网地址（无公网降级） | 内网 MP 地址（如 `http://<NAS内网IP>:3001`） | 局域网直连；外网经 FN ID 中继时由飞牛服务端在内网解析并 302 | 飞牛服务端会解析 STRM 内网 URL（需实测 V3） |

- 模式切换：改 `base_url` + 执行「全量重写 STRM」即可，无需重新整理。
- **FN Connect 不支持代理 MoviePilot 这类第三方端口**（只代理飞牛已注册服务），因此无公网时不能靠 FN ID 直接反代 MP；模式 B 的原理是让解析发生在内网侧。
- 若公网仅回收 IPv4 而保留 IPv6，优先用 IPv6 域名（AAAA）维持模式 A。

---

## 七、风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| `U115Pan` 私有方法/属性变动 | 换链失效 | 封装隔离（`u115.py` 单点）；降级为自实现（读取宿主存储配置的 access_token + 刷新） |
| 115 UA 绑定规则变化 | 403 | 用请求 UA 换链；缓存按 UA 隔离；实测验证（Day 1） |
| 飞牛刷新接口/管理员约束 | 入库延迟 | 鸭子类型调用 + 全库降级；文档强调管理员账号 |
| MP 异步事件阻塞 | 整个宿主卡顿 | 事件只入队，重活在后台线程 |
| 索引并发写损坏 | 数据不一致 | 原子写 + 锁 + 启动校验 + 对账重建 |
| 飞牛版本升级改接口 | 刷新/播放异常 | 聚焦标准 STRM 302（官方能力），不依赖私有播放接口 |
| 115 风控 | 播放失败 | 缓存、2QPS、冷却、错误分类 |
| 公网 IP 被运营商回收 | STRM 内公网地址不可达 | 切到模式 B（内网地址 + 飞牛服务端解析）；或改用 IPv6/穿透；插件支持改 `base_url` 后全量重写 |

---

## 八、预研结论与验证清单

- 预研结论（MoviePilot v3.2.3 源码核实 13 条）、工程结构与开发工作流、补齐的边界规则（R1–R15）、返工风险清单、V1–V12 验证清单、Day 0 预研任务：**全部见 [`docs/DESIGN-REVIEW.md`](DESIGN-REVIEW.md)**。
- 架构假设已全部有源码支撑：匿名 302 路由、UA 换链、宿主 115 授权复用、飞牛刷新、事件驱动、本地插件加载。
- 实现顺序约束：**Day 0 先验证本地插件仓安装（V10），再写业务逻辑**；T3 先验证 V1/V4/V7/V9 再写 `u115.py`。

关键预研结论摘要：

| # | 结论 | 对实现的影响 |
|---|---|---|
| P7 | `U115Pan._request_api` 自带 token 刷新、下载端点独立限流、风控共享冷却、有限重试 | 换链直接复用，不自建限流 |
| P8 | `U115Pan.access_token` 自动续期 | 插件不管理 115 授权生命周期 |
| P9/P10 | `RefreshMediaItem(title/year/type/category/target_path)` + `refresh_library_by_items` | 飞牛刷新按路径匹配媒体库，需双路径换算 |
| P5 | 事件支持 async handler | 事件处理可 async，但重活仍入后台队列 |
| P1/P2 | 本地插件仓 = `package.v3.json` + `plugins.v3/<id>/` | 工程结构按此定稿（见 DESIGN-REVIEW 第二节） |

完整清单见 DESIGN-REVIEW：V1–V12。
