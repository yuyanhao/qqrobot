# 数据备份与恢复

机器人运行时会使用 SQLite WAL。直接复制数据库主文件可能漏掉尚未合并的事务；优先在停止服务后备份整个数据目录。

## systemd 部署

等待正在运行的任务结束，然后停止服务并打包配置与数据：

```bash
sudo systemctl stop qq-math-bot
sudo tar -czf "/root/qq-math-bot-backup-$(date +%Y%m%d-%H%M%S).tar.gz" \
  -C /opt/qq-math-bot .env data
sudo systemctl start qq-math-bot
```

归档包括 `.env`、任务数据库、附件、生成结果和书库。它含 API 密钥与用户材料，应限制访问并放在加密或受控存储中。恢复时先停服务，再将归档还原到 `/opt/qq-math-bot`，确认所有者为 `qqbot` 后启动服务。

如果不能停服务，SQLite 提供一致性备份接口：

```bash
sqlite3 /opt/qq-math-bot/data/tasks.sqlite3 \
  ".backup '/root/tasks-$(date +%Y%m%d-%H%M%S).sqlite3'"
```

在线数据库备份不包含附件、解答文件或书库；仍需用一致的时间点备份 `data` 中其余内容。一般维护和升级应使用停服务后的整目录归档。

## Docker Compose

在 Compose 项目目录停止容器。列出 Compose 数据卷名称，再将卷内容打包到当前目录：

```bash
docker compose stop
volume="$(docker volume ls -q --filter label=com.docker.compose.volume=qqbot-data | head -n 1)"
test -n "$volume"
docker run --rm -v "$volume:/data:ro" -v "$PWD:/backup" alpine:3.22 \
  tar -czf /backup/qqbot-data-backup.tar.gz -C /data .
```

恢复前先停止容器，将归档解压回同一卷，再运行 `docker compose up -d`。`docker compose down -v` 会删除数据卷，不要把它当作普通停止命令。
