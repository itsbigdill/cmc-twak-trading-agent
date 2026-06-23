"""Guards against config drift that would silently void trades."""

import os


def _eligible() -> set:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "config", "eligible_tokens.txt")
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
    return out


def test_trade_targets_are_eligible(cfg):
    # Every tradeable token must be on the official 149-list, else its trades
    # don't count (this is the BTCB/BNB trap).
    eligible = _eligible()
    for token in cfg["twak"]["token_contracts"]:
        assert token in eligible, f"{token} is NOT in the eligible 149-token list"


def test_quote_asset_is_eligible(cfg):
    # We must hold a non-zero in-scope balance; the cash leg must be in-scope.
    assert cfg["quote_asset"] in _eligible()


def test_benchmark_is_signal_only(cfg):
    # BTC/BNB drive regime but are NOT tradeable (not on the list).
    assert cfg["regime"]["benchmark"] not in cfg["twak"]["token_contracts"]


def test_execution_guards_are_sane(cfg):
    ex = cfg["execution"]
    assert 0 < ex["max_price_impact_pct"] <= 2
    assert 0 < ex["balance_buffer_fraction"] < 0.001
    assert ex["min_gas_bnb"] > 0
    assert cfg["twak"]["quote_contract"].lower() == \
        "0x55d398326f99059ff775485246999027b3197955"
