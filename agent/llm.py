"""
Provider-agnostic single-shot LLM completion.

The agent's LLM layer (decision override + news sentiment) is optional and
provider-neutral: it uses whichever key is present, preferring Gemini (generous
free tier) over Anthropic. With no key it returns None and callers fall back to
the deterministic path — so the agent always runs.

    GEMINI_API_KEY  (or GOOGLE_API_KEY) -> Gemini   [default model gemini-2.0-flash]
    ANTHROPIC_API_KEY                    -> Claude   [default model claude-haiku-4-5]
    (neither)                            -> None

Override models via GEMINI_MODEL / ANTHROPIC_MODEL env if desired.
"""

from __future__ import annotations

import os


def provider() -> str | None:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def available() -> bool:
    return provider() is not None


def complete(user: str, *, system: str = "", max_tokens: int = 512,
             model: str | None = None, timeout: float = 30.0) -> str | None:
    """Return the model's text, or None on no-key / any error (caller falls back)."""
    p = provider()
    try:
        if p == "gemini":
            return _gemini(user, system, max_tokens,
                           model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                           timeout)
        if p == "anthropic":
            return _anthropic(user, system, max_tokens,
                              model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                              timeout)
    except Exception:
        return None
    return None


def _gemini(user, system, max_tokens, model, timeout):
    import httpx
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    # Disable "thinking" (Gemini 2.5+): our tasks are short (a number / a JSON
    # object) and thinking silently eats the output-token budget, returning a
    # candidate with no text parts. thinkingBudget:0 -> fast, cheap, deterministic.
    body = {"contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    r = httpx.post(url, params={"key": key}, json=body, timeout=timeout)
    r.raise_for_status()
    parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def _anthropic(user, system, max_tokens, model, timeout):
    import anthropic
    cl = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=timeout)
    kw = {"model": model, "max_tokens": max_tokens,
          "messages": [{"role": "user", "content": user}]}
    if system:
        kw["system"] = system
    return cl.messages.create(**kw).content[0].text.strip()
