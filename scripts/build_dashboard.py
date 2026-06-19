"""
Build the public dashboard (dashboard/index.html) — glassmorphism, real data.

  * Hero  = REAL on-chain portfolio (live wallet balances via twak).
  * Market = LIVE read: regime + Fear&Greed + BTC dominance + momentum leaderboard
            (the agent's actual market view right now — proves CMC+strategy live).
  * Track  = strategy backtest on real prices (labeled). Chart -> live equity when live.

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
        holdings.append({"sym": "USDT", "amount": round(float(u["available"]), 2), "usd": round(usd, 2)})
        total += usd
    b = _twak(["wallet", "balance", "--chain", "bsc"])
    if b and "available" in b:
        usd = float(b.get("totalUsd", 0) or 0)
        holdings.append({"sym": "BNB", "amount": round(float(b["available"]), 5), "usd": round(usd, 2)})
        total += usd
    try:
        st = json.load(open(os.path.join(ROOT, cfg["paths"]["state_file"])))
        for sym in st.get("positions", {}):
            at = cfg["twak"]["token_contracts"].get(sym)
            r = _twak(["balance", "--address", addr, "--token", at, "--chain", "bsc"]) if at else None
            if r and float(r.get("available", 0) or 0) > 0:
                usd = float(r.get("totalUsd", 0) or 0)
                holdings.append({"sym": sym, "amount": round(float(r["available"]), 4), "usd": round(usd, 2)})
                total += usd
    except Exception:
        pass
    return {"total_usd": round(total, 2), "holdings": holdings}


def _market(cfg):
    """Live market read: regime, Fear&Greed, BTC dominance, momentum leaderboard."""
    try:
        from agent.signal_source import TwakCmcSignalClient
        from agent.signal_engine import score_universe, detect_regime
        client = TwakCmcSignalClient(cfg)
        snap = client.get_snapshot(cfg["whitelist"])
        if not snap:
            return None
        regime = detect_regime(snap, cfg).value
        sigs = score_universe(snap, cfg)
        any_d = next(iter(snap.values()))
        ranked = sorted(sigs.values(), key=lambda s: s.score, reverse=True)
        top = [{"sym": s.token, "score": round(s.score, 3), "regime": s.regime.value}
               for s in ranked[:8]]
        return {"regime": regime, "fg": round(float(any_d.get("fear_greed_index", 50))),
                "dom": round(float(any_d.get("btc_dominance", 54)), 1), "leaderboard": top}
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
    live = cfg.get("mode") == "live"
    if live:
        st = json.load(open(os.path.join(ROOT, cfg["paths"]["state_file"])))
        curve = st.get("equity_curve", [])
        chart = {"dates": [c[0][:10] for c in curve], "equity": [round(c[1], 4) for c in curve],
                 "benchmark": [], "label": "Live equity"}
    else:
        chart = {"dates": bt["dates"], "equity": bt["equity"], "benchmark": bt["benchmark"],
                 "label": "Strategy backtest · 1y real prices"}
    return {
        "address": cfg["twak"]["agent_address"], "agent_id": cfg.get("bnb_sdk", {}).get("agent_id", ""),
        "live": live, "generated_ts": int(time.time()),
        "portfolio": _wallet(cfg) if with_wallet else {"total_usd": None, "holdings": []},
        "market": _market(cfg) if with_market else None,
        "track": {"return_pct": bt["kpis"]["total_return_pct"], "buyhold_pct": bt["kpis"]["buyhold_pct"],
                  "maxdd_pct": bt["kpis"]["max_drawdown_pct"], "dq_pct": bt["kpis"]["dq_pct"],
                  "trades": bt["kpis"]["trades"], "tokens": len(cfg["twak"]["token_contracts"])},
        "chart": chart,
        "risk": {"kill": cfg["risk"]["drawdown_kill_pct"] * 100,
                 "stop": cfg["risk"]["per_position_stop_pct"] * 100, "policy": cfg["decision"]["policy"]},
        "blocked": len([r for r in rows if r.get("kind") == "blocked"]),
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
    m = data.get("market")
    print(f"-> dashboard (portfolio ${data['portfolio']['total_usd']}, "
          f"market={'live '+m['regime'] if m else 'n/a'}, {'live' if data['live'] else 'backtest'} chart)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta http-equiv="refresh" content="60"/>
<title>CMC-TWAK Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--g:#2fd27e;--r:#ff6b78;--b:#7ea6ff;--mut:#8b97bd;--mut2:#69749a}
body{min-height:100vh;font-family:'Inter',system-ui,sans-serif;color:#eaf0ff;letter-spacing:-.01em;
background:#060912;background-image:
 radial-gradient(1100px 600px at 10% -8%,rgba(77,141,255,.20),transparent 60%),
 radial-gradient(900px 600px at 98% -5%,rgba(155,140,255,.16),transparent 55%),
 radial-gradient(800px 700px at 55% 115%,rgba(47,210,126,.12),transparent 55%);
background-attachment:fixed;padding:30px 22px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto}
.head{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:24px}
.title{font-size:19px;font-weight:700;display:flex;align-items:center;gap:11px}
.live{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;font-weight:600;color:var(--g);
background:rgba(47,210,126,.1);border:1px solid rgba(47,210,126,.28);padding:5px 11px;border-radius:999px}
.live i{width:7px;height:7px;border-radius:50%;background:var(--g);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,210,126,.5)}70%{box-shadow:0 0 0 8px rgba(47,210,126,0)}100%{box-shadow:0 0 0 0 rgba(47,210,126,0)}}
.chips{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.chip{font-size:11.5px;font-weight:550;color:var(--mut);background:rgba(255,255,255,.045);
border:1px solid rgba(255,255,255,.09);padding:6px 11px;border-radius:999px}
.chip b{color:#eaf0ff}
.beat{font-size:11.5px;color:var(--mut2)}
.glass{background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.022));
border:1px solid rgba(255,255,255,.09);border-radius:22px;backdrop-filter:blur(20px);
-webkit-backdrop-filter:blur(20px);box-shadow:0 10px 50px rgba(0,0,0,.35);transition:transform .3s,border-color .3s}
.glass:hover{transform:translateY(-2px);border-color:rgba(255,255,255,.17)}
.lab{font-size:11px;letter-spacing:.7px;text-transform:uppercase;color:var(--mut);font-weight:600}
.grid{display:grid;grid-template-columns:1.15fr 1fr;gap:15px;margin-bottom:15px}
.hero{padding:25px 27px}
.big{font-size:50px;font-weight:800;letter-spacing:-1.5px;margin:7px 0 2px;line-height:1;
background:linear-gradient(92deg,#fff,#b9ccff);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--mut);font-size:13px}
.hold{margin-top:17px;display:flex;flex-direction:column;gap:7px}
.h-row{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-radius:13px;
background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.06)}
.h-row .s{font-weight:600;display:flex;align-items:center;gap:8px}
.h-row .dotk{width:7px;height:7px;border-radius:50%;background:var(--b)}
.h-row .a{color:var(--mut);font-size:12.5px;font-variant-numeric:tabular-nums}
.stat{padding:23px 25px;display:flex;flex-direction:column}
.v{font-size:36px;font-weight:800;letter-spacing:-.6px;margin-top:7px;font-variant-numeric:tabular-nums}
.pos{color:var(--g)}.neg{color:var(--r)}.acc{color:var(--b)}
.mini{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin-top:15px}
.m{background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:13px 15px}
.m .k{font-size:10.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px}
.m .x{font-size:21px;font-weight:700;margin-top:5px;font-variant-numeric:tabular-nums}
.row{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px}
.panel{padding:21px 24px}
.ph{font-size:12.5px;font-weight:600;color:#cdd7f5;margin-bottom:15px;display:flex;justify-content:space-between;align-items:center}
.ph span{color:var(--mut);font-weight:500;font-size:11px}
.regime{font-size:13px;font-weight:700;padding:5px 12px;border-radius:999px}
.fgwrap{margin:6px 0 16px}
.fgbar{height:8px;border-radius:999px;background:linear-gradient(90deg,#ff5d6c,#ffb547,#2fd27e);position:relative;margin-top:9px}
.fgbar i{position:absolute;top:-4px;width:16px;height:16px;border-radius:50%;background:#fff;border:3px solid #0b1020;transform:translateX(-50%);box-shadow:0 2px 8px rgba(0,0,0,.5)}
.lead .lr{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.05)}
.lead .lr:last-child{border:0}
.lead .rank{font-size:11px;color:var(--mut2);width:16px}
.lead .tk{font-weight:600;width:62px;font-size:13px}
.lead .bar{flex:1;height:6px;background:rgba(255,255,255,.06);border-radius:999px;position:relative;overflow:hidden}
.lead .bar>b{position:absolute;left:50%;top:0;height:100%;border-radius:999px}
.lead .sc{width:52px;text-align:right;font-size:12px;font-variant-numeric:tabular-nums;color:var(--mut)}
.chartwrap{position:relative}
.tip{position:absolute;pointer-events:none;background:rgba(10,15,28,.92);border:1px solid rgba(255,255,255,.14);
border-radius:9px;padding:6px 9px;font-size:11.5px;opacity:0;transition:opacity .12s;white-space:nowrap;transform:translate(-50%,-130%)}
.foot{margin-top:16px;text-align:center;color:var(--mut2);font-size:11.5px}
.foot a{color:var(--b);text-decoration:none}
@media(max-width:860px){.grid,.row{grid-template-columns:1fr}.big{font-size:40px}}
</style></head>
<body><div class="wrap">
<div class="head">
  <div class="title">🤖 CMC-TWAK Agent
    <span class="live"><i></i><span id="mode">DRY-RUN</span></span>
    <span class="beat" id="beat"></span></div>
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
    <div class="lab">Strategy track record · <span id="trk">1y backtest</span></div>
    <div class="v" id="ret"></div>
    <div class="sub" id="vsmkt"></div>
    <div class="mini">
      <div class="m"><div class="k">Max drawdown</div><div class="x pos" id="dd"></div></div>
      <div class="m"><div class="k">DQ headroom</div><div class="x acc" id="hr"></div></div>
    </div>
  </div>
</div>

<div class="row" id="marketrow">
  <div class="glass panel">
    <div class="ph">Live market read <span>CMC Agent Hub</span></div>
    <div id="market"></div>
  </div>
  <div class="glass panel">
    <div class="ph">Momentum leaderboard <span>now</span></div>
    <div class="lead" id="lead"></div>
  </div>
</div>

<div class="row" style="grid-template-columns:1.7fr 1fr">
  <div class="glass panel">
    <div class="ph"><span id="chartlab"></span><span id="chartmeta"></span></div>
    <div class="chartwrap" id="chartwrap"><div class="tip" id="tip"></div></div>
  </div>
  <div class="glass panel">
    <div class="ph">Risk engine <span>armed</span></div>
    <div class="mini" style="grid-template-columns:1fr 1fr">
      <div class="m"><div class="k">Strategy</div><div class="x acc" id="pol" style="font-size:16px"></div></div>
      <div class="m"><div class="k">Per-pos stop</div><div class="x" id="stop" style="font-size:18px"></div></div>
      <div class="m"><div class="k">Kill switch</div><div class="x" id="kill" style="font-size:18px"></div></div>
      <div class="m"><div class="k">Blocked</div><div class="x" id="blk" style="font-size:18px"></div></div>
    </div>
  </div>
</div>

<div class="foot">ERC-8004 agent <b id="aid"></b> · <span id="addr"></span> ·
  <a href="https://github.com/DanMarteens/cmc-twak-trading-agent" target="_blank">source</a> · #CMCAgentHub</div>
</div>
<script>
const D=/*DATA*/, $=i=>document.getElementById(i);
const REGCOL={trend_up:['#2fd27e','rgba(47,210,126,.14)'],trend_down:['#ff6b78','rgba(255,107,120,.14)'],chop:['#ffb547','rgba(255,181,71,.14)']};
$('mode').textContent=D.live?'LIVE':'ARMED · DRY-RUN';
const ago=Math.max(0,Math.round(Date.now()/1000-D.generated_ts));
$('beat').textContent='updated '+(ago<90?ago+'s':Math.round(ago/60)+'m')+' ago';
$('chips').innerHTML=[`<span class="chip">🟢 registered</span>`,`<span class="chip"><b>${D.track.tokens}</b> tokens</span>`,
 `<span class="chip">CMC Agent Hub</span>`,`<span class="chip">TWAK · BSC</span>`].join('');

const pv=D.portfolio.total_usd;
if(pv!=null){const t=performance.now();(function a(n){let p=Math.min((n-t)/750,1);p=1-Math.pow(1-p,3);
 $('pv').textContent='$'+(pv*p).toFixed(2);if(p<1)requestAnimationFrame(a);})(t);}else $('pv').textContent='—';
$('pvsub').textContent=D.portfolio.holdings.length?'across '+D.portfolio.holdings.length+' assets':'fund wallet to begin';
$('holds').innerHTML=D.portfolio.holdings.map(h=>`<div class="h-row"><span class="s"><i class="dotk"></i>${h.sym}</span>
 <span class="a">${h.amount} · $${h.usd.toFixed(2)}</span></div>`).join('');

const t=D.track,edge=(t.return_pct-t.buyhold_pct);
$('ret').textContent=(t.return_pct>=0?'+':'')+t.return_pct+'%';$('ret').className='v '+(t.return_pct>=0?'pos':'neg');
$('vsmkt').innerHTML=`vs market <b style="color:var(--r)">${t.buyhold_pct}%</b> · <b style="color:var(--g)">+${edge.toFixed(0)} pts edge</b>`;
$('dd').textContent=t.maxdd_pct+'%';$('hr').textContent=(t.dq_pct-t.maxdd_pct).toFixed(0)+'%';
$('pol').textContent=D.risk.policy;$('stop').textContent=D.risk.stop+'%';$('kill').textContent=D.risk.kill+'%';$('blk').textContent=D.blocked;
$('aid').textContent='#'+D.agent_id;$('addr').textContent=D.address.slice(0,6)+'…'+D.address.slice(-4);

// live market read
const mk=D.market;
if(mk){const[col,bg]=REGCOL[mk.regime]||REGCOL.chop;
 const fgLabel=mk.fg<25?'Extreme fear':mk.fg<45?'Fear':mk.fg<55?'Neutral':mk.fg<75?'Greed':'Extreme greed';
 $('market').innerHTML=`<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
   <span class="regime" style="color:${col};background:${bg}">${mk.regime.replace('_',' ')}</span>
   <span class="sub">BTC dominance <b style="color:#eaf0ff">${mk.dom}%</b></span></div>
  <div class="fgwrap"><div class="lab">Fear &amp; Greed — <b style="color:${col}">${mk.fg} ${fgLabel}</b></div>
   <div class="fgbar"><i style="left:${mk.fg}%"></i></div></div>`;
 $('lead').innerHTML=mk.leaderboard.map((l,i)=>{const w=Math.min(50,Math.abs(l.score)*50);
   const pos=l.score>=0;return `<div class="lr"><span class="rank">${i+1}</span><span class="tk">${l.sym}</span>
   <span class="bar"><b style="${pos?'left:50%':'right:50%;left:auto'};width:${w}%;background:${pos?'var(--g)':'var(--r)'}"></b></span>
   <span class="sc">${l.score>=0?'+':''}${l.score.toFixed(2)}</span></div>`;}).join('');
}else{$('marketrow').style.display='none';}

// chart — smoothed line + crosshair tooltip
(function(){const c=D.chart,W=640,H=240,p=26,N=c.equity.length;if(N<2)return;
 const hasB=c.benchmark&&c.benchmark.length;const all=c.equity.concat(hasB?c.benchmark:[]);
 let mn=Math.min(...all),mx=Math.max(...all);const pd=(mx-mn)*.12||1;mn-=pd;mx+=pd;
 const X=i=>p+i*(W-2*p)/(N-1),Y=v=>H-p-(v-mn)/(mx-mn)*(H-2*p);
 function smooth(pts){if(pts.length<3)return pts.map((q,i)=>(i?'L':'M')+q[0]+' '+q[1]).join(' ');
  let d='M'+pts[0][0]+' '+pts[0][1];for(let i=0;i<pts.length-1;i++){const p0=pts[i-1]||pts[i],p1=pts[i],p2=pts[i+1],p3=pts[i+2]||p2;
  const c1x=p1[0]+(p2[0]-p0[0])/6,c1y=p1[1]+(p2[1]-p0[1])/6,c2x=p2[0]-(p3[0]-p1[0])/6,c2y=p2[1]-(p3[1]-p1[1])/6;
  d+=`C${c1x.toFixed(1)} ${c1y.toFixed(1)},${c2x.toFixed(1)} ${c2y.toFixed(1)},${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;}return d;}
 const ePts=c.equity.map((v,i)=>[X(i),Y(v)]),bPts=hasB?c.benchmark.map((v,i)=>[X(i),Y(v)]):[];
 const eD=smooth(ePts);let grid='';for(let g=0;g<=3;g++){const y=p+g*(H-2*p)/3;grid+=`<line x1="${p}" y1="${y}" x2="${W-p}" y2="${y}" stroke="rgba(255,255,255,.05)"/>`;}
 $('chartlab').textContent=c.label;$('chartmeta').textContent=N+' pts';
 $('chartwrap').insertAdjacentHTML('afterbegin',`<svg id="svg" viewBox="0 0 ${W} ${H}" width="100%" style="display:block">
  <defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2fd27e" stop-opacity=".32"/><stop offset="1" stop-color="#2fd27e" stop-opacity="0"/></linearGradient>
  <filter id="glow"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
  ${grid}
  <path d="${eD} L${X(N-1)} ${H-p} L${X(0)} ${H-p} Z" fill="url(#ag)"/>
  ${hasB?`<path d="${smooth(bPts)}" fill="none" stroke="var(--r)" stroke-width="1.8" stroke-dasharray="5 5" opacity=".6"/>`:''}
  <path d="${eD}" fill="none" stroke="#2fd27e" stroke-width="2.6" stroke-linejoin="round" filter="url(#glow)"
    pathLength="1" style="stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.5s ease forwards"/>
  <style>@keyframes draw{to{stroke-dashoffset:0}}</style>
  <line id="cx" x1="0" y1="${p}" x2="0" y2="${H-p}" stroke="rgba(255,255,255,.25)" stroke-width="1" opacity="0"/>
  <circle id="cd" r="4" fill="#2fd27e" opacity="0"/>
  <text x="${p}" y="${H-6}" fill="var(--mut2)" font-size="10">${c.dates[0]||''}</text>
  <text x="${W-p}" y="${H-6}" fill="var(--mut2)" font-size="10" text-anchor="end">${c.dates[c.dates.length-1]||''}</text></svg>`);
 const svg=$('svg'),tip=$('tip'),cx=$('cx'),cd=$('cd');
 svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect();const sx=(e.clientX-r.left)/r.width*W;
  let i=Math.round((sx-p)/((W-2*p)/(N-1)));i=Math.max(0,Math.min(N-1,i));
  const x=X(i),y=Y(c.equity[i]);cx.setAttribute('x1',x);cx.setAttribute('x2',x);cx.setAttribute('opacity','1');
  cd.setAttribute('cx',x);cd.setAttribute('cy',y);cd.setAttribute('opacity','1');
  tip.style.opacity=1;tip.style.left=(x/W*100)+'%';tip.style.top=(y/H*100)+'%';
  tip.innerHTML=`<b>$${c.equity[i].toFixed(2)}</b> · ${c.dates[i]||''}`;});
 svg.addEventListener('mouseleave',()=>{tip.style.opacity=0;cx.setAttribute('opacity','0');cd.setAttribute('opacity','0');});
})();
</script></body></html>"""


if __name__ == "__main__":
    main()
