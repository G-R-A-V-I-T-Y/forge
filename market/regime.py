"""Market regime classifier. Derives regime from BTC 30d OHLCV."""
import statistics


def classify_regime(btc_ohlcv_1d: list[list]) -> str:
    """Classify market regime from daily BTC candles.

    Parameters
    ----------
    btc_ohlcv_1d:
        List of daily candles [ts, open, high, low, close, volume] for ~30 days.

    Returns
    -------
    str:
        One of: trending_bull, trending_bear, range_low_vol, range_high_vol, crisis
    """
    if len(btc_ohlcv_1d) < 10:
        return "range_low_vol"

    closes = [c[4] for c in btc_ohlcv_1d]
    highs = [c[2] for c in btc_ohlcv_1d]
    lows = [c[3] for c in btc_ohlcv_1d]

    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    avg_return = statistics.mean(returns) if returns else 0.0
    annualized_return = avg_return * 252

    atr_values = [h - l for h, l in zip(highs, lows)]
    avg_atr = statistics.mean(atr_values) if atr_values else 0.0
    avg_price = statistics.mean(closes) if closes else 1.0
    atr_percent = avg_atr / avg_price

    if avg_atr / avg_price > 0.08 and annualized_return < -0.5:
        return "crisis"

    if annualized_return > 0.2:
        return "trending_bull" if atr_percent > 0.03 else "trending_bull"
    elif annualized_return < -0.15:
        return "trending_bear"

    if atr_percent < 0.025:
        return "range_low_vol"
    else:
        return "range_high_vol"
