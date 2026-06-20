// x402-gated premium-signal endpoint (Base mainnet, USDC, EIP-3009 gasless).
// The agent pays $0.001 per request via TWAK's x402 client to fetch this signal
// as part of its trade loop — real on-chain micro-payment, settled by the
// Coinbase CDP facilitator. This is the x402 leg of "Best Use of TWAK".
//
//   npm i express @x402/express @x402/evm @x402/core @coinbase/x402
//   CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... node server.js
//   (expose over https, e.g. cloudflared tunnel --url http://localhost:4021)
import express from "express";
import fs from "fs";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { HTTPFacilitatorClient } from "@x402/core/server";
import { facilitator } from "@coinbase/x402";              // CDP creds from env

const PAYTO = process.env.X402_PAYTO || "0x32A84F2cf8D55a8eC5414D7DC42b0D873A98AB19";
const LOG = process.env.AGENT_LOG || "/opt/cmc-twak-agent/logs/decisions.jsonl";

const fc = new HTTPFacilitatorClient(facilitator);
const rs = new x402ResourceServer(fc).register("eip155:8453", new ExactEvmScheme());
const app = express();
app.use(paymentMiddleware({
  "GET /signal": {
    accepts: { scheme: "exact", price: "$0.001", network: "eip155:8453", payTo: PAYTO },
    description: "CTA premium market signal (regime + top momentum)",
  },
}, rs));

app.get("/signal", (req, res) => {
  let regime = "unknown", top = [], bias = 0;
  try {
    const sigs = fs.readFileSync(LOG, "utf8").trim().split("\n").slice(-300)
      .map(l => { try { return JSON.parse(l); } catch { return null; } })
      .filter(r => r && r.kind === "signal");
    if (sigs.length) {
      regime = sigs[sigs.length - 1].regime;
      const recent = sigs.slice(-41);                                  // latest universe sweep
      top = recent.slice().sort((a, b) => b.score - a.score).slice(0, 3)
        .map(s => ({ token: s.token, score: s.score }));
      // premium market bias = aggregate momentum across the universe, in [-1,1]
      const avg = recent.reduce((s, r) => s + (r.score || 0), 0) / recent.length;
      bias = Math.max(-1, Math.min(1, +avg.toFixed(4)));
    }
  } catch {}
  res.json({ source: "CTA x402 premium signal", regime, top, bias, ts: Date.now() });
});

app.listen(4021, () => console.log("x402 signal server on :4021"));
