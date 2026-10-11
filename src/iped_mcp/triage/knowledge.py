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

# --- T-4 financeiro: categorias, termos e detectores (DESIGN 09-B §4.2/§4.3) -

# Categorias do eixo financeiro (PT-BR + EN; matching por substring
# case-insensitive da categoria no gate).
# Origem: iped-icaro/strategies/financial_strategy.py:32-35 (13 categorias).
FINANCIAL_CATEGORIES = (
    "documents", "imagens", "pdf", "spreadsheets", "chats", "messages",
    "emails", "documentos", "planilhas", "mensagens", "images", "pictures",
    "fotos",
)

# Categorias de cartão: passam direto no gate, sem termo financeiro.
# Origem: financial_strategy.py:38.
FINANCIAL_CARD_CATEGORIES = (
    "mobile cards", "credit cards", "transfers of funds", "locations",
)

# Marcador de conta/ativo financeiro: substring de categoria que casa o
# singular e o plural. Origem: financial_strategy.py:47-49; o gate do
# plural (ativo) usa a variante explícita :122-128.
FINANCIAL_ACCOUNT_MARKER = "financial account"
FINANCIAL_ACCOUNT_PLURAL_MARKER = "financial accounts"

# Queries de coleta do eixo: 13 categorias + 4 de cartão + o marcador de
# conta. Origem: DESIGN 09-B §2.2 (18 queries de categoria; +4 probes
# Regex §4.3 = ~22 GET de /search do orçamento 5100).
FINANCIAL_QUERY_CATEGORIES = (
    *FINANCIAL_CATEGORIES,
    *FINANCIAL_CARD_CATEGORIES,
    FINANCIAL_ACCOUNT_MARKER,
)
FINANCIAL_QUERY_TEMPLATE = TELEPHONY_QUERY_TEMPLATE  # mesma forma category:"X"

# Termos financeiros no nome/path (minúsculos). O "transferencia" SEM
# acento é o literal do ICARO — "transferência" NUNCA casa: quirk fiel
# pinnado em teste. Origem: financial_strategy.py:56.
FINANCIAL_TERMS = (
    "comprovante", "pix", "extrato", "boleto", "transferencia", "pagamento",
    "nota fiscal",
)

# Mídia sem termo no nome é descartada MESMO com Regex:MONEY (anti-ruído;
# substring do nome minúsculo). Origem: financial_strategy.py:59-63.
FINANCIAL_MEDIA_EXTS = (".svg", ".tmp", ".heic", ".jpg", ".jpeg", ".png", ".gif")

# Detectores Regex sondados POR CASO antes de confiar (a lista é o consumo
# real do ICARO, não o catálogo local KNOWN_TYPES — BR_BOLETO e CREDIT_CARD
# não estão em regex/queries.py:11-21). Origem: financial_strategy.py:168-170
# + DESIGN 09-B §4.3. A sonda CREDIT_CARD é INFORMATIVA (painel): o
# extrator de cartões usa categorias+campos ufed (:243-334), nunca a prop.
FINANCIAL_REGEX_PROBES = ("MONEY", "BR_BOLETO", "BR_BANK_ACCOUNT", "CREDIT_CARD")
FINANCIAL_REGEX_UNAVAILABLE_WARNING = (
    "detectorRegex:{name} indisponível neste caso (0 hits)"
)
# Gate regex de classificação: SOMENTE MONEY/BR_BOLETO (:57 — o
# BR_BANK_ACCOUNT NÃO liga o gate; quirk fiel).
FINANCIAL_GATE_REGEX_FIELDS = ("Regex:MONEY", "Regex:BR_BOLETO")

# --- T-4 financeiro: campos de propriedades (DESIGN 09-B §4.4) ----------------

# Propriedades dos detectores nas evidências. Origem: financial_strategy.py:168-170.
FINANCIAL_REGEX_MONEY_FIELD = "Regex:MONEY"
FINANCIAL_REGEX_BOLETO_FIELD = "Regex:BR_BOLETO"
FINANCIAL_REGEX_BANK_FIELD = "Regex:BR_BANK_ACCOUNT"

# Escada method/amount da evidência. Origem: financial_strategy.py:183-197.
FIN_METHOD_MONEY = "Dinheiro/Valor Encontrado"      # :185
FIN_AMOUNT_BOLETO = "Boleto Bancário"               # :187
FIN_METHOD_BOLETO = "Boleto"                        # :188
FIN_AMOUNT_UNIDENTIFIED = "Valor não identificado"  # :190 (também pino de score :886)
FIN_METHOD_ACCOUNT = "Comprovante/Conta"            # :191
FIN_BENEFICIARY_UNKNOWN = "N/I"                     # :197

# Parser BR de valores (_parse_amount_to_float :206-241): sentinela -1.0,
# regex de números, marcadores não-numéricos e regra do ponto de milhar
# (último bloco de exatamente 3 dígitos). Origens nas linhas citadas.
AMOUNT_NUMBER_RE = r"([\d\.\,]+)"                    # :218
AMOUNT_SENTINEL = -1.0                               # :209/:214/:220/:241
AMOUNT_UNPARSEABLE_MARKERS = ("Valor não identificado", "Boleto")  # :213
AMOUNT_THOUSANDS_BLOCK = 3                           # :232-236

# Cartões (:243-334). Marcadores de categoria :260/:273; chaves de indexação
# p/ jumpTargets por ufed:id[0] e sufixo-UUID do nome :244-252.
CARD_CREDIT_MARKERS = ("credit card", "creditcard")  # :260
CARD_MOBILE_MARKERS = ("mobile card", "mobilecard")  # :273
CARD_ID_FIELD = "ufed:id"                            # :247-249
CARD_NAME_UUID_SEP = "_"                             # :251-252
CARD_NUMBER_FIELD = "ufed:CreditCardNumber"          # :261
CARD_COMPANY_FIELD = "ufed:Company"                  # :265-266
CARD_EXPIRATION_FIELD = "ufed:ExpirationDate"        # :268
CARD_DESCRIPTION_FIELD = "ufed:Description"          # :274
CARD_ORGANIZATION_FIELD = "ufed:Organization"        # :279
CARD_PURCHASE_TIME_FIELD = "ufed:PurchaseTime"       # :280
CARD_WALLET_FIELD = UFED_SOURCE_FIELD                # :269/:281 ("ufed:Source")
CARD_UNKNOWN_KEY = "unknown"                         # :262/:275 (chave default)
CARD_JUMPS_FIELD = "ufed:jumpTargets"                # :288
CARD_JUMP_TX_MARKERS = ("transfer", "fund")          # :300
CARD_JUMP_LOCATION_MARKER = "location"               # :317
CARD_TX_AMOUNT_FIELD = "ufed:Amount"                 # :302-304
CARD_TX_AMOUNT_DEFAULT = "0"                         # :303 (default do parse)
CARD_TX_CURRENCY_FIELD = "ufed:Currency"             # :305
CARD_TX_DATE_FIELD = "ufed:DateProcessed"            # :306
CARD_TX_STATUS_FIELD = "ufed:Status"                 # :307
CARD_TX_TYPE_FIELD = "ufed:TransferType"             # :308
CARD_TX_PAYEE_FIELD = "Communication:To"             # :309
CARD_TX_PAYER_FIELD = "Communication:From"           # :310
CARD_GEO_FIELD = "common:geo:locations"              # :318 (= GEO_LOCATION_FIELD T-5)
CARD_GEO_RE = r"(-?\d+\.\d+)[;,\s]+(-?\d+\.\d+)"     # :320
CARD_GEO_TYPE = "Mobile Payment"                     # :325
CARD_TIMESTAMP_FIELD = "ufed:TimeStamp"              # :326

# Contas/ativos (:106-159). UUID da conta-pai no path do ativo :21; conta de
# nuvem pelo e-mail no path .ufdr :22; contentType markers :126-136.
ACCOUNT_UUID_IN_PATH_RE = r"FinancialAccount_([0-9a-fA-F-]{36})"   # :21
EMAIL_IN_PATH_RE = r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)\.ufdr"     # :22
ASSET_CONTENT_MARKER = "financialasset"              # :126-128
ACCOUNT_CONTENT_MARKER = "financialaccount"          # :135-136
ASSET_CURRENCY_FIELD = "ufed:Currency"               # :132
# Campos da conta na saída (chave de saída -> campo ufed). Origem :141-150.
ACCOUNT_OUTPUT_FIELDS = (
    ("account_id", "ufed:AccountID"),            # :143
    ("account_type", "ufed:FinancialAccountType"),  # :144
    ("found_in_type", "ufed:FoundInModelType"),  # :145
    ("found_in_field", "ufed:FoundInField"),     # :146
    ("source", "ufed:Source"),                   # :147
    ("is_related", "ufed:isrelated"),            # :148
    ("decoding_confidence", "ufed:decoding_confidence"),  # :149
)

# --- T-4 financeiro: scoring dos achados (DESIGN 09-B §4.5) -------------------

# Handler _finding_from_financial_evidence draft_report_iped_manager.py:885-896.
FIN_EVIDENCE_SCORE_WITH_AMOUNT = 75  # :886 (amount presente e != unidentificado)
FIN_EVIDENCE_SCORE_NO_AMOUNT = 55    # :886
FIN_EVIDENCE_PIX_MARKER = "PIX"      # :887 ("PIX" em method.upper() -> ALTA)
# Handler _finding_from_card :900-911.
CARD_SCORE_BASE = 60                 # :901 min(60 + tx*2, 95)
CARD_SCORE_PER_TX = 2                # :901
CARD_SCORE_CAP = 95                  # :901
CARD_RELEVANCE_ALTA_MIN_SCORE = 85   # :905 (>=85 ALTA senão MÉDIA — sem BAIXA no handler)
# Conta financeira: SEM handler dedicado no ICARO (cai no generic :963-965
# score 30). RECOMMENDATION DESIGN 09-B §4.5: score fixo igual ao item
# financeiro estruturado (financial_strategy.py:72) e relevância pelas
# bordas do handler genérico (draft_report:965).
FIN_ACCOUNT_SCORE = 90               # == SCORE_FIN_ITEM de conta :72
RELEVANCE_ALTA_MIN_SCORE = 85        # genérico :965 (>= 85 ALTA)
RELEVANCE_MEDIA_MIN_SCORE = 60       # genérico :965 (>= 60 MÉDIA)

# Templates literais dos achados (preservados para correlação de relatório).
FIN_EVIDENCE_ENTITY_TEMPLATE = "{method} — {beneficiary}"                 # :890
FIN_EVIDENCE_JUSTIFICATION_TEMPLATE = "Valor: {amount} | Método: {method}"  # :893
FIN_EVIDENCE_SOURCE_FALLBACK = "Financeiro"    # :894 (sem arquivo resolvido)
FIN_EVIDENCE_LOCAL_PATH_FIELD = "ufed:local_path"  # fallback da label :688-690
CARD_ENTITY_TEMPLATE = "{card_type}: {label}"  # :902 (label = descricao ou mascarado)
CARD_JUSTIFICATION_TEMPLATE = "{count} transações | Total: R$ {total:,.2f}"  # :907
CARD_WALLET_FALLBACK = "Carteira digital"      # :908

# Tetos de entrega (§4.6; o ICARO ordena evidências SEM teto :201 — o teto
# MCP é sempre marcado em truncated com omissão contada).
FINANCIAL_LIST_TOP = 30               # evidences[:30] e accounts[:30] (§4.6)
CARD_TRANSACTIONS_SAMPLE = 5          # "últimas 5 transações resumidas" (§4.6)

# Boost 95 por ranking.high (:73-76) NÃO portado: YAML externo ausente
# (mesma decisão §3.2 do boost de identidade — nada é inventado).

# Caso sem eixo financeiro: triagem vazia explícita, não exceção (§0.1;
# espelha base_strategy.py:71-73 — StrategyResult vazio).
FINANCIAL_EMPTY_MESSAGE = (
    "nenhum documento do eixo financeiro (categorias de arquivo, cartões, "
    "contas/ativos) com evidência financeira neste source"
)

# --- T-5 geolocalização: probes, campos e clusters (DESIGN 09-B §5) -----------

# Campo de coordenadas consolidado do ICARO.
# Origem: geolocation_strategy.py:26 (GEO_LOCATION_FIELD).
GEO_LOCATION_FIELD = "common:geo:locations"

# Probes ORDENADAS de coleta (§5.1 — o ICARO não filtra categoria: a coleta
# vinha do YAML de targets, ausente; a primeira é o campo fallback do
# ICARO, as demais são variantes de categoria PROVÁVEIS, HYPOTHESIS
# TO_VERIFY-B2 — nenhuma lista fechada, cada 0-hit/falha vira warning).
# Forma canônica com escape do campo (P5/M-13); template category: T-1.
GEO_FIELD_PROBE = "common\\:geo\\:locations:*"
GEO_CATEGORY_PROBES = (
    "locations", "location", "gps", "geolocation", "exif",
)
GEO_PROBE_QUERIES = (
    GEO_FIELD_PROBE,
    *(TELEPHONY_QUERY_TEMPLATE.format(category=cat) for cat in GEO_CATEGORY_PROBES),
)
GEO_PROBE_ZERO_WARNING = (
    "sonda geoespacial '{query}' sem hits (cobertura limitada às probes "
    "listadas — docs geo em categorias não sondadas podem não aparecer)"
)

# Gate por props (a VERDADE do eixo): QUALQUER chave contendo um destes
# marcadores (case-insensitive). Origem: geolocation_strategy.py:215-221.
GEO_GATE_KEY_MARKERS = ("latitude", "longitude", "geo:location")

# Campos de timestamp na ordem literal do ICARO. Origem: :29-35.
GEO_TIMESTAMP_FIELDS = (
    "ufed:TimeStamp", "ufed:StartTime", "timeStamp", "created", "modified",
)

# Padrão "lat,lon" em texto (também ';' e espaço). Origem: :37-39.
GEO_COORDINATE_RE = r"(-?\d+(?:\.\d+)?)[;,\s]+(-?\d+(?:\.\d+)?)"

# Bounds geográficos. Origem: :82-83.
GEO_LAT_MIN, GEO_LAT_MAX = -90.0, 90.0
GEO_LON_MIN, GEO_LON_MAX = -180.0, 180.0

# Null Island: (0,0) é placeholder de falha de leitura de GPS, não
# localização. O ICARO descarta 0,0 exato (:86-88); o design 09 §1 T-5
# alarga para |lat|<=1e-4 E |lon|<=1e-4 (coordenadas mínimas de placeholder
# continuando descartadas; 0.0002 NÃO é descartada — pino de teste).
NULL_ISLAND_EPS = 0.0001  # origem: :86-88 (aditamento DESIGN 09-B §5.2)

# Agrupamento por 4 casas decimais (~11 m). Origem: :90-94.
GEO_CLUSTER_DECIMALS = 4

# Tipo de fonte por substring da categoria (ordem do elif chain).
# Origem: geolocation_strategy.py:165-172; GEO_METADATA é o rótulo do
# campo geo:location no design §5.2 (senão, a categoria crua).
GEO_SOURCE_TYPE_BY_CATEGORY = (
    (("image", "photo", "video"), "EXIF"),       # :165-166
    (("chat", "message", "mensagens"), "CHAT"),  # :167-168
    (("wifi",), "WIFI"),                         # :169-170
    (("cell", "torre"), "CELL_TOWER"),           # :171-172
)
GEO_TYPE_GEO_METADATA = "GEO_METADATA"   # :174 (fallback do chain do ICARO)
GEO_TYPE_NO_CATEGORY = "sem_categoria"   # MCP: categoria crua vazia (§5.2)

# --- T-5 geolocalização: scoring do achado (DESIGN 09-B §5.2) -----------------

# Handler _finding_from_geolocation draft_report_iped_manager.py:932-947
# (a PONTUAÇÃO DO ACHADO é a do handler; a strategy usa score fixo 80 no
# item :54 — não portado porque o achado é o que sai).
GEO_SCORE_BASE = 50            # :933 min(50 + count*5, 95)
GEO_SCORE_PER_EVIDENCE = 5     # :933
GEO_SCORE_CAP = 95             # :933
# Limiares ESTRITOS (contagem exata 3 NÃO é MÉDIA; 11 é ALTA). Origem :934.
GEO_RELEVANCE_ALTA_COUNT = 10
GEO_RELEVANCE_MEDIA_COUNT = 3
# Justificação do design §5.2 (primeiro/último período lexicográficos,
# "N/I" quando ausente — :147/:149).
GEO_JUSTIFICATION_TEMPLATE = (
    "Coordenadas: {lat:.5f}, {lon:.5f} | Ocorrências: {count} | "
    "Período: {first} até {last}"
)
GEO_NOT_IDENTIFIED_TIMESTAMP = "N/I"  # origem: :147/:149
GEO_POINTS_TOP = 100                  # points[:100] marcado (§5.4)

# SEM enriquecimento de país: CoordinatesToCountryTool é ICARO-side/externa
# (rede/ML — fora do contrato WebAPI do iped-mcp, DESIGN 09-B §5.3).
# Campos country/state/city simplesmente NÃO aparecem: ausente ≠ falso.

# Caso sem geolocalização: triagem vazia explícita, não exceção (§0.1).
GEO_EMPTY_MESSAGE = (
    "nenhuma coordenada geográfica válida (fora de bounds e não-Null Island) "
    "nas probes de geolocalização deste source"
)

# Justificação da conta financeira: o ICARO não tem handler dedicado (cai no
# generic :973-983); texto MCP documentado em DESIGN 09-B §4.5.
FIN_ACCOUNT_JUSTIFICATION = "Conta financeira estruturada (FinancialAccount)"

# --- T-6 mídia: categorias, sinais e tetos (DESIGN 09-B §6) --------------------

# Categorias aceitas no eixo mídia (substring da categoria minúscula; o gate
# original é ``any(h in cat for h in _CAT_HINTS)``, media_strategy.py:73-74).
# Origem: iped-icaro/strategies/media_strategy.py:22-23 (_CAT_HINTS, 10
# entradas). DIVERGÊNCIA DO DESIGN: o §6.1 lista "11 entradas" acentuadas
# (fotos/vídeos/áudios/pictures/audio-video) — a cópia real do ICARO contém
# EXATAMENTE estas 10; variantes não portadas (registro D-10 no relatório da
# fase; regra de evidência: a fonte manda).
MEDIA_CATEGORIES = (
    "image", "video", "audio", "document", "pdf", "download",
    "spreadsheet", "presentation", "archive", "whatsapp",
)

# contentType que identifica chat mesmo sem categoria confiável.
# Origem: media_strategy.py:25 (_CHAT_CTYPES).
MEDIA_CHAT_CONTENT_TYPES = ("x-whatsapp-chat", "x-ufed-chat")

# 12 preditores de sinal: (nome, campos candidatos, pontos, avaliador).
# Origem: media_strategy.py:28-46 (_SIGNALS, pontos e rótulos literais).
# O 4º elemento escolhe o predicado em triage/media.py (dados em knowledge,
# predicados em código — separação de responsabilidades); o par (campo,
# ponto) é o literal da fonte. "nudez" usa >=2 e não >0 (:31, quirk pinado).
MEDIA_SIGNALS = (
    ("OCR", ("ocrCharCount",), 15, "num_positive"),            # :29
    ("rosto", ("face_count",), 10, "num_positive"),            # :30
    ("nudez", ("classeNudez",), 20, "num_at_least_2"),         # :31
    ("transcrito", ("audio:transcription",), 15, "present"),   # :32
    ("CPF", ("Regex:BR_CPF",), 10, "present"),                 # :33
    ("telefone", ("Regex:PHONE",), 8, "present"),              # :34
    ("URL", ("Regex:URL", "ufed:URL"), 5, "present"),          # :35
    # GPS mantém AMBAS as variantes — TODO do próprio ICARO (:36-37
    # "Chaves de GPS podem variar - validar na prova real!"); cobertura 0
    # vira warning (MEDIA_GPS_ZERO_WARNING).
    ("GPS", ("latitude", "ufed:latitude"), 15, "present"),     # :36-37
    ("ext divergente", ("extMismatch",), 10, "flag_true"),     # :38
    ("carved", ("carved",), 5, "flag_true"),                   # :39
    ("deletado", ("deletado", "deleted"), 15, "flag_true"),    # :40-41
    # "download financeiro": substring em ufed:URL (:42-45). O literal é
    # "registrato" (não "registrado") — quirk pinado, não corrigir.
    ("download financeiro", ("ufed:URL",), 15, "download_url"),  # :42-45
)

# Substrings do preditor "download financeiro". Origem: media_strategy.py:43-44.
MEDIA_DOWNLOAD_URL_MARKERS = (
    "boleto", "registrato", "bcb.gov.br", "comprovante", "fatura", "extrato",
)

# Base do score: 40 chat / 50 mídia. Origem: media_strategy.py:77; score =
# base + soma dos pontos dos sinais encontrados (:78-79); justificação =
# nomes dos sinais (:80-81, literais).
MEDIA_BASE_CHAT = 40
MEDIA_BASE_MEDIA = 50
MEDIA_JUSTIFICATION_CHAT = "Chat (índice de referências)"        # :80
MEDIA_JUSTIFICATION_PREFIX = "Mídia: "                           # :81
MEDIA_JUSTIFICATION_NO_SIGNALS = "Mídia sem sinais"              # :81

# Gate/chat: "whatsapp" na categoria E "chat" no nome, OU contentType marcada
# (media_strategy.py:70-72); contentType lido de "contentType" (:70).
MEDIA_CHAT_CATEGORY_MARKER = "whatsapp"
MEDIA_CHAT_NAME_MARKER = "chat"
MEDIA_CONTENT_TYPE_FIELD = "contentType"

# Handler _finding_from_ranked_item (draft_report:963-975): score do próprio
# item (default 50 :964 — no MCP o score é sempre o do preditor), relevância
# ALTA >= 85 / MÉDIA >= 60 / BAIXA (:965).
MEDIA_RELEVANCE_ALTA_MIN = 85
MEDIA_RELEVANCE_MEDIA_MIN = 60
MEDIA_SCORE_DEFAULT = 50  # :964 (getattr default; documentado no docstring)

# Itens ranqueados entregues (teto MCP do eixo mais denso; §6.5).
MEDIA_ITEMS_MAX = 50

# Política de poda de serialização do ICARO: _MEDIA_DROP remove chaves de
# blobs/binário e _MEDIA_MAX_VAL corta valores gigantes ANTES do prompt
# (draft_report:75-77; uso real :798-806, substring case-insensitive da
# chave). O envelope MCP NÃO serializa props cruas — a constante entra com o
# papel dado pelo design §6.2: excluir essas chaves do histograma de campos
# publicado (counters.field_hits), mesma intenção/custo zero. O teto de
# valor vira pin de privacidade: nomes de campos entram no histograma,
# VALORES jamais.
MEDIA_DROP_KEYS = (
    "face_encodings", "imagefeatures", "face_locations",
    "thumb", "base64", "features",
)
MEDIA_MAX_VAL = 5000
MEDIA_FIELD_HITS_TOP = 40  # política MCP: tamanho do histograma de nomes

# Agregados do eixo (draft_report._media_owner_summary :1500-1537):
# categoria crua ou "N/I" (:1509), apps por path/marcador e ServiceIdentifier
# (:1519-1524), sinais de dono com "N/I" ignorado, dedup e teto 20
# (:1525-1528, :1533).
# Chaves de dono VISTAS EM MÍDIA. Origem: draft_report:81-84
# (_OWNER_MEDIA_SIGNAL_KEYS). DIVERGÊNCIA DO DESIGN: o §6.3 lista
# ("Author","dc_creator",...) — a cópia real tem EXATAMENTE estas 5 (registro
# D-11 no relatório da fase).
MEDIA_OWNER_SIGNAL_KEYS = (
    "ufed:Account", "ufed:PhoneOwner", "ufed:ServiceIdentifier",
    "Conversation:Account", "common:dc:creator",
)
MEDIA_OWNER_SIGNALS_MAX = 20                    # :1533 signals[:20]
MEDIA_WHATSAPP_PATH_MARKER = "com.whatsapp"     # :1520
MEDIA_WHATSAPP_APP_LABEL = "WhatsApp"           # :1520
MEDIA_SERVICE_IDENTIFIER_FIELD = "ufed:ServiceIdentifier"  # :1522
MEDIA_NOT_IDENTIFIED_VALUE = "N/I"              # :1509/:1527

# Caso sem mídia aceita: triagem vazia explícita, não exceção (§0.1;
# espelha base_strategy.py:71-73 — StrategyResult vazio).
MEDIA_EMPTY_MESSAGE = (
    "nenhum documento do eixo mídia (mídia, downloads ou chats de "
    "referência) passou no gate deste source"
)

# Warning de cobertura do GPS (TODO do ICARO :36-37 materializado como
# honestidade de superfície, M-7): mídia varrida sem UM único GPS.
MEDIA_GPS_ZERO_WARNING = (
    "cobertura GPS = 0: nenhuma das chaves {keys} produziu sinal na mídia "
    "varrida (o índice pode usar outra variante — sinalizador de "
    "descoberta, não afirma ausência de GPS no caso)"
)

# --- T-7 dispositivos/redes: baldes, parse e tetos (DESIGN 09-B §7) -------------

# O único balde de LOG deste eixo vem da categoria "logs" — o bucket do
# ICARO é o marcador "log" na categoria minúscula
# (devices_networks_strategy.py:70-72). wifi/bluetooth chegam de PROPS de
# docs do eixo identidade; no MCP, T-7 re-sonda pelas mesmas categorias de
# T-3 derivadas por filtro (design §7.2, DRY regra 9 — jamais réplica
# literal das tuplas).
DEVICES_NETWORKS_LOG_CATEGORY = "logs"            # devices_networks_strategy.py:70-72
DEVICES_NETWORKS_LOG_MARKER = "log"               # :70 (bucket por substring)
DEVICES_NETWORKS_WIRELESS_MARKER = "wireless"     # :77
DEVICES_NETWORKS_WIFI_SOURCE_MARKER = "wifi"      # :77 (ufed:Source)
DEVICES_NETWORKS_BLUETOOTH_MARKER = BLUETOOTH_MARKER  # :85 (mesma marca de T-3)

# Filtro de IDENTITY_CATEGORIES (:207-214): pares EN/PT de redes sem fio.
DEVICES_NETWORKS_WIRELESS_CATEGORIES = tuple(
    category
    for category in IDENTITY_CATEGORIES
    if DEVICES_NETWORKS_WIRELESS_MARKER in category
    or "sem fio" in category
)
# Bluetooth não é categoria em IDENTITY_CATEGORIES — o literal "bluetooth
# devices" vive em IDENTITY_VALID_SOURCES (:218, identity_strategy.py:146)
# e é reaproveitado por filtro (mesma marca :85 do bucket bluetooth).
DEVICES_NETWORKS_BLUETOOTH_CATEGORIES = tuple(
    category
    for category in IDENTITY_VALID_SOURCES
    if DEVICES_NETWORKS_BLUETOOTH_MARKER in category
)

# Campos de props dos três baldes (devices_networks_strategy.py:76-88;
# logs_networks_normalizer.py:161/:170/:176).
DN_UFED_SOURCE_FIELD = "ufed:Source"        # strategy :76
DN_SSID_FIELDS = ("ufed:SSId", "ufed:SSID")  # strategy :78
DN_LOG_BODY_FIELD = "ufed:Body"             # normalizer :161
DN_APPLICATION_FIELD = "ufed:Application"   # normalizer :170
DN_TIMESTAMP_FIELD = "ufed:TimeStamp"       # normalizer :176

# Regex literais do normalizer (portar é portar o normalizer — §7.1).
# Body 'Service=CLOUDPHOTOLIBRARY IP=2804:...:a7b4' — formato estável (:40).
DN_LOG_BODY_RE = r"\bService=(?P<service>[A-Za-z0-9_-]+)\s+IP=(?P<ip_raw>\S+)"
# ufed:Application formato B: '<D73AP> <iPhone OS;26.6;23G71> <bundle/..>' (:43-47).
DN_APP_BRACKETS_RE = (
    r"<(?P<hw_model>[A-Za-z]\d+[A-Za-z]{0,3})>"
    r"\s*<(?P<os_name>[^;<>]+);(?P<os_version>[\d.]+);(?P<build>[0-9A-Za-z]+)>"
    r"(?:\s*<(?P<bundle>[\w.]+)/[^>]*>)?"
)
# ufed:Application formato C: 'fmflocatord/7.0 iPhone15,2/23F77' (:50-53).
DN_APP_DAEMON_RE = (
    r"^(?P<app>[\w.-]+)/(?P<app_version>[\d.]+)\s+"
    r"(?P<model>(?:iPhone|iPad|iPod)\d+,\d+)/(?P<build>[0-9A-Za-z]+)\s*$"
)
# ufed:Application formato A: '23F77; iPhone15,2' (:56-58).
DN_APP_PAIR_RE = (
    r"^(?P<build>[0-9A-Za-z]{4,8});\s*"
    r"(?P<model>(?:iPhone|iPad|iPod)\d+,\d+)\s*$"
)
# Fallback: qualquer identifier de aparelho no campo (:61).
DN_MODEL_ANY_RE = r"\b((?:iPhone|iPad|iPod)\d+,\d+)\b"
# ufed:TimeStamp: '14/08/2026 17:25:13 UTC' (:64).
DN_TIMESTAMP_RE = r"^(?P<d>\d{2}/\d{2}/\d{4})\s+(?P<h>\d{2}:\d{2}:\d{2})"

# Rótulos legíveis dos serviços iCloud (fallback = código cru) — normalizer
# :68-82, literais.
DN_SERVICE_LABELS = {
    "FMIP": "Buscar (Find My)",
    "FMF": "Localização de amigos (Find My Friends)",
    "FINDMY": "Buscar (Find My)",
    "CLOUDPHOTOLIBRARY": "Fotos do iCloud",
    "ICLOUDDRIVE": "iCloud Drive",
    "NOTES": "Notas (iCloud)",
    "IMESSAGE": "Mensagens (iMessage)",
    "CONTACTS": "Contatos (iCloud)",
    "CALENDAR": "Calendário (iCloud)",
    "ICLOUD": "iCloud (geral)",
    "BACKUP": "Backup do iCloud",
    "SETTINGS": "Ajustes (iCloud)",
    "SAFARI": "Safari (iCloud)",
}

# Tetos de seção do envelope (§7.6). A timeline bruta do ICARO
# (_MAX_RAW_CONNECTIONS=1000, normalizer :35/:254-255) NÃO existe no MCP —
# o conceito portado é connections_truncated sobre a visão agregada.
DN_CONNECTIONS_BY_IP_MAX = 50    # §7.6 [:50] por hits, marcado
DN_WIFI_NETWORKS_MAX = 100       # §7.6 [:100]
DN_BLUETOOTH_DEVICES_MAX = 100   # §7.6 [:100]

# Eixo sem nada aproveitável: None no ICARO (build_payload :280-281); MCP:
# vazio explícito com mensagem (§0.1, nunca exceção).
DN_EMPTY_MESSAGE = (
    "nenhum registro de rede/dispositivo aproveitável (logs Service/IP, "
    "SSIDs ou nomes bluetooth) neste source"
)
