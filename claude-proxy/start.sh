#!/bin/bash

echo "╔═══════════════════════════════════════╗"
echo "║     Claude API Proxy - Linux/Mac      ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 Python，请先安装 Python 3.8+"
    exit 1
fi

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "[提示] 创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 检查依赖
if [ ! -f "venv/bin/uvicorn" ]; then
    echo "[提示] 安装依赖..."
    pip install -r requirements.txt
fi

# 检查配置文件
if [ ! -f ".env" ]; then
    echo "[警告] 未找到 .env 文件，请先配置"
    echo "[提示] 复制 .env.example 到 .env 并填写配置"
    exit 1
fi

# 启动服务
echo "[启动] 正在启动服务..."
python -m proxy
