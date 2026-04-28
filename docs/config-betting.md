# betting 时序配置

这页专门说明下注时序相关参数。

## 一、配置结构

```json
"betting": {
  "prompt_wait_sec": 1.2,
  "predict_timeout_sec": 8.0,
  "click_interval_sec": 0.45,
  "click_timeout_sec": 6.0
}
```

## 二、各字段作用

### `prompt_wait_sec`

盘口消息没有按钮时的等待补偿。

### `predict_timeout_sec`

模型预测超时。

### `click_interval_sec`

多按钮点击之间的间隔。

### `click_timeout_sec`

单次按钮点击超时。

## 三、当前默认值

当前默认值已经恢复到：

- `prompt_wait_sec = 1.2`
- `predict_timeout_sec = 8.0`
- `click_interval_sec = 0.45`
- `click_timeout_sec = 6.0`

## 四、什么时候需要调整

常见情况：

- 新 VPS 网络慢
- 高金额组合点击较多
- 模型接口响应时间明显波动

## 五、使用建议

- 不建议频繁改动
- 如果实盘主要问题是“高金额点击超时”，优先检查 `click_timeout_sec`
- 如果主要问题是“模型请求超时”，再看 `predict_timeout_sec`

