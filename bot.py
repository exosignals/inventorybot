import schedule
import subprocess
import psycopg2
import psycopg2.extras
import psycopg2.pool
import os
import re
import time
import random
import threading
import unicodedata
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from flask import Flask

# ================== CONFIGURAÇÕES E CONSTANTES ==================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("NEON_DATABASE_URL")
ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}

class GameConstants:
    """Constantes centralizadas do jogo"""
    # Valores do jogo
    HP_MAX_DEFAULT = 40
    SP_MAX_DEFAULT = 40
    MAX_ATRIBUTOS = 20
    MAX_PERICIAS = 40
    REROLLS_DAILY = 3
    COOLDOWN = 1
    CACHE_DURATION = 300  # 5 minutos
    
    # Peso máximo por força
    PESO_MAX = {1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 30}
    
    # Listas de atributos e perícias
    ATRIBUTOS_LISTA = ["Força","Destreza","Constituição","Inteligência","Sabedoria","Carisma"]
    PERICIAS_LISTA = ["Percepção","Persuasão","Medicina","Furtividade","Intimidação","Investigação",
                      "Armas de fogo","Armas brancas","Sobrevivência","Cultura","Intuição","Tecnologia"]
    
    # Kits e seus bônus
    KIT_BONUS = {
        "kit basico": 1, "kit básico": 1, "basico": 1, "básico": 1,
        "kit intermediario": 2, "kit intermediário": 2, "intermediario": 2, "intermediário": 2,
        "kit avancado": 3, "kit avançado": 3, "avancado": 3, "avançado": 3,
    }
    
    # Traumas possíveis
    TRAUMAS = [
        "Hipervigilância: não consegue dormir sem vigiar todas as entradas.",
        "Tremor incontrolável nas mãos em situações de estresse.",
        "Mutismo temporário diante de sons altos.",
        "Ataques de pânico ao sentir cheiro de sangue.",
        "Flashbacks paralisantes ao ouvir gritos.",
        "Aversão a ambientes fechados (claustrofobia aguda).",
    ]
    
    # Mensagens padronizadas
    MESSAGES = {
        'need_start': "Você precisa usar /start primeiro!",
        'antispam': "⏳ Ei! Espere um instante antes de usar outro comando.",
        'admin_only': "❌ Apenas administradores podem usar este comando.",
        'player_not_found': "❌ Jogador não encontrado. Peça para a pessoa usar /start.",
        'internal_error': "❌ Ocorreu um erro interno. Tente novamente em alguns instantes.",
        'invalid_usage': "❌ Uso inválido do comando. Verifique a sintaxe.",
        'item_not_found': "❌ Item não encontrado.",
        'insufficient_quantity': "❌ Quantidade insuficiente.",
    }

# ================== LOGGING CONFIGURADO ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def log_user_action(user_id: int, action: str, details: str = ""):
    """Log seguro de ações do usuário"""
    logger.info(f"User {user_id} - {action} - {details}")

# ================== POOL DE CONEXÕES ==================
CONNECTION_POOL = None

def init_connection_pool():
    """Inicializa pool de conexões do banco"""
    global CONNECTION_POOL
    try:
        CONNECTION_POOL = psycopg2.pool.ThreadedConnectionPool(
            1, 20,  # min e max conexões
            DATABASE_URL,
            cursor_factory=psycopg2.extras.DictCursor
        )
        logger.info("✅ Pool de conexões inicializado")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar pool: {e}")
        raise

@contextmanager
def get_db_connection():
    """Context manager para conexões seguras"""
    if not CONNECTION_POOL:
        raise Exception("Pool de conexões não inicializado")
    conn = CONNECTION_POOL.getconn()
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        logger.error(f"Erro na operação do banco: {e}")
        raise
    finally:
        CONNECTION_POOL.putconn(conn)

# ================== VALIDAÇÃO DE ENTRADA ==================
def validate_user_input(text: str, max_length: int = 100) -> str:
    """Sanitiza entrada do usuário"""
    if not text:
        return ""
    # Remove caracteres perigosos e limita tamanho
    text = re.sub(r'[<>"\']', '', text.strip())
    return text[:max_length]

def safe_int_parse(value: str, min_val: int = 0, max_val: int = 100) -> int | None:
    """Parse seguro de inteiros com validação de range"""
    try:
        val = int(str(value).strip())
        return val if min_val <= val <= max_val else None
    except (ValueError, AttributeError, TypeError):
        return None

def safe_float_parse(value: str, min_val: float = 0.0, max_val: float = 100.0) -> float | None:
    """Parse seguro de floats com validação de range"""
    try:
        # Remove kg e converte vírgula para ponto
        clean_val = str(value).lower().replace("kg", "").replace(",", ".").strip()
        val = float(clean_val)
        return val if min_val <= val <= max_val else None
    except (ValueError, AttributeError, TypeError):
        return None

def normalize_text(texto: str) -> str:
    """Normaliza texto removendo acentos e convertendo para minúsculo"""
    if not texto:
        return ""
    texto = texto.lower()
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto)
                    if unicodedata.category(c) != 'Mn')
    return texto

# ================== SISTEMA DE CACHE ==================
@lru_cache(maxsize=128)
def get_catalog_cached(timestamp_bucket: int):
    """Cache do catálogo por buckets de tempo"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT nome, peso FROM catalogo ORDER BY nome COLLATE \"C\"")
            return c.fetchall()
    except Exception as e:
        logger.error(f"Erro ao carregar catálogo: {e}")
        return []

def get_catalog_with_cache():
    """Obtém catálogo com cache de 5 minutos"""
    bucket = int(time.time()) // GameConstants.CACHE_DURATION
    return get_catalog_cached(bucket)

# ================== TRATAMENTO DE ERROS ==================
def safe_command_wrapper(func):
    """Decorator para tratamento seguro de erros"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            user_id = update.effective_user.id if update.effective_user else 0
            command_name = func.__name__
            log_user_action(user_id, f"command_{command_name}", "started")
            result = await func(update, context)
            log_user_action(user_id, f"command_{command_name}", "completed")
            return result
        except Exception as e:
            user_id = update.effective_user.id if update.effective_user else 0
            logger.error(f"Erro no comando {func.__name__} (user {user_id}): {e}")
            try:
                await update.message.reply_text(GameConstants.MESSAGES['internal_error'])
            except:
                logger.error("Não foi possível enviar mensagem de erro ao usuário")
    return wrapper

# ================== HELPERS REUTILIZÁVEIS ==================
LAST_COMMAND = {}
EDIT_PENDING = {}
TRANSFER_PENDING = {}
ABANDON_PENDING = {}

def anti_spam(user_id: int) -> bool:
    """Controle de anti-spam"""
    now = time.time()
    if user_id in LAST_COMMAND and now - LAST_COMMAND[user_id] < GameConstants.COOLDOWN:
        return False
    LAST_COMMAND[user_id] = now
    return True

def mention(user) -> str:
    """Gera mention do usuário"""
    if not user:
        return "Jogador"
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Jogador"

def is_admin(uid: int) -> bool:
    """Verifica se usuário é admin"""
    return uid in ADMIN_IDS

# ================== FUNÇÕES AUXILIARES DO JOGO ==================
ATRIBUTOS_NORMAL = {normalize_text(a): a for a in GameConstants.ATRIBUTOS_LISTA}
PERICIAS_NORMAL = {normalize_text(p): p for p in GameConstants.PERICIAS_LISTA}

def peso_total(player: dict) -> float:
    """Calcula peso total do inventário"""
    if not player or not player.get('inventario'):
        return 0.0
    return sum(item['peso'] * item.get('quantidade', 1) for item in player['inventario'])

def penalidade(player: dict) -> bool:
    """Verifica se jogador está com sobrecarga"""
    return peso_total(player) > player.get("peso_max", 0)

def roll_dados(qtd: int = 4, lados: int = 6) -> list:
    """Rola dados"""
    return [random.randint(1, lados) for _ in range(qtd)]

def resultado_roll(valor_total: int) -> str:
    """Determina resultado da rolagem"""
    if valor_total <= 5:
        return "Fracasso crítico"
    elif valor_total <= 12:
        return "Fracasso"
    elif valor_total <= 19:
        return "Sucesso"
    else:
        return "Sucesso crítico"

# ================== FUNÇÕES DO BANCO DE DADOS ==================
def init_db():
    """Inicializa o banco de dados"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # Tabela de jogadores
            c.execute('''CREATE TABLE IF NOT EXISTS players (
                id BIGINT PRIMARY KEY,
                nome TEXT,
                username TEXT,
                peso_max INTEGER DEFAULT 0,
                hp INTEGER DEFAULT 40,
                hp_max INTEGER DEFAULT 40,
                sp INTEGER DEFAULT 40,
                sp_max INTEGER DEFAULT 40,
                rerolls INTEGER DEFAULT 3
            )''')
            # Outras tabelas
            c.execute('''CREATE TABLE IF NOT EXISTS usernames (
                username TEXT PRIMARY KEY,
                user_id BIGINT,
                first_name TEXT,
                last_seen BIGINT
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS atributos (
                player_id BIGINT,
                nome TEXT,
                valor INTEGER DEFAULT 0,
                PRIMARY KEY(player_id, nome)
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS pericias (
                player_id BIGINT,
                nome TEXT,
                valor INTEGER DEFAULT 0,
                PRIMARY KEY(player_id, nome)
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS inventario (
                player_id BIGINT,
                nome TEXT,
                peso REAL,
                quantidade INTEGER DEFAULT 1,
                PRIMARY KEY(player_id, nome)
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS catalogo (
                nome TEXT PRIMARY KEY,
                peso REAL
            )''')
            c.execute('''CREATE TABLE IF NOT EXISTS coma_bonus (
                target_id BIGINT PRIMARY KEY,
                bonus INTEGER DEFAULT 0
            )''')
            conn.commit()
            logger.info("✅ Banco de dados inicializado")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar banco: {e}")
        raise

def register_username(user_id: int, username: str | None, first_name: str | None):
    """Registra username do usuário"""
    if not username:
        return
    try:
        username = validate_user_input(username.lower(), 50)
        first_name = validate_user_input(first_name or '', 100)
        now = int(time.time())
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO usernames(username, user_id, first_name, last_seen)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT (username) DO UPDATE SET
                user_id=%s, first_name=%s, last_seen=%s''',
                (username, user_id, first_name, now, user_id, first_name, now))
            c.execute("UPDATE players SET username=%s WHERE id=%s", (username, user_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao registrar username: {e}")

def username_to_id(user_tag: str) -> int | None:
    """Converte username para ID"""
    if not user_tag:
        return None
    try:
        uname = user_tag[1:].lower() if user_tag.startswith('@') else user_tag.lower()
        uname = validate_user_input(uname, 50)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM usernames WHERE username=%s", (uname,))
            row = c.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Erro ao buscar username: {e}")
        return None

def get_player(uid: int) -> dict | None:
    """Obtém dados completos do jogador"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM players WHERE id=%s", (uid,))
            row = c.fetchone()
            if not row:
                return None
            
            player = {
                "id": row["id"],
                "nome": row["nome"],
                "username": row["username"],
                "peso_max": row["peso_max"],
                "hp": row["hp"],
                "hp_max": row.get("hp_max", GameConstants.HP_MAX_DEFAULT),
                "sp": row["sp"],
                "sp_max": row.get("sp_max", GameConstants.SP_MAX_DEFAULT),
                "rerolls": row["rerolls"],
                "atributos": {},
                "pericias": {},
                "inventario": []
            }
            
            # Carrega atributos
            c.execute("SELECT nome, valor FROM atributos WHERE player_id=%s", (uid,))
            for nome, valor in c.fetchall():
                player["atributos"][nome] = valor
            
            # Carrega perícias
            c.execute("SELECT nome, valor FROM pericias WHERE player_id=%s", (uid,))
            for nome, valor in c.fetchall():
                player["pericias"][nome] = valor
            
            # Carrega inventário
            c.execute("SELECT nome, peso, quantidade FROM inventario WHERE player_id=%s", (uid,))
            for nome, peso, qtd in c.fetchall():
                player["inventario"].append({"nome": nome, "peso": peso, "quantidade": qtd})
            
            return player
    except Exception as e:
        logger.error(f"Erro ao buscar jogador {uid}: {e}")
        return None

def create_player(uid: int, nome: str, username: str = None):
    """Cria novo jogador"""
    try:
        nome = validate_user_input(nome, 100)
        username = validate_user_input(username or '', 50) or None
        with get_db_connection() as conn:
            c = conn.cursor()
            # Cria jogador
            c.execute('''INSERT INTO players(id, nome, username, hp_max, sp_max)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (uid, nome, username, GameConstants.HP_MAX_DEFAULT, GameConstants.SP_MAX_DEFAULT))
            
            # Cria atributos
            for attr in GameConstants.ATRIBUTOS_LISTA:
                c.execute('''INSERT INTO atributos(player_id, nome, valor)
                    VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''', (uid, attr, 0))
            
            # Cria perícias
            for per in GameConstants.PERICIAS_LISTA:
                c.execute('''INSERT INTO pericias(player_id, nome, valor)
                    VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''', (uid, per, 0))
            
            conn.commit()
            log_user_action(uid, "player_created", f"nome: {nome}")
    except Exception as e:
        logger.error(f"Erro ao criar jogador {uid}: {e}")
        raise

def update_player_field(uid: int, field: str, value):
    """Atualiza campo do jogador"""
    try:
        allowed_fields = ['nome', 'username', 'peso_max', 'hp', 'hp_max', 'sp', 'sp_max', 'rerolls']
        if field not in allowed_fields:
            raise ValueError(f"Campo não permitido: {field}")
        with get_db_connection() as conn:
            c = conn.cursor()
            query = f"UPDATE players SET {field}=%s WHERE id=%s"
            c.execute(query, (value, uid))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao atualizar campo {field} do jogador {uid}: {e}")
        raise

def update_atributo(uid: int, nome: str, valor: int):
    """Atualiza atributo do jogador"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE atributos SET valor=%s WHERE player_id=%s AND nome=%s", (valor, uid, nome))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao atualizar atributo: {e}")

def update_pericia(uid: int, nome: str, valor: int):
    """Atualiza perícia do jogador"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("UPDATE pericias SET valor=%s WHERE player_id=%s AND nome=%s", (valor, uid, nome))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao atualizar perícia: {e}")

def get_catalog_item(nome: str) -> dict | None:
    """Busca item no catálogo"""
    try:
        nome = validate_user_input(nome, 100)
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT nome, peso FROM catalogo WHERE LOWER(nome)=LOWER(%s)", (nome,))
            row = c.fetchone()
            return {"nome": row[0], "peso": row[1]} if row else None
    except Exception as e:
        logger.error(f"Erro ao buscar item do catálogo: {e}")
        return None

def add_catalog_item(nome: str, peso: float):
    """Adiciona item ao catálogo"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO catalogo(nome,peso) VALUES(%s,%s) ON CONFLICT (nome) DO UPDATE SET peso=%s", 
                     (nome, peso, peso))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao adicionar item ao catálogo: {e}")

def del_catalog_item(nome: str) -> bool:
    """Remove item do catálogo"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM catalogo WHERE LOWER(nome)=LOWER(%s)", (nome,))
            deleted = c.rowcount
            conn.commit()
            return deleted > 0
    except Exception as e:
        logger.error(f"Erro ao remover item do catálogo: {e}")
        return False

def ensure_peso_max_by_forca(uid: int):
    """Atualiza peso máximo baseado na força"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT valor FROM atributos WHERE player_id=%s AND nome='Força'", (uid,))
            row = c.fetchone()
            if row:
                valor_forca = max(1, min(6, int(row[0])))
                novo_peso = GameConstants.PESO_MAX.get(valor_forca, 0)
                c.execute("UPDATE players SET peso_max=%s WHERE id=%s", (novo_peso, uid))
                conn.commit()
    except Exception as e:
        logger.error(f"Erro ao atualizar peso máximo: {e}")

def add_coma_bonus(target_id: int, delta: int):
    """Adiciona bônus para teste de coma"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO coma_bonus(target_id, bonus) VALUES(%s,0) ON CONFLICT (target_id) DO NOTHING", 
                     (target_id,))
            c.execute("UPDATE coma_bonus SET bonus = bonus + %s WHERE target_id=%s", (delta, target_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Erro ao adicionar bônus de coma: {e}")

def pop_coma_bonus(target_id: int) -> int:
    """Remove e retorna bônus de coma"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT bonus FROM coma_bonus WHERE target_id=%s", (target_id,))
            row = c.fetchone()
            bonus = row[0] if row else 0
            c.execute("DELETE FROM coma_bonus WHERE target_id=%s", (target_id,))
            conn.commit()
            return bonus
    except Exception as e:
        logger.error(f"Erro ao buscar bônus de coma: {e}")
        return 0

# ================== COMANDOS PRINCIPAIS ==================
@safe_command_wrapper
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    nome = validate_user_input(update.effective_user.first_name or "Jogador", 100)
    username = update.effective_user.username
    
    if not get_player(uid):
        create_player(uid, nome, username)
        register_username(uid, username, nome)
    
    await update.message.reply_text(
        f"\u200B\n 𐚁  𝗕𝗼𝗮𝘀 𝘃𝗶𝗻𝗱𝗮𝘀, {nome} ! \n\n"
        "Este bot gerencia seus Dados, Ficha, Inventário, Vida e Sanidade, "
        "além de diversos outros sistemas que você poderá explorar.\n\n"
        "Use o comando <b>/ficha</b> para visualizar sua ficha atual. "
        "Para editá-la, use o comando <b>/editarficha</b>.\n\n"
        "Outros comandos úteis: <b>/inventario</b>, <b>/itens</b>, <b>/dar</b>, "
        "<b>/cura</b>, <b>/terapia</b>, <b>/coma</b>, <b>/ajudar</b>.\n\n"
        " 𝗔𝗽𝗿𝗼𝘃𝗲𝗶𝘁𝗲!\n\u200B",
        parse_mode="HTML"
    )

@safe_command_wrapper
async def ficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /ficha"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    player = get_player(uid)
    
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return
    
    text = "\u200B\n 「  ཀ  𝗗𝗘𝗔𝗗𝗟𝗜𝗡𝗘, ficha.  」​\u200B\n\n ✦︎  𝗔𝘁𝗿𝗶𝗯𝘂𝘁𝗼𝘀  (20 Pontos)\n"
    
    for a in GameConstants.ATRIBUTOS_LISTA:
        val = player["atributos"].get(a, 0)
        text += f" — {a}﹕{val}\n"
    
    text += "\n ✦︎  𝗣𝗲𝗿𝗶𝗰𝗶𝗮𝘀  (40 Pontos)\n"
    
    for p in GameConstants.PERICIAS_LISTA:
        val = player["pericias"].get(p, 0)
        text += f" — {p}﹕{val}\n"
    
    text += f"\n 𖹭  𝗛𝗣  (Vida)  ▸  {player['hp']}\n 𖦹  𝗦𝗣  (Sanidade)  ▸  {player['sp']}\n"
    
    total_peso = peso_total(player)
    sobre = "  ⚠︎  Você está com <b>SOBRECARGA</b>!" if penalidade(player) else ""
    text += f"\n 𖠩  𝗣𝗲𝘀𝗼 𝗧𝗼𝘁𝗮𝗹 ﹕ {total_peso:.1f}/{player['peso_max']}{sobre}\n\n"
    
    text += "<blockquote>Para editar Atributos e Perícias, utilize o comando /editarficha.</blockquote>\n"
    text += "<blockquote>Para gerenciar seu Inventário, utilize o comando /inventario.</blockquote>\n\u200B"
    
    await update.message.reply_text(text, parse_mode="HTML")

@safe_command_wrapper
async def verficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /verficha - Admin ver ficha de outros"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    
    if not is_admin(uid):
        await update.message.reply_text(GameConstants.MESSAGES['admin_only'])
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /verficha @jogador")
        return
    
    user_tag = context.args[0]
    target_id = username_to_id(user_tag)
    if not target_id:
        await update.message.reply_text(GameConstants.MESSAGES['player_not_found'])
        return
    
    player = get_player(target_id)
    if not player:
        await update.message.reply_text("❌ Jogador não encontrado no sistema.")
        return
    
    text = f"\u200B\n 「  ཀ  𝗗𝗘𝗔𝗗𝗟𝗜𝗡𝗘, ficha de {player['nome']}.  」​\u200B\n\n ✦︎  𝗔𝘁𝗿𝗶𝗯𝘂𝘁𝗼𝘀  (20 Pontos)\n"
    
    for a in GameConstants.ATRIBUTOS_LISTA:
        val = player["atributos"].get(a, 0)
        text += f" — {a}﹕{val}\n"
    
    text += "\n ✦︎  𝗣𝗲𝗿𝗶𝗰𝗶𝗮𝘀  (40 Pontos)\n"
    
    for p in GameConstants.PERICIAS_LISTA:
        val = player["pericias"].get(p, 0)
        text += f" — {p}﹕{val}\n"
    
    text += f"\n 𖹭  𝗛𝗣  (Vida)  ▸  {player['hp']}\n 𖦹  𝗦𝗣  (Sanidade)  ▸  {player['sp']}\n"
    
    total_peso = peso_total(player)
    sobre = "  ⚠︎  Jogador está com <b>SOBRECARGA</b>!" if penalidade(player) else ""
    text += f"\n 𖠩  𝗣𝗲𝘀𝗼 𝗧𝗼𝘁𝗮𝗹 ﹕ {total_peso:.1f}/{player['peso_max']}{sobre}\n"
    
    text += f"\n📊 <b>Info Admin:</b>\n"
    text += f" — ID: {player['id']}\n"
    text += f" — Username: @{player['username'] or 'N/A'}\n"
    text += f" — Rerolls: {player['rerolls']}/3\n\u200B"
    
    await update.message.reply_text(text, parse_mode="HTML")

@safe_command_wrapper
async def inventario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /inventario"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    player = get_player(uid)
    
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return
    
    lines = [f"\u200B\n「 📦 」  Inventário de {player['nome']}\n"]
    
    if not player['inventario']:
        lines.append(" Vazio.")
    else:
        for i in sorted(player['inventario'], key=lambda x: x['nome'].lower()):
            lines.append(f" — {i['nome']} x{i['quantidade']} ({i['peso']:.2f} kg cada)")
    
    total_peso = peso_total(player)
    lines.append(f"\n  𝗣𝗲𝘀𝗼 𝗧𝗼𝘁𝗮𝗹﹕{total_peso:.1f}/{player['peso_max']} kg\n\u200B")
    
    if penalidade(player):
        excesso = total_peso - player['peso_max']
        lines.append(f" ⚠︎ {excesso:.1f} kg de <b>SOBRECARGA</b>!")
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

@safe_command_wrapper
async def itens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /itens - Lista catálogo"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    data = get_catalog_with_cache()
    
    if not data:
        await update.message.reply_text("\u200B\n ☰  Catálogo\nVazio. Use /additem Nome Peso para adicionar.\n\u200B")
        return
    
    lines = ["\u200B ☰  Catálogo de Itens\n"]
    for nome, peso in data:
        lines.append(f" — {nome} ({peso:.2f} kg)")
    
    await update.message.reply_text("\n".join(lines))

@safe_command_wrapper
async def additem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /additem - Admin adicionar item"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(GameConstants.MESSAGES['admin_only'])
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /additem NomeDoItem Peso\nEx.: /additem Escopeta 3,5")
        return
    
    peso_str = context.args[-1]
    nome = " ".join(context.args[:-1])
    peso = safe_float_parse(peso_str, 0.0, 100.0)
    
    if not peso:
        await update.message.reply_text("❌ Peso inválido. Use algo como 2,5")
        return
    
    add_catalog_item(nome, peso)
    await update.message.reply_text(f"✅ Item '{nome}' adicionado ao catálogo com {peso:.2f} kg.\n(Inventário de mestre é virtual e inesgotável.)")

@safe_command_wrapper
async def delitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /delitem - Admin remover item"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(GameConstants.MESSAGES['admin_only'])
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /delitem NomeDoItem")
        return
    
    nome = " ".join(context.args)
    ok = del_catalog_item(nome)
    
    if ok:
        await update.message.reply_text(f"🗑️ Item '{nome}' removido do catálogo.")
    else:
        await update.message.reply_text("❌ Item não encontrado no catálogo.")

@safe_command_wrapper
async def dar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /dar - Transferir itens"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /dar @jogador Nome do item xquantidade (opcional)")
        return

    uid_from = update.effective_user.id
    register_username(uid_from, update.effective_user.username, update.effective_user.first_name)

    user_tag = context.args[0]
    target_id = username_to_id(user_tag)
    if not target_id:
        await update.message.reply_text(GameConstants.MESSAGES['player_not_found'])
        return

    # Parse do item e quantidade
    qtd = 1
    tail = context.args[1:]
    if len(tail) >= 2 and tail[-2].lower() == 'x' and tail[-1].isdigit():
        qtd = int(tail[-1])
        item_input = " ".join(tail[:-2])
    elif len(tail) >= 1 and tail[-1].isdigit():
        qtd = int(tail[-1])
        item_input = " ".join(tail[:-1])
    else:
        item_input = " ".join(tail)

    if qtd < 1:
        await update.message.reply_text("❌ Quantidade inválida.")
        return

    # Verifica item no inventário ou catálogo
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT nome, peso, quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                     (uid_from, item_input))
            row = c.fetchone()

            if row:
                item_nome, item_peso, qtd_doador = row
                if qtd > qtd_doador:
                    await update.message.reply_text(f"❌ Quantidade indisponível. Você tem {qtd_doador}x '{item_nome}'.")
                    return
            else:
                if is_admin(uid_from):
                    item_info = get_catalog_item(item_input)
                    if not item_info:
                        await update.message.reply_text(f"❌ Item '{item_input}' não encontrado no catálogo.")
                        return
                    item_nome = item_info["nome"]
                    item_peso = item_info["peso"]
                else:
                    await update.message.reply_text(f"❌ Você não possui '{item_input}' no seu inventário.")
                    return

        # Verifica sobrecarga do alvo
        target_before = get_player(target_id)
        total_depois_target = peso_total(target_before) + item_peso * qtd
        if total_depois_target > target_before['peso_max']:
            excesso = total_depois_target - target_before['peso_max']
            await update.message.reply_text(
                f"⚠️ {target_before['nome']} ficaria com sobrecarga de {excesso:.1f} kg. Transferência cancelada."
            )
            return

        # Salva transferência pendente
        TRANSFER_PENDING[uid_from] = {
            "item": item_nome,
            "qtd": qtd,
            "doador": uid_from,
            "alvo": target_id
        }

        keyboard = [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data="confirm_dar"),
                InlineKeyboardButton("❌ Cancelar", callback_data="cancel_dar")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"{user_tag}, {update.effective_user.first_name} quer te dar {item_nome} x{qtd}.\n"
            "Aceita a transferência?",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Erro no comando dar: {e}")
        await update.message.reply_text(GameConstants.MESSAGES['internal_error'])

@safe_command_wrapper
async def abandonar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /abandonar - Abandonar itens"""
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /abandonar Nome do item")
        return

    uid = update.effective_user.id
    item_input = " ".join(context.args)

    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT nome, peso, quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                     (uid, item_input.lower()))
            row = c.fetchone()
            
            if not row:
                await update.message.reply_text(f"❌ Você não possui '{item_input}' no seu inventário.")
                return

            item_nome, item_peso, qtd = row

        keyboard = [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_abandonar:{uid}:{item_nome}"),
                InlineKeyboardButton("❌ Cancelar", callback_data="cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"⚠️ Você está prestes a abandonar '{item_nome}' x{qtd}. Confirma?",
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Erro no comando abandonar: {e}")
        await update.message.reply_text(GameConstants.MESSAGES['internal_error'])

@safe_command_wrapper
async def dano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /dano - Causar dano"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /dano hp|sp [@jogador]")
        return
    
    tipo = context.args[0].lower()
    if tipo not in ("hp", "sp", "vida", "sanidade"):
        await update.message.reply_text("Tipo inválido! Use hp/vida ou sp/sanidade.")
        return
    
    alvo_id = uid
    alvo_tag = mention(update.effective_user)
    
    if len(context.args) >= 2:
        maybe_user = context.args[1]
        t = username_to_id(maybe_user)
        if t:
            alvo_id = t
            alvo_tag = maybe_user

    player = get_player(alvo_id)
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['player_not_found'])
        return

    dado = random.randint(1, 6)
    
    if tipo in ("hp", "vida"):
        before = player['hp']
        after = max(0, before - dado)
        update_player_field(alvo_id, 'hp', after)
        msg = (
            f"🎲 {mention(update.effective_user)} causou dano em {alvo_tag}!\n"
            f"Rolagem: 1d6 → {dado}\n"
            f"{player['nome']}: HP {before} → {after}"
        )
        if after == 0:
            msg += "\n💀 Entrou em coma! Use /coma."
        await update.message.reply_text(msg)
    else:
        before = player['sp']
        after = max(0, before - dado)
        update_player_field(alvo_id, 'sp', after)
        msg = (
            f"🎲 {mention(update.effective_user)} causou dano mental em {alvo_tag}!\n"
            f"Rolagem: 1d6 → {dado}\n"
            f"{player['nome']}: SP {before} → {after}"
        )
        if after == 0:
            trauma = random.choice(GameConstants.TRAUMAS)
            msg += f"\n😵 Trauma severo! {trauma}"
        await update.message.reply_text(msg)

@safe_command_wrapper
async def autodano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /autodano - Autolesão"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /autodano hp|sp")
        return
    
    tipo = context.args[0].lower()
    if tipo not in ("hp", "sp", "vida", "sanidade"):
        await update.message.reply_text("Tipo inválido! Use hp/vida ou sp/sanidade.")
        return

    player = get_player(uid)
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return

    dado = random.randint(1, 6)
    
    if tipo in ("hp", "vida"):
        before = player['hp']
        after = max(0, before - dado)
        update_player_field(uid, 'hp', after)
        msg = (
            f"🎲 {mention(update.effective_user)} se autoinfligiu dano!\n"
            f"Rolagem: 1d6 → {dado}\n"
            f"HP: {before} → {after}"
        )
        if after == 0:
            msg += "\n💀 Você entrou em coma! Use /coma."
        await update.message.reply_text(msg)
    else:
        before = player['sp']
        after = max(0, before - dado)
        update_player_field(uid, 'sp', after)
        msg = (
            f"🎲 {mention(update.effective_user)} se autoinfligiu dano mental!\n"
            f"Rolagem: 1d6 → {dado}\n"
            f"SP: {before} → {after}"
        )
        if after == 0:
            trauma = random.choice(GameConstants.TRAUMAS)
            msg += f"\n😵 Trauma severo! {trauma}"
        await update.message.reply_text(msg)

@safe_command_wrapper
async def cura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /cura - Curar outros"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /cura @jogador NomeDoKit")
        return
    
    alvo_tag = context.args[0]
    alvo_id = username_to_id(alvo_tag)
    if not alvo_id:
        await update.message.reply_text(GameConstants.MESSAGES['player_not_found'])
        return

    kit_nome = " ".join(context.args[1:]).strip()
    key = kit_nome.lower()
    bonus_kit = GameConstants.KIT_BONUS.get(key)
    
    if bonus_kit is None:
        await update.message.reply_text("❌ Kit inválido. Use: Kit Básico, Kit Intermediário ou Kit Avançado.")
        return

    healer = get_player(uid)
    cat = get_catalog_item(kit_nome)
    inv_nome = cat['nome'] if cat else kit_nome
    
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT quantidade,peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", 
                     (uid, inv_nome))
            row = c.fetchone()
            
            if not row or row[0] <= 0:
                await update.message.reply_text(f"❌ Você não possui '{kit_nome}' no inventário.")
                return
            
            nova = row[0] - 1
            if nova <= 0:
                c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
            else:
                c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", 
                         (nova, uid, inv_nome))
            conn.commit()

        dado = random.randint(1, 6)
        bonus_med = healer['pericias'].get('Medicina', 0)
        total = dado + bonus_kit + bonus_med

        alvo = get_player(alvo_id)
        before = alvo['hp']
        after = min(alvo['hp_max'], before + total)
        update_player_field(alvo_id, 'hp', after)

        msg = (
            f"🎲 {mention(update.effective_user)} usou {kit_nome} em {alvo_tag}!\n"
            f"Rolagem: 1d6 → {dado}\n"
            f"💊 Kit usado: {kit_nome} (+{bonus_kit})\n"
            f"🏥 Bônus de Medicina: +{bonus_med}\n"
            f"Total: {total}\n\n"
            f"{alvo['nome']}: HP {before} → {after}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Erro no comando cura: {e}")
        await update.message.reply_text(GameConstants.MESSAGES['internal_error'])

@safe_command_wrapper
async def autocura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /autocura - Se curar"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /autocura NomeDoKit")
        return

    kit_nome = " ".join(context.args).strip()
    key = kit_nome.lower()
    bonus_kit = GameConstants.KIT_BONUS.get(key)
    
    if bonus_kit is None:
        await update.message.reply_text("❌ Kit inválido. Use: Kit Básico, Kit Intermediário ou Kit Avançado.")
        return

    player = get_player(uid)
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return

    cat = get_catalog_item(kit_nome)
    inv_nome = cat['nome'] if cat else kit_nome
    
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT quantidade,peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", 
                     (uid, inv_nome))
            row = c.fetchone()
            
            if not row or row[0] <= 0:
                await update.message.reply_text(f"❌ Você não possui '{kit_nome}' no inventário.")
                return
            
            nova = row[0] - 1
            if nova <= 0:
                c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
            else:
                c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", 
                         (nova, uid, inv_nome))
            conn.commit()

        dado = random.randint(1, 6)
        bonus_med = player['pericias'].get('Medicina', 0)
        total = dado + bonus_kit + bonus_med

        before = player['hp']
        after = min(player['hp_max'], before + total)
        update_player_field(uid, 'hp', after)

        msg = (
            f"🎲 {mention(update.effective_user)} se autocurou usando {kit_nome}!\n"
            f"Rolagem: 1d6 → {dado}\n"
            f"💊 Kit usado: {kit_nome} (+{bonus_kit})\n"
            f"🏥 Bônus de Medicina: +{bonus_med}\n"
            f"Total: {total}\n\n"
            f"HP: {before} → {after}"
        )
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Erro no comando autocura: {e}")
        await update.message.reply_text(GameConstants.MESSAGES['internal_error'])

@safe_command_wrapper
async def terapia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /terapia - Terapia mental"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /terapia @jogador")
        return
    
    alvo_tag = context.args[0]
    alvo_id = username_to_id(alvo_tag)
    if not alvo_id:
        await update.message.reply_text(GameConstants.MESSAGES['player_not_found'])
        return
    
    if alvo_id == uid:
        await update.message.reply_text("❌ Terapia só pode ser aplicada em outra pessoa.")
        return

    healer = get_player(uid)
    bonus_pers = healer['pericias'].get('Persuasão', 0)
    dado = random.randint(1, 6)
    total = dado + bonus_pers

    alvo = get_player(alvo_id)
    before = alvo['sp']
    after = min(alvo['sp_max'], before + total)
    update_player_field(alvo_id, 'sp', after)

    msg = (
        f"🎲 {mention(update.effective_user)} aplicou uma sessão de terapia em {alvo_tag}!\n"
        f"Rolagem: 1d6 → {dado}\n"
        f"Bônus: +{bonus_pers} (Persuasão)\n"
        f"Total: {total}\n\n"
        f"{alvo['nome']}: SP {before} → {after}"
    )
    await update.message.reply_text(msg)

@safe_command_wrapper
async def coma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /coma - Teste de coma"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return

    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    player = get_player(uid)
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return
    
    if player['hp'] > 0:
        await update.message.reply_text("❌ Você não está em coma (HP > 0).")
        return

    dados = roll_dados(4, 6)
    soma = sum(dados)
    bonus_ajuda = pop_coma_bonus(uid)
    total = soma + bonus_ajuda

    # Definir resultado narrativo
    if total <= 5:
        status = "☠️ Morte súbita! O corpo não resistiu, e a escuridão se fechou."
    elif total <= 12:
        status = "💀 Continua em coma. O corpo permanece inconsciente, lutando por cada respiração."
    elif total <= 19:
        update_player_field(uid, 'hp', 1)
        status = "🌅 Você desperta, fraco e atordoado. HP agora: 1."
    else:  # 20+
        extra_hp = random.randint(2, 5)
        new_hp = min(player['hp_max'], extra_hp)
        update_player_field(uid, 'hp', new_hp)
        status = f"🌟 Sucesso crítico! Um milagre: você acorda com {new_hp} HP, mais forte que antes!"

    await update.message.reply_text(
        "\n".join([
            "🧊 **Teste de Coma**",
            f"Rolagens dos dados: {dados} → {soma}",
            f"Bônus de ajuda: +{bonus_ajuda}",
            f"Total final: {total}",
            f"Resultado: {status}",
        ])
    )

@safe_command_wrapper
async def ajudar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /ajudar - Ajudar em coma"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /ajudar @jogador NomeDoKit (Básico/Intermediário/Avançado)")
        return
    
    alvo_tag = context.args[0]
    alvo_id = username_to_id(alvo_tag)
    if not alvo_id:
        await update.message.reply_text(GameConstants.MESSAGES['player_not_found'])
        return

    alvo = get_player(alvo_id)
    if alvo['hp'] > 0:
        await update.message.reply_text("❌ O alvo não está em coma no momento.")
        return

    kit_nome = " ".join(context.args[1:]).strip()
    key = kit_nome.lower()
    bonus = GameConstants.KIT_BONUS.get(key)
    
    if bonus is None:
        await update.message.reply_text("❌ Kit inválido. Use: Kit Básico, Kit Intermediário ou Kit Avançado.")
        return

    cat = get_catalog_item(kit_nome)
    inv_nome = cat['nome'] if cat else kit_nome
    
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", 
                     (uid, inv_nome))
            row = c.fetchone()
            
            if not row or row[0] <= 0:
                await update.message.reply_text(f"❌ Você não possui '{kit_nome}' no inventário.")
                return
            
            nova = row[0] - 1
            if nova <= 0:
                c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
            else:
                c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", 
                         (nova, uid, inv_nome))
            conn.commit()

        add_coma_bonus(alvo_id, bonus)
        await update.message.reply_text(
            f"🤝 {mention(update.effective_user)} usou {kit_nome} em {alvo_tag}!\n"
            f"Bônus aplicado ao próximo teste de coma: +{bonus}."
        )
    except Exception as e:
        logger.error(f"Erro no comando ajudar: {e}")
        await update.message.reply_text(GameConstants.MESSAGES['internal_error'])

@safe_command_wrapper
async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /roll - Rolar dados"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return

    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    player = get_player(uid)
    if not player or len(context.args) < 1:
        await update.message.reply_text("Uso: /roll nome_da_pericia_ou_atributo")
        return

    key = " ".join(context.args)
    key_norm = normalize_text(key)

    bonus = 0
    real_key = key

    if key_norm in ATRIBUTOS_NORMAL:
        real_key = ATRIBUTOS_NORMAL[key_norm]
        bonus += player['atributos'].get(real_key, 0)
    elif key_norm in PERICIAS_NORMAL:
        real_key = PERICIAS_NORMAL[key_norm]
        bonus += player['pericias'].get(real_key, 0)
    else:
        await update.message.reply_text(
            "❌ Perícia/atributo não encontrado.\nVeja os nomes válidos em /ficha."
        )
        return

    dados = roll_dados()
    total = sum(dados) + bonus
    res = resultado_roll(sum(dados))
    
    await update.message.reply_text(
        f"🎲 /roll {real_key}\nRolagens: {dados} → {sum(dados)}\nBônus: +{bonus}\nTotal: {total} → {res}"
    )

@safe_command_wrapper
async def reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /reroll - Rerolar dados"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return
    
    uid = update.effective_user.id
    player = get_player(uid)
    
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return
    
    if player['rerolls'] <= 0:
        await update.message.reply_text("Você não tem rerolls disponíveis hoje!")
        return

    # Executa o roll normalmente
    await roll(update, context)
    
    # Consome um reroll
    update_player_field(uid, 'rerolls', player['rerolls'] - 1)

@safe_command_wrapper
async def editarficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /editarficha - Editar ficha"""
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text(GameConstants.MESSAGES['antispam'])
        return

    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return

    EDIT_PENDING[uid] = True
    text = (
        "\u200B\nPara editar os pontos em sua ficha, responda (em apenas uma mensagem, você pode mudar quantos Atributos/Perícias quiser) com todas as alterações que deseja realizar, com base no modelo à seguir: \n\n"
        " ✦︎  𝗔𝘁𝗿𝗶𝗯𝘂𝘁𝗼𝘀  \n"
        "<code>Força: </code>\n<code>Destreza: </code>\n<code>Constituição: </code>\n<code>Inteligência: </code>\n<code>Sabedoria: </code>\n<code>Carisma: </code>\n\n"
        " ✦︎  𝗣𝗲𝗿𝗶𝗰𝗶𝗮𝘀  \n"
        "<code>Percepção: </code>\n<code>Persuasão: </code>\n<code>Medicina: </code>\n<code>Furtividade: </code>\n<code>Intimidação: </code>\n<code>Investigação: </code>\n<code>Armas de fogo: </code>\n<code>Armas brancas: </code>\n<code>Sobrevivência: </code>\n<code>Cultura: </code>\n<code>Intuição: </code>\n<code>Tecnologia: </code>\n\n"
        " ⓘ <b>ATENÇÃO</b>\n<blockquote> ▸ Cada Atributo e Perícia deve conter, sem exceção, entre 1 e 6 pontos.</blockquote>\n"
        "<blockquote> ▸ A soma de todos o pontos de Atributos deve totalizar 20</blockquote>\n"
        "<blockquote> ▸ A soma de todos o pontos de Perícia deve totalizar 40.</blockquote>\n\u200B"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def receber_edicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa edição de ficha"""
    uid = update.effective_user.id
    if uid not in EDIT_PENDING:
        register_username(uid, update.effective_user.username, update.effective_user.first_name)
        return

    player = get_player(uid)
    if not player:
        await update.message.reply_text(GameConstants.MESSAGES['need_start'])
        return

    text = update.message.text
    EDIT_TEMP = player["atributos"].copy()
    EDIT_TEMP.update(player["pericias"])

    linhas = text.split("\n")
    for linha in linhas:
        if not linha.strip():
            continue
        try:
            key, val = linha.split(":")
            key = normalize_text(key)
            val = int(val.strip())
        except:
            await update.message.reply_text(f"❌ Remova esta parte: ({linha}) e envie novamente.")
            return

        if key in ATRIBUTOS_NORMAL:
            key_real = ATRIBUTOS_NORMAL[key]
            if val < 1 or val > 6:
                await update.message.reply_text("❌ Formato inválido! Atributos devem estar entre 1 e 6.")
                return
            soma_atributos = sum(EDIT_TEMP.get(a, 0) for a in GameConstants.ATRIBUTOS_LISTA if a != key_real) + val
            if soma_atributos > GameConstants.MAX_ATRIBUTOS:
                await update.message.reply_text("❌ Total de pontos em atributos excede 20.")
                return
            EDIT_TEMP[key_real] = val

        elif key in PERICIAS_NORMAL:
            key_real = PERICIAS_NORMAL[key]
            if val < 1 or val > 6:
                await update.message.reply_text("❌ Formato inválido! Perícias devem estar entre 1 e 6.")
                return
            soma_pericias = sum(EDIT_TEMP.get(p, 0) for p in GameConstants.PERICIAS_LISTA if p != key_real) + val
            if soma_pericias > GameConstants.MAX_PERICIAS:
                await update.message.reply_text("❌ Total de pontos em perícias excede 40.")
                return
            EDIT_TEMP[key_real] = val

        else:
            await update.message.reply_text(f"❌ Campo não reconhecido: {key}")
            return

    player["atributos"] = {k: EDIT_TEMP[k] for k in GameConstants.ATRIBUTOS_LISTA}
    player["pericias"] = {k: EDIT_TEMP[k] for k in GameConstants.PERICIAS_LISTA}

    for atr in GameConstants.ATRIBUTOS_LISTA:
        update_atributo(uid, atr, player["atributos"][atr])
    for per in GameConstants.PERICIAS_LISTA:
        update_pericia(uid, per, player["pericias"][per])
    ensure_peso_max_by_forca(uid)

    await update.message.reply_text(" ✅ Ficha atualizada com sucesso!")
    EDIT_PENDING.pop(uid, None)

# ================== SISTEMA DE CALLBACKS UNIFICADO ==================
async def handle_confirm_transfer(query, params):
    """Processa confirmação de transferência"""
    user_id = query.from_user.id
    transfer = None
    transfer_key = None
    
    for k, v in TRANSFER_PENDING.items():
        if v['alvo'] == user_id:
            transfer = v
            transfer_key = k
            break
    
    if not transfer:
        await query.edit_message_text("❌ Nenhuma transferência pendente.")
        return

    try:
        doador = transfer['doador']
        alvo = transfer['alvo']
        item = transfer['item']
        qtd = transfer['qtd']
        
        with get_db_connection() as conn:
            c = conn.cursor()
            # Debita do doador
            c.execute("SELECT quantidade, peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                     (doador, item))
            row = c.fetchone()

            if row:
                qtd_doador, peso_item = row
                nova_qtd_doador = qtd_doador - qtd
                if nova_qtd_doador <= 0:
                    c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                             (doador, item))
                else:
                    c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                             (nova_qtd_doador, doador, item))
            else:
                if is_admin(doador):
                    item_info = get_catalog_item(item)
                    if not item_info:
                        await query.edit_message_text("❌ Item não encontrado no catálogo.")
                        return
                    peso_item = item_info["peso"]
                else:
                    await query.edit_message_text("❌ O doador não tem mais o item.")
                    return

            # Credita no alvo
            c.execute("SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                     (alvo, item))
            row_tgt = c.fetchone()
            if row_tgt:
                nova_qtd_tgt = row_tgt[0] + qtd
                c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                         (nova_qtd_tgt, alvo, item))
            else:
                c.execute("INSERT INTO inventario(player_id, nome, peso, quantidade) VALUES(%s,%s,%s,%s)",
                         (alvo, item, peso_item, qtd))

            conn.commit()

        TRANSFER_PENDING.pop(transfer_key, None)

        giver_after = get_player(doador)
        target_after = get_player(alvo)
        total_giver = peso_total(giver_after)
        total_target = peso_total(target_after)

        await query.edit_message_text(
            f"✅ Transferência confirmada! {item} x{qtd} entregue.\n"
            f"📦 {giver_after['nome']}: {total_giver:.1f}/{giver_after['peso_max']} kg\n"
            f"📦 {target_after['nome']}: {total_target:.1f}/{target_after['peso_max']} kg"
        )
        log_user_action(doador, "item_transferred", f"{item} x{qtd} para {alvo}")
    except Exception as e:
        logger.error(f"Erro na transferência: {e}")
        await query.edit_message_text("❌ Erro ao processar transferência.")

async def handle_cancel_transfer(query, params):
    """Processa cancelamento de transferência"""
    user_id = query.from_user.id
    to_remove = None
    for k, v in TRANSFER_PENDING.items():
        if v['alvo'] == user_id:
            to_remove = k
            break
    if to_remove:
        TRANSFER_PENDING.pop(to_remove)
    await query.edit_message_text("❌ Transferência cancelada pelo jogador.")

async def handle_confirm_abandon(query, params):
    """Processa confirmação de abandono"""
    if len(params) < 2:
        await query.edit_message_text("❌ Dados inválidos.")
        return
    
    uid = safe_int_parse(params[0], 0, 999999999999)
    item_nome = params[1]
    
    if not uid:
        await query.edit_message_text("❌ Dados inválidos.")
        return

    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("DELETE FROM inventario WHERE player_id=%s AND nome=%s", (uid, item_nome))
            conn.commit()
        
        jogador = get_player(uid)
        total_peso = peso_total(jogador)
        
        await query.edit_message_text(
            f"✅ '{item_nome}' foi abandonado.\n"
            f"📦 Inventário agora: {total_peso:.1f}/{jogador['peso_max']} kg"
        )
        log_user_action(uid, "item_abandoned", item_nome)
    except Exception as e:
        logger.error(f"Erro ao abandonar item: {e}")
        await query.edit_message_text("❌ Erro ao abandonar item.")

async def handle_cancel_generic(query, params):
    """Processa cancelamento genérico"""
    await query.edit_message_text("❌ Ação cancelada.")

# Handlers de callback centralizados
CALLBACK_HANDLERS = {
    'confirm_dar': handle_confirm_transfer,
    'cancel_dar': handle_cancel_transfer,
    'confirm_abandonar': handle_confirm_abandon,
    'cancel': handle_cancel_generic,
}

async def unified_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler unificado para todos os callbacks"""
    query = update.callback_query
    await query.answer()
    
    try:
        callback_data = query.data
        if ':' in callback_data:
            action, *params = callback_data.split(':')
        else:
            action = callback_data
            params = []
        
        handler = CALLBACK_HANDLERS.get(action)
        if handler:
            await handler(query, params)
        else:
            await query.edit_message_text("❌ Ação não reconhecida.")
    except Exception as e:
        logger.error(f"Erro no callback handler: {e}")
        await query.edit_message_text(GameConstants.MESSAGES['internal_error'])

# ================== SISTEMA DE BACKUP AUTOMÁTICO ==================
def backup_database():
    """Backup automático do banco de dados"""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"backup_{timestamp}.sql"
        
        result = subprocess.run([
            "pg_dump", DATABASE_URL, "-f", backup_file
        ], capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            logger.info(f"✅ Backup criado: {backup_file}")
        else:
            logger.error(f"❌ Erro no backup: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.error("❌ Timeout no backup do banco")
    except Exception as e:
        logger.error(f"❌ Erro no backup: {e}")

def schedule_backup():
    """Agenda backup automático"""
    schedule.every().day.at("03:00").do(backup_database)
    while True:
        schedule.run_pending()
        time.sleep(3600)  # Verifica a cada hora

# ================== RESET DIÁRIO DE REROLLS ==================
def reset_diario_rerolls():
    """Reset diário dos rerolls"""
    while True:
        try:
            now = datetime.now()
            next_reset = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if now >= next_reset:
                next_reset += timedelta(days=1)
            
            wait_seconds = (next_reset - now).total_seconds()
            time.sleep(wait_seconds)
            
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("UPDATE players SET rerolls=%s", (GameConstants.REROLLS_DAILY,))
                conn.commit()
            
            logger.info("🔄 Rerolls diários resetados!")
        except Exception as e:
            logger.error(f"Erro no reset de rerolls: {e}")
            time.sleep(3600)  # Espera 1 hora em caso de erro

# ================== FLASK APP ==================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return {
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0-complete"
    }

@flask_app.route("/health")
def health():
    """Endpoint de saúde"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT 1")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}, 500

def run_flask():
    """Executa servidor Flask"""
    try:
        flask_app.run(host="0.0.0.0", port=10000, debug=False)
    except Exception as e:
        logger.error(f"Erro no servidor Flask: {e}")

# ================== FUNÇÃO MAIN COMPLETA ==================
def main():
    """Função principal completa"""
    try:
        logger.info("🚀 Iniciando bot RPG completo...")
        
        # Inicialização segura
        init_connection_pool()
        init_db()
        
        # Threads auxiliares
        threading.Thread(target=run_flask, daemon=True).start()
        threading.Thread(target=reset_diario_rerolls, daemon=True).start()
        threading.Thread(target=schedule_backup, daemon=True).start()
        
        # Aplicação do bot
        app = Application.builder().token(TOKEN).build()
        
        # Todos os comandos
        command_handlers = [
            ("start", start),
            ("ficha", ficha),
            ("verficha", verficha),
            ("inventario", inventario),
            ("itens", itens),
            ("additem", additem),
            ("delitem", delitem),
            ("dar", dar),
            ("abandonar", abandonar),
            ("dano", dano),
            ("autodano", autodano),
            ("cura", cura),
            ("autocura", autocura),
            ("terapia", terapia),
            ("coma", coma),
            ("ajudar", ajudar),
            ("roll", roll),
            ("reroll", reroll),
            ("editarficha", editarficha),
        ]
        
        for command, handler in command_handlers:
            app.add_handler(CommandHandler(command, handler))
        
        # Handler unificado de callbacks
        app.add_handler(CallbackQueryHandler(unified_callback_handler))
        
        # Handler de mensagens para edição
        app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receber_edicao))
        
        logger.info("✅ Bot iniciado com sucesso!")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrompido pelo usuário")
    except Exception as e:
        logger.critical(f"❌ Erro crítico na inicialização: {e}")
        raise
    finally:
        # Cleanup
        if CONNECTION_POOL:
            CONNECTION_POOL.closeall()
        logger.info("🧹 Recursos liberados")

if __name__ == "__main__":
    main()
