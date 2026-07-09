import os
from datetime import datetime, timezone, timedelta

from src.trading.manager import TradingBotManager


def test_lowbuy_fv_filter_disabled_by_default():
    assert TradingBotManager.LOWBUY_FV_FILTER_ENABLED is False


def test_fv_training_records_window_ref_and_prediction(tmp_path, monkeypatch):
    monkeypatch.setattr(TradingBotManager, "BTC_WINDOW_REFS_FILE", str(tmp_path / "btc_window_refs.json"))
    monkeypatch.setattr(TradingBotManager, "BTC_TICKS_FILE", str(tmp_path / "btc_ticks.jsonl"))
    monkeypatch.setattr(TradingBotManager, "FAIR_VALUE_PREDICTIONS_FILE", str(tmp_path / "fair_value_predictions.jsonl"))
    monkeypatch.setattr(TradingBotManager, "BTC_SNAPSHOT_FILE", str(tmp_path / "btc_snapshot.json"))

    mgr = TradingBotManager()
    now = datetime.now(timezone.utc)
    start_ts = int((now - timedelta(minutes=2)).timestamp())
    end = datetime.fromtimestamp(start_ts, tz=timezone.utc) + timedelta(minutes=15)
    slug = f"btc-updown-15m-{start_ts}"
    market = {
        "slug": slug,
        "end_date": end.isoformat(),
        "outcomes": [
            {"label": "Up", "best_ask": 0.44, "best_bid": 0.43},
            {"label": "Down", "best_ask": 0.58, "best_bid": 0.57},
        ],
    }
    snap = {"price": 60010.0, "sigma_15m": 0.003, "source": "unit", "captured_at": now.isoformat()}

    mgr._record_btc_tick(snap, now.timestamp())
    mgr._record_fv_predictions([market], snap, now)

    assert os.path.exists(TradingBotManager.BTC_TICKS_FILE)
    assert os.path.exists(TradingBotManager.BTC_WINDOW_REFS_FILE)
    assert os.path.exists(TradingBotManager.FAIR_VALUE_PREDICTIONS_FILE)
    pred = open(TradingBotManager.FAIR_VALUE_PREDICTIONS_FILE, encoding="utf-8").read()
    assert slug in pred
    assert '"lowbuy_filter_enabled":false' in pred
