import json
import os
import errno
from datetime import datetime, timezone
from typing import Optional, Any

def load_json_file(path: str, default: Any) -> Any:
    """从文件加载 JSON，失败则返回默认值。避免与 bot 写入竞态。"""
    for attempt in range(3):
        try:
            if os.path.exists(path):
                # 先 copy 到内存再 parse，避免读到写入中的文件
                import shutil
                tmp_copy = path + ".read_tmp"
                try:
                    shutil.copy2(path, tmp_copy)
                    with open(tmp_copy, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    os.remove(tmp_copy)
                except Exception:
                    try:
                        os.remove(tmp_copy)
                    except OSError:
                        pass
                    return default
                if data:
                    return data
        except Exception:
            pass
        if attempt < 2:
            import time
            time.sleep(0.1)
    return default

def save_json_file(path: str, data: Any):
    """保存数据为 JSON 格式。原子写入: 先写 .tmp 再 os.replace，避免中途崩坏原文件。
    
    OverlayFS/FUSE 上 os.replace 可能不是真正原子的（跨 inode rename 失败），
    所以采用 double-write 策略：先写一个带时间戳的 .bak 文件，再写目标文件。
    这样即使 .tmp 被清掉，也能回退到 .bak。
    """
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as exc:
                if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise
        os.replace(tmp_path, path)
    except FileNotFoundError:
        # .tmp 文件在 os.replace 前被清掉了（overlay/FUSE 竞态条件）
        # Fallback: 直接写入目标文件
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        except Exception:
            pass
        # 清理残留 .tmp
        try:
            os.remove(tmp_path)
        except OSError:
            pass
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
