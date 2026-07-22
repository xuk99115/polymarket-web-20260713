"""GitHub runtime backup helpers.

Only exports the recovery-critical runtime state:
- paper_trade_state.json
- state_summary.json
- bot_status.json
- direction_state.json
- sync_health.json
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BACKUP_FILES = [
    "paper_trade_state.json",
    "state_summary.json",
    "bot_status.json",
    "direction_state.json",
    "sync_health.json",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_runtime_snapshot(runtime_dir: str) -> dict:
    base = Path(runtime_dir)
    files = []
    file_hashes = {}
    for name in BACKUP_FILES:
        path = base / name
        if not path.exists():
            raise FileNotFoundError(f"missing runtime file: {name}")
        files.append(name)
        file_hashes[name] = _sha256(path)

    state = json.loads((base / "paper_trade_state.json").read_text(encoding="utf-8"))
    summary = state.get("summary", {}) or {}
    stats = state.get("stats", {}) or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cash_balance": state.get("cash_balance"),
        "realized_pnl": summary.get("realized_pnl"),
        "positions": len(state.get("positions", []) or []),
        "trade_events": len(state.get("trades", []) or []),
        "closed_trades": stats.get("total_trades"),
        "files": files,
        "file_hashes": file_hashes,
    }


def has_runtime_state_changed(prev: dict, curr: dict) -> bool:
    keys = ("cash_balance", "realized_pnl", "positions", "trade_events", "closed_trades", "file_hashes")
    return any(prev.get(k) != curr.get(k) for k in keys)


def write_runtime_backup(runtime_dir: str, backup_dir: str, snapshot: dict) -> None:
    src = Path(runtime_dir)
    dst = Path(backup_dir)
    dst.mkdir(parents=True, exist_ok=True)
    for name in BACKUP_FILES:
        shutil.copy2(src / name, dst / name)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime_dir": str(src),
        "files": BACKUP_FILES,
        "snapshot": snapshot,
    }
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def push_runtime_backup_via_temp_clone(
    runtime_dir: str,
    remote: str,
    branch: str,
    backup_subdir: str = "runtime-backup",
) -> dict:
    """Clone GitHub into /tmp, write the snapshot, commit only on change, push."""
    snapshot = compute_runtime_snapshot(runtime_dir)
    with __import__("tempfile").TemporaryDirectory(prefix="polymarket-runtime-backup-") as temp:
        repo = Path(temp) / "repo"
        subprocess.run(["git", "clone", "--quiet", remote, str(repo)], check=True)
        branches = subprocess.check_output(
            ["git", "-C", str(repo), "branch", "--remote", "--list", f"origin/{branch}"],
            text=True,
        )
        if f"origin/{branch}".strip() in branches.split():
            subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", "-B", branch, f"origin/{branch}"], check=True)
        else:
            subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", "-B", branch, "origin/main"], check=True)
        backup_dir = repo / backup_subdir
        manifest_path = backup_dir / "manifest.json"
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8")).get("snapshot")
        if previous and not has_runtime_state_changed(previous, snapshot):
            return {"changed": False, "snapshot": snapshot}
        write_runtime_backup(runtime_dir, str(backup_dir), snapshot)
        subprocess.run(["git", "-C", str(repo), "add", backup_subdir], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "runtime-backup[bot]"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "runtime-backup[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "chore: snapshot runtime state"], check=True)
        subprocess.run(["git", "-C", str(repo), "push", "origin", f"HEAD:{branch}"], check=True)
    return {"changed": True, "snapshot": snapshot}
