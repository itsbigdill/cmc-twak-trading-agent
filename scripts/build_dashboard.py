"""
Build the public dashboard (dashboard/index.html) — glassmorphism, real data.

  * Hero = REAL on-chain portfolio (live wallet balances via twak).
  * Track record = the strategy backtest on real prices (labeled).
  * Once live (mode: live), the equity chart uses the agent's live state.

    python scripts/build_dashboard.py            # auto
    python scripts/build_dashboard.py --no-wallet # skip on-chain calls (offline)
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USDT = "0x55d398326f99059fF775485246999027B3197955"


def _twak(args):
    try:
        out = subprocess.run(["twak", *args, "--json"], capture_output=True, text=True, timeout=40,
                             cwd=ROOT)
        return json.loads(out.stdout[out.stdout.find("{"):])
    except Exception:
        return None


def _wallet(cfg):
    """Real on-chain holdings: USDT + BNB + any tokens the agent currently holds."""
    addr = cfg["twak"]["agent_address"]
    holdings, total = [], 0.0
    u = _twak(["balance", "--address", addr, "--token", USDT, "--chain", "bsc"])
    if u and "available" in u:
        usd = float(u.get("totalUsd", 0) or 0)
        holdings.append({"sym": "USDT", "amount": round(float(u["available"]), 2), "usd": round(usd, 2)})
        total += usd
    b = _twak(["wallet", "balance", "--chain", "bsc"])
    if b and "available" in b:
        usd = float(b.get("totalUsd", 0) or 0)
        holdings.append({"sym": "BNB", "amount": round(float(b["available"]), 5), "usd": round(usd, 2)})
        total += usd
    # any non-cash positions the agent is holding (from its state)
    try:
        with open(os.path.join(ROOT, cfg["paths"]["state_file"])) as f:
            st = json.load(f)
        for sym in st.get("positions", {}):
            addr_t = cfg["twak"]["token_contracts"].get(sym)
            if not addr_t:
                continue
            r = _twak(["balance", "--address", addr, "--token", addr_t, "--chain", "bsc"])
            if r and float(r.get("available", 0) or 0) > 0:
                usd = float(r.get("totalUsd", 0) or 0)
                holdings.append({"sym": sym, "amount": round(float(r["available"]), 4), "usd": round(usd, 2)})
                total += usd
    except Exception:
        pass
    return {"total_usd": round(total, 2), "holdings": holdings}


def build_data(with_wallet=True):
    from agent.agent import load_config
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    bt = json.load(open(os.path.join(ROOT, "logs", "backtest_result.json")))
    rows = []
    try:
        for line in open(os.path.join(ROOT, cfg["paths"]["decision_log"])):
            rows.append(json.loads(line))
    except Exception:
        pass
    live = cfg.get("mode") == "live"

    # chart: live equity once trading, else the backtest track record
    if live:
        st = json.load(open(os.path.join(ROOT, cfg["paths"]["state_file"])))
        curve = st.get("equity_curve", [])
        chart = {"dates": [c[0][:10] for c in curve], "equity": [round(c[1], 4) for c in curve],
                 "benchmark": [], "label": "Live equity"}
    else:
        chart = {"dates": bt["dates"], "equity": bt["equity"], "benchmark": bt["benchmark"],
                 "label": "Strategy backtest (1y, real prices)"}

    return {
        "address": cfg["twak"]["agent_address"],
        "agent_id": cfg.get("bnb_sdk", {}).get("agent_id", ""),
        "registered": True, "live": live,
        "portfolio": _wallet(cfg) if with_wallet else {"total_usd": None, "holdings": []},
        "track": {
            "return_pct": bt["kpis"]["total_return_pct"], "buyhold_pct": bt["kpis"]["buyhold_pct"],
            "maxdd_pct": bt["kpis"]["max_drawdown_pct"], "dq_pct": bt["kpis"]["dq_pct"],
            "trades": bt["kpis"]["trades"], "tokens": len(cfg["twak"]["token_contracts"]),
        },
        "chart": chart,
        "risk": {"kill": cfg["risk"]["drawdown_kill_pct"] * 100,
                 "stop": cfg["risk"]["per_position_stop_pct"] * 100,
                 "policy": cfg["decision"]["policy"]},
        "blocked": len([r for r in rows if r.get("kind") == "blocked"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--no-wallet", action="store_true")
    args = ap.parse_args()
    data = build_data(with_wallet=not args.no_wallet)
    os.makedirs(os.path.join(ROOT, "dashboard"), exist_ok=True)
    html = TEMPLATE.replace("/*DATA*/", json.dumps(data))
    out = os.path.join(ROOT, "dashboard", "index.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"-> {out} (portfolio ${data['portfolio']['total_usd']}, {'live' if data['live'] else 'backtest'} chart)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="60"/>
<title>CMC-TWAK Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;font-family:'Inter',system-ui,-apple-system,sans-serif;color:#eef2ff;
background:#070b18;background-image:
 radial-gradient(900px 500px at 12% -5%,rgba(77,141,255,.22),transparent 60%),
 radial-gradient(800px 500px at 95% 0%,rgba(155,140,255,.18),transparent 55%),
 radial-gradient(700px 600px at 60% 110%,rgba(47,210,126,.14),transparent 55%);
background-attachment:fixed;padding:32px 20px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto}
.head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:26px}
.title{font-size:20px;font-weight:650;letter-spacing:-.3px;display:flex;align-items:center;gap:10px}
.live{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:600;color:#2fd27e;
background:rgba(47,210,126,.12);border:1px solid rgba(47,210,126,.3);padding:6px 12px;border-radius:999px}
.live i{width:8px;height:8px;border-radius:50%;background:#2fd27e;box-shadow:0 0 0 0 rgba(47,210,126,.6);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,210,126,.5)}70%{box-shadow:0 0 0 9px rgba(47,210,126,0)}100%{box-shadow:0 0 0 0 rgba(47,210,126,0)}}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:12px;font-weight:550;color:#aab6d8;background:rgba(255,255,255,.05);
border:1px solid rgba(255,255,255,.1);padding:6px 12px;border-radius:999px;backdrop-filter:blur(8px)}
.chip b{color:#eef2ff}
.glass{background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.1);border-radius:20px;
backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);box-shadow:0 8px 40px rgba(0,0,0,.3);
transition:transform .25s,border-color .25s}
.glass:hover{transform:translateY(-3px);border-color:rgba(255,255,255,.2)}
.grid{display:grid;grid-template-columns:1.1fr 1fr;gap:16px;margin-bottom:16px}
.hero{padding:26px 28px}
.lab{font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:#8a97c0;font-weight:600}
.big{font-size:46px;font-weight:750;letter-spacing:-1px;margin:6px 0 2px;
background:linear-gradient(90deg,#fff,#bcd0ff);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:#8a97c0;font-size:13px}
.hold{margin-top:18px;display:flex;flex-direction:column;gap:8px}
.h-row{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-radius:12px;
background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06)}
.h-row .s{font-weight:600}.h-row .a{color:#aab6d8;font-size:13px;font-variant-numeric:tabular-nums}
.stat{padding:22px 24px;display:flex;flex-direction:column;justify-content:center}
.stat .v{font-size:34px;font-weight:750;letter-spacing:-.5px;margin-top:8px;font-variant-numeric:tabular-nums}
.pos{color:#2fd27e}.neg{color:#ff6b78}.accent{color:#7ea6ff}
.row2{display:grid;grid-template-columns:2fr 1fr;gap:16px}
.panel{padding:22px 24px}
.panel h2{font-size:13px;font-weight:600;color:#cdd7f5;margin-bottom:4px;display:flex;justify-content:space-between}
.panel h2 span{color:#8a97c0;font-weight:500}
.mini{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
.mini .m{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:14px 16px}
.mini .m .k{font-size:11px;color:#8a97c0;text-transform:uppercase;letter-spacing:.4px}
.mini .m .x{font-size:22px;font-weight:700;margin-top:5px;font-variant-numeric:tabular-nums}
.foot{margin-top:18px;text-align:center;color:#6b779c;font-size:12px}
.foot a{color:#7ea6ff;text-decoration:none}
@media(max-width:820px){.grid,.row2{grid-template-columns:1fr}.big{font-size:38px}}
</style></head>
<body><div class="wrap">
<div class="head">
  <div class="title">🤖 CMC-TWAK Agent
    <span class="live"><i></i><span id="mode">DRY-RUN</span></span></div>
  <div class="chips" id="chips"></div>
</div>

<div class="grid">
  <div class="glass hero">
    <div class="lab">Portfolio value · on-chain</div>
    <div class="big" id="pv">—</div>
    <div class="sub" id="pvsub"></div>
    <div class="hold" id="holds"></div>
  </div>
  <div class="glass stat">
    <div class="lab">Strategy track record <span class="sub" id="trk"></span></div>
    <div class="v pos" id="ret"></div>
    <div class="sub" id="vsmkt"></div>
    <div class="mini">
      <div class="m"><div class="k">Max drawdown</div><div class="x pos" id="dd"></div></div>
      <div class="m"><div class="k">DQ headroom</div><div class="x accent" id="hr"></div></div>
    </div>
  </div>
</div>

<div class="row2">
  <div class="glass panel">
    <h2><span id="chartlab"></span><span id="chartmeta"></span></h2>
    <div id="chart"></div>
  </div>
  <div class="glass panel">
    <h2>Risk engine <span>armed</span></h2>
    <div class="mini" style="grid-template-columns:1fr;gap:10px">
      <div class="m"><div class="k">Strategy</div><div class="x accent" id="pol" style="font-size:18px"></div></div>
      <div class="m"><div class="k">Per-position stop</div><div class="x" id="stop" style="font-size:18px"></div></div>
      <div class="m"><div class="k">Kill switch</div><div class="x" id="kill" style="font-size:18px"></div></div>
      <div class="m"><div class="k">Trades blocked by rules</div><div class="x" id="blk" style="font-size:18px"></div></div>
    </div>
  </div>
</div>

<div class="foot">
  ERC-8004 agent <b id="aid"></b> · <span id="addr"></span> ·
  <a href="https://github.com/DanMarteens/cmc-twak-trading-agent" target="_blank">source</a> ·
  #CMCAgentHub · auto-refresh 60s
</div>
</div>
<script>
const D=/*DATA*/;
const $=i=>document.getElementById(i);
$('mode').textContent=D.live?'LIVE':'ARMED · DRY-RUN';
$('chips').innerHTML=[`<span class="chip">🟢 registered</span>`,
  `<span class="chip"><b>${D.track.tokens}</b> tokens</span>`,
  `<span class="chip">CMC Agent Hub</span>`,`<span class="chip">TWAK · BSC</span>`].join('');

// hero portfolio (animated count-up)
const pv=D.portfolio.total_usd;
if(pv!=null){let s=0,t=performance.now();
 (function anim(n){let p=Math.min((n-t)/700,1);$('pv').textContent='$'+(pv*p).toFixed(2);if(p<1)requestAnimationFrame(anim);})(t);}
else $('pv').textContent='—';
$('pvsub').textContent=D.portfolio.holdings.length?'across '+D.portfolio.holdings.length+' assets':'fund wallet to begin';
$('holds').innerHTML=D.portfolio.holdings.map(h=>`<div class="h-row"><span class="s">${h.sym}</span>
 <span class="a">${h.amount} · $${h.usd.toFixed(2)}</span></div>`).join('');

// track record
const t=D.track,edge=(t.return_pct-t.buyhold_pct);
$('trk').textContent='1y backtest';
$('ret').textContent=(t.return_pct>=0?'+':'')+t.return_pct+'%';
$('ret').className='v '+(t.return_pct>=0?'pos':'neg');
$('vsmkt').innerHTML=`vs market <b style="color:#ff6b78">${t.buyhold_pct}%</b> · <span style="color:#2fd27e">+${edge.toFixed(0)} pts edge</span>`;
$('dd').textContent=t.maxdd_pct+'%';
$('hr').textContent=(t.dq_pct-t.maxdd_pct).toFixed(0)+'%';

// risk
$('pol').textContent=D.risk.policy;$('stop').textContent=D.risk.stop+'%';
$('kill').textContent=D.risk.kill+'%';$('blk').textContent=D.blocked;
$('aid').textContent='#'+D.agent_id;$('addr').textContent=D.address.slice(0,6)+'…'+D.address.slice(-4);

// chart (animated line draw)
(function(){const c=D.chart,W=620,H=230,p=24,N=c.equity.length;if(N<2){return;}
 const all=c.equity.concat(c.benchmark&&c.benchmark.length?c.benchmark:[]);
 let mn=Math.min(...all),mx=Math.max(...all);const pd=(mx-mn)*.1||1;mn-=pd;mx+=pd;
 const X=i=>p+i*(W-2*p)/(N-1),Y=v=>H-p-(v-mn)/(mx-mn)*(H-2*p);
 const pa=s=>s.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
 const bench=c.benchmark&&c.benchmark.length?`<path d="${pa(c.benchmark)}" fill="none" stroke="#ff6b78" stroke-width="2" stroke-dasharray="5 5" opacity=".7"/>`:'';
 $('chartlab').textContent=c.label;$('chartmeta').textContent=N+' pts';
 $('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" style="margin-top:8px">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
   <stop offset="0" stop-color="#2fd27e" stop-opacity=".3"/><stop offset="1" stop-color="#2fd27e" stop-opacity="0"/></linearGradient></defs>
  <path d="${pa(c.equity)} L${X(N-1)} ${H-p} L${X(0)} ${H-p} Z" fill="url(#g)"/>
  ${bench}
  <path d="${pa(c.equity)}" fill="none" stroke="#2fd27e" stroke-width="2.5" stroke-linejoin="round"
    pathLength="1" style="stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.4s ease forwards"/>
  <style>@keyframes draw{to{stroke-dashoffset:0}}</style>
  <text x="${p}" y="${H-5}" fill="#6b779c" font-size="10">${c.dates[0]||''}</text>
  <text x="${W-p}" y="${H-5}" fill="#6b779c" font-size="10" text-anchor="end">${c.dates[c.dates.length-1]||''}</text></svg>`;
})();
</script></body></html>"""


if __name__ == "__main__":
    main()
