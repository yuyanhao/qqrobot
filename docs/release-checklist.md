# 发布检查清单

## 代码与版本

- [x] 每份待读取 PDF 均须明确选页，单份和单任务合计最多 5 页；最新提交的 GitHub Actions 已通过。
- [x] `LICENSE`（MIT，yuyanhao）、安全政策、贡献说明和用户文档已核对。
- [x] 发布版本使用 `VERSION=1.0.0`；版本号、变更记录和升级命令已同步。
- [x] `python tools/check_release.py`、`ruff check .`、`python settings.py --allow-missing-credentials` 通过；发布工作流只提取当前版本说明。
- [x] 确认 `PDF_INPUT_MAX_PAGES` / `PDF_TASK_MAX_PAGES` 默认值均为 5、配置不能超过 5；核对书库配额及部署覆盖值。
- [x] 代码审查通过；无真实密钥、测试用户数据或生成目录进入提交。
- [x] Python 3.12/3.14 CI 各通过 427 项（run 44095712574 / 44095722262）。

## 仓库设置

- [x] GitHub 描述与 topics 已配置；[PR #1](https://github.com/yuyanhao/qqrobot/pull/1) 将转为 ready 并以 squash 合并。
- [x] Secret scanning、push protection、Dependabot alerts/security updates 已启用。
- [x] `main` 要求 `Python 3.12` / `Python 3.14` 状态检查、线性历史、禁止 force push；`v*` 标签 ruleset 已设置。
- [x] GitHub `release` environment 的必需审阅人已配置为 `yuyanhao`。
- [ ] 社交预览图和 homepage 未配置；确认是否关闭未使用的 Wiki。
- [x] 已按 QQ 官方文档于 2026-11-17 复核群聊/单聊窗口、主动消息频控及机器人权限；核对记录见 `docs/qq-platform-rules.md`。

## 发布与验收

- [x] 发布提交和 PR 检查通过后，合并 `main` 并创建 v1.0.0 Release。
- [ ] 检查 GitHub Release 只包含当前版本的说明且标记为 Latest。
- [ ] 在干净 Ubuntu 环境验证安装、配置、服务启停与日志。
- [ ] 从 v0.2.6 数据副本升级，确认任务、收藏、附件和书库可读。
- [ ] 验收 Docker Compose 安装、持久化、更新和恢复流程。
- [ ] 保留回滚包；发布后观察日志、队列、磁盘和模型费用。

> 真实 QQ/DeepSeek、systemd 和 Docker 的完整验收在发布后的云端部署阶段完成；发布前只以 CI、版本门禁和离线配置检查为准。
