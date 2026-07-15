# Polymarket FV Edge Bot

BTC 15-minute UP/DOWN market trader using one strategy only: FV Edge.

The bot estimates the fair UP/DOWN probability from:

- current BTC spot price;
- the window-start BTC reference price;
- estimated 15-minute volatility;
- time remaining until settlement.

It buys only when one side's fair probability exceeds that side's executable ask by the configured edge threshold. Accepted positions are held to expiry.

## Start

Requires Python 3.10 or newer so a current `py-clob-client` build is available.

```bash
chmod +x run.sh
./run.sh
```

The dashboard is available at `http://localhost:8889` by default.

## Configuration

On first launch, `run.sh` copies `.env.example` to `.env`. Important settings:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `TRADING_MODE` | `paper_live` | `paper_live` or `live` |
| `DRY_RUN` | `true` | Must be explicitly set to `false` for real orders |
| `FV_EDGE_POSITION_USD` | `2.0` | Stake per accepted signal |
| `FV_EDGE_THRESHOLD_BPS` | `300` | Minimum positive edge |
| `FV_EDGE_MAX_MTE` | `2.0` | Latest entry window in minutes |
| `FV_EDGE_MIN_PRICE` | `0.10` | Minimum executable ask |
| `FV_EDGE_MAX_PRICE` | `0.85` | Maximum executable ask |
| `FV_EDGE_MAX_BTC_AGE_SECONDS` | `10` | Reject stale BTC prices |
| `FV_EDGE_MAX_REF_DELAY_SECONDS` | `10` | Reject late window references |

Live mode requires valid Polymarket credentials. The bot refuses mode changes while positions or active orders exist.

## Layout

```text
bot.py                    entry point
src/trading/fv_edge.py    FV Edge signal engine
src/trading/manager.py    FV-only lifecycle and risk controls
src/trading/executor.py   paper/live order execution
src/api/                  market, BTC, fair-value, and WebSocket clients
src/server/               local dashboard API
tests/                    FV and infrastructure tests
```

This software is for research and testing. Prediction-market trading can lose the full position value.
