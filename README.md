# moviepilot-cloudstrmkit

**CloudStrmKit** —— 飞牛影视（fnOS / 飞牛影视）专用的 MoviePilot 插件：**整理完成 → 生成 115 网盘 STRM → 飞牛媒体库实时刷新 → 播放 302 直连 115**。

## 解决什么问题

- 现有商业方案功能冗余且收费，302 代理层对本场景（只有飞牛影视 + 115 + qB）没必要。
- 旧方案生成 STRM 依赖手动/凌晨定时任务，整理完成的影片不能立即可见可播。
- 现成开源插件（如 P115StrmHelper）面向 Emby 生态，功能全集但体量大、依赖重、GPL 传染，且未针对飞牛刷新做定向适配。

## 目标链路

```
qB 下载完成 → MoviePilot 整理并上传 115（本地保留做种）
    → 插件监听整理完成事件，生成 .strm（内容为带签名的 302 地址）
    → 调飞牛影视 API 刷新媒体库
    → 飞牛各端播放时请求插件 /play → 302 到 115 直链（NAS 只出 KB 级响应）
```

## 项目目录与部署副本

本项目采用“本地开发 + NAS 部署副本”双目录：

| 角色 | 路径 | 说明 |
|---|---|---|
| 本地项目（Git 仓库） | `<本地项目根>` | 唯一开发目录，与 GitHub 仓库一致 |
| 部署副本（MP 读取） | `<部署副本目录>` | 即 NAS 宿主 `<NAS插件目录>/moviepilot-cloudstrmkit`；仅作为 MoviePilot 的本地插件仓，不是项目主目录，映射可能变动 |

### MoviePilot Compose 配置

给 MP 容器增加一条挂载和两个环境变量（改动前备份原文件）：

```yaml
    volumes:
      - /path/to/moviepilot/plugins-src:/plugins-src
    environment:
      - PLUGIN_LOCAL_REPO_PATHS=/plugins-src/moviepilot-cloudstrmkit
      - PLUGIN_AUTO_RELOAD=true
```

### 开发工作流

1. 在 `<本地项目根>` 修改代码并提交 Git
2. 运行同步脚本，把本地项目镜像到部署副本（部署目录不在脚本里硬编码，用参数或环境变量指定）：

   ```powershell
   $env:CLOUDSTRMKIT_DEPLOY_DIR = '\\NAS\share\moviepilot\plugins-src\moviepilot-cloudstrmkit'
   powershell -ExecutionPolicy Bypass -File scripts\sync-to-nas.ps1
   # 或：... -Target '\\NAS\share\...\moviepilot-cloudstrmkit'
   ```

3. MP → 插件 → 插件市场 → 本地插件 → 安装/同步 CloudStrmKit
4. 需要时在插件详情页点「重新加载」

> 部署副本随时可丢弃重建：删掉 NAS 目录后重新执行同步脚本即可，项目数据以本地 Git 仓库为准。

## 运行模式

| 模式 | `base_url` | 适用 | 说明 |
|---|---|---|---|
| A 公网直连（默认） | 公网域名（MP 反代入口） | 有公网 IPv4/IPv6 | 局域网/外网客户端直接请求，无需反代飞牛 |
| B 无公网降级 | 内网 MP 地址 | 公网 IP 被回收 | STRM 写内网地址；局域网直连；外网经 FN ID 时由飞牛服务端在内网解析并 302（需实测确认） |

注意：**FN Connect 不支持代理 MoviePilot 这类第三方/Docker 端口**，只代理飞牛已注册服务。因此无公网时不能靠 FN ID 直接反代 MP；模式 B 的原理是让解析发生在内网侧。若公网仅回收 IPv4 而保留 IPv6，优先继续用模式 A（AAAA 记录）。

模式切换：改 `base_url` → 执行「全量重写 STRM」，无需重新整理。

## 目录规划（STRM 落点）

原则：**容器内路径固定、宿主路径可配**。插件写文件用容器内 `/strm_test`（默认），飞牛刷新用配置的宿主路径（`strm_root_fnos`）。这样以后换目录只改挂载和配置，代码不动。

### 推荐目录

| 项 | 建议 |
|---|---|
| 宿主 STRM 根 | 独立媒体目录（如 `/path/to/strm`），不要放在 MP 的 `config` 目录内，也不要放在下载目录内 |
| 容器内 STRM 根 | 固定 `/strm_test`（插件 `strm_root_local` 默认值） |
| 飞牛媒体库 | 直接指向宿主 STRM 根 |
| 目录层级 | 与 115 路径保持一致，不要多级嵌套过深（路径映射难维护） |

### 目录结构（与 115 路径一致）

```
/path/to/strm/            # 宿主 STRM 根 → 容器内 /strm_test
├── movie/电影/华语电影/<影片>/<影片>.strm
├── movie/电影/外语电影/<影片>/<影片>.strm
└── tv/电视剧/国产剧/<剧名>/Season 1/<集>.strm
```

### 挂载与配置对应

```yaml
# docker-compose（MP 容器）
volumes:
  - /path/to/strm:/strm_test
```

| 插件配置 | 值 |
|---|---|
| `strm_root_local` | `/strm_test` |
| `strm_root_fnos` | `/path/to/strm` |
| `path_mappings` | `/movie => /strm_test/movie`<br>`/tv => /strm_test/tv` |

### 测试隔离与切换策略

开发期与验收期采用“双目录、双媒体库”隔离，避免影响正在使用的旧方案：

| 阶段 | 旧目录/旧媒体库 | 新目录/新媒体库 | 插件行为 |
|---|---|---|---|
| 阶段 1：开发与验证 | 保留，飞牛影视照常使用 | 独立测试目录 + 飞牛新建测试媒体库 | 只写入新目录，不碰旧目录 |
| 阶段 2：验收通过 | 停用旧方案 | 切换为正式目录 | 迁移旧 STRM，或由插件「全量重新生成」新一版 STRM |
| 阶段 3：收尾 | 删除旧方案软件与旧目录 | 正式目录 | 飞牛媒体库指向新目录（等价重建媒体库） |

> 阶段 2 的媒体库路径切换等价于换库，观看进度可能受影响；建议先在阶段 1 完整验证播放/刷新/对账后再切换。

## 使用说明

### 安装（本地插件仓模式）

1. MoviePilot 容器挂载本地插件仓目录，并设置环境变量：

   ```yaml
   volumes:
     - /path/to/plugins-src:/plugins-src
   environment:
     - PLUGIN_LOCAL_REPO_PATHS=/plugins-src/moviepilot-cloudstrmkit
   ```

2. 插件市场 → 本地插件 → 安装 CloudStrmKit
3. 电影/电视剧整理完成后插件自动生成 STRM（无需手动触发）

### 必填配置

| 配置项 | 说明 | 示例 |
|---|---|---|
| `source_storage` | MoviePilot 中 115 存储的名称 | `115网盘` |
| `fnos_instance` | 飞牛媒体服务器实例名（需管理员账号） | `MyFnos` |
| `base_url` | 对外基础地址（播放器可访问的 MP 地址，无尾斜杠） | `https://media.example.com` |
| `strm_root_local` | 容器内 STRM 根目录 | `/strm` |
| `strm_root_fnos` | 飞牛视角的 STRM 根目录（宿主路径） | `/path/to/strm` |
| `path_mappings` | 115 前缀 → 容器内根，每行一条 | `/movie => /strm/movie` |
| `library_roots` | 对账用的 115 媒体库根路径 | `/movie/电影`、`/tv/电视剧` |

### 处理流程

1. MoviePilot 整理完成 → `TransferComplete` 事件
2. 插件过滤 115 存储与视频扩展 → 后台队列生成 `.strm`（内容为签名播放 URL）
3. 同步 115 同目录的 NFO/海报/字幕到本地（可配）
4. 调用飞牛 `refresh_library_by_items` 刷新媒体库
5. 播放器请求 `/play` → 插件用**请求方 UA**换取 115 直链 → 302

### 插件接口

| 接口 | 鉴权 | 说明 |
|---|---|---|
| `/play?f=&e=&sig=` | 签名 | 302 播放（GET/HEAD） |
| `/play/logs` | bear | 播放日志 |
| `/reconcile` | bear | 手动触发对账（`?paths=` 可按 115 路径前缀精准执行） |
| `/reconcile/status` | bear | 对账状态与结果 |
| `/delete/pending` | bear | 待确认删除列表（超过阈值挂起） |
| `/delete/confirm` | bear | 确认删除挂起的 STRM |
| `/delete/cancel` | bear | 取消删除（清空列表） |
| `/rebuild` | bear | 全量重写 STRM（改 `base_url`/密钥后使用） |
| `/rebuild/status` | bear | 全量重写状态 |
| `/attachments/orphans` | bear | 扫描孤儿附属文件（只读） |
| `/attachments/cleanup?paths=` | bear | 确认清理孤儿附属文件 |
| `/diag/event-log` | bear | 最近整理事件载荷 |
| `/diag/u115` | bear | 115 换链探针（UA 绑定验证） |
| `/diag/u115-list` | bear | 115 目录列举诊断 |
| `/diag/reconcile-source` | bear | 对账数据源诊断 |
| `/diag/notify-test` | bear | 通知服务测试 |
| `/diag/clear-events` | bear | 清空事件记录 |

### 已知限制

- **115 直链与 UA 绑定**：播放请求的 UA 必须原样用于换链（插件已自动处理）；更换播放器一般不影响
- **直链有效期短**：直链缓存默认 20 分钟，过期自动重新换链；STRM 本身长期有效（默认 365 天）
- **115 风控**：全库扫描属于高频操作，日常请用 `history` 对账模式；`scan` 模式仅用于兜底（较慢，建议定时执行）
- **删除同步仅 `scan` 模式生效**：`history` 模式只补漏不删除；批量删除默认挂起等待人工确认
- **飞牛刷新需管理员账号**：非管理员实例会刷新失败
- **路径映射必须覆盖媒体库根**：未映射的 115 路径会被跳过并记录警告
- **首次全量建立索引依赖整理历史**：若历史缺失，需用 `scan` 模式（较慢）

### 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 播放 403 | 签名无效或链接过期 | 重新生成 STRM |
| 播放 404 | 索引缺少该文件 | 执行一次对账补漏 |
| 播放 502 | 115 授权失效 / 换链失败 | 检查 MP 的 115 存储授权 |
| 播放 503 | 115 限频 | 等待冷却或降低 `qps` |
| 飞牛不刷新 | 实例名错误 / 非管理员 | 检查 `fnos_instance` 与飞牛账号 |
| 对账很慢 | 用了 `scan` 模式 | 改用 `history` 模式 |

## 许可证

本项目采用 [MIT License](LICENSE)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/PRD.md](docs/PRD.md) | 需求与验收标准 |
| [docs/SPEC.md](docs/SPEC.md) | 技术方案 |
| [docs/DESIGN-REVIEW.md](docs/DESIGN-REVIEW.md) | 预研结论、工程结构、边界规则、返工风险、验证清单 |
| [docs/PLAN.md](docs/PLAN.md) | 实施计划 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 变更记录 |
| [docs/CONVENTIONS.md](docs/CONVENTIONS.md) | 项目约定声明 |

## 状态

v0.5.4：核心链路已完成并通过真机验证——整理完成 → 生成 STRM → 飞牛刷新 → 302 播放（内网、外网、安卓客户端播放与拖动进度条均正常，MP 重启后自动恢复）。剩余：无公网降级模式验证与验收清单收尾。
