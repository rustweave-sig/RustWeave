#!/bin/bash

set -euo pipefail

echo "🔍 扫描并清理 Rust 项目..."

# 遍历当前目录下所有子目录
for dir in */; do
    if [[ -f "$dir/Cargo.toml" && -d "$dir/target" ]]; then
        echo "📦 清理 Rust 项目: $dir"
        (cd "$dir" && cargo clean && cd ..)
    fi
done

echo "🧹 删除所有 __pycache__ 目录..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

echo "🧹 清理当前目录下的编译产物..."
# 清理 target 目录（如果存在）
[[ -d "./target" ]] && rm -rf ./target

# 清理 Python 编译产物
find . -type f -name "*.pyc" -delete
find . -type f -name "*.so" -delete

echo "✅ 清理完成！"
