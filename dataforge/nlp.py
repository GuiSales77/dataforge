"""Processamento de linguagem natural leve (português), sem dependências externas."""
import re
import unicodedata
from difflib import SequenceMatcher

STOPWORDS = set("""
a o os as de da do das dos e em um uma uns umas para pra por com que no na nos nas ao aos
pelo pela pelos pelas se sua seu suas seus ser foi sao todas todos toda todo cada essa esse
esta este isso isto me minha meu meus minhas planilha arquivo base dado coluna campo tabela
linha registro depois entao tambem gerar gere gera fazer faca faz quero preciso gostaria favor
deve deveria pode poderia mostrar mostre traga relatorio informacao lista listar liste nele nela
qual quais como ja ainda sobre entre vez seguir seguida apos isso la aqui ele ela eles elas
item valor dele dela usando via ou atraves resultado final novo nova numa num the and
""".split())

# palavras que não devem ser singularizadas
_PROTECT = {"mais", "menos", "apenas", "antes", "depois", "entre", "tres", "pais", "simples",
            "status", "vezes", "ambos", "ambas", "atras", "lapis", "onibus", "gratis", "mes",
            "cpf", "cnpj", "cep", "sms", "gas", "jus", "bonus", "campus", "virus", "ais"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _date_iso(m):
    d, mth, y = m.group(1), m.group(2), m.group(3)
    if len(y) == 2:
        y = "20" + y
    return f"{y}-{int(mth):02d}-{int(d):02d}"


def norm(s) -> str:
    """minúsculas, sem acento, números no formato 1234.56, datas em ISO."""
    s = strip_accents(str(s)).lower()
    s = s.replace("r$", " ")
    s = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", _date_iso, s)
    for _ in range(3):
        s = re.sub(r"(\d)\.(\d{3})(?!\d)", r"\1\2", s)
    s = re.sub(r"(\d),(\d)", r"\1.\2", s)
    s = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", s)
    s = re.sub(r"(?<!\d)-|-(?!\d)", " ", s)
    s = re.sub(r"[_/\\|()\[\]{}:;,!?*+#@&~^`´]", " ", s)
    s = re.sub(r"[^a-z0-9<>=%\-\.\"'\s]", " ", s)
    s = s.replace('"', " ").replace("'", " ")
    return re.sub(r"\s+", " ", s).strip()


def singular(tok: str) -> str:
    if len(tok) <= 3 or tok in _PROTECT or tok[0].isdigit():
        return tok
    for suf, rep in (("oes", "ao"), ("aes", "ao"), ("ais", "al"), ("eis", "el"),
                     ("ois", "ol"), ("res", "r"), ("zes", "z"), ("ns", "m")):
        if tok.endswith(suf) and len(tok) > len(suf) + 1:
            return tok[: -len(suf)] + rep
    if tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def canon(s) -> str:
    return " ".join(singular(t) for t in norm(s).split())


def tokens(s) -> list:
    return canon(s).split()


def content_tokens(s) -> list:
    return [t for t in tokens(s) if t not in STOPWORDS and len(t) > 1]


def ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def jaccard(a, b) -> float:
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


def slug(s: str, maxlen: int = 40) -> str:
    s = strip_accents(str(s)).lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or "planilha")[:maxlen]


def char_to_token_index(text: str, pos: int) -> int:
    return text[:pos].count(" ")


def find_mentions(toks: list, names: list, cutoff: float = 0.8, start: int = 0, end: int = None):
    """Procura nomes (colunas, planilhas, valores) dentro de uma lista de tokens.
    Retorna [(indice_token, tamanho, nome, score)] sem sobreposição, ordenado por posição."""
    end = len(toks) if end is None else end
    cands = []
    for name in names:
        ntoks = tokens(name)
        if not ntoks:
            continue
        L = len(ntoks)
        target = " ".join(ntoks)
        compact = target.replace(" ", "")
        best = None
        for i in range(start, max(start, end - L + 1)):
            window = " ".join(toks[i:i + L])
            if window == target:
                sc = 1.0
            elif len(target) < 4:
                continue
            else:
                sc = ratio(window, target)
            if sc >= cutoff and (best is None or sc > best[3]):
                best = (i, L, name, sc)
        # nome composto escrito junto (ex.: "datacadastro") ou nome com 1 token vs 2 tokens
        if best is None and L > 1:
            for i in range(start, end):
                if toks[i] == compact or (len(compact) > 5 and abs(len(toks[i]) - len(compact)) <= 1 and ratio(toks[i], compact) >= max(cutoff, 0.9)):
                    best = (i, 1, name, 0.9)
                    break
        if best:
            cands.append(best)
    cands.sort(key=lambda c: (-c[3], -c[1]))
    taken, out = set(), []
    for c in cands:
        span = set(range(c[0], c[0] + c[1]))
        if span & taken:
            continue
        taken |= span
        out.append(c)
    out.sort(key=lambda c: c[0])
    return out


def parse_number(tok: str):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return None


def num_fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
