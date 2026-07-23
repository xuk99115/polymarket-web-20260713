#!/usr/bin/env bash
# ==============================================================================
# Polymarket BTC Trading Bot - 一键启动脚本
# ==============================================================================
set -euo pipefail

# 基础路径
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 解释器优先 /tmp，避免 /data FUSE EIO 导致 pycryptodome 原生库加载失败
TMP_VENV="${TMP_VENV:-/tmp/polymarket-fv-edge-venv}"
SCRIPTS_DIR="$ROOT_DIR/scripts"

echo "🚀 正在初始化 Polymarket 交易终端..."

# 1. 检查 Python 环境
if ! command -v python3 &>/dev/null; then
    echo "❌ 错误: 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

# 2/3. venv + 依赖由 start_paper_sim.sh 的 ensure_tmp_venv 统一处理
#    （创建 /tmp/polymarket-fv-edge-venv、按 requirements 同步、Crypto 冒烟）
if [[ -x "$TMP_VENV/bin/python3" ]]; then
    echo "   ✓ 将使用 /tmp venv: $TMP_VENV"
else
    echo "   ↻ /tmp venv 不存在，启动脚本将自动创建: $TMP_VENV"
fi

# 4. 配置文件校验
if [ ! -f "$ROOT_DIR/.env" ]; then
    if [ -f "$ROOT_DIR/.env.example" ]; then
        echo "⚠️  未发现 .env 文件，正在从模板创建..."
        cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
        echo "💡 请记得在 .env 中填入您的真实 API Key 等信息。"
    else
        echo "❌ 错误: 缺少 .env 配置文件且未找到模板。"
        exit 1
    fi
fi

# 5. 确保 data / runtime 目录存在
mkdir -p "$ROOT_DIR/data" /tmp/polymarket-fv-edge/data /tmp/polymarket-fv-edge/logs

# 6. 启动服务
echo "🚦 正在启动交易引擎与监控服务器..."
chmod +x "$SCRIPTS_DIR/start_paper_sim.sh"
chmod +x "$SCRIPTS_DIR/stop_paper_sim.sh"

bash "$SCRIPTS_DIR/start_paper_sim.sh"

echo "✨ 启动成功！"
echo "----------------------------------------------------"
echo "📊 控制台地址: http://localhost:8889"
echo "🐍 解释器: ${TMP_VENV}/bin/python3"
echo "🛑 如需停止，请运行: ./scripts/stop_paper_sim.sh"
echo "----------------------------------------------------"

# 自动尝试打开浏览器 (仅限 macOS)
if [[ "$OSTYPE" == "darwin"* ]]; then
    sleep 1
    open "http://localhost:8889"
fi
