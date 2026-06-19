# x402 premium-signal endpoint

An x402-gated HTTP endpoint the agent **pays per request** to fetch a premium
market signal, as part of its trade loop. This is the **native x402** leg of the
Trust Wallet Agent Kit integration (Best Use of TWAK):

- **Server** (`server.js`): `@x402/express` + `ExactEvmScheme`, settled by the
  **Coinbase CDP facilitator** (`@coinbase/x402`) on **Base mainnet** in USDC,
  EIP-3009 (gasless for the payer).
- **Client**: the agent calls `twak x402 request <url>` every Nth tick
  (`config.yaml → x402`), signing a gasless USDC payment from the self-custody
  agent wallet. Verified end-to-end: the quote returns a real 402 challenge
  (`1000 atomic USDC on eip155:8453 via eip3009`).

## Run
```bash
npm i
CDP_API_KEY_ID=... CDP_API_KEY_SECRET=... node server.js      # :4021
cloudflared tunnel --url http://localhost:4021                # https URL (twak requires https)
```
Then set `x402.signal_url` in the agent's `config.yaml` to the https URL.

> Three TWAK surfaces drive the agent: **swap** (execution), **compete/erc8004**
> (on-chain identity), and **x402** (pay-per-request data) — TWAK as the heart of
> a hands-off trader, not plumbing bolted on.
