# 命令参考

这页收日常使用中最常用、最重要的命令。

## 一、基础控制

```text
st [预设名]
status
pause
resume
balance
help
```

说明：

- `st [预设名]`
  启动并切换到指定预设
- `status`
  查看完整状态面板
- `pause / resume`
  手动暂停或恢复
- `balance`
  刷新账户余额

## 二、资金与目标

```text
gf [金额]
stf [数字]
wlc [数字]
```

### `gf [金额]`

设置菠菜资金。

示例：

```text
gf 2000000
```

### `stf [数字]`

设置本轮目标金额，单位直接是“万”。

示例：

```text
stf 100
```

表示：

- 本轮目标金额 = `100 万`

### `wlc [数字]`

设置连输告警阈值。

示例：

```text
wlc 3
```

## 三、预设与测算

```text
yss
yss dl [名]
ys [名称] [连续] [止损] [一输] [二输] [三输] [四输] [首注]
yc [预设名]
yc [参数...]
```

### `yss`

查看当前账号可用的全部预设。

### `ys`

新增或覆盖一个预设。

示例：

```text
ys 2w 1 10 3.0 2.5 2.2 2.1 20000
```

### `yc`

测算预设或临时参数。

示例：

```text
yc 5k
yc 1 12 3.0 2.5 2.2 2.1 5000
```

## 四、模型与密钥

```text
model list
model select [编号/ID]
apikey show
apikey set [key]
apikey add [key]
apikey del [序号]
```

## 五、运维命令

```text
ver
update [版本]
reback [版本]
restart
```

说明：

- `ver`
  查看当前版本和可更新信息
- `update [版本]`
  更新到指定 tag 或最新版本
- `reback [版本]`
  回退到指定版本
- `restart`
  重启脚本

## 六、数据重置

```text
res tj
res state
res bet
```

说明：

- `res tj`
  重置收益/胜率统计
- `res state`
  重置运行状态和历史
- `res bet`
  只重置当前倍投链
