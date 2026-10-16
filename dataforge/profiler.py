"""Agente Perfilador: lê planilhas, detecta tipos de coluna e gera amostras mascaradas (LGPD)."""
import csv
import io
import re
from pathlib import Path

import pandas as pd

from . import nlp

RX = {
    "cpf": re.compile(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$"),
    "cnpj": re.compile(r"^\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}$"),
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$"),
    "telefone": re.compile(r"^(\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}$"),
    "cep": re.compile(r"^\d{5}-?\d{3}$"),
}
NAME_HINTS = {
    "cpf": ("cpf",), "cnpj": ("cnpj",), "email": ("email", "e mail"),
    "telefone": ("telefone", "celular", "fone", "whatsapp", "tel"), "cep": ("cep",),
    "id": ("id", "codigo", "cod", "matricula", "chave", "protocolo", "pedido", "chassi", "vin"),
    "data": ("data", "dt", "nascimento", "cadastro", "vencimento", "emissao"),
}
SENSITIVE = {"cpf", "cnpj", "email", "telefone", "cep", "texto", "id"}
SENSITIVE_NAME = ("nome", "cliente", "endereco", "rua", "logradouro", "bairro", "obs", "rg",
                  "responsavel", "contato", "senha", "conta", "cartao")


def only_digits(v) -> str:
    return re.sub(r"\D", "", str(v))


def cpf_valid(v) -> bool:
    d = only_digits(v).zfill(11)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * (n + 1 - i) for i in range(n))
        if (s * 10 % 11) % 10 != int(d[n]):
            return False
    return True


def cnpj_valid(v) -> bool:
    d = only_digits(v).zfill(14)
    if len(d) != 14 or d == d[0] * 14:
        return False
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    w2 = [6] + w1
    for w, n in ((w1, 12), (w2, 13)):
        s = sum(int(d[i]) * w[i] for i in range(n))
        r = s % 11
        if (0 if r < 2 else 11 - r) != int(d[n]):
            return False
    return True


def to_number(series: pd.Series) -> pd.Series:
    """Converte texto em número aceitando formato brasileiro (1.234,56 / R$ 10,00 / 15%)."""
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return series.astype(float)

    def conv(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip().replace("R$", "").replace("%", "").replace(" ", "").replace(" ", "")
        if not s:
            return None
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", s) or ("," in s and "." in s and s.rfind(",") > s.rfind(".")):
            s = s.replace(".", "").replace(",", ".")
        elif "," in s and "." not in s:
            s = s.replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
            s = s.replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None

    return series.map(conv).astype(float)


def to_datetime(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    s = series.astype("string").str.strip()
    iso = s.str.match(r"^\d{4}-\d{2}-\d{2}", na=False)
    out = pd.to_datetime(s.where(~iso), errors="coerce", dayfirst=True, format="mixed") if (~iso).any() else pd.Series(pd.NaT, index=s.index)
    if iso.any():
        out = out.fillna(pd.to_datetime(s.where(iso), errors="coerce", format="mixed"))
    return out


def _name_has(name_c: str, hints) -> bool:
    toks = name_c.split()
    return any(h in toks or (len(h) > 3 and h in name_c) for h in hints)


def detect_type(series: pd.Series, name: str = "") -> str:
    name_c = nlp.canon(name)
    s = series.dropna()
    if len(s) == 0:
        return "vazio"
    if pd.api.types.is_bool_dtype(s):
        return "booleano"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "data"
    sample = s.head(800)
    if pd.api.types.is_numeric_dtype(s):
        for t in ("cpf", "cnpj", "cep", "telefone"):
            if _name_has(name_c, NAME_HINTS[t]):
                return t
        if _name_has(name_c, NAME_HINTS["id"]) and s.is_unique:
            return "id"
        return "numero"
    txt = sample.astype(str).str.strip()
    txt = txt[txt != ""]
    if len(txt) == 0:
        return "vazio"

    def rate(t):
        return txt.str.match(RX[t]).mean()

    for t in ("cnpj", "cpf", "email", "cep", "telefone"):
        if _name_has(name_c, NAME_HINTS[t]) and rate(t) >= 0.6:
            return t
    if rate("cnpj") >= 0.8:
        return "cnpj"
    if rate("cpf") >= 0.8 and txt.head(100).map(cpf_valid).mean() >= 0.7:
        return "cpf"
    if rate("email") >= 0.8:
        return "email"
    if rate("telefone") >= 0.8:
        return "telefone"
    if rate("cep") >= 0.8 and _name_has(name_c, NAME_HINTS["cep"]):
        return "cep"
    nums = to_number(txt)
    if nums.notna().mean() >= 0.9:
        if _name_has(name_c, NAME_HINTS["id"]) and txt.is_unique:
            return "id"
        return "moeda" if txt.str.contains(r"R\$", regex=True).any() else "numero"
    if txt.str.contains(r"\d", regex=True).mean() >= 0.8 and txt.str.contains(r"[/\-]", regex=True).mean() >= 0.8:
        if to_datetime(txt).notna().mean() >= 0.8:
            return "data"
    low = set(txt.str.lower().unique()[:10])
    if low and low <= {"sim", "nao", "não", "s", "n", "true", "false", "verdadeiro", "falso", "yes", "no"}:
        return "booleano"
    nun = s.astype(str).nunique()
    if _name_has(name_c, NAME_HINTS["id"]) and nun == len(s):
        return "id"
    if nun <= 40 or (len(s) >= 50 and nun / len(s) <= 0.05):
        return "categoria"
    return "texto"


def mask_value(v, tipo: str, col_name: str = ""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v)
    if tipo == "cpf":
        d = only_digits(s).zfill(11)
        return f"***.***.***-{d[-2:]}"
    if tipo == "cnpj":
        d = only_digits(s).zfill(14)
        return f"**.***.***/****-{d[-2:]}"
    if tipo == "email":
        user, _, dom = s.partition("@")
        return f"{user[:1]}***@{dom[:1]}***.{dom.rsplit('.', 1)[-1] if '.' in dom else 'com'}"
    if tipo == "telefone":
        d = only_digits(s)
        return f"(**) *****-{d[-4:]}"
    if tipo == "cep":
        return "*****-" + only_digits(s)[-3:]
    if tipo == "data" and _name_has(nlp.canon(col_name), ("nascimento", "nasc", "aniversario")):
        return "**/**/" + (str(v)[-4:] if not hasattr(v, "year") else str(v.year))
    if tipo in ("texto", "id") or _name_has(nlp.canon(col_name), SENSITIVE_NAME):
        return (s[:2] + "***") if len(s) > 2 else "***"
    return v if not hasattr(v, "isoformat") else v.isoformat()[:10]


# ---------------------------------------------------------------------------
# Leitura de arquivos
# ---------------------------------------------------------------------------
def _clean_frame(df: pd.DataFrame, strip_spaces: bool = True) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    # cabeçalho deslocado: se as colunas são "Unnamed" e a 1ª linha parece cabeçalho
    if len(df) and sum(str(c).startswith("Unnamed") for c in df.columns) >= len(df.columns) / 2:
        first = df.iloc[0]
        if first.notna().mean() >= 0.6 and all(not isinstance(x, (int, float)) or pd.isna(x) for x in first):
            df = df.iloc[1:].copy()
            df.columns = [str(x) if pd.notna(x) else f"coluna_{i+1}" for i, x in enumerate(first)]
    cols, seen = [], {}
    for i, c in enumerate(df.columns):
        c = str(c).strip() if c is not None else ""
        if not c or c.startswith("Unnamed"):
            c = f"coluna_{i+1}"
        c = re.sub(r"\s+", " ", c)
        if c in seen:
            seen[c] += 1
            c = f"{c}_{seen[c]}"
        else:
            seen[c] = 0
        cols.append(c)
    df.columns = cols
    if strip_spaces:
        for c in df.columns:
            if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
                df[c] = df[c].map(lambda v: re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v)
                df[c] = df[c].replace("", None)
    return df.reset_index(drop=True)


def _read_csv(raw: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:20000]
    try:
        sep = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        sep = ";" if sample.count(";") > sample.count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep, dtype=str, keep_default_na=True)


def read_file(path: Path, original_name: str, strip_spaces: bool = True) -> list:
    """Retorna [(alias, DataFrame)] — uma entrada por aba."""
    ext = Path(original_name).suffix.lower()
    stem = nlp.slug(Path(original_name).stem)
    raw = Path(path).read_bytes()
    frames = []
    if ext in (".csv", ".txt", ".tsv"):
        frames.append((stem, _read_csv(raw)))
    elif ext in (".xlsx", ".xlsm", ".xls", ".ods"):
        engine = None
        if ext == ".xls":
            engine = "xlrd"
        elif ext == ".ods":
            engine = "odf"
        sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, engine=engine)
        sheets = {k: v for k, v in sheets.items() if not v.dropna(how="all").empty}
        if len(sheets) <= 1:
            for _, v in sheets.items():
                frames.append((stem, v))
        else:
            for k, v in sheets.items():
                frames.append((f"{stem}_{nlp.slug(k, 20)}", v))
    elif ext == ".json":
        frames.append((stem, pd.read_json(io.BytesIO(raw))))
    else:
        raise ValueError(f"Formato não suportado: {ext}")
    return [(a, _clean_frame(f, strip_spaces)) for a, f in frames]


# ---------------------------------------------------------------------------
# Perfil
# ---------------------------------------------------------------------------
def column_types(df: pd.DataFrame) -> dict:
    return {c: detect_type(df[c], c) for c in df.columns}


def profile(df: pd.DataFrame, alias: str, cfg: dict, file_name: str = "") -> dict:
    n_sample = int(cfg.get("amostra_linhas", 5))
    mask = bool(cfg.get("mascarar_dados", True))
    cols = []
    for c in df.columns:
        s = df[c]
        tipo = detect_type(s, c)
        info = {
            "nome": c,
            "tipo": tipo,
            "nulos": int(s.isna().sum()),
            "unicos": int(s.nunique(dropna=True)),
        }
        if tipo in ("numero", "moeda"):
            n = to_number(s)
            if n.notna().any():
                info.update(minimo=float(n.min()), maximo=float(n.max()),
                            media=round(float(n.mean()), 4), soma=round(float(n.sum()), 4))
        if tipo in ("categoria", "booleano"):
            vc = s.astype(str).value_counts().head(40)
            info["valores"] = [str(k) for k in vc.index]
            info["frequencias"] = {str(k): int(v) for k, v in vc.items()}
        if tipo == "cpf":
            info["invalidos"] = int((~s.dropna().map(cpf_valid)).sum())
        if tipo == "cnpj":
            info["invalidos"] = int((~s.dropna().map(cnpj_valid)).sum())
        ex = s.dropna().head(n_sample).tolist()
        info["exemplos"] = [mask_value(v, tipo, c) for v in ex] if mask else [str(v) for v in ex]
        cols.append(info)
    return {
        "alias": alias,
        "arquivo": file_name,
        "linhas": int(len(df)),
        "colunas": cols,
        "duplicadas": int(df.duplicated().sum()),
        "linhas_vazias": int(df.isna().all(axis=1).sum()),
        "celulas_vazias": int(df.isna().sum().sum()),
    }


def preview_records(df: pd.DataFrame, n: int = 50) -> list:
    head = df.head(n)
    out = []
    for row in head.itertuples(index=False):
        rec = []
        for v in row:
            if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
                rec.append(None)
            elif hasattr(v, "isoformat"):
                rec.append(v.strftime("%d/%m/%Y") if getattr(v, "hour", 0) == 0 and getattr(v, "minute", 0) == 0 else v.strftime("%d/%m/%Y %H:%M"))
            elif isinstance(v, float):
                rec.append(int(v) if v.is_integer() else round(v, 4))
            else:
                rec.append(v if isinstance(v, (int, str, bool)) else str(v))
        out.append(rec)
    return out
