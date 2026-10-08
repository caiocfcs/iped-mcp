# iped_mcp

MCP server (FastMCP) para interagir com dados telemáticos indexados no IPED.

- Endpoints IPED: variáveis de ambiente `IPED_ENDPOINTS` (múltiplos) ou
  `IPED_URL` (único, padrão `http://localhost:1234`)
- Transporte: HTTP (streamable) em `http://127.0.0.1:8000/mcp`

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
| `iped_list_sources` | Lista os sources registrados no IPED (`GET /sources/`) |
| `iped_search` | Busca Lucene livre: contagem total + amostra de doc IDs |
| `iped_doc_info` | Propriedades de um documento (conjunto curado, `fields` ou `all`) |
| `iped_doc_text` | Texto extraído de um documento (truncado, paginado) |
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
| `iped_bookmarks_download` | Baixa todos os arquivos de um bookmark para uma pasta local |
| `iped_bookmarks_create_from_file` | Cria um bookmark a partir de um txt (1 doc ID ou MD5 por linha) |
| `iped_bookmarks_add_from_file` | Adiciona a um bookmark os docs de um txt |
| `iped_bookmarks_remove_from_file` | Remove de um bookmark os docs de um txt |

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
- `tests/` — testes unitários e de integração (integração pula se o IPED estiver fora)

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

```bash
uv run pytest
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
source de um `multicases.json`, alocando uma porta livre a partir de `1234` e
encerrando todos os servidores no Ctrl+C. É **recomendado no Windows** (gerencia
os processos Java via Job Object).

```bash
python web-api-launcher.py --root "C:\caminho\para\iped" --sources "C:\caminho\multicases.json"
```

Sem argumentos, usa `./` como root e `./multicases.json`. Detalhes em
`WEB-API-LAUNCHER.md`.

## Ambiente

É recomendável executar o MCP **no mesmo ambiente do harness** (opencode),
preferencialmente WSL2 — foi testado com o opencode no mesmo ambiente WSL2.
