#!/bin/bash
# XA-202620 技术方案报告编译脚本
# 用法: bash build.sh
set -e
cd "$(dirname "$0")"
xelatex -interaction=nonstopmode main.tex
bibtex main
xelatex -interaction=nonstopmode main.tex
xelatex -interaction=nonstopmode main.tex   # 第三遍稳定交叉引用与目录
echo "=== 编译完成: main.pdf ==="
ls -la main.pdf
