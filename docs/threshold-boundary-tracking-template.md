# Threshold Boundary Tracking Template

## Boundary Definition

- Strategy boundary UTC: `2026-07-22T01:17:46Z`
- Strategy change: `Up>=1200bps, Down>=800bps`
- Sizing rule retained: `edge>=800bps and ask>=0.70 -> $3`, otherwise `$2`
- Pre-change archive: `history/threshold-boundary-20260722-011745/`
- Pre-change snapshot:
  - cash balance: `$129.3806`
  - realized PnL: `+$29.3806`
  - closed trades: `148`
  - trade events: `296`
  - positions: `0`
  - paper_trade_state sha256: `3b4ad2ac58a28fb7da73d09347445b47b850bb2dfe4aa653005b58ffc2968bdf`

## Reporting Window

Always split analysis into:

1. `Boundary-before` — all trades with `closed_at < 2026-07-22T01:17:46Z`
2. `Boundary-after` — all trades with `closed_at >= 2026-07-22T01:17:46Z`

Do not mix them when evaluating the new thresholds.

## Minimum Metrics To Report

### Core performance
- closed trades
- wins / losses
- win rate
- realized PnL
- average PnL per trade
- current cash balance
- open positions

### Direction split
- Up trades / Up PnL
- Down trades / Down PnL

### Risk / sizing split
- count of `$3` trades
- PnL from `$3` trades
- count of `$2` trades
- PnL from `$2` trades

### Edge bucket split
- `800-1199bps`
- `1200-1499bps`
- `1500-1999bps`
- `>=2000bps`

For each bucket report:
- count
- PnL
- win rate

## Decision Questions

When reviewing `Boundary-after`, always answer these explicitly:

1. Is total PnL improving versus the pre-boundary baseline pace?
2. Is Up-side filtering reducing low-quality Up losses?
3. Is Down-side profit contribution still stable?
4. Are `$3` trades still accretive, or are they amplifying drawdowns?
5. Is the new policy improving profit quality, not just shrinking trade count?

## Quick Status Format

Use this concise format in future check-ins:

```text
Boundary-after since 2026-07-22T01:17:46Z
- Trades: X (W/L = A/B, win rate C%)
- PnL: $Y, avg/trade $Z
- Up: N trades, $P
- Down: M trades, $Q
- $3 trades: K trades, $R
- Best edge bucket:
- Worst edge bucket:
- Verdict: better / flat / worse than pre-boundary pace
```

## Interpretation Guide

- If `Boundary-after` has fewer trades but higher avg/trade and stronger total PnL pace, the threshold change is working.
- If Up losses compress while Down PnL stays intact, the directional split is working.
- If `$3` trades stop contributing positive expectancy, revisit the high-confidence sizing rule before touching thresholds again.
- If `800-1199bps` Down trades remain strong but `1200+` Up trades weaken, do not immediately raise the Down threshold with the Up threshold.

## Operational Reminder

The strategy boundary is a research marker, not a restore marker.
Runtime restore still uses GitHub `runtime-backup`, while performance evaluation uses this UTC split.
