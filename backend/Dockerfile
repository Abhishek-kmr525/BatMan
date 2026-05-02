FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/.hf

WORKDIR /app

# Build deps for chromadb / sentence-transformers wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app

# Persistent data paths (attach a Railway Volume at /data in production).
RUN mkdir -p /data /data/knowledge

ENV DATABASE_URL=sqlite+aiosqlite:////data/amta.db \
    CHROMA_DIR=/data/chroma \
    KNOWLEDGE_PDF_DIR=/data/knowledge \
    KALSHI_PRIVATE_KEY_PATH=/data/kalshi_key.pem \
    PORT=4000

EXPOSE 4000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-4000}"]
