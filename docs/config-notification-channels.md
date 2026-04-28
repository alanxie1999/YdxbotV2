# 通知渠道配置

通知渠道只负责“发通知”，不负责命令交互。

当前版本已经把：

- 管理员入口
- 通知出口

分成了不同配置块。

## 一、配置位置

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

## 二、支持的通知渠道

### 1. IYUU

适合：

- 重点告警推送
- 多渠道通知

### 2. telegram_notify_bot

适合：

- Telegram 重点通知
- 多账号统一汇总提醒

说明：

- 它和 `admin_console.telegram_bot` 不是一回事
- 它只负责发通知，不负责收命令

## 三、典型用途

通常会把：

- 连输告警
- 资金暂停
- 模型链失败
- 启动成功
- 更新结果

这类消息发到通知渠道。

## 四、设计原则

- 管理员控制台：负责收命令和回状态
- 通知渠道：负责发重点提醒

这样分开后：

- 不容易混淆
- 账号结构更清晰
- Bot 的职责也更明确

