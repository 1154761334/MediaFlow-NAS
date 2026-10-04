#!/bin/bash
# ==============================================================================
# MediaFlow-NAS 流水线调度周期执行入口
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

# 自愈保证 Xunlei 本地 Unix Socket 权限
if [ -S "/volume1/@appstore/pan-xunlei-com/var/pan-xunlei-com.sock" ]; then
    chmod 666 /volume1/@appstore/pan-xunlei-com/var/pan-xunlei-com.sock 2>/dev/null || true
fi

# 启动核心调度器执行单轮自适应水位巡检
python3 "${PROJECT_ROOT}/core/orchestrator.py" --cycle
