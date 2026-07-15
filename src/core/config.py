import os
import json
import logging
import time
import threading
from dotenv import dotenv_values, load_dotenv
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, Optional

# ============================================================
# 路径常量（启动时定一次，运行中不会变）
# ============================================================
BASE_DIR = Path(__file__).parent.parent.parent.absolute()
DATA_DIR = os.path.join(BASE_DIR, "data")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
STATUS_FILE = os.path.join(DATA_DIR, "bot_status.json")
PAPER_STATE_FILE = os.path.join(DATA_DIR, "paper_trade_state.json")
REPORT_FILE = os.path.join(DOCS_DIR, "paper_trade_report.md")
CONTROL_FILE = os.path.join(DATA_DIR, "trading_control.json")
ENV_FILE = os.path.join(BASE_DIR, ".env")

# 时区常量
NY_TZ = ZoneInfo("America/New_York")

# 加载环境变量（仅在缺失时填充，override=True 保证 .env 覆盖已有 env）
load_dotenv(ENV_FILE, override=True)


class _ConfigCache:
    """轻量级 TTL 缓存，避免每次 Config.get 都重新读盘。

    - runtime cache: 来自 trading_control.json（Web 面板可热更）
    - env cache: 来自 .env（Web 面板写 .env 后热更）
    - 2 秒 TTL: 既能反映热更，又不会高频 IO
    """

    TTL_SECONDS = 2.0

    def __init__(self):
        self._runtime_lock = threading.Lock()
        self._env_lock = threading.Lock()
        self._runtime_cache: Optional[Dict[str, Any]] = None
        self._runtime_at: float = 0.0
        self._env_cache: Optional[Dict[str, Any]] = None
        self._env_at: float = 0.0

    def runtime(self) -> Dict[str, Any]:
        with self._runtime_lock:
            now = time.time()
            if self._runtime_cache is not None and (now - self._runtime_at) < self.TTL_SECONDS:
                return self._runtime_cache
            self._runtime_cache = self._read_runtime()
            self._runtime_at = now
            return self._runtime_cache

    def env(self) -> Dict[str, Any]:
        with self._env_lock:
            now = time.time()
            if self._env_cache is not None and (now - self._env_at) < self.TTL_SECONDS:
                return self._env_cache
            self._env_cache = self._read_env()
            self._env_at = now
            return self._env_cache

    def invalidate(self):
        """外部修改了 .env / trading_control.json 后可主动失效缓存。"""
        with self._runtime_lock:
            self._runtime_cache = None
            self._runtime_at = 0.0
        with self._env_lock:
            self._env_cache = None
            self._env_at = 0.0

    @staticmethod
    def _read_runtime() -> Dict[str, Any]:
        if os.path.exists(CONTROL_FILE):
            try:
                with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _read_env() -> Dict[str, Any]:
        try:
            return {k: v for k, v in dotenv_values(ENV_FILE).items() if v not in (None, "")}
        except Exception:
            return {}


_CACHE = _ConfigCache()

# 配置专用日志器，用于输出大小写不匹配 / 使用默认值的警告
logger = logging.getLogger("config")


class Config:
    """统一配置入口。

    优先级: trading_control.json (Web 端运行时配置) > .env > 进程 env > 默认值
    所有数值/模式/凭证读取都走这里，每次最多触发一次磁盘 IO（2s TTL 缓存）。
    """

    @staticmethod
    def invalidate():
        """外部写盘后调用，强制下次重新读。"""
        _CACHE.invalidate()

    @staticmethod
    def get(key: str, default: Any = None) -> Any:
        # 1) runtime (trading_control.json, 前端写入)
        runtime = _CACHE.runtime()
        if key in runtime:
            return runtime[key]
        # 大小写容错: 前端可能写小写, 后端读大写 (或反之)
        for alt_key in (key.upper(), key.lower()):
            if alt_key != key and alt_key in runtime:
                logger.warning(
                    "Config 大小写不匹配: 代码读 '%s', trading_control.json 写 '%s', 值=%s",
                    key, alt_key, runtime[alt_key],
                )
                return runtime[alt_key]

        # 2) .env 文件
        env_config = _CACHE.env()
        if key in env_config and env_config[key] not in (None, ""):
            return env_config[key]
        for alt_key in (key.upper(), key.lower()):
            if alt_key != key and alt_key in env_config and env_config[alt_key] not in (None, ""):
                logger.warning(
                    "Config 大小写不匹配: 代码读 '%s', .env 写 '%s', 值=%s",
                    key, alt_key, env_config[alt_key],
                )
                return env_config[alt_key]

        # 3) 进程环境变量
        val = os.getenv(key)
        if val is not None:
            return val
        for alt_key in (key.upper(), key.lower()):
            if alt_key != key:
                val = os.getenv(alt_key)
                if val is not None:
                    logger.warning(
                        "Config 大小写不匹配: 代码读 '%s', 环境变量写 '%s', 值=%s",
                        key, alt_key, val,
                    )
                    return val

        logger.debug("Config 使用默认值: %s = %s", key, default)
        return default

    @staticmethod
    def get_bool(key: str, default: str = "true") -> bool:
        val = str(Config.get(key, default)).lower()
        return val in ("true", "1", "yes", "on")

    @staticmethod
    def get_float(key: str, default: str = "0") -> float:
        try:
            return float(Config.get(key, default))
        except (ValueError, TypeError):
            return float(default)

    @staticmethod
    def get_int(key: str, default: str = "0") -> int:
        try:
            return int(Config.get(key, default))
        except (ValueError, TypeError):
            return int(default)


# 便捷模块级函数（方便 from src.core.config import invalidate; invalidate()）
invalidate = Config.invalidate


# ============================================================
# 启动时定一次的常量（API 凭证 / 钱包地址 / 签名类型）
# 改完这些需要重启进程才生效
# ============================================================
POLYMARKET_WALLET_ADDRESS = Config.get("POLYMARKET_WALLET_ADDRESS", "")
POLYMARKET_FUNDER_ADDRESS = Config.get("POLYMARKET_FUNDER_ADDRESS", POLYMARKET_WALLET_ADDRESS)
POLYMARKET_SIGNATURE_TYPE = Config.get_int("POLYMARKET_SIGNATURE_TYPE", "1")

POLYMARKET_API_KEY = Config.get("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = Config.get("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = Config.get("POLYMARKET_API_PASSPHRASE", "")
POLYMARKET_PRIVATE_KEY = Config.get("POLYMARKET_PRIVATE_KEY", "")
