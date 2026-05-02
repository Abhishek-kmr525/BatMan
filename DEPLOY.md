# Deploying AMTA

Three deploy options. Pick one.

---

## A. Local — docker-compose (one command)

Build and run both services:

```bash
docker compose up --build
```

- Backend: http://localhost:4000
- Frontend: http://localhost:3000

Persists SQLite + Chroma + the embedding model in the `amta-data` volume.
Mounts your PDF folder at `KNOWLEDGE_PDF_DIR` (defaults to `./knowledge`)
and your `backend/data/kalshi_key.pem` into the container.

To stop: `docker compose down`. To wipe state: `docker compose down -v`.

---

## B. Cloud — Railway (backend) + Vercel (frontend)

The recommended split per the original spec.

### Backend → Railway

1. `cd backend && railway init` (or via the Railway dashboard, point a new
   project at this repo, root directory `backend`).
2. Railway auto-detects `Dockerfile` and `railway.json`.
3. Add a **Volume** mounted at `/data` (10 GB is plenty).
4. Set environment variables (Variables tab — copy from `.env.example`):
   - `ANTHROPIC_API_KEY`
   - `KALSHI_KEY_ID`
   - `KALSHI_BASE_URL` (production: `https://api.elections.kalshi.com/trade-api/v2`)
   - `KALSHI_PAPER_MODE=true` (keep this on unless you really mean to fire real orders)
   - `EMBEDDING_PROVIDER=local`
   - `STARTING_BALANCE=10000`
   - all the others from `.env.example`
5. Upload your `kalshi_key.pem` — easiest path is to base64 it and stash in
   `KALSHI_PRIVATE_KEY_B64`, then add a small startup hook that decodes it
   to `/data/kalshi_key.pem` (Railway doesn't have native file mounts).
6. Upload your knowledge PDFs to a bucket (S3/R2) and adjust
   `KNOWLEDGE_PDF_DIR`, OR include them in the Docker image by adding a
   `COPY ./knowledge /knowledge` to `backend/Dockerfile`.
7. Deploy. Health check is `GET /` returning `{"status":"ok"}`.

### Frontend → Vercel

1. `cd frontend && vercel` (or import the repo at vercel.com, set the
   root directory to `frontend`).
2. Vercel auto-detects Next.js via `vercel.json`.
3. Add environment variables:
   - `NEXT_PUBLIC_API_URL` → your Railway backend URL (e.g.
     `https://amta-backend.up.railway.app`)
   - `NEXT_PUBLIC_WS_URL` → `wss://amta-backend.up.railway.app/api/ws`
4. Deploy.

### Open the Railway backend's CORS

The current `main.py` allows `*` origins, so Vercel can call Railway out
of the box. Tighten before going public.

---

## C. Single-server (any VPS with Docker)

```bash
git clone <your-fork> amta && cd amta
cp .env.example .env  # fill in keys
mkdir -p ./knowledge && cp /path/to/*.pdf ./knowledge/
cp /path/to/kalshi_key.pem ./backend/data/
docker compose up -d --build
```

Reverse-proxy 3000 / 4000 with nginx/Caddy if you want HTTPS.

---

## Production notes

- **Paper mode is the default.** `KALSHI_PAPER_MODE=true` short-circuits
  real order placement. Flip to `false` only when you're ready to send
  real money to Kalshi.
- **Backups.** Snapshot the `/data` volume — losing `amta.db` loses your
  trade history; losing `chroma/` means re-ingesting all PDFs.
- **Costs.** Anthropic credit usage scales with markets analyzed per tick.
  Sonnet at ~600 output tokens per analysis is roughly $0.005–0.01 per
  market. With the default 30s scan and ~10 candidates, plan ~$3/day.
  Local embeddings cost nothing.
- **Rotation.** Anything pasted into chat/PRs/screenshots is leaked.
  Rotate the Anthropic, OpenAI, and Kalshi keys before going live.
