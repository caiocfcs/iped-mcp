# iped_mcp

MCP server (FastMCP) para interagir com dados telemáticos indexados no IPED.

- Endpoints IPED: variáveis de ambiente `IPED_ENDPOINTS` (múltiplos) ou
  `IPED_URL` (único, padrão `http://localhost:1234`)
- Transporte: HTTP (streamable) em `http://127.0.0.1:8000/mcp` (bind
  configurável por `IPED_MCP_HOST`/`IPED_MCP_PORT`; padrão loopback — expor
  é opt-in explícito)

## Multi-endpoint

O MCP aceita **N endpoints IPED** e age como proxy/roteador transparente: para
o agente continua parecendo um único IPED com um conjunto unificado de
sources (o endpoint nunca é exposto; erros são centrados em source).

```
# múltiplos endpoints (ids únicos):
IPED_ENDPOINTS=ep1=http://host:1234,ep2=http://host:1235

# ou um único (id `default`):
IPED_URL=http://host:1234
```

- `IPED_ENDPOINTS` e `IPED_URL` juntas → erro explícito no startup.
- No startup o MCP sonda `GET /sources/` em paralelo em cada endpoint
  (timeout ~5 s). **Source id duplicado entre endpoints → o MCP não sobe**
  (source id é globalmente único).
- Endpoint offline no startup → aviso no log; o MCP sobe e os sources dele
  simplesmente não aparecem. Todos offline → aviso em destaque; o MCP sobe.
- Toda busca exige `source_id` → 1 request (roteada por registry em memória,
  custo zero extra).
- **Bookmarks são por servidor** (restrição da API do IPED): toda tool
  `bookmarks_*` recebe `source_id` obrigatório e age apenas no servidor
  correspondente. Docs de outro servidor → erro.

## Tools

Nomes como o agente os vê no opencode (prefixo `iped_` = nome do servidor;
no código, as funções do domínio core não levam o prefixo).

| Tool | Descrição |
|---|---|
| `iped_list_sources` | Lista os sources registrados no IPED (`GET /sources/`) + servers offline (`offline_endpoints`) e capacidades do jar (`jar_capabilities`) |
| `iped_case_summary` | Censo do caso: contagem de documentos por categoria (taxonomia real do servidor, sem doc IDs) |
| `iped_search` | Busca Lucene livre: contagem total + amostra de doc IDs |
| `iped_doc_info` | Propriedades de um documento (conjunto curado, `fields` ou `all`) |
| `iped_doc_text` | Texto extraído de um documento (truncado, paginado; doc sem texto responde `status="no_text"` como dado, não erro) |
| `iped_doc_export` | Baixa o arquivo bruto de um documento para o disco |
| `iped_thumb_export` | Baixa a thumbnail de um documento para o disco |
| `iped_ufed_search` | Busca documentos por atributo `ufed:` (escape `ufed\:` tratado internamente); retorna `id`, `name` e o campo buscado (+ `ufed:EntryValue` quando `attribute` é `EntryName`) |
| `iped_ufed_device_info` | Device Information do source: tabela nome/valor (IMEI, ICCID, IMSI, MSISDN, Serial, modelo, SO, Apple ID, operadora, etc.) |
| `iped_ufed_list_fields` | Referência dos campos `ufed:`: indexados (buscáveis) e não indexados (retornam HTTP 500) |
| `iped_get_document` | Retorna as `properties` completas de um documento |
| `iped_regex_list_types` | Tipos `Regex:*` disponíveis no source (CNPJ, CPF, URL, etc.) |
| `iped_regex_stats` | Matches e valores distintos de um tipo `Regex:*` |
| `iped_regex_top_values` | Top valores mais frequentes de um tipo `Regex:*` |
| `iped_regex_export_csv` | Exporta a tabela de frequência de um tipo `Regex:*` como CSV |
| `iped_regex_value_docs` | Docs onde um valor específico de um tipo `Regex:*` aparece |
| `iped_bookmarks_list` | Lista os nomes dos bookmarks |
| `iped_bookmarks_docs` | Docs de um bookmark (IDs por source, paginado) |
| `iped_bookmarks_create` | Cria um bookmark vazio |
| `iped_bookmarks_add` | Adiciona documentos a um bookmark |
| `iped_bookmarks_remove` | Remove documentos de um bookmark |
| `iped_bookmarks_rename` | Renomeia um bookmark |
| `iped_bookmarks_delete` | Exclui um bookmark (não os documentos indexados) |
| `iped_bookmarks_save_search` | Executa uma busca Lucene e salva os resultados num bookmark |
| `iped_bookmarks_report` | Amostra de documentos de um bookmark com propriedades-chave |
| `iped_bookmarks_download` | Baixa os arquivos de um bookmark para uma pasta local (lote opcional via `offset`/`limit`, com `batch.has_more` e `remaining_est`) |
| `iped_bookmarks_download_info` | Exporta todas as informações textuais estruturadas de um bookmark para um arquivo JSON |
| `iped_bookmarks_create_from_file` | Cria um bookmark a partir de um txt (1 doc ID ou MD5 por linha) |
| `iped_bookmarks_add_from_file` | Adiciona a um bookmark os docs de um txt |
| `iped_bookmarks_remove_from_file` | Remove de um bookmark os docs de um txt |
| `iped_whatsapp_overview` | Triagem do WhatsApp no source: totais, período, série mensal, conversas distintas e top interlocutores |
| `iped_whatsapp_account` | Conta do dono do WhatsApp: JID, número, nome de exibição e status |
| `iped_whatsapp_profile_photo` | Baixa a foto de perfil cacheada de um número (dono ou interlocutor) |
| `iped_whatsapp_conversation` | Analisa uma conversa privada: volume total, período, intensidade e divisão enviado/recebido |
| `iped_whatsapp_conversation_window` | Analisa uma conversa privada numa janela de tempo, segmentada por tipo (texto, áudio, imagem, vídeo, PDF) |
| `iped_whatsapp_export_conversation` | Exporta as mensagens de uma conversa privada para um JSON local (texto, transcrição de áudio, texto de PDF) |
| `iped_whatsapp_text_search` | Busca um termo em todas as conversas privadas (corpo da mensagem, transcrição de áudio e texto de PDF) |
| `iped_whatsapp_reference` | Referência validada de campos, padrões de name, marcadores de tipo e queries canônicas do WhatsApp |

**Exports em disco**: todas as tools que gravam arquivos usam nome único
`<UTC>_<source>_<nome>[-N]<ext>` e **nunca sobrescrevem** export anterior
(`overwrite=True` reabre o mesmo arquivo). Pasta padrão:
`$IPED_MCP_EXPORTS_DIR` (`/tmp/iped_mcp/exports`).

Campos `ufed:` indexados: `EntryName`, `EntryValue`, `EntryCategory`, `Source`,
`extractionName`, `Name`, `URL`, `ChangeTime`, `decoding_confidence`, `isrelated`,
`fs`, `MD5`, `SHA256`. Não indexados: `local_path`, `labels`.

## Estrutura

- `main.py` — servidor MCP (cria o FastMCP, registra as tools, roda)
- `src/iped_mcp/client.py` — `IPEDClient` (API REST do IPED, 1 endpoint)
- `src/iped_mcp/router.py` — `Router` (proxy multi-endpoint: sonda, registry
  source→endpoint, roteamento, bookmarks por servidor)
- `src/iped_mcp/core/` — tools de acesso genérico (`tool.py`)
- `src/iped_mcp/ufed/` — tools de atributos `ufed:` (`tool.py`, `queries.py`)
- `src/iped_mcp/regex/` — tools de atributos `Regex:*` (`tool.py`, `queries.py`, `scan.py`)
- `src/iped_mcp/bookmarks/` — tools de bookmarks (`tool.py`, `download.py`, `idsfile.py`)
- `src/iped_mcp/whatsapp/` — tools de WhatsApp (`tool.py`, `queries.py`, `collect.py`)
- `src/iped_mcp/config.py` — configuração por env (timeouts, retry, exports, bind) — fonte única
- `src/iped_mcp/errors.py` — taxonomia de erros de comunicação (`IpedError` e subclasses)
- `src/iped_mcp/resilience.py` — retry/backoff e circuit breaker do cliente
- `src/iped_mcp/validation.py` — validação uniforme de entradas das tools
- `src/iped_mcp/progress.py` — envelope de progresso/loteamento das operações longas
- `tests/` — suíte offline do contrato WebAPI + smoke das tools (roda sem IPED;
  smoke runtime opcional com `IPED_URL`; ver Testes)

## Setup

```bash
uv sync
```

## Uso

Subir o servidor manualmente (HTTP):

```bash
uv run main.py
```

O servidor fica disponível em `http://127.0.0.1:8000/mcp`.

## Testes

A suíte roda **sem** IPED ligado (fake da WebAPI + pinos de contrato):

```bash
uv run pytest          # 330+ testes offline, ~90 s
```

Opcional — smoke `GET`-only contra uma WebAPI real (só habilita com a
variável definida; sem ela os 2 testes marcam `skip`):

```bash
IPED_URL=http://localhost:1234 uv run pytest -m runtime
```

## Configuração no opencode

Adicione ao `opencode.json` (raiz do projeto ou `~/.config/opencode/opencode.json`),
apontando para a porta do servidor:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "iped": {
      "type": "remote",
      "url": "http://localhost:8000/mcp",
      "enabled": true
    }
  }
}
```

Suba o servidor com `uv run main.py` antes de usar o opencode.
As tools acima ficarão disponíveis para o agente.

## Launcher da IPED Web API

O launcher (`web-api-launcher.py`) sobe uma instância da IPED Web API para cada
source de um `multicases.json`, alocando uma porta livre a partir de `1234`
(`--start-port`) e encerrando todos os servidores no Ctrl+C. É **recomendado no
Windows** (gerencia os processos Java via Job Object). Cada WebAPI escuta em
`127.0.0.1` por padrão — expor a rede exige `--host` explícito e emite aviso
(a WebAPI não tem autenticação).

```bash
python web-api-launcher.py --root "C:\caminho\para\iped" --sources "C:\caminho\multicases.json"
```

Sem argumentos, usa `./` como root e `./multicases.json`. Detalhes em
`WEB-API-LAUNCHER.md`.

## Ambiente

É recomendável executar o MCP **no mesmo ambiente do harness** (opencode),
preferencialmente WSL2 — foi testado com o opencode no mesmo ambiente WSL2.

---

## Fork `urlan/patches`

Seção do fork mantido para integração com o ICARO (WebAPI do IPED
customizada). Lista completa, commit a commit, em `CHANGELOG.md`. O texto
acima permanece o do upstream; nada foi removido.

### O que muda para o consumidor

- **Tool nova**: `iped_case_summary` — censo de documentos por categoria do
  caso (taxonomia real via `GET /categories`), apenas contagens, cache ~5 min.
- **Lotes cliente-side**: `iped_bookmarks_download`, `iped_regex_stats`,
  `iped_regex_top_values` e `iped_regex_export_csv` aceitam `offset`/`limit`
  com `batch.has_more`/`remaining_est` (a WebAPI não pagina; o corte é local).
- **Texto como dado**: `iped_doc_text` nunca explode por doc sem texto —
  responde `status="no_text"`; truncamento pelo teto de leitura vem com
  `capped=true` (teto configurável, ver abaixo).
- **Erros diagnósticos**: falhas de rede/HTTP viram a taxonomia `IpedError`
  com mensagens estáveis (`Source '<id>' indisponível`, `Doc não encontrado`
  etc.); o texto cru do `requests` nunca chega ao agente. Doc inexistente
  (HTTP 404 no jar real) recebe diagnóstico assistido, não traceback.
- **Resiliência**: retry ×3 (só conexão/timeout, backoff 1-2-4 s) e circuit
  breaker por endpoint (4 falhas abrem, sonda `/sources/` reabre) — tunáveis
  por env.
- **Timeouts por classe** substituem o timeout único de 10 s (busca 30 s,
  propriedades 90 s, texto 60 s, download 10 s connect / 120 s read).
- **Exports imutáveis**: nomes `<UTC>_<source>_<nome>[-N]<ext>`, sem
  sobrescrita (ver Exports em disco acima).
- **Campos `ufed:` são advertência, não bloqueio**: campo fora da lista
  conhecida não é mais rejeitado client-side; `iped_ufed_list_fields` pode
  amostrar documentos reais (`sample_size`) e reportar os campos observados.
- **Capabilities do jar** (fork): no startup o servidor sonda paginação,
  `case-summary` e `properties?fields=` em cada endpoint online e publica o
  resultado em `iped_list_sources → jar_capabilities`. Parâmetros do fork
  **nunca** são enviados por padrão; a superfície liga apenas com
  `IPED_MCP_FORK_FEATURES=1` e detecção positiva.

### Variáveis de ambiente novas

| Env | Padrão | Para quê |
|---|---|---|
| `IPED_MCP_HOST` / `IPED_MCP_PORT` | `127.0.0.1` / `8000` | Bind do servidor MCP (exposição é opt-in) |
| `IPED_MCP_TIMEOUT_DEFAULT` / `_SEARCH` / `_PROPS` / `_TEXT` | 30 / 30 / 90 / 60 s | Timeouts por classe |
| `IPED_MCP_TIMEOUT_CONNECT_DOWNLOAD` / `_READ_DOWNLOAD` | 10 / 120 s | Par connect/read de download |
| `IPED_MCP_TEXT_CAP` | 10485760 | Teto de leitura do `/text` (bytes) |
| `IPED_MCP_RETRY_ATTEMPTS` / `_RETRY_BACKOFF_BASE` / `_RETRY_BACKOFF_CAP` | 3 / 2 / 8 | Retry do cliente |
| `IPED_MCP_CB_FAILURE_THRESHOLD` / `_CB_COOLDOWN_SECONDS` / `_CB_PROBE_TIMEOUT_SECONDS` | 4 / 60 / 3 | Circuit breaker |
| `IPED_MCP_EXPORTS_DIR` | `/tmp/iped_mcp/exports` | Pasta padrão de exports de todas as tools |
| `IPED_MCP_FORK_FEATURES` | desligado (`0`) | Liga parâmetros da WebAPI do fork quando detectados |

`.env.exemplo` documenta as principais; `config.py` é a fonte única e
valida todas no startup (valor inválido = falha explícita na subida).

### Upstream-ável vs. específico do fork

- Praticamente todo o lote é melhoria geral do servidor (testes públicos,
  erros, resiliência, timeouts, exports, lotes, censo, validação) e é
  candidato a upstream.
- Específico do fork: detecção de capacidades + gate `IPED_MCP_FORK_FEATURES`
  (existem por causa da WebAPI customizada; desligados, o comportamento é o
  do upstream) e o bind configurável do launcher/MCP (motivação: deploy em
  container; defaults preservados).
