# Market Residuals and Weather Proxy v2

Generated: `2026-08-13T06:46:28.655376Z`

## 1. Market midpoint residual analysis

This uses the same `1584` horizon-1 rows scored out of sample by the existing walk-forward protocol. The market midpoint is treated as the forecaster. Signed error is `market_mid - result_yes`, so positive values mean YES was overpredicted. For between strikes, distance is to the nearer boundary.

Pooled midpoint Brier is **0.0986** and pooled signed error is **+0.0089**. A cell is flagged only when `N >= 50` and its point Brier is at least `0.02` worse than the pre-existing pooled reference `0.0986`.

### Station

| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |
|---|---:|---:|---:|---:|---:|---|
| KAUS | 209 | 0.1840 | 0.1770 | 0.0070 | 0.0835 |  |
| KDEN | 210 | 0.1645 | 0.1524 | 0.0121 | 0.1024 |  |
| KLAX | 236 | 0.1660 | 0.1568 | 0.0092 | 0.0876 |  |
| KMDW | 229 | 0.1743 | 0.1747 | -0.0003 | 0.1116 |  |
| KMIA | 239 | 0.1802 | 0.1715 | 0.0087 | 0.0962 |  |
| KNYC | 233 | 0.1711 | 0.1588 | 0.0123 | 0.0941 |  |
| KPHL | 228 | 0.1756 | 0.1623 | 0.0133 | 0.1142 |  |

### Month

| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |
|---|---:|---:|---:|---:|---:|---|
| 2026-06 | 102 | 0.1715 | 0.1765 | -0.0050 | 0.0853 |  |
| 2026-07 | 1080 | 0.1717 | 0.1648 | 0.0069 | 0.1011 |  |
| 2026-08 | 402 | 0.1793 | 0.1617 | 0.0176 | 0.0952 |  |

### Strike distance

| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |
|---|---:|---:|---:|---:|---:|---|
| 10F+ | 16 | 0.0728 | 0.0625 | 0.0103 | 0.0264 |  |
| [0,1)F | 380 | 0.2761 | 0.2553 | 0.0208 | 0.1533 | notable |
| [1,3)F | 517 | 0.1958 | 0.2224 | -0.0267 | 0.1188 | notable |
| [3,5)F | 396 | 0.1264 | 0.0833 | 0.0430 | 0.0639 |  |
| [5,10)F | 275 | 0.0645 | 0.0545 | 0.0099 | 0.0390 |  |

### Price bucket

| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |
|---|---:|---:|---:|---:|---:|---|
| 40-60c | 205 | 0.4824 | 0.5024 | -0.0200 | 0.2480 | notable |
| favorite 60c+ | 35 | 0.6697 | 0.7714 | -0.1017 | 0.1905 |  |
| longshot 0-10c | 803 | 0.0290 | 0.0125 | 0.0165 | 0.0124 |  |
| mid 10-40c | 541 | 0.2393 | 0.2237 | 0.0156 | 0.1639 | notable |

### Price age

| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |
|---|---:|---:|---:|---:|---:|---|
| (30,60]m | 13 | 0.0108 | 0.0000 | 0.0108 | 0.0002 |  |
| (60,120]m | 5 | 0.0110 | 0.0000 | 0.0110 | 0.0002 |  |
| 0m | 1566 | 0.1755 | 0.1667 | 0.0089 | 0.0997 |  |

### Day of week

| Cell | N | Mean midpoint | Observed YES | Signed error | Midpoint Brier | Flag |
|---|---:|---:|---:|---:|---:|---|
| Friday | 214 | 0.1707 | 0.1682 | 0.0025 | 0.1040 |  |
| Monday | 248 | 0.1772 | 0.1653 | 0.0118 | 0.0906 |  |
| Saturday | 218 | 0.1701 | 0.1651 | 0.0049 | 0.1132 |  |
| Sunday | 239 | 0.1738 | 0.1464 | 0.0274 | 0.0863 |  |
| Thursday | 215 | 0.1697 | 0.1674 | 0.0023 | 0.0988 |  |
| Tuesday | 246 | 0.1831 | 0.1829 | 0.0002 | 0.1100 |  |
| Wednesday | 204 | 0.1688 | 0.1569 | 0.0119 | 0.0874 |  |

### Highlighted cells and cluster uncertainty

The intervals below use `1000` paired resamples of `(station_id, target_date)` clusters and estimate **cell Brier minus pooled Brier**. They account for shared outcomes across strikes within a station-day.

| Dimension | Cell | N | Brier | Excess vs pooled | 95% cluster CI |
|---|---|---:|---:|---:|---:|
| Price bucket | 40-60c | 205 | 0.2480 | +0.1494 | (0.1402, 0.1592) |
| Price bucket | mid 10-40c | 541 | 0.1639 | +0.0653 | (0.0545, 0.0763) |
| Strike distance | [0,1)F | 380 | 0.1533 | +0.0547 | (0.0397, 0.0710) |
| Strike distance | [1,3)F | 517 | 0.1188 | +0.0202 | (0.0080, 0.0328) |

Raw Brier scores are mechanically higher for contracts near 50c and strikes near the forecast because those outcomes are intrinsically less certain. Thus, the middle-price and near-strike flags locate forecast difficulty; they do not by themselves diagnose market miscalibration or an exploitable residual.

### Favorite-longshot diagnostic

| Price bucket | N | Mean midpoint | Observed YES | Gap (midpoint - observed) |
|---|---:|---:|---:|---:|
| longshot 0-10c | 803 | 0.0290 | 0.0125 | +0.0165 |
| mid 10-40c | 541 | 0.2393 | 0.2237 | +0.0156 |
| 40-60c | 205 | 0.4824 | 0.5024 | -0.0200 |
| favorite 60c+ | 35 | 0.6697 | 0.7714 | -0.1017 |

The direction is consistent with the classic favorite-longshot pattern: longshot YES contracts resolved less often than their mean price, while 60c+ favorites resolved more often. The favorite cell has only 35 observations, however, and this was not a preregistered test.

This is exploratory hypothesis generation, not evidence of a tradeable edge. The analysis examines dozens of overlapping cells, so some extreme point estimates are expected by chance. The CIs are not adjusted for multiple comparisons, and any pattern needs preregistration and confirmation on new data.

## 2. Leakage-safe proxy iteration

All variants use the identical expanding folds and 1,584-row OOS cohort. The station-bias model fits a shared sigma plus one station parameter defined as `E[forecast high - actual high]` by binary likelihood on each training fold. It does not reconstruct actual temperatures from labels. The Platt layer fits `sigmoid(a * logit(p) + b)` on training-fold predictions only, then applies it to that fold's held-out rows. Skill is midpoint Brier minus model Brier; intervals use `1000` station-day cluster resamples. P&L uses the existing one-contract taker policy at a strict `0.12` edge threshold.

| Model | N | Model Brier | Midpoint Brier | Skill (95% cluster CI) | Trades | Net P&L |
|---|---:|---:|---:|---:|---:|---:|
| Original zero-bias Gaussian | 1584 | 0.1492 | 0.0986 | -0.0506 (-0.0586, -0.0426) | 715 | $-18.47 |
| Station-bias Gaussian | 1584 | 0.1443 | 0.0986 | -0.0457 (-0.0537, -0.0376) | 671 | $-18.49 |
| Station-bias + Platt | 1584 | 0.1326 | 0.0986 | -0.0341 (-0.0407, -0.0277) | 735 | $-26.55 |

The best tested proxy is **Station-bias + Platt** and it still trails the midpoint on Brier skill (-0.0341). These are proxy-model results, not a verdict on the production ensemble route.

## Limitations

- The same rolling fixed-lead GFS and hourly-maximum limitations documented in the original backtest apply.
- Platt calibration is fitted on in-fold training predictions from the already fitted Gaussian parameters; held-out evaluation remains OOS, but a nested calibration fit would be more conservative.
- Sparse stale-price cells and overlapping slices are especially unstable.
- Transaction costs and executable-side pricing are included, but this remains a historical paper policy.
