#!/usr/bin/env python3
"""
Polymarket 通用盘口交易机器人 - 引导脚本
支持双目录架构 + 定时同步 + 原子替换
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import signal
import sys
import os
from src.core.sync_runtime import force_sync, periodic_sync

def configure_logging(
    log_dir: str | None = None,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 5,
) -> RotatingFileHandler:
    """把 Bot 日志写入临时盘并限制总量，隔离持久卷 EIO。"""
    runtime_log_dir = log_dir or os.environ.get(
        "RUNTIME_LOG_DIR", "/tmp/polymarket-fv-edge/logs"
    )
    os.makedirs(runtime_log_dir, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(runtime_log_dir, "paper_bot.log"),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    handler._polymarket_runtime_handler = True

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        if getattr(existing, "_polymarket_runtime_handler", False):
            root_logger.removeHandler(existing)
            existing.close()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    return handler


logger = logging.getLogger("bot")

# 运行态唯一数据目录；GitHub 是恢复备份，不参与交易主循环。
RUNTIME_DIR = os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data")

_stop_event = asyncio.Event()


async def main():
    from src.trading.manager import TradingBotManager

    # GitHub 备份在独立脚本中执行，不阻塞交易循环。
    manager = TradingBotManager()

    # 注册 SIGTERM/SIGINT 处理器
    def _signal_handler():
        logger.info("Received shutdown signal")
        manager.running = False
        _stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    periodic_task = asyncio.create_task(
        periodic_sync(
            os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data"),
            os.environ.get("PERSIST_DIR", os.path.join(os.path.dirname(__file__), "data")),
            interval=float(os.environ.get("RUNTIME_SYNC_INTERVAL_SECONDS", "300")),
            stop_event=_stop_event,
        ),
        name="runtime-periodic-sync",
    )

    # 运行交易主循环
    try:
        await manager.start()
    finally:
        _stop_event.set()
        try:
            await asyncio.wait_for(periodic_task, timeout=5.0)
        except Exception:
            periodic_task.cancel()
            try:
                await periodic_task
            except Exception:
                pass
        try:
            force_sync(
                os.environ.get("RUNTIME_DIR", "/tmp/polymarket-fv-edge/data"),
                os.environ.get("PERSIST_DIR", os.path.join(os.path.dirname(__file__), "data")),
            )
        except Exception as exc:
            logger.warning("Final force sync failed: %s", exc)


if __name__ == "__main__":
    configure_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 收到停止信号，程序退出。")
    except Exception as e:
        print(f"\n❌ 程序异常: {e}")
        raise
