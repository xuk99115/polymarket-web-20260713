#!/usr/bin/env python3
"""
仓位审计日志模块
拆自 manager.py 的 _append_position_audit.
"""

import json
import logging

logger = logging.getLogger("trading_manager")


def _append_position_audit(file_path: str, event: dict) -> None:
    """追加一条仓位事件到 JSONL 审计 log (每行一个 JSON 对象)."""
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        logger.debug("[Audit] 写审计 log 失败: %s", exc)
