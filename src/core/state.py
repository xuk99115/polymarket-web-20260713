import os
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from .utils import load_json_file, save_json_file
from .config import Config, PAPER_STATE_FILE, STATUS_FILE

logger = logging.getLogger("state_manager")

class StateManager:
    """管理机器人状态持久化 (PnL, Positions, trades)

    Bug fix 2026-07-01: 加 batch 模式 (defer_save).
    历史问题: 单个 trading cycle 内多次 self.state_manager.save() 会产生多个
    不同的"中间帧"写到 disk. 前端每 15s 轮询时如果撞上 save 之间, 会看到 trades
    数量 / stats 在两个数字之间跳 (例如 99 ↔ 101 ↔ 102).
    根因: 同一个 cycle 关闭多笔仓位时每关一笔就 save 一次, 而 save 之间 stats
    还没累加完. 修法: 用 defer_save 上下文, cycle 入口打开, cycle 末尾统一
    flush 一次. 内存里的 state 改动始终即时生效 (其他代码读 state 看到的是
    最新), 只是 disk write 合并.
    """

    def __init__(self, state_file: str):
        self.state_file = state_file
        self.state = self.load()
        # batch mode: True 时 save() 只标记 dirty, 不实际写盘
        self._defer_save: bool = False
        self._dirty: bool = False

    def load(self) -> Dict[str, Any]:
        return load_json_file(self.state_file, self._get_default_state())

    def save(self, force: bool = False):
        """持久化 state. 默认 batch-mode 时只标 dirty 不写盘; force=True 时即使
        在 batch 也立刻写. 退出 batch 模式时若有 dirty 自动 flush 一次."""
        if self._defer_save and not force:
            self._dirty = True
            return
        save_json_file(self.state_file, self.state)
        self._dirty = False

    def flush(self):
        """强制把 dirty 状态落盘. batch 退出时调用."""
        # 每次都写 summary 快照（status_server 优先读这个）
        summary = self.state.get("summary", {})
        if summary:
            summary_file = os.path.join(os.path.dirname(self.state_file), "state_summary.json")
            try:
                save_json_file(summary_file, summary)
            except Exception:
                pass
        if self._dirty:
            save_json_file(self.state_file, self.state)
            self._dirty = False

    def get_state(self) -> Dict[str, Any]:
        return self.state

    def update(self, key: str, value: Any):
        self.state[key] = value
        self.save()

    def _get_default_state(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "mode": "paper_live",
            "generated_at": now,
            "session_started_at": now,
            "cash_balance": Config.get_float("PAPER_START_BALANCE", "100"),
            "positions": [],
            "orders": [],
            "trades": [],
            "closed_markets": [],
            "fv_signal_history": [],
            "stats": {"total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_profit": 0.0},
            "market": {},
            "summary": {},
            "report": {},
        }

class StatusExporter:
    """负责将当前活跃状态写入 bot_status.json 供前端读取"""

    @staticmethod
    def export(data: Dict[str, Any]):
        save_json_file(STATUS_FILE, data)
