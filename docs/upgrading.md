# 升级指南

## 从 v0.2.6 升级到 v1.0.0

先按[备份指南](backup.md)停止服务并备份 `.env`、代码和整个 `data` 目录。升级会保留 SQLite 任务记录、收藏、附件和共享书库；应用启动时按需迁移数据库结构。**不要用 `.env.example` 覆盖现有 `.env`。**

v1.0.0 的命令变化：

- `/收集` 只负责收集；收齐后用 `/解题 收集题号` 解题，或 `/查错 收集题号` 查错。
- `/开始` 保留为 `/解题` 的别名。
- `/查错 收集题号` 自动识别题目、草稿和同页材料；识别不准时可用 `/查错 收集题号 1题 2-4稿` 指定。
- `/取消 题号` 会级联取消由该题派生的收集、排队或运行中任务；不带题号的 `/取消` 取消最近一个任务。
- 原来“先发题目图、再发草稿图、最后 /开始”的分步查错流程已移除。
- 每份要读取的 PDF（题面、书库引用、草稿、补充/追问和附加材料）都必须明确写出物理页码；单份 PDF 和整项任务合计最多 5 页。`PDF_INPUT_MAX_PAGES`、`PDF_TASK_MAX_PAGES` 可在 1–5 之间调低，不能调高。没有明确页码的旧任务在重试时会被拒绝；请改为例如 `/引用 B0001 第3-5页` 或在附件说明里写「只读第3-5页」。管理员书库入库只保存 PDF，不读取页面，不需要选页。

## systemd 升级（默认 `/opt/qq-math-bot`）

```bash
sudo systemctl stop qqbot 2>/dev/null || true
sudo systemctl stop qq-math-bot
stamp=$(date +%Y%m%d-%H%M%S)
sudo tar -czf "/root/qq-math-bot-before-v1.0.0-$stamp.tar.gz" -C /opt/qq-math-bot .env data
sudo tar -czf "/root/qq-math-bot-code-before-v1.0.0-$stamp.tar.gz" -C /opt/qq-math-bot --exclude=data --exclude=.env .
cd /tmp
curl -fL https://github.com/yuyanhao/qqrobot/archive/refs/tags/v1.0.0.tar.gz -o qqrobot-v1.0.0.tar.gz
rm -rf qqrobot-v1.0.0
mkdir -p qqrobot-v1.0.0
tar -xzf qqrobot-v1.0.0.tar.gz --strip-components=1 -C qqrobot-v1.0.0
cd qqrobot-v1.0.0
sudo bash setup.sh
sudo -u qqbot /opt/qq-math-bot/.venv/bin/python /opt/qq-math-bot/qq_panel.py --apply
```

`setup.sh` 只在 `/opt/qq-math-bot/.env` 不存在时复制模板；已有 `.env`、`data/`、题目、附件、收藏和书库不会被覆盖。安装脚本会更新 Python 依赖和 systemd 单元，然后启动 `qq-math-bot`；它要求先停止旧的 `qqbot` 服务。

检查：

```bash
sudo systemctl status qq-math-bot --no-pager
sudo journalctl -u qq-math-bot -n 100 --no-pager
grep -x 1.0.0 /opt/qq-math-bot/VERSION
```

## Docker Compose 升级

在现有 Compose 项目目录执行，`.env` 由 `env_file` 读取，保持原文件不变：

```bash
cp .env "/root/qqbot-env-before-v1.0.0-$(date +%Y%m%d-%H%M%S)"
docker compose stop
volume="$(docker volume ls -q --filter label=com.docker.compose.volume=qqbot-data | head -n 1)"
test -n "$volume"
docker run --rm -v "$volume:/data:ro" -v "$PWD:/backup" alpine:3.22 \
  tar -czf "/backup/qqbot-data-before-v1.0.0.tar.gz" -C /data .
git fetch --tags
git checkout v1.0.0
docker compose up -d --build
docker compose exec qqbot python qq_panel.py --apply
```

不要执行 `docker compose down -v`，它会删除数据卷。部署后确认 `.env` 的校验和未变化，并检查 `docker compose logs --tail=100`。

## 验证

1. 服务状态和日志无 traceback；`VERSION` 为 `1.0.0`。
2. 在 QQ 私聊和群聊分别验证：`/帮助`、文字题、图片题、指定 PDF 页、查询、追问、取消和结果领取。
3. 验证新流程：`/收集` 多张材料 → `/解题 收集号`；`/收集` 题目和草稿 → `/查错 收集号`；`/取消`。
4. 面板显示 `/查错`、`/解题`、`/取消` 的新说明。

## 回滚

停止新服务，恢复与旧代码相匹配的 `.env` 和完整 `data` 备份，再部署备份时对应的代码版本。不要只回滚程序文件并继续使用尚未备份或结构已改变的数据库。
