"""Agente Executor: catálogo de operações e execução segura do plano."""
import re
import time

import pandas as pd

from . import nlp
from .profiler import cnpj_valid, cpf_valid, detect_type, only_digits, to_datetime, to_number

REGISTRY = {}


def op(name, label, fase, descricao, params, multi=False):
    def deco(fn):
        REGISTRY[name] = dict(nome=name, label=label, fase=fase, descricao=descricao,
                              params=params, multi=multi, fn=fn)
        return fn
    return deco


def catalog():
    return [{k: v for k, v in o.items() if k != "fn"} for o in REGISTRY.values()]


def _cols(df, cols):
    if not cols:
        return []
    if isinstance(cols, str):
        cols = [cols]
    return [c for c in cols if c in df.columns]


def _text_cols(df):
    return [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])
            and not pd.api.types.is_datetime64_any_dtype(df[c])]


def _numeric(df, c):
    return to_number(df[c])


# ---------------------------------------------------------------------------
# Fase 0 — Limpeza
# ---------------------------------------------------------------------------
@op("remover_duplicatas", "Remover duplicatas", 0,
    "Remove linhas repetidas (considerando todas as colunas ou apenas as informadas).",
    {"colunas": "lista opcional de colunas que definem duplicidade", "manter": "primeira|ultima"})
def remover_duplicatas(inputs, p):
    df = inputs[0]
    sub = _cols(df, p.get("colunas")) or None
    keep = "last" if p.get("manter") == "ultima" else "first"
    return df.drop_duplicates(subset=sub, keep=keep).reset_index(drop=True)


@op("remover_linhas_vazias", "Remover linhas vazias", 0,
    "Remove linhas totalmente vazias, ou vazias nas colunas informadas.",
    {"colunas": "lista opcional de colunas obrigatórias"})
def remover_linhas_vazias(inputs, p):
    df = inputs[0]
    cols = _cols(df, p.get("colunas"))
    if cols:
        return df.dropna(subset=cols).reset_index(drop=True)
    return df.dropna(how="all").reset_index(drop=True)


@op("remover_espacos", "Remover espaços extras", 0,
    "Tira espaços no início/fim e espaços duplicados dos textos.", {"colunas": "opcional"})
def remover_espacos(inputs, p):
    df = inputs[0].copy()
    for c in _cols(df, p.get("colunas")) or _text_cols(df):
        df[c] = df[c].map(lambda v: re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v)
    return df


@op("padronizar_texto", "Padronizar texto", 0,
    "Converte textos para MAIÚSCULAS, minúsculas ou Título.",
    {"colunas": "opcional (padrão: colunas de texto)", "caixa": "maiuscula|minuscula|titulo",
     "remover_acentos": "true|false"})
def padronizar_texto(inputs, p):
    df = inputs[0].copy()
    caixa = p.get("caixa", "maiuscula")
    cols = _cols(df, p.get("colunas"))
    if not cols:
        skip = {"email", "cpf", "cnpj", "telefone", "cep", "data", "numero", "moeda", "id"}
        cols = [c for c in _text_cols(df) if detect_type(df[c], c) not in skip]
    for c in cols:
        def f(v):
            if not isinstance(v, str):
                return v
            if p.get("remover_acentos") in (True, "true", "sim"):
                v = nlp.strip_accents(v)
            if caixa == "minuscula":
                return v.lower()
            if caixa == "titulo":
                return " ".join(w if w.lower() in ("de", "da", "do", "das", "dos", "e") else w.capitalize()
                                for w in v.lower().split())
            return v.upper()
        df[c] = df[c].map(f)
    return df


def _fmt_doc(v, tipo):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return v
    d = only_digits(v)
    if tipo == "cpf" and d:
        d = d.zfill(11)
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:11]}" if len(d) == 11 else v
    if tipo == "cnpj" and d:
        d = d.zfill(14)
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:14]}" if len(d) == 14 else v
    if tipo == "telefone":
        if d.startswith("55") and len(d) in (12, 13):
            d = d[2:]
        if len(d) == 11:
            return f"({d[:2]}) {d[2:7]}-{d[7:]}"
        if len(d) == 10:
            return f"({d[:2]}) {d[2:6]}-{d[6:]}"
        return v
    if tipo == "cep" and len(d) == 8:
        return f"{d[:5]}-{d[5:]}"
    if tipo == "email" and isinstance(v, str):
        return v.strip().lower()
    return v


@op("formatar_documentos", "Formatar CPF/CNPJ/telefone/CEP/e-mail", 0,
    "Aplica a máscara padrão brasileira nas colunas de documentos (detecção automática).",
    {"colunas": "opcional", "tipo": "opcional: cpf|cnpj|telefone|cep|email"})
def formatar_documentos(inputs, p):
    df = inputs[0].copy()
    cols = _cols(df, p.get("colunas")) or list(df.columns)
    for c in cols:
        tipo = p.get("tipo") or detect_type(df[c], c)
        if tipo in ("cpf", "cnpj", "telefone", "cep", "email"):
            df[c] = df[c].map(lambda v, t=tipo: _fmt_doc(v, t))
    return df


@op("preencher_vazios", "Preencher vazios", 0,
    "Preenche células vazias com um valor fixo, zero, média, mediana ou o valor anterior.",
    {"colunas": "opcional", "valor": "texto|número|zero|media|mediana|anterior|mais_comum"})
def preencher_vazios(inputs, p):
    df = inputs[0].copy()
    val = p.get("valor", "")
    for c in _cols(df, p.get("colunas")) or list(df.columns):
        if val in ("media", "mediana"):
            n = _numeric(df, c)
            if n.notna().any():
                df[c] = n.fillna(n.mean() if val == "media" else n.median())
        elif val == "zero":
            df[c] = df[c].fillna(0)
        elif val == "anterior":
            df[c] = df[c].ffill()
        elif val == "mais_comum":
            m = df[c].mode()
            if len(m):
                df[c] = df[c].fillna(m.iloc[0])
        else:
            df[c] = df[c].astype(object).where(df[c].notna(), val)
    return df


@op("converter_tipos", "Converter tipos", 0,
    "Converte colunas de texto para número ou data (aceita formato brasileiro).",
    {"colunas": "lista", "tipo": "numero|data|texto"})
def converter_tipos(inputs, p):
    df = inputs[0].copy()
    for c in _cols(df, p.get("colunas")):
        t = p.get("tipo", "numero")
        if t == "numero":
            df[c] = to_number(df[c])
        elif t == "data":
            df[c] = to_datetime(df[c])
        else:
            df[c] = df[c].astype("string")
    return df


# ---------------------------------------------------------------------------
# Fase 1 — Validação
# ---------------------------------------------------------------------------
@op("validar_documentos", "Validar CPF/CNPJ/e-mail", 1,
    "Cria uma coluna '<coluna>_valido' (SIM/NÃO) verificando os dígitos verificadores.",
    {"colunas": "opcional (detecção automática)", "somente_invalidos": "true|false"})
def validar_documentos(inputs, p):
    df = inputs[0].copy()
    flags = []
    for c in _cols(df, p.get("colunas")) or list(df.columns):
        tipo = detect_type(df[c], c)
        fn = {"cpf": cpf_valid, "cnpj": cnpj_valid,
              "email": lambda v: bool(re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", str(v).strip())),
              "telefone": lambda v: len(only_digits(v)) in (10, 11, 12, 13)}.get(tipo)
        if fn:
            col = f"{c}_valido"
            df[col] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else ("SIM" if fn(v) else "NÃO"))
            flags.append(col)
    if flags and p.get("somente_invalidos") in (True, "true", "sim"):
        mask = pd.Series(False, index=df.index)
        for f in flags:
            mask |= df[f] == "NÃO"
        df = df[mask].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Fase 2 — Combinação de planilhas
# ---------------------------------------------------------------------------
def _key_series(s: pd.Series) -> pd.Series:
    """Normaliza a chave para o cruzamento (ignora máscara, caixa e espaços)."""
    def f(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        sv = str(v).strip()
        d = only_digits(sv)
        if d and len(d) >= len(re.sub(r"[\s.\-/()]", "", sv)):
            return d.lstrip("0") or "0"
        return nlp.strip_accents(sv).upper()
    return s.map(f)


@op("cruzar", "Cruzar planilhas (PROCV/JOIN)", 2,
    "Junta duas planilhas por uma coluna-chave. tipo: interno (só correspondências), "
    "esquerda (mantém todos da 1ª, como PROCV), completo (todos de ambas).",
    {"chave_esquerda": "coluna da 1ª planilha", "chave_direita": "coluna da 2ª planilha",
     "tipo": "interno|esquerda|direita|completo", "colunas_direita": "opcional: colunas a trazer da 2ª"},
    multi=True)
def cruzar(inputs, p):
    a, b = inputs[0], inputs[1]
    ka, kb = p["chave_esquerda"], p.get("chave_direita") or p["chave_esquerda"]
    how = {"interno": "inner", "esquerda": "left", "direita": "right", "completo": "outer"}.get(p.get("tipo", "interno"), "inner")
    keep = _cols(b, p.get("colunas_direita"))
    if keep:
        b = b[[kb] + [c for c in keep if c != kb]]
    a = a.copy()
    b = b.copy()
    a["__k"] = _key_series(a[ka])
    b["__k"] = _key_series(b[kb])
    b = b.drop_duplicates("__k") if how in ("left", "inner") and p.get("permitir_multiplos") not in (True, "true") else b
    suf = "_" + str(p.get("_alias_direita", "2"))
    b["__kb"] = b[kb]
    out = a.merge(b.drop(columns=[kb]), on="__k", how=how, suffixes=("", suf), indicator=how != "inner")
    if how in ("right", "outer"):
        out[ka] = out[ka].astype(object).where(out[ka].notna(), out["__kb"])
    out = out.drop(columns=["__kb"])
    if "_merge" in out.columns:
        out["correspondencia"] = out["_merge"].map({"both": "ENCONTRADO", "left_only": "SEM CORRESPONDÊNCIA",
                                                     "right_only": "SÓ NA 2ª PLANILHA"}).astype(str)
        out = out.drop(columns=["_merge"])
    return out.drop(columns=["__k"]).reset_index(drop=True)


@op("comparar", "Comparar planilhas", 2,
    "Compara duas planilhas pela chave e indica o que está em ambas, só na 1ª ou só na 2ª.",
    {"chave_esquerda": "coluna da 1ª planilha", "chave_direita": "coluna da 2ª planilha",
     "mostrar": "todos|somente_diferencas|somente_esquerda|somente_direita"}, multi=True)
def comparar(inputs, p):
    a, b = inputs[0].copy(), inputs[1].copy()
    ka, kb = p["chave_esquerda"], p.get("chave_direita") or p["chave_esquerda"]
    na, nb = p.get("_alias_esquerda", "1ª"), p.get("_alias_direita", "2ª")
    a["__k"], b["__k"] = _key_series(a[ka]), _key_series(b[kb])
    sa, sb = set(a["__k"].dropna()), set(b["__k"].dropna())
    a["situacao"] = a["__k"].map(lambda k: "EM AMBAS" if k in sb else f"SOMENTE EM {na}".upper())
    only_b = b[~b["__k"].isin(sa)].copy()
    only_b["situacao"] = f"SOMENTE EM {nb}".upper()
    if kb != ka:
        only_b = only_b.rename(columns={kb: ka})
    out = pd.concat([a, only_b[[c for c in only_b.columns if c in a.columns]]], ignore_index=True)
    mostrar = p.get("mostrar", "todos")
    if mostrar == "somente_diferencas":
        out = out[out["situacao"] != "EM AMBAS"]
    elif mostrar == "somente_esquerda":
        out = out[out["situacao"] == f"SOMENTE EM {na}".upper()]
    elif mostrar == "somente_direita":
        out = out[out["situacao"] == f"SOMENTE EM {nb}".upper()]
    cols = ["situacao"] + [c for c in out.columns if c not in ("situacao", "__k")]
    return out[cols].reset_index(drop=True)


@op("unificar", "Unificar planilhas", 2,
    "Empilha várias planilhas em uma só (alinha colunas com nomes parecidos) e marca a origem.",
    {"coluna_origem": "true|false"}, multi=True)
def unificar(inputs, p):
    names = p.get("_aliases") or [f"planilha_{i+1}" for i in range(len(inputs))]
    base_cols = list(inputs[0].columns)
    canon_map = {nlp.canon(c): c for c in base_cols}
    frames = []
    for name, df in zip(names, inputs):
        ren = {}
        for c in df.columns:
            cc = nlp.canon(c)
            if cc in canon_map:
                ren[c] = canon_map[cc]
            else:
                best = max(canon_map, key=lambda k: nlp.ratio(k, cc), default=None)
                if best and nlp.ratio(best, cc) >= 0.85:
                    ren[c] = canon_map[best]
        d = df.rename(columns=ren).copy()
        if p.get("coluna_origem", True) not in (False, "false"):
            d.insert(0, "origem", name)
        frames.append(d)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Fase 3 — Filtro e colunas
# ---------------------------------------------------------------------------
def _condition(df, c):
    col, oper, val = c.get("coluna"), c.get("operador", "="), c.get("valor")
    if col not in df.columns:
        raise ValueError(f"coluna '{col}' não existe")
    s = df[col]
    if oper == "vazio":
        return s.isna() | (s.astype(str).str.strip() == "")
    if oper == "nao_vazio":
        return ~(s.isna() | (s.astype(str).str.strip() == ""))
    vals = val if isinstance(val, list) else [val]
    if oper in (">", ">=", "<", "<=", "entre"):
        if re.match(r"^\d{4}-\d{2}-\d{2}", str(vals[0])) or detect_type(s, col) == "data":
            left = to_datetime(s)
            vals = [pd.to_datetime(v, errors="coerce", dayfirst=not re.match(r"^\d{4}", str(v))) for v in vals]
        else:
            left = to_number(s)
            vals = [float(v) for v in vals]
        if oper == "entre":
            return (left >= vals[0]) & (left <= vals[-1])
        return {">": left > vals[0], ">=": left >= vals[0], "<": left < vals[0], "<=": left <= vals[0]}[oper]
    txt = s.astype(str).map(lambda v: nlp.strip_accents(v).lower().strip())
    norm_vals = [nlp.strip_accents(str(v)).lower().strip() for v in vals]
    if oper in ("=", "!=", "em", "fora"):
        num = to_number(s)
        try:
            fv = [float(v) for v in vals]
            m = num.isin(fv) if num.notna().mean() > 0.8 else txt.isin(norm_vals)
        except (TypeError, ValueError):
            m = txt.isin(norm_vals)
        return ~m if oper in ("!=", "fora") else m
    if oper == "contem":
        m = pd.Series(False, index=df.index)
        for v in norm_vals:
            m |= txt.str.contains(re.escape(v), na=False)
        return m
    if oper == "nao_contem":
        m = pd.Series(True, index=df.index)
        for v in norm_vals:
            m &= ~txt.str.contains(re.escape(v), na=False)
        return m
    if oper == "comeca_com":
        return txt.str.startswith(tuple(norm_vals))
    if oper == "termina_com":
        return txt.str.endswith(tuple(norm_vals))
    raise ValueError(f"operador desconhecido: {oper}")


@op("filtrar", "Filtrar registros", 3,
    "Mantém apenas as linhas que atendem às condições. operadores: = != > >= < <= entre em fora "
    "contem nao_contem comeca_com termina_com vazio nao_vazio.",
    {"condicoes": "lista de {coluna, operador, valor}", "logica": "e|ou"})
def filtrar(inputs, p):
    df = inputs[0]
    conds = p.get("condicoes") or []
    if not conds:
        return df
    masks = [_condition(df, c).fillna(False) for c in conds]
    m = masks[0]
    for x in masks[1:]:
        m = (m | x) if p.get("logica") == "ou" else (m & x)
    return df[m].reset_index(drop=True)


@op("selecionar_colunas", "Selecionar colunas", 3, "Mantém apenas as colunas informadas, na ordem.",
    {"colunas": "lista"})
def selecionar_colunas(inputs, p):
    df = inputs[0]
    cols = _cols(df, p.get("colunas"))
    return df[cols] if cols else df


@op("remover_colunas", "Remover colunas", 3, "Exclui as colunas informadas.", {"colunas": "lista"})
def remover_colunas(inputs, p):
    df = inputs[0]
    return df.drop(columns=_cols(df, p.get("colunas")))


@op("renomear_colunas", "Renomear colunas", 3, "Renomeia colunas.", {"mapa": "{antigo: novo}"})
def renomear_colunas(inputs, p):
    return inputs[0].rename(columns=p.get("mapa") or {})


@op("coluna_calculada", "Criar coluna calculada", 3,
    "Cria uma coluna a partir de uma expressão aritmética entre colunas (ex.: `preco * quantidade`). "
    "Use crases para nomes com espaço.",
    {"nome": "nome da nova coluna", "expressao": "expressão (+ - * / e parênteses)"})
def coluna_calculada(inputs, p):
    df = inputs[0].copy()
    expr = str(p.get("expressao", ""))
    if re.search(r"__|import|lambda|exec|eval|open|\bos\b|\bsys\b", expr):
        raise ValueError("expressão não permitida")
    tmp = pd.DataFrame(index=df.index)
    mapping = {}
    for i, c in enumerate(df.columns):
        key = f"c{i}"
        if f"`{c}`" in expr or re.search(rf"(?<![\w`]){re.escape(c)}(?![\w`])", expr):
            n = to_number(df[c])
            tmp[key] = n if n.notna().mean() > 0.5 else df[c]
            mapping[c] = key
    for c in sorted(mapping, key=len, reverse=True):
        expr = expr.replace(f"`{c}`", mapping[c])
        expr = re.sub(rf"(?<![\w`]){re.escape(c)}(?![\w`])", mapping[c], expr)
    if not re.fullmatch(r"[\w\s+\-*/().%]+", expr):
        raise ValueError("expressão contém caracteres não permitidos")
    df[p.get("nome", "calculado")] = tmp.eval(expr, engine="python")
    return df


# ---------------------------------------------------------------------------
# Fase 4 — Agregação / análise
# ---------------------------------------------------------------------------
@op("contar_por", "Contagem por coluna", 4,
    "Conta registros agrupando pelas colunas informadas (com percentual). Sem colunas = total.",
    {"colunas": "lista de colunas de agrupamento"})
def contar_por(inputs, p):
    df = inputs[0]
    cols = _cols(df, p.get("colunas"))
    if not cols:
        return pd.DataFrame({"total_registros": [len(df)]})
    g = df.groupby(cols, dropna=False).size().reset_index(name="quantidade")
    g = g.sort_values("quantidade", ascending=False)
    g["percentual"] = (g["quantidade"] / max(1, len(df)) * 100).round(2)
    return g.reset_index(drop=True)


AGG = {"soma": "sum", "media": "mean", "max": "max", "min": "min", "contagem": "count",
       "mediana": "median", "contagem_distinta": "nunique"}


@op("agrupar", "Agrupar e calcular", 4,
    "Agrupa pelas colunas 'por' e calcula soma/média/max/min/contagem/mediana das colunas 'valores'.",
    {"por": "lista de colunas de agrupamento (vazia = total geral)", "valores": "lista de colunas numéricas",
     "agregacao": "soma|media|max|min|contagem|mediana|contagem_distinta"})
def agrupar(inputs, p):
    df = inputs[0].copy()
    by = _cols(df, p.get("por"))
    vals = _cols(df, p.get("valores"))
    agg = p.get("agregacao", "soma")
    aggs = agg if isinstance(agg, list) else [agg]
    if not vals:
        raise ValueError("nenhuma coluna de valor informada")
    for v in vals:
        if aggs != ["contagem"] and aggs != ["contagem_distinta"]:
            df[v] = to_number(df[v])
    spec = {}
    for v in vals:
        for a in aggs:
            name = v if len(aggs) == 1 else f"{v}_{a}"
            spec[name] = (v, AGG.get(a, "sum"))
    if by:
        g = df.groupby(by, dropna=False).agg(**spec).reset_index()
        g = g.sort_values(list(spec)[0], ascending=False)
    else:
        g = pd.DataFrame({k: [getattr(df[c], f)()] for k, (c, f) in spec.items()})
    for c in spec:
        if pd.api.types.is_float_dtype(g[c]):
            g[c] = g[c].round(2)
    return g.reset_index(drop=True)


@op("tabela_dinamica", "Tabela dinâmica", 4, "Cria uma tabela dinâmica (linhas x colunas).",
    {"linhas": "lista", "colunas": "coluna", "valores": "coluna numérica (opcional)",
     "agregacao": "soma|media|contagem|max|min"})
def tabela_dinamica(inputs, p):
    df = inputs[0].copy()
    rows = _cols(df, p.get("linhas"))
    cols = _cols(df, p.get("colunas"))
    val = (_cols(df, p.get("valores")) or [None])[0]
    agg = AGG.get(p.get("agregacao", "soma" if val else "contagem"), "sum")
    if not rows:
        raise ValueError("informe as linhas da tabela dinâmica")
    if val is None:
        val, agg = rows[0], "count"
        df["__v"] = 1
        val = "__v"
        agg = "sum"
    else:
        df[val] = to_number(df[val])
    t = pd.pivot_table(df, index=rows, columns=cols or None, values=val, aggfunc=agg, fill_value=0,
                       margins=True, margins_name="TOTAL")
    t.columns = [" / ".join(map(str, c)) if isinstance(c, tuple) else str(c) for c in t.columns]
    return t.reset_index()


@op("estatisticas", "Estatísticas / perfil", 4,
    "Gera um perfil de cada coluna: tipo, vazios, únicos, mínimo, máximo, média, soma e mais frequente.",
    {"colunas": "opcional"})
def estatisticas(inputs, p):
    df = inputs[0]
    rows = []
    for c in _cols(df, p.get("colunas")) or list(df.columns):
        s = df[c]
        tipo = detect_type(s, c)
        r = {"coluna": c, "tipo": tipo, "preenchidos": int(s.notna().sum()), "vazios": int(s.isna().sum()),
             "unicos": int(s.nunique())}
        if tipo in ("numero", "moeda"):
            n = to_number(s)
            r.update(minimo=n.min(), maximo=n.max(), media=round(n.mean(), 2), mediana=n.median(), soma=round(n.sum(), 2))
        m = s.mode()
        r["mais_frequente"] = str(m.iloc[0]) if len(m) else None
        rows.append(r)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Fase 5 — Ordenação
# ---------------------------------------------------------------------------
@op("ordenar", "Ordenar", 5, "Ordena pelas colunas informadas.",
    {"colunas": "lista", "crescente": "true|false"})
def ordenar(inputs, p):
    df = inputs[0].copy()
    cols = _cols(df, p.get("colunas"))
    if not cols:
        return df
    asc = p.get("crescente", True) not in (False, "false")
    keys = {}
    for c in cols:
        t = detect_type(df[c], c)
        if t in ("numero", "moeda"):
            keys[c] = to_number(df[c])
        elif t == "data":
            keys[c] = to_datetime(df[c])
        else:
            keys[c] = df[c].astype(str).map(lambda v: nlp.strip_accents(v).lower())
    tmp = pd.DataFrame(keys)
    order = tmp.sort_values(cols, ascending=asc, na_position="last").index
    return df.loc[order].reset_index(drop=True)


@op("top_n", "Ranking / Top N", 5, "Retorna as N maiores (ou menores) linhas por uma coluna numérica.",
    {"coluna": "coluna numérica", "n": "quantidade", "maiores": "true|false"})
def top_n(inputs, p):
    df = inputs[0].copy()
    c = p.get("coluna")
    n = int(p.get("n", 10))
    if c not in df.columns:
        return df.head(n)
    key = to_number(df[c])
    big = p.get("maiores", True) not in (False, "false")
    idx = (key.nlargest(n) if big else key.nsmallest(n)).index
    out = df.loc[idx].reset_index(drop=True)
    out.insert(0, "posicao", range(1, len(out) + 1))
    return out


# ---------------------------------------------------------------------------
# Fase 6 — Saída
# ---------------------------------------------------------------------------
@op("dividir_por", "Separar em abas", 6, "Cria uma aba para cada valor da coluna informada.",
    {"coluna": "coluna categórica"})
def dividir_por(inputs, p):
    df = inputs[0]
    c = p.get("coluna")
    if c not in df.columns:
        raise ValueError(f"coluna '{c}' não existe")
    out = {}
    for k, g in df.groupby(df[c].fillna("(vazio)").astype(str), sort=True):
        out[nlp.slug(k, 25)] = g.reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# Descrição legível
# ---------------------------------------------------------------------------
OPER_TXT = {"=": "igual a", "!=": "diferente de", ">": "maior que", ">=": "maior ou igual a", "<": "menor que",
            "<=": "menor ou igual a", "contem": "contém", "nao_contem": "não contém", "comeca_com": "começa com",
            "termina_com": "termina com", "vazio": "está vazio", "nao_vazio": "está preenchido", "entre": "entre",
            "em": "em", "fora": "fora de"}


def describe(step: dict) -> str:
    o, p = step.get("op"), step.get("params") or {}
    ent = step.get("entradas") or []
    alvo = ent[0] if ent else "?"

    def lst(x):
        x = x if isinstance(x, list) else ([x] if x else [])
        return ", ".join(f"“{c}”" for c in x)

    if o == "remover_duplicatas":
        return f"Remover duplicatas em {alvo}" + (f" considerando {lst(p.get('colunas'))}" if p.get("colunas") else "")
    if o == "remover_linhas_vazias":
        return f"Remover linhas vazias de {alvo}" + (f" (sem {lst(p.get('colunas'))})" if p.get("colunas") else "")
    if o == "remover_espacos":
        return f"Remover espaços extras dos textos de {alvo}"
    if o == "padronizar_texto":
        return f"Padronizar texto ({p.get('caixa', 'maiuscula')}) em {alvo}" + (f": {lst(p.get('colunas'))}" if p.get("colunas") else "")
    if o == "formatar_documentos":
        return f"Formatar documentos em {alvo}" + (f": {lst(p.get('colunas'))}" if p.get("colunas") else " (detecção automática)")
    if o == "preencher_vazios":
        return f"Preencher vazios de {alvo} com “{p.get('valor', '')}”" + (f" em {lst(p.get('colunas'))}" if p.get("colunas") else "")
    if o == "validar_documentos":
        return f"Validar CPF/CNPJ/e-mail em {alvo}" + (" e manter só os inválidos" if p.get("somente_invalidos") in (True, "true") else "")
    if o == "cruzar":
        tipo = {"interno": "só correspondências", "esquerda": "mantendo todos de " + alvo, "completo": "todos de ambas", "direita": "mantendo todos de " + (ent[1] if len(ent) > 1 else "")}.get(p.get("tipo", "interno"), "")
        return f"Cruzar {alvo} com {ent[1] if len(ent) > 1 else '?'} por “{p.get('chave_esquerda')}” = “{p.get('chave_direita') or p.get('chave_esquerda')}” ({tipo})"
    if o == "comparar":
        mo = {"somente_diferencas": " — só as diferenças", "somente_esquerda": f" — só o que está em {alvo} e não na outra",
              "somente_direita": " — só o que está na 2ª e não na 1ª"}.get(p.get("mostrar"), "")
        return f"Comparar {alvo} com {ent[1] if len(ent) > 1 else '?'} pela chave “{p.get('chave_esquerda')}”{mo}"
    if o == "unificar":
        return f"Unificar {len(ent)} planilhas ({', '.join(ent)}) em uma base única"
    if o == "filtrar":
        lg = " OU " if p.get("logica") == "ou" else " E "
        conds = [f"“{c.get('coluna')}” {OPER_TXT.get(c.get('operador'), c.get('operador'))}" +
                 ("" if c.get("operador") in ("vazio", "nao_vazio") else f" {c.get('valor')}") for c in p.get("condicoes", [])]
        return f"Filtrar {alvo} onde " + lg.join(conds)
    if o == "selecionar_colunas":
        return f"Manter apenas as colunas {lst(p.get('colunas'))}"
    if o == "remover_colunas":
        return f"Remover as colunas {lst(p.get('colunas'))}"
    if o == "renomear_colunas":
        return "Renomear colunas: " + ", ".join(f"{k} → {v}" for k, v in (p.get("mapa") or {}).items())
    if o == "coluna_calculada":
        return f"Criar coluna “{p.get('nome')}” = {p.get('expressao')}"
    if o == "contar_por":
        return f"Contar registros de {alvo}" + (f" por {lst(p.get('colunas'))}" if p.get("colunas") else " (total)")
    if o == "agrupar":
        ag = p.get("agregacao", "soma")
        return f"Calcular {ag if isinstance(ag, str) else '/'.join(ag)} de {lst(p.get('valores'))}" + (f" por {lst(p.get('por'))}" if p.get("por") else " (total geral)")
    if o == "tabela_dinamica":
        return f"Tabela dinâmica: linhas {lst(p.get('linhas'))} × colunas {lst(p.get('colunas'))}"
    if o == "estatisticas":
        return f"Gerar estatísticas e perfil das colunas de {alvo}"
    if o == "ordenar":
        return f"Ordenar por {lst(p.get('colunas'))} ({'crescente' if p.get('crescente', True) not in (False, 'false') else 'decrescente'})"
    if o == "top_n":
        return f"Top {p.get('n', 10)} {'maiores' if p.get('maiores', True) not in (False, 'false') else 'menores'} por “{p.get('coluna')}”"
    if o == "dividir_por":
        return f"Separar em abas por “{p.get('coluna')}”"
    return o


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def execute(plan: dict, datasets: dict) -> dict:
    env = {k: v.copy() for k, v in datasets.items()}
    log, produced = [], []
    for i, step in enumerate(plan.get("passos", []), 1):
        o = REGISTRY.get(step.get("op"))
        entry = {"passo": i, "op": step.get("op"), "descricao": describe(step), "status": "ok"}
        t0 = time.perf_counter()
        try:
            if not o:
                raise ValueError("operação desconhecida")
            ins = step.get("entradas") or []
            missing = [a for a in ins if a not in env]
            if missing:
                raise ValueError(f"planilha não encontrada: {', '.join(missing)}")
            inputs = [env[a] for a in ins]
            params = dict(step.get("params") or {})
            if o["multi"]:
                params["_aliases"] = ins
                params["_alias_esquerda"] = ins[0]
                params["_alias_direita"] = ins[1] if len(ins) > 1 else ""
            entry["linhas_antes"] = int(sum(len(x) for x in inputs))
            out = o["fn"](inputs, params)
            saida = step.get("saida") or (ins[0] if ins else f"resultado_{i}")
            if isinstance(out, dict):
                total = 0
                for k, v in out.items():
                    name = f"{saida}_{k}"
                    env[name] = v
                    produced.append(name)
                    total += len(v)
                entry["linhas_depois"] = total
                entry["saidas"] = list(out)
            else:
                env[saida] = out
                produced.append(saida)
                entry["linhas_depois"] = int(len(out))
            entry["saida"] = saida
        except Exception as e:  # o executor nunca quebra: registra e segue
            entry["status"] = "erro"
            entry["erro"] = str(e)
        entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        log.append(entry)
    seen, results = set(), {}
    for name in produced:
        if name not in seen:
            seen.add(name)
            results[name] = env[name]
    # se uma planilha foi transformada e depois virou base para outra, só mantém a final de cada linhagem
    return {"resultados": results, "log": log, "env": env}
