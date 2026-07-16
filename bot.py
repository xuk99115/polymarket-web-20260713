#!/usr/bin/env python3
"""
Polymarket 通用盘口交易机器人 - 引导脚本
支持双目录架构 + 定时同步 + 原子替换
"""

import asyncio
import logging
import signal
import sys
import os

from src.trading.manager import TradingBotManager
from src.core.sync_runtime import (
    sync_persist_to_runtime,
    force_sync,
    periodic_sync,
)

# 设置日志格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("bot")

# 双目录路径（与 manager.py 保持一致）
RUNTIME_DIR = os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data")
PERSIST_DIR = os.environ.get("PERSIST_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))

_stop_event = asyncio.Event()


async def main():
    # 1. 启动时：PERSIST -> RUNTIME（恢复最新数据）
    logger.info("Startup sync: %s -> %s", PERSIST_DIR, RUNTIME_DIR)
    synced = sync_persist_to_runtime(PERSIST_DIR, RUNTIME_DIR)
    logger.info("Restored %d files from persist dir", synced)

    # 2. 启动后台同步任务
    sync_task = asyncio.create_task(
        periodic_sync(RUNTIME_DIR, PERSIST_DIR, interval=30.0, stop_event=_stop_event),
        name="periodic_sync"
    )

    # 3. 启动交易管理器
    manager = TradingBotManager()

    # 4. 注册 SIGTERM/SIGINT 处理器
    def _signal_handler():
        logger.info("Received shutdown signal")
        _stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    # 5. 运行交易主循环
    try:
        await manager.start()
    finally:
        # 6. 停止时：强制同步
        logger.info("Shutting down, force syncing...")
        _stop_event.set()
        synced = force_sync(RUNTIME_DIR, PERSIST_DIR)
        logger.info("Final sync: %d files written to persist dir", synced)
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 收到停止信号，程序退出。")
    except Exception as e:
        print(f"\n❌ 程序异常: {e}")
        raise
