"""
Build the public dashboard (dashboard/index.html) — glassmorphism, real data.

  * Hero  = REAL on-chain portfolio (live wallet balances via twak).
  * Market = LIVE read: regime + Fear&Greed + BTC dominance + momentum leaderboard.
  * Track  = strategy backtest on real prices; chart -> live/paper equity once it
            has enough points.

    python scripts/build_dashboard.py             # full (slow: live market fetch)
    python scripts/build_dashboard.py --no-market  # fast (skip 40-token fetch)
    python scripts/build_dashboard.py --no-wallet  # offline
"""

import argparse
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
        top = [{"sym": s.token, "score": round(s.score, 3), "logo": _logo(s.token, tc.get(s.token))}
               for s in ranked[:9]]
        bullish = sum(1 for s in sigs.values() if s.score > 0)
        return {"regime": regime, "fg": round(float(any_d.get("fear_greed_index", 50))),
                "dom": round(float(any_d.get("btc_dominance", 54)), 1),
                "funding": round(float(any_d.get("funding_rate", 0)), 4),
                "bullish": bullish, "total": len(sigs), "leaderboard": top}
    except Exception:
        return None


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
    curve = []
    if live:
        st = json.load(open(os.path.join(ROOT, cfg["paths"]["state_file"])))
        curve = st.get("equity_curve", [])
    if live and len(curve) >= 10:
        lbl = "Live equity" if mode == "live" else "Paper equity · real signals"
        chart = {"dates": [c[0][:10] for c in curve], "equity": [round(c[1], 4) for c in curve],
                 "benchmark": [], "label": lbl}
    else:
        chart = {"dates": bt["dates"], "equity": bt["equity"], "benchmark": bt["benchmark"],
                 "label": "Backtest · 1y real prices"}
    return {
        "address": cfg["twak"]["agent_address"], "agent_id": cfg.get("bnb_sdk", {}).get("agent_id", ""),
        "live": live, "mode": mode, "generated_ts": int(time.time()),
        "portfolio": _wallet(cfg) if with_wallet else {"total_usd": None, "holdings": []},
        "market": _market(cfg) if with_market else None,
        "track": {"return_pct": bt["kpis"]["total_return_pct"], "buyhold_pct": bt["kpis"]["buyhold_pct"],
                  "maxdd_pct": bt["kpis"]["max_drawdown_pct"], "dq_pct": bt["kpis"]["dq_pct"],
                  "trades": bt["kpis"]["trades"], "tokens": len(cfg["twak"]["token_contracts"])},
        "chart": chart,
        "risk": {"kill": cfg["risk"]["drawdown_kill_pct"] * 100,
                 "stop": cfg["risk"]["per_position_stop_pct"] * 100, "policy": cfg["decision"]["policy"]},
        "blocked": len([r for r in rows if r.get("kind") == "blocked"]),
        "activity": [
            {"kind": r.get("kind"), "token": r.get("token", ""), "action": r.get("action", ""),
             "reason": (r.get("reason") or r.get("note") or "")[:60], "tx": r.get("tx_hash") or r.get("tx"),
             "ts": (r.get("ts") or "")[11:19]}
            for r in rows if r.get("kind") in ("fill", "blocked", "x402", "position_stop", "kill_switch")
        ][-12:][::-1],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--no-wallet", action="store_true")
    ap.add_argument("--no-market", action="store_true")
    args = ap.parse_args()
    data = build_data(with_wallet=not args.no_wallet, with_market=not args.no_market)
    os.makedirs(os.path.join(ROOT, "dashboard"), exist_ok=True)
    html = TEMPLATE.replace("/*DATA*/", json.dumps(data))
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
          f"market={'live '+m['regime'] if m else 'n/a'}, {'live' if data['live'] else 'backtest'} chart)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta http-equiv="refresh" content="60"/>
<title>CTA · CMC-TWAK-Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--g:#2fd27e;--r:#ff6b78;--b:#7ea6ff;--am:#ffb547;--tx:#eaf0ff;--mut:#8b97bd;--mut2:#646f96;
--bd:rgba(255,255,255,.09);--cell:rgba(255,255,255,.035)}
body{min-height:100vh;font-family:'Inter',system-ui,sans-serif;color:var(--tx);letter-spacing:-.01em;
background:#070b15;background-image:
 radial-gradient(1100px 620px at 8% -10%,rgba(77,141,255,.18),transparent 60%),
 radial-gradient(950px 600px at 99% -6%,rgba(155,140,255,.15),transparent 55%),
 radial-gradient(900px 700px at 55% 118%,rgba(47,210,126,.11),transparent 55%);
background-attachment:fixed;padding:28px 20px;-webkit-font-smoothing:antialiased;font-size:14px}
.wrap{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
/* one unified card */
.card{background:linear-gradient(180deg,rgba(255,255,255,.052),rgba(255,255,255,.022));
 border:1px solid var(--bd);border-radius:20px;padding:24px 26px;backdrop-filter:blur(18px);
 -webkit-backdrop-filter:blur(18px);box-shadow:0 8px 36px rgba(0,0,0,.32)}
.g2{display:grid;grid-template-columns:1.12fr 1fr;gap:14px;align-items:start}
.g2c{display:grid;grid-template-columns:1.7fr 1fr;gap:14px;align-items:start}
@media(max-width:840px){.g2,.g2c{grid-template-columns:1fr}}
.mkt{display:flex;align-items:center;gap:34px;flex-wrap:wrap}
.mkt .it{display:flex;flex-direction:column;gap:5px}
.mkt .fg{flex:1;min-width:230px}
.mkt .cell{background:var(--cell);border:1px solid var(--bd);border-radius:13px;padding:11px 16px}
.mkv{font-size:19px;font-weight:700}
.prow{display:flex;align-items:flex-end;gap:34px;flex-wrap:wrap;margin-top:8px}
.holds{display:flex;gap:10px;flex-wrap:wrap;flex:1;justify-content:flex-end}
.hchip{display:flex;align-items:center;gap:9px;background:var(--cell);border:1px solid var(--bd);border-radius:13px;padding:11px 16px;font-weight:600}
.hchip .ha{color:var(--mut);font-weight:500;font-size:12px;margin-left:2px}
.leadgrid{display:grid;grid-template-columns:1fr 1fr;gap:2px 30px}
@media(max-width:720px){.leadgrid{grid-template-columns:1fr}}
.act{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:12.5px}
.act:last-child{border:0}
.act .kd{font-weight:600;width:96px}
.act .rs{flex:1;color:var(--mut)}
.act .tm{color:var(--mut2);font-size:11px;font-variant-numeric:tabular-nums}
.act a{color:var(--b);text-decoration:none}
.lab{font-size:10.5px;letter-spacing:.7px;text-transform:uppercase;color:var(--mut);font-weight:600}
.head{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
.title{font-size:18px;font-weight:700;display:flex;align-items:center;gap:10px}
.logo{width:46px;height:46px;border-radius:12px;vertical-align:middle;background:rgba(255,255,255,.04);border:1px solid var(--bd)}
.nm{display:flex;flex-direction:column;line-height:1.1}
.nm b{font-size:22px;font-weight:800;letter-spacing:-.4px}
.nm small{font-weight:500;color:var(--mut);font-size:11px;letter-spacing:.3px}
.badge{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:var(--g);
 background:rgba(47,210,126,.1);border:1px solid rgba(47,210,126,.28);padding:5px 11px;border-radius:999px}
.badge i{width:7px;height:7px;border-radius:50%;background:var(--g);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,210,126,.5)}70%{box-shadow:0 0 0 7px rgba(47,210,126,0)}100%{box-shadow:0 0 0 0 rgba(47,210,126,0)}}
.chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.chip{font-size:11px;font-weight:550;color:var(--mut);background:var(--cell);border:1px solid var(--bd);padding:5px 10px;border-radius:999px}
.chip b{color:var(--tx)}.beat{font-size:11px;color:var(--mut2)}
.chip.on{color:var(--g);border-color:rgba(47,210,126,.3)}
.trgrid{display:grid;grid-template-columns:1.3fr 1fr;gap:28px;align-items:center;margin-top:8px}
@media(max-width:720px){.trgrid{grid-template-columns:1fr;gap:18px}}
.cmp{margin-top:16px;display:flex;flex-direction:column;gap:9px}
.cmprow{display:flex;align-items:center;gap:12px;font-size:12.5px}
.cmprow .cl{width:52px;color:var(--mut)}
.cmprow .cbar{flex:1;height:9px;background:rgba(255,255,255,.05);border-radius:999px;overflow:hidden}
.cmprow .cbar b{display:block;height:100%;border-radius:999px}
.cmprow .cv{width:60px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
.trstats{display:flex;flex-direction:column;gap:10px}
.trstats .m{background:var(--cell);border:1px solid var(--bd);border-radius:13px;padding:11px 15px;display:flex;justify-content:space-between;align-items:center}
.trstats .m .k{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.trstats .m .x{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.big{font-size:46px;font-weight:800;letter-spacing:-1.4px;line-height:1;margin:8px 0 3px;
 background:linear-gradient(92deg,#fff,#b9ccff);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--mut);font-size:12.5px}
.rowline{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.05)}
.rowline:last-child{border:0}
.dotk{width:7px;height:7px;border-radius:50%;background:var(--b);display:inline-block;margin-right:8px}
.ico{width:22px;height:22px;border-radius:50%;background:#19202f;object-fit:cover;vertical-align:middle;border:1px solid var(--bd)}
.ico.sm{width:17px;height:17px}
.aset{display:flex;align-items:center;gap:9px;font-weight:600}
.num{font-variant-numeric:tabular-nums}
.pos{color:var(--g)}.neg{color:var(--r)}.acc{color:var(--b)}
.ret{font-size:34px;font-weight:800;letter-spacing:-.6px;margin:7px 0 2px}
.kv{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}
.kv .c{background:var(--cell);border:1px solid var(--bd);border-radius:13px;padding:12px 14px}
.kv .c .k{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.kv .c .v{font-size:20px;font-weight:700;margin-top:5px}
.ph{font-size:12px;font-weight:600;color:#cdd7f5;display:flex;justify-content:space-between;align-items:center;margin-bottom:13px}
.ph span{color:var(--mut);font-weight:500;font-size:10.5px}
.regime{font-size:12.5px;font-weight:700;padding:4px 11px;border-radius:999px}
.fgbar{height:7px;border-radius:999px;background:linear-gradient(90deg,#ff5d6c,#ffb547,#2fd27e);position:relative;margin-top:9px}
.fgbar i{position:absolute;top:-4.5px;width:15px;height:15px;border-radius:50%;background:#fff;border:3px solid #0a0f1c;transform:translateX(-50%);box-shadow:0 2px 7px rgba(0,0,0,.5)}
.lr{display:flex;align-items:center;gap:9px;padding:6px 0}
.lr .rk{font-size:10.5px;color:var(--mut2);width:14px}.lr .tk{font-weight:600;width:58px;font-size:12.5px}
.lr .bar{flex:1;height:5px;background:rgba(255,255,255,.06);border-radius:999px;position:relative}
.lr .bar b{position:absolute;top:0;height:100%;border-radius:999px}
.lr .sc{width:48px;text-align:right;font-size:11.5px;color:var(--mut)}
.strip{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
@media(max-width:840px){.strip{grid-template-columns:repeat(2,1fr)}}
.strip .c .k{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.strip .c .v{font-size:18px;font-weight:700;margin-top:4px}
.cw{position:relative;margin-top:6px}
.tip{position:absolute;pointer-events:none;background:rgba(8,13,26,.95);border:1px solid rgba(255,255,255,.16);
 border-radius:9px;padding:6px 10px;font-size:11.5px;opacity:0;transition:opacity .1s;white-space:nowrap;transform:translate(-50%,-135%);z-index:2}
.lg{display:flex;gap:16px;font-size:11px;color:var(--mut);margin-top:8px}
.lg i{display:inline-block;width:9px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.foot{text-align:center;color:var(--mut2);font-size:11px;margin-top:2px}
.foot a{color:var(--b);text-decoration:none}
</style></head>
<body><div class="wrap">

<div class="head">
  <div class="title">
    <img class="logo" src="logo.png" alt="CTA" onerror="this.outerHTML='🤖'"/>
    <span class="nm"><b>CTA</b><small>CMC · TWAK · Agent</small></span>
    <span class="badge"><i></i><span id="mode"></span></span>
    <span class="beat" id="beat"></span></div>
  <div class="chips" id="chips"></div>
</div>

<div class="card">
  <div class="lab">Portfolio · on-chain</div>
  <div class="prow">
    <div><div class="big num" id="pv">—</div><div class="sub" id="pvsub"></div></div>
    <div id="holds" class="holds"></div>
  </div>
</div>

<div class="card">
  <div class="ph">Strategy track record <span id="trk">1y backtest · real prices</span></div>
  <div class="trgrid">
    <div>
      <div class="ret num" id="ret"></div>
      <div class="sub" id="vsmkt"></div>
      <div class="cmp" id="cmp"></div>
    </div>
    <div class="trstats">
      <div class="m"><span class="k">Max drawdown</span><span class="x pos num" id="dd"></span></div>
      <div class="m"><span class="k">DQ headroom</span><span class="x acc num" id="hr"></span></div>
      <div class="m"><span class="k">Backtest trades</span><span class="x num" id="tr"></span></div>
    </div>
  </div>
</div>

<div class="card" id="mcard">
  <div class="ph">Live market read <span>CMC Agent Hub</span></div>
  <div id="market"></div>
</div>

<div class="card">
  <div class="ph"><span id="clab"></span><span id="cmeta"></span></div>
  <div class="cw" id="cw"><div class="tip" id="tip"></div></div>
  <div class="lg" id="lg"></div>
</div>

<div class="card">
  <div class="ph">Momentum leaderboard <span>live · top movers now</span></div>
  <div id="lead" class="leadgrid"></div>
</div>

<div class="card">
  <div class="ph">Recent activity · decision log <span><a href="decisions.jsonl" style="color:var(--b);text-decoration:none">raw log ↓</a></span></div>
  <div id="activity"></div>
</div>

<div class="card strip">
  <div class="c"><div class="k">Strategy</div><div class="v acc" id="pol"></div></div>
  <div class="c"><div class="k">Per-position stop</div><div class="v num" id="stop"></div></div>
  <div class="c"><div class="k">Kill switch</div><div class="v num" id="kill"></div></div>
  <div class="c"><div class="k">Eligible tokens</div><div class="v num" id="toks"></div></div>
  <div class="c"><div class="k">Blocked by rules</div><div class="v num" id="blk"></div></div>
</div>

<div class="foot">ERC-8004 agent <b id="aid"></b> · <span id="addr"></span> ·
 <a href="https://github.com/DanMarteens/cmc-twak-trading-agent" target="_blank">source</a> · #CMCAgentHub</div>
</div>
<script>
const D=/*DATA*/, $=i=>document.getElementById(i);
const REG={trend_up:['#2fd27e','rgba(47,210,126,.14)','uptrend'],trend_down:['#ff6b78','rgba(255,107,120,.14)','downtrend'],chop:['#ffb547','rgba(255,181,71,.14)','chop']};
$('mode').textContent={live:'LIVE',paper:'PAPER · real signals',dry_run:'ARMED'}[D.mode]||'ARMED';
const ago=Math.max(0,Math.round(Date.now()/1000-D.generated_ts));
$('beat').textContent='updated '+(ago<90?ago+'s':Math.round(ago/60)+'m')+' ago';
$('chips').innerHTML=[`<span class="chip on">🟢 registered on-chain</span>`,
 `<span class="chip">ERC-8004 <b>#${D.agent_id}</b></span>`,
 `<span class="chip"><b>${D.track.tokens}</b> eligible tokens</span>`].join('');

const pv=D.portfolio.total_usd;
if(pv!=null){const t0=performance.now();(function a(n){let p=Math.min((n-t0)/750,1);p=1-Math.pow(1-p,3);
 $('pv').textContent='$'+(pv*p).toFixed(2);if(p<1)requestAnimationFrame(a);})(t0);}else $('pv').textContent='—';
$('pvsub').textContent=D.portfolio.holdings.length?'across '+D.portfolio.holdings.length+' assets':'fund wallet to begin';
$('holds').innerHTML=D.portfolio.holdings.map(h=>`<div class="hchip">
 <img class="ico" src="${h.logo}" onerror="this.outerHTML='<i class=dotk></i>'"/>${h.sym}
 <span class="ha num">${h.amount} · $${h.usd.toFixed(2)}</span></div>`).join('');

const t=D.track,edge=(t.return_pct-t.buyhold_pct);
$('ret').textContent=(t.return_pct>=0?'+':'')+t.return_pct+'%';$('ret').className='ret num '+(t.return_pct>=0?'pos':'neg');
$('vsmkt').innerHTML=`<b class="pos">+${edge.toFixed(0)} pts</b> better than holding the same tokens`;
$('dd').textContent=t.maxdd_pct+'%';$('hr').textContent=(t.dq_pct-t.maxdd_pct).toFixed(0)+'%';$('tr').textContent=t.trades;
(function(){const ar=Math.abs(t.return_pct),mr=Math.abs(t.buyhold_pct),mx=Math.max(ar,mr,1);
 $('cmp').innerHTML=`
  <div class="cmprow"><span class="cl">Agent</span><span class="cbar"><b style="width:${(ar/mx*100).toFixed(0)}%;background:${t.return_pct>=0?'var(--g)':'var(--r)'}"></b></span><span class="cv ${t.return_pct>=0?'pos':'neg'}">${(t.return_pct>=0?'+':'')+t.return_pct}%</span></div>
  <div class="cmprow"><span class="cl">Market</span><span class="cbar"><b style="width:${(mr/mx*100).toFixed(0)}%;background:var(--r);opacity:.55"></b></span><span class="cv neg">${t.buyhold_pct}%</span></div>`;
})();
$('pol').textContent=D.risk.policy;$('stop').textContent=D.risk.stop+'%';$('kill').textContent=D.risk.kill+'%';
$('toks').textContent=D.track.tokens;$('blk').textContent=D.blocked;
$('aid').textContent='#'+D.agent_id;$('addr').textContent=D.address.slice(0,6)+'…'+D.address.slice(-4);

const mk=D.market;
if(mk){const[col,bg,nm]=REG[mk.regime]||REG.chop;
 const fl=mk.fg<25?'Extreme fear':mk.fg<45?'Fear':mk.fg<55?'Neutral':mk.fg<75?'Greed':'Extreme greed';
 $('market').innerHTML=`<div class="mkt">
   <span class="regime" style="color:${col};background:${bg}">${nm}</span>
   <div class="it fg"><div class="lab">Fear &amp; Greed — <b style="color:${col}">${mk.fg} · ${fl}</b></div>
     <div class="fgbar"><i style="left:${mk.fg}%"></i></div></div>
   <div class="it cell"><div class="lab">BTC dominance</div><div class="mkv num">${mk.dom}%</div></div>
   <div class="it cell"><div class="lab">Funding (perps)</div><div class="mkv num" style="color:${mk.funding>=0?'var(--g)':'var(--r)'}">${mk.funding>=0?'+':''}${mk.funding}%</div></div>
   <div class="it cell"><div class="lab">Bullish now</div><div class="mkv num"><b class="pos">${mk.bullish}</b> / ${mk.total}</div></div>
  </div>`;
 $('lead').innerHTML=mk.leaderboard.map((l,i)=>{const w=Math.min(50,Math.abs(l.score)*50),p=l.score>=0;
   return `<div class="lr"><span class="rk">${i+1}</span>
   <img class="ico sm" src="${l.logo}" onerror="this.style.visibility='hidden'"/><span class="tk">${l.sym}</span>
   <span class="bar"><b style="${p?'left:50%':'right:50%'};width:${w}%;background:${p?'var(--g)':'var(--r)'}"></b></span>
   <span class="sc num">${l.score>=0?'+':''}${l.score.toFixed(2)}</span></div>`;}).join('');
}else $('mcard').style.display='none';

// recent activity / decision log
$('activity').innerHTML=((D.activity&&D.activity.length)?D.activity:[]).map(a=>{
 const col=a.kind==='fill'?'var(--g)':a.kind==='blocked'?'var(--mut)':a.kind==='x402'?'var(--b)':'var(--r)';
 const ex=a.kind==='x402'?'https://basescan.org/tx/':'https://bscscan.com/tx/';
 const link=(a.tx&&(''+a.tx).startsWith('0x'))?` · <a href="${ex}${a.tx}" target="_blank">tx ↗</a>`:'';
 const label=a.kind==='fill'?((a.action||'').toUpperCase()+' '+a.token):a.kind==='blocked'?('blocked '+a.token):a.kind==='x402'?'x402 paid':a.kind.replace('_',' ');
 return `<div class="act"><span class="kd" style="color:${col}">${label}</span><span class="rs">${a.reason}${link}</span><span class="tm">${a.ts}</span></div>`;
}).join('')||'<div class="rs" style="color:var(--mut2);font-size:12.5px">Holding cash in the downtrend. Maintenance trade keeps the daily minimum; rotations resume when the market turns up.</div>';

// ---- chart (redesigned: y-axis ticks, baseline, perf-colored, hover pill) ----
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const fmt=s=>{if(!s)return'';const d=s.split('-');return MON[(+d[1]||1)-1]+' '+(+d[2]||'');};
(function(){const c=D.chart,N=c.equity.length;if(N<2)return;
 const W=900,H=300,L=14,R=54,T=18,B=28;
 const hasB=c.benchmark&&c.benchmark.length;const all=c.equity.concat(hasB?c.benchmark:[]);
 let mn=Math.min(...all),mx=Math.max(...all);const pad=(mx-mn)*.12||1;mn-=pad;mx+=pad;
 const X=i=>L+i*(W-L-R)/(N-1),Y=v=>T+(1-(v-mn)/(mx-mn))*(H-T-B);
 const up=c.equity[N-1]>=c.equity[0],col=up?'#2fd27e':'#ff6b78';
 function sm(pts){let d='M'+pts[0][0].toFixed(1)+' '+pts[0][1].toFixed(1);
  for(let i=0;i<pts.length-1;i++){const a=pts[i-1]||pts[i],b=pts[i],e=pts[i+1],f=pts[i+2]||e;
  d+=`C${(b[0]+(e[0]-a[0])/6).toFixed(1)} ${(b[1]+(e[1]-a[1])/6).toFixed(1)},${(e[0]-(f[0]-b[0])/6).toFixed(1)} ${(e[1]-(f[1]-b[1])/6).toFixed(1)},${e[0].toFixed(1)} ${e[1].toFixed(1)}`;}return d;}
 const eP=c.equity.map((v,i)=>[X(i),Y(v)]),eD=sm(eP);
 const ydec=(mx-mn)<2?3:(mx-mn)<20?2:0;
 let ticks='';for(let k=0;k<=3;k++){const v=mn+(mx-mn)*k/3,y=Y(v);
  ticks+=`<line x1="${L}" y1="${y.toFixed(1)}" x2="${W-R}" y2="${y.toFixed(1)}" stroke="rgba(255,255,255,.05)"/>
  <text x="${W-R+8}" y="${(y+3).toFixed(1)}" fill="var(--mut2)" font-size="10">$${v.toFixed(ydec)}</text>`;}
 const base=Y(c.equity[0]);
 $('clab').textContent=c.label;$('cmeta').textContent=N+' points';
 $('lg').innerHTML=`<span><i style="background:${col}"></i>Agent</span>`+(hasB?`<span><i style="background:var(--r)"></i>Market (buy&amp;hold)</span>`:'')+`<span><i style="background:var(--mut2)"></i>start</span>`;
 $('cw').insertAdjacentHTML('afterbegin',`<svg id="svg" viewBox="0 0 ${W} ${H}" width="100%" style="display:block">
  <defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".30"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient>
  <filter id="gl"><feGaussianBlur stdDeviation="2.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
  ${ticks}
  <line x1="${L}" y1="${base.toFixed(1)}" x2="${W-R}" y2="${base.toFixed(1)}" stroke="var(--mut2)" stroke-width="1" stroke-dasharray="2 4" opacity=".7"/>
  <path d="${eD} L${X(N-1).toFixed(1)} ${H-B} L${L} ${H-B} Z" fill="url(#ag)"/>
  ${hasB?`<path d="${sm(c.benchmark.map((v,i)=>[X(i),Y(v)]))}" fill="none" stroke="var(--r)" stroke-width="1.7" stroke-dasharray="5 5" opacity=".55"/>`:''}
  <path d="${eD}" fill="none" stroke="${col}" stroke-width="2.6" stroke-linejoin="round" filter="url(#gl)"
    pathLength="1" style="stroke-dasharray:1;stroke-dashoffset:1;animation:dr 1.5s ease forwards"/>
  <style>@keyframes dr{to{stroke-dashoffset:0}}</style>
  <line id="cx" x1="0" y1="${T}" x2="0" y2="${H-B}" stroke="rgba(255,255,255,.28)" stroke-width="1" opacity="0"/>
  <circle id="cd" r="4" fill="${col}" stroke="#0a0f1c" stroke-width="2" opacity="0"/>
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
