# 更新与发布

这页用于说明日常更新、回退和版本发布流程。

## 一、使用者更新

常用命令：

```text
ver
update [版本]
reback [版本]
restart
```

推荐顺序：

1. `ver`
2. `update vX.Y.Z`
3. `restart`
4. `status`

## 二、如果 update 被阻塞

常见原因：

- VPS 上有 Git 脏文件
- 示例配置、说明文档或账号预设被手工改过

排查请结合：

- [常见故障排查](ops-troubleshooting.md)

## 三、版本发布原则

当前项目统一使用：

- Git Tag
- GitHub Release

同一版本号。

示例：

- Tag：`v1.2.5`
- Release：`v1.2.5`

## 四、发布前建议

发布前至少确认：

- 工作区干净
- 测试通过
- 更新日志已补充
- tag / release 说明已准备好

