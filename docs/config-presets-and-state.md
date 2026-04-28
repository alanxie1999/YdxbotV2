# 预设与状态文件

这页专门说明两个容易混淆的文件：

- `presets.json`
- `state.json`

## 一、presets.json 是做什么的

`presets.json` 用来保存当前账号可用的预设。

典型位置：

```text
users/<账号>/presets.json
```

当前内置预设命名统一为：

- `5k`
- `1w`
- `2w`
- `3w`
- `5w`
- `10w`
- `15w`
- `20w`
- `30w`

## 二、state.json 是做什么的

`state.json` 保存的是运行态，不是预设。

典型位置：

```text
users/<账号>/state.json
```

它里面主要包括：

- history
- bet_type_history
- predictions
- bet_sequence_log
- runtime

## 三、删 state.json 会发生什么

删除 `state.json` 只会重置：

- 历史
- 统计
- 当前运行状态

不会删除：

- `presets.json`
- 内置预设

所以如果你删了 `state.json`，但 `yss` 里预设还在，这是正常的。

## 四、删 presets.json 会发生什么

删除 `presets.json` 会清掉账号自己的自定义预设文件。

但如果代码里还有内置预设，脚本启动后仍然会重新加载内置预设。

## 五、当前版本的处理方式

当前版本中：

- 内置预设已经统一为新的 bankroll 命名
- 旧 `yc*` 预设在加载时会被自动丢弃

所以如果你更新到了新版本，旧 `yc*` 一般不会再回流。

## 六、什么时候需要删 state.json

建议只在这些场景删：

- 想完全重新开始一轮
- 历史状态明显混乱
- 需要清空统计与链路

如果只是想换预设，不需要删 `state.json`。

