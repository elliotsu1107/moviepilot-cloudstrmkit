# moviepilot-cloudstrmkit

**CloudStrmKit** —— 飞牛影视（fnOS / 飞牛影视）专用的 MoviePilot 插件：**整理完成 → 生成 115 网盘 STRM → 飞牛媒体库实时刷新 → 播放 302 直连 115**。

## 解决什么问题

- 现有商业方案（SmartStrm Pro）功能冗余且收费，302 代理层对本场景（只有飞牛影视 + 115 + qB）没必要。
- SmartStrm 生成 STRM 依赖手动/凌晨定时任务，整理完成的影片不能立即可见可播。
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

原则：**容器内路径固定、宿主路径可配**。插件写文件用容器内 `/strm`（默认），飞牛刷新用配置的宿主路径（`strm_root_fnos`）。这样以后换目录只改挂载和配置，代码不动。

### 推荐目录

| 项 | 建议 |
|---|---|
| 宿主 STRM 根 | 独立媒体目录（如 `/path/to/strm`），不要放在 MP 的 `config` 目录内，也不要放在下载目录内 |
| 容器内 STRM 根 | 固定 `/strm`（插件 `strm_root_local` 默认值） |
| 飞牛媒体库 | 直接指向宿主 STRM 根 |
| 目录层级 | 与 115 路径保持一致，不要多级嵌套过深（路径映射难维护） |

### 目录结构（与 115 路径一致）

```
/path/to/strm/            # 宿主 STRM 根 → 容器内 /strm
├── movie/电影/华语电影/<影片>/<影片>.strm
├── movie/电影/外语电影/<影片>/<影片>.strm
└── tv/电视剧/国产剧/<剧名>/Season 1/<集>.strm
```

### 挂载与配置对应

```yaml
# docker-compose（MP 容器）
volumes:
  - /path/to/strm:/strm
```

| 插件配置 | 值 |
|---|---|
| `strm_root_local` | `/strm` |
| `strm_root_fnos` | `/path/to/strm` |
| `path_mappings` | `/movie => /strm/movie`<br>`/tv => /strm/tv` |

### 测试隔离与切换策略

开发期与验收期采用“双目录、双媒体库”隔离，避免影响正在使用的旧方案：

| 阶段 | 旧目录/旧媒体库 | 新目录/新媒体库 | 插件行为 |
|---|---|---|---|
| 阶段 1：开发与验证 | 保留，飞牛影视照常使用 | 独立测试目录 + 飞牛新建测试媒体库 | 只写入新目录，不碰旧目录 |
| 阶段 2：验收通过 | 停用旧方案 | 切换为正式目录 | 迁移旧 STRM，或由插件「全量重新生成」新一版 STRM |
| 阶段 3：收尾 | 删除旧方案软件与旧目录 | 正式目录 | 飞牛媒体库指向新目录（等价重建媒体库） |

> 阶段 2 的媒体库路径切换等价于换库，观看进度可能受影响；建议先在阶段 1 完整验证播放/刷新/对账后再切换。

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

设计阶段（v0.1.0）。代码尚未开始，等待方案确认。
