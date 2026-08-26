import os
import re
import json
import time
import math
import requests
import pandas as pd
import builtins
from datetime import datetime
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz
 
# Força flush imediato em todos os prints
_original_print = builtins.print
 
 
def print(*args, **kwargs):
    kwargs["flush"] = True
    _original_print(*args, **kwargs)
 
 
API_TIMEOUT = 60  # segundos
 
# ==============================
# CONFIGURAÇÕES
# ==============================
SOCIA_API_KEY = os.environ.get("SOCIAVAULT_API_KEY")
GEMINI_API_KEY_KC = os.environ.get("GEMINI_API_KEY_KC")
 
COMMENTS_LIMIT = 300
BATCH_SIZE = 20
POST_EXPIRY_DAYS = 14
 
# Endpoints SociaVault
POST_INFO_ENDPOINT = "https://api.sociavault.com/v1/scrape/instagram/post-info"
COMMENTS_ENDPOINT = "https://api.sociavault.com/v1/scrape/instagram/comments"
PROFILE_ENDPOINT = "https://api.sociavault.com/v1/scrape/instagram/profile"
 
# O endpoint /comments devolve ~15 comentários por página e custa 1 crédito por
# requisição. O teto de páginas é derivado do COMMENTS_LIMIT (com uma folga de 2
# páginas) para nunca queimar créditos além do necessário.
COMMENTS_PER_PAGE_ESTIMATE = 15
MAX_COMMENT_PAGES = math.ceil(COMMENTS_LIMIT / COMMENTS_PER_PAGE_ESTIMATE) + 2
 
# Spreadsheet IDs
SPREADSHEET_PROFILES_ID = "1PGajyPdI45WPENpWdRK3eYFFbaHIPiKvzZ3uVtErcTg"
SPREADSHEET_DATA_PROFILE_ID = "1R6b2vfc_UyFmsOuiZBm6Y5b824MQRnH4nh2eM__NgNo"
SPREADSHEET_DATA_COMMENTS_ID = "1MpdbGBD2YS2-J2tOTFqPW2tDwUzW-1H6QNblyxuJTgw"
 
# Sheet names
SHEET_PROFILES = "instagram_profile"
SHEET_DATA_PROFILE = "data_profile_post"
SHEET_DATA_PROFILE_MAX = "data_profile_post_max"
SHEET_DATA_COMMENTS = "data_comments_post"
 
tz_br = pytz.timezone("America/Sao_Paulo")
 
NUMERIC_COLS_MAX = [
    "followers_count", "following_count", "total_posts_count",
    "comment_count", "like_count", "play_count",
]
DATETIME_COLS_MAX = ["run_datetime", "taken_at", "first_extracted_at"]
DATETIME_OUT_FMT = "%Y-%m-%d %H:%M:%S"
 
 
# ==============================
# GOOGLE SERVICES
# ==============================
def get_google_services():
    creds_json = json.loads(os.environ.get("GDRIVE_CREDENTIALS_KC"))
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = service_account.Credentials.from_service_account_info(
        creds_json, scopes=scopes
    )
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    return drive_service, sheets_service
 
 
# ==============================
# ETAPA 1 — LER PERFIS E LINKS
# ==============================
def extract_shortcode_from_url(url):
    """
    Extrai o shortcode de uma URL no formato:
    https://www.instagram.com/p/SHORTCODE/
    Retorna None se a URL for inválida.
    """
    if not url or not isinstance(url, str):
        return None
    match = re.search(r"instagram\.com/(?:p|reels?)/([A-Za-z0-9_\-]+)", url.strip())
    if match:
        return match.group(1)
    return None
 
 
def parse_date(date_str):
    """
    Tenta converter a string de data da planilha para datetime com timezone.
    Suporta formatos comuns do Google Sheets.
    """
    if not date_str or not isinstance(date_str, str):
        return None
    formats = [
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return tz_br.localize(dt)
        except ValueError:
            continue
    return None
 
 
def is_post_expired_by_date(date_added):
    """Retorna True se o post foi adicionado há mais de 14 dias."""
    if not date_added:
        return False
    hoje = datetime.now(tz_br)
    return (hoje - date_added).days > POST_EXPIRY_DAYS
 
 
def read_profiles_and_links(sheets_service):
    """
    Lê a planilha instagram_profile e retorna lista de dicts com:
    - username, link_of_post, shortcode, date_added, plataform, country, type
    Loga erros para links inválidos e pula posts expirados.
    """
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_PROFILES_ID,
        range=f"{SHEET_PROFILES}!A:Z"
    ).execute()
    rows = result.get("values", [])
    if len(rows) <= 1:
        print("Nenhuma linha encontrada na planilha instagram_profile.")
        return []
 
    headers = [h.strip().lower() for h in rows[0]]
    print(f"Colunas encontradas: {headers}")
 
    # Mapeia colunas pelo nome (case-insensitive)
    col_map = {}
    for expected, variants in {
        "date": ["date"],
        "plataform": ["plataform", "platform"],
        "username": ["username"],
        "country": ["country"],
        "type": ["type"],
        "link_of_post": ["link of post", "link_of_post", "linkofpost"],
    }.items():
        for variant in variants:
            if variant in headers:
                col_map[expected] = headers.index(variant)
                break
 
    required = ["username", "link_of_post", "date"]
    for col in required:
        if col not in col_map:
            print(f"ERRO: Coluna obrigatória '{col}' não encontrada. Colunas disponíveis: {headers}")
            return []
 
    entries = []
    for i, row in enumerate(rows[1:], start=2):  # linha 2 em diante (1-indexed)
        def get_col(key, default=""):
            idx = col_map.get(key)
            if idx is None:
                return default
            return row[idx].strip() if len(row) > idx else default
 
        username = get_col("username")
        link = get_col("link_of_post")
        date_str = get_col("date")
        plataform = get_col("plataform", "Instagram")
        country = get_col("country")
        post_type = get_col("type")
 
        # Valida link
        shortcode = extract_shortcode_from_url(link)
        if not shortcode:
            print(f"  [LINHA {i}] Link inválido ou vazio para @{username}: '{link}' — pulando.")
            continue
 
        # Valida e parseia data
        date_added = parse_date(date_str)
        if not date_added:
            print(f"  [LINHA {i}] Data inválida para @{username} (link={link}): '{date_str}' — pulando.")
            continue
 
        # Verifica expiração
        if is_post_expired_by_date(date_added):
            print(f"  [LINHA {i}] Post expirado (>14 dias desde {date_str}) para @{username}: {link} — pulando.")
            continue
 
        entries.append({
            "username": username,
            "link_of_post": link,
            "shortcode": shortcode,
            "date_added": date_added,
            "plataform": plataform,
            "country": country,
            "type": post_type,
        })
 
    print(f"\n{len(entries)} post(s) válido(s) e dentro do prazo encontrado(s).")
    return entries
 
 
# ==============================
# ETAPA 2 — PERFIL
# ==============================
def fetch_profile(handle):
    headers = {"X-API-Key": SOCIA_API_KEY}
    params = {"handle": handle}
    response = requests.get(PROFILE_ENDPOINT, headers=headers, params=params, timeout=API_TIMEOUT)
    print(f"  Status profile ({handle}): {response.status_code}")
    response.raise_for_status()
    data = response.json()
    user = (
        data.get("data", {})
            .get("data", {})
            .get("user")
        or data.get("user")
        or {}
    )
    return {
        "username": user.get("username", handle),
        "followers_count": user.get("edge_followed_by", {}).get("count", ""),
        "following_count": user.get("edge_follow", {}).get("count", ""),
        "total_posts_count": user.get("edge_owner_to_timeline_media", {}).get("count", "")
    }
 
 
# ==============================
# ETAPA 3 — POST INFO (caption + métricas)
# ==============================
# Mapeamento de __typename para label legível
MEDIA_TYPE_MAP = {
    "XDTGraphImage": "Image",
    "XDTGraphVideo": "Video",
    "XDTGraphSidecar": "Carousel",
}
 
 
def build_post_url(shortcode):
    return f"https://www.instagram.com/p/{shortcode}/"
 
 
def fetch_post_info(shortcode):
    """
    Chama o endpoint /post-info UMA única vez e retorna:
    - caption (str): texto de descrição do post
    - post_meta (dict): métricas e metadados do post
 
    IMPORTANTE: este endpoint NÃO pagina comentários. Ele devolve apenas uma
    amostra dos ~12-15 primeiros comentários embutida no payload e ignora o
    parâmetro `cursor`. Os comentários são buscados por fetch_comments(), que
    usa o endpoint dedicado /comments.
    """
    headers = {"X-API-Key": SOCIA_API_KEY}
    params = {"url": build_post_url(shortcode)}
 
    response = requests.get(POST_INFO_ENDPOINT, params=params,
                            headers=headers, timeout=API_TIMEOUT)
    print(f"  Status post-info ({shortcode}): {response.status_code}")
    response.raise_for_status()
    data = response.json()
 
    media = (
        data.get("data", {})
            .get("data", {})
            .get("xdt_shortcode_media", {})
    ) or {}
 
    # Caption
    caption_edges = media.get("edge_media_to_caption", {}).get("edges", {})
    if isinstance(caption_edges, dict):
        first = caption_edges.get("0", {})
    elif isinstance(caption_edges, list) and len(caption_edges) > 0:
        first = caption_edges[0]
    else:
        first = {}
    caption = first.get("node", {}).get("text", "")
 
    # Métricas e metadados do post
    typename = media.get("__typename", "")
    taken_at_raw = media.get("taken_at_timestamp")
    taken_at = (
        datetime.fromtimestamp(taken_at_raw, tz=tz_br).strftime(DATETIME_OUT_FMT)
        if taken_at_raw else ""
    )
    owner = media.get("owner", {}) or {}
    username_shared = owner.get("username", "")
    like_count = media.get("edge_media_preview_like", {}).get("count", "")
    # ATENÇÃO: este contador inclui respostas e comentários ocultos, enquanto o
    # endpoint /comments devolve apenas comentários de primeiro nível visíveis.
    # A diferença entre os dois números é esperada, não é falha de coleta.
    comment_count = media.get("edge_media_preview_comment", {}).get("count", "")
    play_count = media.get("video_play_count", "")
    preview_image_url = media.get("thumbnail_src", "") or media.get("display_url", "")
 
    display_resources = media.get("display_resources", {})
    first_frame_url = ""
    if isinstance(display_resources, dict) and display_resources:
        last_key = str(max(int(k) for k in display_resources.keys()))
        first_frame_url = display_resources.get(last_key, {}).get("src", "")
    elif isinstance(display_resources, list) and display_resources:
        first_frame_url = display_resources[-1].get("src", "")
 
    post_meta = {
        "username_shared": username_shared,
        "taken_at": taken_at,
        "media_type": MEDIA_TYPE_MAP.get(typename, typename),
        "like_count": like_count,
        "comment_count": comment_count,
        "play_count": play_count,
        "preview_image_url": preview_image_url,
        "first_frame_url": first_frame_url,
    }
 
    return caption, post_meta
 
 
# ==============================
# ETAPA 4 — COMENTÁRIOS (endpoint dedicado, com paginação real)
# ==============================
def _nodes_from_comments_field(field):
    """
    A API pode devolver `comments` como dict indexado ({"0": {...}, "1": {...}})
    ou como lista. Normaliza para lista, preservando a ordem numérica das chaves.
    """
    if isinstance(field, dict):
        try:
            keys = sorted(field.keys(), key=lambda k: int(k))
        except (ValueError, TypeError):
            keys = list(field.keys())
        return [field[k] for k in keys if isinstance(field[k], dict)]
    if isinstance(field, list):
        return [item for item in field if isinstance(item, dict)]
    return []
 
 
def _unwrap_comments_payload(body):
    """
    Desembrulha o corpo da resposta até achar o nível que contém 'comments'.
    Tolera tanto {"data": {"comments": ...}} quanto {"data": {"data": {...}}}.
    """
    payload = body.get("data", {}) or {}
    if "comments" not in payload and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return payload
 
 
def fetch_comments(shortcode):
    """
    Pagina o endpoint /v1/scrape/instagram/comments até esgotar os comentários,
    atingir COMMENTS_LIMIT ou MAX_COMMENT_PAGES.
 
    O token de paginação vem em data.cursor e deve ser repassado LITERALMENTE
    no parâmetro `cursor` — não parsear nem extrair subcampos do JSON interno.
    """
    headers = {"X-API-Key": SOCIA_API_KEY}
    post_url_param = build_post_url(shortcode)
 
    all_comments = []
    seen_ids = set()
    cursor = None
    page = 1
 
    while page <= MAX_COMMENT_PAGES:
        params = {"url": post_url_param}
        if cursor:
            params["cursor"] = cursor
 
        response = requests.get(COMMENTS_ENDPOINT, params=params,
                                headers=headers, timeout=API_TIMEOUT)
 
        # 404 durante a paginação = cursor expirado, trata como fim dos comentários
        if response.status_code == 404 and page > 1:
            print(f"    Página {page}: cursor expirado (404), encerrando paginação.")
            break
        response.raise_for_status()
 
        payload = _unwrap_comments_payload(response.json())
        nodes = _nodes_from_comments_field(payload.get("comments"))
 
        if not nodes:
            print(f"    Página {page}: nenhum comentário retornado, encerrando paginação.")
            break
 
        # Deduplica por id (a API pode repetir itens na fronteira das páginas)
        new_nodes = [n for n in nodes if str(n.get("id", "")) not in seen_ids]
        if not new_nodes:
            print(f"    Página {page}: todos os IDs já vistos, encerrando paginação.")
            break
 
        seen_ids.update(str(n.get("id", "")) for n in new_nodes)
        all_comments.extend(normalize_comments(new_nodes, page))
        print(f"    Página {page}: {len(new_nodes)} comentários (total acumulado: {len(all_comments)})")
 
        if len(all_comments) >= COMMENTS_LIMIT:
            print(f"    Limite de {COMMENTS_LIMIT} comentários atingido, encerrando paginação.")
            all_comments = all_comments[:COMMENTS_LIMIT]
            break
 
        next_cursor = payload.get("cursor")
        if not next_cursor:
            print(f"    Página {page}: sem cursor na resposta, fim dos comentários.")
            break
        if next_cursor == cursor:
            print(f"    Página {page}: cursor repetido, encerrando paginação.")
            break
 
        cursor = next_cursor
        page += 1
        time.sleep(1)
    else:
        print(f"    Limite de {MAX_COMMENT_PAGES} páginas atingido, encerrando paginação.")
 
    return all_comments
 
 
def normalize_comments(comment_nodes, page):
    comments = []
    for idx, node in enumerate(comment_nodes, start=1):
        node["_page"] = page
        node["_comment_number"] = idx
        node["_custom_comment_id"] = f"{page}_{idx}"
        comments.append(node)
    return comments
 
 
def normalize_created_at(value):
    """
    O endpoint /comments devolve created_at em ISO 8601
    (ex.: '2025-09-16T18:09:34.000Z'); o payload antigo do GraphQL devolvia
    epoch em segundos. Padroniza os dois para 'YYYY-MM-DD HH:MM:SS' em
    America/Sao_Paulo. Se quiser manter o valor cru, troque esta função por
    `return value`.
    """
    if value in (None, ""):
        return ""
    # epoch (int, float ou string numérica)
    try:
        epoch = float(value)
        return datetime.fromtimestamp(epoch, tz=tz_br).strftime(DATETIME_OUT_FMT)
    except (TypeError, ValueError):
        pass
    # ISO 8601
    try:
        dt = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(dt):
            return str(value)
        return dt.tz_convert(tz_br).strftime(DATETIME_OUT_FMT)
    except Exception:
        return str(value)
 
 
def has_emoji(text):
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002700-\U000027BF"
        "\U0001F900-\U0001F9FF"
        "\U00002600-\U000026FF"
        "]+",
        flags=re.UNICODE,
    )
    return bool(emoji_pattern.search(text))
 
 
def get_saved_comment_ids(sheets_service, post_url):
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_DATA_COMMENTS_ID,
            range=f"{SHEET_DATA_COMMENTS}!A:Z"
        ).execute()
        rows = result.get("values", [])
        if len(rows) <= 1:
            return set()
        headers = rows[0]
        if "id" not in headers or "post_url" not in headers:
            return set()
        id_col = headers.index("id")
        url_col = headers.index("post_url")
        saved_ids = set()
        for row in rows[1:]:
            if len(row) > max(id_col, url_col):
                if row[url_col].strip() == post_url.strip():
                    saved_ids.add(row[id_col].strip())
        print(f"    IDs já salvos para {post_url}: {len(saved_ids)}")
        return saved_ids
    except Exception as e:
        print(f"    Aviso ao ler data_comments: {e}")
        return set()
 
 
def comments_to_dataframe(comments, post_url, perfil, saved_ids):
    """
    Monta o DataFrame de comentários.
 
    O endpoint /comments devolve objetos "achatados"
    (id, text, created_at, user.{id,pk,username,is_verified,is_unpublished}),
    sem edge_liked_by / edge_threaded_comments. As leituras abaixo aceitam os
    dois formatos, então continuam funcionando se a API voltar ao shape do
    GraphQL.
    """
    rows = []
    skipped = 0
    for item in comments:
        comment_id = str(item.get("id", ""))
        if comment_id in saved_ids:
            skipped += 1
            continue
        user = item.get("owner") or item.get("user") or {}
 
        like_count = item.get("comment_like_count")
        if like_count is None:
            like_count = item.get("like_count")
        if like_count is None:
            like_count = (item.get("edge_liked_by") or {}).get("count", "")
 
        child_count = item.get("child_comment_count")
        if child_count is None:
            child_count = (item.get("edge_threaded_comments") or {}).get("count", 0)
 
        is_unpublished = user.get("is_unpublished")
        if is_unpublished is None:
            is_unpublished = item.get("is_restricted_pending")
 
        user_id = user.get("id") or user.get("pk") or ""
        user_pk = user.get("pk") or user.get("id") or ""
 
        rows.append({
            "post_url": post_url,
            "perfil": perfil,
            "Id Comentário": item.get("_custom_comment_id"),
            "id": comment_id,
            "text": item.get("text"),
            "comment_like_count": like_count,
            "child_comment_count": child_count,
            "created_at": normalize_created_at(item.get("created_at")),
            "user": json.dumps(user, ensure_ascii=False),
            "username": user.get("username"),
            "id_user": user_id,
            "is_unpublished": is_unpublished,
            "pk": user_pk,
            "is_verified": user.get("is_verified")
        })
 
    print(f"    Comentários novos: {len(rows)} | Já salvos (ignorados): {skipped}")
    if not rows:
        return pd.DataFrame()
 
    df = pd.DataFrame(rows)
    df = df.fillna("")
    df["text"] = df["text"].astype(str)
    df["Id Comentário"] = df["Id Comentário"].astype(str)
    df["text_debug"] = df["text"].apply(repr)
    df["tem_emoji"] = df["text"].apply(has_emoji)
    return df
 
 
# ==============================
# CLASSIFICAÇÃO GEMINI
# ==============================
def extrair_retry_seconds(error_message):
    match = re.search(r"retry in ([0-9.]+)s", str(error_message))
    if match:
        return float(match.group(1)) + 2
    return 60
 
 
def classificar_lote_comentarios(comentarios, tentativa=1, max_tentativas=2):
    client = genai.Client(api_key=GEMINI_API_KEY_KC)
    prompt = f"""
Você é um especialista em análise de sentimentos para redes sociais.
Sua tarefa é classificar comentários em 'promotor', 'neutro' ou 'detrator'.
 
REGRAS CRÍTICAS:
1. Se o comentário for claramente positivo, elogio, entusiasmo ou recomendação, classifique como 'promotor'.
2. Se houver qualquer reclamação, dúvida técnica, ironia ou crítica, classifique como 'detrator'.
3. Se o comentário for puramente informativo, ambíguo, irrelevante ao produto/marca, ou não expressar opinião clara (ex: apenas marcação de outro usuário, pergunta neutra sem tom negativo, comentário genérico tipo "ok"), classifique como 'neutro'.
4. Não force um comentário para 'promotor' ou 'detrator' apenas para evitar usar 'neutro' — use 'neutro' sempre que não houver sinal claro de sentimento positivo ou negativo.
 
Comentários para análise:
{json.dumps(comentarios, ensure_ascii=False)}
"""
    schema = {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "Id Comentário": {"type": "string"},
                        "sentimento_nps": {
                            "type": "string",
                            "enum": ["promotor", "neutro", "detrator"]
                        },
                        "justificativa": {"type": "string"}
                    },
                    "required": ["Id Comentário", "sentimento_nps", "justificativa"]
                }
            }
        },
        "required": ["results"]
    }
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=schema,
                temperature=0.1
            )
        )
        return json.loads(response.text)["results"]
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
            wait_seconds = extrair_retry_seconds(error_str)
            if tentativa <= max_tentativas:
                print(f"    Rate limit atingido. Aguardando {wait_seconds:.0f}s (tentativa {tentativa}/{max_tentativas})...")
                time.sleep(wait_seconds)
                return classificar_lote_comentarios(comentarios, tentativa=tentativa + 1, max_tentativas=max_tentativas)
            else:
                print(f"    Máximo de tentativas atingido para este lote.")
                raise
        raise
 
 
def classificar_dataframe(df):
    resultados = []
    print(f"    Classificando {len(df)} comentários...")
    for i in range(0, len(df), BATCH_SIZE):
        lote = df.iloc[i:i + BATCH_SIZE]
        comentarios_lote = [
            {"Id Comentário": row["Id Comentário"], "text": row["text"]}
            for _, row in lote.iterrows()
        ]
        try:
            classificados = classificar_lote_comentarios(comentarios_lote)
            resultados.extend(classificados)
            print(f"    Lote {i // BATCH_SIZE + 1} OK")
        except Exception as e:
            print(f"    Erro no lote {i // BATCH_SIZE + 1}: {e}")
            for item in comentarios_lote:
                resultados.append({
                    "Id Comentário": item["Id Comentário"],
                    "sentimento_nps": "FALHA_API",
                    "justificativa": str(e)
                })
        time.sleep(2)
 
    df_result = pd.DataFrame(resultados)
    df_result["Id Comentário"] = df_result["Id Comentário"].astype(str)
    df = df.drop(columns=["sentimento_nps", "justificativa"], errors="ignore")
    df = df.merge(df_result, on="Id Comentário", how="left")
    return df
 
 
# ==============================
# SALVAMENTO
# ==============================
def save_post_snapshot_to_sheets(sheets_service, post_entry, caption, post_meta, profile_data, run_datetime):
    """Salva snapshot do post (com todas as métricas) no data_profile_post."""
    row_data = {
        "run_datetime": run_datetime,
        "Plataform": post_entry.get("plataform", "Instagram"),
        "username": post_entry.get("username", ""),
        "username_shared": post_meta.get("username_shared", ""),
        "followers_count": profile_data.get("followers_count", ""),
        "following_count": profile_data.get("following_count", ""),
        "total_posts_count": profile_data.get("total_posts_count", ""),
        "code": post_entry.get("shortcode", ""),
        "taken_at": post_meta.get("taken_at", ""),
        "url": post_entry.get("link_of_post", ""),
        "media_type": post_meta.get("media_type", ""),
        "comment_count": post_meta.get("comment_count", ""),
        "like_count": post_meta.get("like_count", ""),
        "play_count": post_meta.get("play_count", ""),
        "preview_image_url": post_meta.get("preview_image_url", ""),
        "first_frame_url": post_meta.get("first_frame_url", ""),
        "post_caption": caption,
        "first_extracted_at": run_datetime,
    }
    df = pd.DataFrame([row_data])
    df = df.fillna("")
 
    existing_data = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
        range=f"{SHEET_DATA_PROFILE}!A:A"
    ).execute()
    existing_rows = existing_data.get("values", [])
 
    if not existing_rows:
        values = [df.columns.tolist()] + df.astype(str).values.tolist()
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
            range=f"{SHEET_DATA_PROFILE}!A1",
            valueInputOption="RAW",
            body={"values": values}
        ).execute()
        print(f"  data_profile_post: 1 linha inserida com cabeçalho.")
    else:
        append_values = df.astype(str).values.tolist()
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
            range=f"{SHEET_DATA_PROFILE}!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": append_values}
        ).execute()
        print(f"  data_profile_post: snapshot salvo ({run_datetime}).")
 
 
def update_data_profile_post_max(sheets_service):
    """
    Atualiza a aba 'data_profile_post_max', que mantém a versão mais recente
    de cada post (indexado por 'code').
 
    Lógica:
      1. Lê 'data_profile_post' e, para cada 'code', mantém apenas a linha
         com o maior 'run_datetime'.
      2. Compara essas linhas com 'data_profile_post_max' usando 'code' como índice.
      3. Se o 'code' já existe no _max: substitui a linha SOMENTE se o
         'run_datetime' da origem for maior que o do _max.
      4. Se o 'code' não existe no _max: adiciona como nova linha.
    """
    print("\n[POS-PROCESSO] Atualizando data_profile_post_max...")
 
    # --- 1. Lê data_profile_post ---
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
        range=f"{SHEET_DATA_PROFILE}!A:Z"
    ).execute()
    rows = resp.get("values", [])
    if len(rows) <= 1:
        print("  data_profile_post_max: origem vazia, nada a fazer.")
        return
 
    header = rows[0]
    ncols = len(header)
    data_rows = [(r + [""] * ncols)[:ncols] for r in rows[1:]]
    df_src = pd.DataFrame(data_rows, columns=header)
 
    if "code" not in df_src.columns or "run_datetime" not in df_src.columns:
        print("  data_profile_post_max: colunas 'code'/'run_datetime' ausentes na origem.")
        return
 
    # Remove linhas sem 'code' (linhas em branco / vazias)
    df_src = df_src[df_src["code"].astype(str).str.strip() != ""]
    if df_src.empty:
        print("  data_profile_post_max: nenhuma linha com 'code' válido na origem.")
        return
 
    # --- 2. Mantém, por 'code', apenas a linha com run_datetime mais recente ---
    df_src["_dt"] = pd.to_datetime(df_src["run_datetime"], errors="coerce")
    df_src = df_src.sort_values("_dt").drop_duplicates(subset="code", keep="last")
 
    # --- 3. Lê data_profile_post_max ---
    resp_max = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
        range=f"{SHEET_DATA_PROFILE_MAX}!A:Z"
    ).execute()
    rows_max = resp_max.get("values", [])
    if rows_max:
        header_max = rows_max[0]
        nmax = len(header_max)
        max_rows = [(r + [""] * nmax)[:nmax] for r in rows_max[1:]]
        df_max = pd.DataFrame(max_rows, columns=header_max)
    else:
        # aba vazia: inicializa com o mesmo cabeçalho da origem
        header_max = header
        df_max = pd.DataFrame(columns=header)
 
    df_max["_dt"] = pd.to_datetime(df_max.get("run_datetime"), errors="coerce")
 
    # Índice: code -> posição da linha no df_max (última ocorrência vence)
    max_by_code = {str(c): idx for idx, c in df_max["code"].items()}
 
    # --- 4. Aplica atualizações / inserções ---
    updated, appended = 0, 0
    for _, srow in df_src.iterrows():
        code = str(srow["code"])
        src_dt = srow["_dt"]
        if code in max_by_code:
            idx = max_by_code[code]
            max_dt = df_max.at[idx, "_dt"]
            # substitui apenas se a origem for mais recente
            if pd.notna(src_dt) and (pd.isna(max_dt) or src_dt > max_dt):
                for col in header:
                    if col in df_max.columns:
                        df_max.at[idx, col] = srow[col]
                df_max.at[idx, "_dt"] = src_dt
                updated += 1
        else:
            new_row = {col: srow.get(col, "") for col in df_max.columns if col != "_dt"}
            new_row["_dt"] = src_dt
            df_max = pd.concat([df_max, pd.DataFrame([new_row])], ignore_index=True)
            appended += 1
 
    # --- 5. Reescreve a aba inteira (o nº de linhas nunca diminui) ---
    out_cols = [c for c in df_max.columns if c != "_dt"]
    df_out = df_max[out_cols].fillna("").astype(str)
    values = [out_cols] + df_out.values.tolist()
 
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
        range=f"{SHEET_DATA_PROFILE_MAX}!A1",
        valueInputOption="RAW",
        body={"values": values}
    ).execute()
 
    print(f"  data_profile_post_max: {updated} linha(s) atualizada(s), "
          f"{appended} nova(s) adicionada(s). Total: {len(df_out)} linha(s).")
 
 
def _coerce_numeric_series(s):
    """
    Converte uma série para números.
    - Remove separadores de milhar e espaços.
    - Vazios/inválidos viram '' (célula em branco).
    - Retorna int quando o valor é inteiro; senão float.
    """
    def conv(v):
        raw = str(v).strip()
        if raw == "" or raw.lower() in ("nan", "none"):
            return ""
        cleaned = raw.replace(",", "").replace(" ", "")
        num = pd.to_numeric(cleaned, errors="coerce")
        if pd.isna(num):
            return ""  # não numérico -> deixa em branco
        if float(num).is_integer():
            return int(num)
        return float(num)
 
    return s.apply(conv)
 
 
def _coerce_datetime_series(s):
    """
    Converte uma série para datetime e devolve string padronizada
    (YYYY-MM-DD HH:MM:SS). Vazios/inválidos viram ''.
    """
    dt = pd.to_datetime(s, errors="coerce")
    return dt.apply(lambda x: "" if pd.isna(x) else x.strftime(DATETIME_OUT_FMT))
 
 
def normalize_data_profile_post_max_types(sheets_service):
    """
    Relê a versão FINAL de 'data_profile_post_max', normaliza os tipos das
    colunas e reescreve a aba usando USER_ENTERED, para que o Google Sheets
    grave números e datas de fato (e não texto).
 
      - Numéricas: followers_count, following_count, total_posts_count,
                   comment_count, like_count, play_count
      - Datas:     run_datetime, taken_at, first_extracted_at
    """
    print("\n[POS-PROCESSO] Normalizando tipos em data_profile_post_max...")
 
    resp = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
        range=f"{SHEET_DATA_PROFILE_MAX}!A:Z"
    ).execute()
    rows = resp.get("values", [])
    if len(rows) <= 1:
        print("  data_profile_post_max: vazia, nada a normalizar.")
        return
 
    header = rows[0]
    ncols = len(header)
    data_rows = [(r + [""] * ncols)[:ncols] for r in rows[1:]]
    df = pd.DataFrame(data_rows, columns=header)
 
    # Normaliza numéricos
    num_done = []
    for col in NUMERIC_COLS_MAX:
        if col in df.columns:
            df[col] = _coerce_numeric_series(df[col])
            num_done.append(col)
 
    # Normaliza datas
    dt_done = []
    for col in DATETIME_COLS_MAX:
        if col in df.columns:
            df[col] = _coerce_datetime_series(df[col])
            dt_done.append(col)
 
    # Preserva tipos: números como int/float, o resto como str
    def cell(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        return "" if v is None else str(v)
 
    values = [header] + [[cell(v) for v in row] for row in df.itertuples(index=False, name=None)]
 
    # USER_ENTERED faz o Sheets interpretar número/data em vez de texto
    sheets_service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_DATA_PROFILE_ID,
        range=f"{SHEET_DATA_PROFILE_MAX}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()
 
    print(f"  data_profile_post_max: tipos normalizados "
          f"(numéricos: {num_done}; datas: {dt_done}). "
          f"Total: {len(df)} linha(s).")
 
 
def save_comments_to_sheets(sheets_service, df):
    df = df.fillna("")
    existing_data = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_DATA_COMMENTS_ID,
        range=f"{SHEET_DATA_COMMENTS}!A:A"
    ).execute()
    existing_rows = existing_data.get("values", [])
 
    if not existing_rows:
        values = [df.columns.tolist()] + df.astype(str).values.tolist()
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_DATA_COMMENTS_ID,
            range=f"{SHEET_DATA_COMMENTS}!A1",
            valueInputOption="RAW",
            body={"values": values}
        ).execute()
        print(f"    data_comments: {len(df)} linhas inseridas com cabeçalho.")
    else:
        append_values = df.astype(str).values.tolist()
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_DATA_COMMENTS_ID,
            range=f"{SHEET_DATA_COMMENTS}!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": append_values}
        ).execute()
        print(f"    data_comments: {len(append_values)} linhas adicionadas.")
 
 
# ==============================
# EXECUÇÃO PRINCIPAL
# ==============================
def main():
    print("=" * 60)
    print("INICIANDO PIPELINE INSTAGRAM (MODO LINKS)")
    print("=" * 60)
 
    print("\n[CONFIG] Verificando variáveis de ambiente...")
    missing = []
    for var in ["SOCIAVAULT_API_KEY", "GEMINI_API_KEY_KC", "GDRIVE_CREDENTIALS_KC"]:
        val = os.environ.get(var)
        if not val:
            missing.append(var)
            print(f"  ERRO: {var} não encontrada!")
        else:
            print(f"  OK: {var} ({len(val)} chars)")
    if missing:
        print(f"\nVariáveis faltando: {missing}. Encerrando.")
        return
 
    print(f"\n[CONFIG] Comentários: limite {COMMENTS_LIMIT}, "
          f"máx. {MAX_COMMENT_PAGES} páginas/post (~{COMMENTS_PER_PAGE_ESTIMATE} por página, "
          f"1 crédito por página).")
 
    print("\n[CONFIG] Inicializando Google Services...")
    drive_service, sheets_service = get_google_services()
    print("  Google Services OK")
 
    # ETAPA 1 — Ler perfis e links
    print("\n[ETAPA 1] Lendo perfis e links de posts...")
    entries = read_profiles_and_links(sheets_service)
    if not entries:
        print("Nenhum post válido para processar. Encerrando.")
        return
 
    # ETAPA 2/3/4 — Para cada post: post-info, perfil, snapshot e comentários
    print(f"\n[ETAPA 3] Processando {len(entries)} post(s)...")
    for entry in entries:
        shortcode = entry["shortcode"]
        post_url = entry["link_of_post"]
        username = entry["username"]
        run_datetime = datetime.now(tz_br).strftime(DATETIME_OUT_FMT)
 
        print(f"\n{'=' * 60}")
        print(f"POST: {post_url} (@{username})")
        print(f"{'=' * 60}")
 
        try:
            # Busca caption e métricas do post.
            # O post-info retorna o "dono" real do post (post_meta['username_shared']),
            # que é o handle correto do Instagram — diferente do "username" da planilha,
            # que pode estar com typo, nome de exibição, espaços etc.
            caption, post_meta = fetch_post_info(shortcode)
 
            real_handle = (post_meta.get("username_shared") or "").strip()
            if real_handle:
                if real_handle != username:
                    print(f"  Aviso: username da planilha ('{username}') difere do "
                          f"handle real do post ('{real_handle}'). Usando o handle real.")
                handle_to_fetch = real_handle
            else:
                print(f"  Aviso: post-info não retornou o handle do dono do post. "
                      f"Usando o username da planilha ('{username}') como fallback.")
                handle_to_fetch = username
 
            # Busca dados do perfil usando o handle correto
            print(f"  Buscando perfil de @{handle_to_fetch}...")
            profile_data = fetch_profile(handle_to_fetch)
 
            if caption:
                print(f"  Caption: {caption[:100]}{'...' if len(caption) > 100 else ''}")
            else:
                print(f"  Caption: (vazia)")
 
            print(f"  username_shared: {post_meta.get('username_shared')} | "
                  f"media_type: {post_meta.get('media_type')} | "
                  f"likes: {post_meta.get('like_count')} | "
                  f"comments: {post_meta.get('comment_count')} | "
                  f"plays: {post_meta.get('play_count')}")
 
            # Salva snapshot do post ANTES de buscar comentários, para que uma
            # falha na coleta de comentários não faça perder as métricas do post.
            _, sheets_service = get_google_services()  # reconecta para evitar timeout
            save_post_snapshot_to_sheets(sheets_service, entry, caption, post_meta, profile_data, run_datetime)
 
        except Exception as e:
            print(f"  ERRO ao processar post {post_url}: {e}. Pulando.")
            continue
 
        # Comentários — em bloco próprio: se falhar, o snapshot acima já foi salvo.
        try:
            print(f"  Coletando comentários...")
            all_comments = fetch_comments(shortcode)
            print(f"  Total de comentários coletados: {len(all_comments)}")
 
            saved_ids = get_saved_comment_ids(sheets_service, post_url)
            comments_to_classify = all_comments[-COMMENTS_LIMIT:] if len(all_comments) > COMMENTS_LIMIT else all_comments
            df_comments = comments_to_dataframe(comments_to_classify, post_url, username, saved_ids)
 
            if df_comments.empty:
                print("  Nenhum comentário novo. Pulando classificação.")
            else:
                df_comments = classificar_dataframe(df_comments)
                df_comments["data_execucao"] = run_datetime
                save_comments_to_sheets(sheets_service, df_comments)
 
        except Exception as e:
            print(f"  ERRO ao coletar/salvar comentários de {post_url}: {e}. "
                  f"Snapshot do post foi preservado. Pulando.")
            continue
 
    try:
        _, sheets_service = get_google_services()  # reconecta para evitar timeout
        update_data_profile_post_max(sheets_service)
    except Exception as e:
        print(f"  ERRO ao atualizar data_profile_post_max: {e}")
 
    # PÓS-PROCESSO — normaliza os tipos da versão final de data_profile_post_max
    try:
        _, sheets_service = get_google_services()  # reconecta para evitar timeout
        normalize_data_profile_post_max_types(sheets_service)
    except Exception as e:
        print(f"  ERRO ao normalizar tipos de data_profile_post_max: {e}")
 
    print(f"\n{'=' * 60}")
    print("PIPELINE FINALIZADO COM SUCESSO")
    print(f"{'=' * 60}")
 
 
if __name__ == "__main__":
    main()
