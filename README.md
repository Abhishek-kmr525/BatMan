# AMTA — AI Master Trading Agent

Autonomous Kalshi paper trading agent. $1 per trade, $10,000 starting balance, never real money.

## Architecture (simplified 2-service stack)

- **backend/** — Python FastAPI: Kalshi client, ChromaDB knowledge base, Claude analysis, bot loop, REST + WebSocket. SQLite for persistence.
- **frontend/** — Next.js 14 dashboard with live updates over WebSocket.

## Quick start

```bash
# 1. backend
cd backend
./run.sh                    # creates .venv, installs deps, runs on :4000

# 2. frontend (new terminal)
cd frontend
npm install
npm run dev                 # runs on :3000
```

Open http://localhost:3000.

## First-run flow

1. Backend boots → creates `data/amta.db`, seeds wallet at $10,000.
2. From the dashboard, click **Reload PDFs** to ingest the knowledge base (one-time, takes a few minutes).
3. Click **▶ Start Bot**. The bot scans markets every 30s, enriches each market with external intel (GDELT, Guardian, FRED, BLS), scores with local/Claude analyzer, and opens $1 positions on markets scoring ≥ 65.
4. Open positions appear live; trades close on take-profit / stop-loss / time-exit. Wallet updates each tick.

## Configuration

Edit `.env` at the repo root. Key flags:

- `KALSHI_DEMO=true` — runs against the built-in mock market generator (no Kalshi creds required). Set to `false` and fill Kalshi credentials.
- `KNOWLEDGE_PDF_DIR` — path to your PDFs.
- `MIN_TRADE_SCORE`, `MAX_CONCURRENT_POSITIONS`, `BOT_SCAN_INTERVAL_SECONDS` — agent tuning.
- `INTEL_FEATURES_ENABLED`, `INTEL_STRICT_SKIP` — enable/disable external intel gating.
- `GUARDIAN_API_KEY` defaults to `test`; `FRED_API_KEY` is optional but required for FRED signals.
- `QUICK_EXPIRY_ALWAYS_ON=true` with `QUICK_EXPIRY_MIN_SECONDS` and `QUICK_EXPIRY_MAX_SECONDS` constrains trading to a time window (e.g. 60..86400 seconds).
- `POLYMARKET_*` variables control the separate phase-1 Polymarket paper bot (page: `/polymarket`).
- Phase-2 safety APIs: `GET /api/risk/limits` and `GET /api/risk/reconcile` (paper wallet consistency + shared risk guard visibility).
- Phase-3 arming APIs:
  - `GET /api/mode/status`
  - `POST /api/mode/request-live`
  - `POST /api/mode/confirm-live`
  - `POST /api/mode/set-paper`
  - `POST /api/mode/kill-switch`
- Phase-4 canary safety API: `GET /api/risk/canary-status` with strict `LIVE_CANARY_*` per-platform limits.

## API

See `backend/app/api/routes.py`. WebSocket at `ws://localhost:4000/api/ws` emits `bot:status`, `trade:opened`, `trade:closed`, `wallet:updated`, `agent:log`, `market:scan`.

## Status vs spec (files/01-09)

- ✅ AI agent (RAG + Claude scoring)
- ✅ Paper wallet ($10k seed, deposit, P&L, win rate)
- ✅ Bot state machine (IDLE → SCANNING → ANALYZING → EXECUTING → MONITORING)
- ✅ Trade executor ($1 buy, take-profit / stop-loss / time-exit)
- ✅ Live dashboard (wallet, status, P&L chart, open + closed tables, log feed, start/stop, deposit)
- 🔌 Kalshi client implemented (paper mode ready)
- 🟡 Strategy Marketplace, Quick Trade — P1
- 🟡 Deployment (Vercel + Railway)
