# Changelog — fork `urlan/patches`

Pilha de mudanças sobre o upstream `main` em `df37eb9`
("Readme: novas instruções sobre tools bookmark e whatsapp").
Branch: `urlan/patches`. Suíte de testes da pilha: 330 passaram, 2 pularam
(os 2 pulos são o smoke runtime, opcional por `IPED_URL`).

Legenda de cada item:
- **[Consumidor]** — mudança visível a quem consome o servidor (tools,
  shapes de resposta, variáveis de ambiente, comportamento de erro).
- **[Interno]** — transparência: não altera o contrato visto pelo agente.
- **[fork]** — pensado para o ambiente do fork (WebAPI customizada /
  deploy ICARO); ver também a seção "Candidato a upstream vs. fork".

Itens em ordem de implementação (o hash é o commit na branch).

---

## M-1 — `743d581` — Suíte pública de testes do cliente WebAPI — [Interno]

- Cria `tests/` (o README upstream já prometia a seção; não existia):
  contratos do `IPEDClient` contra um fake da WebAPI, smoke de todas as
  tools, e smoke runtime **opt-in** (marca `runtime`; roda só com
  `IPED_URL` definida, senão pula).
- `pyproject.toml`: grupo dev `pytest` + `testpaths` + registradores das
  marcas `runtime` e `characterization`.

## M-5 — `9563fe9` — Launcher da WebAPI escuta em loopback por padrão — [Consumidor][fork]

- `web-api-launcher.py` sobe cada instância da WebAPI em `127.0.0.1`
  (antes: `0.0.0.0`, exposto à rede). Expor exige `--host` explícito, com
  aviso: a WebAPI **não tem autenticação**.
- Novo `--start-port` (padrão 1234) para escolher a primeira porta.

## M-2 — `e0095e8` — Política única de erros HTTP — [Consumidor]

- Toda falha de rede/HTTP do cliente é traduzida na fronteira para a
  taxonomia `IpedError` (`errors.py`): `IpedConnectionError`,
  `IpedTimeoutError`, `IpedResponseError`, `IpedHTTPError`
  (`ResourceNotFoundError` 404, `BadRequestError` 4xx, `ServerError` 5xx).
  O texto cru do `requests` ("404 Client Error: ... for url: ...") nunca
  chega ao agente.
- O doc inexistente no jar-alvo responde **404** (o upstream assumia 500):
  o diagnóstico assistido agora dispara no 404 e o contexto do doc entra na
  mensagem.
- Prefixos de mensagem estáveis (contrato de UX): `Source '<id>'
  indisponível`, `Source '<id>' não encontrado`, `Timeout no source '<id>'`,
  `Doc não encontrado`.
- Tradução centrada no `source_id` valendo para todas as famílias
  (antes: vazava `HTTPError` cru em `ufed_get_document` e nos bookmarks).

## M-3 — `88d68d1` — Retry + circuit breaker no cliente — [Consumidor][Interno]

- Falhas transitórias de **conexão/timeout** são re-tentadas até
  `IPED_MCP_RETRY_ATTEMPTS` (padrão 3) com backoff exponencial
  `IPED_MCP_RETRY_BACKOFF_BASE`^n (1 s, 2 s, 4 s…), teto por espera
  `IPED_MCP_RETRY_BACKOFF_CAP` (8 s). Erro 5xx de busca **não** é
  re-tentado (rejeição determinística da query).
- Circuit breaker por endpoint: `IPED_MCP_CB_FAILURE_THRESHOLD` falhas de
  conexão consecutivas (4) abrem o circuito; fail-fast por
  `IPED_MCP_CB_COOLDOWN_SECONDS` (60 s); reabertura decidida por sonda
  `GET /sources/` com timeout `IPED_MCP_CB_PROBE_TIMEOUT_SECONDS` (3 s).
- Interno: o retry duplicado que existia em `fetch.search_ids` foi
  absorvido pelo cliente (uma implementaçao só).

## M-4 — `1c31cd2` — Timeouts por classe de operação — [Consumidor]

- Substitui o timeout único de 10 s do upstream, que abortava operações
  longas legítimas. Classes com env própria (segundos; padrão):
  `IPED_MCP_TIMEOUT_DEFAULT` (30), `IPED_MCP_TIMEOUT_SEARCH` (30),
  `IPED_MCP_TIMEOUT_PROPS` (90 — `/docs/{id}` pode reabrir SQLite/Media.tar),
  `IPED_MCP_TIMEOUT_TEXT` (60), `IPED_MCP_TIMEOUT_CONNECT_DOWNLOAD` (10),
  `IPED_MCP_TIMEOUT_READ_DOWNLOAD` (120).
- Valores inválidos (não numéricos ou ≤ 0) falham no startup com mensagem
  explícita, não em tempo de requisição.

## M-6 — `10045d9` — Tool nova: censo do caso (`iped_case_summary`) — [Consumidor]

- Nova tool: `iped_case_summary(source_id)` responde "o que este caso
  contém?" — lê a taxonomia real do jar (`GET /categories`, endpoint novo
  no cliente) e conta documentos por categoria (`category:"<cat>"`), com
  concorrência limitada e cache (~5 min por source+catálogo).
- Apenas contagens no envelope (`total_categories`, `total_docs_counted`,
  `categories[{name,count}]`, `warnings`, `cached`); **nenhum id de
  documento** entra na resposta. Categoria que falha vira `count=-1` com
  aviso, sem derrubar o censo.

## M-7 — `b1d4c07` — Campos `ufed:` passam de hard-block a advertência — [Consumidor]

- A lista estática `INDEXED_FIELDS` (13 campos) deixa de rejeitar busca:
  campo fora da lista é **advertência** com hipóteses de causa, e a busca
  segue (a lista estava desatualizada vs. jar real — falso negativo mudo).
- `iped_ufed_list_fields` ganhou `sample_size` (1..200, padrão 10): amostra
  real de documentos e reporta `observed_fields` (campos presentes na
  amostra) ao lado da lista base — descoberta leve, sem catálogo fechado.

## M-8 — `7496ef1` — Texto ausente vira dado; teto de leitura configurável — [Consumidor]

- `iped_doc_text` não levanta mais exceção para doc sem texto: responde
  `{"status": "no_text", "no_text": true, "note": ...}` — também quando o
  corpo vem vazio ou o `offset` pedido já passou do fim. `status="ok"` traz
  paginação (`truncated`, `next_offset`).
- Teto de leitura do `/text`: era literal fixo de 10 MB no cliente; vira
  `IPED_MCP_TEXT_CAP` (bytes; padrão 10485760 — default preservado). Ao
  atingir o teto, `capped=true` com nota citando a env.
- O marcador `capped` propaga para os consumidores de texto (export de
  WhatsApp grava `[TRUNCADO em N bytes — capped]` e `attachment_capped`).

## M-9 — `7eb058b` — Feedback e lotes em operações longas — [Consumidor]

- Tools varredura/download/report ganham envelope de progresso:
  `phase` (`complete`/`partial`), `scanned`, `processed`, `elapsed_s`,
  `remaining_est`.
- `offset`/`limit` como **lote cliente-side** em `iped_regex_stats`,
  `iped_regex_top_values`, `iped_regex_export_csv` (a WebAPI não pagina:
  o corte é local; `batch.has_more` diz se falta, e a próxima chamada
  continua de `offset + limit`).

## M-10 — `cc38f5f` — Exports imutáveis e únicos — [Consumidor]

- Toda tool que grava disco (`doc_export`, `thumb_export`,
  `bookmarks_download*`, `regex_export_csv`, `whatsapp_export_conversation`,
  `whatsapp_profile_photo`) passa a usar nome único
  `<AAAAMMDDTHHMMSSZ>_<source>_<nome>[-N]<ext>` — timestamp **UTC** (antes,
  hora local em regex/whatsapp), `source_id` no nome (colisão entre
  servidores), sufixo `-N` em colisão no mesmo segundo e **nunca
  sobrescreve** export anterior (`overwrite=True` reabre o MESMO arquivo).
- Exports padrão em `IPED_MCP_EXPORTS_DIR` (antes a env só valia para 2
  tools; core/bookmarks exigiam `dest_dir`).

## M-12 — `a5f4bc4` — Bind do servidor MCP por env — [Consumidor][fork]

- O bind `127.0.0.1:8000` era fixo em `main.py`; vira `IPED_MCP_HOST` /
  `IPED_MCP_PORT` (padrões preservados: loopback:8000; expor continua
  opt-in). Necessário em container: dentro do container o loopback é
  inalcançável do host.
- Validação no startup: host vazio, porta não numérica ou fora de 1–65535
  falham cedo com mensagem explícita.

## M-13 — `ff226eb` — URL-quote de ids em todo o cliente — [Consumidor][Interno]

- `source_id`/`doc_id` (e nomes de bookmark) são escapados
  (`urllib.parse.quote`, com `@` preservado cru — ids reais de source usam
  `@`) em **todos** os caminhos montados pelo cliente. Antes o id era
  interpolado cru na URL: ids com espaço, `%` ou acentos geravam requisições
  malformadas (a armadilha de dupla escapagem `%20`→`%2520` ao montar a mão
  é exatamente o que a centralização no cliente elimina — `requote_uri` do
  requests idempotiza o caminho único).
- Interno: sanitização centralizada no cliente; tools não montam mais URL.

## M-14 — `ea806b7` — Validação de entrada uniforme — [Consumidor][Interno]

- Módulo único `validation.py`: inteiros, faixas (`check_int_range`),
  mínimos, contagens com dica (`check_count`) — aplicados de forma
  consistente em todas as famílias (antes: `limit` validado só em parte das
  tools; `bool` aceito onde deveria ser rejeitado).
- Revertido o passe-through especial de `ServerError` em `search_ids`:
  erros chegam como o contrato único M-2 (`ValueError` traduzida pelo
  router), mantido apenas o ramo defensivo.

## M-17 — `800f6a8` — Fonte única de configuração — [Interno]

- `load_dotenv()` passa a existir em um único ponto (`config.py`); o
  default de `IPED_URL` também mora só ali (antes, duplicado no cliente —
  mudar o default exigia mexer em 2 lugares).
- Diretório de exports: constante duplicada em `regex/` e `whatsapp/`
  erradicada; todos leem `config.EXPORTS_DIR`.

## M-18 — `cc7fada` — `iped_whatsapp_reference` gerada da fonte única — [Consumidor][Interno]

- As queries canônicas publicadas pela referência passam a ser **geradas
  pelos builders** de `whatsapp/queries.py` (antes o texto as duplicava à
  mão e divergiu). Pinned por golden `tests/golden/whatsapp_reference.json`.
- Conteúdo não derivável de código (padrões de name, armadilhas) continua
  estático, agora revisado e com as armadilhas conhecidas documentadas.

## M-11 — `b3d4f7d` — Detecção de capacidades do jar — [Consumidor][fork]

- No startup, cada endpoint **online** é sondado (paralelo, tolerante a
  falha — nunca derruba o servidor): paginação (`/search` com `limit=1`),
  `GET /case-summary` e `GET /properties?fields=hash`.
- Resultado reportado como dado em `iped_list_sources`:
  `jar_capabilities.endpoints[] {endpoint, url, search_pagination,
  case_summary, properties_fields}` + `fork_features_enabled`.
- A superfície do fork (limit/offset explícito, `fields=`, filtros) só liga
  com `IPED_MCP_FORK_FEATURES=1` **e** detecção positiva. **Nenhuma tool
  envia parâmetro do fork por default**: o jar padrão ignora parâmetros
  desconhecidos em silêncio e truncaria resultados sem aviso.

## M-19 — `4405009` — Telemetria de desvinculação WhatsApp — [Consumidor]

- Coleta WhatsApp passa a reportar contadores da premissa
  `linkedItems → ufed:SHA256`: `media_links_total` /
  `media_links_unresolved` e `unclassified_messages`.
- Quando a razão de mídia não resolvida ultrapassa 50 %, as tools de
  WhatsApp emitem aviso explícito de desvinculação (a premissa pode ter
  mudado no build do IPED) — dado/aviso, nunca exceção.

## M-7/M-9 follow-ups — `7d98dda` — Sampling compartilhado + lotes em download — [Consumidor][Interno]

- `iped_bookmarks_download` ganhou `offset`/`limit` (mesmo contrato de lote
  do M-9: `batch.has_more`, `remaining_est`; `(0, None)` baixa tudo como
  antes) — permite retomar/limitar downloads longos.
- Interno: helper único `sampling.py` (amostragem estratificada) usado por
  M-7 e família.

## M-8-fix — `fd75b96` — Correção: `export_bookmark_info` quebrado pós-M-8 — [Consumidor]

- `bookmarks_download_info` lia o `TextResult` do M-8 como tupla de 3
  valores (estourava em todo documento). Passa a ler por atributo.
  Corrigido na revisão independente da pilha (fase 8a).

---

## Variáveis de ambiente (estado da branch)

| Env | Padrão | Origem | Uso |
|---|---|---|---|
| `IPED_URL` | `http://localhost:1234` | upstream | Endpoint único da WebAPI |
| `IPED_ENDPOINTS` | — | upstream | Múltiplos endpoints `id=url,...` (exclui `IPED_URL`) |
| `IPED_MCP_WORKERS` | 32 | upstream | Paralelismo de fetch (pool casado) |
| `IPED_MCP_TIMEOUT_DEFAULT` | 30 | M-4 | Operações curtas |
| `IPED_MCP_TIMEOUT_SEARCH` | 30 | M-4 | `/search` |
| `IPED_MCP_TIMEOUT_PROPS` | 90 | M-4 | `/docs/{id}` |
| `IPED_MCP_TIMEOUT_TEXT` | 60 | M-4 | `/text` |
| `IPED_MCP_TIMEOUT_CONNECT_DOWNLOAD` | 10 | M-4 | Download: connect |
| `IPED_MCP_TIMEOUT_READ_DOWNLOAD` | 120 | M-4 | Download: read |
| `IPED_MCP_RETRY_ATTEMPTS` | 3 | M-3 | Tentativas (conexão/timeout) |
| `IPED_MCP_RETRY_BACKOFF_BASE` | 2 | M-3 | Base do backoff (s) |
| `IPED_MCP_RETRY_BACKOFF_CAP` | 8 | M-3 | Teto de 1 espera (s) |
| `IPED_MCP_CB_FAILURE_THRESHOLD` | 4 | M-3 | Falhas p/ abrir o circuito |
| `IPED_MCP_CB_COOLDOWN_SECONDS` | 60 | M-3 | Fail-fast do circuito aberto |
| `IPED_MCP_CB_PROBE_TIMEOUT_SECONDS` | 3 | M-3 | Sonda de reabertura |
| `IPED_MCP_TEXT_CAP` | 10485760 | M-8 | Teto de leitura do `/text` (bytes) |
| `IPED_MCP_EXPORTS_DIR` | `/tmp/iped_mcp/exports` | upstream→M-10/M-17 | Pasta padrão de exports de **todas** as tools |
| `IPED_MCP_HOST` | `127.0.0.1` | M-12 | Bind do servidor MCP |
| `IPED_MCP_PORT` | `8000` | M-12 | Porta do servidor MCP |
| `IPED_MCP_FORK_FEATURES` | `0` | M-11 | Liga a superfície do fork (com detecção) |

## Candidato a upstream vs. específico do fork

- **Candidatos a upstream** (melhorias gerais, sem dependência do jar do
  fork): M-1..M-4, M-6..M-10, M-12..M-14, M-17..M-19. M-12 e M-5 são
  hardening de bind com defaults preservados — seguros fora do contexto do
  fork. M-7 (campos como advertência) e M-8 (404/texto como dado)
  **alinham o servidor ao comportamento real do IPED** documentado na
  investigação (fork responde 404 p/ doc inexistente; `INDEXED_FIELDS`
  desatualizado).
- **Específico do fork**: M-11 — as sondas e o gate `IPED_MCP_FORK_FEATURES`
  existem para distinguir a WebAPI customizada do fork (paginação,
  `case-summary`, `fields=`) da WebAPI padrão; o default desligado mantém o
  servidor idêntico ao upstream. M-5 na prática também beneficia o deploy
  do fork (o jar exposto era risco real), mas seu default loopback é
  benéfico a qualquer uso.
