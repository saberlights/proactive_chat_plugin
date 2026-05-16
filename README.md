# 主动私聊插件 (Proactive Chat Plugin)

让 MaiSaka 在不规律的时机自己想"要不要找某个人聊聊"，由她自己基于人设、记忆、当前情境决定开不开口、聊什么、用什么语气。仅限私聊。

## 设计哲学

这是一个**不做决策的插件**。

- 插件**不**预设动机、概率、话题、口吻。
- 插件**只**负责三件事：
  1. 在不规律的时机"叫醒" MaiSaka 思考一次；
  2. 采集外部世界信号（时段语义/节日/今日热点/天气）；
  3. 把对方身份、最近聊天回顾、外部世界打包成 intent，投递给 `maisaka.proactive.trigger`。
- 是否真的开口、用什么称呼、聊什么、几时聊，全部交还给 MaiSaka 的 planner + replyer，基于她自己的人设和记忆决定。

配置只暴露**防呆参数**（每日上限、单聊冷却、白名单）和**事实型偏好**（要不要看热点、看哪个城市天气），不暴露概率/话题/话术等策略参数。

## 工作流

```
on_load → 启动后台 _wakeup_loop
    ↓ 在 min/max_interval_minutes 区间内均匀随机睡眠
_do_one_wakeup_sweep
    ↓ chat.get_private_streams + 白名单过滤
    ↓ 对每个允许的私聊 stream
_try_wakeup_one → 构造情境快照（最近消息回顾 + 时段/节日/热点/天气）
    ↓ call_capability("maisaka.proactive.trigger", intent=..., metadata=...)
MaiSaka 主循环收到任务,planner 思考,自行决定要不要开口
```

## 安装

按 [MaiBot 插件 SDK 文档](https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md) 把本仓库放进 MaiBot 的 `plugins/` 目录即可。首次运行时宿主会自动按默认值生成 `config.toml`，再按下方说明编辑白名单。

## 配置

首次启动后会生成 `config.toml`，关键字段：

### `[wakeup]` 唤醒循环防呆

| 字段 | 默认 | 说明 |
|---|---|---|
| `min_interval_minutes` / `max_interval_minutes` | 60 / 180 | 每次睡眠随机区间，模拟"念头随机冒出" |
| `daily_wakeup_cap` | 24 | 全局每日最多投递多少次主动任务（硬上限） |
| `per_chat_min_gap_hours` | 6 | 单聊两次主动唤醒最少间隔（防骚扰） |
| `startup_delay_seconds` | 60 | 启动后多久才开始第一次唤醒 |

### `[whitelist]` 白名单

```toml
[whitelist]
mode = "strict"   # strict 仅找下方 entries / off 所有私聊都可能被找

[[whitelist.entries]]
platform = "qq"
user_id  = "你对方的 QQ 号"
identity = "对方在你这里登记的身份（如 哥哥/大学同学小明）"
```

`identity` 只是事实信号，MaiSaka 仍按人设决定实际称呼和口吻。

**空 entries + strict 模式 = 谁都不会被打扰**，这是有意的安全默认。

> ⚠️ 添加白名单条目**必须**用 `[[whitelist.entries]]` 段写多条，**不要**写 `entries = [...]` 数组形式。混用两种写法时 TOML 解析会把后者错位为前者的子字段，宿主合并时检测到 extra key 会触发整文件重写，导致注释丢失。

### `[external_world]` 外部世界

| 字段 | 默认 | 说明 |
|---|---|---|
| `enable_time_semantics` | true | 把"周六 20 点"翻译为"周末傍晚,大多数人下班放松" |
| `enable_festival` | true | 公历节日识别（不含农历，避免引依赖） |
| `enable_hot_topics` | true | 今日热点，作为"现实世界正在聊什么"的背景餐布 |
| `hot_topics_source` | `60s` | 可选 `60s`/`weibo`/`baidu`/`zhihu`/`douyin`/`custom` |
| `enable_weather` | false | wttr.in 免 key 天气，需填 `weather_city` |

外部世界信号只是参考素材，MaiSaka 完全可以基于自己的人设和心情自由发起任何无关话题（事实上更被鼓励）。

## 开发测试命令

私聊中输入 `/proactive test` 或 `/主动测试` 可立即触发一次主动唤醒，绕过白名单/冷却/概率/上限。

## 跑测试

```bash
uv run plugins/proactive_chat_plugin/tests/test_integration.py
```

不依赖 MaiBot 主程序，纯 mock 跑通插件的所有判断逻辑。

## 已知问题与绕过

### MaiBot 主程序的 `reply` 工具找不到 proactive task 的目标消息

**症状**：MaiSaka 的 planner 决定回复后调 `reply` 工具，把插件的 `task_id` 当成 `msg_id` 传过去，主程序的 `find_source_message_by_id` 找不到对应消息，工具失败。

**根因**：主程序 `src/maisaka/runtime.py` 的 `enqueue_proactive_task` 把 task 作为 `SessionBackedMessage` 写入 `_chat_history` 时没设置 `original_message`，单独保存的 `_proactive_anchor_message` 又不在 chat history 里，所以 `reply` 工具用 task_id 查不到目标。

**插件侧绕过**：本插件在 `intent` 文本里：
1. 明确告诉 LLM"任务编号不能当 msg_id 用，会报『未找到要回复的目标消息』"；
2. 把 DB 里对方最近一条真实消息 `msg_id` 作为**候选**列出来，并强调"当前 chat_history 可能已被裁剪，需要 LLM 自己核对"——插件能看 DB 但看不到 MaiSaka 运行时的 `_chat_history`，所以不能担保候选可用；
3. 引导 LLM 直接看头顶聊天历史，从 `<message msg_id="..." user="对方名">` 标签里挑一条值得接续的；
4. 历史里只有命令 / 系统提示时，引导 LLM 退到 `send_emoji` 发表情，或 `finish` / `no_action` 收手。

**绕过的局限**：如果当前 `_chat_history` 里没有任何对方的真实对话消息（如插件首次通过 `chat.open_session` 新开会话、或聊天历史只剩 `/xxx` 命令），LLM 会按提示退到 `send_emoji` 或直接放弃 ——**没有真正可作锚点的消息时，文字主动消息是发不出去的**。真正修复需要改主程序，让 `enqueue_proactive_task` 把 anchor message 挂到 chat history 的 `original_message` 字段上。

## License

GPL-v3.0-or-later
