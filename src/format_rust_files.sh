#!/bin/bash
# filepath: d:\desktop\C2RustXW\format_rust_files.sh

# 检查是否提供了目录参数
if [ $# -eq 0 ]; then
    echo "用法: ./format_rust_files.sh <目录路径>"
    exit 1
fi

INPUT_DIR="$1"

# 检查目录是否存在
if [ ! -d "$INPUT_DIR" ]; then
    echo "错误: 目录 '$INPUT_DIR' 不存在"
    exit 1
fi

# 检查是否安装了 rustfmt
if ! command -v rustfmt &> /dev/null; then
    echo "正在安装 rustfmt..."
    rustup component add rustfmt
    
    # 再次检查安装是否成功
    if ! command -v rustfmt &> /dev/null; then
        echo "错误: rustfmt 安装失败，请确保 rustup 已正确安装"
        exit 1
    fi
fi

echo "开始格式化 '$INPUT_DIR' 目录中的所有 Rust 文件..."

# 查找并格式化所有 .rs 文件
RUST_FILES_COUNT=0
FORMATTED_COUNT=0
FAILED_COUNT=0

# 使用数组而不是管道来避免子shell问题
mapfile -t RUST_FILES < <(find "$INPUT_DIR" -type f -name "*.rs")

for file in "${RUST_FILES[@]}"; do
    ((RUST_FILES_COUNT++))
    echo "正在格式化: $file"
    
    # 使用 rustfmt 格式化文件
    if rustfmt "$file"; then
        ((FORMATTED_COUNT++))
        echo "✓ 成功格式化: $file"
    else
        ((FAILED_COUNT++))
        echo "✗ 格式化失败: $file"
    fi
done

echo "格式化完成!"
echo "总共找到 $RUST_FILES_COUNT 个 Rust 文件"
echo "成功格式化: $FORMATTED_COUNT 个文件"
echo "格式化失败: $FAILED_COUNT 个文件"