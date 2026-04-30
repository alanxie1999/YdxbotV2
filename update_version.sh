#!/bin/bash
# 自动更新版本号和日期脚本
# 用法：./update_version.sh [major|minor|patch]

set -e

VERSION_FILE="zq_multiuser.py"
CHANGELOG_FILE="docs/CHANGELOG.md"

# 获取当前版本号
CURRENT_VERSION=$(grep "^版本:" "$VERSION_FILE" | awk '{print $2}')
echo "当前版本：$CURRENT_VERSION"

# 解析版本号
MAJOR=$(echo "$CURRENT_VERSION" | cut -d. -f1)
MINOR=$(echo "$CURRENT_VERSION" | cut -d. -f2)
PATCH=$(echo "$CURRENT_VERSION" | cut -d. -f3)

# 确定更新类型（默认 patch）
UPDATE_TYPE="${1:-patch}"

case "$UPDATE_TYPE" in
    major)
        MAJOR=$((MAJOR + 1))
        MINOR=0
        PATCH=0
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
    patch)
        PATCH=$((PATCH + 1))
        ;;
    *)
        echo "错误：更新类型必须是 major、minor 或 patch"
        exit 1
        ;;
esac

NEW_VERSION="$MAJOR.$MINOR.$PATCH"
CURRENT_DATE=$(date +%Y-%m-%d)

echo "新版本：$NEW_VERSION"
echo "日期：$CURRENT_DATE"

# 更新 zq_multiuser.py 中的版本号
sed -i "s|^版本：.*|版本：$NEW_VERSION|" "$VERSION_FILE"
sed -i "s|^日期：.*|日期：$CURRENT_DATE|" "$VERSION_FILE"

# 更新 CHANGELOG.md 添加新版本的占位符
if ! grep -q "## v$NEW_VERSION" "$CHANGELOG_FILE"; then
    # 在文件开头插入新版本记录（在第一个 ## 之前）
    TEMP_FILE=$(mktemp)
    cat > "$TEMP_FILE" << EOF
## v$NEW_VERSION

发布日期：$CURRENT_DATE

更新内容：
- 自动生成的版本更新

EOF
    cat "$CHANGELOG_FILE" >> "$TEMP_FILE"
    mv "$TEMP_FILE" "$CHANGELOG_FILE"
fi

echo "✅ 版本号和日期已更新"
echo ""
echo "下一步操作："
echo "1. git add -A"
echo "2. git commit -m 'chore: 更新版本到 v$NEW_VERSION ($CURRENT_DATE)'"
echo "3. git push"
