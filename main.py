import logging
import sys

from fastmcp import FastMCP

from iped_mcp import config
from iped_mcp.bookmarks import register as register_bookmarks
from iped_mcp.core import register as register_core
from iped_mcp.regex import register as register_regex
from iped_mcp.router import RouterStartupError, get_router
from iped_mcp.triage import register as register_triage
from iped_mcp.ufed import register as register_ufed
from iped_mcp.whatsapp import register as register_whatsapp

mcp = FastMCP(
    "iped_mcp",
    instructions=(
        "MCP para consultar dados telemáticos indexados no IPED. "
        "Comece com iped_list_sources para ver os sources disponíveis. "
        "Acesso genérico (família iped_): iped_search faz busca Lucene livre "
        "(devolve contagem + amostra de IDs), iped_doc_info mostra propriedades, "
        "iped_doc_text lê o texto (truncado), iped_doc_export/iped_thumb_export "
        "baixam arquivo bruto/thumbnail para o disco. "
        "Antes de refinar buscas, iped_case_summary faz o censo por categoria "
        "de um source (contagens e avisos; nenhum id de documento na resposta). "
        "Para atributos ufed: (IMEI, ICCID, etc.), use ufed_search ou ufed_device_info; "
        "ufed_list_fields mostra os campos válidos. "
        "Para atributos Regex:* (CNPJ, CPF, URL, etc.), comece com regex_list_types; "
        "regex_stats e regex_top_values dão totais e frequência, "
        "regex_value_docs investiga um valor e regex_export_csv exporta a tabela "
        "(passe output_dir com o caminho absoluto da pasta exports/ do seu projeto). "
        "Para gerenciar coleções de documentos (bookmarks/marcadores), use as tools "
        "bookmarks_*: bookmarks_list/bookmarks_docs listam, bookmarks_create/add/remove "
        "gerenciam, bookmarks_save_search salva uma busca, bookmarks_report amostra "
        "propriedades-chave, bookmarks_download baixa os arquivos e "
        "bookmarks_download_info exporta as informações estruturadas (propriedades "
        "completas + texto) para um JSON (lotes grandes de IDs entram via arquivo txt "
        "nas tools *_from_file). Todas as tools de bookmark "
        "recebem source_id (obrigatório) para rotear a operação. "
        "Para dados do WhatsApp (família whatsapp_), comece com whatsapp_overview "
        "(triagem: totais, período, top interlocutores) e whatsapp_account (dono); "
        "whatsapp_conversation/whatsapp_conversation_window analisam uma conversa "
        "privada (volume, intensidade, janela por tipo), whatsapp_export_conversation "
        "grava o JSON da conversa no disco, whatsapp_text_search busca um termo em "
        "mensagem/áudio/PDF e whatsapp_profile_photo baixa o avatar de um número. "
        "whatsapp_reference documenta os campos e queries canônicas validados. "
        "A família triage_ traz triagem pronta sem LLM: triage_telephony agrega "
        "chamadas/contatos por número e devolve o top contatos com score e "
        "relevância (tetos e truncamento sempre marcados na resposta); triage_identity traz a identidade principal (contas, e-mails, telefones, SIM/Wi-Fi/Bluetooth, device_info padronizado, agenda) com score por confiança mais os dispositivos reconhecidos pela conta de nuvem; triage_financial extrai evidências financeiras (valores/PIX/contas por detectores Regex), cartões com transações agregadas e contas financeiras com score e relevância; triage_geolocation agrupa coordenadas GPS em clusters com ocorrências, período e relevância (Null Island descartada; sem país — ferramenta externa ao contrato); triage_media ranqueia a mídia por 12 sinais (OCR, rosto, nudez, transcrito, CPF, telefone, URL, GPS, ext divergente, carved, deletado, download financeiro) com contagem por categoria, apps e titulares vistos em mídia; triage_devices_networks agrega logs de rede (conexões por IP com colapso IPv6 /64, serviços iCloud, fingerprints de aparelho), redes wifi e dispositivos bluetooth — somente agregados, nunca a timeline bruta; transcripts_generate grava o arquivo de transcrições de áudio (metadado audio:transcription) no disco de exports e devolve metadados + preview \" o texto completo nunca vai na resposta\""
    ),
)

register_core(mcp)
register_ufed(mcp)
register_regex(mcp)
register_bookmarks(mcp)
register_whatsapp(mcp)
register_triage(mcp)


def main() -> None:
    """Ponto de entrada do servidor: roteador, logging e execução do FastMCP.

    Returns:
        Nada.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        get_router()
    except RouterStartupError as exc:
        print(f"IPED MCP: {exc}", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="http", host=config.SERVER_HOST, port=config.SERVER_PORT)


if __name__ == "__main__":
    main()
