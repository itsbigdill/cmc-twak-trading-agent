"""
Refine the trade universe by LIQUIDITY (executability).

For each token in config/trade_universe.json, quote a fixed-size USDT->token swap
and read priceImpact. Illiquid tokens have high impact and bad fills (our 1%
slippage cap would abort them anyway), so we keep only low-impact names and cap
to the most liquid MAX. Rotation gets breadth without slippage traps.

    python scripts/rank_liquidity.py --size 200 --max 40 --max-impact 1.0
"""

import argparse
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def impact(addr, size):
    try:
        out = subprocess.run(
            ["twak", "swap", "--usd", str(size), "USDT", addr, "--chain", "bsc",
             "--quote-only", "--json"], capture_output=True, text=True, timeout=60)
        d = json.loads(out.stdout[out.stdout.find("{"):])
        if "output" not in d:
            return None
        return abs(float(str(d.get("priceImpact", "999")).replace("%", "")))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=200.0)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--max-impact", type=float, default=1.0)
    args = ap.parse_args()

    path = os.path.join(ROOT, "config", "trade_universe.json")
    uni = json.load(open(path))
    scored = []
    for i, (sym, addr) in enumerate(uni.items(), 1):
        imp = impact(addr, args.size)
        ok = imp is not None and imp <= args.max_impact
        if ok:
            scored.append((imp, sym, addr))
        print(f"[{i}/{len(uni)}] {sym:12} impact={imp}  {'keep' if ok else 'drop'}")

    scored.sort()                                  # lowest impact (most liquid) first
    kept = scored[: args.max]
    out = {sym: addr for _, sym, addr in kept}
    # always keep the core liquid majors even if a quote flaked
    core = {"ETH": "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
            "CAKE": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"}
    for s, a in core.items():
        out.setdefault(s, a)
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nKEPT {len(out)} liquid tokens (cap {args.max}, impact<={args.max_impact}%) -> {path}")
    print(", ".join(out))


if __name__ == "__main__":
    main()
