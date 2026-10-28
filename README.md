# qqrobot

基于 QQ 官方机器人接口与 DeepSeek 的数学、物理解题机器人。支持文字、图片、PDF 输入，按题号保存任务、关联追问，并将解答排版为图片和完整 PDF。

模型推理在远端 API 完成，本地负责材料处理、任务队列、SymPy 辅助计算、绘图、持久化与 XeLaTeX 排版。程序面向个人或小范围使用，默认串行处理重任务；部署配置为服务设置了 1536 MB 内存上限。

## 功能

- 数学、物理题解答、概念解释、证明检查及关联追问；按需调用 DeepSeek 搜索。
- 文字、题图与 PDF 识题；PDF 默认读取全部页面，也支持指定连续或分散页码。
- 多图收集：先登记题号，分消息补齐照片，再统一开始解题。
- 即时返回题号，SQLite 保存题干、任务状态、草稿与结果，重启后可以继续查询。
- 区分解答生成与复核状态；超时或复核失败时，已保存的草稿仍可领取。
- 解答为 1–3 页时默认发送全部页面图片，超过 3 页时默认发送完整 PDF；支持单独领取 PDF、图片和分段文字。
- 限定表达式的符号计算与函数、参数曲线、抛体、简谐振动绘图；计算与排版使用可终止子进程。
- 按会话和提问人隔离结果，支持白名单、任务配额、附件限制和结果过期清理。

## 部署前准备

1. 在 QQ 机器人平台创建应用，取得 `QQ_APPID`、`QQ_SECRET`，并配置目标群聊或单聊场景的接入权限。
2. 准备可用的 `DEEPSEEK_API_KEY`。解题、识图和按需搜索会调用远端接口，可能产生费用。
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

密钥含 `$`、`#` 等字符时，用单引号包裹。真实配置只放在本机或服务器的 `.env`；仓库仅提供不含凭据的 `.env.example`。

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

群聊中每条操作消息都需 **@ 机器人**，私聊直接发送。查询和追问须使用提交原题的账号，并在原会话操作。以下题号仅为格式示例。

| 消息 | 作用 |
|---|---|
| 直接发送题目文字、图片或 PDF | 登记新题并开始处理 |
| `Q261028-000001` 或 `/结果 Q261028-000001` | 查询状态，完成后领取结果 |
| `/进度 Q261028-000001` | 只查看进度和复核状态 |
| `/PDF Q261028-000001` | 领取完整 PDF，也支持 `/pdf` |
| `/图片 Q261028-000001 3` | 从第 3 页起领取最多两页图片 |
| `/文字 Q261028-000001 1` | 领取第 1 段文字，每段最多 1500 字符 |
| `/追问 Q261028-000001 为什么第三步成立` | 带原题和已有解答创建关联追问 |
| `/补充 Q261028-000001 补充条件` | 追加条件，可同条附图；已开始的题目会创建关联题 |
| `/收集 题目说明` | 登记材料收集题，暂不解题 |
| `/补图 Q261028-000001`，同条附图 | 向收集中的题目追加材料 |
| `/开始 Q261028-000001` | 收齐后以同一个题号开始解题 |
| `/取消 Q261028-000001` | 取消收集、排队或运行中的任务 |
| `/重试 Q261028-000001` | 创建新题号重新解题，可能产生新的 API 费用 |
| `/题目` | 查看本会话中自己最近五道题 |
| `/帮助` | 显示完整用法 |

图片起始页和文字段号省略时默认为 1。追问、补充可以只附图片；QQ 未提供被引用消息的附件时，需要重新发送原图。

### 分消息提交多张题图

1. 发送 `/收集 这道题分三张照片，请一起解答`，记下返回的题号。
2. 每张照片与 `/补图 题号` 放在同一条消息中发送，等待保存成功的回执。
3. 全部收齐后发送 `/开始 题号`。

收集期间不调用模型。每条消息最多四个附件，同一收集题累计最多十二个附件和 24000 字符。需要为已有题收集多张补充图时，先用 `/收集 原题号 补充说明` 创建关联收集题，再使用新题号补图和开始。

### PDF 与结果领取

随 PDF 写 `只读第3-5页` 或 `第2、5、8-10页` 可以指定页码；页码从文件第一页开始计数，与正文印刷页码无关。不指定时读取全部页面，多份 PDF 同条提交时共用该页码范围。

长 PDF 仍受材料总长度、单题时间和模型额度限制；超限时应减少页码或按章节拆分。完整解答排版上限为 40 页，排版失败或超限时保留文字结果。

QQ 的回复窗口和消息次数可能影响自动交付。程序默认关闭主动发送，结果保存后可用新消息发送题号或 `/PDF 题号` 领取；已有结果的领取不会重新调用模型。PDF 未生成时，可使用 `/文字 题号 1`。历史规则核对记录见 [QQ 平台规则说明](docs/qq-platform-rules.md)，实际权限以账号与接口响应为准。

## 配置说明

完整配置与注释见 [`.env.example`](.env.example)。布尔值支持 `true/false`、`1/0`、`yes/no`、`on/off`。修改配置后需要重启或重新创建服务实例。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `QQ_APPID` / `QQ_SECRET` / `DEEPSEEK_API_KEY` | 必填 | QQ 与模型接口凭据 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 模型接口地址 |
| `DEEPSEEK_MODEL` / `VISION_MODEL` | `deepseek-flash` | 解题模型与识图模型 |
| `DEEPSEEK_REASONING_EFFORT` | `high` | `low`、`high` 或 `max` |
| `SOLVE_MAX_TOKENS` / `VERIFY_MAX_TOKENS` | 按档位选择 | 默认 65536；`max` 档为 131072，可单独覆盖 |
| `QQ_API_BASE_URL` | `https://api.bot.qq.com` | QQ SDK 与媒体请求共用的 HTTPS 地址 |
| `SEARCH_ENABLED` | `true` | 解题流程按需搜索，复用模型密钥 |
| `TASK_TIMEOUT` | `1200` | 单题总预算，秒；不含排队和交付 |
| `SOLVE_TIMEOUT` / `VERIFY_TIMEOUT` | `600` / `300` | 单次解题与复核上限，秒，仍受总预算约束 |
| `VERIFY_SOLUTION` / `SYMPY_ENABLED` | `true` / `true` | 模型复核与符号计算 |
| `ACTIVE_DELIVERY` | `false` | 是否尝试主动发送，需要实际 QQ 权限 |
| `MAX_QUEUE` / `MAX_USER_TASKS` | `5` / `2` | 全局与单会话单用户的未完成任务上限 |
| `MAX_DAILY_TASKS` | `50` | 单会话单用户最近 24 小时提交次数上限 |
| `RESULT_TTL_DAYS` / `RESULT_MAX_MB` | `7` / `512` | 按提交时间计算的保留天数、缓存预算 |
| `ALLOWED_GROUPS` / `ALLOWED_USERS` | 空 | 群与私聊白名单，填写平台 OpenID，用英文逗号分隔 |
| `DATA_DIR` | 程序旁的 `data` | 直接运行的默认目录；systemd 和 Docker 固定使用各自持久化目录 |

白名单中的 OpenID 不是普通 QQ 号码。留空时接入范围由 QQ 平台已有权限决定。次数配额并非金额上限，一道题可能包含多次模型请求。

旧 `PDF_INPUT_PAGES`、`BOCHA_*` 等配置不再控制本项目，迁移时以当前模板为准。

## 开发与离线检查

本地开发需要 Python 3.12+。PDF 排版测试还需要可执行的 `xelatex`、TeX 宏包和中文字体；Ubuntu 用户可参考 `setup.sh` 的系统依赖列表。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python settings.py --allow-missing-credentials
.venv/bin/python -m pytest -q
```

上述命令适用于 Bash；Windows 使用 `.venv\Scripts\python.exe`。填写真实配置后，可运行 `python settings.py` 离线检查配置格式，该命令不连接 QQ 或模型，也不验证密钥余额和权限。直接运行开发实例的入口为 `python task_bot.py`，`bot.py` 是兼容入口。

离线测试包含模拟模型与 QQ 接口、题号权限、材料收集、PDF 选页、任务生命周期、结果交付和更新脚本。离线测试不能代替真实账号验收；首次部署后，应在目标群聊和私聊中分别验证发题、查询、追问、图片和 PDF 领取。

## 更新

先等待当前任务完成并备份实际 `.env` 与数据目录。systemd 安装从仓库的独立工作目录获取新版，然后重新运行安装脚本：

```bash
git pull --ff-only
sudo bash setup.sh
```

脚本保留已安装的配置与数据，更新程序、Python 依赖和服务定义后重启。失败时会尝试重新启动服务，但不保证回滚已更新的程序和依赖，应保留上一版本。

Docker 部署在仓库目录更新后重新构建：

```bash
git pull --ff-only
docker compose up -d --build
```

`update_delivery.sh`、`update_search.sh`、`update_pdf_input.sh` 和 `repair_pdf.sh` 是针对默认 systemd 安装的专项维护脚本。它们的更新范围各不相同，不能替代完整安装；使用前请阅读脚本注释并确认安装路径。

## 常见问题

| 现象 | 检查方法 |
|---|---|
| 服务启动失败 | 检查安装目录的 `.env`，查看 systemd 或 Compose 日志 |
| 模型鉴权、限流或网络错误 | 检查密钥、余额、模型权限和接口连通性 |
| 任务超时或输出截断 | 按小问拆分，减少 PDF 页码；已保存草稿可用 `/文字` 领取 |
| 有结果但没自动收到 | 在原会话发送题号、`/PDF` 或 `/图片`，检查 QQ 回复权限 |
| PDF 排版失败 | 检查 XeLaTeX 与中文字体，查看题目目录中的 `render-error.txt` 和保留的 TeX 日志 |
| 服务重启或退出 | 查看服务退出码、内存占用及系统 OOM 日志 |

报告问题时提供题号、阶段和脱敏日志；不要提交 `.env`、用户 OpenID、题目原始附件或 API 密钥。

## 目录

```text
qqrobot/
├── task_bot.py           # QQ 消息、命令、排队与结果交付
├── bot.py                # 兼容入口
├── settings.py           # 配置与离线检查
├── solver.py             # 识题、解题、复核和排版流程
├── ai.py                 # 模型请求、格式校验和恢复
├── task_store.py         # SQLite 任务持久化
├── input_cache.py        # 收集材料的本地缓存
├── qqmedia.py            # QQ 媒体上传与发送
├── render*.py            # TeX 排版及排版子进程
├── pdf_tools.py          # PDF 选页与渲染
├── safe_math.py          # 受限表达式计算
├── plot_tools.py         # 数学与物理绘图
├── search_tools.py       # 解题搜索
├── skills/               # 解题、物理与绘图规范
├── tests/                # 离线回归测试
├── setup.sh              # Ubuntu / systemd 安装
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── requirements-dev.txt  # 测试依赖
└── .env.example          # 配置模板，无真实凭据
```

## 使用边界与数据

解答由模型生成，复核状态会单独标明；生成完成不代表数学结论已经得到人工确认。绘图仅支持限定类型，不能代替证明或任意物理仿真。

题目、附件及相关上下文会发送给模型服务，按需搜索的关键词会发送给搜索接口。服务器保存题目、附件和解答，默认自提交起保留七天，管理员可访问。请按自己的使用场景设置白名单、保存期限与数据备份。

本项目包含离线回归测试；Linux/systemd、Docker、真实模型调用、QQ 消息权限以及小内存服务器负载仍需在实际部署环境验收。
