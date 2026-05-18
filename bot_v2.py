#!/usr/bin/env python3
"""
HIP-4 OPENING OBSERVATION BOT v2
==================================
Pure observation enrichie — Couche 1.5

Ce que le bot fait :
  - À 06:00:00 UTC pile chaque jour
  - Détecte les 3 nouveaux outcomes catégoriels (index:0, 1, 2)
  - Capture des snapshots du book toutes les 10 secondes
  - Logge 5 niveaux de profondeur des deux côtés
  - Logge le prix BTC spot synchronisé
  - Détecte le moment exact de fin d'auction (premier fill)
  - Continue 30 minutes après l'ouverture (06:00 → 06:30 UTC)
  - Sauve tout en CSV propre par marché

Ce que le bot NE fait PAS :
  - Aucun ordre placé
  - Aucune décision automatique
  - Aucun capital risqué

Objectif : comprendre la mécanique exacte de l'opening auction sur HIP-4.
"""

import os
import sys
import time
import csv
import logging
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ─── CONFIG ────────────────────────────────────────────────────────────────────
load_dotenv()

MAIN_ADDR = os.environ.get("HYPERLIQUID_MAIN_ADDRESS", "")

# Paramètres
OBSERVATION_DURATION_S = 30 * 60   # 30 min d'observation
SNAPSHOT_INTERVAL_S    = 10        # snapshot toutes les 10s
API_URL                = "https://api.hyperliquid.xyz/info"

# Chemins
BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "observations"
LOG_DIR.mkdir(exist_ok=True)

# Logging
log_file = LOG_DIR / f"bot_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── API HELPERS ───────────────────────────────────────────────────────────────

def post(payload: dict) -> dict:
    """Wrapper pour les appels POST à l'API Hyperliquid."""
    resp = requests.post(API_URL, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_outcome_meta() -> list[dict]:
    """Récupère les marchés HIP-4 actifs."""
    data = post({"type": "outcomeMeta"})
    return data.get("outcomes", [])


def fetch_l2_book(coin: str) -> dict:
    """Récupère le order book L2 (5 niveaux de profondeur)."""
    return post({"type": "l2Book", "coin": coin})


def fetch_all_mids() -> dict:
    """Récupère les prix mid de tous les actifs (inclut BTC spot)."""
    return post({"type": "allMids"})


def fetch_recent_trades(coin: str) -> list[dict]:
    """Récupère les trades récents d'un asset."""
    try:
        return post({"type": "recentTrades", "coin": coin})
    except Exception:
        return []


# ─── MARKET DISCOVERY ──────────────────────────────────────────────────────────

def find_new_categorical_markets(outcomes: list[dict]) -> list[dict]:
    """
    Trouve les 3 outcomes catégoriels les plus récents (index:0, 1, 2).
    On suppose que les plus récents ont les outcome_id les plus élevés
    et viennent juste d'ouvrir.
    """
    categorical = []
    for o in outcomes:
        desc = o.get("description", "")
        if "index:" in desc:
            try:
                idx = int(desc.split("index:")[1].split("|")[0])
                outcome_id = o.get("outcome")
                if idx in (0, 1, 2):
                    categorical.append({
                        "outcome_id": outcome_id,
                        "index":      idx,
                        "yes_coin":   f"#{outcome_id * 10}",
                        "no_coin":    f"#{outcome_id * 10 + 1}",
                        "name":       o.get("name", ""),
                        "description": desc,
                    })
            except (ValueError, IndexError):
                continue

    # On garde les 3 outcomes catégoriels les plus récents (ids les plus élevés)
    categorical.sort(key=lambda x: x["outcome_id"], reverse=True)

    # Group by base outcome_id pour identifier le triplet le plus récent
    if len(categorical) < 3:
        return categorical

    # Le triplet le plus récent c'est les 3 ids les plus élevés
    return categorical[:3]


def parse_book_depth(book: dict, levels: int = 5) -> dict:
    """Extrait les N premiers niveaux de profondeur des deux côtés."""
    book_levels = book.get("levels", [[], []])
    bids = book_levels[0] if len(book_levels) > 0 else []
    asks = book_levels[1] if len(book_levels) > 1 else []

    result = {
        "best_bid":  float(bids[0]["px"]) if bids else None,
        "best_ask":  float(asks[0]["px"]) if asks else None,
        "spread":    None,
        "bid_depth_total": sum(float(b["sz"]) for b in bids[:levels]),
        "ask_depth_total": sum(float(a["sz"]) for a in asks[:levels]),
        "num_bid_levels":  len(bids),
        "num_ask_levels":  len(asks),
    }

    # 5 premiers niveaux des deux côtés
    for i in range(levels):
        if i < len(bids):
            result[f"bid_{i+1}_px"] = float(bids[i]["px"])
            result[f"bid_{i+1}_sz"] = float(bids[i]["sz"])
        else:
            result[f"bid_{i+1}_px"] = None
            result[f"bid_{i+1}_sz"] = None

        if i < len(asks):
            result[f"ask_{i+1}_px"] = float(asks[i]["px"])
            result[f"ask_{i+1}_sz"] = float(asks[i]["sz"])
        else:
            result[f"ask_{i+1}_px"] = None
            result[f"ask_{i+1}_sz"] = None

    if result["best_bid"] and result["best_ask"]:
        result["spread"] = round(result["best_ask"] - result["best_bid"], 4)

    return result


# ─── WAIT FOR OPENING ──────────────────────────────────────────────────────────

def wait_for_opening():
    """Attend jusqu'à 06:00:00 UTC précisément."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)

    seconds_to_wait = (target - now).total_seconds()
    log.info(f"Prochaine ouverture HIP-4 : {target.isoformat()} UTC")
    log.info(f"Attente de {seconds_to_wait:.0f}s ({seconds_to_wait/3600:.1f}h)")

    while True:
        now = datetime.now(timezone.utc)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 300:    # plus de 5min : check toutes les 60s
            time.sleep(60)
        elif remaining > 30:   # entre 30s et 5min : check toutes les 5s
            time.sleep(5)
        elif remaining > 2:    # moins de 30s : check toutes les 0.5s
            time.sleep(0.5)
        else:                  # 2 dernières secondes : précision maximum
            time.sleep(0.05)


# ─── MAIN OBSERVATION ─────────────────────────────────────────────────────────

def observe_opening():
    """Observe l'opening auction et le post-auction sur les 3 outcomes."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    log.info(f"━━━ DÉBUT OBSERVATION CYCLE {cycle_id} ━━━")

    # Discovery
    log.info("Récupération outcomeMeta...")
    outcomes = fetch_outcome_meta()
    targets = find_new_categorical_markets(outcomes)

    if len(targets) < 3:
        log.error(f"❌ Trouvé seulement {len(targets)} outcomes catégoriels. "
                  f"Au moins 3 attendus.")
        log.error("Outcomes trouvés:")
        for t in targets:
            log.error(f"  {t}")
        return

    log.info(f"✅ 3 outcomes catégoriels identifiés :")
    for t in targets:
        log.info(f"  index:{t['index']} → {t['yes_coin']} (outcome_id={t['outcome_id']})")
        log.info(f"    Description: {t['description'][:80]}")

    # Prépare les CSVs (un par outcome)
    writers = {}
    files = {}
    fieldnames = [
        "timestamp", "elapsed_s", "btc_price",
        "best_bid", "best_ask", "spread",
        "bid_depth_total", "ask_depth_total",
        "num_bid_levels", "num_ask_levels",
    ]
    # Ajoute les 5 niveaux
    for i in range(1, 6):
        fieldnames += [f"bid_{i}_px", f"bid_{i}_sz", f"ask_{i}_px", f"ask_{i}_sz"]
    fieldnames += ["last_trade_px", "last_trade_sz", "num_trades_since_start"]

    for target in targets:
        coin = target["yes_coin"]
        idx = target["index"]
        csv_path = LOG_DIR / f"obs_{cycle_id}_idx{idx}_{coin.replace('#','')}.csv"
        f = open(csv_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        files[coin] = f
        writers[coin] = writer

    # Track les fills pour détecter la fin d'auction
    trade_counters = {t["yes_coin"]: 0 for t in targets}
    first_fill_logged = {t["yes_coin"]: False for t in targets}

    # Boucle d'observation
    start_time = time.time()
    snapshot_count = 0

    try:
        while time.time() - start_time < OBSERVATION_DURATION_S:
            snapshot_start = time.time()
            elapsed = round(snapshot_start - start_time, 1)
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

            # Récupère le prix BTC spot
            try:
                mids = fetch_all_mids()
                btc_price = float(mids.get("BTC", 0))
            except Exception as e:
                log.warning(f"Erreur fetch BTC price: {e}")
                btc_price = None

            # Pour chaque outcome cible, snapshot
            for target in targets:
                coin = target["yes_coin"]
                try:
                    book = fetch_l2_book(coin)
                    depth = parse_book_depth(book, levels=5)

                    # Récupère les trades récents pour détecter la fin d'auction
                    trades = fetch_recent_trades(coin)
                    last_trade_px = None
                    last_trade_sz = None
                    if trades:
                        last_trade_px = float(trades[0].get("px", 0))
                        last_trade_sz = float(trades[0].get("sz", 0))
                        new_count = len(trades)
                        if new_count > trade_counters[coin]:
                            if not first_fill_logged[coin]:
                                log.info(f"🔔 PREMIER FILL détecté sur "
                                        f"index:{target['index']} à T+{elapsed:.1f}s "
                                        f"(prix: {last_trade_px})")
                                first_fill_logged[coin] = True
                            trade_counters[coin] = new_count

                    row = {
                        "timestamp": ts,
                        "elapsed_s": elapsed,
                        "btc_price": btc_price,
                        "last_trade_px": last_trade_px,
                        "last_trade_sz": last_trade_sz,
                        "num_trades_since_start": trade_counters[coin],
                        **depth,
                    }
                    writers[coin].writerow(row)
                    files[coin].flush()
                except Exception as e:
                    log.error(f"Erreur snapshot {coin}: {e}")

            snapshot_count += 1

            # Log de progression
            if snapshot_count == 1 or snapshot_count % 6 == 0:  # toutes les minutes
                log.info(f"T+{int(elapsed)}s | Snapshot #{snapshot_count} | "
                        f"BTC={btc_price}")
                for target in targets:
                    coin = target["yes_coin"]
                    # Ré-fetch pour log (déjà sauvé dans CSV)
                    try:
                        b = fetch_l2_book(coin)
                        d = parse_book_depth(b)
                        log.info(f"  idx:{target['index']} bid={d['best_bid']} "
                                f"ask={d['best_ask']} spread={d['spread']} "
                                f"trades={trade_counters[coin]}")
                    except Exception:
                        pass

            # Sleep pour atteindre l'intervalle exact
            elapsed_this_snapshot = time.time() - snapshot_start
            sleep_time = SNAPSHOT_INTERVAL_S - elapsed_this_snapshot
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        log.info("Interruption manuelle pendant l'observation.")
    finally:
        for f in files.values():
            f.close()

    log.info(f"━━━ FIN OBSERVATION CYCLE {cycle_id} ━━━")
    log.info(f"Total snapshots : {snapshot_count}")
    log.info(f"Durée totale : {time.time() - start_time:.1f}s")
    log.info(f"Fichiers : {LOG_DIR}")

    # Résumé
    log.info("\n━━━ RÉSUMÉ FIRST FILLS ━━━")
    for target in targets:
        coin = target["yes_coin"]
        idx = target["index"]
        if first_fill_logged[coin]:
            log.info(f"  index:{idx} → trades détectés ({trade_counters[coin]})")
        else:
            log.info(f"  index:{idx} → AUCUN trade détecté (auction encore ouverte?)")


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("  HIP-4 OPENING OBSERVATION BOT v2")
    log.info(f"  Mode: OBSERVATION PURE (zéro ordre)")
    log.info(f"  Durée observation : {OBSERVATION_DURATION_S//60} min")
    log.info(f"  Intervalle snapshot : {SNAPSHOT_INTERVAL_S}s")
    log.info(f"  Output : {LOG_DIR}")
    log.info("=" * 70)

    if not MAIN_ADDR:
        log.warning("HYPERLIQUID_MAIN_ADDRESS non défini. Continue quand même "
                   "(observation ne nécessite pas d'auth).")

    # Boucle quotidienne
    while True:
        try:
            wait_for_opening()
            observe_opening()
            log.info("\nObservation terminée. Attente du prochain cycle.\n")
        except KeyboardInterrupt:
            log.info("Arrêt manuel du bot.")
            break
        except Exception as e:
            log.error(f"❌ Erreur cycle: {e}", exc_info=True)
            time.sleep(3600)  # 1h avant retry


if __name__ == "__main__":
    main()
