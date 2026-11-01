<!-- 实例化为 docs/CHANGELOG.md -->
# 变更记录（CHANGELOG）

> 格式参考 Keep a Changelog；版本遵循 SemVer（v0.x 阶段）。

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
- 播放走"客户端直连公网域名 → 插件 302 → 115"，不反代整个飞牛服务，因此不存在 SmartStrm 文档所述"不能用 FN ID"的反代入口问题。
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
