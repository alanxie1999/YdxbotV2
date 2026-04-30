#!/bin/bash
# 自动创建发布版本并推送
# 用法：./release.sh [major|minor|patch] "更新说明"

set -e

UPDATE_TYPE="${1:-patch}"
RELEASE_NOTE="${2:-自动版本更新}"

echo "🚀 开始发布流程..."
echo "更新类型：$UPDATE_TYPE"
echo "更新说明：$RELEASE_NOTE"
echo ""

# 1. 更新版本号和日期
echo "📝 更新版本号..."
./update_version.sh "$UPDATE_TYPE"

# 2. 获取新版本号
NEW_VERSION=$(grep "^版本:" zq_multiuser.py | awk '{print $2}')
CURRENT_DATE=$(date +%Y-%m-%d)

echo ""
echo "📦 新版本：v$NEW_VERSION"
echo ""

# 3. 添加更改到 git
echo "💾 提交更改..."
git add -A
git commit -m "chore: 更新版本到 v$NEW_VERSION ($CURRENT_DATE)

$RELEASE_NOTE"

# 4. 创建 git tag
echo "🏷️  创建 tag..."
git tag -a "v$NEW_VERSION" -m "Release v$NEW_VERSION - $CURRENT_DATE"

# 5. 推送到远程
echo "📤 推送到远程仓库..."
git push origin main
git push origin "v$NEW_VERSION"

echo ""
echo "✅ 发布完成！"
echo ""
echo "已创建："
echo "  - 提交：v$NEW_VERSION"
echo "  - Tag: v$NEW_VERSION"
echo ""
echo "在 Telegram 中使用以下命令更新："
echo "  update v$NEW_VERSION"
echo "  restart"
