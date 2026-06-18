from agent import risk_gate
from agent.state import PortfolioState, Position


def _fresh_state(cash=1000.0, peak=1000.0):
    s = PortfolioState(cash_usd=cash, initial_equity=cash, peak_equity=peak)
    s.day_start_equity = cash
    return s


def test_oversize_is_capped_not_blocked(cfg):
    s = _fresh_state()
    res = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.99, confidence=0.9,
        token_risk_score=10, state=s, equity=1000.0, cfg=cfg, now=10_000,
    )
    assert res.approved
    # capped at max_position_pct (0.35) * full budget (dd=0) = 350
    assert res.adjusted_size_usd <= 1000 * cfg["risk"]["max_position_pct"] + 1e-6


def test_kill_switch_blocks_opening(cfg):
    s = _fresh_state(cash=1000, peak=1000)
    equity = 750.0   # 25% peak-to-now drawdown == kill line
    res = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.2, confidence=0.9,
        token_risk_score=10, state=s, equity=equity, cfg=cfg, now=10_000,
    )
    assert not res.approved
    assert "drawdown_kill" in res.reason


def test_daily_pause_blocks_opening(cfg):
    # 10% down on the day but only 10% peak-to-now (under the 25% kill) -> pause
    s = _fresh_state(cash=1000, peak=1000)
    s.day_start_equity = 1000
    res = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.2, confidence=0.9,
        token_risk_score=10, state=s, equity=900.0, cfg=cfg, now=10_000,
    )
    assert not res.approved
    assert "daily_pause" in res.reason


def test_close_allowed_even_past_drawdown(cfg):
    s = _fresh_state(cash=1000, peak=1000)
    res = risk_gate.evaluate(
        token="CAKE", action="close", requested_size_pct=0.0, confidence=0.9,
        token_risk_score=10, state=s, equity=700.0, cfg=cfg, now=10_000,
    )
    assert res.approved   # de-risking always permitted


def test_low_confidence_blocks(cfg):
    s = _fresh_state()
    res = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.2, confidence=0.4,
        token_risk_score=10, state=s, equity=1000.0, cfg=cfg, now=10_000,
    )
    assert not res.approved
    assert "low_confidence" in res.reason


def test_high_token_risk_blocks(cfg):
    s = _fresh_state()
    res = risk_gate.evaluate(
        token="SCAM", action="buy", requested_size_pct=0.2, confidence=0.9,
        token_risk_score=90, state=s, equity=1000.0, cfg=cfg, now=10_000,
    )
    assert not res.approved
    assert "token_risk_score" in res.reason


def test_tournament_sizing_shrinks_near_dq(cfg):
    # Isolate the drawdown-budget multiplier from the daily-loss stop by
    # putting the day's start at the same level as current equity.
    s = _fresh_state(cash=1000, peak=1000)
    healthy = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.35, confidence=0.9,
        token_risk_score=10, state=s, equity=1000.0, cfg=cfg, now=10_000,
    )
    # 5% peak-to-now drawdown, but flat on the day -> budget = (0.20-0.05)/0.20 = 0.75
    s.day_start_equity = 950.0
    stressed = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.35, confidence=0.9,
        token_risk_score=10, state=s, equity=950.0, cfg=cfg, now=10_000,
    )
    assert stressed.adjusted_size_usd < healthy.adjusted_size_usd
    assert stressed.approved


def test_trade_rate_limit(cfg):
    s = _fresh_state()
    s.trades_today = cfg["risk"]["max_trades_per_day"]
    res = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.2, confidence=0.9,
        token_risk_score=10, state=s, equity=1000.0, cfg=cfg, now=10_000,
    )
    assert not res.approved
    assert "max_trades_per_day" in res.reason


def test_concentration_cap_shrinks(cfg):
    s = _fresh_state(cash=1000, peak=1000)
    # already holding 350 of CAKE; cap is 40% of 1000 = 400 -> only 50 more allowed
    s.positions["CAKE"] = Position(token="CAKE", qty=350.0, avg_price=1.0)
    res = risk_gate.evaluate(
        token="CAKE", action="buy", requested_size_pct=0.35, confidence=0.9,
        token_risk_score=10, state=s, equity=1000.0, cfg=cfg, now=10_000,
    )
    assert res.approved
    assert res.adjusted_size_usd <= 50 + 1e-6
