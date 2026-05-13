# SpreadWatch

> Real-time spread monitoring and market inefficiency detection for prediction markets.

## What is SpreadWatch?

SpreadWatch is an analytics tool that continuously monitors bid/ask spreads across prediction market platforms — starting with **Opinion.trade** — to surface pricing inefficiencies in real time.

Most prediction market traders focus on picking the right outcome. SpreadWatch focuses on *how* markets are priced: identifying moments where the gap between buyers and sellers is wide enough to represent a structural opportunity, before the market self-corrects.

## How it works

SpreadWatch polls the order book for every active market on a platform, calculates the bid/ask spread for each outcome token, and logs the results every 30 seconds. It surfaces the markets where spreads are largest, flags anomalies, and exports everything to CSV for further analysis.

```
SPREAD    BID    ASK    VOL24H    MARKET / OUTCOME
0.1300  0.620  0.750  $116,000  LoL LCK 2026 / LCK (South Korea)  ◄◄◄
0.0900  0.280  0.370   $96,000  LoL Worlds / LPL (China)           ◄◄
0.0400  0.410  0.450   $10,000  Democratic 2028 / Gavin Newsom
```

## Core features

- **Full market scan** — covers all active binary and categorical markets
- **Order book depth analysis** — best bid, best ask, spread per outcome token
- **Spread ranking** — sorted leaderboard updated every 30 seconds
- **CSV logging** — timestamped export for historical analysis
- **Session summary** — identifies markets with persistently wide spreads across multiple scans

## Platform support

| Platform | Status |
|---|---|
| Opinion.trade | In development |
| Polymarket | Implemented |
| Kalshi | Planned |

## Use cases

- **Traders** — identify markets where limit orders can be placed at favorable prices with maker fees of 0%
- **Researchers** — track how spreads evolve over a market's lifecycle, from launch to resolution
- **Market makers** — monitor which markets have the least competition for liquidity provision

## Background

SpreadWatch grew out of research into prediction market microstructure — specifically the observation that newer platforms and newly-launched markets tend to have significantly wider spreads than established ones, representing a window of opportunity before professional market makers arrive.

The project is inspired by how the most profitable prediction market traders operate: not by predicting outcomes, but by understanding *when* and *where* markets are mispriced.

## Status

Currently in active development. Opinion.trade integration in progress pending API access.

---

*Built by reily157
