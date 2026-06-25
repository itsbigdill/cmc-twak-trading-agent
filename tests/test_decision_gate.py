from agent import decision
from agent.decision import LLMDecider, tradeable_buy_tokens


class Base:
    def decide(self, snapshot, signals, portfolio, risk_limits):
        return [
            {"token": "ETH", "action": "buy", "size_pct": 0.3,
             "confidence": 0.8, "rationale": "deterministic"},
            {"token": "CAKE", "action": "close", "size_pct": 0.0,
             "confidence": 0.7, "rationale": "exit"},
        ]


def test_llm_cannot_invent_trade_and_cannot_veto_exit(monkeypatch, cfg):
    cfg = {**cfg, "llm": {**cfg["llm"], "second_gate": True}}
    monkeypatch.setattr(decision.llm, "complete", lambda *a, **k: (
        '{"decisions":['
        '{"token":"APE","action":"buy","size_pct":1,"confidence":1,"rationale":"invent"},'
        '{"token":"ETH","action":"hold","size_pct":0,"confidence":1,"rationale":"veto"}'
        ']}'
    ))
    out = LLMDecider(cfg, fallback=Base()).decide({}, {},
                                                   {"positions": {}}, {})
    assert out == [{"token": "CAKE", "action": "close", "size_pct": 0.0,
                    "confidence": 0.7, "rationale": "exit"}]


def test_llm_review_can_only_reduce_buy_size(monkeypatch, cfg):
    cfg = {**cfg, "llm": {**cfg["llm"], "second_gate": True}}

    class SizeBase:
        def decide(self, snapshot, signals, portfolio, risk_limits):
            return [
                {"token": "ETH", "action": "buy", "size_pct": 0.4,
                 "confidence": 0.8, "rationale": "deterministic"},
                {"token": "CAKE", "action": "buy", "size_pct": 0.2,
                 "confidence": 0.7, "rationale": "deterministic"},
            ]

    monkeypatch.setattr(decision.llm, "complete", lambda *a, **k: (
        '{"decisions":['
        '{"token":"ETH","action":"buy","size_pct":0.25,"confidence":0.6,"rationale":"late chase risk"},'
        '{"token":"CAKE","action":"buy","size_pct":0.9,"confidence":1,"rationale":"try increase"}'
        ']}'
    ))
    out = LLMDecider(cfg, fallback=SizeBase()).decide({}, {},
                                                       {"positions": {}}, {})
    eth = next(x for x in out if x["token"] == "ETH")
    cake = next(x for x in out if x["token"] == "CAKE")
    assert eth["size_pct"] == 0.25
    assert eth["confidence"] == 0.8
    assert cake["size_pct"] == 0.2
    assert cake["confidence"] == 0.7


def test_llm_cash_preservation_veto_can_be_overridden_for_high_conviction(monkeypatch, cfg):
    cfg = {**cfg, "llm": {**cfg["llm"], "second_gate": True,
                          "cash_veto_override_enabled": True,
                          "cash_veto_override_size_pct": 0.4}}

    class RecoveryBase:
        last_debug = {
            "top_ranked": [{
                "token": "LAB",
                "score": 0.41,
                "cmc_score": 0.88,
                "x402_token_score": 0.28,
                "round_trip_loss_pct": 1.9,
                "token_risk_score": 10,
                "return_6h": 0.01,
            }]
        }

        def decide(self, snapshot, signals, portfolio, risk_limits):
            return [{"token": "LAB", "action": "buy", "size_pct": 0.55,
                     "confidence": 0.75, "rationale": "deterministic"}]

    monkeypatch.setattr(decision.llm, "complete", lambda *a, **k: (
        '{"decisions":[{"token":"LAB","action":"hold","size_pct":0,'
        '"confidence":0,"rationale":"trend_down drawdown preserve cash"}]}'
    ))
    out = LLMDecider(cfg, fallback=RecoveryBase()).decide(
        {}, {}, {"positions": {}}, {"leaderboard_rank": 47,
                                    "leaderboard_drawdown_pct": 17.1})
    assert out == [{"token": "LAB", "action": "buy", "size_pct": 0.4,
                    "confidence": 0.75,
                    "rationale": "deterministic; AI cash-veto overridden by high-conviction recovery guardrail"}]


def test_llm_cash_preservation_veto_can_be_overridden_for_validated_scout(monkeypatch, cfg):
    cfg = {**cfg,
           "decision": {**cfg["decision"],
                        "entry_filter": {**cfg["decision"]["entry_filter"],
                                         "scout_exposure_pct": 0.18,
                                         "scout_min_score": 0.31,
                                         "scout_min_cmc": 0.25,
                                         "scout_min_x402": 0.25,
                                         "scout_min_return_6h_downtrend": -0.02,
                                         "scout_max_round_trip_loss_pct": 1.8,
                                         "scout_max_token_risk_score": 30}},
           "llm": {**cfg["llm"], "second_gate": True,
                   "cash_veto_override_enabled": True}}

    class ScoutBase:
        last_debug = {
            "top_ranked": [{
                "token": "XPL",
                "score": 0.342,
                "cmc_score": 0.42,
                "x402_token_score": 0.37,
                "round_trip_loss_pct": 1.36,
                "token_risk_score": 10,
                "return_6h": -0.011,
            }]
        }

        def decide(self, snapshot, signals, portfolio, risk_limits):
            return [{"token": "XPL", "action": "buy", "size_pct": 0.18,
                     "confidence": 0.72,
                     "rationale": "rotate in: validated_scout, gross=0.18"}]

    monkeypatch.setattr(decision.llm, "complete", lambda *a, **k: (
        '{"decisions":[{"token":"XPL","action":"hold","size_pct":0,'
        '"confidence":0,"rationale":"trend_down preserve cash"}]}'
    ))
    out = LLMDecider(cfg, fallback=ScoutBase()).decide(
        {}, {}, {"positions": {}}, {"leaderboard_rank": 33,
                                    "leaderboard_drawdown_pct": 17.3})
    assert out == [{"token": "XPL", "action": "buy", "size_pct": 0.18,
                    "confidence": 0.72,
                    "rationale": "rotate in: validated_scout, gross=0.18; AI cash-veto overridden by scout recovery guardrail"}]


def test_llm_cash_preservation_veto_still_blocks_scout_with_hard_risk(monkeypatch, cfg):
    cfg = {**cfg,
           "decision": {**cfg["decision"],
                        "entry_filter": {**cfg["decision"]["entry_filter"],
                                         "scout_max_round_trip_loss_pct": 1.8}},
           "llm": {**cfg["llm"], "second_gate": True,
                   "cash_veto_override_enabled": True}}

    class ScoutBase:
        last_debug = {
            "top_ranked": [{
                "token": "XPL",
                "score": 0.36,
                "cmc_score": 0.60,
                "x402_token_score": 0.37,
                "round_trip_loss_pct": 1.36,
                "token_risk_score": 10,
                "return_6h": -0.002,
            }]
        }

        def decide(self, snapshot, signals, portfolio, risk_limits):
            return [{"token": "XPL", "action": "buy", "size_pct": 0.18,
                     "confidence": 0.72,
                     "rationale": "rotate in: validated_scout, gross=0.18"}]

    monkeypatch.setattr(decision.llm, "complete", lambda *a, **k: (
        '{"decisions":[{"token":"XPL","action":"hold","size_pct":0,'
        '"confidence":0,"rationale":"liquidity route risk"}]}'
    ))
    out = LLMDecider(cfg, fallback=ScoutBase()).decide(
        {}, {}, {"positions": {}}, {"leaderboard_rank": 33,
                                    "leaderboard_drawdown_pct": 17.3})
    assert out == []
    assert LLMDecider(cfg, fallback=ScoutBase())


def test_llm_hard_veto_is_candidate_specific_for_cash_override(cfg):
    cfg = {**cfg, "llm": {**cfg["llm"], "second_gate": True,
                          "cash_veto_override_enabled": True,
                          "cash_veto_override_min_score": 0.30,
                          "cash_veto_override_min_cmc": 0.20,
                          "cash_veto_override_min_x402": 0.20}}
    decider = LLMDecider(cfg)
    base_debug = {"top_ranked": [
        {"token": "XPL", "score": 0.40, "cmc_score": 0.30,
         "x402_token_score": 0.30, "round_trip_loss_pct": 1.2,
         "token_risk_score": 10, "return_6h": 0.01},
        {"token": "LAB", "score": 0.34, "cmc_score": 0.35,
         "x402_token_score": 0.28, "round_trip_loss_pct": 1.2,
         "token_risk_score": 10, "return_6h": 0.01},
    ]}
    vetoed = [
        {"token": "XPL", "rationale": "holder concentration risk"},
        {"token": "LAB", "rationale": "trend_down preserve cash"},
    ]

    blocked = decider._cash_veto_override(
        {"token": "XPL", "action": "buy", "size_pct": 0.3,
         "confidence": 0.7, "rationale": "deterministic"},
        base_debug, {"leaderboard_rank": 31, "leaderboard_drawdown_pct": 17.0}, vetoed)
    allowed = decider._cash_veto_override(
        {"token": "LAB", "action": "buy", "size_pct": 0.3,
         "confidence": 0.7, "rationale": "deterministic"},
        base_debug, {"leaderboard_rank": 31, "leaderboard_drawdown_pct": 17.0}, vetoed)

    assert blocked is None
    assert allowed["token"] == "LAB"
    assert "cash-veto overridden" in allowed["rationale"]


def test_deny_buy_lifts_only_after_executable_validation(cfg):
    cfg = {**cfg, "twak": {**cfg["twak"],
                           "token_contracts": {"ZETA": "0x1", "CAKE": "0x2"},
                           "deny_buy": ["ZETA"]}}
    assert "ZETA" not in tradeable_buy_tokens(cfg)

    cfg = {**cfg, "universe_runtime": {"ZETA": {
        "round_trip_loss_pct": 1.2,
        "risk_level": "low",
        "history_bars": cfg["universe"]["min_history_bars"],
    }}}
    assert "ZETA" in tradeable_buy_tokens(cfg)

    cfg["universe_runtime"]["ZETA"]["round_trip_loss_pct"] = 9.0
    assert "ZETA" not in tradeable_buy_tokens(cfg)


def test_sell_only_token_stays_out_of_buy_universe_even_when_validated(cfg):
    cfg = {**cfg, "twak": {**cfg["twak"],
                           "token_contracts": {"TAC": "0x1", "CAKE": "0x2"},
                           "sell_only_tokens": ["TAC"]},
           "universe_runtime": {"TAC": {
               "round_trip_loss_pct": 1.2,
               "risk_level": "low",
               "history_bars": cfg["universe"]["min_history_bars"],
           }}}
    assert "TAC" not in tradeable_buy_tokens(cfg)
    assert "CAKE" in tradeable_buy_tokens(cfg)
