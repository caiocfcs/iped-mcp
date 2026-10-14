"""Validação única de parâmetros numéricos e contagens entre tools (M-14).

Antes deste módulo cada família de tools mantinha seu próprio teto, com
valores inconsistentes e literais espalhados (2b/D-08): `limit` 1..500 só
em core/ufed, `regex_value_docs` e `top` sem teto nenhum, tetos válidos só
no JSON de add/remove de bookmarks enquanto os `*_from_file` enviavam
payload ilimitado à WebAPI. Aqui vivem TODOS os tetos nomeados e os três
helpers que as tools usam para rejeitar entrada fora de faixa ANTES de
qualquer chamada de rede — com mensagem clara que nomeia o parâmetro
(requisito de UX do M-14; a rejeição antecipada em payloads que antes
"passavam" é comportamento desejável e documentado no relatório da fase).

Regra de ouro (NR-9 da proposta): as tools mantêm suas assinaturas; só a
VALIDAÇÃO é compartilhada.
"""

from __future__ import annotations

# --- tetos nomeados (única fonte; valores herdados dos contratos vigentes) --

# "limit" de docs/mensagens por chamada: amostra do iped_search,
# ufed_search, página de bookmarks_docs, JSON de bookmarks_add/remove,
# drill-down regex_value_docs e export whatsapp (todos 1 GET/doc ou
# payload JSON proporcional). 500 é o teto histórico do core/ufed.
DOCS_LIMIT_MAX = 500

# bookmarks_save_search: salvar referência de busca é 1 POST com a lista de
# ids (sem fetch por doc), por isso o teto histórico maior (1000) se mantém.
SAVE_SEARCH_LIMIT_MAX = 1000

# bookmarks_report_page: página do relatório com várias propriedades/doc.
REPORT_LIMIT_MAX = 50

# regex_top_values: cada valor devolvido traz amostras de até 3 docs; o teto
# protege a janela de contexto do agente (antes NÃO havia teto algum).
TOP_VALUES_MAX = 1000

# regex_list_types: amostra de docs para descobrir tipos desconhecidos
# (1 GET/doc). Valor histórico da tool (2000).
REGEX_SAMPLE_SIZE_MAX = 2000

# ufed_list_fields: amostra da descoberta leve de campos (M-7) — match-all
# cara + fetch por doc, por isso o teto menor e intencional (200).
FIELD_SAMPLE_SIZE_MAX = 200

# iped_doc_text: teto de caracteres por chamada (histórico da tool).
MAX_CHARS_MAX = 100_000

# bookmarks *_from_file: payload aceito pela WebAPI numa mutação de bookmark.
# O caminho JSON já era limitado a DOCS_LIMIT_MAX; o fluxo de arquivo é o
# destino documentado para lotes maiores, mas até M-14 enviava payload
# ILIMITADO (D-08). 10 000 linhas mantém o POST na casa de centenas de KB,
# muito abaixo dos limites padrão do Grizzly do jar-alvo.
FILE_IDS_MAX = 10_000


def _reject_non_int(value: object, name: str) -> None:
    """Rejeita tipos que não são inteiro puro antes da comparação de faixa.

    Args:
        value: valor recebido do chamador.
        name: nome do parâmetro (entra na mensagem).

    Raises:
        ValueError: se `value` não é `int` (ou é `bool`, que não é contagem
            legítima em nenhum parâmetro destas tools).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{name}' deve ser um inteiro (recebido: {value!r}).")


def check_int_range(value: int, name: str, minimum: int, maximum: int) -> int:
    """Valida um inteiro dentro de [minimum, maximum]; retorna o valor.

    Args:
        value: valor a validar.
        name: nome do parâmetro (a mensagem de erro OBRIGATORIAMENTE o
            nomeia — requisito M-14).
        minimum: limite inferior inclusivo.
        maximum: limite superior inclusivo.

    Returns:
        O próprio `value` (permite uso em expressão de atribuição).

    Raises:
        ValueError: tipo não-inteiro, ou valor fora da faixa.
    """
    _reject_non_int(value, name)
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} deve estar entre {minimum} e {maximum} (recebido: {value})."
        )
    return value


def check_min(value: int, name: str, minimum: int) -> int:
    """Valida um inteiro `>= minimum`; retorna o valor.

    Args:
        value: valor a validar.
        name: nome do parâmetro (nomeado na mensagem de erro).
        minimum: limite inferior inclusivo.

    Returns:
        O próprio `value`.

    Raises:
        ValueError: tipo não-inteiro, ou valor abaixo do mínimo.
    """
    _reject_non_int(value, name)
    if value < minimum:
        raise ValueError(f"{name} deve ser >= {minimum} (recebido: {value}).")
    return value


def check_count(count: int, name: str, maximum: int, hint: str = "") -> None:
    """Valida uma contagem contra o teto por chamada.

    Args:
        count: quantidade de itens (docs, ids, linhas de arquivo).
        name: rótulo dos itens (ex.: "docs", "ids/md5 no arquivo"); entra
            na mensagem para o agente identificar o parâmetro.
        maximum: teto aceito por chamada.
        hint: frase opcional sugerindo o caminho alternativo (ex.: usar o
            fluxo `*_from_file` para lotes maiores).

    Raises:
        ValueError: `count` acima de `maximum`.
    """
    if count > maximum:
        message = f"{count} {name} excede o teto de {maximum} por chamada."
        if hint:
            message = f"{message} {hint}"
        raise ValueError(message)
