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
from datetime import datetime, timezone
from pathlib import Path

from .utils import load_json_file, save_json_file

logger = logging.getLogger("sync_runtime")

# 需要同步的文件列表
SYNC_FILES = [
    # 运行时交易状态 / 前端快照
    "paper_trade_state.json",
    "state_summary.json",
    "bot_status.json",
    "direction_state.json",
    "fv_direction.jsonl",
    # 运行时行情与审计
    "btc_ticks.jsonl",
    "fair_value_predictions.jsonl",
    "btc_snapshot.json",
    "btc_window_refs.json",
    "position_audit.jsonl",
    # 同步健康快照
    "sync_health.json",
]
SYNC_HEALTH_FILE = "sync_health.json"


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


def sync_runtime_to_persist(
    runtime_dir: str,
    persist_dir: str,
    files: list[str] | None = None,
) -> int:
    """把运行时文件尽力备份到永久卷；失败不影响交易主循环。"""
    count = 0
    errors = []
    requested_files = list(files or SYNC_FILES)
    for fname in requested_files:
        src = os.path.join(runtime_dir, fname)
        dst = os.path.join(persist_dir, fname)
        if not os.path.exists(src):
            errors.append(f"{fname}: missing runtime source")
            logger.warning("sync R->P skipped %s: missing runtime source", fname)
            continue
        try:
            os.makedirs(persist_dir, exist_ok=True)
            _atomic_copy(src, dst)
            count += 1
        except Exception as e:
            errors.append(f"{fname}: {e}")
            logger.warning("sync R->P failed %s: %s", fname, e)

    health_path = os.path.join(runtime_dir, SYNC_HEALTH_FILE)
    previous = load_json_file(health_path, {})
    now = datetime.now(timezone.utc).isoformat()
    all_succeeded = bool(requested_files) and count == len(requested_files)
    health = {
        "last_sync_attempt_at": now,
        "last_sync_success_at": now if all_succeeded else previous.get("last_sync_success_at"),
        "sync_healthy": all_succeeded,
        "sync_files_synced": count,
        "sync_files_total": len(requested_files),
        "sync_error_count": int(previous.get("sync_error_count", 0)) + len(errors),
        "sync_last_error": errors[-1] if errors else None,
    }
    save_json_file(health_path, health)
    return count


async def periodic_sync(
    runtime_dir: str,
    persist_dir: str,
    interval: float = 300.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """后台低频同步：默认每 300 秒 RUNTIME -> PERSIST。"""
    logger.info("Periodic sync started: %s -> %s (every %.0fs)", runtime_dir, persist_dir, interval)
    while stop_event is None or not stop_event.is_set():
        try:
            sync_runtime_to_persist(runtime_dir, persist_dir)
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
