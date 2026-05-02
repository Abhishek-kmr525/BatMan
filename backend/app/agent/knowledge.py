"""PDF ingestion + ChromaDB vector store + RAG retrieval."""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import chromadb
from chromadb.config import Settings as ChromaSettings
from pypdf import PdfReader

from app.core.config import settings

_COLLECTION = "amta_knowledge"
_CHUNK_TOKENS = 500           # ~ characters / 4
_CHUNK_CHARS = _CHUNK_TOKENS * 4
_CHUNK_OVERLAP = 200

_client: chromadb.PersistentClient | None = None
_oai = None
_st_model = None


def _clear_chroma_cache() -> None:
    try:
        from chromadb.api.client import SharedSystemClient
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass


def _chroma() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        os.makedirs(settings.CHROMA_DIR, exist_ok=True)
        try:
            _client = chromadb.PersistentClient(
                path=settings.CHROMA_DIR,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        except Exception:
            # Self-heal corrupted/incomplete Chroma sqlite state.
            _clear_chroma_cache()
            shutil.rmtree(settings.CHROMA_DIR, ignore_errors=True)
            os.makedirs(settings.CHROMA_DIR, exist_ok=True)
            _client = chromadb.PersistentClient(
                path=settings.CHROMA_DIR,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
    return _client


def _openai():
    global _oai
    if _oai is None:
        from openai import OpenAI
        _oai = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _oai


def _sentence_transformer():
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer(settings.EMBEDDING_MODEL)
    return _st_model


def _hash_embed(text: str, dims: int = 256) -> list[float]:
    """Lightweight deterministic fallback embedding when local ML libs are unavailable."""
    vec = [0.0] * dims
    for tok in re.findall(r"[a-z0-9_]+", text.lower()):
        idx = hash(tok) % dims
        vec[idx] += 1.0
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def _collection():
    return _chroma().get_or_create_collection(_COLLECTION)


def _chunk_text(text: str) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i : i + _CHUNK_CHARS])
        i += _CHUNK_CHARS - _CHUNK_OVERLAP
    return out


def _read_pdf(path: Path) -> list[tuple[int, str]]:
    pages: list[tuple[int, str]] = []
    try:
        reader = PdfReader(str(path))
    except Exception as e:
        print(f"[knowledge] failed to open {path}: {e}")
        return pages
    for idx, page in enumerate(reader.pages, start=1):
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        if txt.strip():
            pages.append((idx, txt))
    return pages


def _embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if settings.EMBEDDING_PROVIDER == "openai":
        resp = _openai().embeddings.create(model=settings.EMBEDDING_MODEL, input=texts)
        return [d.embedding for d in resp.data]
    # local sentence-transformers
    try:
        embs = _sentence_transformer().encode(texts, normalize_embeddings=True)
        return [list(map(float, e)) for e in embs]
    except Exception:
        # Deployment-safe fallback for environments without heavy local ML deps.
        return [_hash_embed(t) for t in texts]


def list_pdfs(root: str) -> list[Path]:
    p = Path(root)
    if not p.exists():
        return []
    return sorted(p.rglob("*.pdf"))


def save_uploaded_pdfs(files: list[tuple[str, bytes]]) -> dict:
    """Persist uploaded PDF blobs into KNOWLEDGE_PDF_DIR."""
    root = Path(settings.KNOWLEDGE_PDF_DIR or "").expanduser()
    if not str(root).strip():
        raise ValueError("KNOWLEDGE_PDF_DIR is not configured")
    root.mkdir(parents=True, exist_ok=True)

    saved = []
    skipped = []
    for name, blob in files:
        clean = Path(name).name
        if not clean.lower().endswith(".pdf"):
            skipped.append({"file": clean, "reason": "not a pdf"})
            continue
        target = root / clean
        with open(target, "wb") as f:
            f.write(blob)
        saved.append(str(target))
    return {"saved": saved, "skipped": skipped, "dir": str(root)}


def ingest_directory(root: str | None = None) -> dict:
    root = root or settings.KNOWLEDGE_PDF_DIR
    coll = _collection()
    pdfs = list_pdfs(root)
    total_chunks = 0
    file_summaries = []
    for pdf in pdfs:
        rel = str(pdf)
        existing = coll.get(where={"source_file": rel}, limit=1)
        if existing and existing.get("ids"):
            file_summaries.append({"file": pdf.name, "chunks": "skipped (already ingested)"})
            continue
        pages = _read_pdf(pdf)
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict] = []
        for page_no, txt in pages:
            for j, chunk in enumerate(_chunk_text(txt)):
                ids.append(f"{pdf.name}::p{page_no}::c{j}")
                docs.append(chunk)
                metas.append({"source_file": rel, "file_name": pdf.name, "page": page_no})
        if not docs:
            file_summaries.append({"file": pdf.name, "chunks": 0})
            continue
        # Embed in batches
        batch = 64
        for k in range(0, len(docs), batch):
            embs = _embed(docs[k : k + batch])
            coll.add(
                ids=ids[k : k + batch],
                documents=docs[k : k + batch],
                metadatas=metas[k : k + batch],
                embeddings=embs,
            )
        total_chunks += len(docs)
        file_summaries.append({"file": pdf.name, "chunks": len(docs)})
    return {"files": file_summaries, "total_chunks": total_chunks, "pdfs_seen": len(pdfs)}


def reset_index() -> None:
    global _client
    # Drop in-memory client first, then wipe on-disk Chroma state so resets
    # cannot leave a partially-initialized sqlite schema behind.
    _client = None
    _clear_chroma_cache()
    chroma_path = Path(settings.CHROMA_DIR)
    if chroma_path.exists():
        shutil.rmtree(chroma_path, ignore_errors=True)
    os.makedirs(settings.CHROMA_DIR, exist_ok=True)

    # Best-effort: eagerly initialize the collection after reset.
    try:
        _chroma().get_or_create_collection(_COLLECTION)
    except Exception:
        _client = None


def query(text: str, k: int = 5) -> list[dict]:
    coll = _collection()
    if coll.count() == 0:
        return []
    embs = _embed([text])
    res = coll.query(query_embeddings=embs, n_results=k)
    out: list[dict] = []
    for i in range(len(res["ids"][0])):
        out.append(
            {
                "id": res["ids"][0][i],
                "text": res["documents"][0][i],
                "metadata": res["metadatas"][0][i],
                "distance": (res.get("distances") or [[None]])[0][i],
            }
        )
    return out


def stats() -> dict:
    coll = _collection()
    total = coll.count()
    # per-file breakdown — chroma's `get(include=['metadatas'])` over the whole
    # collection is fine at our scale (~3k chunks).
    by_file: dict[str, int] = {}
    if total > 0:
        try:
            data = coll.get(include=["metadatas"])
            for md in (data.get("metadatas") or []):
                if not md:
                    continue
                name = md.get("file_name") or md.get("source_file") or "unknown"
                by_file[name] = by_file.get(name, 0) + 1
        except Exception:
            pass
    files = sorted(
        ({"file_name": k, "chunks": v} for k, v in by_file.items()),
        key=lambda x: -x["chunks"],
    )
    return {
        "total_chunks": total,
        "files": files,
        "chroma_dir": settings.CHROMA_DIR,
        "embedding_provider": settings.EMBEDDING_PROVIDER,
        "embedding_model": settings.EMBEDDING_MODEL,
        "pdf_dir": settings.KNOWLEDGE_PDF_DIR,
    }
