# 账户额度查询

在 AstrBot 中查询本机登录的 Codex 账号额度，以及 DeepSeek 官方 API 账号余额。支持命令与唤醒后的自然语言查询，直接返回查询结果，无需模型生成回答。

## 安装与前提

### 通过官方插件市场（推荐）

在 AstrBot 的插件市场搜索 **账户额度查询** 或 `astrbot_plugin_account_quota`，点击安装。

### 通过 GitHub 仓库安装

在 AstrBot 插件管理页选择从链接安装，填写仓库地址：

```text
https://github.com/zsmkmojang-a11y/astrbot-plugin-account-quota
```

### 本地安装

在仓库页面通过 **Code → Download ZIP** 下载，或使用提供的 `astrbot_plugin_account_quota.zip`，然后在 AstrBot 插件管理页上传 ZIP；也可以将仓库内容放入 AstrBot 的 `data/plugins/astrbot_plugin_account_quota/` 目录，保证该目录直接包含 `main.py` 和 `metadata.yaml`。

安装后重新加载插件，在 AstrBot 中将自己的平台用户 ID 配置为管理员，默认仅管理员可以查询。

Codex 查询需要 AstrBot 运行环境内有 Codex CLI，并能访问已通过 ChatGPT 登录的 Codex 用户目录。DeepSeek 查询需要 AstrBot 已配置官方 DeepSeek 提供商。

插件接口按 AstrBot 官方 `v4.28.1` 源码核对，使用 Python 3.12+。HTTP 客户端使用 AstrBot 自带的 `aiohttp`，不增加额外依赖。未声明未经实测的最低 AstrBot 兼容版本。

**部署位置影响 Codex 查询：** 推荐 AstrBot 与 Codex 在同一台电脑、同一系统用户下运行。Windows 服务或其他用户需要配置可访问的 CLI 路径、Codex 用户目录；使用系统凭据库保存登录时，仅指向目录仍可能不足，须在对应运行用户下登录。

Docker 容器不能直接执行宿主机的 Windows `codex.exe`，也不会自动读取宿主机的登录身份。本版没有远程采集桥接：容器内需安装适用的 Codex CLI 并完成登录，才能查询该登录账号。DeepSeek 官方余额查询不要求与 Windows Codex 同机。

## 使用

| 指令 | 查询范围 |
| --- | --- |
| `/额度` | Codex + DeepSeek |
| `/codex额度` | Codex |
| `/deepseek余额` | DeepSeek |

`/` 是常见的 AstrBot 唤醒前缀；如果你的唤醒前缀不同，请使用实际配置的前缀。

强唤醒词假设为 `小助手`，可以发送：

```text
小助手 帮我查一下额度
小助手 查一下 Codex 额度
小助手 DeepSeek 还剩多少钱
小助手 查看 Codex 和 DeepSeek 的额度
```

也支持 @ 机器人后发送上述查询。私聊是否需要唤醒词遵循 AstrBot 自身设置。插件读取 AstrBot 的唤醒标记，不另设唤醒词；请先在 AstrBot 配置你的强唤醒前缀。

首版匹配明确、完整的查询短语，不调用 LLM 做意图识别。“额度怎么计算”“不要帮我查一下额度”“帮我查一下额度，然后写诗”等不会触发查询。普通未唤醒的群聊消息不会触发。你可以在配置中关闭自然语言查询，命令仍然有效。

查询命中后仅回复一次，并停止该消息继续调用模型。查询失败和权限不足也会直接返回提示，不需要 DeepSeek 有余额才能执行查询。

## 查询时的提醒

符合条件时，在对应来源的查询结果后追加提醒。下面是默认文案，三条文案都可以在插件配置中分别修改：

| 判断条件 | 提醒文案 |
| --- | --- |
| DeepSeek 人民币 `total_balance` 严格小于 10 元 | 该充蓝色大肥鱼了喵，要吃不起白饭了。 |
| Codex 周剩余额度严格大于 50%，且距离该周周期的重置时间大于 0、不足 2 天 | 要重置了，快蹬喵 |
| 至少一张可用 Codex 重置卡距离过期大于 0、不足 2 天 | 重置卡要过期了，快蹬喵 |

“周额度”按剩余比例判断，服务返回的 10080 分钟周期视为周周期；不会根据 5 小时周期的重置时间触发周提醒。2 天为连续 48 小时，恰好 2 天不触发，恰好剩余 50% 不触发。USD 余额不自动换算成人民币。

重置卡数量取服务返回的 `availableCount`；过期提醒仅检查服务返回、状态为 `available` 且尚未过期的卡片详情。若服务只返回数量、不返回有效期，就显示数量而不猜测过期时间。同一条结果中，每种提醒最多显示一次。

本版提醒随查询结果显示，没有后台定时推送，不执行重置卡兑换。缓存有效期间，提醒基于缓存数据的查询时间。

## 提醒图片与文案

从 `v1.1.0` 起，默认在触发任意提醒时，在同一条结果中附加一张图片。默认图片是用户提供的附件，原样存放于 `assets/default_reminder.png`，已包含在安装包中，不依赖原来的临时附件路径。

在插件管理页配置：

- **DeepSeek 余额不足提醒文案**：修改人民币总余额不足 10 元时的提示。
- **Codex 周额度即将重置提醒文案**：修改周重置提醒。
- **Codex 重置卡即将过期提醒文案**：修改重置卡过期提醒。
- **提醒时附带图片**：默认开启，关闭后仅发送文字。
- **提醒图片路径或 URL**：留空使用内置图，也可填写自己图片的路径或 URL。

自定义图片示例：

```text
Windows 本地绝对路径：D:\Pictures\quota.png
Linux / Docker 内路径：/app/data/quota.png
相对插件目录的路径：assets/my_reminder.png
网络图片：https://example.com/quota.png
```

本地路径必须能被 AstrBot 运行环境读取；Docker 中填写容器内路径。网络图片需可被 AstrBot 或消息适配器读取，最终图片发送能力取决于消息平台。配置的本地图片不存在或路径无效时，仅发送文字，并在日志提示检查配置。

普通查询、查询失败和权限不足不附图。同一条汇总结果中，即使多个提醒同时触发，也只附一张图。留空某条提醒文案会隐藏该条文字，但只要提醒条件成立，仍遵循图片开关。文案在输出时读取配置，缓存结果不会锁住旧文案；保存配置并重新加载插件后使用新设置。

## 配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `admin_only` | `true` | 只允许 AstrBot 管理员查询。关闭后，所有能够唤醒机器人的用户均可查询已配置账号的额度。 |
| `natural_language_enabled` | `true` | 启用自然语言查询。 |
| `codex_path` | `codex` | CLI 名称或完整路径，只填程序路径，不填参数。 |
| `codex_home` | 空 | 留空继承 CLI 运行环境；可显式指定 `.codex` 用户数据目录。 |
| `deepseek_provider_id` | 空 | 指定 AstrBot 中的 DeepSeek 官方提供商 ID，复用其凭据。 |
| `deepseek_show_usd` | `false` | 显示官方接口返回的 USD 余额；默认关闭，不进行汇率换算。 |
| `deepseek_low_balance_reply` | 该充蓝色大肥鱼了喵，要吃不起白饭了。 | DeepSeek 余额不足提醒，留空隐藏文字。 |
| `codex_weekly_reset_reply` | 要重置了，快蹬喵 | Codex 周重置提醒，留空隐藏文字。 |
| `codex_card_expiry_reply` | 重置卡要过期了，快蹬喵 | Codex 重置卡过期提醒，留空隐藏文字。 |
| `reminder_image_enabled` | `true` | 提醒时附带图片。 |
| `reminder_image_path` | 空 | 留空用内置图；支持本地路径、插件相对路径和 HTTP/HTTPS 图片 URL。 |
| `query_timeout_seconds` | `20` | 单个来源查询超时，范围 3–120 秒。Codex 超时后回收进程最多另需约 2 秒。 |
| `cache_seconds` | `30` | 数据缓存，范围 0–300 秒；0 关闭缓存。查询失败最多缓存 5 秒。 |

Windows 官方 npm 安装的 `codex.cmd` / `codex.ps1` 包装器可自动解析到原生程序。无法自动定位时，在插件配置中填写原生 `codex.exe` 完整路径。比如本次验证环境的路径为：

```text
C:\Users\你的用户名\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe
```

该路径仅为示例，不会写入默认配置，CLI 更新后路径可能变化。`codex_home` 示例为 `C:\Users\你的用户名\.codex`。

DeepSeek 提供商选择顺序：

1. 配置了 `deepseek_provider_id` 时，使用指定提供商。
2. 否则，优先使用当前会话的官方 DeepSeek 提供商。
3. 当前会话不是 DeepSeek 时，使用唯一一个已加载的官方 DeepSeek 提供商；没有或有多个候选时提示指定 ID。

仅认可 HTTPS 的 `api.deepseek.com`（根路径、`/v1` 或 `/beta`）；第三方中转站即使模型名含 DeepSeek，也不会被当作官方账户查询。API Key 优先取提供商当前 Key；无法取得且存在多个不同 Key 时提示配置，避免猜测账号。余额属于所选 Key 对应的账号，不属于单个模型。

插件只向固定官方余额地址发送 Key，不跟随 HTTP 重定向，不在日志或回复中输出凭据，不创建登录副本。HTTP 连接遵循系统代理环境变量；代理与网络可达性需在 AstrBot 运行环境中配置。

## 结果与口径

- Codex 使用 `account/rateLimits/read`，按服务返回的额度分组与周期显示，不固定假设只有两个窗口。剩余比例为 `100 - usedPercent`，限制在 0–100%；缺失值显示“未知”。
- Codex 额度是登录账号的共享使用状态，不是当前聊天的 token 数。额外 credits 保留服务返回的单位，不将它换算成金额。
- DeepSeek 使用 `/user/balance`，分别显示总可用余额、充值余额、赠金及币种；保留小额余额精度。默认只显示人民币余额，开启“显示 DeepSeek USD 余额”后也显示接口返回的美元余额。若接口只返回 USD 且开关关闭，会提示余额已隐藏，不把缺失的人民币余额当作 0；接口未返回 USD 时不会换算或生成美元数值。
- 每个结果附数据查询时间，时间按北京时间显示。缓存结果沿用原数据时间，重置倒计时也是该次查询时计算的值。
- 两个来源并发查询，某一方失败不影响另一方显示；相同来源的同时查询会合并。不同 DeepSeek Key 的缓存隔离。
- 本地 Codex 登录身份变化后，已有缓存可能保留到缓存到期；可将 `cache_seconds` 设为 0 或重新加载插件。

## 验证

在插件目录运行：

```console
python -B -m unittest discover -s tests -v
```

32 项自动测试覆盖：明确查询与解释性问题、注册过滤器阶段的唤醒门槛、管理员限制、重复处理、提供商选择及官方地址校验、多周期与缺失字段、余额精度、三种提醒的阈值和有效期边界、缓存隔离与并发合并、独立失败，以及真实本地 HTTP / stdio 管道的握手、超时、取消、重定向、异常响应和进程回收。包含超时清理期间卸载及凭据选择期间卸载的回归测试；新增自定义文案、缓存后的文案变化、默认与自定义图片、关闭附图、空文案、多提醒只附一张图及缺图降级测试，以及 USD 默认隐藏、开启显示、不换算币种和切换开关后的缓存隔离测试。

另已用本机 Codex CLI `0.155.1` 完成真实额度查询，未发送模型请求。DeepSeek 通信使用本地 HTTP 模拟验证，没有读取真实 API Key；真实 AstrBot 加载和 QQ 等消息平台还需部署后联调。

图片部分另对官方 `v4.28.1` 的 `Image`、`Plain` 组件做过独立烟测，验证本地路径与 URL 构造；未启动完整 AstrBot，不替代真实消息平台的图片发送测试。

安装后建议依次验证管理员的三个命令、强唤醒查询、未唤醒群消息、非管理员查询以及解释性问题。预期查询只回复一次，未唤醒和解释性问题保持原流程。

## 官方参考

- [AstrBot 消息与唤醒、事件传播](https://docs.astrbot.app/dev/star/guides/listen-message-event.html)
- [AstrBot v4.28.1 唤醒检查](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.1/astrbot/core/pipeline/waking_check/stage.py)
- [AstrBot v4.28.1 提供商接口](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.1/astrbot/core/star/context.py)
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [DeepSeek 官方余额查询](https://api-docs.deepseek.com/zh-cn/api/get-user-balance/)
