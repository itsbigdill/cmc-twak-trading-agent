"""
Build a self-contained dashboard (dashboard/index.html) from backtest/live data.

Reads logs/backtest_result.json (produced by backtest.py) and embeds it into a
polished, standalone dark dashboard — open in a browser, screenshot for the
tweet, or show judges in the demo. No server, no deps.

    python scripts/backtest.py --policy rotation --universe core --period year
    python scripts/build_dashboard.py
    open dashboard/index.html
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    src = os.path.join(ROOT, "logs", "backtest_result.json")
    with open(src) as f:
        data = json.load(f)
    os.makedirs(os.path.join(ROOT, "dashboard"), exist_ok=True)
    html = TEMPLATE.replace("/*DATA*/", json.dumps(data))
    out = os.path.join(ROOT, "dashboard", "index.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"-> {out}")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CMC-TWAK Trading Agent — Performance</title>
<style>
:root{--bg:#0a0e17;--surf:#131a28;--surf2:#1b2435;--line:#27314a;--txt:#e8edf7;
--mut:#8a96ad;--grn:#2fd27e;--red:#ff5d6c;--blu:#4d8dff;--amb:#ffb547;--vio:#9b8cff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:'Inter',system-ui,-apple-system,sans-serif;
padding:28px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.brand h1{font-size:23px;font-weight:650;letter-spacing:-.3px}
.brand p{color:var(--mut);font-size:13px;margin-top:4px}
.badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{background:var(--surf2);border:1px solid var(--line);border-radius:999px;
padding:6px 12px;font-size:12px;font-weight:550;color:var(--mut)}
.badge.on{color:var(--grn);border-color:rgba(47,210,126,.35)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
.card{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.card .lab{color:var(--mut);font-size:12px;font-weight:550;text-transform:uppercase;letter-spacing:.5px}
.card .val{font-size:26px;font-weight:700;margin-top:8px;font-variant-numeric:tabular-nums}
.card .sub{color:var(--mut);font-size:12px;margin-top:4px}
.pos{color:var(--grn)}.neg{color:var(--red)}.hero-num{font-size:30px}
.cols{display:grid;grid-template-columns:2fr 1fr;gap:14px}
.panel{background:var(--surf);border:1px solid var(--line);border-radius:14px;padding:18px}
.panel h2{font-size:14px;font-weight:600;margin-bottom:14px;display:flex;justify-content:space-between;align-items:center}
.panel h2 span{color:var(--mut);font-size:12px;font-weight:500}
.legend{display:flex;gap:16px;font-size:12px;color:var(--mut);margin-top:6px}
.dot{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px;vertical-align:middle}
.row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.row:last-child{border:0}.row .t{color:var(--mut)}
.gauge{height:10px;background:var(--surf2);border-radius:999px;overflow:hidden;margin:10px 0 6px}
.gauge>i{display:block;height:100%;background:linear-gradient(90deg,var(--grn),var(--amb))}
.tag{font-size:11px;padding:3px 8px;border-radius:6px;background:var(--surf2);color:var(--mut);font-weight:600}
.foot{margin-top:20px;color:var(--mut);font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.foot b{color:var(--blu)}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.cols{grid-template-columns:1fr}}
</style></head>
<body><div class="wrap">
<div class="top">
  <div class="brand">
    <h1>🤖 CMC-TWAK Autonomous Trading Agent</h1>
    <p id="byline">BNB Hack: AI Trading Agent Edition · live on BSC</p>
  </div>
  <div class="badges" id="badges"></div>
</div>
<div class="grid" id="kpis"></div>
<div class="cols">
  <div class="panel">
    <h2>Equity vs. equal-weight buy &amp; hold <span id="chartmeta"></span></h2>
    <div id="chart"></div>
    <div class="legend">
      <span><i class="dot" style="background:var(--grn)"></i>Agent</span>
      <span><i class="dot" style="background:var(--red)"></i>Buy &amp; hold (market)</span>
    </div>
  </div>
  <div class="panel">
    <h2>Risk &amp; rules</h2>
    <div style="font-size:12px;color:var(--mut)">Drawdown headroom to DQ line</div>
    <div class="gauge"><i id="ddbar"></i></div>
    <div style="font-size:12px;color:var(--mut);margin-bottom:14px"><span id="ddtxt"></span></div>
    <div id="rules"></div>
  </div>
</div>
<div class="cols" style="margin-top:14px">
  <div class="panel">
    <h2>Recent decisions <span>signal → action → reason</span></h2>
    <div id="fills"></div>
  </div>
  <div class="panel">
    <h2>Holdings</h2>
    <div id="holdings"></div>
  </div>
</div>
<div class="foot">
  <span>Signals: <b>#CMCAgentHub</b> (12 MCP tools) · Execution: Trust Wallet Agent Kit · Identity: ERC-8004</span>
  <span id="ts"></span>
</div>
</div>
<script>
const D=/*DATA*/;
const $=id=>document.getElementById(id);
const pct=v=>(v>=0?'+':'')+v.toFixed(2)+'%';
const cls=v=>v>=0?'pos':'neg';

// badges
$('badges').innerHTML=[`<span class="badge on">● ${D.policy} strategy</span>`,
  `<span class="badge">${D.tokens.length} eligible tokens</span>`,
  `<span class="badge">CMC Agent Hub</span>`,`<span class="badge">TWAK · BSC</span>`].join('');

// KPIs
const k=D.kpis, edge=(k.total_return_pct-k.buyhold_pct);
$('kpis').innerHTML=[
 ['Total return',pct(k.total_return_pct),cls(k.total_return_pct),'backtest, '+D.period],
 ['vs. buy &amp; hold','+'+edge.toFixed(1)+' pts','pos','market did '+pct(k.buyhold_pct)],
 ['Max drawdown',k.max_drawdown_pct.toFixed(2)+'%','pos','DQ line '+k.dq_pct+'% — safe'],
 ['Sharpe-like',k.sharpe_like.toFixed(2),k.sharpe_like>=0?'pos':'neg','per-bar'],
 ['Trades',String(k.trades),'',D.blocked+' blocked by rules'],
 ['Final equity','$'+k.final.toFixed(0),cls(k.total_return_pct),'from $'+k.initial.toFixed(0)],
].map(([l,v,c,s])=>`<div class="card"><div class="lab">${l}</div>
 <div class="val ${c}">${v}</div><div class="sub">${s}</div></div>`).join('');
// make the edge card the hero
$('kpis').children[1].querySelector('.val').classList.add('hero-num');

// chart
function chart(){
 const W=660,H=240,pad=28,N=D.equity.length;
 const all=D.equity.concat(D.benchmark);let mn=Math.min(...all),mx=Math.max(...all);
 const pd=(mx-mn)*0.08||1;mn-=pd;mx+=pd;
 const X=i=>pad+i*(W-2*pad)/(N-1), Y=v=>H-pad-(v-mn)/(mx-mn)*(H-2*pad);
 const path=s=>s.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
 const area=path(D.equity)+` L${X(N-1)} ${H-pad} L${X(0)} ${H-pad} Z`;
 let grid='';for(let g=0;g<=3;g++){const yy=pad+g*(H-2*pad)/3;
  grid+=`<line x1="${pad}" y1="${yy}" x2="${W-pad}" y2="${yy}" stroke="var(--line)" stroke-width="1"/>`;}
 $('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
   <stop offset="0" stop-color="var(--grn)" stop-opacity=".28"/>
   <stop offset="1" stop-color="var(--grn)" stop-opacity="0"/></linearGradient></defs>
  ${grid}
  <path d="${area}" fill="url(#g)"/>
  <path d="${path(D.benchmark)}" fill="none" stroke="var(--red)" stroke-width="2" stroke-dasharray="5 4" opacity=".85"/>
  <path d="${path(D.equity)}" fill="none" stroke="var(--grn)" stroke-width="2.5"/>
  <text x="${pad}" y="${H-6}" fill="var(--mut)" font-size="11">${D.dates[0]||''}</text>
  <text x="${W-pad}" y="${H-6}" fill="var(--mut)" font-size="11" text-anchor="end">${D.dates[D.dates.length-1]||''}</text>
  <text x="${W-pad}" y="${pad-8}" fill="var(--mut)" font-size="11" text-anchor="end">$${mx.toFixed(0)}</text>
 </svg>`;
 $('chartmeta').textContent=N+' bars';
}
chart();

// risk gauge
const head=Math.max(0,k.dq_pct-k.max_drawdown_pct);
$('ddbar').style.width=Math.min(100,k.max_drawdown_pct/k.dq_pct*100).toFixed(1)+'%';
$('ddtxt').innerHTML=`Used <b style="color:var(--txt)">${k.max_drawdown_pct.toFixed(2)}%</b> of the ${k.dq_pct}% DQ budget · ${head.toFixed(1)}% headroom`;
const br=Object.entries(D.block_reasons||{}).map(([r,n])=>`<div class="row"><span class="t">${r}</span><span class="tag">${n}</span></div>`).join('');
$('rules').innerHTML=`<div class="row"><span class="t">Min trades / week</span><span>${k.trades} / 7</span></div>
 <div class="row"><span class="t">Trades blocked (rule adherence)</span><span>${D.blocked}</span></div>${br}`;

// fills
$('fills').innerHTML=(D.fills.length?D.fills:[]).map(f=>`<div class="row">
  <span><b>${(f.action||'').toUpperCase()}</b> ${f.token}</span>
  <span class="t">${(f.reason||'').slice(0,42)}</span></div>`).join('')||'<div class="t" style="color:var(--mut);font-size:13px">Capital preserved in cash — disciplined inactivity in risk-off.</div>';

// holdings
const pos=Object.entries(D.positions||{});
$('holdings').innerHTML=`<div class="row"><span>USDT (cash)</span><span class="pos">$${D.cash.toFixed(2)}</span></div>`+
 (pos.length?pos.map(([t,q])=>`<div class="row"><span>${t}</span><span>${q}</span></div>`).join('')
  :'<div class="t" style="color:var(--mut);font-size:13px;margin-top:8px">Fully in cash — risk-off regime.</div>');

$('ts').textContent='Generated '+D.generated+' · '+D.universe+' universe';
</script></body></html>"""


if __name__ == "__main__":
    main()
