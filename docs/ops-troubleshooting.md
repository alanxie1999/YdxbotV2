# 常见故障排查

这页记录当前最常见、最值得先查的几类问题。

## 1. update 被阻塞

如果更新时看到：

- 存在未提交代码变更，已阻止更新

说明 VPS 上有 Git 脏文件。

最常见的阻塞文件包括：

- `README.md`
- `constants.py`
- `docs/CHANGELOG.md`
- `config/global_config.example.json`
- `users/<账号>/presets.json`

## 2. 按钮点击超时

如果看到：

- 本轮下注响应超时
- 本轮下注窗口已失效

通常不是模型问题，而是：

- 高金额需要拆很多个按钮
- 点击阶段总耗时过长

当前版本已经做了两层保护：

- 高金额组合点击超时放宽
- 点击失败不会再错误推进倍投链

## 3. 预设不一致

如果 `yss` 里同时看到旧 `yc*` 和新 `5k/1w/...`：

原因通常是：

- 账号目录还留着旧 `presets.json`
- 或运行版本还不是新预设体系

当前新版本已经：

- 统一内置预设命名
- 加载时会自动丢弃旧 `yc*`

## 4. state.json 越来越大

`state.json` 会增长，但不是无限增长。

当前代码会裁剪：

- `history`
- `predictions`
- `bet_type_history`
- `bet_sequence_log`

所以文件会增长到一个上限附近后趋于稳定。

## 5. 管理员 bot 格式异常

如果管理员 bot 里：

- 标题不加粗
- 底部出现大 `/help` 条

说明当前运行版本过旧。

新版本已经改成：

- 管理员 bot 消息统一转 HTML
- 使用 Telegram 原生命令菜单
