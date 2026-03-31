# 常见问题

## 1. 为什么删了 state.json，预设还在？

因为：

- `state.json` 管运行状态
- 预设在 `presets.json` 和内置预设里

## 2. 为什么 yss 里会看到旧预设？

通常是：

- 运行版本过旧
- 账号目录里还留着旧 `presets.json`

新版本会自动丢弃旧 `yc*`。

## 3. 为什么会出现“本轮下注响应超时”？

常见原因不是模型，而是：

- 高金额需要拆很多个按钮
- 点击阶段耗时太长

## 4. 为什么 update 会被阻塞？

因为 VPS 上有 Git 脏文件。

最常见的是：

- `README.md`
- `constants.py`
- `docs/CHANGELOG.md`
- `users/<账号>/presets.json`

## 5. 管理员 Bot 和通知 Bot 有什么区别？

- `admin_console.telegram_bot`
  负责命令交互
- `notification.channels.telegram_notify_bot`
  负责发通知

它们不是同一个概念。

## 6. 为什么 stats 和我手动数的结果不完全一样？

因为当前 `stats`：

- 只统计当前策略链
- 只统计已结算记录
- 不把未结算和异常挂单算进去

