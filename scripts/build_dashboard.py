"""
Build the public dashboard (dashboard/index.html) — minimal, elegant, real data.

  * Hero   = REAL on-chain portfolio (live wallet balances via twak).
  * Track  = strategy backtest on real prices; chart -> live/paper equity once it
            has enough points.
  * Market = LIVE read: regime + Fear&Greed + BTC dominance + funding + leaderboard.

The CTA logo is embedded as a base64 data URI (assets/cta-logo.png) so it always
renders — live, locally, or after any redeploy — and never depends on a loose file.

    python scripts/build_dashboard.py             # full (slow: live market fetch)
    python scripts/build_dashboard.py --no-market  # fast (skip 40-token fetch)
    python scripts/build_dashboard.py --no-wallet  # offline
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USDT = "0x55d398326f99059fF775485246999027B3197955"
_TW = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/smartchain"


def _logo(sym, contract):
    if sym == "BNB":
        return _TW + "/info/logo.png"
    return f"{_TW}/assets/{contract}/logo.png" if contract else ""


def _logo_datauri():
    """Embed the CTA logo as a data URI so it never breaks. '' -> JS shows 🤖."""
    for p in (os.path.join(ROOT, "assets", "cta-logo.png"),
              os.path.join(ROOT, "dashboard", "logo.png")):
        if os.path.exists(p):
            b = base64.b64encode(open(p, "rb").read()).decode()
            return "data:image/png;base64," + b
    return ""


def _llm_provider():
    """Detect the configured LLM brain from env or .env (for a dashboard chip)."""
    env = dict(os.environ)
    envf = os.path.join(ROOT, ".env")
    if os.path.exists(envf):
        for line in open(envf):
            s = line.strip()
            if "=" in s and not s.startswith("#"):
                k, v = s.split("=", 1)
                env.setdefault(k, v.strip())
    if (env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip():
        return "Gemini"
    if (env.get("ANTHROPIC_API_KEY") or "").strip():
        return "Claude"
    return None


def _twak(args):
    try:
        out = subprocess.run(["twak", *args, "--json"], capture_output=True, text=True,
                             timeout=40, cwd=ROOT)
        return json.loads(out.stdout[out.stdout.find("{"):])
    except Exception:
        return None


def _wallet(cfg):
    addr = cfg["twak"]["agent_address"]
    holdings, total = [], 0.0
    u = _twak(["balance", "--address", addr, "--token", USDT, "--chain", "bsc"])
    if u and "available" in u:
        usd = float(u.get("totalUsd", 0) or 0)
        holdings.append({"sym": "USDT", "amount": round(float(u["available"]), 2), "usd": round(usd, 2),
                         "logo": _logo("USDT", USDT)})
        total += usd
    b = _twak(["wallet", "balance", "--chain", "bsc"])
    if b and "available" in b:
        usd = float(b.get("totalUsd", 0) or 0)
        holdings.append({"sym": "BNB", "amount": round(float(b["available"]), 5), "usd": round(usd, 2),
                         "logo": _logo("BNB", None)})
        total += usd
    try:
        st = json.load(open(os.path.join(ROOT, cfg["paths"]["state_file"])))
        for sym in st.get("positions", {}):
            at = cfg["twak"]["token_contracts"].get(sym)
            r = _twak(["balance", "--address", addr, "--token", at, "--chain", "bsc"]) if at else None
            if r and float(r.get("available", 0) or 0) > 0:
                usd = float(r.get("totalUsd", 0) or 0)
                holdings.append({"sym": sym, "amount": round(float(r["available"]), 4), "usd": round(usd, 2),
                                 "logo": _logo(sym, at)})
                total += usd
    except Exception:
        pass
    return {"total_usd": round(total, 2), "holdings": holdings}


def _market(cfg):
    try:
        from agent.signal_source import TwakCmcSignalClient
        from agent.signal_engine import score_universe, detect_regime
        snap = TwakCmcSignalClient(cfg).get_snapshot(cfg["whitelist"])
        if not snap:
            return None
        regime = detect_regime(snap, cfg).value
        sigs = score_universe(snap, cfg)
        any_d = next(iter(snap.values()))
        ranked = sorted(sigs.values(), key=lambda s: s.score, reverse=True)
        tc = cfg["twak"]["token_contracts"]
        top = [{"sym": s.token, "score": round(s.score, 3), "logo": _logo(s.token, tc.get(s.token)),
                "price": float(snap.get(s.token, {}).get("price", 0))}
               for s in ranked[:12]]
        bullish = sum(1 for s in sigs.values() if s.score > 0)
        tradeable = set(cfg["twak"]["token_contracts"])
        prices = {t: float(d.get("price", 0)) for t, d in snap.items()
                  if t in tradeable and float(d.get("price", 0)) > 0}
        return {"regime": regime, "fg": round(float(any_d.get("fear_greed_index", 50))),
                "dom": round(float(any_d.get("btc_dominance", 54)), 1),
                "funding": round(float(any_d.get("funding_rate", 0)), 4),
                "bullish": bullish, "total": len(sigs), "leaderboard": top, "prices": prices}
    except Exception:
        return None


def _bench_return(prices):
    """REAL equal-weight market return since the agent started, anchored once in
    dashboard/bench_anchor.json (reset by go_live.sh at the live window start)."""
    if not prices:
        return None
    path = os.path.join(ROOT, "dashboard", "bench_anchor.json")
    if os.path.exists(path):
        try:
            anchor = json.load(open(path))
        except Exception:
            anchor = None
    else:
        anchor = None
    if not anchor:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(prices, open(path, "w"))
        return 0.0
    rets = [prices[s] / anchor[s] - 1 for s in anchor
            if s in prices and anchor.get(s, 0) > 0]
    return round(sum(rets) / len(rets) * 100, 2) if rets else 0.0


def build_data(with_wallet=True, with_market=True):
    from agent.agent import load_config
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    bt = json.load(open(os.path.join(ROOT, "logs", "backtest_result.json")))
    rows = []
    try:
        for line in open(os.path.join(ROOT, cfg["paths"]["decision_log"])):
            rows.append(json.loads(line))
    except Exception:
        pass
    mode = cfg.get("mode", "dry_run")
    live = mode in ("live", "paper")
    market = _market(cfg) if with_market else None
    st, curve = {}, []
    if live:
        st = json.load(open(os.path.join(ROOT, cfg["paths"]["state_file"])))
        curve = st.get("equity_curve", [])
    dq = cfg["risk"]["drawdown_dq_reference_pct"] * 100
    ntok = len(cfg["twak"]["token_contracts"])
    if live and len(curve) >= 5:
        # REAL performance + chart from the live/paper equity curve
        chart = {"dates": [c[0][:10] for c in curve], "equity": [round(c[1], 4) for c in curve],
                 "benchmark": [], "label": "Live equity" if mode == "live" else "Paper equity"}
        eq = [c[1] for c in curve]
        init = st.get("initial_equity") or eq[0]
        ret = (eq[-1] / init - 1) * 100 if init else 0.0
        peak = eq[0]; mdd = 0.0
        for v in eq:
            if v > peak:
                peak = v
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        bh = _bench_return(market.get("prices")) if market else None
        track = {"return_pct": round(ret, 2), "buyhold_pct": bh if bh is not None else 0.0,
                 "maxdd_pct": round(mdd * 100, 2), "dq_pct": dq,
                 "trades": st.get("trade_count_total", 0), "tokens": ntok}
        track_source = mode                       # live | paper
    else:
        chart = {"dates": bt["dates"], "equity": bt["equity"], "benchmark": bt["benchmark"],
                 "label": "Backtest · 1y real prices"}
        track = {"return_pct": bt["kpis"]["total_return_pct"], "buyhold_pct": bt["kpis"]["buyhold_pct"],
                 "maxdd_pct": bt["kpis"]["max_drawdown_pct"], "dq_pct": bt["kpis"]["dq_pct"],
                 "trades": bt["kpis"]["trades"], "tokens": ntok}
        track_source = "backtest"
    kill = cfg["risk"]["drawdown_kill_pct"]
    posture = "Aggressive" if kill >= 0.24 else "Balanced" if kill >= 0.18 else "Defensive"
    prov = _llm_provider()
    return {
        "address": cfg["twak"]["agent_address"], "agent_id": cfg.get("bnb_sdk", {}).get("agent_id", ""),
        "live": live, "mode": mode, "generated_ts": int(time.time()),
        "llm": prov or "rule-based", "posture": posture,
        "portfolio": _wallet(cfg) if with_wallet else {"total_usd": None, "holdings": []},
        "market": market,
        "track": track, "track_source": track_source,
        "chart": chart,
        "risk": {"kill": kill * 100,
                 "stop": cfg["risk"]["per_position_stop_pct"] * 100, "policy": cfg["decision"]["policy"]},
        "blocked": len([r for r in rows if r.get("kind") == "blocked"]),
        "activity": [
            {"kind": r.get("kind"), "token": r.get("token", ""), "action": r.get("action", ""),
             "reason": (r.get("reason") or r.get("note") or "")[:70], "tx": r.get("tx_hash") or r.get("tx"),
             "realized": r.get("realized"),
             "logo": _logo(r.get("token", ""), cfg["twak"]["token_contracts"].get(r.get("token", ""))),
             "ts": (r.get("ts") or "")[11:16]}
            for r in rows if r.get("kind") in ("fill", "blocked", "x402", "position_stop", "kill_switch")
            # a hold/close "*_allowed" is a no-op verdict, not a real block -> hide it
            and not (r.get("kind") == "blocked" and str(r.get("reason", "")).endswith("_allowed"))
        ][-10:][::-1],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--no-wallet", action="store_true")
    ap.add_argument("--no-market", action="store_true")
    args = ap.parse_args()
    data = build_data(with_wallet=not args.no_wallet, with_market=not args.no_market)
    os.makedirs(os.path.join(ROOT, "dashboard"), exist_ok=True)
    html = TEMPLATE.replace("/*DATA*/", json.dumps(data)).replace("LOGO_SRC", _logo_datauri())
    with open(os.path.join(ROOT, "dashboard", "index.html"), "w") as f:
        f.write(html)
    # publish the raw decision log (tail) for reproducibility / judges
    try:
        from agent.agent import load_config
        lp = os.path.join(ROOT, load_config(os.path.join(ROOT, "config.yaml"))["paths"]["decision_log"])
        lines = open(lp).read().splitlines()[-2000:]
        with open(os.path.join(ROOT, "dashboard", "decisions.jsonl"), "w") as f:
            f.write("\n".join(lines))
    except Exception:
        pass
    m = data.get("market")
    print(f"-> dashboard (portfolio ${data['portfolio']['total_usd']}, "
          f"market={'live '+m['regime'] if m else 'n/a'}, {'live' if data['live'] else 'backtest'} chart, "
          f"llm={data['llm']}, {data['posture']})")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta http-equiv="refresh" content="60"/>
<title>CTA · CMC-TWAK-Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--g:#34d399;--r:#fb7185;--b:#8aa9ff;--am:#fbbf63;--tx:#eef2fb;--mut:#9aa6c6;--mut2:#5d6788;
--bd:rgba(255,255,255,.07);--cell:rgba(255,255,255,.025);--card:rgba(255,255,255,.028)}
html{-webkit-text-size-adjust:100%}
body{min-height:100vh;font-family:'Inter',system-ui,sans-serif;color:var(--tx);letter-spacing:-.011em;
background:#05070e;
background-image:radial-gradient(1200px 700px at 50% -15%,rgba(80,120,240,.10),transparent 60%);
background-attachment:fixed;padding:40px 20px 32px;-webkit-font-smoothing:antialiased;font-size:13.5px;line-height:1.5}
.wrap{max-width:940px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:26px 28px}
.lab{font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--mut);font-weight:600}
.num{font-variant-numeric:tabular-nums}
.pos{color:var(--g)}.neg{color:var(--r)}.acc{color:var(--b)}
/* header */
.head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:0 4px}
.brand{display:flex;align-items:center;gap:13px}
.logo{width:42px;height:42px;border-radius:11px;object-fit:cover;background:rgba(255,255,255,.04);border:1px solid var(--bd)}
.nm{display:flex;flex-direction:column;line-height:1.15}
.nm b{font-size:19px;font-weight:800;letter-spacing:-.3px}
.nm small{font-weight:500;color:var(--mut);font-size:10.5px;letter-spacing:.4px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:600;color:var(--g);
 background:rgba(52,211,153,.09);border:1px solid rgba(52,211,153,.24);padding:4px 10px;border-radius:999px;margin-left:4px}
.badge i{width:6px;height:6px;border-radius:50%;background:var(--g);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45)}70%{box-shadow:0 0 0 6px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
.beat{font-size:10.5px;color:var(--mut2);margin-left:2px}
.chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center;justify-content:flex-end}
.chip{font-size:10.5px;font-weight:500;color:var(--mut);background:var(--cell);border:1px solid var(--bd);padding:5px 11px;border-radius:999px}
.chip b{color:var(--tx);font-weight:700}
.chip.on{color:var(--g);border-color:rgba(52,211,153,.26)}
/* portfolio hero */
.prow{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;flex-wrap:wrap;margin-top:10px}
.big{font-size:44px;font-weight:800;letter-spacing:-1.4px;line-height:1;margin:6px 0 4px;
 background:linear-gradient(96deg,#fff,#c4d4ff);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--mut);font-size:12px}
.holds{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.hchip{display:flex;align-items:center;gap:8px;background:var(--cell);border:1px solid var(--bd);border-radius:12px;padding:9px 14px;font-weight:600;font-size:12.5px}
.hchip .ha{color:var(--mut);font-weight:500;font-size:11.5px;margin-left:1px}
/* section header inside card */
.ph{font-size:11px;font-weight:600;letter-spacing:.4px;color:#c4cdea;display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;text-transform:uppercase}
.ph span,.ph a{color:var(--mut2);font-weight:500;font-size:10px;letter-spacing:.3px;text-transform:none}
.ph a{color:var(--b);text-decoration:none}
/* track record */
.trgrid{display:grid;grid-template-columns:1.25fr 1fr;gap:30px;align-items:center}
@media(max-width:720px){.trgrid{grid-template-columns:1fr;gap:20px}}
.ret{font-size:40px;font-weight:800;letter-spacing:-1px;line-height:1;margin-bottom:4px}
.cmp{margin-top:18px;display:flex;flex-direction:column;gap:10px}
.cmprow{display:flex;align-items:center;gap:12px;font-size:12px}
.cmprow .cl{width:50px;color:var(--mut)}
.cmprow .cbar{flex:1;height:8px;background:rgba(255,255,255,.045);border-radius:999px;overflow:hidden}
.cmprow .cbar b{display:block;height:100%;border-radius:999px}
.cmprow .cv{width:58px;text-align:right;font-weight:700}
.trstats{display:flex;flex-direction:column;gap:9px}
.trstats .m{background:var(--cell);border:1px solid var(--bd);border-radius:12px;padding:11px 15px;display:flex;justify-content:space-between;align-items:center}
.trstats .m .k{font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.trstats .m .x{font-size:18px;font-weight:700}
/* chart */
.cw{position:relative;margin-top:22px}
.tip{position:absolute;pointer-events:none;background:rgba(8,12,22,.96);border:1px solid rgba(255,255,255,.14);
 border-radius:8px;padding:5px 9px;font-size:11px;opacity:0;transition:opacity .1s;white-space:nowrap;transform:translate(-50%,-135%);z-index:2}
.lg{display:flex;gap:16px;font-size:10.5px;color:var(--mut);margin-top:10px}
.lg i{display:inline-block;width:9px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
/* market */
.mtop{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.regime{font-size:11.5px;font-weight:700;padding:5px 13px;border-radius:999px}
.fgblock{margin-bottom:18px;max-width:540px}
.fgbar{height:6px;border-radius:999px;background:linear-gradient(90deg,#fb7185,#fbbf63,#34d399);position:relative;margin-top:10px}
.fgbar i{position:absolute;top:-4px;width:14px;height:14px;border-radius:50%;background:#fff;border:3px solid #070b14;transform:translateX(-50%);box-shadow:0 2px 6px rgba(0,0,0,.5);animation:fgpulse 2s ease-in-out infinite}
@keyframes fgpulse{0%,100%{box-shadow:0 0 0 0 rgba(255,255,255,.22),0 2px 6px rgba(0,0,0,.5)}50%{box-shadow:0 0 0 6px rgba(255,255,255,0),0 2px 6px rgba(0,0,0,.5)}}
.mstats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:22px}
@media(max-width:620px){.mstats{grid-template-columns:1fr}}
.cell{background:var(--cell);border:1px solid var(--bd);border-radius:12px;padding:13px 15px}
.mkv{font-size:18px;font-weight:700;margin-top:6px}
.leadgrid{display:grid;grid-template-columns:1fr 1fr;gap:1px 32px}
@media(max-width:620px){.leadgrid{grid-template-columns:1fr}}
.lr{display:flex;align-items:center;gap:8px;padding:7px 0}
.lr .rk{font-size:10px;color:var(--mut2);width:13px}
.lr .tk{font-weight:600;width:50px;font-size:12px}
.lr .px{flex:1;text-align:right;font-size:11px;color:var(--mut2);font-variant-numeric:tabular-nums}
.lr .pulse{width:7px;height:7px;border-radius:50%;flex:none;animation:dotpulse 1.8s ease-in-out infinite}
.lr .sc{width:46px;text-align:right;font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums}
@keyframes dotpulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(.78)}}
.ico{width:21px;height:21px;border-radius:50%;background:#141a27;object-fit:cover;vertical-align:middle;border:1px solid var(--bd)}
.ico.sm{width:17px;height:17px}
.ico.lt{display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:8px;letter-spacing:-.3px;color:#aeb8d8;background:#1b2334}
.dotk{width:7px;height:7px;border-radius:50%;background:var(--b);display:inline-block}
/* activity */
.act{display:flex;align-items:center;gap:11px;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.04);font-size:12px}
.act:last-child{border:0}
.act .kd{font-weight:700;width:60px;font-size:10.5px;letter-spacing:.3px}
.act .tkn{width:52px;font-weight:600}
.act .rs{flex:1;color:var(--mut)}
.act .tm{color:var(--mut2);font-size:10.5px;font-variant-numeric:tabular-nums}
.act a{color:var(--b);text-decoration:none;font-weight:600}
/* footer config */
.foot{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:2px}
.fchip{font-size:10.5px;color:var(--mut);background:var(--cell);border:1px solid var(--bd);padding:6px 12px;border-radius:999px}
.fchip b{color:var(--tx);font-weight:700}
.fchip.k b{color:var(--g)}
.credit{text-align:center;color:var(--mut2);font-size:10.5px;margin-top:8px}
.credit a{color:var(--b);text-decoration:none}.credit b{color:var(--mut)}
/* tagline + hero framing + sponsors */
.tagline{color:var(--mut);font-size:13px;line-height:1.5;padding:0 4px;max-width:680px}
.tagline b{color:var(--tx);font-weight:600}
.edge{font-size:52px;font-weight:800;letter-spacing:-1.6px;line-height:1;margin:4px 0 6px;
 background:linear-gradient(96deg,#5ef0a8,#34d399);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.edge.neg{background:none;-webkit-text-fill-color:var(--r);color:var(--r)}
.story{color:var(--tx);font-size:14px;font-weight:600;margin-bottom:2px}
.grow{font-size:12.5px;color:var(--mut);margin-top:14px;line-height:1.7}
.grow b{font-weight:700}
.sponsors{display:flex;align-items:center;justify-content:center;gap:9px;flex-wrap:wrap;
 padding:16px 18px;background:var(--card);border:1px solid var(--bd);border-radius:16px;font-size:11.5px;color:var(--mut)}
.sponsors b{color:var(--tx);font-weight:600}
.sponsors .dot{color:var(--mut2)}
.sponsors .x402{color:var(--b);font-weight:700}
</style></head>
<body><div class="wrap">

<div class="head">
  <div class="brand">
    <img class="logo" src="LOGO_SRC" alt="CTA" onerror="this.outerHTML='<span style=font-size:34px>🤖</span>'"/>
    <span class="nm"><b>CTA</b><small>CMC · TWAK · Agent</small></span>
    <span class="badge"><i></i><span id="mode"></span></span>
    <span class="beat" id="beat"></span>
  </div>
  <div class="chips" id="chips"></div>
</div>

<div class="card">
  <div class="lab">Portfolio</div>
  <div class="prow">
    <div><div class="big num" id="pv">—</div><div class="sub" id="pvsub"></div></div>
    <div id="holds" class="holds"></div>
  </div>
</div>

<div class="card">
  <div class="ph">Performance <span id="trk"></span></div>
  <div class="trgrid">
    <div>
      <div class="lab">Bot vs market</div>
      <div class="edge num" id="ret"></div>
      <div class="cmp" id="cmp"></div>
    </div>
    <div class="trstats">
      <div class="m"><span class="k">Max drawdown</span><span class="x pos num" id="dd"></span></div>
      <div class="m"><span class="k">DQ headroom</span><span class="x acc num" id="hr"></span></div>
      <div class="m"><span class="k">Trades</span><span class="x num" id="tr"></span></div>
    </div>
  </div>
  <div class="ph" style="margin:26px 0 0"><span id="clab"></span><span id="cmeta"></span></div>
  <div class="cw" id="cw"><div class="tip" id="tip"></div></div>
  <div class="lg" id="lg"></div>
</div>

<div class="card" id="mcard">
  <div class="ph">Live market read <span>CMC Agent Hub</span></div>
  <div id="market"></div>
  <div class="ph" style="margin:24px 0 14px">Momentum leaderboard <span>top movers now</span></div>
  <div id="lead" class="leadgrid"></div>
</div>

<div class="card">
  <div class="ph">Recent activity · decision log <span><a href="decisions.jsonl">raw log ↓</a></span></div>
  <div id="activity"></div>
</div>

<div class="sponsors"><span>Powered by</span><b>CoinMarketCap Agent Hub</b><span class="dot">·</span><b>Trust Wallet Agent Kit</b><span class="dot">·</span><b>BNB ERC-8004</b><span class="dot">·</span><span class="x402">x402 micropayments</span></div>
<div class="foot" id="cfg"></div>
<div class="credit">agent <span id="addr"></span> ·
 <a href="https://github.com/DanMarteens/cmc-twak-trading-agent" target="_blank">source</a> · #CMCAgentHub</div>

</div>
<script>
const D=/*DATA*/, $=i=>document.getElementById(i);
// token icon missing on the CDN -> clean letter avatar (no blank gaps)
function fbk(el,s){el.outerHTML='<span class="ico sm lt">'+(s||'?').slice(0,3)+'</span>';}
const REG={trend_up:['#34d399','rgba(52,211,153,.13)','uptrend'],trend_down:['#fb7185','rgba(251,113,133,.13)','downtrend'],chop:['#fbbf63','rgba(251,191,99,.13)','chop']};
$('mode').textContent={live:'LIVE',paper:'PAPER · real signals',dry_run:'ARMED'}[D.mode]||'ARMED';
const ago=Math.max(0,Math.round(Date.now()/1000-D.generated_ts));
$('beat').textContent='updated '+(ago<90?ago+'s':Math.round(ago/60)+'m')+' ago';
$('chips').innerHTML=[`<span class="chip on">🟢 registered</span>`,
 `<span class="chip">ERC-8004 <b>#${D.agent_id}</b></span>`,
 `<span class="chip">🧠 <b>${D.llm}</b></span>`,
 `<span class="chip"><b>${D.posture}</b></span>`,
 `<span class="chip"><b>${D.track.tokens}</b>/149 eligible</span>`].join('');

const pv=D.portfolio.total_usd;
if(pv!=null){const t0=performance.now();(function a(n){let p=Math.min((n-t0)/750,1);p=1-Math.pow(1-p,3);
 $('pv').textContent='$'+(pv*p).toFixed(2);if(p<1)requestAnimationFrame(a);})(t0);}else $('pv').textContent='—';
$('pvsub').textContent=D.portfolio.holdings.length?'across '+D.portfolio.holdings.length+' assets':'fund wallet to begin';
$('holds').innerHTML=D.portfolio.holdings.map(h=>`<div class="hchip">
 <img class="ico" src="${h.logo}" onerror="this.outerHTML='<i class=dotk></i>'"/>${h.sym}
 <span class="ha num">${h.amount} · $${h.usd.toFixed(2)}</span></div>`).join('');

const t=D.track,delta=+(t.return_pct-t.buyhold_pct).toFixed(2);
$('trk').textContent=D.track_source==='backtest'?'1y backtest · real prices':'';
$('ret').textContent=(delta>=0?'+':'')+delta+'%';$('ret').className='edge num'+(delta>=0?'':' neg');
$('dd').textContent=t.maxdd_pct+'%';$('hr').textContent=(t.dq_pct-t.maxdd_pct).toFixed(0)+'%';$('tr').textContent=t.trades;
(function(){const ar=Math.abs(t.return_pct),mr=Math.abs(t.buyhold_pct),mx=Math.max(ar,mr,1);
 $('cmp').innerHTML=`
  <div class="cmprow"><span class="cl">Bot</span><span class="cbar"><b style="width:${(ar/mx*100).toFixed(0)}%;background:${t.return_pct>=0?'var(--g)':'var(--r)'}"></b></span><span class="cv ${t.return_pct>=0?'pos':'neg'}">${(t.return_pct>=0?'+':'')+t.return_pct}%</span></div>
  <div class="cmprow"><span class="cl">Market</span><span class="cbar"><b style="width:${(mr/mx*100).toFixed(0)}%;background:var(--r);opacity:.5"></b></span><span class="cv neg">${t.buyhold_pct}%</span></div>`;
})();

$('cfg').innerHTML=[`<span class="fchip">strategy <b>${D.risk.policy}</b></span>`,
 `<span class="fchip k">per-position stop <b>${D.risk.stop}%</b></span>`,
 `<span class="fchip k">kill switch <b>${D.risk.kill}%</b></span>`,
 `<span class="fchip">blocked by rules <b>${D.blocked}</b></span>`].join('');
$('addr').textContent=D.address.slice(0,6)+'…'+D.address.slice(-4);

const mk=D.market;
if(mk){const[col,bg,nm]=REG[mk.regime]||REG.chop;
 const fl=mk.fg<25?'Extreme fear':mk.fg<45?'Fear':mk.fg<55?'Neutral':mk.fg<75?'Greed':'Extreme greed';
 $('market').innerHTML=`
  <div class="fgblock"><div class="lab">Fear &amp; Greed — <b style="color:${col}">${mk.fg} · ${fl}</b></div>
    <div class="fgbar"><i style="left:${mk.fg}%"></i></div></div>
  <div class="mstats">
    <div class="cell"><div class="lab">BTC dominance</div><div class="mkv num">${mk.dom}%</div></div>
    <div class="cell"><div class="lab">Funding (perps)</div><div class="mkv num" style="color:${mk.funding>=0?'var(--g)':'var(--r)'}">${mk.funding>=0?'+':''}${mk.funding}%</div></div>
    <div class="cell"><div class="lab">Bullish now</div><div class="mkv num"><b class="pos">${mk.bullish}</b> / ${mk.total}</div></div>
  </div>`;
 $('lead').innerHTML=mk.leaderboard.map((l,i)=>{const p=l.score>=0,c=p?'var(--g)':'var(--r)';
   const px=l.price>=1?('$'+l.price.toFixed(2)):('$'+(+l.price||0).toPrecision(2));
   return `<div class="lr"><span class="rk">${i+1}</span>
   <img class="ico sm" src="${l.logo}" onerror="fbk(this,'${l.sym}')"/><span class="tk">${l.sym}</span>
   <span class="px">${px}</span>
   <span class="pulse" style="background:${c};box-shadow:0 0 6px ${c}"></span>
   <span class="sc" style="color:${c}">${p?'+':''}${l.score.toFixed(2)}</span></div>`;}).join('');
}else $('mcard').style.display='none';

// recent activity / decision log
function actClean(a){
 if(a.kind==='fill'){
  if(a.action==='buy'){const m=(a.reason||'').match(/\$[\d.]+/);return 'entered'+(m?' '+m[0]:'');}
  const r=a.realized;            // realized P&L on the close — the real win/loss
  if(r!=null&&r!==0){const g=r>=0;return 'exited <b style="color:'+(g?'var(--g)':'var(--r)')+'">'+(g?'+':'−')+'$'+Math.abs(r).toFixed(2)+'</b>';}
  return 'exited';}
 if(a.kind==='blocked'){const r=a.reason||'';
  if(r.includes('min_seconds'))return 'rate-limited';
  if(r.includes('daily_pause'))return 'daily pause (risk-off)';
  if(r.includes('drawdown_kill'))return 'kill switch';
  if(r.includes('concentration'))return 'concentration cap';
  if(r.includes('not_tradeable'))return 'off-universe';
  if(r.includes('low_confidence'))return 'low confidence';
  if(r.includes('max_trades'))return 'daily trade cap';
  if(r.includes('token_risk'))return 'token risk too high';
  return r.split(':')[0];}
 if(a.kind==='x402')return 'paid $0.001 for premium signal';
 if(a.kind==='position_stop')return 'per-position stop';
 if(a.kind==='kill_switch')return 'kill switch · liquidate all';
 return a.reason||'';}
$('activity').innerHTML=((D.activity&&D.activity.length)?D.activity:[]).map(a=>{
 const isBuy=a.kind==='fill'&&a.action==='buy', isClose=a.kind==='fill'&&(a.action==='close'||a.action==='sell');
 const col=isBuy?'var(--g)':isClose?'var(--b)':a.kind==='blocked'?'var(--am)':a.kind==='x402'?'var(--b)':'var(--r)';
 const tag=isBuy?'BUY':isClose?'CLOSE':a.kind==='blocked'?'BLOCKED':a.kind==='x402'?'X402':a.kind.replace('_',' ').toUpperCase();
 const real=a.tx&&(''+a.tx).startsWith('0x')&&!(''+a.tx).startsWith('0xMOCK');
 const ex=a.kind==='x402'?'https://basescan.org/tx/':'https://bscscan.com/tx/';
 const link=real?` <a href="${ex}${a.tx}" target="_blank">↗</a>`:'';
 const ic=a.logo?`<img class="ico sm" src="${a.logo}" onerror="fbk(this,'${a.token}')"/>`:(a.token?`<span class="ico sm lt">${a.token.slice(0,3)}</span>`:'<span style="width:17px;display:inline-block"></span>');
 return `<div class="act">${ic}<span class="kd" style="color:${col}">${tag}</span><span class="tkn">${a.token||''}</span><span class="rs">${actClean(a)}${link}</span><span class="tm">${a.ts}</span></div>`;
}).join('')||'<div class="rs" style="color:var(--mut2);font-size:12px;padding:6px 0">Holding cash in the downtrend (capital preserved). Rotations resume when the market turns up; a maintenance trade keeps the daily minimum.</div>';

// ---- chart ----
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmt=s=>{if(!s)return'';const d=s.split('-');return MON[(+d[1]||1)-1]+' '+(+d[2]||'');};
(function(){const c=D.chart,N=c.equity.length;if(N<2)return;
 const W=900,H=290,L=14,R=54,T=18,B=28;
 const hasB=c.benchmark&&c.benchmark.length;const all=c.equity.concat(hasB?c.benchmark:[]);
 let mn=Math.min(...all),mx=Math.max(...all);const pad=(mx-mn)*.12||1;mn-=pad;mx+=pad;
 const X=i=>L+i*(W-L-R)/(N-1),Y=v=>T+(1-(v-mn)/(mx-mn))*(H-T-B);
 const up=c.equity[N-1]>=c.equity[0],col=up?'#34d399':'#fb7185';
 function sm(pts){let d='M'+pts[0][0].toFixed(1)+' '+pts[0][1].toFixed(1);
  for(let i=0;i<pts.length-1;i++){const a=pts[i-1]||pts[i],b=pts[i],e=pts[i+1],f=pts[i+2]||e;
  d+=`C${(b[0]+(e[0]-a[0])/6).toFixed(1)} ${(b[1]+(e[1]-a[1])/6).toFixed(1)},${(e[0]-(f[0]-b[0])/6).toFixed(1)} ${(e[1]-(f[1]-b[1])/6).toFixed(1)},${e[0].toFixed(1)} ${e[1].toFixed(1)}`;}return d;}
 const eP=c.equity.map((v,i)=>[X(i),Y(v)]),eD=sm(eP);
 const ydec=(mx-mn)<2?3:(mx-mn)<20?2:0;
 let ticks='';for(let k=0;k<=3;k++){const v=mn+(mx-mn)*k/3,y=Y(v);
  ticks+=`<line x1="${L}" y1="${y.toFixed(1)}" x2="${W-R}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.045)"/>
  <text x="${W-R+8}" y="${(y+3).toFixed(1)}" fill="var(--mut2)" font-size="10">$${v.toFixed(ydec)}</text>`;}
 const base=Y(c.equity[0]);
 $('clab').textContent=c.label;$('cmeta').textContent=N+' points';
 $('lg').innerHTML=`<span><i style="background:${col}"></i>Agent</span>`+(hasB?`<span><i style="background:var(--r)"></i>Market (buy&amp;hold)</span>`:'')+`<span><i style="background:var(--mut2)"></i>start</span>`;
 $('cw').insertAdjacentHTML('afterbegin',`<svg id="svg" viewBox="0 0 ${W} ${H}" width="100%" style="display:block">
  <defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".26"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient>
  <filter id="gl"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
  ${ticks}
  <line x1="${L}" y1="${base.toFixed(1)}" x2="${W-R}" y2="${base.toFixed(1)}" stroke="var(--mut2)" stroke-width="1" stroke-dasharray="2 4" opacity=".6"/>
  <path d="${eD} L${X(N-1).toFixed(1)} ${H-B} L${L} ${H-B} Z" fill="url(#ag)"/>
  ${hasB?`<path d="${sm(c.benchmark.map((v,i)=>[X(i),Y(v)]))}" fill="none" stroke="var(--r)" stroke-width="1.6" stroke-dasharray="5 5" opacity=".5"/>`:''}
  <path d="${eD}" fill="none" stroke="${col}" stroke-width="2.4" stroke-linejoin="round" filter="url(#gl)"
    pathLength="1" style="stroke-dasharray:1;stroke-dashoffset:1;animation:dr 1.5s ease forwards"/>
  <style>@keyframes dr{to{stroke-dashoffset:0}}</style>
  <line id="cx" x1="0" y1="${T}" x2="0" y2="${H-B}" stroke="rgba(255,255,255,.26)" stroke-width="1" opacity="0"/>
  <circle id="cd" r="4" fill="${col}" stroke="#070b14" stroke-width="2" opacity="0"/>
  <text x="${L}" y="${H-8}" fill="var(--mut2)" font-size="10">${fmt(c.dates[0])}</text>
  <text x="${(L+(W-R))/2}" y="${H-8}" fill="var(--mut2)" font-size="10" text-anchor="middle">${fmt(c.dates[Math.floor(N/2)])}</text>
  <text x="${W-R}" y="${H-8}" fill="var(--mut2)" font-size="10" text-anchor="end">${fmt(c.dates[c.dates.length-1])}</text></svg>`);
 const svg=$('svg'),tip=$('tip'),cx=$('cx'),cd=$('cd');
 svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();let i=Math.round(((e.clientX-r.left)/r.width*W-L)/((W-L-R)/(N-1)));
  i=Math.max(0,Math.min(N-1,i));const x=X(i),y=Y(c.equity[i]);
  cx.setAttribute('x1',x);cx.setAttribute('x2',x);cx.setAttribute('opacity','1');cd.setAttribute('cx',x);cd.setAttribute('cy',y);cd.setAttribute('opacity','1');
  tip.style.opacity=1;tip.style.left=(x/W*100)+'%';tip.style.top=(y/H*100)+'%';
  tip.innerHTML=`<b>$${c.equity[i].toFixed(2)}</b> · ${fmt(c.dates[i])}`;});
 svg.addEventListener('mouseleave',()=>{tip.style.opacity=0;cx.setAttribute('opacity','0');cd.setAttribute('opacity','0');});
})();
</script></body></html>"""


if __name__ == "__main__":
    main()
