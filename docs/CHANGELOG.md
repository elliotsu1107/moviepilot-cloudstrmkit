<!-- 实例化为 docs/CHANGELOG.md -->
# 变更记录（CHANGELOG）

> 格式参考 Keep a Changelog；版本遵循 SemVer（v0.x 阶段）。

## [v0.5.4] - 2026-11-01

### 对账（T10）

- 新增 `history` 模式（默认）：基于 MoviePilot 整理历史（`app.sdk.queries` 分页快照）批量补漏；实测 2473 个 115 视频 **3.8 秒**完成。
- 保留 `scan` 模式（扫描 115 全库）：仅此模式清理孤儿，且扫描失败或零结果时**禁止清理**。
- 对账批量落盘索引；不再逐个补查 pickcode（改为播放时按需补查并回填）。
- 新增 `/diag/reconcile-source` 数据源诊断接口。

### 事故与恢复

- 联调中对账因 `U115Pan.list` 调用方式错误导致扫描为空，误删一条测试 STRM；已通过安全阀修复与 `history` 模式全量重建（2473 个 STRM）恢复，并实测《仙逆》S01E165 播放正常（302 + 同 UA 200 / 异 UA 403）。

## [v0.4.3] - 2026-11-01

### 修复（对账安全与性能）

- **安全**：目录扫描失败或扫描结果为 0 时，禁止执行孤儿清理（联调中曾因 `U115Pan.list` 调用方式错误导致扫描为空，误删一条测试 STRM，已修复并通过重新对账恢复）。
- **正确性**：v3 的 `U115Pan.list()` 接收 `FileItem` 而非 `Path`，改为先 `get_item()` 再 `list()`。
- **性能**：对账复用目录列举结果中的 `pickcode`；附属文件同步缓存目录列表，避免每个视频重复列举。

### 新增

- 定时对账（`get_service` 注册 cron）与手动触发接口 `/reconcile`、状态接口 `/reconcile/status`。
- `/diag/u115-list` 目录列举诊断接口。

## [v0.3.0] - 2026-11-01

### 新增

- 附属文件同步（`attachments.py`）：把 115 同目录的 NFO、海报、字幕按白名单复制到本地 STRM 目录；支持大小上限、冲突策略（skip/overwrite）、目录级索引记录与覆盖整理时的旧文件清理。

## [v0.2.2] - 2026-11-01

### 验证结论（核心链路，真实整理《仙逆》S01E165）

| 验证项 | 结果 |
|---|---|
| V4 事件载荷 | ✅ 载荷为 dict，成功解析 `target_item.storage=u115`、115 路径、`pickcode`、`file_list_new`、`media.title/year/type` |
| V5 路径映射 | ✅ 生成到 `/strm_test/tv/电视剧/国漫/仙逆 (2023)/Season 1/仙逆 S01E165 2160p.ADWeb.strm`，宿主侧可见 |
| STRM 内容 | ✅ 含 `base_url + /play?f=&e=&sig=` 签名 URL |
| 302 播放 | ✅ `/play` 返回 302，`Location` 为 115 CDN 直链，`Content-Disposition` 正确 |
| UA 绑定 | ✅ 同 UA 请求直链返回 206 |

### 修复

- 媒体类型转换：事件载荷中的 `MediaType.TV` 字符串无法直接构造枚举，改为兼容 `MediaType.xxx` / `tv` / `movie` / 中文值。
- 空索引真值判断（`__len__` 为 0 导致误判索引不存在）。
- 事件载荷 dict/对象双兼容。

## [v0.2.0] - 2026-11-01

### 新增

- `u115.py`：115 直链客户端。token 取自宿主 115 存储，请求直连 115 开放平台并显式禁用环境代理；按 `(pickcode, UA)` 缓存直链；全局 QPS 限频；错误分类（授权/限频/未找到/网络）。
- `strm.py`：路径映射（最长前缀优先）、STRM 签名 URL 构建与校验、飞牛视角路径换算、STRM 原子写入/删除。
- `fnos.py`：飞牛媒体服务器实例定位、`refresh_library_by_items` 刷新、失败降级全库扫描、3 秒去抖合并。
- `redirect.py`：`/play` 匿名路由，验签/过期校验 → 查索引 → 请求方 UA 换链 → 302；播放日志环形记录。
- 事件接入：`TransferComplete` → 过滤存储与视频扩展 → 后台队列串行生成 STRM → 落索引 → 提交飞牛刷新。
- 配置：签名密钥留空时自动生成 32 位随机密钥并写回配置。

### 说明

- 实测确认 115 直链与换链 UA 绑定，因此 `/play` 必须使用请求方 UA 换链。
- 宿主 `U115Pan._request_api` 在带全局代理的容器中换链失败，故 115 请求改为插件内直连。

## [v0.1.4] - 2026-11-01

### 验证结论（T3）

- **V1 通过**：115 直链与换链时的 User-Agent 绑定。实测 3 组 UA：同 UA 请求返回 206，异 UA 请求同一直链返回 403。
- **环境问题**：宿主 `U115Pan._request_api` 换链在本环境返回空（直连 115 开放平台正常，判断为 MP 容器 `HTTP_PROXY` 影响宿主请求链路）。
- 结论：T4 改为**插件内直连 115 开放平台**（显式禁用环境代理，以请求方 UA 换链）；`U115Pan` 仅用于取 token 与按路径查 pickcode。

### 变更

- `/diag/u115` 增强：115 直连测试、按 UA 分组换链 + 同 UA/异 UA 交叉验证。
- 执行开发期版本自增（0.1.1 → 0.1.4）。

## [v0.1.1] - 2026-11-01

### 新增

- T2：配置模型 `CloudStrmKitConfig`、STRM 索引（JSON 原子持久化）、插件配置页、详情页统计。
- T3：`TransferComplete` 事件载荷记录（`/diag/event-log`）、115 换链探针（`/diag/u115`，验证 UA 绑定假设）。
- 容器内 STRM 根目录默认值改为 `/strm_test`（与旧目录隔离的测试策略）。

### 说明

- **开发期版本自增策略**：本地插件仓的更新依赖版本号变化，每次需要把开发代码更新到 MoviePilot 运行目录时，必须递增 `package.v3.json` 的 `version` 与插件 `plugin_version`，否则市场不会显示“更新”。

## [v0.1.0] - 2026-11-01

### 新增

- 建立项目文档：`README.md`、`docs/PRD.md`、`docs/SPEC.md`、`docs/PLAN.md`、`docs/CONVENTIONS.md`。
- 确定技术路线：MoviePilot v3 插件形态，复用宿主 115 存储授权与飞牛影视客户端，零新增外部服务。
- 确定对账策略：MP 整理事件驱动 + 定时全量对账（方案 A）。
- 确定刮削文件策略：MP 上传到 115 的 NFO/海报同步到本地 STRM 目录（飞牛读取本地 NFO）。

### 设计决策记录

- 插件定名 **CloudStrmKit**（目录 `cloudstrmkit`），保留后续扩展网盘管理/直链工具的空间。
- 不采用 P115StrmHelper 直接复用：体量大（7400+ 行）、GPL-3.0、依赖重（p115client/p115center/mfusepy 等），且其复杂度主要服务于非本场景的 115 全盘监听。
- 不吸收 P115Disk 的存储驱动：它是自定义存储模块（约 1500 行 + p115client 依赖），与本插件"302 播放"职责不同；如需更强的网盘管理，建议直接安装 P115Disk，本插件按存储名兼容；自研网盘管理页列入 v0.2 备选。
- 不做 Emby/Jellyfin/Plex 302 反代与飞牛 8005 Emby 兼容端点：播放端仅飞牛官方客户端。
- 播放走"客户端直连公网域名 → 插件 302 → 115"，不反代整个飞牛服务，因此不存在 现有方案文档所述"不能用 FN ID"的反代入口问题。
- STRM 采用双路径配置：容器内写入路径 `strm_root_local`（默认 `/strm_test`）+ 飞牛视角路径 `strm_root_fnos`，便于后续更换 STRM 目录而不改代码。

### 设计审查（开工前）

- 完成 MoviePilot v3.2.3 源码预研，核实 13 条关键事实（插件加载、事件、115 换链、飞牛刷新、本地插件仓等），见 `docs/DESIGN-REVIEW.md`。
- 工程结构定稿：项目根作为本地插件市场仓库（`package.v3.json` + `plugins.v3/cloudstrmkit/`）。
- 补齐 R1–R15 边界规则（覆盖整理、改名移动、整季合并、目录级附属文件生命周期、签名过期重写、索引损坏恢复等）。
- 输出返工风险清单与对策、V1–V12 验证清单、Day 0 预研任务（需主人配合 4 项）。
- 无公网降级模式（模式 B）纳入 v0.1.0。

### 部署准备（2026-11-01）

- 修改 MoviePilot 的 `docker-compose.yml`：新增 `plugins-src` 挂载、`PLUGIN_LOCAL_REPO_PATHS`、`PLUGIN_AUTO_RELOAD`（原文件已备份）。
- 记录开发工作流：本地仓 → 市场安装 → 改源码后同步/重载。
- 明确测试策略：飞牛新建"测试媒体库"指向独立子目录，插件测试配置独立，不影响正式媒体库。

### 开发骨架（Day 0，2026-11-01）

- 建立插件工程骨架：`package.v3.json`（本地插件市场索引，key=CloudStrmKit）+ `plugins.v3/cloudstrmkit/`（空骨架 `__init__.py`，含 `/ping` 健康检查；`pyproject.toml` 无第三方依赖）。
- 明确双目录流程：`<本地项目根>` 为唯一项目根（Git 仓库），`<部署副本目录>` 为 MoviePilot 读取的部署副本；新增 `scripts/sync-to-nas.ps1` 一键镜像。
- MoviePilot compose：新增 `plugins-src` 挂载、`PLUGIN_LOCAL_REPO_PATHS` 指向项目根、`PLUGIN_AUTO_RELOAD=true`。
- 待验证：MP 插件市场「本地插件」安装 CloudStrmKit、`/api/v1/plugin/cloudstrmkit/ping` 返回正常。

### 文档脱敏与历史重建（2026-11-01）

- 项目文档与代码移除真实环境信息（NAS 路径、本机路径、飞牛实例名），统一替换为占位符；`scripts/sync-to-nas.ps1` 改为通过 `-Target` 参数或环境变量 `CLOUDSTRMKIT_DEPLOY_DIR` 指定部署目录，不再硬编码机器路径。
- GitHub 仓库历史重建为单一干净提交并强制推送；如需彻底清除旧 dangling 提交，可在 GitHub 删除仓库后重建。
- 明确测试隔离策略：阶段 1 保留旧媒体库与旧 STRM 目录（飞牛照常使用），插件只写新目录并在飞牛新建测试媒体库验证；阶段 2 验收通过后再切换/迁移飞牛媒体库。

### 待办

- Day 0 预研（本地插件仓安装验证等 4 项）。
- 代码实现（见 `docs/PLAN.md` T1~T12），等待开工通知。
