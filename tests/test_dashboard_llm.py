import scripts.build_dashboard as dashboard


def test_dashboard_llm_status_inactive_without_key(monkeypatch):
    monkeypatch.setattr(dashboard, "_llm_provider", lambda: None)

    status = dashboard._llm_status({"llm": {"second_gate": True}})

    assert status["label"] == "rule-based"
    assert status["active"] is False


def test_dashboard_llm_status_active_only_when_gate_enabled(monkeypatch):
    monkeypatch.setattr(dashboard, "_llm_provider", lambda: "Gemini")

    inactive = dashboard._llm_status({"llm": {"second_gate": False}})
    active = dashboard._llm_status({"llm": {"second_gate": True}})

    assert inactive["active"] is False
    assert active["label"] == "Gemini review"
    assert active["status"] == "active veto"
