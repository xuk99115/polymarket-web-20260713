"""双向同步器：PERSIST_DIR <-> RUNTIME_DIR

职责:
  1. 启动时: PERSIST -> RUNTIME (从永久卷恢复最新数据)
  2. 定时: RUNTIME -> PERSIST (每 30s 备份日志到永久卷)
  3. 停止时: RUNTIME -> PERSIST (强制同步最后一次数据)

安全策略:
  - 写 .tmp 再 mv 替换，避免永久卷写坏时连累旧数据
  - 单向覆盖: 启动时 PERSIST 是权威，运行时 RUNTIME 是权威
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("sync_runtime")

# 需要同步的文件列表
SYNC_FILES = [
    "btc_ticks.jsonl",
    "fair_value_predictions.jsonl",
    "btc_snapshot.json",
    "btc_window_refs.json",
    "position_audit.jsonl",
]


def _atomic_copy(src: str, dst: str) -> None:
    """先写 .tmp 再 mv，避免写坏永久卷。"""
    dst_dir = os.path.dirname(dst)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dst_dir, suffix=".tmp")
    try:
        os.close(tmp_fd)
        shutil.copy2(src, tmp_path)
        os.rename(tmp_path, dst)  # 原子替换
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def sync_persist_to_runtime(persist_dir: str, runtime_dir: str) -> int:
    """启动时调用：把永久卷的最新数据恢复到临时卷。"""
    count = 0
    for fname in SYNC_FILES:
        src = os.path.join(persist_dir, fname)
        dst = os.path.join(runtime_dir, fname)
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
                count += 1
            except Exception as e:
                logger.warning("sync P->R failed %s: %s", fname, e)
    return count


def sync_runtime_to_persist(runtime_dir: str, persist_dir: str) -> int:
    """定时调用：把临时卷的数据备份到永久卷。"""
    count = 0
    for fname in SYNC_FILES:
        src = os.path.join(runtime_dir, fname)
        dst = os.path.join(persist_dir, fname)
        if os.path.exists(src):
            try:
                os.makedirs(persist_dir, exist_ok=True)
                _atomic_copy(src, dst)
                count += 1
            except Exception as e:
                logger.warning("sync R->P failed %s: %s", fname, e)
    return count


async def periodic_sync(
    runtime_dir: str,
    persist_dir: str,
    interval: float = 30.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """后台定时同步：每 interval 秒 RUNTIME -> PERSIST。"""
    logger.info("Periodic sync started: %s -> %s (every %.0fs)", runtime_dir, persist_dir, interval)
    while stop_event is None or not stop_event.is_set():
        try:
            synced = sync_runtime_to_persist(runtime_dir, persist_dir)
            if synced > 0:
                logger.debug("Synced %d files to persist dir", synced)
        except Exception as e:
            logger.error("Periodic sync error: %s", e)

        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            else:
                await asyncio.sleep(interval)
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass


def force_sync(runtime_dir: str, persist_dir: str) -> int:
    """停止时调用：强制同步所有文件到永久卷。"""
    logger.info("Force sync: %s -> %s", runtime_dir, persist_dir)
    try:
        return sync_runtime_to_persist(runtime_dir, persist_dir)
    except Exception as e:
        logger.error("Force sync failed: %s", e)
        return 0
