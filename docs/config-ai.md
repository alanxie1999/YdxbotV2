# AI 与模型配置

这页说明 AI 模型链相关配置。

## 一、基本结构

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
    },
    "2": {
      "model_id": "moonshotai/kimi-k2-instruct",
      "enabled": true
    }
  },
  "fallback_chain": ["1", "2"]
}
```

## 二、关键字段

### `api_keys`

当前账号可用的模型 key 列表。

### `base_url`

模型接口地址。

### `timeout`

单次模型请求总超时。

### `max_retries`

单次模型调用的重试次数。

### `rate_limit_rpm`

每分钟请求上限。

### `models`

当前账号可用的模型列表。

### `fallback_chain`

模型失败时的切换顺序。

## 三、实际运行逻辑

脚本会：

1. 优先使用当前模型
2. 失败时按 `fallback_chain` 切换
3. 整条模型链都失败时走统计兜底
4. 连续兜底达到阈值后暂停并后台探测恢复

## 四、配置建议

- 每个账号单独配置自己的 `api_keys`
- `fallback_chain` 不要留空
- 新模型接入后，先确认返回格式兼容再放进生产链

