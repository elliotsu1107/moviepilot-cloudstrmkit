---
level: L1
commit: zh-conventional
docs_root: docs
versioning: semver
repo_split: single
changelog_sync: single
evidence: none
release: none
i18n: 单语
---

# 项目约定（CONVENTIONS）

> 更新日期：2026-11-01
> 本文件是**本项目**的最高优先级约定；与 skill 默认、全局宪章冲突时以本文件为准。

## 一、档位选择

- 档位：**L1（初始）**。理由：个人自用插件，单仓、无公开发布流程、无多语要求。
- 必须项：根 `README.md`；本项目在此之上保留 `docs/PRD.md`、`docs/SPEC.md`、`docs/PLAN.md`、`docs/CHANGELOG.md`，以对齐全局 Agent 宪章的中大型任务流程。

## 二、仓库模式

| 项 | 值 |
|---|---|
| 模式 | 单仓（single） |
| 主仓 | 暂不公开（本地仓库） |
| 发布渠道 | 无（本地插件仓加载） |

## 三、提交信息

- 格式：`type(中文scope): 中文动宾描述`，描述不超过 50 字符。
- type 取值：feat / fix / docs / refactor / test / chore / perf / build。
- 示例：`docs(初始化): 建立 PRD 与 SPEC 文档`。

## 四、版本与变更记录

- 版本：SemVer，当前 v0.x 阶段；`0.y.z` 中 y 递增表示功能里程碑，z 表示修复。
- 变更记录统一写 `docs/CHANGELOG.md`，不额外维护根目录 CHANGELOG。

## 五、证据归档

- L1 不设固定 evidence 目录；关键验证证据（命令输出、抓包结论、飞牛播放实测截图）记录到 `docs/PLAN.md` 对应任务或 `docs/CHANGELOG.md`。

## 六、已裁决冲突

| 冲突 | 裁决 | 依据 |
|---|---|---|
| 全局宪章要求六分区 docs；本技能 L1 只强制 README | 采用最小文档集：README + PRD/SPEC/PLAN/CHANGELOG | 个人自用项目，避免过度工程化（用户明确偏好） |
