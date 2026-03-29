# 快速开始

这页只讲一件事：

**怎样在一台新机器上把脚本最小化跑起来。**

## 1. 克隆仓库

```bash
git clone https://github.com/ibarnard/YdxbotV2.git
cd YdxbotV2
```

## 2. 准备 Python 环境

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. 准备通用配置

```bash
cp config/global_config.example.json config/global_config.json
```

编辑 [config/global_config.json](/Users/barnard/code/YdxbotV2/config/global_config.json)。

至少确认这些内容：

- 监听来源 `groups.zq_group`
- 结算/触发来源 `groups.zq_bot`
- 更新 token（如需要）

## 4. 新建一个账号目录

下面以 `xu` 为例：

```bash
mkdir -p users/xu
cp users/_template/example_config.json users/xu/xu_config.json
cp users/_template/state.json.default users/xu/state.json
cp users/_template/presets.json.default users/xu/presets.json
```

## 5. 编辑账号配置

编辑：

```text
users/xu/xu_config.json
```

至少填这些：

- `telegram.api_id`
- `telegram.api_hash`
- `telegram.session_name`
- `telegram.user_id`
- `account.name`
- `zhuque.cookie`
- `zhuque.x_csrf`
- `admin_console`
- `notification.channels`
- `ai`

详细结构见 [配置说明](config.md)。

## 6. 放置 session

把该账号对应的 `.session` 文件放到：

```text
users/xu/
```

## 7. 启动脚本

```bash
python3 main_multiuser.py
```

如果启动成功，你应该能看到：

- 控制台显示账号启动成功
- 管理员入口收到“脚本启动成功”通知

## 8. 首次验证

启动成功后，建议依次试：

```text
status
yss
help
```

如果管理员入口用的是 bot，则对应发送：

```text
/status
/yss
/help
```

## 常见第一步错误

### 没有管理员入口

当前版本要求 `admin_console` 必配。

如果没配：

- 账号配置会加载失败
- 脚本不会正常进入运行态

### 旧配置结构

当前版本已经把：

- 管理员入口
- 通知渠道

拆成了不同结构。

不要继续只写旧的：

```json
"notification": {
  "admin_chat": ...
}
```

请直接按新结构写 `admin_console`。

