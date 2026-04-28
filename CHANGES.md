# 修改说明

## 功能需求
1. 去掉 AI 下注功能
2. 修改下注模式为每次下注相同方向（开 1 继续下注 1，开 0 继续下注 0）
3. 遇到 10101 和 01010 模式时，第 6 次下注强制改为交替循环

## 修改内容

### 1. 添加简易预测函数
在 `fallback_prediction` 函数后添加了 `simple_prediction` 函数：

```python
def simple_prediction(history):
    """
    简易预测逻辑：
    1. 检测 10101 或 01010 模式，第 6 次强制交替
    2. 默认追注：开 1 下 1，开 0 下 0
    """
    if not isinstance(history, list) or len(history) < 1:
        return 1
    
    # 检测交替循环模式（10101 或 01010）
    if len(history) >= 5:
        recent_5 = history[-5:]
        seq = "".join(str(x) for x in recent_5)
        
        if seq in {"10101", "01010"}:
            # 第 6 次强制交替
            last_value = recent_5[-1]
            return 1 - last_value
    
    # 默认追注：跟最新一手相同
    return history[-1]
```

### 2. 简化核心预测函数
将 `predict_next_bet_core` 函数替换为简化版本，直接调用 `simple_prediction`：

```python
async def predict_next_bet_core(user_ctx: UserContext, global_config: dict, current_round: int = 1) -> int:
    """
    简化预测：使用简易逻辑决定下注方向
    1. 检测 10101 或 01010 模式，第 6 次强制交替
    2. 默认追注：开 1 下 1，开 0 下 0
    """
    # ... 使用 simple_prediction(history) ...
```

### 3. 移除 AI 相关逻辑
- 移除了模型超时处理
- 移除了模型健康检查
- 移除了模型降级链
- 移除了 `_apply_alternation_break_override`和`_apply_fixed_pattern_override` 的调用

## 工作逻辑

### 正常模式
- 历史：`[1, 0, 1]` -> 预测：**1**（追注最新一手）
- 历史：`[0, 1, 0, 0]` -> 预测：**0**（追注最新一手）

### 交替循环模式
- 历史：`[1, 0, 1, 0, 1]` -> 检测到 10101 -> 预测：**0**（强制交替）
- 历史：`[0, 1, 0, 1, 0]` -> 检测到 01010 -> 预测：**1**（强制交替）

### 循环效果
一旦进入 10101 或 01010 模式，会持续交替：
```
[1, 0, 1, 0, 1] -> 预测 0 -> 结果 0 -> 历史变为 [1, 0, 1, 0, 1, 0]
[1, 0, 1, 0, 1, 0] -> 检测到 01010 -> 预测 1 -> 结果 1 -> 历史变为 [1, 0, 1, 0, 1, 0, 1]
[1, 0, 1, 0, 1, 0, 1] -> 检测到 10101 -> 预测 0 -> ...
```

## 测试验证
已对 `simple_prediction` 函数进行单元测试，所有测试用例通过。
