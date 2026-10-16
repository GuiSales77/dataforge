"""Rotas da aplicação — independentes de transporte (servidor local ou navegador)."""
import base64
import threading
import time
import uuid
from pathlib import Path

from . import agents, config, exporter, nlp, operations, profiler, storage

SESSIONS = {}
_slock = threading.Lock()


def _session(sid=None):
    with _slock:
        if not sid or sid not in SESSIONS:
            sid = uuid.uuid4().hex[:12]
            SESSIONS[sid] = {"datasets": {}, "profiles": {}, "files": {}, "criado": time.time()}
        return sid, SESSIONS[sid]


def _ds_payload(alias, df, prof, cfg):
    return {**prof, "preview_colunas": list(df.columns),
            "preview": profiler.preview_records(df, 8)}


def _cleanup_uploads(cfg):
    days = int(cfg.get("reter_uploads_dias", 7))
    limit = time.time() - days * 86400
    for d in (config.UPLOAD_DIR,):
        for f in d.glob("*"):
            try:
                if f.stat().st_mtime < limit:
                    f.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
def api_upload(body):
    cfg = config.load()
    sid, ses = _session(body.get("sid"))
    added, errors = [], []
    for f in body.get("files", []):
        name = f.get("name", "arquivo.xlsx")
        try:
            raw = base64.b64decode(f["data"].split(",", 1)[-1])
            path = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{nlp.slug(Path(name).stem)}{Path(name).suffix.lower()}"
            path.write_bytes(raw)
            for alias, df in profiler.read_file(path, name, cfg.get("limpar_espacos_auto", True)):
                base, i = alias, 2
                while alias in ses["datasets"]:
                    alias, i = f"{base}_{i}", i + 1
                prof = profiler.profile(df, alias, cfg, name)
                prof["tamanho"] = len(raw)
                ses["datasets"][alias] = df
                ses["profiles"][alias] = prof
                ses["files"][alias] = name
                added.append(_ds_payload(alias, df, prof, cfg))
            storage.audit("upload", {"arquivo": name, "bytes": len(raw)})
        except Exception as e:
            errors.append({"arquivo": name, "erro": str(e)})
    return {"sid": sid, "adicionados": added, "erros": errors,
            "todas": [a for a in ses["datasets"]]}


def api_rename(body):
    sid, ses = _session(body.get("sid"))
    old, new = body.get("old"), nlp.slug(body.get("new", ""), 40)
    if old not in ses["datasets"] or not new:
        return {"erro": "planilha não encontrada"}, 400
    if new in ses["datasets"] and new != old:
        return {"erro": "já existe uma planilha com esse nome"}, 400
    for k in ("datasets", "profiles", "files"):
        ses[k] = {(new if a == old else a): v for a, v in ses[k].items()}
    ses["profiles"][new]["alias"] = new
    return {"ok": True, "alias": new}


def api_remove(body):
    sid, ses = _session(body.get("sid"))
    for k in ("datasets", "profiles", "files"):
        ses[k].pop(body.get("alias"), None)
    return {"ok": True}


def api_plan(body):
    cfg = config.load()
    sid, ses = _session(body.get("sid"))
    cmd = (body.get("command") or "").strip()
    if not ses["datasets"]:
        return {"erro": "Envie ao menos uma planilha."}, 400
    if not cmd:
        return {"erro": "Descreva o que deseja fazer."}, 400
    override = body.get("modo")
    if override in ("local", "hibrido", "ia"):
        cfg["modo"] = override
    res = agents.Orchestrator(cfg).plan(cmd, ses["datasets"], ses["profiles"])
    ses["ultimo_plano"] = {"comando": cmd, **res}
    return res


def api_execute(body):
    cfg = config.load()
    sid, ses = _session(body.get("sid"))
    plan = body.get("plan") or {}
    meta = body.get("meta") or {}
    cmd = body.get("command") or meta.get("comando", "")
    v = agents.validate(plan, ses["datasets"])
    t0 = time.perf_counter()
    run = operations.execute({"passos": v["passos"]}, ses["datasets"])
    ms = int((time.perf_counter() - t0) * 1000)
    results = run["resultados"]
    errs = [e for e in run["log"] if e["status"] != "ok"]
    status = "ok" if results and not errs else ("parcial" if results else "erro")
    linhas_in = sum(len(ses["datasets"][a]) for a in ses["datasets"])
    jid = uuid.uuid4().hex[:12]
    out_path = ""
    if results:
        out_path = str(config.RESULT_DIR / f"resultado_{time.strftime('%Y%m%d_%H%M%S')}_{jid[:4]}.xlsx")
        exporter.export(out_path, results, {
            "comando": cmd, "estrategia": meta.get("estrategia", ""), "modelo": meta.get("modelo", ""),
            "confianca": meta.get("confianca", 0), "entradas": list(ses["datasets"]),
            "linhas_entrada": linhas_in, "ms": ms}, run["log"], cfg)
    uso = meta.get("uso") or {}
    storage.add_job(
        id=jid, comando=cmd,
        planilhas=[{"alias": a, "arquivo": ses["files"].get(a, ""), "linhas": len(df), "colunas": list(df.columns)}
                   for a, df in ses["datasets"].items()],
        estrategia=meta.get("estrategia", "manual"), modelo=meta.get("modelo", ""), confianca=meta.get("confianca", 0),
        plano={"passos": v["passos"]}, status="ok" if status == "ok" else status,
        linhas_entrada=linhas_in, linhas_saida=sum(len(x) for x in results.values()), ms=ms,
        arquivo_resultado=out_path, tokens_entrada=uso.get("tokens_entrada", 0), tokens_saida=uso.get("tokens_saida", 0),
        custo_usd=uso.get("custo_usd", 0), erros=[e.get("erro") for e in errs])
    if meta.get("memoria_job"):
        storage.bump_reuse(meta["memoria_job"])
    storage.audit("execucao", {"job": jid, "status": status, "passos": len(v["passos"])})
    n_prev = int(cfg.get("linhas_preview", 50))
    return {
        "job_id": jid, "status": status, "ms": ms, "log": run["log"], "avisos": v["avisos"],
        "linhas_entrada": linhas_in,
        "resultados": [{"alias": k, "linhas": len(df), "colunas": list(df.columns),
                        "preview": profiler.preview_records(df, n_prev)} for k, df in results.items()],
        "download": jid if out_path else None,
    }


def api_settings_get(_=None):
    return config.public(config.load())


def api_settings_post(body):
    upd = dict(body)
    if "api_key" in upd and not upd["api_key"]:
        upd.pop("api_key")
    if upd.get("remover_api_key"):
        upd["api_key"] = ""
    cfg = config.save(upd)
    storage.audit("config", {k: ("***" if k == "api_key" else v) for k, v in body.items()})
    return config.public(cfg)


def api_test_ai(body):
    cfg = config.load()
    if body.get("api_key"):
        cfg["api_key"] = body["api_key"]
    agent = agents.AIAgent(cfg)
    if not agent.available():
        return {"ok": False, "mensagem": "Informe a chave, o endpoint e pelo menos um modelo."}
    try:
        t0 = time.perf_counter()
        model = cfg.get("modelo_simples") or cfg.get("modelo_complexo")
        r = agent.ping(model)
        return {"ok": True, "mensagem": f"Conectado ({model}) em {time.perf_counter() - t0:.1f}s: {r.strip()[:40]}"}
    except Exception as e:
        return {"ok": False, "mensagem": str(e)}


def api_history(_=None):
    jobs = storage.list_jobs()
    for j in jobs:
        j["plano_desc"] = [operations.describe(s) for s in (j.get("plano") or {}).get("passos", [])]
        j["tem_arquivo"] = bool(j.get("arquivo_resultado")) and Path(j["arquivo_resultado"]).exists()
    return {"jobs": jobs, "stats": storage.stats()}


def api_feedback(body):
    storage.set_feedback(body["job_id"], int(body.get("valor", 0)))
    return {"ok": True}


def api_forget(body):
    storage.delete_job(body["job_id"])
    return {"ok": True}


def api_reuse(body):
    job = storage.get_job(body["job_id"])
    if not job:
        return {"erro": "não encontrado"}, 404
    return {"comando": job["comando"], "passos": job["plano"].get("passos", [])}


def api_operations(_=None):
    return {"operacoes": operations.catalog()}


POST = {"/api/upload": api_upload, "/api/rename": api_rename, "/api/remove": api_remove, "/api/plan": api_plan,
        "/api/execute": api_execute, "/api/settings": api_settings_post, "/api/test_ai": api_test_ai,
        "/api/feedback": api_feedback, "/api/forget": api_forget, "/api/reuse": api_reuse}
GET = {"/api/settings": api_settings_get, "/api/history": api_history, "/api/operations": api_operations}




def download(jid):
    """Retorna (nome, bytes) do Excel de um processamento."""
    job = storage.get_job(jid)
    if not job or not job.get("arquivo_resultado") or not Path(job["arquivo_resultado"]).exists():
        return None, None
    p = Path(job["arquivo_resultado"])
    storage.audit("download", {"job": jid})
    return p.name, p.read_bytes()


def api_download_b64(body):
    name, data = download(body.get("job_id", ""))
    if not data:
        return {"erro": "arquivo não encontrado"}, 404
    return {"nome": name, "data": base64.b64encode(data).decode("ascii")}


POST["/api/download"] = api_download_b64
_ready = False


def dispatch(method, path, body=None):
    """Ponto único de entrada: retorna (status, payload)."""
    global _ready
    if not _ready:
        storage.init()
        _cleanup_uploads(config.load())
        _ready = True
    table = GET if method == "GET" else POST
    fn = table.get(path)
    if not fn:
        return 404, {"erro": "rota não encontrada"}
    res = fn(body or {}) if method == "POST" else fn()
    if isinstance(res, tuple):
        return res[1], res[0]
    return 200, res
