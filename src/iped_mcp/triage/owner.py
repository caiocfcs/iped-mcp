"""Tool ``owner_candidates`` — candidatos a proprietário do dispositivo (T-9).

Porta o MOTOR HEURÍSTICO de ``owner_inference_engine.py`` do ICARO
(``iped-icaro/owner/owner_inference_engine.py``, 100% determinístico — o LLM
do ICARO entra só no resumo geral, fora daqui) sem re-extrair dado algum:
os feeds consomem por IMPORT as entidades dos eixos já computados na MESMA
chamada (T-3 ``triage_identity``, T-4 ``triage_financial``, T-6
``triage_media``, T-8 ``triage_communication`` — nunca as tools MCP
registradas, e nenhuma query nova é emitida; o ``http_calls`` do envelope é
a união dos eixos-membro, cap composto ``OWNER_BUDGET``, DESIGN 09-B §9).

Divergências documentadas do motor original (design §9.3/§12):

- Telefonia (8/10, engine :223-229) e geolocalização (:240-241) NÃO são
  eixos-membro do orçamento composto (§2.2/§9.6 listam 4 eixos); o feed de
  telefonia é portado por paridade com o engine e recebe ``None`` no fluxo.
- O feed de mídia consome ``owner_media_signals`` (envelope T-6) porque o
  M-9 não publica props brutas (o engine lia Author/Owner/Creator/By-line,
  :243-252); peso idêntico (10/10), fonte agregada equivalente.
- ``ufed:Name`` por conta (65/65, :162-167) não tem fonte no envelope T-3
  (só o nome de maior confiança é publicado, identity.py:417-434); a MESMA
  string entra pelo feed 70/70 e a fusão do próprio engine resultaria em 70
  de todo modo — sem perda para o nome primário.
- O feed de path usa os paths dos docs varridos, não o "path do laudo"
  (D-8 — não há input equivalente no contrato de 1 argumento).
- O sentinela de nome do envelope T-3 é ``NOT_IDENTIFIED_NAME``
  ("Não identificado"); o guard do feed o veto explicitamente (no ICARO o
  campo vinha vazio quando não identificado).
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable


from iped_mcp import progress
from iped_mcp.progress import op_envelope
from iped_mcp.router import get_router
from iped_mcp.triage import knowledge
from iped_mcp.triage.communication import build_triage_communication
from iped_mcp.triage.financial import build_triage_financial
from iped_mcp.triage.identity import build_triage_identity
from iped_mcp.triage.media import build_triage_media

_EMPTY_SID_MESSAGE = "'source_id' é obrigatório (veja iped_list_sources)."

# Regex do engine compiladas uma vez (literais em knowledge.py, regra 8).
_RE_STRIP_EMAIL = re.compile(knowledge.OWNER_RE_STRIP_EMAIL)
_RE_STRIP_CPF = re.compile(knowledge.OWNER_RE_STRIP_CPF)
_RE_STRIP_CNPJ = re.compile(knowledge.OWNER_RE_STRIP_CNPJ)
_RE_STRIP_PHONE = re.compile(knowledge.OWNER_RE_STRIP_PHONE)
_RE_STRIP_LONG_DIGITS = re.compile(knowledge.OWNER_RE_STRIP_LONG_DIGITS)
_RE_KEEP_CHARS = re.compile(knowledge.OWNER_RE_KEEP_CHARS)
_RE_WHITESPACE = re.compile(knowledge.OWNER_RE_WHITESPACE)
_RE_PACKAGE_PREFIX = re.compile(knowledge.OWNER_RE_PACKAGE_PREFIX)
_RE_HEX_ARTIFACT = re.compile(knowledge.OWNER_RE_HEX_ARTIFACT)
_RE_INSTITUTIONAL_ACRONYM = re.compile(knowledge.OWNER_RE_INSTITUTIONAL_ACRONYM)
_RE_WORD = re.compile(knowledge.OWNER_RE_WORD)
_RE_DIGIT = re.compile(knowledge.OWNER_RE_DIGIT)
_RE_NON_DIGIT = re.compile(knowledge.OWNER_RE_NON_DIGIT)
_RE_COMPACT = re.compile(knowledge.OWNER_RE_COMPACT_KEY)
_RE_SSID_HEX = re.compile(knowledge.OWNER_RE_SSID_HEX)
_RE_PATH_SEGMENT = re.compile(knowledge.OWNER_RE_PATH_SEGMENT)
_RE_TRANSACTION = tuple(
    re.compile(pattern) for pattern in knowledge.OWNER_TRANSACTION_ID_PATTERNS
)

# Campos de entidade financeira que alimentam o feed (chave, (base, cap),
# rótulo de fonte) — engine :196-221, na ordem do motor.
_FINANCIAL_FEED_FIELDS = (
    (knowledge.OWNER_FIELD_PAYER, knowledge.OWNER_W_PAYER,
     knowledge.OWNER_SRC_PAYER),
    (knowledge.OWNER_FIELD_PAYEE, knowledge.OWNER_W_PAYEE,
     knowledge.OWNER_SRC_PAYEE),
    (knowledge.OWNER_FIELD_BENEFICIARY, knowledge.OWNER_W_BENEFICIARY,
     knowledge.OWNER_SRC_BENEFICIARY),
    (knowledge.OWNER_FIELD_DESCRIPTION, knowledge.OWNER_W_CARD_DESC,
     knowledge.OWNER_SRC_CARD_DESC),
)

# Eixos-membro (axis, tool, builder) — §2.2; a ordem de EXECUÇÃO dos feeds
# segue o engine (:91-97): identity, financial, telephony, communication,
# geolocation (noop), media, path.
_AXIS_BUILDERS: tuple[tuple[str, str, Callable[[str], dict[str, Any]]], ...] = (
    ("identity", "triage_identity", lambda sid: build_triage_identity(sid)),
    ("financial", "triage_financial", lambda sid: build_triage_financial(sid)),
    ("media", "triage_media", lambda sid: build_triage_media(sid)),
    (
        "communication",
        "triage_communication",
        lambda sid: build_triage_communication(sid),
    ),
)


def _fold(text: str) -> str:
    """Minúsculas sem acentos, para checagem de palavras (engine :67-72).

    Args:
        text: texto bruto.

    Returns:
        Texto em NFD sem marcas de acento, em minúsculas.
    """
    return "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    ).lower()


def _folded_words(text: str) -> set[str]:
    """Palavras folded de ``text`` (engine :341/:405).

    Args:
        text: texto a tokenizar.

    Returns:
        Conjunto de palavras minúsculas sem acento.
    """
    return set(_RE_WORD.findall(_fold(text)))


@dataclass
class _OwnerCandidate:
    """Candidato acumulado do motor (espelho ``NameCandidate`` engine :76-81).

    Attributes:
        name: nome normalizado exibido (canonical com mais palavras).
        score: pontuação acumulada sob o cap corrente.
        cap: cap acumulativo (máximo entre fontes já fundidas).
        sources: rótulos de fonte que alimentaram o candidato.
        raw_evidence: strings cruas que geraram o candidato (ordem de vista).
    """

    name: str
    score: float = 0.0
    cap: float = 100.0
    sources: list[str] = field(default_factory=list)
    raw_evidence: list[str] = field(default_factory=list)


class _OwnerEngine:
    """Motor heurístico de nomes do ICARO portado (engine :83-459, sem LLM).

    Acumula candidatos por feed com pesos/caps literais (:151-275), funde
    variações por chave compacta (:299-327) e ranqueia com a penalidade
    finance-only (:117-121) e a confiança 60/35 (:449-454). Estado por
    instância (sem global mutável); o tool cria um engine por chamada.
    """

    def __init__(self) -> None:
        """Inicializa índices vazios (um engine por chamada)."""
        self.candidates: dict[str, _OwnerCandidate] = {}
        self._normalized_cache: dict[str, str] = {}
        self._compact_index: dict[str, str] = {}

    # --- heurísticas (engine :339-459) --------------------------------------

    def normalize_name(self, raw: str) -> str:
        """Limpa uma string crua até forma de nome (engine :366-386).

        Args:
            raw: texto bruto de um feed.

        Returns:
            Nome sem e-mails/CPF/CNPJ/telefones/IDs longos, pontuação
            residual e title case quando era todo maiúsculas.
        """
        if raw in self._normalized_cache:
            return self._normalized_cache[raw]
        cleaned = raw.strip()
        for pattern in (
            _RE_STRIP_EMAIL, _RE_STRIP_CPF, _RE_STRIP_CNPJ,
            _RE_STRIP_PHONE, _RE_STRIP_LONG_DIGITS,
        ):
            cleaned = pattern.sub("", cleaned)
        cleaned = _RE_KEEP_CHARS.sub("", cleaned)
        cleaned = _RE_WHITESPACE.sub(" ", cleaned).strip()
        if (
            cleaned
            and cleaned == cleaned.upper()
            and len(cleaned) > knowledge.OWNER_TITLE_CONVERT_MIN
        ):
            cleaned = cleaned.title()
        self._normalized_cache[raw] = cleaned
        return cleaned

    def looks_like_name(self, text: str) -> bool:
        """Forma plausível de nome de pessoa (engine :413-433).

        Args:
            text: texto a avaliar.

        Returns:
            False para ponto/dígitos (regras absolutas), texto curto demais
            sem palavra com 3+ letras, tudo-dígitos e IDs de transação.
        """
        if knowledge.OWNER_NAME_DOT_FORBIDDEN in text:
            return False
        words = text.split()
        if (
            len(words) < 2
            and len(text) < knowledge.OWNER_NAME_SHORT_LEN_MIN
        ):
            return False
        if not any(
            len(word) >= knowledge.OWNER_NAME_WORD_MIN_LEN for word in words
        ):
            return False
        if text.replace(" ", "").replace("-", "").isdigit():
            return False
        if _RE_DIGIT.search(text):
            return False
        return not self.is_transaction_id(text)

    def looks_technical(self, text: str) -> bool:
        """Artefato técnico (arquivo, pacote, hex/UUID) — engine :344-364.

        Args:
            text: texto a avaliar.

        Returns:
            True quando o texto é nome de arquivo, identificador com
            underscore, pacote reverse-domain ou hex longo contíguo.
        """
        lower = text.strip().lower()
        if (
            knowledge.OWNER_NAME_DOT_FORBIDDEN in lower
            and " " not in lower
        ):
            return True
        if "_" in lower:
            return True
        if _RE_PACKAGE_PREFIX.match(lower):
            return True
        return (
            " " not in lower
            and len(lower) >= knowledge.OWNER_TECH_TOKEN_MIN
            and bool(_RE_HEX_ARTIFACT.fullmatch(lower))
            and any(char.isdigit() for char in lower)
        )

    def is_transaction_id(self, text: str) -> bool:
        """ID de transação/merchant, não nome (engine :388-400).

        Args:
            text: texto a avaliar.

        Returns:
            True para os 5 padrões literais ou maioria de dígitos.
        """
        upper = text.strip().upper()
        if any(pattern.match(upper) for pattern in _RE_TRANSACTION):
            return True
        digits = sum(char.isdigit() for char in text)
        letters = sum(char.isalpha() for char in text)
        return (
            digits > letters
            and digits > knowledge.OWNER_ID_DIGITS_MIN
        )

    def is_institutional(self, name: str) -> bool:
        """Palavra institucional (por palavra folded) ou sigla (:402-411).

        Args:
            name: nome a avaliar.

        Returns:
            True quando alguma palavra pertence ao vocabulário institucional
            ou o nome inteiro é sigla de até 5 maiúsculas.
        """
        words = _folded_words(name)
        if words.intersection(knowledge.OWNER_INSTITUTIONAL_KEYWORDS):
            return True
        return bool(_RE_INSTITUTIONAL_ACRONYM.match(name))

    def has_non_person_term(self, text: str) -> bool:
        """Alguma palavra pertence ao vocabulário não-pessoa (:339-342).

        Args:
            text: texto a avaliar.

        Returns:
            True em caso de interseção com ``OWNER_NON_PERSON_TERMS``.
        """
        words = _folded_words(text)
        return bool(words.intersection(knowledge.OWNER_NON_PERSON_TERMS))

    def looks_like_device_model(self, text: str) -> bool:
        """Marca/modelo de aparelho em substring (engine :435-439).

        Args:
            text: segmento de path a avaliar.

        Returns:
            True quando alguma marca conhecida aparece (case-insensitive).
        """
        lower = text.lower()
        return any(term in lower for term in knowledge.OWNER_DEVICE_MODEL_TERMS)

    def is_likely_personal_wifi(self, ssid: str) -> bool:
        """SSID provavelmente pessoal (engine :441-447).

        Args:
            ssid: nome da rede Wi-Fi.

        Returns:
            False para SSIDs genéricos e hex de BSSID; True caso contrário.
        """
        lower = ssid.lower()
        if lower in knowledge.OWNER_GENERIC_SSIDS:
            return False
        squashed = lower.replace(":", "").replace("-", "")
        return not bool(_RE_SSID_HEX.match(squashed))

    # --- acúmulo/fusão (engine :281-337) -------------------------------------

    def _rejected(self, name: str) -> bool:
        """Vetos absolutos pós-normalização (engine :290-297).

        Args:
            name: nome já normalizado (sem ponto).

        Returns:
            True se técnico, institucional, ID de transação ou não-pessoa.
        """
        return (
            self.looks_technical(name)
            or self.is_institutional(name)
            or self.is_transaction_id(name)
            or self.has_non_person_term(name)
        )

    def _lookup(self, name: str, compact: str) -> _OwnerCandidate | None:
        """Busca candidato pelo nome exato ou pela chave compacta (:299-307).

        Args:
            name: nome normalizado.
            compact: chave compacta (sem espaço/ponto/hífen/underscore).

        Returns:
            Candidato existente ou ``None``.
        """
        existing = self.candidates.get(name)
        if existing is None:
            canonical = self._compact_index.get(compact)
            if canonical:
                existing = self.candidates.get(canonical)
        return existing

    def _rename(self, existing: _OwnerCandidate, name: str, compact: str) -> None:
        """Display = nome com mais palavras ("Titular Teste" > "TitularTeste").

        Args:
            existing: candidato fundido (mutado).
            name: novo nome (mais palavras) que passa a exibir.
            compact: chave compacta que passa a apontar para ``name``.

        Returns:
            Nada.
        """
        old = existing.name
        existing.name = name
        del self.candidates[old]
        self.candidates[name] = existing
        self._compact_index[compact] = name

    def _reinforce(
        self,
        existing: _OwnerCandidate,
        raw_name: str,
        name: str,
        base_score: float,
        source: str,
        cap: float,
        compact: str,
    ) -> None:
        """Funde reforço no candidato com cap acumulativo (:309-327).

        Args:
            existing: candidato acumulado (mutado).
            raw_name: string crua do feed (evidência bruta).
            name: nome normalizado desta vista.
            base_score: peso base do feed.
            source: rótulo de fonte.
            cap: cap do feed (o maior entre fontes vence).
            compact: chave compacta desta vista.

        Returns:
            Nada.
        """
        family = source.split(knowledge.OWNER_FAMILY_SEPARATOR)[0]
        from_family = sum(1 for s in existing.sources if family in s)
        if from_family < knowledge.OWNER_FAMILY_REINFORCE_MAX:
            existing.cap = max(existing.cap, cap)
            existing.score = min(existing.score + base_score, existing.cap)
        if source not in existing.sources:
            existing.sources.append(source)
        existing.raw_evidence.append(raw_name)
        if len(name.split()) > len(existing.name.split()):
            self._rename(existing, name, compact)

    def add_candidate(
        self, raw_name: str, base_score: float, source: str, cap: float = 100.0
    ) -> None:
        """Acumula um candidato com todos os vetos do engine (:281-297).

        Args:
            raw_name: string crua do feed (ex.: SSID, pagador, alias).
            base_score: peso base do feed (``OWNER_W_*``).
            source: rótulo de fonte (família = prefixo antes de ``:``).
            cap: cap do feed; o acumulativo é o máximo entre fontes.

        Returns:
            Nada.
        """
        name = self.normalize_name(raw_name)
        if not name or len(name) < knowledge.OWNER_MIN_NAME_LEN:
            return
        if knowledge.OWNER_NAME_DOT_FORBIDDEN in name:
            return
        if self._rejected(name):
            return
        compact = _RE_COMPACT.sub("", name).lower()
        existing = self._lookup(name, compact)
        if existing is not None:
            self._reinforce(
                existing, raw_name, name, base_score, source, cap, compact
            )
            return
        self.candidates[name] = _OwnerCandidate(
            name=name,
            score=min(base_score, cap),
            cap=cap,
            sources=[source],
            raw_evidence=[raw_name],
        )
        self._compact_index[compact] = name

    # --- feeds (engine :151-275) ---------------------------------------------

    def feed_identity(self, envelope: dict[str, Any] | None) -> None:
        """Alimenta o motor com o eixo identidade (engine :151-194).

        Args:
            envelope: envelope T-3 (bloco ``identity`` interno) ou ``None``.

        Returns:
            Nada.
        """
        identity = (envelope or {}).get(knowledge.OWNER_SECTION_IDENTITY) or {}
        name = str(identity.get(knowledge.NAME_FIELD) or "")
        if (
            len(name) >= knowledge.OWNER_ACCOUNT_NAME_MIN_LEN
            and name != knowledge.NOT_IDENTIFIED_NAME
        ):
            base, cap = knowledge.OWNER_W_ACCOUNT_MAIN
            self.add_candidate(
                name, base, knowledge.OWNER_SRC_ACCOUNT_MAIN, cap
            )
        msisdn = str(identity.get(knowledge.OWNER_FIELD_SIM_MSISDN) or "")
        if msisdn:
            base, cap = knowledge.OWNER_W_SIM_MSISDN
            self.add_candidate(
                knowledge.OWNER_SRC_SIM_LINE.format(msisdn=msisdn),
                base, knowledge.OWNER_SRC_SIM_MSISDN, cap,
            )
        self._feed_wifi_names(identity)
        self._feed_self_contact(identity, msisdn)
        self._feed_email_aliases(identity)

    def _feed_wifi_names(self, identity: dict[str, Any]) -> None:
        """Redes Wi-Fi pessoais — 12/15 (engine :173-176).

        Args:
            identity: bloco ``identity`` do envelope T-3.

        Returns:
            Nada.
        """
        for ssid in identity.get(knowledge.OWNER_FIELD_WIFI_NETWORKS) or []:
            if self.is_likely_personal_wifi(str(ssid)):
                base, cap = knowledge.OWNER_W_WIFI_PERSONAL
                self.add_candidate(
                    str(ssid), base,
                    knowledge.OWNER_SRC_WIFI.format(ssid=ssid), cap,
                )

    def _feed_self_contact(
        self, identity: dict[str, Any], msisdn: str
    ) -> None:
        """Contato salvo com o próprio número — 55/55 (engine :179-183).

        Divergências: a agenda completa não é publicada (consome a amostra
        ``known_contacts_sample`` do envelope T-3) e o eixo identidade já
        recusa o próprio número da agenda (identity.py:512-514) — no fluxo
        ao vivo o nome do dono chega de todo modo pelo feed 70/70; o feed
        é portado fiel ao engine (peso 55 é conhecimento ICARO) e vale
        sobre envelopes diretos (teste de paridade).

        Args:
            identity: bloco ``identity`` do envelope T-3.
            msisdn: número do SIM da identidade (dígitos comparados).

        Returns:
            Nada.
        """
        owner_digits = _RE_NON_DIGIT.sub("", msisdn)
        if not owner_digits:
            return
        for entry in identity.get(knowledge.OWNER_FIELD_CONTACTS_SAMPLE) or []:
            phone = str(entry.get(knowledge.OWNER_FIELD_PHONE) or "")
            contact = str(entry.get(knowledge.NAME_FIELD) or "")
            if (
                _RE_NON_DIGIT.sub("", phone) == owner_digits
                and contact != knowledge.DEFAULT_CONTACT_NAME
            ):
                base, cap = knowledge.OWNER_W_SELF_CONTACT
                self.add_candidate(
                    contact, base, knowledge.OWNER_SRC_SELF_CONTACT, cap
                )

    def _feed_email_aliases(self, identity: dict[str, Any]) -> None:
        """Alias de e-mail: local-part com './_' virando espaço — 15/15 (:186-194).

        Args:
            identity: bloco ``identity`` do envelope T-3.

        Returns:
            Nada.
        """
        for email in identity.get(knowledge.OWNER_FIELD_EMAILS) or []:
            email = str(email)
            if "@" not in email:
                continue
            alias = email.split("@")[0]
            for char in knowledge.OWNER_ALIAS_STRIP_CHARS:
                alias = alias.replace(char, knowledge.OWNER_ALIAS_REPLACEMENT)
            alias = alias.title()
            if self.looks_like_name(alias):
                base, cap = knowledge.OWNER_W_EMAIL_ALIAS
                self.add_candidate(
                    alias, base, knowledge.OWNER_SRC_EMAIL_ALIAS, cap
                )

    def _feed_financial_entity(self, entity: dict[str, Any]) -> None:
        """Uma entidade financeira: pagador/recebedor/favorecido/descrição.

        Args:
            entity: dict de evidência/cartão/conta do envelope T-4.

        Returns:
            Nada.
        """
        for key, (base, cap), source in _FINANCIAL_FEED_FIELDS:
            value = str(entity.get(key) or "")
            if value and self.looks_like_name(value):
                self.add_candidate(value, base, source, cap)

    def feed_financial(self, envelope: dict[str, Any] | None) -> None:
        """Alimenta o motor com o eixo financeiro (engine :196-221).

        Args:
            envelope: envelope T-4 (``evidences``/``cards``/``accounts``).

        Returns:
            Nada.
        """
        env = envelope or {}
        for section in (
            knowledge.OWNER_SECTION_EVIDENCES,
            knowledge.OWNER_SECTION_CARDS,
            knowledge.OWNER_SECTION_ACCOUNTS,
        ):
            for entity in env.get(section) or []:
                self._feed_financial_entity(entity)

    def feed_telephony(self, envelope: dict[str, Any] | None) -> None:
        """Contato recorrente com mais de 20 chamadas (engine :223-229).

        Paridade com o motor: telefonia NÃO é eixo-membro do orçamento
        composto (§2.2/§9.6), então o fluxo do tool passa ``None`` e o feed
        nunca contribui — portado porque o envelope T-1 existe e o peso é
        conhecimento codificado do ICARO (teste de paridade direto).

        Args:
            envelope: envelope T-1 (``top_contacts``) ou ``None``.

        Returns:
            Nada.
        """
        env = envelope or {}
        contacts = env.get(knowledge.OWNER_SECTION_TELEPHONY_CONTACTS) or []
        for record in contacts:
            name = str(record.get(knowledge.OWNER_FIELD_CONTACT_NAME) or "")
            calls = int(record.get(knowledge.OWNER_FIELD_TOTAL_CALLS) or 0)
            if (
                calls > knowledge.OWNER_TELEPHONY_CALLS_MIN
                and name
                and name != knowledge.DEFAULT_CONTACT_NAME
            ):
                base, cap = knowledge.OWNER_W_TELEPHONY
                self.add_candidate(
                    name, base, knowledge.OWNER_SRC_TELEPHONY, cap
                )

    def feed_communication(self, envelope: dict[str, Any] | None) -> None:
        """Interlocutor com mais de 100 mensagens — 5/5 (engine :231-238).

        O total do ICARO é o agregado do chat; no envelope T-8 ele vive em
        ``chat_total_messages``/``message_count`` — usa-se o máximo (mesma
        resolução de ``_score_record`` do próprio T-8).

        Args:
            envelope: envelope T-8 (``chats``) ou ``None``.

        Returns:
            Nada.
        """
        env = envelope or {}
        for chat in env.get(knowledge.OWNER_SECTION_CHATS) or []:
            total = max(
                int(chat.get(knowledge.OWNER_FIELD_MESSAGE_COUNT) or 0),
                int(chat.get(knowledge.OWNER_FIELD_CHAT_TOTAL) or 0),
            )
            who = str(chat.get(knowledge.OWNER_FIELD_INTERLOCUTOR) or "")
            if self._chat_rejected(who, total):
                continue
            base, cap = knowledge.OWNER_W_INTERLOCUTOR
            self.add_candidate(
                who, base, knowledge.OWNER_SRC_INTERLOCUTOR, cap
            )

    def _chat_rejected(self, who: str, total: int) -> bool:
        """Interlocutor fora do feed: total baixo, vazio, sentinela ou ID.

        Args:
            who: nome do interlocutor no chat.
            total: mensagens agregadas do chat.

        Returns:
            True quando o chat não contribui candidato.
        """
        if total <= knowledge.OWNER_COMMUNICATION_MSG_MIN:
            return True
        if not who or knowledge.DEFAULT_CONTACT_NAME in who:
            return True
        return self.is_transaction_id(who)

    def feed_media(self, envelope: dict[str, Any] | None) -> None:
        """Titulares vistos em mídia — 10/10 (engine :243-252, adaptado).

        Args:
            envelope: envelope T-6 (``owner_media_signals``) ou ``None``.

        Returns:
            Nada.
        """
        env = envelope or {}
        signals = env.get(knowledge.OWNER_SECTION_MEDIA_SIGNALS) or []
        for value in signals:
            value = str(value or "")
            if (
                len(value) > knowledge.OWNER_MEDIA_VALUE_MIN_LEN
                and self.looks_like_name(value)
            ):
                base, cap = knowledge.OWNER_W_MEDIA
                self.add_candidate(
                    value, base, knowledge.OWNER_SRC_MEDIA, cap
                )

    def _feed_one_path(self, path: str) -> bool:
        """Um path: no máximo um candidato, primeiro segmento válido.

        Args:
            path: caminho de um doc já varrido (fonte D-8).

        Returns:
            True quando algum segmento foi aceito (motor para de procurar).
        """
        for part in path.replace("\\", "/").split("/"):
            if len(part) <= knowledge.OWNER_PATH_PART_MIN_LEN:
                continue
            if not any(m in part for m in knowledge.OWNER_PATH_DASH_MARKERS):
                continue
            for segment in _RE_PATH_SEGMENT.split(part):
                segment = segment.strip()
                if len(segment) < knowledge.OWNER_PATH_SEGMENT_MIN_LEN:
                    continue
                if not self.looks_like_name(segment):
                    continue
                if self.looks_like_device_model(segment):
                    continue
                base, cap = knowledge.OWNER_W_PATH
                self.add_candidate(
                    segment, base,
                    knowledge.OWNER_SRC_PATH.format(seg=segment), cap,
                )
                return True
        return False

    def feed_paths(self, paths: list[str]) -> None:
        """Path do laudo (aqui: paths dos docs varridos, D-8) — 60/60.

        Args:
            paths: caminhos coletados dos envelopes-membro (ordem estável).

        Returns:
            Nada.
        """
        for path in paths:
            if self._feed_one_path(path):
                return

    # --- resultado (engine :110-132, :449-459) --------------------------------

    @staticmethod
    def score_to_confidence(score: float) -> str:
        """Confiança por score: 60+ ALTA, 35+ MÉDIA, senão BAIXA (:449-454).

        Args:
            score: pontuação do candidato.

        Returns:
            Um dos rótulos ``RELEVANCE_*`` (mesma família de rótulos MCP).
        """
        if score >= knowledge.OWNER_CONF_ALTA_MIN:
            return knowledge.RELEVANCE_ALTA
        if score >= knowledge.OWNER_CONF_MEDIA_MIN:
            return knowledge.RELEVANCE_MEDIA
        return knowledge.RELEVANCE_BAIXA

    @staticmethod
    def _evidence_text(candidate: _OwnerCandidate) -> str:
        """Texto de evidência do ``_build_evidence_text`` (engine :456-459).

        Args:
            candidate: candidato a descrever.

        Returns:
            "Nome 'X' eleito com score N. Fontes: a; b; c."
        """
        top = candidate.sources[: knowledge.OWNER_SOURCES_VIEW]
        return " ".join(
            (
                knowledge.OWNER_EVIDENCE_TEMPLATE.format(
                    name=candidate.name, score=candidate.score
                ),
                knowledge.OWNER_EVIDENCE_SOURCES_PREFIX
                + knowledge.OWNER_EVIDENCE_SOURCES_JOIN.join(top)
                + knowledge.OWNER_EVIDENCE_SOURCES_SUFFIX,
            )
        )

    @staticmethod
    def _to_public(candidate: _OwnerCandidate) -> dict[str, Any]:
        """Projeta o candidato no shape §9.6 (fontes com teto de visão).

        Args:
            candidate: candidato ranqueado.

        Returns:
            ``{name, score, confidence, sources[:3], evidence}``.
        """
        return {
            "name": candidate.name,
            "score": round(
                candidate.score, knowledge.OWNER_SCORE_ROUND_DIGITS
            ),
            "confidence": _OwnerEngine.score_to_confidence(candidate.score),
            "sources": list(
                candidate.sources[: knowledge.OWNER_SOURCES_VIEW]
            ),
            "evidence": _OwnerEngine._evidence_text(candidate),
        }

    @staticmethod
    def _apply_penalty(
        winner: _OwnerCandidate, penalty: dict[str, Any]
    ) -> bool:
        """Penalidade ×0.3 para vencedor vindo só de "Financeiro" (:117-121).

        Args:
            winner: candidato de maior score pré-ajuste.
            penalty: dict de resultado (mutado quando a penalidade aplica).

        Returns:
            True quando o score do vencedor foi reduzido.
        """
        if winner.score <= knowledge.OWNER_PENALTY_SCORE_MIN:
            return False
        marker = knowledge.OWNER_PENALTY_SOURCE_MARKER
        if not all(marker in source for source in winner.sources):
            return False
        winner.score *= knowledge.OWNER_PENALTY_FACTOR
        penalty["applied"] = True
        penalty["name"] = winner.name
        return True

    def rank(self) -> dict[str, Any]:
        """Ranqueia, aplica a penalidade e corta no teto (§9.5).

        Returns:
            ``{candidates: [:5 no shape §9.6], total: pré-teto, penalty:
            {applied, name}}`` — desempate pela ordem de inserção (sorted
            estável, como no engine :110).
        """
        ranked = sorted(
            self.candidates.values(), key=lambda c: c.score, reverse=True
        )
        penalty: dict[str, Any] = {"applied": False, "name": None}
        if ranked and self._apply_penalty(ranked[0], penalty):
            ranked = sorted(
                self.candidates.values(),
                key=lambda c: c.score,
                reverse=True,
            )
        return {
            "candidates": [
                self._to_public(c)
                for c in ranked[: knowledge.OWNER_CANDIDATES_MAX]
            ],
            "total": len(ranked),
            "penalty": penalty,
        }


# --- orquestração do tool (design §9.2/§9.6) -----------------------------------


def _collect_axis_envelopes(
    sid: str,
) -> tuple[dict[str, dict[str, Any] | None], list[str]]:
    """Executa os 4 eixos-membro por import (nenhuma query nova, §9.1).

    Eixo que falha (M-2 interno ou inesperado) vira ``None`` + warning — a
    ferramenta NÃO morre (§9.6); erros de roteamento já foram tratados
    antes, no ``resolve`` do build.

    Args:
        sid: source resolvido no Router.

    Returns:
        ``(envelopes por eixo, avisos de falha e dos membros)``.
    """
    envelopes: dict[str, dict[str, Any] | None] = {}
    warnings: list[str] = []
    for axis, tool, builder in _AXIS_BUILDERS:
        try:
            envelopes[axis] = builder(sid)
        except Exception as exc:  # §9.6: falha de eixo é warning explícito
            envelopes[axis] = None
            warnings.append(
                knowledge.OWNER_AXIS_FAILED_TEMPLATE.format(
                    axis=axis, error=exc
                )
            )
            continue
        for warning in envelopes[axis].get("warnings") or []:
            warnings.append(
                knowledge.OWNER_MEMBER_WARNING_PREFIX.format(tool=tool)
                + str(warning)
            )
    return envelopes, warnings


def _axis_envelope(
    envelopes: dict[str, dict[str, Any] | None], axis: str
) -> dict[str, Any]:
    """Envelope de um eixo, tolerando eixo indisponível (``None``).

    Args:
        envelopes: envelopes dos eixos-membro.
        axis: nome do eixo.

    Returns:
        O envelope do eixo ou ``{}``.
    """
    return envelopes.get(axis) or {}


def _section_list(
    envelopes: dict[str, dict[str, Any] | None], axis: str, key: str
) -> list[dict[str, Any]]:
    """Uma lista de entidades publicada por um eixo-membro.

    Args:
        envelopes: envelopes dos eixos-membro.
        axis: nome do eixo.
        key: chave da seção no envelope.

    Returns:
        A lista (vazia quando o eixo falhou ou a chave não existe).
    """
    return list(_axis_envelope(envelopes, axis).get(key) or [])


def _axis_paths(envelopes: dict[str, dict[str, Any] | None]) -> list[str]:
    """Coleta os paths publicados pelos membros (feed de path, D-8).

    Args:
        envelopes: envelopes dos eixos-membro (``None`` tolerado).

    Returns:
        Paths na ordem identity (principal + reconhecidos) → financial
        (evidências + contas) → media (itens ranqueados).
    """
    identity = _axis_envelope(envelopes, "identity").get(
        knowledge.OWNER_SECTION_IDENTITY
    ) or {}
    rows = [
        identity,
        *_section_list(envelopes, "identity",
                       knowledge.OWNER_SECTION_RECOGNIZED),
        *_section_list(envelopes, "financial",
                       knowledge.OWNER_SECTION_EVIDENCES),
        *_section_list(envelopes, "financial",
                       knowledge.OWNER_SECTION_ACCOUNTS),
        *_section_list(envelopes, "media",
                       knowledge.OWNER_SECTION_RANKED_ITEMS),
    ]
    paths: list[str] = []
    for row in rows:
        value = row.get(knowledge.PATH_FIELD)
        if value:
            paths.append(str(value))
    return paths


def _run_feeds(
    engine: _OwnerEngine, envelopes: dict[str, dict[str, Any] | None]
) -> None:
    """Executa os feeds na ordem do engine (:91-97), com noop de telefonia.

    Args:
        engine: motor vazio (mutado).
        envelopes: envelopes dos eixos-membro.

    Returns:
        Nada.
    """
    engine.feed_identity(envelopes.get("identity"))
    engine.feed_financial(envelopes.get("financial"))
    engine.feed_telephony(None)  # eixo não-membro — ver docstring do feed
    engine.feed_communication(envelopes.get("communication"))
    engine.feed_media(envelopes.get("media"))
    engine.feed_paths(_axis_paths(envelopes))


def _member_counter(
    envelopes: dict[str, dict[str, Any] | None], key: str
) -> dict[str, int]:
    """Um contador ``counters`` de cada membro, por eixo disponível.

    Args:
        envelopes: envelopes dos eixos-membro.
        key: chave do contador (ex.: ``http_calls``).

    Returns:
        ``{axis: valor}`` (0 para eixo indisponível ou sem a chave).
    """
    return {
        axis: int((envelopes.get(axis) or {}).get("counters", {}).get(key, 0))
        for axis, _tool, _builder in _AXIS_BUILDERS
    }


def _composite_counters(
    started: float, envelopes: dict[str, dict[str, Any] | None]
) -> tuple[dict[str, int], int, dict[str, Any]]:
    """Contadores compostos: por membro, soma de GETs e op_envelope somado.

    ``docs_searched``/``docs_fetched`` dos membros são SOMADOS nos contadores
    do envelope (§9.6 — o custo da montagem é a união dos eixos).

    Args:
        started: ``time.monotonic()`` do início da montagem.
        envelopes: envelopes dos eixos-membro.

    Returns:
        ``(member_http, http_calls, counters)`` já com member_http_calls,
        http_calls, http_budget e cached=False.
    """
    member_http = _member_counter(envelopes, "http_calls")
    http_calls = sum(member_http.values())
    counters = dict(
        op_envelope(
            started,
            scanned=sum(
                _member_counter(envelopes, "docs_searched").values()
            ),
            processed=sum(
                _member_counter(envelopes, "docs_fetched").values()
            ),
        )
    )
    counters.update(
        member_http_calls=member_http,
        http_calls=http_calls,
        http_budget=knowledge.OWNER_BUDGET,
        cached=False,
    )
    return member_http, http_calls, counters


def _truncated_block(
    envelopes: dict[str, dict[str, Any] | None],
    outcome: dict[str, Any],
    http_calls: int,
) -> dict[str, Any]:
    """Bloco ``truncated``: estouro composto + flags por membro + teto (§9.6).

    Args:
        envelopes: envelopes dos eixos-membro.
        outcome: resultado de ``_OwnerEngine.rank``.
        http_calls: GETs compostos da chamada.

    Returns:
        ``{http_budget_exceeded, http_budget_calls_at_stop, members,
        candidates_truncated}``.
    """
    members = {
        axis: bool(
            (envelopes.get(axis) or {})
            .get("truncated", {})
            .get("http_budget_exceeded", False)
        )
        for axis, _tool, _builder in _AXIS_BUILDERS
    }
    exceeded = any(members.values()) or http_calls > knowledge.OWNER_BUDGET
    return {
        "http_budget_exceeded": exceeded,
        "http_budget_calls_at_stop": http_calls if exceeded else None,
        "members": members,
        "candidates_truncated": (
            outcome["total"] > knowledge.OWNER_CANDIDATES_MAX
        ),
    }


def _payload(
    sid: str,
    started: float,
    envelopes: dict[str, dict[str, Any] | None],
    outcome: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Envelope final §9.6 (contadores compostos, tetos e vazio explícito).

    Args:
        sid: source triado.
        started: ``time.monotonic()`` do início.
        envelopes: envelopes dos eixos-membro.
        outcome: resultado de ``_OwnerEngine.rank``.
        warnings: avisos acumulados (falhas, membros, penalidade, orçamento).

    Returns:
        Envelope do tool com ``candidates`` [:5], ``total_candidates``,
        ``financial_only_penalty_applied``, ``per_axis_available``,
        ``counters`` (op_envelope + http_calls composto), ``truncated`` e
        ``warnings``; ``message`` quando não há candidato (§0.1).
    """
    member_http, http_calls, counters = _composite_counters(started, envelopes)
    truncated = _truncated_block(envelopes, outcome, http_calls)
    if truncated["http_budget_exceeded"] and (
        http_calls > knowledge.OWNER_BUDGET
    ):
        counters["phase"] = progress.PHASE_PARTIAL
        warnings.append(
            knowledge.OWNER_BUDGET_WARNING_TEMPLATE.format(
                used=http_calls, cap=knowledge.OWNER_BUDGET
            )
        )
    envelope: dict[str, Any] = {
        "source_id": sid,
        "candidates": outcome["candidates"],
        "total_candidates": outcome["total"],
        "financial_only_penalty_applied": outcome["penalty"],
        "per_axis_available": {
            axis: envelopes.get(axis) is not None
            for axis, _tool, _builder in _AXIS_BUILDERS
        },
        "counters": counters,
        "truncated": truncated,
        "warnings": warnings,
    }
    if not outcome["candidates"]:
        envelope["message"] = knowledge.OWNER_EMPTY_MESSAGE
    return envelope


def build_owner_candidates(source_id: str) -> dict[str, Any]:
    """Núcleo do T-9 (lógica testável sem MCP).

    Args:
        source_id: source a triar (erros M-2 viram ValueError via Router).

    Returns:
        Envelope de ``_payload`` (shape DESIGN 09-B §9.6).

    Raises:
        ValueError: source_id vazio, inexistente ou endpoint offline
            (textos estáveis da política M-2, antes de qualquer HTTP).
    """
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError(_EMPTY_SID_MESSAGE)
    get_router().resolve(sid)
    started = time.monotonic()
    envelopes, warnings = _collect_axis_envelopes(sid)
    engine = _OwnerEngine()
    _run_feeds(engine, envelopes)
    outcome = engine.rank()
    if outcome["penalty"]["applied"]:
        warnings.append(
            knowledge.OWNER_PENALTY_WARNING_TEMPLATE.format(
                name=outcome["penalty"]["name"],
                factor=knowledge.OWNER_PENALTY_FACTOR,
            )
        )
    return _payload(sid, started, envelopes, outcome, warnings)
