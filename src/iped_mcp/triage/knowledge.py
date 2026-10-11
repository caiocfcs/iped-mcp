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
# Contato de agenda: whitelist ORDENADA de campos de nome (T-1.1,
# DESIGN 09-B §1.2). O literal original do ICARO-telephony era somente
# ``ufed:Name`` (iped-icaro/strategies/telephony_strategy.py:117-118), que
# vale 0 nomes neste build — FACT de runtime: ``ufed:Name`` = 0 em amostra
# de 400 docs "contacts" (research/reports/phase-9-pilot-runtime.md §7;
# veredito R1). O alargamento é decisão do operador (veredito R1) e alinha
# o eixo à lista ordenada do ICARO-identity
# (strategies/identity_strategy.py:358-362; o ramo WhatsApp usa userName
# em :319). Divergência INTENCIONAL do ICARO-telephony, que leria 0 nomes
# neste build. ``ufed:userName`` nunca foi observado em runtime
# (TO_VERIFY-B1): entra como último candidato de sonda — custo zero por
# doc em ``fields.first_str``, nunca removido.
CONTACT_NAME_FIELDS = (
    "ufed:Name",      # identidade/contatos clássicos — telephony:117, identity:359
    "ufed:name",      # variante minúscula — identity_strategy.py:360
    "userName",       # agenda deste build — identity:361 + runtime §7
    "ufed:userName",  # variante a confirmar em runtime (TO_VERIFY-B1)
)
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

# --- T-2 transcripts: queries e campos ----------------------------------------

# Queries candidatas de áudio, na ordem: a primeira que produzir registros
# encerra a varredura (fallback só quando a anterior não produziu nada).
# Origem: iped-icaro/transcripts/transcript_generator_service.py:37-41
# (categoria [Audios] validada no caso real do ICARO; service:35-36).
AUDIO_QUERIES = (
    'category:"Audios"',
    'category:"Áudios"',
    "ext:opus OR ext:amr OR ext:m4a OR ext:mp3 OR ext:wav",
)

# Metadado ASR embutido no índice. Origem: service:146.
TRANSCRIPT_FIELD = "audio:transcription"
# Chave do dedup (minúsculo; fallback nome). Origem: service:150-154.
AUDIO_HASH_FIELDS = ("hash", "md5")
# Campos de data de criação, na ordem de fallback. Origem: service:202.
AUDIO_CREATED_FIELDS = ("criacao", "criação", "created")
# Data/hora explícita "dd/mm/aaaa HH:MM:SS". Origem: service:205-208.
CREATED_DTL_RE = r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})"
CREATED_DT_FORMAT = "%d/%m/%Y %H:%M:%S"
# Dígitos puros com este tamanho ou mais = epoch em milissegundos.
# Origem: service:213.
EPOCH_MS_MIN_LEN = 13
# Posições da tupla de ordenação: docs COM data vêm antes dos sem data.
# Origem: service:200-215 (sort :99).
WITH_DATE_SORT_RANK = 0
NO_DATE_SORT_RANK = 1
# Guarda de varredura = PAGE_SIZE × MAX_PAGES (500 × 200).
# Origem: service:32-33.
AUDIO_SCAN_MAX_DOCS = 100_000
# Nome do artefato (análogo a "Transcrições.txt", service:31 — sem cedilha
# para não depender do mapa do safe_name do M-10).
TRANSCRIPT_FILE_BASENAME = "Transcricoes.txt"
# Formato literal do bloco por áudio e o join entre blocos.
# Origem: service:217-228.
BLOCK_TEMPLATE = "Nome: {name}\nCaminho: {path}\nHASH: {hash}\n\n{text}\n"
BLOCK_JOIN = "\n"
# Caminho do índice usa ">>" como separador; o artefato usa "/".
# Origem: service:157.
PATH_TOKEN = ">>"
PATH_SEPARATOR = "/"
# Texto colapsado em 1 linha (evita colidir com o parser de blocos).
# Origem: service:195-198.
WS_COLLAPSE_RE = r"\s+"
# --- T-2 transcripts: contrato de contexto ------------------------------------

# Teto de texto na resposta MCP (DESIGN 09 §3.2): o texto completo NUNCA
# viaja na resposta — só o preview; o arquivo é o artefato (M-10).
PREVIEW_CHARS = 2000
# Sufixo que marca o corte do preview (truncamento SEMPRE marcado, §3.4).
PREVIEW_ELLIPSIS = "…"
# Caso sem áudio/sem transcrição: zero explícito, não erro. Texto-espelho
# do service:92-97.
TRANSCRIPTS_EMPTY_MESSAGE = (
    "Nenhum áudio com transcrição embutida encontrado no IPED "
    "(metadado 'audio:transcription'). Verifique se a extração "
    "possui transcrição automática de áudio."
)

# --- T-3 identidade: categorias, fontes e scores (DESIGN 09-B §3.2) ----------

# Template de query de categoria do motor (mesma forma do eixo telefonia;
# origem: iped-icaro/strategies/base_strategy.py:177).
IDENTITY_QUERY_TEMPLATE = TELEPHONY_QUERY_TEMPLATE

# Categorias do eixo identidade (19 entradas, contadas no design).
# Origem: iped-icaro/strategies/identity_strategy.py:136-144.
IDENTITY_CATEGORIES = (
    "contacts", "contatos", "user accounts", "contas de usuário",
    "account", "device info", "informações do dispositivo",
    "device information", "deviceinfoentry", "sim",
    "wireless networks", "redes sem fio",
    "whatsapp", "telegram", "messages", "emails", "chats", "calls",
    "recognized devices",
)

# Fontes ufed:Source que valem identidade mesmo sem categoria canônica.
# Origem: identity_strategy.py:146.
IDENTITY_VALID_SOURCES = (
    "siminfo", "native wifi", "wifi", "bluetooth devices", "native",
    "partner",
)

# Score por doc classificado (:177 default; :179-182 reconhecido). O boost
# 90/70 por ranking.high/medium (:184-193) vem de YAML EXTERNO ausente na
# cópia (FASE 3 §Limitações): NÃO portar valores inventados — boost
# desligado até existir a fonte (DESIGN 09-B §3.2).
SCORE_IDENTITY_DEFAULT = 50
SCORE_IDENTITY_RECOGNIZED = 70
# Justificação literal do reconhecido. Origem: identity_strategy.py:180-182.
RECOGNIZED_JUSTIFICATION = (
    "Dispositivo reconhecido pela conta de nuvem (ufed:RecognizedDevice)"
)

# Teto defensivo de dispositivos reconhecidos entregues (casos reais
# 5-20 aparelhos). Origem: draft_report_iped_manager.py:547.
RECOGNIZED_DEVICES_MAX = 50

# --- T-3 identidade: campos canônicos de propriedades (DESIGN 09-B §3.3) -----

# Variantes de nome: MESMA tupla da whitelist T-1.1 (DRY; §3.3).
IDENTITY_NAME_FIELDS = CONTACT_NAME_FIELDS

UFED_SOURCE_FIELD = "ufed:Source"  # origem: identity_strategy.py:156-162
ENTRY_NAME_FIELD = "ufed:EntryName"  # origem: :44 (Android) / :102 (Apple)
ENTRY_VALUE_FIELD = "ufed:EntryValue"  # origem: :45 / :103
UFED_CATEGORY_FIELD = "ufed:Category"  # origem: :74
UFED_VALUE_FIELD = "ufed:Value"  # origem: :75
CONTENT_TYPE_FIELD = "contentType"  # dupla guarda de reconhecidos: :168-170
RECOGNIZED_CATEGORY = "recognized devices"  # origem: :144/:166
RECOGNIZED_MARKER = "recognizeddevice"  # origem: :168-170 (substring)

# Device info Android: EntryName/EntryValue quando a fonte é nativa.
# Origem: identity_strategy.py:42-53 (skip de valor "N/A" em :51).
ANDROID_ENTRY_SOURCES = ("native", "siminfo", "partner")
ENTRY_SKIP_VALUE = "N/A"  # origem: :51/:77 (o "N/A" do jar, não o "N/I")
# Device info Apple/geral: categoria device info*. Origem: :100-113.
DEVICE_INFO_CATEGORY_MARKERS = (
    "device info", "informações do dispositivo", "deviceinfoentry",
)

# SIM MSISDN: fonte siminfo ou categoria sim. Origem: :57-79.
SIM_SOURCE = "siminfo"
SIM_CATEGORY_MARKER = "sim"
MSISDN_ENTRY_KEY = "MSISDN"  # origem: :66
SIM_MSISDN_CATEGORY = "SIM/USIM MSISDN"  # origem: :77
MSISDN_NAME_MARKER = "msisdn"  # origem: :78 (ufed:name com substring)
# Fallback de telefone do dono pelo device_info. Origem: :439-443.
MSISDN_FALLBACK_KEYS = ("MSISDN", "Last used MSISDN")

# Wi-Fi: ufed:SSId (SEM o side-effect de virar alias; decisão
# devices_networks_strategy.py:74-75, DESIGN 09-B §3.3). Origem: :81-89.
WIFI_SOURCE_MARKER = "wifi"
WIFI_CATEGORY_MARKER = "wireless"
SSID_FIELD = "ufed:SSId"
# Bluetooth: ufed:DeviceName. Origem: :91-96.
BLUETOOTH_MARKER = "bluetooth"
BLUETOOTH_NAME_FIELD = "ufed:DeviceName"

# Ramificação contas x contatos (is_accounts/is_contacts).
# Origem: identity_strategy.py:237-238.
ACCOUNT_CATEGORY_MARKERS = ("user accounts", "contas de usuário")
USER_ACCOUNTS_MARKER = "user accounts"  # exceção do ramo contatos (:238)
CONTACT_CATEGORY_MARKER = "contatos"
CONTACT_CATEGORY_EXACT = "contacts"

# Conta WhatsApp (accountType). Origem: identity_strategy.py:316-326.
ACCOUNT_TYPE_FIELD = "accountType"
WHATSAPP_ACCOUNT_MARKER = "whatsapp"
WA_PHONE_FIELD = "phoneNumber"  # :320
WA_NAME_FIELD = "userName"  # :321
PHOTO_ID_FIELD = "ufed:Photo:id"  # :314
# Confiança ALTA só com estas fontes E ufed:Name presente. Origem: :328-332.
HIGH_CONFIDENCE_SOURCE_MARKERS = (
    "google", "health", "alexa", "icloud", "apple", "native",
)
# E-mail da conta: EmailAddress; fallback Username só com "@".
# Origem: identity_strategy.py:310-311/:334-344.
EMAIL_FIELD = "ufed:EmailAddress"
USERNAME_FIELD = "ufed:Username"
AT_MARKER = "@"
# Aliases/usernames de conta. Origem: identity_strategy.py:312-313/:346-351.
ACCOUNT_ALIAS_FIELD = "account"
USERNAME_LIST_FIELD = "username"

# Telefones estruturados de conta. Origem: identity_strategy.py:257-262.
ACCOUNT_PHONE_FIELDS = ("ufed:PhoneNumber", "phoneNumber")
REGEX_PHONE_FIELD = "Regex:PHONE"  # origem: :264
# Nome clássico lido DIRETO pelo eixo (gate de confiança ALTA e branch
# SIM): o ICARO não usa a whitelist nestes pontos — paridade fiel.
# Origem: identity_strategy.py:329 (ALTA) / :78 (branch SIM, ufed:name).
NAME_CLASSIC_FIELD = "ufed:Name"  # mesmo literal de CONTACT_NAME_FIELDS[0]
NAME_LOWER_FIELD = "ufed:name"  # mesmo literal de CONTACT_NAME_FIELDS[1]

# Filtros de rejeição do Regex:PHONE (na ordem do ICARO :266-278):
PHONE_DECIMAL_STRIP_CHARS = (".", "-", " ")  # :266-268 (rejeita decimal)
PHONE_ICCID_PREFIX = "89"  # :270-271 (com >= 10 dígitos = ICCID)
PHONE_ICCID_MIN_DIGITS = 10
PHONE_INDICATORS = "+-() "  # :272-274 (sem indicador exige >= 11 dígitos)
PHONE_MIN_BARE_DIGITS = 11
PHONE_SPACED_PATTERN_RE = r"\d{2,3}\s\d{4,5}[-.]?\d{4}"  # :276-277
PHONE_MENTION_MIN_DIGITS = 10  # :278 (janela aceita)
PHONE_MENTION_MAX_DIGITS = 13
PHONE_INT_MAX_LITERAL = "2147483647"  # :278 (INT_MAX nunca é telefone)

# Agenda do eixo (known_contacts): candidatos na ordem de concatenação.
# Origem: identity_strategy.py:366-393.
KNOWN_CONTACT_PHONE_FIELDS = (
    "ufed:PhoneNumber", "phoneNumber", "Regex:PHONE", "ufed:UserID",
)
KNOWN_CONTACT_REJECT_MARKERS = ("@g.us", "@newsletter")  # :403/:407
LID_MARKER = "@lid"  # :411 (@lid só vale com nome legível)
KNOWN_CONTACT_MIN_DIGITS = 8  # :424-426 (recusa emergência/lixo)
KNOWN_CONTACT_MAX_DIGITS = 13
PARENTHETICAL_CLEAN_RE = r"\s*\(.*?\)"  # :455/:461 (nome/e-mail "(...)" )
PHONE_MIN_NORM_DIGITS = 8  # :467-471 (_norm_phone)
PERSONAL_EMAIL_DOMAINS = (
    "gmail", "outlook", "hotmail", "yahoo", "icloud", "apple",
)  # :215 (nome inferido do e-mail pessoal)

CONFIDENCE_ALTA = "ALTA"  # origem: identity_strategy.py:331
CONFIDENCE_MEDIA = "MÉDIA"  # origem: :210 (default; BAIXA só via handler)
DEFAULT_CONFIDENCE = CONFIDENCE_MEDIA

# Inferência de origem da evidência. Origem: identity_strategy.py:489-500.
EVIDENCE_DEVICE = "Dispositivo Móvel"
EVIDENCE_ICLOUD = "Nuvem iCloud"
EVIDENCE_GOOGLE = "Nuvem Google"
EVIDENCE_ONLINE = "Nuvem / Conta Online"
EVIDENCE_UNKNOWN = "Origem Desconhecida"
ICLOUD_EMAIL_MARKERS = ("icloud", "apple")  # :493
GOOGLE_EMAIL_MARKERS = ("gmail", "google")  # :495/:535

# --- T-3 identidade: schema de device_info (DESIGN 09-B §3.4) ----------------

# Schema comum iPhone/Android: 16 chaves; primeira alias que preenche vence;
# restante "N/I". As DUAS cópias idênticas no ICARO
# (identity_strategy.py:504-521 e draft_report_iped_manager.py:585-602)
# viram UMA constante aqui (DRY, regra 9 do lote).
DEVICE_INFO_SCHEMA = {
    "device_name": ["Device name", "DeviceName", "ro.product.model",
                    "Detected model", "Detected Phone Model", "DeviceModel"],
    "os_type": ["DeviceInfoOSType", "OS Type", "os_type", "OS"],
    "os_version": ["OS Version", "DeviceInfoOSVersion", "os_version"],
    "model": ["Detected model", "Detected Phone Model", "Model number",
              "model", "DeviceModel"],
    "imei": ["IMEI", "imei", "imei1", "IMEI1"],
    "imei2": ["IMEI2", "imei2"],
    "serial": ["Serial", "serial", "Serial number"],
    "msisdn": ["MSISDN", "Last used MSISDN", "SIM/USIM MSISDN"],
    "iccid": ["ICCID", "iccid", "Last user ICCID"],
    "imsi": ["IMSI", "imsi"],
    "mac_address": ["MAC address", "Wi-Fi MAC address",
                    "Bluetooth device address", "Bluetooth MAC Address",
                    "Randomized Mac Address"],
    "carrier": ["Carrier Name", "carrier", "Operator", "SPN"],
    "icloud_account": ["Apple ID", "iCloud account present"],
    "google_account": ["Google Account", "google_account"],
    "factory_reset": ["DeviceInfoFactoryReset", "Last Factory Reset"],
    "location_services": ["Location Services Enabled"],
}
DEVICE_INFO_UNKNOWN = "N/I"  # origem: identity_strategy.py:522
# Chaves do schema com regra de preenchimento especial.
# Origem: identity_strategy.py:531-543.
DEVICE_KEY_MSISDN = "msisdn"
DEVICE_KEY_GOOGLE_ACCOUNT = "google_account"
DEVICE_KEY_ICLOUD_ACCOUNT = "icloud_account"
ICLOUD_ACCOUNT_EMAIL_MARKERS = ("icloud", "me.com")  # origem: :541

# --- T-3 identidade: scoring do achado (DESIGN 09-B §3.5) --------------------

# Handler draft_report_iped_manager.py:847 (confiança -> score; else 50
# cobre BAIXA/ausente) e :856 (relevância >=85 / >=60, pin por valor).
IDENTITY_SCORE_BY_CONFIDENCE = {CONFIDENCE_ALTA: 90, CONFIDENCE_MEDIA: 70}
IDENTITY_SCORE_DEFAULT = 50
IDENTITY_RELEVANCE_ALTA_MIN_SCORE = 85
IDENTITY_RELEVANCE_MEDIA_MIN_SCORE = 60
# Justificação literal do handler (:848-852; amostra de 2 e-mails/telefones).
IDENTITY_JUSTIFICATION_TEMPLATE = "Entidade de identidade (confiança: {confidence})"
IDENTITY_JUSTIFICATION_EMAILS_SUFFIX = " | E-mails: {emails}"
IDENTITY_JUSTIFICATION_PHONES_SUFFIX = " | Tel: {phones}"
JUSTIFICATION_SAMPLE_SIZE = 2
NOT_IDENTIFIED_NAME = "Não identificado"  # origem: draft_report:846

# Amostra da agenda na saída (adição deliberada do MCP, DESIGN 09-B §3.7;
# precedente amostral T-1 SAMPLE_DOC_IDS_MAX).
KNOWN_CONTACTS_SAMPLE = 20

# --- T-3 identidade: dispositivos reconhecidos (DESIGN 09-B §3.6) ------------

# Campos lidos do item RecognizedDevice.
# Origem: iped-icaro/devices_networks/recognized_devices_extractor.py:88-110.
RECOGNIZED_DEVICE_FIELDS = {
    "ufed_id": "ufed:id",  # :89
    "device_model": "ufed:DeviceModel",  # :82/:92
    "brand": "ufed:Brand",  # :93
    "manufacturer": "ufed:DeviceInfoDetectedManufacturer",  # :94
    "device_type": "ufed:DeviceType",  # :95
    "imeis": "ufed:IMEI",  # :83/:96 (multi-vírgula, :32-42)
    "meid": "ufed:MEID",  # :97
    "serial_number": "ufed:SerialNumber",  # :84/:98
    "android_id": "ufed:DeviceInfoAndroidID",  # :99
    "android_fingerprint": "ufed:AndroidFingerprint",  # :100
    "firmware_version": "ufed:DeviceInfoFirmWareVersion",  # :101
    "hardware": "ufed:DeviceInfoHardwareNumber",  # :102
    "locale_language": "ufed:DeviceInfoLocaleLanguage",  # :103
    "timezone": "ufed:DeviceInfoTimeZone",  # :104
    "carrier_id": "ufed:DeviceInfoCarrierID",  # :105
    "ips": "ufed:IP Address",  # :106 (chave COM espaço)
    "decoding_confidence": "ufed:decoding_confidence",  # :107
    "is_related": "ufed:isrelated",  # :108
    "event_count": "ufed:jumpTargets",  # :109 (contagem :45-58)
}
# Gate essencial: model OU imeis OU serial; None senão.
# Origem: recognized_devices_extractor.py:82-86.
RECOGNIZED_ESSENTIAL_FIELDS = ("device_model", "imeis", "serial_number")
# Conta da nuvem no path (.ufdr). Origem: :61-72.
CLOUD_ACCOUNT_UFDR_RE = r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)\.ufdr"
CLOUD_ACCOUNT_ANY_RE = r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)"
# Rótulos PT da saída compacta (14 chaves; origem: format_for_prompt
# :133-149; o design §3.6 dizia 13 — contagem real no código-fonte é 14,
# portada fiel).
RECOGNIZED_OUTPUT_LABELS = {
    "device_model": "modelo",
    "brand": "marca",
    "device_type": "tipo",
    "imeis": "imei",
    "meid": "meid",
    "serial_number": "serial",
    "android_id": "android_id",
    "android_fingerprint": "fingerprint",
    "ips": "ips",
    "timezone": "timezone",
    "locale_language": "idioma",
    "source_account": "conta_nuvem",
    "event_count": "eventos",
    "decoding_confidence": "confianca_decodificacao",
}
# Atributos lidos com semântica especial no parse fiel (:32-58):
# multi-vírgula, contagem de eventos, rótulo com fallback de marca e
# valores padrão "" (o resto usa "N/I").
RECOGNIZED_MULTI_FIELDS = ("imeis", "ips")  # :32-42 (_split_multi)
RECOGNIZED_COUNT_FIELD = "event_count"  # :45-58 (_count_events)
RECOGNIZED_ACCOUNT_FIELD = "source_account"  # :61-72 (conta do path)
RECOGNIZED_BRAND_FIELD = "brand"  # :136 (marca = brand or manufacturer)
RECOGNIZED_MANUFACTURER_FIELD = "manufacturer"  # :136
RECOGNIZED_OUTPUT_EMPTY_DEFAULTS = {
    "meid": "",  # :138
    "android_id": "",  # :140
    "fingerprint": "",  # :141
}

# Caso sem identidade: triagem vazia explícita, não exceção (§0.1;
# espelha base_strategy.py:71-73 — StrategyResult vazio).
IDENTITY_EMPTY_MESSAGE = (
    "nenhum documento do eixo identidade (contas, dispositivos, SIM, "
    "wireless, reconhecidos) neste source"
)

# --- Orçamento R-TRIAGE-1 (DESIGN 09-B §2.2; aprovado no lote) ----------------

# Teto padrão de GET internos por chamada (veredito §5.2). As tools ainda
# NÃO-orçadas (retrofit no item R-TRIAGE-1) já nascem com o valor definido.
DEFAULT_MAX_INTERNAL_GETS = 3000
# Orçamento EFETIVO por tool (justificativas por linha no design §2.2).
TOOL_BUDGETS = {
    "triage_telephony": 3000,  # medido 2.594 no runtime (folga 15%)
    "transcripts_generate": 3000,  # medido 235; cap de áudios fica abaixo
    "triage_identity": 5100,  # 19 queries (:136-144) + TOTAL_CANDIDATE_CAP
                              # 5000 + margem 81 (design §2.2)
    "triage_financial": 5100,  # ~22 queries + 5000 docs + margem
    "triage_geolocation": 3000,  # probes esparsos (<= TOTAL_CAP)
    "triage_media": 5100,  # 11 hints + 5000 docs (eixo mais denso)
    "triage_devices_networks": 3000,  # categorias pequenas
    "triage_communication": 3000,  # CHAT_SCAN_CAP 2500 + probes
}
# Aviso canônico de estouro (§2.3; NUNCA exceção, NUNCA silencioso).
HTTP_BUDGET_WARNING_TEMPLATE = (
    "http_budget_exceeded: varredura interrompida em {calls} GETs "
    "(cap {cap}); resultados PARCIAIS — {omitted}"
)
