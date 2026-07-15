import json
import os
from datetime import datetime, timedelta, timezone

from src.trading.manager import TradingBotManager


def test_fv_training_records_window_ref_and_prediction(tmp_path, monkeypatch):
    monkeypatch.setattr(TradingBotManager, "BTC_WINDOW_REFS_FILE", str(tmp_path / "btc_window_refs.json"))
    monkeypatch.setattr(TradingBotManager, "BTC_TICKS_FILE", str(tmp_path / "btc_ticks.jsonl"))
    monkeypatch.setattr(TradingBotManager, "FAIR_VALUE_PREDICTIONS_FILE", str(tmp_path / "fair_value_predictions.jsonl"))
    monkeypatch.setattr(TradingBotManager, "BTC_SNAPSHOT_FILE", str(tmp_path / "btc_snapshot.json"))

    manager = TradingBotManager()
    now = datetime.now(timezone.utc)
    start_ts = int((now - timedelta(seconds=2)).timestamp())
    slug = f"btc-updown-15m-{start_ts}"
    market = {
        "slug": slug,
        "end_date": (datetime.fromtimestamp(start_ts, tz=timezone.utc) + timedelta(minutes=15)).isoformat(),
        "outcomes": [
            {"label": "Up", "best_ask": 0.44, "best_bid": 0.43},
            {"label": "Down", "best_ask": 0.58, "best_bid": 0.57},
        ],
    }
    snapshot = {"price": 60010.0, "sigma_15m": 0.003, "source": "unit", "captured_at": now.isoformat()}

    manager._append_jsonl(TradingBotManager.BTC_TICKS_FILE, {"t": now.isoformat(), "price": snapshot["price"]})
    manager._record_fv_predictions([market], snapshot, now)

    assert os.path.exists(TradingBotManager.BTC_TICKS_FILE)
    assert os.path.exists(TradingBotManager.BTC_WINDOW_REFS_FILE)
    with open(TradingBotManager.FAIR_VALUE_PREDICTIONS_FILE, encoding="utf-8") as file:
        prediction = json.loads(file.readline())
    assert prediction["slug"] == slug
    assert abs(prediction["edge_up_bps"] - (prediction["fair_up"] - 0.44) * 10000) < 1.0
