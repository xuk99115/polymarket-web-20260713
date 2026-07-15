import json
import os
from datetime import datetime, timezone
from typing import Optional, Any

def load_json_file(path: str, default: Any) -> Any:
    """从文件加载 JSON，失败则返回默认值"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json_file(path: str, data: Any):
    """保存数据为 JSON 格式。原子写入: 先写 .tmp 再 os.replace，避免中途崩坏原文件。"""
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                # fsync 不可用（部分文件系统）不致命，跳过
                pass
        os.replace(tmp_path, path)
    except Exception:
        # 清理临时文件，避免堆积
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """安全转换为 float"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def first_float(*values: Any, default: Optional[float] = None) -> Optional[float]:
    """依次尝试多个值，取第一个能成功转 float 的。避免 `safe_float(safe_float(x, y), 0)` 这种嵌套。"""
    for v in values:
        result = safe_float(v, None)
        if result is not None:
            return result
    return default

def extract_market_slug(market_input: str) -> str:
    """从市场输入解析出 slug"""
    if not market_input:
        return ""
    value = str(market_input).strip()
    if not value:
        return ""
    for marker in ("polymarket.com/event/", "polymarket.com/market/"):
        if marker in value:
            value = value.split(marker, 1)[-1]
            break
    value = value.split("#", 1)[0].split("?", 1)[0].strip().strip("/")
    return value

def short_wallet(address: str) -> str:
    """展示缩写的钱包地址"""
    if not address or len(address) < 10:
        return address or "Unknown"
    return f"{address[:6]}...{address[-4:]}"

def iso_to_utc_dt(value: str) -> datetime:
    """ISO 字符串转 UTC datetime"""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def parse_json_list(value: Any) -> list:
    """解析 JSON 列表字符串"""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except Exception:
        return []

def load_trading_control(control_file_path: str) -> dict:
    """从控制文件加载交易开关状态"""
    state = {"trading_enabled": False}
    try:
        if os.path.exists(control_file_path):
            with open(control_file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            state["trading_enabled"] = bool(raw.get("trading_enabled", False))
            state["updated_at"] = raw.get("updated_at")
    except Exception:
        pass
    return state
