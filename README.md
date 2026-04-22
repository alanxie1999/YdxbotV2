# YdxbotV2

YdxbotV2 是一个基于 Telegram 的多账号自动化脚本。

它面向长期在群里手动盯盘、手动押注的使用者，核心目标是把重复操作、状态查看、风险提醒和多账号管理收进一套统一流程。

## 主要功能

- 自动接收盘口消息与结算消息
- 按预设参数自动下注
- 支持多账号独立运行
- 支持模型预测与统计兜底
- 支持连输告警、盈利暂停、炸号保护
- 支持模型异常探测、自动切换与自动恢复
- 支持管理员控制台与通知渠道分离
- 支持版本更新、回退与运行状态查看

## 适用场景

- 长期人工盯盘、重复手动下注
- 需要同时管理多个账号
- 希望把告警、暂停、恢复、统计查看集中起来

## 免责声明

本项目以开源形式提供，仅供学习、测试与技术研究使用。

使用者应自行判断其适用范围，并自行承担部署、运行、配置、更新及使用过程中产生的一切风险与责任。

项目维护者与贡献者不对任何直接或间接损失、封禁、数据异常、账户风险、平台风险或其他后果承担责任。

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/ibarnard/YdxbotV2.git
cd YdxbotV2
```

### 2. 安装依赖

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

如果遇到权限问题，可以使用：

```bash
pip install -r requirements.txt --break-system-packages
```

### 3. 配置并启动

详细配置步骤请查看 [快速开始文档](docs/quick-start.md)

```bash
python3 main_multiuser.py
```

## 文档入口

详细文档请查看 Wiki：

- Wiki 首页：[https://ibarnard.github.io/YdxbotV2/](https://ibarnard.github.io/YdxbotV2/)
- 快速开始：[https://ibarnard.github.io/YdxbotV2/quick-start/](https://ibarnard.github.io/YdxbotV2/quick-start/)
- 配置说明：[https://ibarnard.github.io/YdxbotV2/config/](https://ibarnard.github.io/YdxbotV2/config/)
- 命令参考：[https://ibarnard.github.io/YdxbotV2/commands/](https://ibarnard.github.io/YdxbotV2/commands/)

## 常见问题

### ModuleNotFoundError: No module named 'telethon'

这是因为依赖包未安装，请执行：

```bash
pip install -r requirements.txt
```

如果仍有问题，尝试：

```bash
pip install -r requirements.txt --break-system-packages
```
