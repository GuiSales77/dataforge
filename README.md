# DataForge — automação inteligente de planilhas

Descreva em português o que você precisa — *"cruzar clientes com vendas pelo CPF e somar o valor por estado"* —
e o DataForge entende o pedido, monta um plano, testa, executa e devolve um Excel pronto.

**As planilhas são processadas no navegador de quem usa.** Nenhum arquivo é enviado para servidores.

## Recursos

- Upload de várias planilhas ao mesmo tempo (.xlsx, .xlsm, .xls, .csv, .json); cada aba vira uma tabela com apelido próprio.
- Detecção automática de tipos: CPF, CNPJ, e-mail, telefone, CEP, data, número, moeda, categoria.
- Diagnóstico de qualidade: duplicadas, células vazias, CPF/CNPJ inválidos.
- 23 operações combináveis: limpeza, formatação de documentos, validação, cruzamento (PROCV/JOIN), comparação,
  unificação, filtros, contagens, somas/médias, tabela dinâmica, ranking, ordenação e separação em abas.
- Motor de interpretação próprio em português (offline), com memória que reaproveita planos aprovados.
- IA externa opcional para pedidos mais livres — recebe apenas a estrutura das planilhas e amostras mascaradas.
- Excel de saída formatado com abas de resultado, resumo e log.
- Histórico, aprendizado por feedback (👍/👎) e configurações salvos no próprio navegador.

## Arquitetura

| Componente | Função |
|---|---|
| Perfilador | Lê os arquivos, detecta tipos e mede a qualidade dos dados |
| Memória | Encontra pedidos parecidos já aprovados e adapta o plano à estrutura atual |
| Intérprete | Entende o comando: planilhas, colunas, valores, operadores e chaves de cruzamento |
| Roteador | Calcula confiança e complexidade e decide se executa localmente ou consulta a IA |
| Validador | Corrige nomes e testa o plano numa amostra antes de tocar nos dados |
| Executor / Relator | Executa as operações e gera o Excel final |

O núcleo é escrito em Python (`dataforge/`) e roda no navegador via [Pyodide](https://pyodide.org) dentro de um Web Worker.
O mesmo código também roda como aplicação local (`python main.py`).

## Publicar no GitHub Pages

1. Crie um repositório e envie todos os arquivos desta pasta para a branch `main`.
2. No repositório: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Pronto: a cada push o workflow `.github/workflows/pages.yml` publica o site em
   `https://<seu-usuario>.github.io/<nome-do-repositorio>/`.

## Rodar localmente

```bash
pip install -r requirements.txt
python main.py
```

Abre em `http://127.0.0.1:8765`. Os dados ficam na pasta `dados/`.

## IA externa (opcional)

Em **Configurações → IA externa**, informe a chave, o endpoint de mensagens e o ID do(s) modelo(s).
Sem isso, o sistema funciona normalmente com o motor local. A chave fica salva apenas no navegador de quem a cadastrou.

## Estrutura

```
├── index.html          interface
├── worker.js           motor no navegador (Pyodide)
├── dataforge/          núcleo em Python
│   ├── agents.py       intérprete, roteador, validador e orquestrador
│   ├── operations.py   operações e executor
│   ├── profiler.py     leitura, tipos, máscaras, validação de documentos
│   ├── storage.py      memória, histórico e auditoria (SQLite)
│   ├── nlp.py          normalização e similaridade em português
│   ├── exporter.py     geração do Excel
│   ├── api.py          rotas da aplicação
│   ├── server.py       servidor local
│   └── config.py       parâmetros
└── main.py             execução local
```
