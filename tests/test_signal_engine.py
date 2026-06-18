from agent.signal_engine import (
    Regime,
    detect_regime,
    rsi_component,
    score_token,
    score_universe,
)


def test_rsi_component_directionality():
    # oversold -> long bias (+), overbought -> short bias (-)
    assert rsi_component(20, 30, 70) == 1.0
    assert rsi_component(80, 30, 70) == -1.0
    assert abs(rsi_component(50, 30, 70)) < 1e-9


def test_regime_from_benchmark_trend(cfg):
    up = {"BTC": {"ema_trend": "up", "fear_greed_index": 60}}
    down = {"BTC": {"ema_trend": "down", "fear_greed_index": 40}}
    flat = {"BTC": {"ema_trend": "flat", "fear_greed_index": 50}}
    assert detect_regime(up, cfg) is Regime.TREND_UP
    assert detect_regime(down, cfg) is Regime.TREND_DOWN
    assert detect_regime(flat, cfg) is Regime.CHOP


def test_strong_long_setup_is_actionable(cfg):
    # RSI oversold + bullish cross + ema up in an uptrend => strong long
    data = {
        "rsi": 25, "macd_state": "bullish_cross", "ema_trend": "up",
        "fear_greed_index": 35, "news_sentiment": 0.3,
    }
    sig = score_token("CAKE", data, Regime.TREND_UP, cfg)
    assert sig.score > 0.5
    assert sig.actionable is True


def test_strong_short_setup_is_negative(cfg):
    data = {
        "rsi": 78, "macd_state": "bearish_cross", "ema_trend": "down",
        "fear_greed_index": 80, "news_sentiment": -0.4,
    }
    sig = score_token("ETH", data, Regime.TREND_DOWN, cfg)
    assert sig.score < -0.5
    assert sig.actionable is True   # negative score is actionable (short via perps)


def test_chop_dampens_and_blocks_action(cfg):
    data = {
        "rsi": 25, "macd_state": "bullish_cross", "ema_trend": "up",
        "fear_greed_index": 35, "news_sentiment": 0.3,
    }
    strong = score_token("CAKE", data, Regime.TREND_UP, cfg).score
    chopped = score_token("CAKE", data, Regime.CHOP, cfg)
    assert abs(chopped.score) < abs(strong)       # dampened
    assert chopped.actionable is False            # never act in chop


def test_score_universe_skips_quote_asset(cfg):
    snap = {
        "BTC": {"ema_trend": "up", "fear_greed_index": 60, "rsi": 55,
                "macd_state": "bullish", "news_sentiment": 0.1},
        "USDT": {"rsi": 50},
    }
    out = score_universe(snap, cfg)
    assert "USDT" not in out
    assert "BTC" in out
