"""Tools públicas da família triage (registro MCP no padrão do projeto).

Este módulo define, em nível de módulo, as 10 tools públicas da triagem
determinística (DESIGN 09). Contrato preservado: nomes, assinaturas,
docstrings e envelopes de retorno. Cada função tem corpo fino e delega ao
``build_*`` do módulo do eixo (helpers internos estáveis). O registro no
servidor é feito por ``iped_mcp.triage.register`` via ``mcp.add_tool``.
"""

from typing import Any

from fastmcp.exceptions import ToolError

from iped_mcp.triage.communication import build_triage_communication
from iped_mcp.triage.devices_networks import build_triage_devices_networks
from iped_mcp.triage.discovery import build_triage_query_discovery
from iped_mcp.triage.financial import build_triage_financial
from iped_mcp.triage.geolocation import build_triage_geolocation
from iped_mcp.triage.identity import build_triage_identity
from iped_mcp.triage.media import build_triage_media
from iped_mcp.triage.owner import build_owner_candidates
from iped_mcp.triage.telephony import build_triage_telephony
from iped_mcp.triage.transcripts import build_transcripts


def triage_telephony(source_id: str) -> dict[str, Any]:
    """Triagem do eixo telefonia: top contatos por peso de chamadas.

    Varre as categorias canônicas de telefonia (``chamadas telefônicas``,
    ``calls``, ``call logs``, ``contatos``, ``contacts``), agrega chamadas
    e contatos de agenda por número e devolve os 20 mais relevantes com
    score ``min(50 + total_calls, 90)`` e relevância ``ALTA`` (>30
    chamadas), ``MÉDIA`` (>10) ou ``BAIXA`` — heurística do fluxo ICARO,
    sem LLM. A resposta traz somente registros compactos + contadores:
    nenhum id de documento fora de ``sample_doc_ids`` (até 3 por contato)
    e nenhuma propriedade bruta. Contatos de agenda sem chamadas vêm com
    ``total_calls=0``/``score=50``/``BAIXA``.

    O nome de agenda usa a whitelist ordenada T-1.1 (``ufed:Name`` ->
    ``ufed:name`` -> ``userName`` -> ``ufed:userName``; primeiro campo com
    valor vence). Divergência INTENCIONAL do ICARO-telephony (que lê só
    ``ufed:Name``, telephony_strategy.py:117-118, e cobriria 0 nomes neste
    build — veredito R1): a whitelist alinha o eixo à lista do
    ICARO-identity (identity_strategy.py:358-362). O histograma
    ``contact_name_field_hits`` no envelope diz qual campo produziu cada
    nome, e um aviso ``nome_da_agenda_origem=<campo>`` marca a origem
    quando o campo clássico fica em zero.

    Args:
        source_id: source a triar (veja iped_list_sources).

    Returns:
        {source_id, contacts: [{phone, contact_name, total_calls,
        calls_out, calls_in, calls_missed, total_duration_seconds, score,
        relevance, justification, sample_doc_ids}], query_hits (por query;
        -1 = query que falhou), counters (phase/scanned/processed/
        elapsed_s/docs_searched/docs_fetched/call_docs/contact_docs/
        contact_name_field_hits/contacts_total/cached), truncated (tetos
        SEMPRE marcados), warnings}; ``message`` quando o eixo está vazio
        (contacts=[], nunca exceção).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_triage_telephony(source_id)


def transcripts_generate(
    source_id: str, dest_dir: str | None = None
) -> dict[str, Any]:
    """Gera o arquivo de transcrições de áudio embutidas no índice.

    Varre as queries canônicas de áudio (categoria ``Audios``/``Áudios`` e
    o fallback por extensão), mantém os docs com o metadado ASR embutido
    ``audio:transcription``, deduplica por hash e ordena no tempo (com
    data antes dos sem data). Grava o artefato ``Transcricoes.txt`` no
    disco de exports (nome UTC/source, no-clobber — o MCP não toca o caso)
    e devolve SOMENTE metadados + preview: o texto completo nunca vai na
    resposta. Caso sem áudios (ou sem nenhuma transcrição) é o contrato
    zero explícito: nada é gravado e ``message`` nomeia o metadado —
    nunca exceção.

    Args:
        source_id: source a varrer (veja iped_list_sources).
        dest_dir: pasta de destino do artefato (None = pasta de exports do
            servidor, ``IPED_MCP_EXPORTS_DIR``).

    Returns:
        {source_id, file_path, audio_docs, with_transcript,
        without_transcript, duplicates, file_bytes, preview,
        preview_truncated, counters, truncated, warnings}; ``message``
        quando não há transcrição alguma (``file_path=None``, contadores
        zerados).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_transcripts(source_id, dest_dir)


def triage_identity(source_id: str) -> dict[str, Any]:
    """Triagem do eixo identidade: entidade principal + reconhecidos + agenda.

    Varre as 19 categorias canônicas de identidade, replica a classificação
    do eixo ICARO (categoria OU fonte válida; reconhecidos por dupla guarda
    categoria/contentType) e devolve a identidade principal normalizada
    (nome, confiança, e-mails, telefones com os 5 filtros do Regex:PHONE,
    SIM/Wi-Fi/Bluetooth, agenda, device_info padronizado em 16 chaves) com
    score 90/70/50 por confiança e relevância >=85/>=60 — heurística do
    fluxo ICARO, sem LLM. Dispositivos reconhecidos pela conta de nuvem são
    entidades próprias (não alimentam a identidade) com teto 50 sempre
    marcado. A resposta nunca traz propriedades brutas nem listas de ids.

    Cada chamada emite no máximo ``TOOL_BUDGETS["triage_identity"]`` GETs
    internos; o envelope reporta ``http_calls`` (tráfego real) e estouro vira
    ``truncated.http_budget_exceeded`` + warning, nunca exceção. A pré-sonda
    de campos de nome (``counters.name_field_hits``) elimina o 0-silencioso:
    cobertura zero com docs varridos emite aviso nomeando os literais.

    Args:
        source_id: source a triar (veja iped_list_sources).

    Returns:
        {source_id, identity? (nome, confidence, score, relevance,
        justification, emails, phone_numbers, mentioned_phone_numbers,
        aliases, usernames, sim_msisdn, wifi_networks, bluetooth_devices,
        known_contacts_count, known_contacts_sample[:20], device_info{16},
        evidence_source_type, photo_id, source_name, doc_id, path),
        recognized_devices ([:50], rótulos PT), recognized_devices_count,
        query_hits (-1 = query que falhou), counters (op_envelope +
        docs_searched/docs_fetched/classified_docs/recognized_total/
        name_field_hits/http_calls/cached), truncated (tetos SEMPRE
        marcados), warnings}; ``message`` quando o eixo está vazio (nunca
        exceção).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_triage_identity(source_id)


def triage_financial(source_id: str) -> dict[str, Any]:
    """Triagem financeira do eixo "Financeiro" do ICARO (T-4).

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, evidences[:30] + evidence_count, cards,
        accounts[:30] + account_count, regex_availability, query_hits,
        counters, truncated, warnings[, message]}`` — evidências por
        detector Regex (escada MONEY > Boleto > Conta, valor BR parseado,
        ordenadas por valor desc, score 75/55, PIX→ALTA), cartões com
        transações/locais agregados por ``ufed:jumpTargets`` (score
        ``min(60+2×tx, 95)``, ≥85 ALTA; transações truncadas em 5 com
        omissão marcada) e contas/ativos financeiros (score fixo 90 com
        moedas/ativos agregados e conta de nuvem pelo e-mail do path).
        ``regex_availability`` informa o painel de sondas por caso (o
        detector ausente vira warning, nunca exceção — M-7).

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_financial(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_financial falhou: {exc}") from exc


def triage_geolocation(source_id: str) -> dict[str, Any]:
    """Triagem de geolocalização do ICARO (T-5).

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, points[:100] + locations_count,
        coordinates_by_source, query_hits, counters, truncated, warnings[,
        message]}`` — clusters de coordenadas (~11 m, 4 decimais) com
        ``evidence_count`` (docs que contribuíram),
        ``unique_coordinate_count`` (cruas únicas — poluição por
        duplicação), ``source_types`` (EXIF/CHAT/WIFI/CELL_TOWER/
        GEO_METADATA por categoria :165-174), período lexicográfico e score
        ``min(50+5×evidências, 95)`` com relevância ESTREITA (>10 ALTA, >3
        MÉDIA). Bounds ±90/±180 e Null Island (|lat|,|lon| ≤ 1e-4) são
        descartados. SEM país: ``CoordinatesToCountryTool`` é
        ICARO-side/externa (§5.3) — os campos country/state/city não
        existem no shape (ausente ≠ falso).

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_geolocation(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_geolocation falhou: {exc}") from exc


def triage_media(source_id: str) -> dict[str, Any]:
    """Triagem de mídia do ICARO (T-6): itens ranqueados por sinais.

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, ranked_items[:50] + items_total,
        by_category, apps, owner_media_signals[:20], query_hits, counters
        (+field_hits), truncated, warnings[, message]}`` — cada item com
        ``score`` (base 50 mídia / 40 chat + soma dos 12 sinais
        OCR/rosto/nudez/transcrito/CPF/telefone/URL/GPS/ext-divergente/
        carved/deletado/download-financeiro), ``relevance`` por score
        (ALTA >= 85, MÉDIA >= 60, BAIXA), ``signals`` e justificativa
        literal do ICARO. Contagem por categoria crua, apps (marcador
        ``com.whatsapp`` no path + ``ufed:ServiceIdentifier``) e
        contas/titulares vistos em mídia com "N/I" ignorado. O histograma
        ``counters.field_hits`` publica NOMES de campos (teto 40) com a poda
        ``MEDIA_DROP`` do ICARO — valores jamais.

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_media(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_media falhou: {exc}") from exc


def triage_devices_networks(source_id: str) -> dict[str, Any]:
    """Triagem de dispositivos/redes do ICARO (T-7): só agregados.

    Args:
        source_id: id do source no servidor WebAPI do caso.

    Returns:
        Envelope ``{source_id, connections_by_ip[:50] (IPv6 colapsado por
        /64, por hits), services_summary[] (rótulos iCloud),
        device_fingerprints[] (dedup model/hw/build, apps_observed),
        wifi_networks[:100], bluetooth_devices[:100], connections_total,
        logs_parsed/logs_total, query_hits, counters, truncated,
        warnings[, message]}`` — a timeline bruta de conexões do ICARO
        (normalizer :35) NUNCA é publicada; o conceito de truncamento é
        portado sobre a visão agregada. Parse fiel de ``ufed:Body``
        (``Service=… IP=…``) e dos 3 formatos de ``ufed:Application``.

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_devices_networks(source_id)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_devices_networks falhou: {exc}") from exc


def triage_communication(
    source_id: str, search_terms: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Triagem de comunicação do ICARO (T-8): chats agregados + buscas.

    Args:
        source_id: id do source no servidor WebAPI do caso.
        search_terms: termos investigativos opcionais (padrão vazio); com
            termos vale ``min(50+total, 95)``/ALTA, sem termos
            ``min(40+total//2, 70)``/MÉDIA apenas com ``total > 50``.

    Returns:
        Envelope ``{source_id, chats[:30] (interlocutor, is_group,
        message_count, chat_total_messages, terms_hits, score, relevance;
        grupos sempre mantidos, indivíduos no top-20), searches[:50] (query,
        timestamp, source_app, score 40, BAIXA), chats_individuals_total,
        chats_grupos_total[, top_terms] (só com search_terms), query_hits,
        counters (+http_calls), truncated, warnings[, message]}`` — o corpo
        das mensagens NUNCA é publicado; agregação "agregada-v1" (sem mescla
        por chat-id, D-9, declarado em warning).

    Raises:
        ToolError: source inexistente/servidor offline (M-2) ou erro
            inesperado traduzido; estouro de orçamento NUNCA raising — vira
            ``truncated.http_budget_exceeded`` + warning.
    """
    try:
        return build_triage_communication(source_id, search_terms)
    except ValueError:
        raise
    except Exception as exc:  # traduz erro inesperado (padrão da família)
        raise ToolError(f"triage_communication falhou: {exc}") from exc


def owner_candidates(source_id: str) -> dict[str, Any]:
    """Candidatos a proprietário do dispositivo: motor heurístico (T-9).

    Roda o motor do ``owner_inference_engine.py`` do ICARO SEM LLM sobre as
    entidades dos 4 eixos já computados na mesma chamada (identidade,
    financeiro, mídia, comunicação — import direto dos módulos internos;
    nenhuma query nova além das dos eixos-membro). Devolve até 5 candidatos
    ranqueados com score (pesos/caps literais por feed: conta principal 70,
    path 60, contato-com-próprio-número 55, cartão 20, pagador 18,
    e-mail/favorecido 15, Wi-Fi pessoal 12, recebedor 12, mídia 10, telefonia
    8, interlocutor 5, SIM 5), confiança ALTA/MÉDIA/BAIXA (bordas 60/35),
    fontes, evidência textual, fusão de variações por chave compacta
    ("Titular Teste" ≡ "TitularTeste"), vetos (ponto, técnico, institucional, ID
    de transação, termo não-pessoa) e a penalidade ×0.3 para vencedor vindo
    só de fontes financeiras com score acima de 100. O custo é a união dos
    ``http_calls`` dos membros (teto composto ``OWNER_BUDGET``; estouro vira
    ``truncated`` + warning com phase "partial", nunca exceção); eixo-membro
    que falha sai com ``per_axis_available=false`` + warning sem derrubar a
    ferramenta; sem candidato, ``message`` explícita com lista vazia.

    Args:
        source_id: source a triar (veja iped_list_sources).

    Returns:
        {source_id, candidates[:5] (name, score, confidence, sources[:3],
        evidence), total_candidates, financial_only_penalty_applied
        {applied, name}, per_axis_available{identity, financial, media,
        communication}, counters (op_envelope + member_http_calls +
        http_calls + http_budget), truncated (composto + por membro + teto
        de candidatos), warnings}; ``message`` quando vazio.

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_owner_candidates(source_id)


def triage_query_discovery(source_id: str) -> dict[str, Any]:
    """Descobre o QUE EXISTE num source: catálogo por evidência, com cache.

    Roda 33 probes de frase LUCENE (lista canônica do ICARO em
    ``knowledge.DISCOVERY_PROBES`` — frases soltas, nunca regex do usuário),
    amostra até ``DISCOVERY_SAMPLE_SIZE`` docs de cada probe com hits e
    devolve o histograma de NOMES de campos com cobertura (docs que o têm e
    até 3 probes que o veem, ``value_type`` str/list/mixed), buckets de
    mime/extensão da amostra e a disponibilidade dos 9 detectores
    ``Regex:<N>`` canônicos do servidor (sonda = KNOWN_TYPES; o
    RegexConfig.txt completo é externo ao MCP — warning nominal documenta).
    Pin de privacidade: nenhum VALOR de conteúdo atravessa — só nomes e
    contagens. Falha de probe (500/timeout) marca ``available: false`` +
    ``error`` e segue (fiel ao ICARO :338-353, nunca aborta).

    O catálogo fica em cache por source (TTL ``DISCOVERY_CACHE_TTL``): a
    2ª chamada na janela serve ``cache.served_from_cache=true`` com
    ``counters.http_calls=0`` e warning ``catalogo_servido_de_cache``
    (R-TRIAGE-1: cache não conta GET). Builds truncados por orçamento
    nunca são cacheados. Probes de frase são queries de conteúdo caras —
    elapsed > ``DISCOVERY_SLOW_WARNING_S`` emite warning sugerindo o reuso
    do cache (o agente decide re-chamar só pelo bloco ``fields``).

    Args:
        source_id: source a descobrir (veja iped_list_sources).

    Returns:
        {source_id, probes {termo: {total_hits, available, error?}}, fields
        {campo: {docs_na_amostra, probes_que_o_veem[:3], value_type}},
        regex_availability {N: hits} (-1 = query que falhou), mime_types
        {mime: contagem}, extensions {ext: contagem}, sample_size,
        cache {served_from_cache, ttl_s, age_s?}, counters (op_envelope +
        probes_ran/docs_sampled/http_calls/cached), truncated
        (http_budget_exceeded), warnings}; ``message`` quando o catálogo
        está vazio (nunca exceção).

    Raises:
        ValueError: source_id vazio, inexistente ou servidor indisponível
            (textos estáveis da política M-2).
    """
    return build_triage_query_discovery(source_id)
