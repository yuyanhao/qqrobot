# qqrobot

![CI](https://github.com/yuyanhao/qqrobot/actions/workflows/ci.yml/badge.svg)
![Release](https://img.shields.io/github/v/release/yuyanhao/qqrobot)
![License](https://img.shields.io/github/license/yuyanhao/qqrobot)

当前代码版本：**v1.0.5**。变更内容见 [CHANGELOG.md](CHANGELOG.md)。

`VERSION` 保存版本号；主分支更新此文件时，发布流程会创建对应 Git 标签和 GitHub Release。

基于 QQ 官方机器人接口，可接入 DeepSeek 或任意 OpenAI 兼容模型接口的数学、物理解题机器人。支持文字、图片、PDF 输入，按题号保存任务、关联追问，并将解答排版为图片和完整 PDF。代码按 MIT License 授权，详见 [LICENSE](LICENSE)。

模型推理在远端 API 完成，本地负责材料处理、任务队列、SymPy 辅助计算、绘图、持久化与 XeLaTeX 排版。程序面向个人或小范围使用，默认串行处理重任务；部署配置为服务设置了 1536 MB 内存上限。

## 功能

- 数学、物理题解答、概念解释、证明检查及关联追问；按需调用搜索接口。
- 文字、题图与 PDF 识题；每份要读取的 PDF 都必须明确指定物理页码，每份 PDF 和单任务合计均最多读取 5 页。
- 多图收集：先登记题号，分消息补齐照片或 PDF，再用「/解题 收集题号」或「/查错 收集题号」处理。
- 即时返回题号，SQLite 保存题干、任务状态、草稿与结果，重启后可以继续查询。
- 区分解答生成与复核状态；超时或复核失败时，已保存的草稿仍可领取。
- 解答为 1–3 页时默认发送全部页面图片，超过 3 页时默认发送完整 PDF；支持单独领取 PDF、图片和分段文字。
- 限定表达式的符号计算与函数、参数曲线、抛体、简谐振动绘图；计算与排版使用可终止子进程。
- 按会话和提问人隔离结果，支持白名单、任务配额、附件限制和结果过期清理。

## 部署前准备

1. 在 QQ 机器人平台创建应用，取得 `QQ_APPID`、`QQ_SECRET`，并配置目标群聊或单聊场景的接入权限。
2. 准备模型接口的密钥。默认使用 DeepSeek；也可以接入 OpenAI、OpenRouter、Anthropic（OpenAI 兼容接口）、Gemini、通义千问、Kimi、智谱、SiliconFlow、Ollama 等 OpenAI 兼容接口。解题、识图和按需搜索会调用远端接口，可能产生费用。
3. 准备可以访问 QQ 与模型接口的主机。systemd 安装脚本使用 `apt-get`，需要 Python 3.12 或更新版本；可使用 Ubuntu 24.04，或使用下方 Docker 方案。

仓库名为 `qqrobot`；为兼容已有安装，程序的 systemd 服务名、容器名仍为 `qq-math-bot`，systemd 安装路径仍为 `/opt/qq-math-bot`。

## 快速开始

下面两种部署方式任选一种，同一机器人只运行一个实例。

```bash
git clone https://github.com/yuyanhao/qqrobot.git
cd qqrobot
cp .env.example .env
nano .env
```

至少填写以下三项，其余配置可先保留模板默认值：

```dotenv
QQ_APPID=你的应用ID
QQ_SECRET=你的应用密钥
DEEPSEEK_API_KEY=你的模型密钥
```

若改用其他供应商，把 `DEEPSEEK_API_KEY` 换成 `LLM_PROVIDER`、`LLM_API_KEY`、`LLM_MODEL`（见下一节），不需要改代码。

密钥含 `$`、`#` 等字符时，用单引号包裹。真实配置只放在本机或服务器的 `.env`；仓库仅提供不含凭据的 `.env.example`。

已有只含 `DEEPSEEK_*` 的 `.env` 无需改动即可升级到 v1.0.5：默认供应商仍是 DeepSeek，配置会作为新变量的回退值读取。

### 模型供应商

`LLM_PROVIDER` 选择供应商，`LLM_API_KEY`、`LLM_MODEL` 填写密钥和型号；`LLM_BASE_URL` 留空时使用下表内置地址。`custom` 必须填写 `LLM_BASE_URL`。

| `LLM_PROVIDER` | 接口地址 | 说明 |
|---|---|---|
| `deepseek`（默认） | `https://api.deepseek.com` | 可直接沿用 `DEEPSEEK_*` 旧变量 |
| `openai` | `https://api.openai.com/v1` | 需填写当前可用的模型名 |
| `openrouter` | `https://openrouter.ai/api/v1` | 模型名通常带供应商前缀 |
| `anthropic` | `https://api.anthropic.com/v1` | 走 Anthropic 的 OpenAI 兼容接口 |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | 走 Gemini 的 OpenAI 兼容接口 |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 阿里云百炼 |
| `moonshot` | `https://api.moonshot.cn/v1` | Kimi |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | GLM |
| `siliconflow` | `https://api.siliconflow.cn/v1` | 模型名由平台提供 |
| `ollama` / `lmstudio` | `http://127.0.0.1:11434/v1` / `http://127.0.0.1:1234/v1` | 本机服务，可省略密钥 |
| `custom` | 自行填写 | 任何 OpenAI 兼容地址 |

示例（OpenAI 兼容接口）：

```dotenv
LLM_PROVIDER=openai
LLM_API_KEY=你的模型密钥
LLM_MODEL=你的模型名
LLM_VISION_MODEL=支持图片的模型名
```

不是所有模型都支持图片。需要识题时设置 `LLM_VISION_MODEL`；模型不支持图片或 `response_format` 时，程序会按错误信息给出提示，个别接口不支持的参数会自动去掉并重试一次。联网搜索仍使用 DeepSeek Search API：DeepSeek 部署无需另配；其他供应商需填写 `SEARCH_API_KEY`，或设 `SEARCH_ENABLED=false` 关闭搜索。

### Ubuntu / systemd

在仓库目录执行：

```bash
sudo bash setup.sh
sudo systemctl status qq-math-bot --no-pager
sudo journalctl -u qq-math-bot -n 100 --no-pager
```

脚本会安装 Python、TeX 与中文字体依赖，建立低权限运行账户和 systemd 服务，并启动机器人。首次安装 TeX 依赖需要时间和磁盘空间。

若原有 `qqbot` 服务正在运行，先等待当前任务结束，再执行 `sudo systemctl stop qqbot`，然后安装。脚本检测到旧服务仍在运行时会拒绝安装，以免重复登录。

| 内容 | 位置 |
|---|---|
| 程序 | `/opt/qq-math-bot` |
| 实际配置 | `/opt/qq-math-bot/.env` |
| 数据库、附件和解答 | `/opt/qq-math-bot/data` |
| 日志 | `journalctl -u qq-math-bot` |

已有安装的 `.env` 会保留。修改配置应编辑安装目录中的文件，然后重启：

```bash
sudo nano /opt/qq-math-bot/.env
sudo systemctl restart qq-math-bot
```

### Docker Compose

主机已安装 Docker 和 Compose 插件时，在填写好 `.env` 的仓库目录执行：

```bash
docker compose up -d --build
docker compose logs --tail=100
```

镜像基于 Python 3.12，包含 XeLaTeX 和中文字体，以非 root 用户运行。结果保存在 Compose 命名卷 `qqbot-data` 中，容器内路径为 `/app/data`。

修改 `.env` 后执行 `docker compose up -d --force-recreate`。停止时可使用 `docker compose down`；`docker compose down -v` 会删除持久化数据卷，包含已保存的题目和解答。

## 在 QQ 中使用

群聊每条操作都需 @ 机器人，私聊可直接发送。带题号的操作用原账号在原会话发送。

- 直接发数学或物理题的文字、图。群聊可 @ 机器人并附 PDF，私聊先发 PDF，再引用文件消息并写明要求。普通 PDF 每份最多 10 MB；所有要读取的 PDF，包括题面、草稿、追问和附加材料，都必须写明页码，每份 PDF 和单任务累计最多 5 页。未写页码不会读取。多条提交材料先用「/收集」，再用「/补图 收集题号」追加，最后发「/解题 收集题号」开始解题。
- 查错推荐先把题目和草稿用「/收集」收进同一题号，再发「/查错 收集题号」；自动识别题目和草稿，不准时用「/查错 收集题号 1题 2-4稿」指定。也可用「/查错 题号」在同一条消息附草稿，或用「/查错 同页」发一张题目和作答同页的图。PDF 页码须在收集或同条命令中指定，每份 PDF 和每个任务最多 5 页。查错会指出第一处错误、说明原因并给出更正结论。
- 直接发题号或「/结果 题号」查询并领取，排版后 1–3 页发全部图片，超过 3 页发 PDF。若只有文字稿，会发回第一段，再按提示用「/结果 题号 第2段」继续领取。用「/PDF 题号」可指定领取完整 PDF。
- 「/书库」查看共享 PDF，编号按当前在库顺序连续排列；移除一本后，后续书籍编号自动顺延。「/引用 B0001 第3-5页」读取指定书页。所有附加 PDF 也须明确指定页码，且整项任务累计最多 5 页。管理员书库单份 PDF 默认限 100 MB、书库总量默认限 1 GB（`BOOK_MAX_PDF_MB` / `BOOK_LIBRARY_MAX_MB`）；暂存 15 分钟后过期。
- 结果默认保留 7 天，期间随时用题号领取。
- 手机 QQ 群的原生面板可用 `python -m qqrobot.panel` 预览，部署后用 `python -m qqrobot.panel --apply` 同步。「/最近」群聊首行 @ 你，下面只有两行两列题号按钮，不输出文字（按钮不可用时退回题号列表）。

[完整指令、书库管理员配置和面板同步方式](docs/qq-command-guide.md)

## 配置说明

完整配置与注释见 [`.env.example`](.env.example)。布尔值支持 `true/false`、`1/0`、`yes/no`、`on/off`。修改配置后需要重启或重新创建服务实例。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `QQ_APPID` / `QQ_SECRET` | 必填 | QQ 接口凭据 |
| `LLM_PROVIDER` | `deepseek` | 模型供应商，见上表 |
| `LLM_API_KEY` | DeepSeek 可留空 | 模型密钥；DeepSeek 可继续使用 `DEEPSEEK_API_KEY` |
| `LLM_BASE_URL` / `LLM_MODEL` | 按供应商 | 接口地址与模型名；`custom` 必须填写地址 |
| `LLM_VISION_MODEL` | 跟随 `LLM_MODEL` | 识图模型；不填则与解题模型相同 |
| `LLM_REASONING_EFFORT` | `high` | `low`、`high` 或 `max`；只对支持该参数的接口发送 |
| `SOLVE_MAX_TOKENS` / `VERIFY_MAX_TOKENS` | 按档位选择 | 默认 65536；`max` 档为 131072，可单独覆盖 |
| `QQ_API_BASE_URL` | `https://api.bot.qq.com` | QQ SDK 与媒体请求共用的 HTTPS 地址 |
| `SEARCH_ENABLED` | `true` | 解题流程按需搜索；DeepSeek 复用模型密钥，其他供应商需 `SEARCH_API_KEY` |
| `PDF_INPUT_MAX_PAGES` / `PDF_TASK_MAX_PAGES` | `5` / `5` | 单份 PDF 页数上限与单任务合计页数上限；合法配置范围均为 1–5 页；每份待读取 PDF 均须明确指定页码 |
| `BOOK_MAX_PDF_MB` / `BOOK_LIBRARY_MAX_MB` | `100` / `1024` | 管理员书库单份 PDF 与书库总容量上限 |
| `TASK_TIMEOUT` | `1800` | 单题总预算，秒；不含排队和交付 |
| `SOLVE_TIMEOUT` / `VERIFY_TIMEOUT` | `900` / `600` | 单次解题与复核上限，秒，仍受总预算约束 |
| `VERIFY_SOLUTION` / `SYMPY_ENABLED` | `true` / `true` | 模型复核与符号计算 |
| `ACTIVE_DELIVERY` | `false` | 是否尝试主动发送，需要实际 QQ 权限 |
| `MAX_QUEUE` / `MAX_USER_TASKS` | `5` / `2` | 全局与单会话单用户的未完成任务上限 |
| `MAX_DAILY_TASKS` | `50` | 单会话单用户最近 24 小时提交次数上限 |
| `RESULT_TTL_DAYS` / `RESULT_MAX_MB` | `7` / `512` | 按提交时间计算的保留天数、缓存预算 |
| `ALLOWED_GROUPS` / `ALLOWED_USERS` | 空 | 群与私聊白名单，填写平台 OpenID，用英文逗号分隔 |
| `DATA_DIR` | 程序旁的 `data` | 直接运行的默认目录；systemd 和 Docker 固定使用各自持久化目录 |

白名单中的 OpenID 不是普通 QQ 号码。留空时接入范围由 QQ 平台已有权限决定。次数配额并非金额上限，一道题可能包含多次模型请求。

使用 DeepSeek 官方接口时，已验证的 `LLM_MODEL`（或旧 `DEEPSEEK_MODEL`）为 `deepseek-flash` 和 `deepseek-v4-pro`，已验证的识图模型为支持图片的 `deepseek-flash`。其他非空名称会给出启动提示并尝试运行；请确认上游可用性和图片能力。旧 `DEEPSEEK_*` 变量在新变量未设置时继续生效，便于不改 `.env` 升级。

旧 `PDF_INPUT_PAGES`、`BOCHA_*` 等配置不再控制本项目，迁移时以当前模板为准。

## 开发与离线检查

本地开发需要 Python 3.12+。PDF 排版测试还需要可执行的 `xelatex`、TeX 宏包和中文字体；Ubuntu 用户可参考 `setup.sh` 的系统依赖列表。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m qqrobot.settings --allow-missing-credentials
.venv/bin/python -m pytest -q
```

上述命令适用于 Bash；Windows 使用 `.venv\Scripts\python.exe`。填写真实配置后，可运行 `python -m qqrobot.settings` 离线检查配置格式，该命令不连接 QQ 或模型，也不验证密钥余额和权限。直接运行开发实例的入口为 `python -m qqrobot`；群聊面板为 `python -m qqrobot.panel`。

离线测试包含模拟模型与 QQ 接口、供应商配置、题号权限、材料收集、PDF 选页、任务生命周期和结果交付。离线测试不能代替真实账号验收；首次部署后，应在目标群聊和私聊中分别验证发题、查询、追问、图片和 PDF 领取。

### 数学符号覆盖

系统核对了已加载 LaTeX/AMS 宏包中的 443 个标准符号、界符、重音及安全别名，另验证 39 个常用数学宏；当前支持命令共 612 项。整套目录通过实际 XeLaTeX 编译，文件操作、宏定义及任意包加载等指令仍被拒绝。自定义宏和未加载宏包不在此范围。

目录保存在 `tests/fixtures/standard-math-symbols.json`。安装 TeX Live 后，可用以下命令重新核对并合并目录，再运行测试：

```bash
python tools/update_math_symbols.py --apply
python -m pytest -q
```

## 更新

### 不改 `.env` 升级到 v1.0.5

v1.0.5 默认仍使用 DeepSeek，并继续读取现有 `.env` 中的 `DEEPSEEK_*`；`setup.sh` 只在安装目录没有 `.env` 时复制模板，因此不需要重新填写或替换配置文件。v1.0.5 把代码移入 `qqrobot/` 包，启动命令改为 `python -m qqrobot`，`setup.sh` 会一并更新 systemd 单元。

先等待当前任务完成，然后只备份 `.env` 和 `data/`：

```bash
sudo systemctl stop qqbot || true
sudo systemctl stop qq-math-bot || true
stamp=$(date +%Y%m%d-%H%M%S)
sudo tar -czf "/root/qq-math-bot-before-v1.0.5-$stamp.tar.gz" -C /opt/qq-math-bot .env data
cd /tmp
curl -fL https://github.com/yuyanhao/qqrobot/archive/refs/tags/v1.0.5.tar.gz -o qqrobot-v1.0.5.tar.gz
rm -rf qqrobot-v1.0.5 && mkdir -p qqrobot-v1.0.5
tar -xzf qqrobot-v1.0.5.tar.gz --strip-components=1 -C qqrobot-v1.0.5
cd qqrobot-v1.0.5
sudo bash setup.sh
sudo -u qqbot /opt/qq-math-bot/.venv/bin/python -m qqrobot.panel --apply
grep -x 1.0.5 /opt/qq-math-bot/VERSION
```

`setup.sh` 会保留 `/opt/qq-math-bot/.env` 与 `data/`，更新 Python 依赖、复制 `qqrobot/` 和 `skills/`，重写 systemd 单元并重启服务。首次升级后旧的扁平 `*.py` 文件会留在安装目录，但不再被加载；确认新版本运行正常后可手动删除。不要用 `.env.example` 覆盖 `.env`，也不要执行 `git clean -fdx`。

若当前服务器用 Docker，在现有 Compose 项目目录执行；`.env` 由 Compose 的 `env_file` 读取，更新时保持不变：

```bash
cd /path/to/qqrobot
cp .env "/root/qqbot-env-before-v1.0.5-$(date +%Y%m%d-%H%M%S)"
docker compose stop
volume="$(docker volume ls -q --filter label=com.docker.compose.volume=qqbot-data | head -n 1)"
test -n "$volume"
backup="qqbot-data-before-v1.0.5-$(date +%Y%m%d-%H%M%S).tar.gz"
docker run --rm -v "$volume:/data:ro" -v "$PWD:/backup" alpine:3.22 \
  tar -czf "/backup/$backup" -C /data .
git fetch --tags
git checkout v1.0.5
docker compose up -d --build
docker compose exec qqbot python -m qqrobot.panel --apply
```

不要执行 `docker compose down -v`，它会删除数据卷。发布页为 `https://github.com/yuyanhao/qqrobot/releases/tag/v1.0.5`，源码归档地址为 `https://github.com/yuyanhao/qqrobot/archive/refs/tags/v1.0.5.tar.gz`；使用 Git 仓库目录部署时，正式发布后可执行 `git checkout v1.0.5`。

`update_delivery.sh`、`update_pdf_input.sh`、`update_search.sh` 和 `repair_pdf.sh` 在 v1.0.5 中已移除；升级请使用完整压缩包和 `setup.sh`，不要再用旧脚本覆盖安装目录。

备份与旧版升级说明见 [数据备份](docs/backup.md) 和 [升级指南](docs/upgrading.md)。

## 常见问题

| 现象 | 检查方法 |
|---|---|
| 服务启动失败 | 检查安装目录的 `.env`，查看 systemd 或 Compose 日志 |
| 模型鉴权、限流或网络错误 | 检查密钥、余额、模型权限和接口连通性 |
| 任务超时或输出截断 | 按小问拆分，减少 PDF 页码 |
| 有结果但没自动收到 | 在原会话发送题号或 `/PDF`，检查 QQ 回复权限 |
| PDF 排版失败 | 检查 XeLaTeX 与中文字体，查看题目目录中的 `render-error.txt` 和保留的 TeX 日志 |
| 服务重启或退出 | 查看服务退出码、内存占用及系统 OOM 日志 |

报告问题时提供题号、阶段和脱敏日志；不要提交 `.env`、用户 OpenID、题目原始附件或 API 密钥。

## 目录

```text
qqrobot/
├── qqrobot/
│   ├── __main__.py          # 启动入口：python -m qqrobot
│   ├── settings.py          # 配置、校验与离线检查
│   ├── providers.py         # 模型供应商预设与 LLM_* 配置
│   ├── panel.py             # 群聊原生指令面板同步
│   ├── core/                # 配置、输入、缓存、子进程与符号安全
│   ├── solving/             # 模型请求、解题流程、搜索、SymPy、书库
│   ├── media/               # 文件、PDF、绘图与 QQ 媒体
│   ├── rendering/           # XeLaTeX 排版与排版子进程
│   └── tasks/               # QQ 事件、指令、队列、存储与交付
├── skills/                  # 解题、物理与绘图规范
├── tests/                   # 离线回归测试
├── tools/                   # 发布检查与符号目录维护
├── docs/                    # 升级、备份、指令与安全说明
├── setup.sh                 # Ubuntu / systemd 安装
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt     # 测试依赖
└── .env.example             # 配置模板，无真实凭据
```

## 使用边界与数据

解答由模型生成，复核状态会单独标明；生成完成不代表数学结论已经得到人工确认。绘图仅支持限定类型，不能代替证明或任意物理仿真。

题目、附件及相关上下文会发送给模型服务，按需搜索的关键词会发送给搜索接口。服务器保存题目、附件和解答，默认自提交起保留七天，管理员可访问。群聊中发出的回复仍对群成员可见。请按自己的使用场景设置白名单、保存期限与数据备份。

本项目包含离线回归测试；Linux/systemd、Docker、真实模型调用、QQ 消息权限以及小内存服务器负载仍需在实际部署环境验收。
