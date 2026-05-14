#!/usr/bin/env python3
"""
HIP-4 Prediction Market Spread Scanner
Pure observation — no orders, no wallet.
Single-run mode: one scan, append to data/hip4_YYYYMMDD.csv, exit.
GitHub Actions re-triggers every 10 minutes.
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.hyperliquid.xyz/info"
CSV_DIR = Path(os.getenv("CSV_DIR", "data"))
REQUEST_TIMEOUT = 15
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

CSV_FIELDS = [
    "timestamp", "market_id", "question",
    "yes_bid", "yes_ask", "no_bid", "no_ask", "spread", "volume",
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _post(payload: dict) -> dict | list:
    resp = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if DEBUG:
        import json
        print(f"[DEBUG] {payload['type']} → {json.dumps(data)[:500]}", file=sys.stderr)
    return data


def fetch_all_mids() -> dict[str, str]:
    return _post({"type": "allMids"})


def fetch_outcome_meta() -> list[dict] | dict:
    return _post({"type": "outcomeMeta"})


def fetch_l2_book(coin: str) -> dict:
    return _post({"type": "l2Book", "coin": coin, "nSigFigs": None})


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    """Return (best_bid, best_ask) from an l2Book response."""
    levels = book.get("levels") or [[], []]
    bids, asks = levels[0], levels[1]
    bid = float(bids[0]["px"]) if bids else None
    ask = float(asks[0]["px"]) if asks else None
    return bid, ask


def book_volume(book: dict) -> float:
    """Sum of bid-side sizes as a liquidity proxy."""
    levels = book.get("levels") or [[], []]
    return sum(float(l["sz"]) for l in levels[0])


def hip4_coins_from_mids(all_mids: dict) -> list[str]:
    """Extract #N coins from allMids, sorted numerically."""
    coins = [k for k in all_mids if k.startswith("#")]
    return sorted(coins, key=lambda x: int(x[1:]) if x[1:].isdigit() else 10**9)


# ---------------------------------------------------------------------------
# outcomeMeta parsing  (format may change as HIP-4 evolves)
# ---------------------------------------------------------------------------

def parse_outcome_meta(raw: list | dict) -> dict[str, dict]:
    """
    Parse outcomeMeta into:
      { "#N": {yes_coin, no_coin, question, yes_name, no_name} }

    Confirmed format (observed 2026-05-13):
      {"outcomes": [{"outcome": 35, "name": "...", "description": "...",
                     "sideSpecs": [{"name": "Yes"}, {"name": "No"}]}]}
    Coin formula: yes_coin = f"#{outcome*10}", no_coin = f"#{outcome*10+1}"
    """
    markets: dict[str, dict] = {}

    entries: list[dict] = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        for key in ("outcomes", "markets", "universe", "data"):
            if isinstance(raw.get(key), list):
                entries = raw[key]
                break

    for entry in entries:
        outcome_id = entry.get("outcome")
        if outcome_id is None:
            continue

        question = entry.get("description") or entry.get("name") or ""
        side_specs = entry.get("sideSpecs", [])

        yes_name = side_specs[0].get("name", "Yes") if len(side_specs) >= 1 else "Yes"
        no_name  = side_specs[1].get("name", "No")  if len(side_specs) >= 2 else "No"

        markets[f"#{outcome_id}"] = {
            "yes_coin": f"#{outcome_id * 10}",
            "no_coin":  f"#{outcome_id * 10 + 1}",
            "question": question[:80],
            "yes_name": yes_name,
            "no_name":  no_name,
        }

    return markets


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def scan() -> list[dict]:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []

    all_mids = fetch_all_mids()
    raw_meta = fetch_outcome_meta()
    market_map = parse_outcome_meta(raw_meta)

    if market_map:
        # Full YES/NO path when outcomeMeta gives us the coin mapping
        for market_id, info in sorted(market_map.items(),
                                      key=lambda kv: int(kv[0][1:])
                                      if kv[0][1:].isdigit() else 10**9):
            yes_coin = info["yes_coin"]
            no_coin  = info["no_coin"]

            yes_book = fetch_l2_book(yes_coin) if yes_coin else {}
            no_book  = fetch_l2_book(no_coin)  if no_coin  else {}

            yes_bid, yes_ask = best_bid_ask(yes_book)
            no_bid,  no_ask  = best_bid_ask(no_book)

            spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None
            volume = book_volume(yes_book)

            rows.append({
                "timestamp": ts,
                "market_id": market_id,
                "question":  info["question"][:80],
                "yes_bid":   yes_bid,
                "yes_ask":   yes_ask,
                "no_bid":    no_bid,
                "no_ask":    no_ask,
                "spread":    spread,
                "volume":    volume,
            })
    else:
        # Fallback: one coin per #N entry, no YES/NO split
        print("[WARN] outcomeMeta returned no parseable market map — "
              "falling back to per-coin mode.", file=sys.stderr)
        for coin in hip4_coins_from_mids(all_mids):
            book = fetch_l2_book(coin)
            bid, ask = best_bid_ask(book)
            spread = (ask - bid) if (ask is not None and bid is not None) else None
            rows.append({
                "timestamp": ts,
                "market_id": coin,
                "question":  "",
                "yes_bid":   bid,
                "yes_ask":   ask,
                "no_bid":    None,
                "no_ask":    None,
                "spread":    spread,
                "volume":    book_volume(book),
            })

    return sorted(rows, key=lambda r: r["spread"] if r["spread"] is not None else -1,
                  reverse=True)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_csv(rows: list[dict], path: Path) -> None:
    write_header = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def _fmt(val: float | None, d: int = 4) -> str:
    return f"{val:.{d}f}" if val is not None else "  —  "


def display(rows: list[dict]) -> None:
    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    w = 90
    sep = "─" * w

    print(f"\n{sep}")
    print(f"  HIP-4 Spread Scanner  |  {ts_str}  |  {len(rows)} markets found")
    print(sep)
    header = (
        f"  {'MARKET':<8}  {'YES BID':>9}  {'YES ASK':>9}"
        f"  {'NO BID':>9}  {'NO ASK':>9}  {'SPREAD':>9}  {'BID VOL':>11}"
    )
    print(header)
    print(sep)

    for r in rows:
        line = (
            f"  {r['market_id']:<8}"
            f"  {_fmt(r['yes_bid']):>9}"
            f"  {_fmt(r['yes_ask']):>9}"
            f"  {_fmt(r['no_bid']):>9}"
            f"  {_fmt(r['no_ask']):>9}"
            f"  {_fmt(r['spread']):>9}"
            f"  {_fmt(r['volume'], 2):>11}"
        )
        print(line)
        if r.get("question"):
            print(f"  {'':8}  {r['question']}")

    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    csv_path = CSV_DIR / f"hip4_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    try:
        rows = scan()
        display(rows)
        save_csv(rows, csv_path)
        print(f"\n{len(rows)} markets saved → {csv_path}")
    except requests.HTTPError as exc:
        print(f"[HTTP {exc.response.status_code}] {exc}", file=sys.stderr)
        sys.exit(1)
    except requests.ConnectionError as exc:
        print(f"[CONN ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
