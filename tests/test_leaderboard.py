import json

from agent import leaderboard


class _Response:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_current_status_exposes_authoritative_metrics(cfg, monkeypatch):
    address = cfg["twak"]["agent_address"].lower()
    data = {
        "generated_ts": 1234,
        "rows": [
            {"agent": "0xother", "traded": True, "value": 30, "ret_pct": 2,
             "dd_pct": 1, "trades": 2, "holds": [["USDT", 30]]},
            {"agent": address, "traded": True, "value": 19.36, "ret_pct": -8.51,
             "dd_pct": 21.36, "trades": 29, "holds": [["USDT", 19.36]]},
        ],
    }
    html = f"<script>const D={json.dumps(data)}, R=D.rows||[];</script>"
    monkeypatch.setattr(leaderboard.urllib.request, "urlopen",
                        lambda *args, **kwargs: _Response(html))
    leaderboard._cache.update(at=0.0, data={})

    status = leaderboard.current_status(cfg)

    assert status["return_pct"] == -8.51
    assert status["drawdown_pct"] == 21.36
    assert status["value_usd"] == 19.36
    assert status["trades"] == 29
    assert status["holdings"] == [["USDT", 19.36]]
    assert status["leaderboard_generated_ts"] == 1234
