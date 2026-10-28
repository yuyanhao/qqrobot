# QQ 消息与文件规则核对

核对日期：2026-10-26。使用 QQ 机器人官方当前网站；旧 GitHub 镜像和博客可能不同。实际权限以账号、接收方设置和接口响应为准。

| 场景 | 被动回复窗口 | 每条用户消息可回复次数 |
|---|---|---|
| QQ 群聊 | 5 分钟 | 5 次 |
| QQ 单聊/C2C | 60 分钟 | 4 次 |

来源：[消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[发送群消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)、[发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)。

文档矛盾：单聊接口页规则及总览写 60 分钟，但 `msg_id` 字段仍有一处 5 分钟描述。程序依据总览和页面规则设为 60 分钟，预留 15 秒余量；群聊按 290 秒处理。若实际权限更严格，发送失败后保留结果，等待用户用新消息领取。

回执、状态和结果分别占用次数。同一条消息的多次回复递增 `msg_seq`；输入事件重复不会重复创建题目。回复次数不等于 PDF 页数。

## 主动消息

当前官方总览列出主动消息能力及频控，用户可关闭主动消息接收。程序默认关闭主动交付，窗口内尝试被动发送，过期则等待题号查询。

单聊主动消息的单关系频控为每分钟 20 条、每天每用户 1000 条；群聊同样列出每分钟每关系 20 条、每天每群 1000 条。机器人整体限速另随认证状态和场景变化。程序的简单节流并未实现平台所有配额，触发限制仍保留结果。[官方频控说明](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)

## PDF 与上传

当前群聊、单聊接口均列出通用文件 `file_type=4`，文件上限 200MB；程序为小服务器设置 50MB 上限。上传得到 `file_info` 后，通过 `msg_type=7` 消息发送。[单聊上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_files.post.html)、[群聊上传](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_files.post.html)

官方直传限制为 5M 以内；程序保守地以 4,000,000 字节作为分界。更大的文件通过预上传、PUT 分片、通知完成、合并上传，不需要额外域名或公网文件托管。[预上传接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_id_upload_prepare.post.html)、[分片完成接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_id_upload_part_finish.post.html)

`file_info` 有有效期，群聊和单聊的上传结果不能混用，领取时会重新上传。文件上传成功不代表消息已经送达，发送权限和回复窗口需另外满足。[富媒体说明](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)

## 模型空响应

DeepSeek 的 JSON Output 文档说明需要明确的 JSON 示例、合理输出上限，并提示可能返回空 `content`。旧版统一报错不能直接归因于题目难度或 QQ 限制。新版区分结构检查、一次格式恢复及阶段报错。[DeepSeek JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)
