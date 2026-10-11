"""Conhecimento pericial da família triage: constantes canônicas do ICARO.

Este módulo é a razão de existir da superfície de triagem (DESIGN 09 §2.2/
§3.2): entrega ao agente LLM o conhecimento de eixo que o fluxo ICARO
codificou — categorias, campos de propriedades, limiares e tetes — sem
nenhuma lógica de execução. Cada constante cita o arquivo:linha de origem
em ``./iped-icaro`` (portado conforme o NR-2 aditado; nenhum valor aqui é
inventado).
"""

from __future__ import annotations

# --- T-1 telefonia: categorias e queries -----------------------------------

# Categorias que definem o eixo telefonia.
# Origem: iped-icaro/strategies/telephony_strategy.py:25-27.
TELEPHONY_CATEGORIES = (
    "chamadas telefônicas",
    "calls",
    "call logs",
    "contatos",
    "contacts",
)

# Forma da query de categoria usada pelo motor do ICARO (matching exato do
# Lucene). Origem: iped-icaro/strategies/base_strategy.py:177.
TELEPHONY_QUERY_TEMPLATE = 'category:"{category}"'

# Pré-ranking por tipo de item no ICARO (não compõe a saída do T-1; fica
# registrado como conhecimento do eixo).
# Origem: iped-icaro/strategies/telephony_strategy.py:33-36.
SCORE_CALL = 70  # categoria contém "call"
SCORE_CONTACT = 50  # demais categorias válidas

# Ramificação chamada x contato pela substring da categoria.
# Origem: iped-icaro/strategies/telephony_strategy.py:34 ("call"),
# :58 ("chamadas"), :115 ("contato"/"contacts").
CALL_BRANCH_MARKERS = ("call", "chamadas")
CONTACT_BRANCH_MARKERS = ("contato", "contacts")

# --- T-1 telefonia: campos de propriedades ----------------------------------

# Chaves de propriedades no envelope do jar (valores sempre lista; doc no
# topo em GET /docs/{id}). Origem: iped-mcp src/iped_mcp/core/tool.py:46-52
# (KEY_FIELDS: name/path/category) e tests/conftest.py:61-79 (make_doc).
CATEGORY_FIELD = "category"
NAME_FIELD = "name"
PATH_FIELD = "path"

# Chamada: destinatário com fallback de remetente.
# Origem: iped-icaro/strategies/telephony_strategy.py:59.
CALL_TO_FIELDS = ("Communication:To", "Communication:From")
# Fallback de número sem o campo To. Origem: telephony_strategy.py:77-78.
PHONE_FALLBACK_FIELDS = ("ufed:PhoneNumber", "Regex:PHONE")
DIRECTION_FIELD = "ufed:Direction"  # origem: telephony_strategy.py:92
DURATION_FIELD = "ufed:Duration"  # origem: telephony_strategy.py:100
# Contato de agenda. Origem: telephony_strategy.py:117-118.
CONTACT_NAME_FIELDS = ("ufed:Name",)
CONTACT_PHONE_FIELDS = ("ufed:PhoneNumber", "phoneNumber")

# --- T-1 telefonia: extração -------------------------------------------------

# "Nome (+55...)" do campo To. Origem: telephony_strategy.py:65.
HEADTO_RE = r"(.*?)\s*\((\+?\d+)"
# Fallback só-dígitos do To exige mínimo de dígitos. Origem: :72-73.
MIN_CALL_PHONE_DIGITS = 8
# Recusa do registro por telefone curto. Origem: :83-84.
MIN_PHONE_LEN = 3
# Contato de agenda só vale com mínimo de dígitos. Origem: :122.
MIN_CONTACT_PHONE_DIGITS = 8
# Nome padrão sem par no To. Origem: telephony_strategy.py:88.
DEFAULT_CONTACT_NAME = "DESCONHECIDO"
# Aspas remanescentes do valor de telefone. Origem: telephony_strategy.py:79.
QUOTE_STRIP_CHARS = '"'
# Baldes de direção por substring, na ordem de teste do ICARO (:95-97);
# valores = atributos do agregado interno.
DIRECTION_BUCKETS = {
    "outgoing": "calls_out",
    "incoming": "calls_in",
    "missed": "calls_missed",
}

# --- T-1 telefonia: pontuação e ordenação ------------------------------------

# score = min(50 + total_calls, 90).
# Origem: iped-icaro/draft_report/draft_report_iped_manager.py:914.
RELEVANCE_BASE = 50
RELEVANCE_CAP = 90
# Limiares ESTRITOS (> 30 = ALTA, > 10 = MÉDIA, senão BAIXA).
# Origem: draft_report_iped_manager.py:915.
RELEVANCE_ALTA_MIN_CALLS = 30
RELEVANCE_MEDIA_MIN_CALLS = 10
RELEVANCE_ALTA = "ALTA"
RELEVANCE_MEDIA = "MÉDIA"
RELEVANCE_BAIXA = "BAIXA"
# Ordem da lista final (relevância asc, depois -score).
# Origem: draft_report_iped_manager.py:483-484.
RELEVANCE_ORDER = {RELEVANCE_ALTA: 0, RELEVANCE_MEDIA: 1, RELEVANCE_BAIXA: 2}
# Justificação literal do ICARO (texto preservado para correlação com
# relatórios antigos). Origem: draft_report_iped_manager.py:922-925.
JUSTIFICATION_TEMPLATE = "{calls} chamadas (E:{out}/R:{inn}/P:{missed})"

# --- T-1 telefonia: tetos de varredura/entrega -------------------------------

# ids por query category: (max_candidates_per_category_query).
# Origem: iped-icaro/strategies/base_strategy.py:59.
CATEGORY_QUERY_CAP = 2500
# ids únicos varridos no total (max_total_candidates_per_strategy).
# Origem: base_strategy.py:60.
TOTAL_CANDIDATE_CAP = 5000
# Contatos devolvidos (records[:20]).
# Origem: iped-icaro/strategies/telephony_strategy.py:135.
TOP_CONTACTS = 20
# ids de exemplo por contato (adição deliberada do MCP, DESIGN 09 §2.4 —
# alvos de iped_doc_info para o agente).
SAMPLE_DOC_IDS_MAX = 3
# Caso sem telefonia: triagem vazia explícita, não exceção (DESIGN 09 §2.6;
# espelha base_strategy.py:71-73 — StrategyResult vazio).
TELEPHONY_EMPTY_MESSAGE = (
    "nenhum documento das categorias de telefonia neste source"
)
