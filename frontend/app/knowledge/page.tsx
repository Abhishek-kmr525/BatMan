"use client";
import { useEffect, useState } from "react";
import { API, api } from "../../lib/api";

type Stats = {
  total_chunks: number;
  files: { file_name: string; chunks: number }[];
  chroma_dir: string;
  embedding_provider: string;
  embedding_model: string;
  pdf_dir: string;
};

type Hit = {
  id: string;
  text: string;
  metadata: { file_name?: string; page?: number; source_file?: string };
  distance: number | null;
};

type PdfRow = { path: string; name: string };

export default function KnowledgePage() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [pdfs, setPdfs] = useState<PdfRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [busyMsg, setBusyMsg] = useState<string | null>(null);
  const [query, setQuery] = useState("momentum entry signal");
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [uploadFiles, setUploadFiles] = useState<FileList | null>(null);

  async function loadStats() {
    try {
      const s = await api<Stats>("/agent/knowledge/stats");
      setStats(s);
    } catch {}
  }
  async function loadPdfs() {
    try {
      const r = await api<{ files: PdfRow[] }>("/agent/knowledge/pdfs");
      setPdfs(r.files);
    } catch {}
  }
  useEffect(() => { loadStats(); loadPdfs(); }, []);

  async function reload() {
    if (!confirm("Re-ingest all PDFs? Already-ingested files are skipped.")) return;
    setBusy(true); setBusyMsg("Re-ingesting PDFs (may take a minute)…");
    try { await api("/agent/knowledge/reload", { method: "POST" }); await loadStats(); }
    finally { setBusy(false); setBusyMsg(null); }
  }
  async function reset() {
    if (!confirm("Drop the entire vector index? You'll need to re-ingest after this.")) return;
    setBusy(true); setBusyMsg("Resetting index…");
    try { await api("/agent/knowledge/reset", { method: "POST" }); await loadStats(); }
    finally { setBusy(false); setBusyMsg(null); }
  }
  async function runQuery() {
    setBusy(true); setBusyMsg("Querying…"); setHits(null);
    try {
      const r = await api<{ hits: Hit[] }>("/agent/knowledge/query", {
        method: "POST",
        body: JSON.stringify({ text: query, k: 5 }),
      });
      setHits(r.hits);
    } finally { setBusy(false); setBusyMsg(null); }
  }

  async function uploadAndIngest() {
    if (!uploadFiles || uploadFiles.length === 0) {
      alert("Select one or more PDF files first.");
      return;
    }
    setBusy(true); setBusyMsg("Uploading PDFs…");
    try {
      const form = new FormData();
      Array.from(uploadFiles).forEach((f) => form.append("files", f));
      const r = await fetch(`${API}/api/agent/knowledge/upload`, {
        method: "POST",
        body: form,
      });
      if (!r.ok) {
        throw new Error(await r.text());
      }
      setBusyMsg("Re-ingesting uploaded PDFs…");
      await api("/agent/knowledge/reload", { method: "POST" });
      await Promise.all([loadStats(), loadPdfs()]);
      setUploadFiles(null);
      alert("Upload and re-ingest complete.");
    } catch (e: any) {
      alert(`Upload failed: ${e?.message || e}`);
    } finally {
      setBusy(false); setBusyMsg(null);
    }
  }

  const ingestedNames = new Set((stats?.files ?? []).map(f => f.file_name));
  const notIngested = pdfs.filter(p => !ingestedNames.has(p.name));

  return (
    <div className="container">
      <h1>Knowledge Base</h1>
      <div className="sub">
        ChromaDB vector store of your strategy PDFs. The analyzer queries this index before scoring each market.
      </div>

      <div className="row" style={{ marginTop: 16 }}>
        <div className="card">
          <h3>Total chunks</h3>
          <div className="big">{stats?.total_chunks ?? "—"}</div>
          <div className="sub" style={{ marginTop: 6 }}>{stats?.files.length ?? 0} files indexed</div>
        </div>
        <div className="card">
          <h3>Embedding model</h3>
          <div style={{ fontSize: 14, marginTop: 4 }}>{stats?.embedding_model ?? "—"}</div>
          <div className="sub" style={{ marginTop: 6 }}>provider: {stats?.embedding_provider}</div>
        </div>
        <div className="card">
          <h3>Actions</h3>
          <div style={{ display: "flex", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
            <button className="btn btn-start" onClick={reload} disabled={busy}>Re-ingest PDFs</button>
            <button className="btn btn-stop" onClick={reset} disabled={busy}>Reset index</button>
          </div>
          {busyMsg && <div className="sub" style={{ marginTop: 8 }}>{busyMsg}</div>}
        </div>
      </div>

      <h2>Upload PDFs</h2>
      <div className="card">
        <div className="sub" style={{ marginBottom: 8 }}>
          Upload local PDFs into the live knowledge directory, then ingest immediately.
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <input
            type="file"
            accept="application/pdf,.pdf"
            multiple
            onChange={(e) => setUploadFiles(e.target.files)}
            disabled={busy}
          />
          <button className="btn btn-start" onClick={uploadAndIngest} disabled={busy || !uploadFiles?.length}>
            Upload & ingest
          </button>
        </div>
      </div>

      <h2>Ingested files</h2>
      <div className="card" style={{ overflowX: "auto" }}>
        {(!stats || stats.files.length === 0) ? (
          <div className="sub">No files yet. Drop PDFs into <code>{stats?.pdf_dir}</code> and click Re-ingest.</div>
        ) : (
          <table>
            <thead><tr><th>File</th><th>Chunks</th></tr></thead>
            <tbody>
              {stats.files.map(f => (
                <tr key={f.file_name}>
                  <td>{f.file_name}</td>
                  <td>{f.chunks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {notIngested.length > 0 && (
        <>
          <h2>PDFs on disk not yet ingested ({notIngested.length})</h2>
          <div className="card">
            {notIngested.slice(0, 20).map(p => <div key={p.path} className="sub">{p.name}</div>)}
            <button className="btn btn-secondary" style={{ marginTop: 12 }} onClick={reload} disabled={busy}>Ingest now</button>
          </div>
        </>
      )}

      <h2>Query the index</h2>
      <div className="card">
        <div className="sub" style={{ marginBottom: 8 }}>
          Test what the analyzer sees for a given query — same path the bot uses before scoring a market.
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            type="text"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="e.g. momentum entry signal volume spike"
            style={{ flex: 1 }}
          />
          <button className="btn btn-start" onClick={runQuery} disabled={busy || !query.trim()}>Search</button>
        </div>
        {hits !== null && (
          <div style={{ marginTop: 16 }}>
            {hits.length === 0 && <div className="sub">No matches.</div>}
            {hits.map((h, i) => (
              <div key={h.id} style={{ borderTop: i ? "1px solid #232938" : "none", paddingTop: 12, marginTop: 12 }}>
                <div className="sub" style={{ fontSize: 11, marginBottom: 4 }}>
                  {h.metadata?.file_name} · p.{h.metadata?.page} · distance {h.distance?.toFixed(3)}
                </div>
                <div style={{ fontSize: 13, color: "#c0c5d0" }}>{h.text.slice(0, 480)}{h.text.length > 480 ? "…" : ""}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
