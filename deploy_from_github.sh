#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Abhishek-kmr525/BatMan.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/amta/repo}"

echo "[deploy] repo=${REPO_URL} branch=${BRANCH} app_dir=${APP_DIR}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "[deploy] first-time clone"
  sudo mkdir -p "$(dirname "${APP_DIR}")"
  sudo chown -R "$(id -u)":"$(id -g)" "$(dirname "${APP_DIR}")"
  git clone "${REPO_URL}" "${APP_DIR}"
fi

cd "${APP_DIR}"
git fetch --all --prune
git checkout "${BRANCH}"
git reset --hard "origin/${BRANCH}"

if [[ ! -f ".env" && -f ".env.example" ]]; then
  cp .env.example .env
  echo "[deploy] .env was missing, created from .env.example (fill secrets before live bot start)"
fi

echo "[deploy] docker compose build/up"
docker compose down || true
docker compose up -d --build

echo "[deploy] status"
docker compose ps

echo "[deploy] local health checks"
curl -fsS http://127.0.0.1:4000/api/polymarket/live/preflight || true
curl -fsS "http://127.0.0.1:4000/api/polymarket/logs?mode=live&limit=30" || true

echo "[deploy] done"
