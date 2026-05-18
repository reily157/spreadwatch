#!/usr/bin/env python3
"""
HIP-4 OPENING SCOUT BOT
=========================
Bot d'exécution simple — Couche 1 : Observation + exécution mécanique

Ce que le bot fait :
  - À 06:00:00 UTC chaque jour
  - Détecte les nouveaux marchés HIP-4 ouverts
  - Identifie l'outcome index:2 (range haut, faible probabilité)
  - Lit le book initial
  - Place 1 ordre limite maker à best_bid + 0.001
  - Monitor l'ordre pendant 30 minutes (snapshot toutes les 5s)
  - Annule si pas rempli après 30 min
  - Log tout dans CSV

Ce que le bot ne fait pas :
  - Aucune décision adaptative
  - Aucun retrait de fonds (limité par l'API Wallet)
  - Aucune intervention humaine pendant l'exécution
"""

import os
import sys
import time
import csv
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from eth_account import Account

# ─── CONFIG ────────────────────────────────────────────────────────────────────
load_dotenv()

API_KEY     = os.environ.get("HYPERLIQUID_API_PRIVATE_KEY", "")
API_ADDRESS = os.environ.get("HYPERLIQUID_API_WALLET_ADDRESS", "")
MAIN_ADDR   = os.environ.get("HYPERLIQUID_MAIN_ADDRESS", "")

if not all([API_KEY, API_ADDRESS, MAIN_ADDR]):
    print("❌ Variables manquantes dans .env")
    print("   Requises : HYPERLIQUID_API_PRIVATE_KEY, HYPERLIQUID_API_WALLET_ADDRESS, HYPERLIQUID_MAIN_ADDRESS")
    sys.exit(1)

# Paramètres du bot
ORDER_SIZE_USDH    = 1.0    # taille de l'ordre en USDH
PRICE_OFFSET       = 0.001  # offset au-dessus du best_bid
TARGET_OUTCOME     = 2      # index:2 (range haut, faible proba)
MONITOR_DURATION_S = 30 * 60  # 30 minutes de monitoring
SNAPSHOT_INTERVAL_S = 5     # snapshot du book toutes les 5s

# Chemins
BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
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

# ─── INITIALIZATION ────────────────────────────────────────────────────────────

def init_hyperliquid():
    """Initialise les clients Hyperliquid (Info pour lecture, Exchange pour ordres)."""
    account = Account.from_key(API_KEY)
    log.info(f"API Wallet address: {account.address}")
    log.info(f"Main address: {MAIN_ADDR}")

    # Info client (lecture publique, pas besoin d'auth)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    # Exchange client (signature des ordres)
    exchange = Exchange(
        account,
        constants.MAINNET_API_URL,
        account_address=MAIN_ADDR,  # adresse principale qui contient les fonds
    )

    return info, exchange


# ─── HIP-4 MARKET DISCOVERY ────────────────────────────────────────────────────

def fetch_outcome_meta(info: Info) -> list[dict]:
    """Récupère la liste des marchés HIP-4 actifs."""
    import requests
    resp = requests.post(
        constants.MAINNET_API_URL + "/info",
        json={"type": "outcomeMeta"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("outcomes", [])


def find_new_categorical_markets(outcomes: list[dict]) -> list[dict]:
    """
    Identifie les nouveaux marchés catégoriels (index:0/1/2).
    On veut les marchés qui viennent d'ouvrir à 06:00 UTC.
    """
    new_markets = []
    for o in outcomes:
        desc = o.get("description", "")
        # Les outcomes catégoriels ont "index:N" dans la description
        if "index:" in desc:
            try:
                idx = int(desc.split("index:")[1].split("|")[0])
                outcome_id = o.get("outcome")
                new_markets.append({
                    "outcome_id": outcome_id,
                    "index":      idx,
                    "yes_coin":   f"#{outcome_id * 10}",
                    "no_coin":    f"#{outcome_id * 10 + 1}",
                    "name":       o.get("name", ""),
                    "description": desc,
                })
            except (ValueError, IndexError):
                continue
    return new_markets


def filter_target_outcome(markets: list[dict]) -> Optional[dict]:
    """
    Filtre l'outcome qui nous intéresse (index:2) parmi les nouveaux marchés.
    On prend le plus récent (outcome_id le plus élevé).
    """
    candidates = [m for m in markets if m["index"] == TARGET_OUTCOME]
    if not candidates:
        return None
    # Le plus récent = outcome_id le plus élevé
    return max(candidates, key=lambda x: x["outcome_id"])


# ─── ORDER BOOK ────────────────────────────────────────────────────────────────

def fetch_l2_book(coin: str) -> dict:
    """Récupère le order book L2 d'un asset."""
    import requests
    resp = requests.post(
        constants.MAINNET_API_URL + "/info",
        json={"type": "l2Book", "coin": coin},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def parse_book(book: dict) -> dict:
    """Extrait les meilleurs bid/ask et la profondeur."""
    levels = book.get("levels", [[], []])
    bids = levels[0] if len(levels) > 0 else []
    asks = levels[1] if len(levels) > 1 else []

    result = {
        "best_bid":  float(bids[0]["px"]) if bids else None,
        "best_ask":  float(asks[0]["px"]) if asks else None,
        "bid_size":  float(bids[0]["sz"]) if bids else 0,
        "ask_size":  float(asks[0]["sz"]) if asks else 0,
        "bid_depth_5": sum(float(b["sz"]) for b in bids[:5]),
        "ask_depth_5": sum(float(a["sz"]) for a in asks[:5]),
        "spread":    None,
    }
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

    # Sleep en chunks pour pouvoir interrompre proprement
    while True:
        now = datetime.now(timezone.utc)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 60:
            time.sleep(30)
        elif remaining > 5:
            time.sleep(1)
        else:
            time.sleep(0.05)  # haute précision dans les 5 dernières secondes


# ─── MAIN EXECUTION ────────────────────────────────────────────────────────────

def execute_opening_strategy(info: Info, exchange: Exchange):
    """Exécute la stratégie complète à 06:00 UTC."""
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%d")
    log.info(f"━━━ DÉBUT CYCLE {cycle_id} ━━━")

    # ── Étape 1 : Discovery des nouveaux marchés ──────────────────────────────
    log.info("Étape 1: récupération outcomeMeta...")
    outcomes = fetch_outcome_meta(info)
    new_markets = find_new_categorical_markets(outcomes)
    log.info(f"  {len(new_markets)} marchés catégoriels trouvés")

    target = filter_target_outcome(new_markets)
    if not target:
        log.error("❌ Aucun outcome index:2 trouvé. Arrêt.")
        return

    log.info(f"  Outcome cible: index:{target['index']} → coin {target['yes_coin']}")
    log.info(f"  Description: {target['description'][:80]}")

    # ── Étape 2 : Lecture du book initial ─────────────────────────────────────
    coin = target["yes_coin"]
    log.info(f"Étape 2: lecture book de {coin}...")
    book = fetch_l2_book(coin)
    book_data = parse_book(book)
    log.info(f"  Best bid: {book_data['best_bid']}")
    log.info(f"  Best ask: {book_data['best_ask']}")
    log.info(f"  Spread: {book_data['spread']}")

    if book_data["best_bid"] is None:
        log.error("❌ Book vide côté bid. Impossible de poser un ordre maker.")
        save_snapshot(cycle_id, coin, book_data, "no_bid_skip")
        return

    # ── Étape 3 : Calcul du prix de l'ordre ───────────────────────────────────
    order_price = round(book_data["best_bid"] + PRICE_OFFSET, 4)
    log.info(f"Étape 3: prix d'ordre calculé = {order_price}")

    # Vérifier que notre prix reste sous le best_ask
    if order_price >= book_data["best_ask"]:
        log.warning(f"⚠️  order_price ({order_price}) >= best_ask ({book_data['best_ask']})")
        log.warning("    Réajustement à best_ask - 0.001")
        order_price = round(book_data["best_ask"] - 0.001, 4)

    # Calcul de la taille en shares
    order_size_shares = round(ORDER_SIZE_USDH / order_price, 2)
    log.info(f"  Taille: {order_size_shares} shares (= {ORDER_SIZE_USDH} USDH)")

    # ── Étape 4 : Placement de l'ordre ────────────────────────────────────────
    log.info("Étape 4: placement de l'ordre limite maker...")
    try:
        order_result = exchange.order(
            coin,
            True,                # is_buy = True (on achète YES)
            order_size_shares,   # taille en shares
            order_price,
            {"limit": {"tif": "Gtc"}},  # Good Till Cancel
        )
        log.info(f"  Réponse API: {order_result}")
    except Exception as e:
        log.error(f"❌ Erreur placement ordre: {e}")
        save_snapshot(cycle_id, coin, book_data, f"error_{e}")
        return

    # Extraire l'order_id
    order_id = None
    if order_result.get("status") == "ok":
        statuses = order_result.get("response", {}).get("data", {}).get("statuses", [])
        if statuses and "resting" in statuses[0]:
            order_id = statuses[0]["resting"]["oid"]
            log.info(f"✅ Ordre placé avec succès. Order ID: {order_id}")
        elif statuses and "filled" in statuses[0]:
            log.info(f"✅ Ordre rempli immédiatement: {statuses[0]['filled']}")
            save_snapshot(cycle_id, coin, book_data, "filled_immediate",
                         order_price=order_price, order_id="immediate")
            return

    if not order_id:
        log.error("❌ Pas d'order_id retourné, abandon du monitoring.")
        return

    # ── Étape 5 : Monitoring pendant 30 min ───────────────────────────────────
    log.info(f"Étape 5: monitoring pendant {MONITOR_DURATION_S//60} minutes...")
    monitor_order(info, exchange, cycle_id, coin, order_id, order_price)


def monitor_order(info: Info, exchange: Exchange, cycle_id: str,
                  coin: str, order_id: int, order_price: float):
    """Monitore l'ordre toutes les 5 secondes pendant 30 minutes."""
    start = time.time()
    snapshots_csv = LOG_DIR / f"snapshots_{cycle_id}_{coin.replace('#','')}.csv"

    with open(snapshots_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "timestamp", "elapsed_s", "best_bid", "best_ask", "spread",
            "bid_size", "ask_size", "bid_depth_5", "ask_depth_5",
            "order_status", "order_filled_size", "order_remaining"
        ])
        writer.writeheader()

        filled = False
        while time.time() - start < MONITOR_DURATION_S:
            elapsed = round(time.time() - start, 1)
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Snapshot du book
            try:
                book = fetch_l2_book(coin)
                bd = parse_book(book)
            except Exception as e:
                log.error(f"Erreur fetch book: {e}")
                bd = {"best_bid": None, "best_ask": None, "spread": None,
                      "bid_size": 0, "ask_size": 0, "bid_depth_5": 0, "ask_depth_5": 0}

            # Status de notre ordre
            try:
                open_orders = info.open_orders(MAIN_ADDR)
                our_order = next((o for o in open_orders if o.get("oid") == order_id), None)
                if our_order:
                    status = "resting"
                    remaining = float(our_order.get("sz", 0))
                    filled_size = 0
                else:
                    status = "filled_or_cancelled"
                    remaining = 0
                    filled_size = ORDER_SIZE_USDH / order_price
            except Exception as e:
                log.error(f"Erreur fetch open_orders: {e}")
                status = "unknown"
                remaining = -1
                filled_size = -1

            row = {
                "timestamp": ts,
                "elapsed_s": elapsed,
                "best_bid": bd["best_bid"],
                "best_ask": bd["best_ask"],
                "spread": bd["spread"],
                "bid_size": bd["bid_size"],
                "ask_size": bd["ask_size"],
                "bid_depth_5": bd["bid_depth_5"],
                "ask_depth_5": bd["ask_depth_5"],
                "order_status": status,
                "order_filled_size": filled_size,
                "order_remaining": remaining,
            }
            writer.writerow(row)
            f.flush()

            # Log progress toutes les minutes
            if int(elapsed) % 60 < SNAPSHOT_INTERVAL_S:
                log.info(f"  T+{int(elapsed)}s: bid={bd['best_bid']} ask={bd['best_ask']} "
                        f"spread={bd['spread']} status={status}")

            # Si rempli, log et sors
            if status == "filled_or_cancelled":
                log.info(f"✅ Ordre rempli à T+{elapsed:.1f}s !")
                filled = True
                break

            time.sleep(SNAPSHOT_INTERVAL_S)

    # Si non rempli après 30 min, annuler
    if not filled:
        log.info(f"⏱️  30 min écoulées, annulation de l'ordre {order_id}...")
        try:
            cancel_result = exchange.cancel(coin, order_id)
            log.info(f"  Annulation: {cancel_result}")
        except Exception as e:
            log.error(f"Erreur annulation: {e}")

    log.info(f"━━━ FIN CYCLE {cycle_id} ━━━\n")


def save_snapshot(cycle_id: str, coin: str, book_data: dict, status: str,
                  order_price: float = None, order_id = None):
    """Sauvegarde un snapshot simple (pour les cas où l'ordre n'est pas posé)."""
    summary_csv = LOG_DIR / "cycle_summary.csv"
    write_header = not summary_csv.exists()
    with open(summary_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "cycle_id", "timestamp", "coin", "best_bid", "best_ask",
            "spread", "order_price", "order_id", "status"
        ])
        if write_header:
            writer.writeheader()
        writer.writerow({
            "cycle_id": cycle_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin": coin,
            "best_bid": book_data["best_bid"],
            "best_ask": book_data["best_ask"],
            "spread": book_data["spread"],
            "order_price": order_price,
            "order_id": order_id,
            "status": status,
        })


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────

def main():
    log.info("═" * 60)
    log.info("  HIP-4 OPENING SCOUT BOT — démarrage")
    log.info(f"  Taille d'ordre: {ORDER_SIZE_USDH} USDH")
    log.info(f"  Outcome cible: index:{TARGET_OUTCOME}")
    log.info(f"  Monitoring: {MONITOR_DURATION_S//60} min")
    log.info("═" * 60)

    # Initialisation
    try:
        info, exchange = init_hyperliquid()
        log.info("✅ Connexion Hyperliquid établie")
    except Exception as e:
        log.error(f"❌ Erreur initialisation: {e}")
        sys.exit(1)

    # Boucle infinie : attendre 06:00 UTC, exécuter, recommencer
    while True:
        try:
            wait_for_opening()
            execute_opening_strategy(info, exchange)
        except KeyboardInterrupt:
            log.info("Interruption manuelle, arrêt du bot.")
            break
        except Exception as e:
            log.error(f"❌ Erreur cycle: {e}", exc_info=True)
            # Attendre 1h avant de reprendre pour éviter les boucles d'erreur
            time.sleep(3600)


if __name__ == "__main__":
    main()
