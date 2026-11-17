# 发布检查清单

## 代码与版本

- [x] P0/P1 修复与回归覆盖已提交；本机 `python -m pytest -q` 通过（当前 418 passed / 2 skipped）。
- [ ] 推送后确认 CI 在 Python 3.12、3.14 全绿。
- [x] `LICENSE`（MIT，yuyanhao）、安全政策、贡献说明和用户文档已核对。
- [x] 复核本版本 `CHANGELOG.md` 顶部段落；`VERSION`、README 发布链接及 Docker/更新命令一致。
- [x] `python tools/check_release.py`、`ruff check .`、`python settings.py --allow-missing-credentials` 通过；发布工作流只提取当前版本说明。
- [x] 核对 `PDF_INPUT_MAX_PAGES` / `PDF_TASK_MAX_PAGES` 与 `BOOK_MAX_PDF_MB` / `BOOK_LIBRARY_MAX_MB` 的默认值或部署覆盖值。
- [x] 代码审查通过；无真实密钥、测试用户数据或生成目录进入提交。

## 仓库设置

- [ ] GitHub 描述、topics、主页和社交预览图已补齐。
- [ ] Secret scanning、push protection、Dependabot alerts 已启用。
- [ ] `main` 要求 CI 通过并禁止 force push；`v*` 标签保护规则已设置。
- [ ] GitHub `release` environment 的必需审阅人已配置（或明确接受无人审阅）。
- [ ] 确认不使用 Wiki 时关闭 Wiki。
- [x] 已按 QQ 官方文档于 2026-11-17 复核群聊/单聊窗口、主动消息频控及机器人权限；核对记录见 `docs/qq-platform-rules.md`。

## 发布与验收

- [ ] 合并到 `main` 前确认发布工作流的 VERSION 变更会创建 **v1.0.0** 标签；不要提前推送未经验收的版本。
- [ ] 检查 GitHub Release 只包含当前版本的说明且标记为 Latest。
- [ ] 在干净 Ubuntu 环境验证安装、配置、服务启停与日志。
- [ ] 从 v0.2.6 数据副本升级，确认任务、收藏、附件和书库可读。
- [ ] 验收 Docker Compose 安装、持久化、更新和恢复流程。
- [ ] 保留回滚包；发布后观察日志、队列、磁盘和模型费用。
