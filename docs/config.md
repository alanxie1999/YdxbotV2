# 配置说明

当前配置分成两层：

- 通用配置：`config/global_config.json`
- 账号配置：`users/<账号>/<账号>_config.json`

## 一、目录结构

典型账号目录：

```text
users/
  xu/
    xu_config.json
    state.json
    presets.json
    xu.session
```

说明：

- `*_config.json`：账号主配置
- `state.json`：运行状态
- `presets.json`：账号可用预设
- `.session`：Telegram 登录会话

## 二、管理员入口：admin_console

当前版本要求 `admin_console` 必配。

只能二选一：

- `telegram_id`
- `telegram_bot`

### 1. telegram_id 模式

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

适合：

- 自己的 Telegram 私聊
- 专用管理 chat

### 2. telegram_bot 模式

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

适合：

- 每个账号一个独立管理员 bot
- 命令和管理消息不想混在个人聊天里

说明：

- 当前只支持 bot 私聊
- 不支持群
- 不支持双入口同时收命令

## 三、通知渠道：notification.channels

通知渠道只负责“发通知”，不负责命令交互。

```json
"notification": {
  "channels": {
    "iyuu": {
      "enable": false,
      "url": "",
      "token": ""
    },
    "telegram_notify_bot": {
      "enable": true,
      "bot_token": "",
      "chat_id": ""
    }
  }
}
```

说明：

- `iyuu`：重点通知
- `telegram_notify_bot`：Telegram 通知 bot
- 这两个和 `admin_console.telegram_bot` 不是一回事

## 四、AI 配置

当前项目主要支持 OpenAI 兼容方式。

典型结构：

```json
"ai": {
  "enabled": true,
  "api_keys": [
    "sk-xxx"
  ],
  "base_url": "https://integrate.api.nvidia.com/v1",
  "timeout": 45,
  "max_retries": 3,
  "rate_limit_rpm": 40,
  "models": {
    "1": {
      "model_id": "qwen/qwen3-next-80b-a3b-instruct",
      "enabled": true
    }
  },
  "fallback_chain": ["1"]
}
```

重点字段：

- `api_keys`
- `base_url`
- `timeout`
- `rate_limit_rpm`
- `models`
- `fallback_chain`

## 五、betting 时序配置

当前默认值与 `v1.2.0` 对齐：

```json
"betting": {
  "prompt_wait_sec": 1.2,
  "predict_timeout_sec": 8.0,
  "click_interval_sec": 0.45,
  "click_timeout_sec": 6.0
}
```

说明：

- `prompt_wait_sec`：盘口消息没有按钮时的等待补偿
- `predict_timeout_sec`：模型预测超时
- `click_interval_sec`：多按钮点击之间的间隔
- `click_timeout_sec`：单次按钮点击超时

## 六、预设文件

模板预设现在统一为：

- `5k`
- `1w`
- `2w`
- `3w`
- `5w`
- `10w`
- `15w`
- `20w`
- `30w`

如果旧账号目录里还留着旧 `yc*` 预设，当前版本加载时会自动丢弃这些旧键。

