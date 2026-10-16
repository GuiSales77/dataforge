"""Configuração e parâmetros do DataForge (persistidos em dados/config.json)."""
import json
import os
import sys
import threading
from pathlib import Path

VERSION = "1.0.0"


def base_dir() -> Path:
    """Pasta onde ficam os dados do usuário (ao lado do executável ou do projeto)."""
    if os.environ.get("DATAFORGE_DIR"):
        return Path(os.environ["DATAFORGE_DIR"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Pasta dos recursos empacotados (interface web)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", base_dir()))
    return Path(__file__).resolve().parent.parent


DATA_DIR = base_dir() / "dados"
UPLOAD_DIR = DATA_DIR / "uploads"
RESULT_DIR = DATA_DIR / "resultados"
DB_PATH = DATA_DIR / "dataforge.db"
CONFIG_PATH = DATA_DIR / "config.json"
WEB_DIR = resource_dir() / "web"

for _d in (DATA_DIR, UPLOAD_DIR, RESULT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Parâmetros (todos editáveis pela tela de Configurações)
# ---------------------------------------------------------------------------
DEFAULTS = {
    # --- Cérebro / roteamento ---
    "modo": "hibrido",              # local | hibrido | ia
    "limiar_confianca": 0.65,       # abaixo disso, o modo híbrido consulta a IA
    "usar_memoria": True,           # reaproveita planos que já deram certo
    "limiar_memoria": 0.82,         # similaridade mínima para reutilizar um plano
    "aprender_com_feedback": True,  # 👍/👎 ajustam o peso dos planos na memória
    "similaridade_colunas": 0.78,   # tolerância para achar colunas por nome aproximado
    "limiar_complexidade": 3,       # a partir deste score usa o modelo complexo
    # --- IA externa (opcional) ---
    "api_key": "",
    "api_url": "https://fzhzdwwaartjulmqysrs.supabase.co/functions/v1/dataforge-ia",
    "modelo_simples": "padrao",
    "modelo_complexo": "",
    "max_tokens": 2000,
    "timeout_ia": 60,
    "temperatura": 0.0,
    "preco_entrada_simples": 1.0,   # US$ por milhão de tokens
    "preco_saida_simples": 5.0,
    "preco_entrada_complexo": 3.0,
    "preco_saida_complexo": 15.0,
    # --- Privacidade (LGPD) ---
    "mascarar_dados": True,         # a IA só recebe amostras mascaradas
    "amostra_linhas": 5,
    "enviar_valores_categoria": True,  # envia valores de colunas categóricas não sensíveis
    "reter_uploads_dias": 7,
    # --- Processamento ---
    "limpar_espacos_auto": True,    # tira espaços extras na leitura
    "linhas_preview": 50,
    "max_abas_saida": 40,
    "estilizar_excel": True,
    # --- Servidor ---
    "porta": 8765,
    "abrir_navegador": True,
}

_lock = threading.Lock()
_cache = None


def load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)
        cfg = dict(DEFAULTS)
        if CONFIG_PATH.exists():
            try:
                cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except Exception:
                pass
        if not cfg.get("api_key"):
            cfg["api_key"] = os.environ.get("DATAFORGE_API_KEY", "")
        _cache = cfg
        return dict(cfg)


def save(updates: dict) -> dict:
    global _cache
    cfg = load()
    for k, v in updates.items():
        if k not in DEFAULTS:
            continue
        default = DEFAULTS[k]
        try:
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "sim", "on")
            elif isinstance(default, int):
                v = int(float(v))
            elif isinstance(default, float):
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        cfg[k] = v
    with _lock:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        _cache = cfg
    return dict(cfg)


def public(cfg: dict) -> dict:
    """Versão segura para enviar à interface (sem expor a chave)."""
    out = dict(cfg)
    key = out.pop("api_key", "")
    out["api_key_definida"] = bool(key)
    out["api_key_mascara"] = (key[:7] + "•••••" + key[-4:]) if len(key) > 12 else ""
    out["versao"] = VERSION
    return out
