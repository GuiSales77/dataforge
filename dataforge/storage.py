"""Agente de Memória: histórico, aprendizado com feedback e busca de contexto similar (SQLite)."""
import json
import sqlite3
import threading
import time
import uuid

from . import nlp
from .config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    criado REAL,
    comando TEXT,
    comando_tokens TEXT,
    planilhas TEXT,          -- [{alias, arquivo, linhas, colunas:[...]}]
    estrategia TEXT,
    modelo TEXT,
    confianca REAL,
    plano TEXT,
    status TEXT,
    linhas_entrada INTEGER,
    linhas_saida INTEGER,
    ms INTEGER,
    arquivo_resultado TEXT,
    feedback INTEGER DEFAULT 0,
    reutilizado INTEGER DEFAULT 0,
    tokens_entrada INTEGER DEFAULT 0,
    tokens_saida INTEGER DEFAULT 0,
    custo_usd REAL DEFAULT 0,
    erros TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_criado ON jobs(criado DESC);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criado REAL, acao TEXT, detalhes TEXT
);
"""


def _conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)


def audit(acao: str, detalhes: dict = None):
    with _lock, _conn() as c:
        c.execute("INSERT INTO audit(criado, acao, detalhes) VALUES (?,?,?)",
                  (time.time(), acao, json.dumps(detalhes or {}, ensure_ascii=False, default=str)))


def add_job(**kw) -> str:
    jid = kw.get("id") or uuid.uuid4().hex[:12]
    row = dict(
        id=jid, criado=time.time(), comando=kw.get("comando", ""),
        comando_tokens=" ".join(nlp.content_tokens(kw.get("comando", ""))),
        planilhas=json.dumps(kw.get("planilhas", []), ensure_ascii=False),
        estrategia=kw.get("estrategia", ""), modelo=kw.get("modelo", ""), confianca=kw.get("confianca", 0),
        plano=json.dumps(kw.get("plano", {}), ensure_ascii=False, default=str), status=kw.get("status", "ok"),
        linhas_entrada=kw.get("linhas_entrada", 0), linhas_saida=kw.get("linhas_saida", 0), ms=kw.get("ms", 0),
        arquivo_resultado=kw.get("arquivo_resultado", ""), reutilizado=kw.get("reutilizado", 0),
        tokens_entrada=kw.get("tokens_entrada", 0), tokens_saida=kw.get("tokens_saida", 0),
        custo_usd=kw.get("custo_usd", 0), erros=json.dumps(kw.get("erros", []), ensure_ascii=False),
    )
    with _lock, _conn() as c:
        c.execute(f"INSERT INTO jobs({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    return jid


def set_feedback(jid: str, value: int):
    with _lock, _conn() as c:
        c.execute("UPDATE jobs SET feedback=? WHERE id=?", (int(value), jid))


def bump_reuse(jid: str):
    with _lock, _conn() as c:
        c.execute("UPDATE jobs SET reutilizado = reutilizado + 1 WHERE id=?", (jid,))


def get_job(jid: str):
    with _conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return _row(r) if r else None


def delete_job(jid: str):
    with _lock, _conn() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))


def _row(r):
    d = dict(r)
    for k in ("planilhas", "plano", "erros"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else ([] if k != "plano" else {})
        except Exception:
            pass
    return d


def list_jobs(limit: int = 200):
    with _conn() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY criado DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def stats():
    with _conn() as c:
        r = c.execute("""SELECT COUNT(*) total,
            SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok,
            SUM(CASE WHEN date(criado,'unixepoch','localtime')=date('now','localtime') THEN 1 ELSE 0 END) hoje,
            SUM(CASE WHEN feedback>0 THEN 1 ELSE 0 END) positivos,
            SUM(CASE WHEN feedback<0 THEN 1 ELSE 0 END) negativos,
            SUM(custo_usd) custo, SUM(tokens_entrada) tin, SUM(tokens_saida) tout,
            SUM(linhas_entrada) linhas,
            AVG(ms) ms_medio
            FROM jobs""").fetchone()
        est = c.execute("SELECT estrategia, COUNT(*) n FROM jobs GROUP BY estrategia").fetchall()
    d = {k: (r[k] or 0) for k in r.keys()}
    d["por_estrategia"] = {e["estrategia"]: e["n"] for e in est}
    return d


# ---------------------------------------------------------------------------
# Busca de contexto: encontra o plano passado mais parecido e o adapta
# ---------------------------------------------------------------------------
def _col_sim(a: list, b: list) -> float:
    return nlp.jaccard([nlp.canon(x) for x in a], [nlp.canon(x) for x in b])


def find_similar(command: str, current: dict, min_text: float = 0.5):
    """current: {alias: [colunas]}. Retorna lista de candidatos ordenados por score."""
    toks = nlp.content_tokens(command)
    with _conn() as c:
        rows = c.execute("SELECT * FROM jobs WHERE status='ok' AND feedback >= 0 ORDER BY criado DESC LIMIT 500").fetchall()
    out = []
    for r in rows:
        job = _row(r)
        tsim = nlp.jaccard(toks, (job["comando_tokens"] or "").split())
        if nlp.canon(job["comando"]) == nlp.canon(command):
            tsim = 1.0
        if tsim < min_text:
            continue
        old = {p["alias"]: p.get("colunas", []) for p in job["planilhas"] if isinstance(p, dict)}
        if len(old) != len(current) and len(old) > len(current):
            continue
        # mapeia planilhas antigas -> atuais pela semelhança de estrutura (colunas) e nome
        mapping, ssum, used = {}, 0.0, set()
        for oa, ocols in old.items():
            best, bs = None, -1
            for ca, ccols in current.items():
                if ca in used:
                    continue
                s = 0.75 * _col_sim(ocols, ccols) + 0.25 * nlp.ratio(nlp.canon(oa), nlp.canon(ca))
                if s > bs:
                    best, bs = ca, s
            if best is None:
                break
            mapping[oa] = best
            used.add(best)
            ssum += bs
        if len(mapping) != len(old):
            continue
        ssim = ssum / max(1, len(old))
        if ssim < 0.5:
            continue
        fb = job.get("feedback", 0)
        score = 0.7 * tsim + 0.3 * ssim + (0.05 if fb > 0 else 0) + min(0.03, 0.01 * job.get("reutilizado", 0))
        out.append({"job": job, "score": round(min(score, 1.0), 3), "texto": round(tsim, 3),
                    "estrutura": round(ssim, 3), "mapa": mapping})
    out.sort(key=lambda x: -x["score"])
    return out[:5]
