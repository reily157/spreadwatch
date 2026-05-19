Hello Claude. Je travaille sur un projet quant d'observation des marchés HIP-4 d'Hyperliquid, et je viens d'avoir une longue conversation stratégique avec une autre instance qui m'a aidé à structurer le problème. J'ai besoin que tu m'aides à coder l'observer v3. Lis tout ce qui suit attentivement avant de commencer à coder.
1. Contexte du projet
Je suis basé en Suisse. Je tourne un VPS AWS Lightsail à Tokyo pour la latence vers Hyperliquid.
Mon repo existant : https://github.com/reily157/spreadwatch
Il contient :

bot_v2.py : un observer REST-polling 10s, ciblait 3 outcomes catégoriels via "index:" dans description. Je veux le remplacer par v3.
hip4_scanner.py : un scanner spread CSV-append tournant en cron 10min via GitHub Actions. À garder en parallèle, ne pas toucher.
bot.py : un ancien bot Polymarket spread, hors scope.

Capital cible : 300 CHF max à terme. Donc pas de course à la latence sub-10ms.
Objectif primaire : comprendre la microstructure de l'opening HIP-4, identifier des inefficiences structurelles réplicables. Pas générer du PnL court terme. Le bot d'exécution viendra dans une phase ultérieure, quand H1..H5 (ci-dessous) seront validées sur 4-6 semaines de données.
2. Contexte protocolaire HIP-4 (vérifié par probing live, pas documenté officiellement)
Encoding asset:
- enc = 10 * outcome_id + side  (side: 0=YES, 1=NO)
- WebSocket coin: "#<enc>"
- Spot balance coin: "+<enc>"
- Order action asset int: 100_000_000 + enc

WebSocket endpoint: wss://api.hyperliquid.xyz/ws

Subscriptions disponibles:
- {"method":"subscribe","subscription":{"type":"l2Book","coin":"#<enc>"}}
- {"method":"subscribe","subscription":{"type":"trades","coin":"#<enc>"}}
- {"method":"subscribe","subscription":{"type":"bbo","coin":"#<enc>"}}
- {"method":"subscribe","subscription":{"type":"activeAssetCtx","coin":"#<enc>"}}

Info REST endpoints (https://api.hyperliquid.xyz/info):
- {"type":"outcomeMeta"} — liste des outcomes actifs + questions multi-outcome
- {"type":"allMids"} — mids keyed par #N
- {"type":"l2Book", "coin":"#<enc>"} — snapshot REST du book

Description schema priceBinary:
"class:priceBinary|underlying:BTC|expiry:YYYYMMDD-HHMM|targetPrice:X|period:1d"

Description schema priceBucket (multi-outcome via questions):
"class:priceBucket|underlying:BTC|expiry:YYYYMMDD-HHMM|priceThresholds:X,Y|period:1d"
La question référence namedOutcomes (les buckets) + fallbackOutcome.
Chaque namedOutcome a description "index:0", "index:1", etc.

Contraintes:
- szDecimals = 0 (orders en entiers de contrats)
- Price range [0.001, 0.999], 5 décimales (le SDK officiel a la mauvaise valeur)
- Sum invariant: sum(mid_YES_i) ≈ 1.0 pour les outcomes d'une même question
- Settlement = interpolation linéaire entre les 2 marks bracketant settlementTime
  (PAS un TWAP, PAS un snapshot)
- Oracle = mark price perp HyperCore du sous-jacent (zéro basis risk vs perp hedge)
- candleSnapshot ne marche QUE pour le perp sous-jacent, PAS pour les books outcome
- outcomeMeta roule : sauvegarder les specs au startup pour pouvoir résoudre après
- Pas de funding, pas de liquidation, USDH-collateralisé
- Fees sur close/burn/settle, pas sur open. Montant exact non documenté.

Référence externe utile (à lire avant de coder) :
https://github.com/zaakirio/hl-hip4-arb — voir src/hyoer/parsers.py, capture.py, 
store.py, contract.py, hip4.py pour la plomberie WS + encoding + storage.
3. Le marché observé en ce moment
Snapshot outcomeMeta actuel :
json{
  "outcomes": [
    {"outcome": 65, "name": "Recurring", 
     "description": "class:priceBinary|underlying:BTC|expiry:20260520-0600|targetPrice:76886|period:1d"},
    {"outcome": 66, "name": "Recurring Fallback", "description": "other"},
    {"outcome": 67, "name": "Recurring Named Outcome", "description": "index:0"},
    {"outcome": 68, "name": "Recurring Named Outcome", "description": "index:1"},
    {"outcome": 69, "name": "Recurring Named Outcome", "description": "index:2"}
  ],
  "questions": [
    {"question": 12, "name": "Recurring",
     "description": "class:priceBucket|underlying:BTC|expiry:20260520-0600|priceThresholds:75348,78423|period:1d",
     "fallbackOutcome": 66, "namedOutcomes": [67, 68, 69]}
  ]
}
Sémantique :

Outcome 65 (binary) : BTC > 76886 à l'expiry 2026-05-20 06:00 UTC
Question 12 (bucket à 3 issues, expiry 2026-05-20 06:00 UTC) :

idx:0 (outcome 67) : BTC < 75348
idx:1 (outcome 68) : 75348 ≤ BTC < 78423
idx:2 (outcome 69) : BTC ≥ 78423


Outcome 66 : fallback (oracle failure, devrait quote très bas)

Relation algébrique critique :
P(outcome 65 YES) = P(BTC > 76886)
                  = P(BTC ∈ [76886, 78423]) + P(idx:2)
                  ≈ une fraction de mid(idx:1) + mid(idx:2)
4. Les 5 hypothèses à instrumenter
L'observer v3 doit capturer les bonnes données pour pouvoir tester (offline, dans une phase ultérieure) :
H1 — Sum-to-1 deviation (pair-arb des 3 buckets)
À chaque tick : dev_t = mid_YES(idx:0) + mid_YES(idx:1) + mid_YES(idx:2) − 1
Hypothèse : pendant opening (T+0 à T+5min), |dev_t| est en moyenne plus grand qu'en régime stable (T+30min à T+60min).
H2 — IV implicite vs realized BTC
Calculer pour chaque tick l'IV implicite réconciliant les 3 mids du bucket avec le prix BTC perp via Black-Scholes-bucket.
Hypothèse : pendant opening, IV > realized vol EWMA 24h.
H3 — Convergence speed du middle bucket (idx:1)
Idx:1 est non-monotone par rapport au mouvement BTC.
Hypothèse : idx:1 converge vers fair value plus lentement que idx:0 et idx:2 après l'opening.
H4 — Asymétrie de réaction des 3 buckets aux mouvements BTC
Hypothèse : delta empirique vs delta modèle est plus élevé sur les buckets extrêmes (idx:0 et idx:2) pendant l'opening — sur-réaction retail.
H5 — Arb cross-marché binary ↔ bucket (le plus rare)
La relation algébrique entre outcome 65 et les buckets devrait tenir. Si elle diverge plus que 2× les fees totales, c'est un arb.
5. Spec de l'observer v3
Scope IMPÉRATIF
Ce que l'observer v3 DOIT faire :

Capturer en haute résolution les marchés HIP-4 d'opening BTC quotidien
Persister dans DuckDB pour analyse offline ultérieure
Tourner 24/7 sur VPS Tokyo, robuste aux disconnects

Ce que l'observer v3 NE DOIT PAS faire (rejette si je le demande dans cette session) :

❌ Pas de signal_engine
❌ Pas de logique d'exécution / signature / orders
❌ Pas de calcul de fair value en temps réel (ça viendra dans l'analyzer offline)
❌ Pas de paper trading
❌ Pas de dashboard live (un health check minimal suffit)
❌ Pas de notebook d'analyse (ça viendra séparément)

Si je dérive vers ces sujets pendant qu'on code, rappelle-moi le scope.
Marchés à capturer
À chaque cycle (= chaque jour), au démarrage du recorder :

Fetch outcomeMeta via REST
Identifier :

Le priceBucket BTC actif avec son expiry (parse description du questions)
Le priceBinary BTC du même expiry (parse description du outcomes)
Les 3 namedOutcomes du bucket


Subscribe en WS pour chacun :

YES coin et NO coin → l2Book + trades + bbo


Aussi : subscribe BTC perp → trades + bbo + activeAssetCtx

Cas dégénérés à gérer :

Pas de bucket actif → log warning, continuer avec ce qui existe
Bucket actif mais pas de binary correspondant → continuer avec bucket seul
Nouvelle rotation pendant l'observation → re-discover et re-subscribe

Architecture
observer/
├── recorder.py          # entry point, orchestre le tout
├── ws_client.py         # async WS client + reconnect exponential backoff
├── discovery.py         # parse outcomeMeta, identifie marchés cibles
├── parsers.py           # parsers des messages WS HL (l2Book, trades, bbo)
├── store.py             # DuckDB writes batched
├── codec.py             # encoding #N / +N / asset-int
├── health.py            # health check HTTP local
└── config.py            # constantes
Schéma DuckDB
sql-- Métadonnées de chaque cycle (= chaque jour d'observation)
CREATE TABLE cycles (
    cycle_id        VARCHAR PRIMARY KEY,  -- 'YYYYMMDD'
    started_at      TIMESTAMP,
    bucket_question_id  INTEGER,
    bucket_expiry   TIMESTAMP,
    bucket_thresholds   VARCHAR,  -- 'X,Y'
    bucket_underlying   VARCHAR,
    binary_outcome_id   INTEGER,
    binary_target_price DOUBLE,
    binary_expiry   TIMESTAMP,
    raw_meta        VARCHAR   -- JSON snapshot complet pour reproductibilité
);

-- Mapping outcome → coin pour ce cycle
CREATE TABLE outcomes_map (
    cycle_id        VARCHAR,
    outcome_id      INTEGER,
    role            VARCHAR,  -- 'bucket_idx_0', 'bucket_idx_1', 'bucket_idx_2', 'binary'
    yes_coin        VARCHAR,  -- '#670'
    no_coin         VARCHAR,  -- '#671'
    description     VARCHAR,
    PRIMARY KEY (cycle_id, outcome_id)
);

-- L2 book updates (1 row par level par snapshot)
CREATE TABLE book_levels (
    ts_local        TIMESTAMP,    -- timestamp local de réception
    ts_remote       TIMESTAMP,    -- ts du message HL si dispo (sinon NULL)
    coin            VARCHAR,
    side            VARCHAR,      -- 'bid' ou 'ask'
    level_idx       INTEGER,      -- 0 = best, 1 = second best, etc.
    px              DOUBLE,
    sz              DOUBLE,
    n_orders        INTEGER       -- si l'API le fournit
);
CREATE INDEX book_levels_idx ON book_levels (coin, ts_local);

-- Trades (1 row par fill)
CREATE TABLE trades (
    ts_local        TIMESTAMP,
    ts_remote       TIMESTAMP,
    coin            VARCHAR,
    px              DOUBLE,
    sz              DOUBLE,
    side            VARCHAR,      -- 'B' (buy aggressor) ou 'A' (sell aggressor) si HL fournit, sinon NULL
    tid             VARCHAR       -- trade id si dispo
);
CREATE INDEX trades_idx ON trades (coin, ts_local);

-- BBO snapshots (best bid/best ask compact)
CREATE TABLE bbo (
    ts_local        TIMESTAMP,
    ts_remote       TIMESTAMP,
    coin            VARCHAR,
    bid_px          DOUBLE,
    bid_sz          DOUBLE,
    ask_px          DOUBLE,
    ask_sz          DOUBLE
);
CREATE INDEX bbo_idx ON bbo (coin, ts_local);

-- BTC perp context (mark, oracle, funding si applicable)
CREATE TABLE perp_ctx (
    ts_local        TIMESTAMP,
    coin            VARCHAR,      -- 'BTC'
    mark_px         DOUBLE,
    mid_px          DOUBLE,
    oracle_px       DOUBLE
);

-- Métriques de santé du recorder
CREATE TABLE health_log (
    ts              TIMESTAMP,
    ws_connected    BOOLEAN,
    n_subs_active   INTEGER,
    msgs_per_sec    DOUBLE,
    buffer_size     INTEGER,
    last_db_flush   TIMESTAMP
);
Discipline d'observabilité

Log structuré JSONL dans logs/recorder_YYYYMMDD.jsonl
Health endpoint HTTP local sur 127.0.0.1:8765/health retournant les compteurs en cours
Heartbeat toutes les 60s dans le log avec : msgs reçus / msgs écrits DB / latence WS moyenne
Alerte console si aucun message reçu pendant 30s
Mesurer latence (ts_local - ts_remote) sur les messages qui ont un ts remote, logger la moyenne par minute

Robustesse

Reconnect WS avec exponential backoff (1s, 2s, 4s, ... max 30s)
Re-subscribe automatique après reconnect
Batch DB writes : flush toutes les 1s ou tous les 1000 events
Tous les écrits DB dans un thread/task séparé pour ne jamais bloquer le WS
Si DB lock → retry avec backoff
Catch tout au niveau du dispatcher : une subscription qui crash ne tue pas le recorder

Stack technique

Python 3.11+
asyncio + websockets pour le WS
duckdb pour le storage
aiohttp pour le health endpoint et les calls REST async
python-dotenv pour la config
pytest pour les tests

Tests minimaux requis

test_codec.py : encoder/decoder #N / +N / asset-int round-trip
test_parsers.py : parser une payload WS de chaque type (fixtures statiques)
test_discovery.py : parser un outcomeMeta avec bucket + binary
test_store.py : insert + read back depuis DuckDB en mémoire

6. Comment on travaille ensemble
Phase 1 — Setup
Tu lis mon repo existant (bot_v2.py, hip4_scanner.py) avant d'écrire quoi que ce soit. Tu me confirmes que tu as compris l'archi actuelle et tu me proposes l'arborescence finale observer/.
Phase 2 — Fondations
Tu codes dans cet ordre, en t'arrêtant à chaque étape pour validation :

codec.py + ses tests
parsers.py + ses tests (avec fixtures statiques)
discovery.py + ses tests
store.py + ses tests (DuckDB en mémoire)

Phase 3 — Intégration
5. ws_client.py (le morceau le plus délicat — reconnect, multiplex subs, dispatch)
6. recorder.py (orchestration)
7. health.py
8. Smoke test : lance le recorder pendant 60s en testnet ou mainnet hors opening, vérifie que la DB se remplit
Phase 4 — Run réel
9. README spécifique à observer/ (comment lancer, config, monitoring)
10. systemd service file pour le VPS Tokyo
11. Documentation des queries d'analyse offline que je pourrai lancer sur les données
7. Règles de communication

Si je dérive vers du signal / exécution / analyse, refuse poliment et rappelle-moi qu'on est en phase observer pure.
Si tu vois une ambiguïté dans la spec, demande-moi avant de coder.
Si tu as une opinion divergente sur un choix d'archi, dis-le franchement, justifie. Je préfère un push-back argumenté qu'un yes-man.
Avant de coder un fichier, annonce ce que tu vas y mettre en 3-5 lignes et attends que je dise go.
Ne refais pas l'archi existante de SpreadWatch. L'observer v3 cohabite dans le même repo mais dans son propre dossier observer/. hip4_scanner.py et bot.py restent intacts.

8. Première action attendue de toi

Lis bot_v2.py et hip4_scanner.py dans le repo.
Confirme-moi que tu as compris :

Ce que fait bot_v2.py aujourd'hui (et pourquoi on le remplace)
Ce que fait hip4_scanner.py (et pourquoi on le garde intact)
Les 5 hypothèses qu'on va tester offline plus tard
Le scope strict (recorder pur, rien d'autre)


Propose-moi l'arborescence finale observer/ et la liste de fichiers à créer.
Pose-moi 3-5 questions de clarification si quelque chose te paraît ambigu.

N'écris pas une ligne de code avant que je valide ton plan.
