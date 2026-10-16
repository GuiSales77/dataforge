"""Agentes de interpretação do DataForge.

  Contexto      -> monta o retrato das planilhas (colunas, tipos, valores)
  Memória       -> busca planos parecidos que já funcionaram e os adapta
  Intérprete    -> entende o comando em português usando regras + similaridade (offline)
  IA externa    -> planejador via API, acionado quando a confiança local é baixa
  Validador     -> corrige nomes de colunas e testa o plano numa amostra (dry-run)
  Orquestrador  -> o "cérebro": escolhe a melhor estratégia e o melhor modelo
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import nlp, operations, storage
from .operations import REGISTRY, _key_series

NUMERIC = {"numero", "moeda"}
DOCS = {"cpf", "cnpj", "telefone", "cep", "email"}

# (operação, regex sobre o texto canônico, fase, gatilho fraco?)
TRIGGERS = [
    ("limpeza_geral", r"\blimp(ar|e|eza|o)\b|higieniz|sanitiz|\btratar (o |a )?dado|\btrate (o |a )?dado", 0, False),
    ("remover_duplicatas", r"duplica|repetid|dedup", 0, False),
    ("remover_linhas_vazias", r"linha (vazia|em branco|sem dado)|registro (vazio|em branco)", 0, False),
    ("remover_espacos", r"\bespaco|\btrim\b", 0, False),
    ("formatar_documentos", r"\b(format|padroniz|mascar|corrig|arrum)\w* (o |a |os |as |de |do |da )?(cpf|cnpj|telefone|celular|cep|e ?mail|documento)", 0, False),
    ("padronizar_texto", r"maiuscul|minuscul|caixa (alta|baixa)|capitaliz|\bpadroniz", 0, False),
    ("preencher_vazios", r"\bpreench|\bcomplet\w* (o |os |a |as )?(vazio|campo vazio|celula vazia)|substitu\w* (o |os )?vazio|valor (nulo|faltante|ausente)", 0, False),
    ("validar_documentos", r"\bvalid(ar|e|a|acao|o)\b|\binvalid|verific\w* (o |os |a |as |se )?(cpf|cnpj|e ?mail|documento)", 1, False),
    ("comparar", r"\bcompar|\bdiferenc|nao (esta|estao|existe|existem|consta|constam|aparece|aparecem)|\bfaltando|\bfaltante|\bausente|\bdivergen|que nao tem", 2, False),
    ("cruzar", r"\bcruz(ar|e|a|amento|ando)\b|\bprocv\b|\bvlookup\b|\bjoin\b|\brelacion|\bvincul|\b(mescl|junt|combin|unir|une|uma)\w* (a |o |as |os )?\w+( \w+)? com\b|\btrazer\b|\btraga\b|\btrazend|\bbuscar\b|\bbusque\b|\bbuscand|\bcorrespondencia", 2, True),
    ("unificar", r"\bunific|\bconsolid|\bempilh|\bconcaten|\bjunt\w* (toda|tudo|a planilha|ela)|\bmescl\w* toda|\buma (base|planilha|tabela) (unica|so)|\bunir toda", 2, False),
    ("remover_colunas", r"\b(remov|exclu|apag|tir|delet)\w* (a |as |o |os )?coluna", 3, False),
    ("selecionar_colunas", r"\b(mant|selecion|mostr|exib|deix|ficar)\w* (com )?(apenas? |somente |so )?(a |as |o |os )?coluna", 3, False),
    ("filtrar", r"\bfiltr|\bapenas?\b|\bsomente\b|\bso (o|a)\b|\bonde\b|\bem que\b|\bcujo|\bcuja|\bexceto\b|\b(remov|exclu|tir)\w* (o |os |a |as )?(registro|cliente|linha|pedido|item)|\bque (tenha|possua|seja|estej)", 3, True),
    ("contar_por", r"\bcont(ar|e|a|agem|abiliz|ando)\b|\bquanto\b|\bquanta\b|\bquantidade (de|por)\b|\bnumero de\b|\bfrequencia|\bdistribuicao", 4, False),
    ("agrupar", r"\bsom(ar|a|e|atorio|ando)\b|\btotal\b|\btotai\b|\btotaliz|\bmedia\b|\bmedio\b|\bmaximo\b|\bminimo\b|\bmaior valor|\bmenor valor|\bmediana\b|\bfaturamento por|\breceita por", 4, True),
    ("tabela_dinamica", r"tabela dinamica|\bpivot|tabela cruzada|matriz", 4, False),
    ("estatisticas", r"\bestatistic|\bresumo\b|\banalis|\bperfil\b|\bpanorama|\bvisao geral|\bdiagnostic|\bqualidade do dado", 4, True),
    ("ordenar", r"\borden|\bclassific|maior para (o )?menor|menor para (o )?maior|\bcrescente|\bdecrescente|ordem alfabetica|mais recente primeiro", 5, False),
    ("top_n", r"\btop ?\d+|\b\d+ (maior|menor|primeiro|melhor|pior)\b|\branking\b|\bos (maior|menor|melhor|pior)\b", 5, False),
    ("dividir_por", r"\bsepar\w* (\w+ ){0,3}?(por|para cada)|\bdivid\w* (\w+ ){0,3}?por|\buma aba (por|para cada)|\baba por|\baba para cada|\bexport\w* (separado )?por|\bquebr\w* por", 6, False),
]
TRIGGERS = [(o, re.compile(rx), f, w) for o, rx, f, w in TRIGGERS]
PHASE = {o: f for o, _, f, _ in TRIGGERS}
PHASE.update({k: v["fase"] for k, v in REGISTRY.items()})

OPER_RX = [
    ("entre", r"^entre\b"),
    (">=", r"^(maior ou igual( a)?|pelo menos?|no minimo|a partir de|>=)\b"),
    ("<=", r"^(menor ou igual( a)?|no maximo|ate|<=)\b"),
    (">", r"^(maior (que|do que|de)|acima (de|do|da)|superior (a|ao)|mai (que|de|do que)|mais (que|de|do que)|depois (de|do|da)|apos|>)"),
    ("<", r"^(menor (que|do que|de)|abaixo (de|do|da)|inferior (a|ao)|menos (que|de|do que)|antes (de|do|da)|<)"),
    ("!=", r"^(diferente (de|do|da)|exceto|que nao seja|nao e|nao for|nao seja|!=)\b"),
    ("nao_vazio", r"^(nao (vazio|vazia|em branco|nulo|nula)|preenchid|com valor|informad)"),
    ("vazio", r"^(vazio|vazia|em branco|sem valor|sem preench|nulo|nula|nao informad|nao preenchid)"),
    ("contem", r"^(contem|contendo|que contem|que tenha|tenha|com a palavra|inclui|incluindo|com o texto)\b"),
    ("comeca_com", r"^(comec\w* (com|por)|inici\w* (com|por))\b"),
    ("termina_com", r"^(termin\w* (com|em))\b"),
    ("=", r"^(igual (a|ao)|=|for|seja|e igual|com|de|do|da|em|no|na|como|sendo)\b"),
]
OPER_RX = [(o, re.compile(r)) for o, r in OPER_RX]
ENTITY_NOUNS = {"cliente", "usuario", "pessoa", "funcionario", "registro", "dado", "item", "cadastro", "linha"}
NEUTRAL = re.compile(r"^(remov|exclu|tir|apag|elimin|delet|mostr|exib|list|cri|aplic|deix|mant|ger|faz|fac|calcul|"
                     r"obter|obtenh|retorn|devolv|export|salv|maior|menor|crescent|decrescent|alfabetic|recent|apena|"
                     r"somente|invalid|valid|nova|novo|aba|planilh|arquivo|tabela|base|todo|toda|ambas|ambos|uma|um|vazi|mais|meno|seguinte|exibi)")
VALUE_HINT_NAMES = ("valor", "total", "preco", "venda", "receita", "faturamento", "quantidade", "qtd", "qtde",
                    "montante", "saldo", "custo", "salario", "pontuacao", "nota", "km", "peso")


def _is_num_type(t):
    return t in NUMERIC


def build_context(datasets: dict, profiles: dict) -> dict:
    ctx = {}
    for alias, df in datasets.items():
        prof = profiles.get(alias, {})
        types = {c["nome"]: c["tipo"] for c in prof.get("colunas", [])}
        values = {c["nome"]: c.get("valores", []) for c in prof.get("colunas", []) if c.get("valores")}
        ctx[alias] = {"cols": list(df.columns), "types": types, "values": values, "linhas": len(df)}
    return ctx


# ===========================================================================
# Intérprete local (regras + similaridade)
# ===========================================================================
class LocalInterpreter:
    def __init__(self, cfg: dict, datasets: dict = None):
        self.cfg = cfg
        self.datasets = datasets or {}
        self.col_cut = float(cfg.get("similaridade_colunas", 0.78))

    # ----------------------------------------------------------------- utils
    def _mark(self, ti, tl=1):
        self.explained.update(range(ti, ti + tl))

    def _seg(self, s, e):
        return " ".join(self.toks[s:e])

    def _find_por(self, s, e):
        for i in range(s, e):
            t = self.toks[i]
            if t == "por" or (t == "cada" and i > s and self.toks[i - 1] in ("para", "de", "em")):
                if i + 1 < len(self.toks) and self.toks[i + 1] in ("favor",):
                    continue
                self._mark(i)
                return i + 1
        return None

    def _cols_in(self, alias, s, e, types=None):
        info = self.env[alias]
        found = nlp.find_mentions(self.toks, info["cols"], self.col_cut, s, e)
        # menção parcial: um token distintivo do nome da coluna (ex.: "nomes" -> "Nome Cliente")
        claimed = set()
        for ti, tl, _, _ in found:
            claimed.update(range(ti, ti + tl))
        have = {f[2] for f in found}
        for col in info["cols"]:
            if col in have:
                continue
            ctoks = nlp.tokens(col)
            if len(ctoks) < 2:
                continue
            for t in ctoks:
                if len(t) < 4 or t in nlp.STOPWORDS or t in ENTITY_NOUNS:
                    continue
                if sum(t in nlp.tokens(c) for c in info["cols"]) != 1:
                    continue
                hit = [i for i in range(s, e) if self.toks[i] == t and i not in claimed]
                if hit:
                    found.append((hit[0], 1, col, 0.72))
                    claimed.add(hit[0])
                    break
        found.sort(key=lambda f: f[0])
        out = []
        for ti, tl, col, sc in found:
            span = set(range(ti, ti + tl))
            if span & self.ds_spans:
                continue
            if types and info["types"].get(col) not in types:
                continue
            out.append((ti, tl, col, sc))
        return out

    def _use_cols(self, mentions):
        for ti, tl, _, _ in mentions:
            self._mark(ti, tl)
        return [m[2] for m in mentions]

    def _cat_values(self, alias, s, e, exclude_cols=()):
        """Encontra valores de categoria citados (ex.: 'ativos' -> status = Ativo)."""
        info = self.env[alias]
        found = {}
        for col, vals in info["values"].items():
            if col in exclude_cols:
                continue
            vals = [v for v in vals if len(nlp.canon(v)) >= 2 and not re.fullmatch(r"\d{1,2}", str(v).strip())]
            for ti, tl, val, sc in nlp.find_mentions(self.toks, vals, 0.86, s, e):
                if set(range(ti, ti + tl)) & (self.ds_spans | self.trigger_spans):
                    continue
                # o próprio nome da coluna não conta como valor
                if nlp.canon(val) in [nlp.canon(c) for c in info["cols"]]:
                    continue
                neg = ti > 0 and self.toks[ti - 1] in ("nao", "exceto", "sem", "menos", "fora")
                found.setdefault(col, []).append((ti, tl, val, neg))
        return found

    def _value_after(self, i, e, multi=False):
        """Lê um valor começando no token i: número, data ou texto."""
        if i >= len(self.toks):
            return None, i
        raw = self.raw[i]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            self._mark(i)
            return raw, i + 1
        n = nlp.parse_number(raw)
        if n is not None:
            j = i + 1
            if j < len(self.toks) and self.toks[j] in ("mil", "k"):
                n *= 1000
                j += 1
            elif j < len(self.toks) and self.toks[j] in ("milhao", "milhoe", "mi"):
                n *= 1_000_000
                j += 1
            self._mark(i, j - i)
            return n, j
        words = []
        j = i
        while j < min(e, i + (4 if multi else 3)):
            t = self.toks[j]
            if t in ("e", "ou", "por", "depois", "entao", "ordenado", "ordenar", "pela", "pelo") or (j in self.trigger_spans):
                break
            words.append(self.raw[j])
            j += 1
        if not words:
            return None, i
        self._mark(i, j - i)
        return " ".join(words), j

    def _parse_conditions(self, alias, s, e, negate=False, allow_cat=True, skip_explained=False):
        conds = []
        busy = set(self.explained) if skip_explained else set()
        used_cols = set()
        info = self.env[alias]
        # 1) valores de categoria citados
        if allow_cat:
            for col, hits in self._cat_values(alias, s, e).items():
                hits = [h for h in hits if not (set(range(h[0], h[0] + h[1])) & busy)]
                if not hits:
                    continue
                vals = [h[2] for h in hits]
                neg = negate or any(h[3] for h in hits)
                for h in hits:
                    self._mark(h[0], h[1])
                if len(vals) == 1:
                    conds.append({"coluna": col, "operador": "!=" if neg else "=", "valor": vals[0]})
                else:
                    conds.append({"coluna": col, "operador": "fora" if neg else "em", "valor": vals})
                used_cols.add(col)
        # 2) coluna + operador + valor
        for ti, tl, col, sc in self._cols_in(alias, s, e):
            if col in used_cols or (set(range(ti, ti + tl)) & busy):
                continue
            if ti > 0 and self.toks[ti - 1] in ("sem", "faltando", "falta"):
                self._mark(ti - 1, tl + 1)
                conds.append({"coluna": col, "operador": "vazio", "valor": None})
                used_cols.add(col)
                continue
            j = ti + tl
            # pula palavras de ligação
            while j < e and self.toks[j] in ("for", "seja", "esteja", "estiver", "que", "foi", "estao", "esta", "tiver", "tenha"):
                j += 1
            rest = " ".join(self.toks[j:min(e, j + 6)])
            oper, olen = None, 0
            for o, rx in OPER_RX:
                m = rx.match(rest)
                if m:
                    oper, olen = o, len(m.group(0).split())
                    break
            if oper is None:
                continue
            k = j + olen
            if oper in ("vazio", "nao_vazio"):
                self._mark(ti, k - ti)
                conds.append({"coluna": col, "operador": oper, "valor": None})
                used_cols.add(col)
                continue
            if oper == "entre":
                v1, k2 = self._value_after(k, e)
                if k2 < e and self.toks[k2] == "e":
                    v2, _ = self._value_after(k2 + 1, e)
                    if v1 is not None and v2 is not None:
                        self._mark(ti, k - ti)
                        self._mark(k2)
                        conds.append({"coluna": col, "operador": "entre", "valor": [v1, v2]})
                        used_cols.add(col)
                continue
            val, _ = self._value_after(k, e)
            if val is None:
                continue
            t = info["types"].get(col)
            if oper == "=" and self.toks[j] in ("de", "do", "da", "em", "no", "na", "com", "como") and not isinstance(val, float):
                # 'clientes de SP': só aceita se for um valor conhecido ou coluna de texto curta
                if t not in ("categoria", "texto", "booleano"):
                    continue
            if oper in (">", ">=", "<", "<=") and isinstance(val, str) and not re.match(r"\d{4}-", val):
                continue
            if isinstance(val, str) and any(nlp.canon(val).startswith(nlp.canon(c)) for c in info["cols"]):
                continue
            if oper in ("=", "!=") and isinstance(val, str) and t == "categoria" and info["values"].get(col):
                if max(nlp.ratio(nlp.canon(v), nlp.canon(val)) for v in info["values"][col]) < 0.8:
                    continue
            if negate and oper == "=":
                oper = "!="
            self._mark(ti, k - ti)
            # aproxima do valor real da categoria, se existir
            if isinstance(val, str) and info["values"].get(col):
                best = max(info["values"][col], key=lambda v: nlp.ratio(nlp.canon(v), nlp.canon(val)))
                if nlp.ratio(nlp.canon(best), nlp.canon(val)) >= 0.8:
                    val = best
            conds.append({"coluna": col, "operador": oper, "valor": val})
            used_cols.add(col)
        return conds

    def _guess_value_col(self, alias):
        info = self.env[alias]
        nums = [c for c in info["cols"] if _is_num_type(info["types"].get(c))]
        for h in VALUE_HINT_NAMES:
            for c in nums:
                if h in nlp.canon(c):
                    return c
        return nums[0] if nums else None

    def _auto_key(self, a, b):
        ia, ib = self.env[a], self.env[b]
        best, bs = None, 0.0
        da, db = self.datasets.get(a), self.datasets.get(b)
        for ca in ia["cols"]:
            for cb in ib["cols"]:
                ta, tb = ia["types"].get(ca), ib["types"].get(cb)
                name = nlp.ratio(nlp.canon(ca), nlp.canon(cb))
                idlike = 1.0 if (ta in DOCS | {"id"} and ta == tb) else 0.0
                if name < 0.5 and not idlike:
                    continue
                overlap = 0.0
                if da is not None and db is not None and ca in da.columns and cb in db.columns:
                    ka = set(_key_series(da[ca].dropna().head(3000)).dropna())
                    kb = set(_key_series(db[cb].dropna().head(3000)).dropna())
                    if ka and kb:
                        overlap = len(ka & kb) / min(len(ka), len(kb))
                score = 0.35 * name + 0.45 * overlap + 0.2 * idlike
                if ta in ("categoria", "booleano") and tb in ("categoria", "booleano"):
                    score *= 0.6  # categorias raramente são chaves
                if score > bs:
                    best, bs = (ca, cb), score
        return (best, bs) if bs >= 0.4 else (None, bs)

    def _join_key(self, a, b, s, e):
        ma, mb = self._cols_in(a, s, e), self._cols_in(b, s, e)
        # mesma menção servindo às duas planilhas
        for x in ma:
            for y in mb:
                if x[0] == y[0]:
                    self._mark(x[0], x[1])
                    return (x[2], y[2]), 1.0
        if ma and mb:
            self._mark(ma[-1][0], ma[-1][1])
            self._mark(mb[-1][0], mb[-1][1])
            return (ma[-1][2], mb[-1][2]), 0.9
        key, sc = self._auto_key(a, b)
        if key and (ma or mb):
            mm = (ma or mb)[0]
            if mm[2] in key:
                self._mark(mm[0], mm[1])
                return key, 0.9
        if key:
            self.notes.append(f"Chave de cruzamento detectada automaticamente: “{key[0]}” ↔ “{key[1]}” (score {sc:.2f})")
            return key, 0.7 if sc >= 0.6 else 0.55
        if ma or mb:
            m = (ma or mb)[0]
            other = b if ma else a
            alt = max(self.env[other]["cols"], key=lambda c: nlp.ratio(nlp.canon(c), nlp.canon(m[2])))
            return ((m[2], alt) if ma else (alt, m[2])), 0.5
        return None, 0

    # ------------------------------------------------------------- env update
    def _new_alias(self, base):
        name, i = base[:40], 2
        while name in self.env:
            name, i = f"{base[:37]}_{i}", i + 1
        return name

    def _register(self, alias, cols, types, values=None):
        self.env[alias] = {"cols": cols, "types": types, "values": values or {}, "linhas": 0}

    # ----------------------------------------------------------- main method
    def interpret(self, command: str, ctx: dict) -> dict:
        self.env = {k: dict(v) for k, v in ctx.items()}
        self.raw = nlp.norm(command).split()
        self.toks = [nlp.singular(t) for t in self.raw]
        text = " ".join(self.toks)
        self.explained, self.notes = set(), []
        steps, scores = [], []

        # planilhas citadas
        aliases = list(ctx)
        all_cols = list(dict.fromkeys(c for v in ctx.values() for c in v["cols"]))
        col_spans = set()
        for ti, tl, c, sc in nlp.find_mentions(self.toks, all_cols, 0.95):
            col_spans.update(range(ti, ti + tl))
        ds_m = [x for x in nlp.find_mentions(self.toks, aliases, 0.88)
                if x[3] >= 0.99 or not (set(range(x[0], x[0] + x[1])) & col_spans)]
        taken = set()
        for ti, tl, a, _ in ds_m:
            taken.update(range(ti, ti + tl))
        for a in aliases:  # menção parcial: token distintivo do nome (ex.: 'vendas' -> vendas_q3)
            for t in nlp.tokens(a):
                if len(t) >= 4 and t not in nlp.STOPWORDS and sum(t in nlp.tokens(x) for x in aliases) == 1:
                    for i, x in enumerate(self.toks):
                        if x == t and i not in taken and i not in col_spans:
                            ds_m.append((i, 1, a, 0.75))
                            taken.add(i)
        ds_m.sort(key=lambda m: m[0])
        self.ds_spans = set()
        for ti, tl, a, _ in ds_m:
            self.ds_spans.update(range(ti, ti + tl))
            self._mark(ti, tl)
        global_ds = []
        for m in ds_m:
            if m[2] not in global_ds:
                global_ds.append(m[2])

        # gatilhos
        matches = []
        for o, rx, ph, weak in TRIGGERS:
            for m in rx.finditer(text):
                ti = nlp.char_to_token_index(text, m.start())
                tl = max(1, len(m.group(0).split()))
                if not any(x["op"] == o for x in matches):
                    matches.append({"op": o, "ti": ti, "tl": tl, "fase": ph, "fraco": weak, "txt": m.group(0)})
                else:
                    self._mark(ti, tl)
        # gatilho fraco dentro do trecho de um gatilho forte é descartado (ex.: "manter APENAS as colunas")
        strong = [m for m in matches if not m["fraco"]]
        matches = [m for m in matches if not (m["fraco"] and any(x["ti"] <= m["ti"] < x["ti"] + x["tl"] for x in strong))]
        ops = {m["op"] for m in matches}
        # "vendas por filial" sem verbo de agregação -> contagem
        if not ops & {"contar_por", "agrupar", "tabela_dinamica", "dividir_por", "ordenar", "top_n", "remover_duplicatas",
                      "estatisticas", "cruzar", "comparar", "selecionar_colunas", "remover_colunas"} and "por" in self.toks:
            pi = self.toks.index("por")
            if pi + 1 < len(self.toks) and any(nlp.find_mentions(self.toks, v["cols"], self.col_cut, pi + 1, len(self.toks)) for v in ctx.values()):
                matches.append({"op": "contar_por", "ti": 0, "tl": 0, "fase": 4, "fraco": True, "txt": "", "implicito": True})
                ops.add("contar_por")
        if "formatar_documentos" in ops:
            f = next(m for m in matches if m["op"] == "formatar_documentos")
            matches = [m for m in matches if not (m["op"] == "padronizar_texto" and f["ti"] <= m["ti"] < f["ti"] + f["tl"])]
        if "comparar" in ops and "cruzar" in ops:
            matches = [m for m in matches if m["op"] != "cruzar"] if re.search(r"nao (esta|existe|consta|aparece)|faltando|ausente|diferenc", text) else [m for m in matches if m["op"] != "comparar"]
        if "cruzar" in ops and "unificar" in ops:
            matches = [m for m in matches if m["op"] != "cruzar"]
        if "top_n" in ops:
            matches = [m for m in matches if m["op"] != "ordenar" or "alfabetic" in text]
        if "estatisticas" in ops and len({m["fase"] for m in matches if m["op"] != "estatisticas"} & {4, 5}) > 0:
            matches = [m for m in matches if m["op"] != "estatisticas"]
        if "validar_documentos" in ops and "formatar_documentos" in ops:
            v = next(m for m in matches if m["op"] == "validar_documentos")
            f = next(m for m in matches if m["op"] == "formatar_documentos")
            if v["txt"].startswith("verific") and abs(v["ti"] - f["ti"]) < 2:
                matches = [m for m in matches if m["op"] != "validar_documentos"]
        if "limpeza_geral" in ops:
            lg = next(m for m in matches if m["op"] == "limpeza_geral")
            matches = [m for m in matches if m["op"] != "limpeza_geral"]
            for sub in ("remover_espacos", "remover_linhas_vazias", "remover_duplicatas"):
                if sub not in ops:
                    matches.append({**lg, "op": sub, "implicito": True})
        if "agrupar" in ops and "contar_por" in ops:
            ag = next(m for m in matches if m["op"] == "agrupar")
            if ag["txt"] in ("total", "totai"):
                matches = [m for m in matches if m["op"] != "agrupar"]

        self.trigger_spans = set()
        for m in matches:
            self.trigger_spans.update(range(m["ti"], m["ti"] + m["tl"]))
            self._mark(m["ti"], m["tl"])
        by_pos = sorted(matches, key=lambda m: (m["ti"], m["fase"]))
        starts = sorted({m["ti"] for m in by_pos})
        for m in by_pos:
            nxt = [s for s in starts if s > m["ti"]]
            m["fim"] = nxt[0] if nxt else len(self.toks)
            prv = [s for s in starts if s < m["ti"]]
            m["pre"] = prv[-1] if prv else 0
        ordered = sorted(matches, key=lambda m: (m["fase"], m["ti"]))

        latest = {a: a for a in aliases}
        combined = None
        last_value_col = {}

        def clause_ds(m):
            inside = [a for ti, tl, a, _ in ds_m if m["ti"] <= ti < m["fim"]]
            if inside or combined:
                return inside
            return [a for ti, tl, a, _ in ds_m if m["pre"] <= ti < m["ti"] and m["fase"] >= 3]

        def targets_for(m):
            cds = clause_ds(m)
            if cds:
                seen, out = set(), []
                for d in dict.fromkeys(cds):
                    if latest[d] not in seen:
                        seen.add(latest[d])
                        out.append((d, latest[d]))
                return out, 1.0
            if combined:
                return [(None, combined)], 1.0
            if len(aliases) == 1:
                return [(aliases[0], latest[aliases[0]])], 1.0
            if len(global_ds) == 1:
                return [(global_ds[0], latest[global_ds[0]])], 0.9
            # infere a planilha pelas colunas citadas no trecho (ou no comando todo)
            for a0, b0 in ((m["ti"], m["fim"]), (0, len(self.toks))):
                hits = {a: len(self._cols_in(latest[a], a0, b0)) for a in aliases}
                top = max(hits.values())
                if top > 0 and list(hits.values()).count(top) == 1:
                    a = max(hits, key=hits.get)
                    return [(a, latest[a])], 0.95
            if m["fase"] <= 1 or m["op"] in ("estatisticas", "dividir_por"):
                return [(a, latest[a]) for a in aliases], 0.9
            first = global_ds[0] if global_ds else aliases[0]
            self.notes.append(f"Não ficou claro em qual planilha aplicar “{REGISTRY[m['op']]['label']}”; usei {first}.")
            return [(first, latest[first])], 0.6

        def set_latest(d, old, new):
            nonlocal combined
            if d is None or old == combined:
                combined = new
            if d is not None:
                latest[d] = new

        for m in ordered:
            o, s, e = m["op"], m["ti"], m["fim"]
            seg = self._seg(m["ti"], e)

            # ---------------------------------------------- combinação (multi)
            if o in ("cruzar", "comparar"):
                cds = list(dict.fromkeys(clause_ds(m))) or global_ds
                if len(cds) < 2:
                    rest = [a for a in aliases if a not in cds]
                    cds = (cds + rest)[:2]
                if len(cds) < 2:
                    self.notes.append("Cruzamento exige duas planilhas — passo ignorado.")
                    continue
                a, b = cds[0], cds[1]
                ka, kb = latest[a], latest[b]
                key, ksc = self._join_key(ka, kb, s, e)
                if not key:
                    self.notes.append(f"Não encontrei uma coluna-chave comum entre {a} e {b}.")
                    scores.append(0.2)
                    continue
                if o == "cruzar":
                    tipo = "interno"
                    if re.search(r"procv|vlookup|trazer|traga|trazend|buscar|busque|buscand|manter todo|todos (os|a)|mantendo", seg):
                        tipo = "esquerda"
                    if re.search(r"completo|ambas|todos de ambo|full", seg):
                        tipo = "completo"
                    params = {"chave_esquerda": key[0], "chave_direita": key[1], "tipo": tipo}
                    # colunas a trazer (ex.: 'trazer o email e telefone')
                    if tipo == "esquerda":
                        bring = [c for c in self._use_cols(self._cols_in(kb, m["ti"], e)) if c != key[1]]
                        if bring:
                            params["colunas_direita"] = bring
                    out = self._new_alias("cruzamento")
                    cols = self.env[ka]["cols"] + [c for c in self.env[kb]["cols"] if c != key[1] and c not in self.env[ka]["cols"]] + \
                        [f"{c}_{kb}" for c in self.env[kb]["cols"] if c != key[1] and c in self.env[ka]["cols"]]
                else:
                    mostrar = "todos"
                    neg = re.search(r"(nao (esta|existe|consta|aparece|tem)|faltando|ausente|sem)", seg)
                    if neg:
                        mostrar = "somente_diferencas"
                        ni = m["ti"] + nlp.char_to_token_index(seg, neg.start())
                        self._mark(ni, len(neg.group(0).split()))
                        after = [x[2] for x in ds_m if x[0] > ni]
                        if after:
                            mostrar = "somente_esquerda" if after[0] == b else "somente_direita"
                    params = {"chave_esquerda": key[0], "chave_direita": key[1], "mostrar": mostrar}
                    out = self._new_alias("comparacao")
                    cols = ["situacao"] + self.env[ka]["cols"]
                types = {**self.env[kb]["types"], **self.env[ka]["types"]}
                values = {**self.env[kb]["values"], **self.env[ka]["values"]}
                self._register(out, cols, types, values)
                steps.append({"op": o, "entradas": [ka, kb], "params": params, "saida": out})
                scores.append(ksc)
                set_latest(a, ka, out)
                latest[b] = out
                combined = out
                continue

            if o == "unificar":
                cds = list(dict.fromkeys(clause_ds(m)))
                if len(cds) < 2:
                    cds = aliases
                if len(cds) < 2:
                    self.notes.append("Unificar exige duas ou mais planilhas — passo ignorado.")
                    continue
                ins = [latest[a] for a in cds]
                out = self._new_alias("unificado")
                cols = ["origem"] + list(dict.fromkeys(c for a in ins for c in self.env[a]["cols"]))
                types = {k: v for a in ins for k, v in self.env[a]["types"].items()}
                values = {k: v for a in ins for k, v in self.env[a]["values"].items()}
                self._register(out, cols, types, values)
                steps.append({"op": "unificar", "entradas": ins, "params": {"coluna_origem": True}, "saida": out})
                scores.append(1.0)
                combined = out
                for a in cds:
                    latest[a] = out
                continue

            # ---------------------------------------------- operações simples
            tgts, tsc = targets_for(m)
            for d, t in tgts:
                info = self.env[t]
                sc = tsc
                params, out, implicit_filter = {}, t, None

                if o == "remover_duplicatas":
                    p_i = None
                    for i in range(s, e):
                        if self.toks[i] in ("por", "pelo", "pela", "considerando", "base", "usando"):
                            p_i = i
                            break
                    cols = self._use_cols(self._cols_in(t, p_i, e)) if p_i is not None else []
                    if cols:
                        params["colunas"] = cols
                    if re.search(r"ultim|mais recente", seg):
                        params["manter"] = "ultima"
                elif o == "remover_linhas_vazias":
                    cols = self._use_cols(self._cols_in(t, s, e))
                    if cols:
                        params["colunas"] = cols
                elif o == "remover_espacos":
                    pass
                elif o == "padronizar_texto":
                    params["caixa"] = "minuscula" if re.search(r"minuscul|caixa baixa", seg) else \
                        "titulo" if re.search(r"capitaliz|titulo|primeira letra|nome proprio", seg) else "maiuscula"
                    if "acento" in seg:
                        params["remover_acentos"] = True
                    cols = self._use_cols(self._cols_in(t, s, e, types={"texto", "categoria", "booleano"}))
                    if cols:
                        params["colunas"] = cols
                elif o == "formatar_documentos":
                    wanted = set()
                    for w, tp in (("cpf", "cpf"), ("cnpj", "cnpj"), ("telefone", "telefone"), ("celular", "telefone"),
                                  ("cep", "cep"), ("email", "email"), ("e mail", "email")):
                        if re.search(rf"\b{w}\b", seg):
                            wanted.add(tp)
                    cols = self._use_cols(self._cols_in(t, s, e, types=DOCS))
                    if not cols and wanted:
                        cols = [c for c in info["cols"] if info["types"].get(c) in wanted]
                    if not cols and wanted:
                        cols = [c for c in info["cols"] if any(w in nlp.canon(c) for w in wanted)]
                    if cols:
                        params["colunas"] = cols
                    elif wanted:
                        self.notes.append(f"Nenhuma coluna de {', '.join(wanted)} encontrada em {t}.")
                        continue
                elif o == "preencher_vazios":
                    val = "NÃO INFORMADO"
                    for i in range(s, e):
                        if self.toks[i] in ("com", "por") and i + 1 < e:
                            nxt = self.toks[i + 1]
                            if nxt in ("zero", "0"):
                                val = "zero"
                            elif nxt in ("media", "medio"):
                                val = "media"
                            elif nxt == "mediana":
                                val = "mediana"
                            elif nxt in ("anterior", "acima"):
                                val = "anterior"
                            elif nxt in ("moda", "mais") and "comum" in seg:
                                val = "mais_comum"
                            else:
                                v, _ = self._value_after(i + 1, e, multi=True)
                                if v is not None:
                                    val = nlp.num_fmt(v) if isinstance(v, float) else v.upper()
                            self._mark(i, 2)
                            break
                    params["valor"] = val
                    cols = self._use_cols(self._cols_in(t, s, e))
                    if cols:
                        params["colunas"] = cols
                elif o == "validar_documentos":
                    cols = self._use_cols(self._cols_in(t, s, e, types=DOCS))
                    if not cols:
                        cols = [c for c in info["cols"] if info["types"].get(c) in ("cpf", "cnpj", "email")]
                        sc *= 0.9
                    if not cols:
                        self.notes.append(f"Nenhuma coluna de CPF/CNPJ/e-mail encontrada em {t}.")
                        continue
                    params["colunas"] = cols
                    if re.search(r"invalid", seg) and re.search(r"apena|somente|so |mostr|list|filtr|separ|quais", text):
                        params["somente_invalidos"] = True
                    for c in cols:
                        info["cols"].append(f"{c}_valido")
                        info["types"][f"{c}_valido"] = "booleano"
                elif o == "filtrar":
                    negate = bool(re.match(r"(remov|exclu|tir)", m["txt"])) or m["txt"] == "exceto"
                    conds = self._parse_conditions(t, m["ti"], e, negate=negate)
                    if not conds:
                        if not m["fraco"]:
                            self.notes.append("Entendi que há um filtro, mas não identifiquei a condição.")
                            scores.append(0.3)
                        continue
                    params["condicoes"] = conds
                    if re.search(r"\bou\b", seg) and len(conds) > 1 and conds[0]["coluna"] == conds[1]["coluna"]:
                        params["logica"] = "ou"
                elif o in ("selecionar_colunas", "remover_colunas"):
                    cols = self._use_cols(self._cols_in(t, s, e))
                    if not cols:
                        continue
                    params["colunas"] = cols
                    if o == "selecionar_colunas":
                        info["cols"] = [c for c in info["cols"] if c in cols]
                    else:
                        info["cols"] = [c for c in info["cols"] if c not in cols]
                elif o == "contar_por":
                    pi = self._find_por(s, e)
                    if pi:
                        cols = self._use_cols(self._cols_in(t, pi, e))
                        pre_cols = [c for c in self._cols_in(t, s, pi - 1) if info["types"].get(c[2]) in ("categoria", "booleano") and c[2] not in cols]
                    else:
                        found = self._cols_in(t, s, e)
                        cols = self._use_cols([c for c in found if not _is_num_type(info["types"].get(c[2]))])
                        pre_cols = []
                    implicit = self._parse_conditions(t, s, (pi - 1) if pi else e, allow_cat=True) if pi else []
                    implicit = [c for c in implicit if c["coluna"] not in cols]
                    if implicit:
                        implicit_filter = {"op": "filtrar", "entradas": [t], "params": {"condicoes": implicit}, "saida": t}
                    elif pre_cols and not cols:
                        cols = self._use_cols(pre_cols)
                    if pi and not cols:
                        sc *= 0.5
                        self.notes.append("Não identifiquei a coluna depois de “por”; contei o total.")
                    params["colunas"] = cols
                    out = self._new_alias(("contagem_" + "_".join(nlp.slug(c, 15) for c in cols[:2])) if cols else "contagem_total")
                    self._register(out, cols + ["quantidade", "percentual"] if cols else ["total_registros"],
                                   {**{c: info["types"].get(c) for c in cols}, "quantidade": "numero", "percentual": "numero"})
                    last_value_col[out] = "quantidade" if cols else "total_registros"
                elif o == "agrupar":
                    tx = m["txt"]
                    agg = "media" if re.search(r"media|medio", tx + " " + seg[:30]) else "max" if re.search(r"maximo|maior valor", tx) \
                        else "min" if re.search(r"minimo|menor valor", tx) else "mediana" if "mediana" in tx else "soma"
                    pi = self._find_por(s, e)
                    group = self._use_cols(self._cols_in(t, pi, e)) if pi else []
                    vals_m = [c for c in self._cols_in(t, s, (pi - 1) if pi else e) if _is_num_type(info["types"].get(c[2]))]
                    if not vals_m:
                        vals_m = [c for c in self._cols_in(t, m["pre"], m["ti"]) if _is_num_type(info["types"].get(c[2]))]
                    vals = self._use_cols(vals_m)
                    if not vals:
                        g = self._guess_value_col(t)
                        if not g:
                            if not m["fraco"]:
                                self.notes.append("Não encontrei coluna numérica para somar/calcular.")
                            continue
                        vals = [g]
                        sc *= 0.7
                        self.notes.append(f"Coluna de valor inferida: “{g}”.")
                    implicit = self._parse_conditions(t, s, (pi - 1) if pi else e) if pi else []
                    implicit = [c for c in implicit if c["coluna"] not in group + vals]
                    if implicit:
                        implicit_filter = {"op": "filtrar", "entradas": [t], "params": {"condicoes": implicit}, "saida": t}
                    params = {"por": group, "valores": vals, "agregacao": agg}
                    out = self._new_alias(f"{agg}_" + ("_".join(nlp.slug(c, 15) for c in group[:2]) if group else "geral"))
                    self._register(out, group + vals, {**{c: info["types"].get(c) for c in group}, **{v: "numero" for v in vals}})
                    last_value_col[out] = vals[0]
                elif o == "tabela_dinamica":
                    found = self._cols_in(t, s, e)
                    nums = [c[2] for c in found if _is_num_type(info["types"].get(c[2]))]
                    cats = [c[2] for c in found if not _is_num_type(info["types"].get(c[2]))]
                    self._use_cols(found)
                    if not cats:
                        continue
                    params = {"linhas": cats[:1], "colunas": cats[1:2], "valores": nums[:1],
                              "agregacao": "media" if "media" in seg else ("soma" if nums else "contagem")}
                    out = self._new_alias("tabela_dinamica")
                    self._register(out, cats[:1] + ["TOTAL"], {})
                elif o == "estatisticas":
                    out = self._new_alias(f"estatisticas_{t}")
                    self._register(out, ["coluna", "tipo", "preenchidos", "vazios", "unicos", "minimo", "maximo", "media", "soma"], {})
                elif o == "ordenar":
                    cols = self._use_cols(self._cols_in(t, s, e))
                    desc = bool(re.search(r"decrescente|maior para (o )?menor|mais recente|do maior|\bdesc\b|z a a", seg))
                    if not cols:
                        lv = last_value_col.get(t)
                        if lv:
                            cols, desc = [lv], (desc or not re.search(r"crescente|menor para", seg))
                        else:
                            continue
                    params = {"colunas": cols, "crescente": not desc}
                elif o == "top_n":
                    mm = re.search(r"top ?(\d+)|(\d+) (maior|menor|primeiro|melhor|pior)", seg)
                    n = int(mm.group(1) or mm.group(2)) if mm else 10
                    if mm:
                        for i in range(m["ti"], min(e, m["ti"] + 3)):
                            if self.toks[i].isdigit():
                                self._mark(i)
                    big = not re.search(r"menor|pior", seg)
                    cm = [c for c in self._cols_in(t, s, e) if _is_num_type(info["types"].get(c[2]))]
                    col = self._use_cols(cm)[0] if cm else last_value_col.get(t) or self._guess_value_col(t)
                    if not col:
                        continue
                    if not cm and not last_value_col.get(t):
                        sc *= 0.7
                    params = {"coluna": col, "n": n, "maiores": big}
                    cats = [c for c in self._cols_in(t, s, e) if info["types"].get(c[2]) in ("categoria", "texto") and c[2] != col]
                    if cats and t not in last_value_col:
                        gcol = self._use_cols(cats[:1])[0]
                        gout = self._new_alias(f"soma_{nlp.slug(gcol, 20)}")
                        steps.append({"op": "agrupar", "entradas": [t], "params": {"por": [gcol], "valores": [col], "agregacao": "media" if "media" in seg else "soma"}, "saida": gout})
                        scores.append(sc)
                        self._register(gout, [gcol, col], {gcol: info["types"].get(gcol), col: "numero"})
                        last_value_col[gout] = col
                        set_latest(d, t, gout)
                        t = gout
                        out = t
                        info = self.env[t]
                    if t not in last_value_col and t in aliases:
                        out = self._new_alias(f"top{n}_{nlp.slug(col, 20)}")
                        self._register(out, ["posicao"] + info["cols"], dict(info["types"]))
                elif o == "dividir_por":
                    pi = self._find_por(m["ti"], e) or s
                    cm = self._cols_in(t, pi, e)
                    if not cm:
                        continue
                    params = {"coluna": self._use_cols(cm[:1])[0]}
                    out = self._new_alias("aba")
                else:
                    continue

                if implicit_filter:
                    steps.append(implicit_filter)
                    scores.append(0.9)
                steps.append({"op": o, "entradas": [t], "params": params, "saida": out})
                scores.append(sc * (0.85 if m.get("implicito") else 1.0))
                if out != t:
                    set_latest(d, t, out)

        # filtro implícito: condições citadas sem a palavra "filtrar" (ex.: "clientes sem telefone")
        if not any(st["op"] == "filtrar" for st in steps):
            if combined:
                cand = [(None, combined)]
            elif len(aliases) == 1:
                cand = [(aliases[0], aliases[0])]
            else:
                hits = {a: len(self._cols_in(a, 0, len(self.toks))) + sum(len(v) for v in self._cat_values(a, 0, len(self.toks)).values()) for a in aliases}
                pref = global_ds[0] if len(global_ds) == 1 else max(hits, key=hits.get)
                cand = [(pref, pref)]
            d, tgt = cand[0]
            src = tgt
            if d is None:
                src = combined
                # filtra logo após a combinação
                pos = next((i + 1 for i, st in enumerate(steps) if st["saida"] == combined), len(steps))
            conds = self._parse_conditions(src if src in self.env else tgt, 0, len(self.toks), skip_explained=True)
            if conds:
                if d is None:
                    steps.insert(pos, {"op": "filtrar", "entradas": [combined], "params": {"condicoes": conds}, "saida": combined})
                else:
                    pos = next((i for i, st in enumerate(steps) if PHASE.get(st["op"], 0) >= 2 and d in st["entradas"]), len(steps))
                    steps.insert(pos, {"op": "filtrar", "entradas": [d], "params": {"condicoes": conds}, "saida": d})
                scores.append(0.85)
                self.notes.append("Condição de filtro identificada no comando.")

        content = [i for i, tk in enumerate(self.toks) if tk not in nlp.STOPWORDS and len(tk) > 1 and not NEUTRAL.match(tk)]
        coverage = len([i for i in content if i in self.explained]) / max(1, len(content))
        if not steps:
            conf = 0.0
        else:
            conf = 0.65 * (sum(scores) / len(scores)) + 0.35 * coverage
        unexplained = [self.raw[i] for i in content if i not in self.explained]
        return {"passos": steps, "confianca": round(conf, 3), "cobertura": round(coverage, 3),
                "notas": self.notes, "nao_entendido": unexplained}


# ===========================================================================
# Agente de IA externa — usado sob demanda
# ===========================================================================
SYSTEM_PROMPT = """Você é o Planejador do DataForge, um sistema que transforma pedidos em português em planos de \
processamento de planilhas. Você NÃO vê os dados reais — apenas a estrutura e amostras mascaradas.

Responda SOMENTE com um JSON válido, sem texto antes ou depois, no formato:
{"passos":[{"op":"<operação>","entradas":["<alias>"],"params":{...},"saida":"<alias>"}],"explicacao":"<1-3 frases>"}

Regras:
- Use apenas operações do catálogo e exatamente os nomes de planilhas (alias) e colunas informados.
- "entradas": planilhas originais ou saídas de passos anteriores. Operações de duas planilhas (cruzar, comparar) usam 2 entradas; unificar usa 2 ou mais.
- "saida": use o mesmo alias da entrada para transformar no lugar; use um nome novo e curto (snake_case) para tabelas derivadas (contagens, agrupamentos, cruzamentos).
- Ordem lógica: limpeza → validação → combinação → filtros/colunas → agregação → ordenação/top → separar em abas.
- Valores de filtro devem seguir os valores de categoria informados quando existirem.
- Se o pedido for ambíguo, escolha a interpretação mais útil para um analista de dados e explique em "explicacao".
- Se houver um rascunho do intérprete local, corrija-o e complete-o; não o copie se estiver errado."""


def _urllib_post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except urllib.error.URLError as e:
        return 0, f"sem conexão: {e.reason}"


# transporte HTTP substituível (a versão web injeta uma função baseada em fetch/XHR)
HTTP_POST = _urllib_post


class AIAgent:
    def __init__(self, cfg):
        self.cfg = cfg

    def available(self):
        return bool(self.cfg.get("api_key") and self.cfg.get("api_url") and (self.cfg.get("modelo_simples") or self.cfg.get("modelo_complexo")))

    def _openai_style(self):
        u = (self.cfg.get("api_url") or "").lower()
        return "chat/completions" in u or "openrouter" in u

    def _post(self, payload):
        if self._openai_style():
            headers = {"content-type": "application/json", "authorization": f"Bearer {self.cfg['api_key']}",
                       "x-title": "DataForge"}
            msgs = ([{"role": "system", "content": payload["system"]}] if payload.get("system") else []) + payload["messages"]
            body = {"model": payload["model"], "messages": msgs, "max_tokens": payload.get("max_tokens", 1000)}
            if "temperature" in payload:
                body["temperature"] = payload["temperature"]
            payload = body
        else:
            headers = {"content-type": "application/json", "x-api-key": self.cfg["api_key"],
                       "anthropic-version": "2023-06-01", "anthropic-dangerous-direct-browser-access": "true"}
        last = None
        for attempt in range(3):
            code, text = HTTP_POST(self.cfg.get("api_url"), headers, json.dumps(payload), int(self.cfg.get("timeout_ia", 60)))
            if 200 <= code < 300:
                return self._normalize(json.loads(text))
            last = f"HTTP {code}: {text[:300]}" if code else text
            if code in (0, 429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        raise RuntimeError(last or "falha desconhecida")

    @staticmethod
    def _normalize(data):
        """Converte respostas no formato chat/completions para o formato interno."""
        if "choices" in data:
            msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
            u = data.get("usage") or {}
            return {"content": [{"type": "text", "text": msg.get("content") or ""}],
                    "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}}
        return data

    def ping(self, model):
        data = self._post({"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "responda: ok"}]})
        return "".join(b.get("text", "") for b in data.get("content", []))

    def plan(self, command, profiles, draft, model):
        send_vals = self.cfg.get("enviar_valores_categoria", True)
        sheets = []
        for alias, p in profiles.items():
            cols = []
            for c in p["colunas"]:
                item = {"nome": c["nome"], "tipo": c["tipo"], "exemplos": c.get("exemplos", [])[: int(self.cfg.get("amostra_linhas", 5))]}
                if send_vals and c.get("valores") and c["tipo"] in ("categoria", "booleano"):
                    item["valores"] = c["valores"][:25]
                if c["tipo"] in NUMERIC and "minimo" in c:
                    item["faixa"] = [c["minimo"], c["maximo"]]
                cols.append(item)
            sheets.append({"alias": alias, "linhas": p["linhas"], "colunas": cols})
        cat = [{"op": o["nome"], "descricao": o["descricao"], "params": o["params"]} for o in operations.catalog()]
        user = {"comando": command, "planilhas": sheets, "catalogo": cat}
        if draft and draft.get("passos"):
            user["rascunho_local"] = {"passos": draft["passos"], "confianca": draft.get("confianca"), "notas": draft.get("notas")}
        payload = {"model": model, "max_tokens": int(self.cfg.get("max_tokens", 2000)),
                   "temperature": float(self.cfg.get("temperatura", 0)), "system": SYSTEM_PROMPT,
                   "messages": [{"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)}]}
        data = self._post(payload)
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        usage = data.get("usage", {})
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError("a IA não retornou JSON")
        plan = json.loads(m.group(0))
        return plan, {"tokens_entrada": usage.get("input_tokens", 0), "tokens_saida": usage.get("output_tokens", 0)}


# ===========================================================================
# Validador — corrige e testa o plano em amostra
# ===========================================================================
COL_PARAMS = ("colunas", "por", "valores", "linhas", "colunas_direita")


def _fix_col(name, cols, cut=0.7):
    if name in cols:
        return name
    cn = nlp.canon(name)
    for c in cols:
        if nlp.canon(c) == cn:
            return c
    best = max(cols, key=lambda c: nlp.ratio(nlp.canon(c), cn), default=None)
    if best is not None and nlp.ratio(nlp.canon(best), cn) >= cut:
        return best
    return None


def validate(plan: dict, datasets: dict, sample_rows: int = 300) -> dict:
    env = {k: v.head(sample_rows).copy() for k, v in datasets.items()}
    fixed, warnings = [], []
    for i, st in enumerate(plan.get("passos", []) or [], 1):
        st = json.loads(json.dumps(st, default=str))
        o = st.get("op")
        if o not in REGISTRY:
            warnings.append(f"Passo {i}: operação “{o}” não existe — ignorado.")
            continue
        ins = st.get("entradas") or []
        if isinstance(ins, str):
            ins = [ins]
        ins2 = []
        for a in ins:
            if a in env:
                ins2.append(a)
            else:
                alt = _fix_col(a, list(env), 0.75)
                if alt:
                    ins2.append(alt)
        if not ins2 and len(datasets) == 1:
            ins2 = [list(datasets)[0]]
        need = 2 if REGISTRY[o]["multi"] else 1
        if len(ins2) < need:
            warnings.append(f"Passo {i} ({o}): planilhas de entrada inválidas — ignorado.")
            continue
        st["entradas"] = ins2
        p = st.setdefault("params", {}) or {}
        st["params"] = p
        cols0 = list(env[ins2[0]].columns)
        ok = True
        for k in COL_PARAMS:
            if k in p and p[k]:
                vals = p[k] if isinstance(p[k], list) else [p[k]]
                base = list(env[ins2[1]].columns) if k == "colunas_direita" and len(ins2) > 1 else cols0
                new = []
                for c in vals:
                    fc = _fix_col(str(c), base)
                    if fc:
                        new.append(fc)
                    else:
                        warnings.append(f"Passo {i}: coluna “{c}” não encontrada — removida do passo.")
                p[k] = new if isinstance(p[k], list) else (new[0] if new else None)
        if "coluna" in p and p["coluna"]:
            fc = _fix_col(str(p["coluna"]), cols0)
            if fc:
                p["coluna"] = fc
            else:
                warnings.append(f"Passo {i}: coluna “{p['coluna']}” não encontrada.")
                ok = False
        if o in ("cruzar", "comparar"):
            ka = _fix_col(str(p.get("chave_esquerda", "")), cols0)
            kb = _fix_col(str(p.get("chave_direita") or p.get("chave_esquerda", "")), list(env[ins2[1]].columns))
            if not ka or not kb:
                warnings.append(f"Passo {i}: chave de cruzamento inválida — ignorado.")
                continue
            p["chave_esquerda"], p["chave_direita"] = ka, kb
        if o == "filtrar":
            conds = []
            for c in p.get("condicoes", []) or []:
                fc = _fix_col(str(c.get("coluna", "")), cols0)
                if fc:
                    c["coluna"] = fc
                    conds.append(c)
                else:
                    warnings.append(f"Passo {i}: coluna de filtro “{c.get('coluna')}” não encontrada.")
            if not conds:
                continue
            p["condicoes"] = conds
        if o == "agrupar" and not p.get("valores"):
            warnings.append(f"Passo {i}: agrupamento sem coluna de valor — ignorado.")
            continue
        if not ok:
            continue
        st.setdefault("saida", ins2[0])
        if not st.get("saida"):
            st["saida"] = ins2[0]
        st["saida"] = nlp.slug(st["saida"], 40) if st["saida"] not in env else st["saida"]
        # dry-run na amostra
        try:
            params = dict(p)
            if REGISTRY[o]["multi"]:
                params["_aliases"] = ins2
                params["_alias_esquerda"] = ins2[0]
                params["_alias_direita"] = ins2[1] if len(ins2) > 1 else ""
            out = REGISTRY[o]["fn"]([env[a] for a in ins2], params)
            if isinstance(out, dict):
                for k, v in out.items():
                    env[f"{st['saida']}_{k}"] = v
            else:
                env[st["saida"]] = out
        except Exception as e:
            warnings.append(f"Passo {i} ({REGISTRY[o]['label']}): falhou no teste — {e}")
            continue
        fixed.append(st)
    return {"passos": fixed, "avisos": warnings, "total_original": len(plan.get("passos", []) or [])}


# ===========================================================================
# Orquestrador — o cérebro
# ===========================================================================
COMPLEX_WORDS = r"\bse\b|caso|calcul|percentu|%|proporc|variacao|crescimento|compar|acumulad|media movel|regra|condic|exceto|desde que|cada um|respectiv"


def complexity(command, ctx, draft):
    c = 0
    c += max(0, len(ctx) - 1)
    c += max(0, len(draft.get("passos", [])) - 2)
    c += 2 if draft.get("confianca", 0) < 0.3 else 0
    c += 1 if len(command) > 140 else 0
    c += 2 if re.search(COMPLEX_WORDS, nlp.canon(command)) else 0
    return c


def _remap_plan(plan, mapping):
    s = json.dumps(plan, ensure_ascii=False)
    # substitui aliases antigos (mais longos primeiro) pelos atuais
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        if old != new:
            s = re.sub(rf'(?<![\w]){re.escape(old)}(?![\w])', new, s)
    return json.loads(s)


class Orchestrator:
    def __init__(self, cfg):
        self.cfg = cfg

    def plan(self, command: str, datasets: dict, profiles: dict) -> dict:
        cfg = self.cfg
        t0 = time.perf_counter()
        trace = []

        def log(agente, msg, nivel="info"):
            trace.append({"agente": agente, "mensagem": msg, "nivel": nivel,
                          "ms": round((time.perf_counter() - t0) * 1000)})

        ctx = build_context(datasets, profiles)
        log("Contexto", f"{len(ctx)} planilha(s): " + "; ".join(f"{a} ({v['linhas']} linhas, {len(v['cols'])} colunas)" for a, v in ctx.items()))
        modo = cfg.get("modo", "hibrido")
        limiar = float(cfg.get("limiar_confianca", 0.65))
        candidates = []

        # 1) Memória
        if cfg.get("usar_memoria", True):
            sims = storage.find_similar(command, {a: v["cols"] for a, v in ctx.items()})
            if sims:
                best = sims[0]
                log("Memória", f"Plano parecido encontrado (score {best['score']:.2f} • texto {best['texto']:.2f} • estrutura {best['estrutura']:.2f}): “{best['job']['comando'][:80]}”")
                if best["score"] >= float(cfg.get("limiar_memoria", 0.82)):
                    adapted = _remap_plan(best["job"]["plano"], best["mapa"])
                    v = validate(adapted, datasets)
                    if v["passos"] and len(v["passos"]) == v["total_original"]:
                        candidates.append({"estrategia": "memoria", "passos": v["passos"], "confianca": best["score"],
                                           "avisos": v["avisos"], "memoria_job": best["job"]["id"], "modelo": "—"})
                        log("Memória", "Plano adaptado e validado na amostra com sucesso.")
                    else:
                        log("Memória", "Plano antigo não se encaixa nas planilhas atuais — descartado.", "aviso")
                else:
                    log("Memória", f"Abaixo do limiar de reuso ({cfg.get('limiar_memoria')}).")
            else:
                log("Memória", "Nenhum plano anterior semelhante.")

        # 2) Intérprete local
        local = LocalInterpreter(cfg, datasets).interpret(command, ctx)
        vloc = validate(local, datasets)
        conf_local = local["confianca"] * (len(vloc["passos"]) / max(1, len(local["passos"])) if local["passos"] else 0)
        log("Intérprete", f"{len(vloc['passos'])} passo(s) • confiança {conf_local:.2f} • cobertura do comando {local['cobertura']:.0%}")
        for n in local["notas"]:
            log("Intérprete", n, "aviso")
        if local["nao_entendido"]:
            log("Intérprete", "Termos não mapeados: " + ", ".join(local["nao_entendido"][:12]), "aviso")
        if vloc["passos"]:
            candidates.append({"estrategia": "local", "passos": vloc["passos"], "confianca": round(conf_local, 3),
                               "avisos": vloc["avisos"], "modelo": "regras locais"})

        cx = complexity(command, ctx, local)
        simples = cfg.get("modelo_simples") or cfg.get("modelo_complexo") or ""
        complexo = cfg.get("modelo_complexo") or simples
        model = complexo if cx >= int(cfg.get("limiar_complexidade", 3)) else simples
        log("Roteador", f"Complexidade {cx} → modelo indicado: {model or 'nenhum configurado'}")

        best = max(candidates, key=lambda c: c["confianca"]) if candidates else None
        need_ai = modo == "ia" or (modo == "hibrido" and (best is None or best["confianca"] < limiar))
        uso = {"tokens_entrada": 0, "tokens_saida": 0, "custo_usd": 0.0}

        if need_ai:
            agent = AIAgent(cfg)
            if not agent.available():
                log("Roteador", "A confiança está baixa e a IA externa não está configurada — seguindo com o melhor plano local." if best else
                    "Não entendi o comando e a IA externa não está configurada.", "aviso")
            else:
                log("IA", f"Consultando {model} com metadados mascarados (nenhum dado real é enviado)…")
                try:
                    ai_plan, usage = agent.plan(command, profiles, local, model)
                    pin, pout = (("preco_entrada_complexo", "preco_saida_complexo") if model == cfg["modelo_complexo"]
                                 else ("preco_entrada_simples", "preco_saida_simples"))
                    custo = usage["tokens_entrada"] / 1e6 * float(cfg[pin]) + usage["tokens_saida"] / 1e6 * float(cfg[pout])
                    uso = {**usage, "custo_usd": round(custo, 6)}
                    va = validate(ai_plan, datasets)
                    total = max(1, va["total_original"])
                    conf_ai = 0.92 * len(va["passos"]) / total
                    log("IA", f"Plano recebido: {len(va['passos'])}/{va['total_original']} passos válidos • "
                                  f"{usage['tokens_entrada']}+{usage['tokens_saida']} tokens • US$ {custo:.4f}")
                    if ai_plan.get("explicacao"):
                        log("IA", ai_plan["explicacao"])
                    if va["passos"]:
                        candidates.append({"estrategia": "ia" if not candidates else "hibrido", "passos": va["passos"],
                                           "confianca": round(conf_ai, 3), "avisos": va["avisos"], "modelo": model,
                                           "explicacao": ai_plan.get("explicacao", "")})
                        best = candidates[-1]
                except Exception as e:
                    log("IA", f"Falha na IA: {e}. Usando fallback local.", "erro")
                    best = max([c for c in candidates if c["estrategia"] != "ia"], key=lambda c: c["confianca"], default=None)
        elif best:
            log("Roteador", f"Confiança {best['confianca']:.2f} ≥ limiar {limiar} — executando sem custo de IA.")

        if not best:
            return {"passos": [], "estrategia": "nenhuma", "confianca": 0, "trace": trace, "avisos": [],
                    "complexidade": cx, "modelo": "—", "uso": uso, "descricoes": [],
                    "sugestao": "Tente descrever com verbos como limpar, cruzar, filtrar, contar, somar, ordenar, unificar."}

        log("Orquestrador", f"Estratégia escolhida: {best['estrategia'].upper()} ({len(best['passos'])} passos)", "sucesso")
        return {
            "passos": best["passos"],
            "estrategia": best["estrategia"],
            "confianca": best["confianca"],
            "modelo": best.get("modelo", "—"),
            "complexidade": cx,
            "avisos": best.get("avisos", []),
            "trace": trace,
            "uso": uso,
            "memoria_job": best.get("memoria_job"),
            "explicacao": best.get("explicacao", ""),
            "descricoes": [operations.describe(s) for s in best["passos"]],
            "alternativas": [{"estrategia": c["estrategia"], "confianca": c["confianca"], "passos": len(c["passos"])} for c in candidates],
        }
