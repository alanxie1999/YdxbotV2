# 管理员控制台配置

管理员控制台负责：

- 收命令
- 回命令结果
- 发管理员侧状态消息

当前版本中，`admin_console` 是必配项。

## 一、二选一模式

只支持：

- `telegram_id`
- `telegram_bot`

不支持：

- 双入口同时收命令
- 群聊 Bot 控制台

## 二、telegram_id 模式

适合：

- 自己的 Telegram 私聊
- 单独管理 chat

示例：

```json
"admin_console": {
  "mode": "telegram_id",
  "telegram_id": {
    "chat_id": 1234567890
  },
  "telegram_bot": {
    "bot_token": "",
    "chat_id": "",
    "allowed_sender_ids": []
  }
}
```

说明：

- `chat_id` 必填
- 这种模式下不需要额外白名单
- 适合你自己直接在 Telegram 里发命令

## 三、telegram_bot 模式

适合：

- 每个账号一个独立管理员 bot
- 命令和状态结果不想混在个人聊天里

示例：

```json
"admin_console": {
  "mode": "telegram_bot",
  "telegram_id": {
    "chat_id": 1234567890
  },
  "telegram_bot": {
    "bot_token": "123456:ABC",
    "chat_id": "1234567890",
    "allowed_sender_ids": [1234567890]
  }
}
```

说明：

- `bot_token` 必填
- `chat_id` 必填
- `allowed_sender_ids` 建议填写
- 当前只支持 bot 私聊

## 四、管理员 Bot 的当前行为

当前版本中：

- 消息统一转成 HTML 渲染
- 标题、代码片段会正常加粗/高亮
- 底部不会再显示大 `/help` 按钮
- 会使用 Telegram 原生命令菜单

## 五、配置错误的后果

如果 `admin_console` 配置不完整：

- 账号配置会加载失败
- 脚本不会进入正常运行态

建议排查：

- `mode` 是否写对
- `telegram_id.chat_id` 是否为空
- `telegram_bot.bot_token` / `chat_id` 是否为空

