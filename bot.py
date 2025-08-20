import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
from urllib.parse import quote, unquote
import psycopg2
import psycopg2.extras
import os
from flask import Flask
import random
import threading
import time
from datetime import datetime, timedelta
import re
import unicodedata

def normalizar(texto):
    texto = texto.lower()
    texto = ''.join(c for c in unicodedata.normalize('NFD', texto)
                    if unicodedata.category(c) != 'Mn')
    return texto

# ================== CONFIGURAÇÕES ==================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("NEON_DATABASE_URL")

ADMIN_IDS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}
PESO_MAX = {1: 5.0, 2: 10.0, 3: 15.0, 4: 20.0, 5: 25.0, 6: 30.0}
LAST_COMMAND = {}
COOLDOWN = 1

MAX_ATRIBUTOS = 20
MAX_PERICIAS = 40
ATRIBUTOS_LISTA = ["Força","Destreza","Constituição","Inteligência","Sabedoria","Carisma"]
PERICIAS_LISTA = ["Percepção","Persuasão","Medicina","Furtividade","Intimidação","Investigação",
                  "Pontaria","Luta","Sobrevivência","Cultura","Intuição","Tecnologia"]
ATRIBUTOS_NORMAL = {normalizar(a): a for a in ATRIBUTOS_LISTA}
PERICIAS_NORMAL = {normalizar(p): p for p in PERICIAS_LISTA}

EDIT_PENDING = {}
EDIT_TIMERS = {}  # Para timeouts de edição

TRANSFER_PENDING = {}
ABANDON_PENDING = {}

KIT_BONUS = {
    "kit basico": 1,
    "kit básico": 1,
    "basico": 1,
    "básico": 1,
    "kit intermediario": 2,
    "kit intermediário": 2,
    "intermediario": 2,
    "intermediário": 2,
    "kit avancado": 3,
    "kit avançado": 3,
    "avancado": 3,
    "avançado": 3,
}

CONSUMIVEIS = [
    "comida enlatada", "água", "garrafa d'água", "ração", "barrinha", "barra de cereal"
    # Adicione aqui todos os nomes normalizados dos seus consumíveis!
]

TRAUMAS = [
    "Hipervigilância: não consegue dormir sem vigiar todas as entradas.",
    "Tremor incontrolável nas mãos em situações de estresse.",
    "Mutismo temporário diante de sons altos.",
    "Ataques de pânico ao sentir cheiro de sangue.",
    "Flashbacks paralisantes ao ouvir gritos.",
    "Aversão a ambientes fechados (claustrofobia aguda).",
]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== POSTGRESQL ==================
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players (
                    id BIGINT PRIMARY KEY,
                    nome TEXT,
                    username TEXT,
                    peso_max INTEGER DEFAULT 0,
                    hp INTEGER DEFAULT 40,
                    sp INTEGER DEFAULT 40,
                    rerolls INTEGER DEFAULT 3
                )''')
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
                    PRIMARY KEY(player_id,nome)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pericias (
                    player_id BIGINT,
                    nome TEXT,
                    valor INTEGER DEFAULT 0,
                    PRIMARY KEY(player_id,nome)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS inventario (
                    player_id BIGINT,
                    nome TEXT,
                    peso REAL,
                    quantidade INTEGER DEFAULT 1,
                    PRIMARY KEY(player_id,nome)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS catalogo (
                    nome TEXT PRIMARY KEY,
                    peso REAL
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS coma_bonus (
                    target_id BIGINT PRIMARY KEY,
                    bonus INTEGER DEFAULT 0
                )''')
    # Tabelas para sistema de turnos/XP
    c.execute('''CREATE TABLE IF NOT EXISTS turnos (
                    player_id BIGINT,
                    data DATE,
                    caracteres INTEGER,
                    mencoes TEXT,
                    PRIMARY KEY (player_id, data)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS xp_semana (
                    player_id BIGINT,
                    semana_inicio DATE,
                    xp_total INTEGER DEFAULT 0,
                    streak_atual INTEGER DEFAULT 0,
                    PRIMARY KEY (player_id, semana_inicio)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS interacoes_mutuas (
                    semana_inicio DATE,
                    jogador1 BIGINT,
                    jogador2 BIGINT,
                    PRIMARY KEY (semana_inicio, jogador1, jogador2)
                )''')
    # ✅ Garante que a tabela catalogo tenha a coluna consumivel
    try:
        c.execute("ALTER TABLE catalogo ADD COLUMN consumivel BOOLEAN DEFAULT FALSE;")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()  # ignora erro caso a coluna já exista
    conn.commit()
    conn.close()

def register_username(user_id: int, username: str | None, first_name: str | None):
    if not username:
        return
    username = username.lower()
    now = int(time.time())
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO usernames(username, user_id, first_name, last_seen) VALUES(%s,%s,%s,%s) ON CONFLICT (username) DO UPDATE SET user_id=%s, first_name=%s, last_seen=%s",
        (username, user_id, first_name or '', now, user_id, first_name or '', now))
    c.execute("UPDATE players SET username=%s WHERE id=%s", (username, user_id))
    conn.commit()
    conn.close()

def username_to_id(user_tag: str) -> int | None:
    if not user_tag:
        return None
    if user_tag.startswith('@'):
        uname = user_tag[1:].lower()
    else:
        uname = user_tag.lower()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM usernames WHERE username=%s", (uname,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_player(uid):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE id=%s", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    player = {
        "id": row["id"],
        "nome": row["nome"],
        "username": row["username"],
        "peso_max": row["peso_max"],
        "hp": row["hp"],
        "hp_max": 40,   # DEFAULT
        "sp": row["sp"],
        "sp_max": 40,   # DEFAULT
        "rerolls": row["rerolls"],
        "atributos": {},
        "pericias": {},
        "inventario": []
    }
    # Atributos
    c.execute("SELECT nome, valor FROM atributos WHERE player_id=%s", (uid,))
    for a, v in c.fetchall():
        player["atributos"][a] = v
    # Perícias
    c.execute("SELECT nome, valor FROM pericias WHERE player_id=%s", (uid,))
    for a, v in c.fetchall():
        player["pericias"][a] = v
    # Inventário
    c.execute("SELECT nome,peso,quantidade FROM inventario WHERE player_id=%s", (uid,))
    for n, p, q in c.fetchall():
        player["inventario"].append({"nome": n, "peso": p, "quantidade": q})
    conn.close()
    return player

def create_player(uid, nome, username=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO players(id,nome,username) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (uid, nome, (username or None)))
    for a in ATRIBUTOS_LISTA:
        c.execute("INSERT INTO atributos(player_id,nome,valor) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (uid, a, 0))
    for p in PERICIAS_LISTA:
        c.execute("INSERT INTO pericias(player_id,nome,valor) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (uid, p, 0))
    conn.commit()
    conn.close()

def update_player_field(uid, field, value):
    conn = get_conn()
    c = conn.cursor()
    c.execute(f"UPDATE players SET {field}=%s WHERE id=%s", (value, uid))
    conn.commit()
    conn.close()

def update_atributo(uid, nome, valor):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE atributos SET valor=%s WHERE player_id=%s AND nome=%s", (valor, uid, nome))
    conn.commit()
    conn.close()

def update_pericia(uid, nome, valor):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE pericias SET valor=%s WHERE player_id=%s AND nome=%s", (valor, uid, nome))
    conn.commit()
    conn.close()

def update_inventario(uid, item):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, item['nome']))
    row = c.fetchone()
    if row:
        c.execute("UPDATE inventario SET quantidade=%s, peso=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                  (item['quantidade'], item['peso'], uid, item['nome']))
    else:
        c.execute("INSERT INTO inventario(player_id, nome, peso, quantidade) VALUES (%s, %s, %s, %s)",
                  (uid, item['nome'], item['peso'], item['quantidade']))
    conn.commit()
    conn.close()

def adjust_item_quantity(uid, item_nome, delta):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade, peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, item_nome))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    qtd, peso = row
    nova = qtd + delta
    if nova <= 0:
        c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, item_nome))
    else:
        c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (nova, uid, item_nome))
    conn.commit()
    conn.close()
    return True

def get_catalog_item(nome: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT nome, peso, consumivel FROM catalogo WHERE LOWER(nome)=LOWER(%s)", (nome,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {"nome": row[0], "peso": row[1], "consumivel": row[2]}

def add_catalog_item(nome: str, peso: float, consumivel: bool = False):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO catalogo(nome,peso,consumivel) VALUES(%s,%s,%s) "
        "ON CONFLICT (nome) DO UPDATE SET peso=%s, consumivel=%s",
        (nome, peso, consumivel, peso, consumivel)
    )
    conn.commit()
    conn.close()

def del_catalog_item(nome: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM catalogo WHERE LOWER(nome)=LOWER(%s)", (nome,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted > 0

def list_catalog():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT nome,peso,consumivel FROM catalogo ORDER BY nome COLLATE \"C\"")
    data = c.fetchall()
    conn.close()
    return data

def is_consumivel_catalogo(nome: str):
    item = get_catalog_item(nome)
    return item and item.get("consumivel")

def remove_item(uid, item_nome):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, item_nome))
    conn.commit()
    conn.close()

def peso_total(player):
    return sum(i['peso'] * i.get('quantidade', 1) for i in player.get("inventario", []))

def penalidade(player):
    return peso_total(player) > player["peso_max"]

def penalidade_sobrecarga(player):
    excesso = peso_total(player) - player["peso_max"]
    if excesso <= 0:
        return 0
    if excesso <= 5:
        return -1
    elif excesso <= 10:
        return -2
    else:
        return -3

def anti_spam(user_id):
    now = time.time()
    if user_id in LAST_COMMAND and now - LAST_COMMAND[user_id] < COOLDOWN:
        return False
    LAST_COMMAND[user_id] = now
    return True

def roll_dados(qtd=4, lados=6):
    return [random.randint(1, lados) for _ in range(qtd)]

def resultado_roll(valor_total):
    if valor_total <= 5:
        return "Fracasso crítico"
    elif valor_total <= 12:
        return "Fracasso"
    elif valor_total <= 19:
        return "Sucesso"
    else:
        return "Sucesso crítico"

def parse_float_br(s: str) -> float | None:
    s = s.strip().lower().replace("kg", "").strip()
    s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except:
        return None

def ensure_peso_max_by_forca(uid: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT valor FROM atributos WHERE player_id=%s AND nome='Força'", (uid,))
    row = c.fetchone()
    if row:
        valor_forca = max(1, min(6, int(row[0])))
        novo = PESO_MAX.get(valor_forca, 0)
        c.execute("UPDATE players SET peso_max=%s WHERE id=%s", (novo, uid))
        conn.commit()
    conn.close()

def add_coma_bonus(target_id: int, delta: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO coma_bonus(target_id, bonus) VALUES(%s,0) ON CONFLICT (target_id) DO NOTHING", (target_id,))
    c.execute("UPDATE coma_bonus SET bonus = bonus + %s WHERE target_id=%s", (delta, target_id))
    conn.commit()
    conn.close()

def pop_coma_bonus(target_id: int) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT bonus FROM coma_bonus WHERE target_id=%s", (target_id,))
    row = c.fetchone()
    bonus = row[0] if row else 0
    c.execute("DELETE FROM coma_bonus WHERE target_id=%s", (target_id,))
    conn.commit()
    conn.close()
    return bonus

def reset_diario_rerolls():
    while True:
        try:
            now = datetime.now()
            next_reset = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if now >= next_reset:
                next_reset += timedelta(days=1)
            wait_seconds = (next_reset - now).total_seconds()
            time.sleep(wait_seconds)
            
            conn = get_conn()
            c = conn.cursor()
            c.execute("UPDATE players SET rerolls=3")
            conn.commit()
            conn.close()
            logger.info("🔄 Rerolls diários resetados!")
            
        except Exception as e:
            logger.error(f"Erro no reset de rerolls: {e}")
            time.sleep(60)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def mention(user):
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Jogador"

def cleanup_expired_transfers():
    while True:
        try:
            now = time.time()
            expired_keys = []
            for key, transfer in TRANSFER_PENDING.items():
                if now > transfer.get('expires', now):
                    expired_keys.append(key)
            
            for key in expired_keys:
                TRANSFER_PENDING.pop(key, None)
                
            time.sleep(300)
        except Exception as e:
            logger.error(f"Erro na limpeza de transferências: {e}")
            time.sleep(60)

def semana_atual():
    hoje = datetime.now()
    segunda = hoje - timedelta(days=hoje.weekday())
    return segunda.date()

def xp_por_caracteres(n):
    if n < 500:
        return 0
    elif n < 1000:
        return 10
    elif n < 1500:
        return 15
    elif n < 2000:
        return 20
    elif n <= 4096:
        return 25
    else:
        return 25

async def turno(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == 'private':
        await update.message.reply_text("Este comando só pode ser usado em grupos!")
        return

    uid = update.effective_user.id
    username = update.effective_user.username
    hoje = datetime.now().date()
    semana = semana_atual()

    texto = update.message.text or ""
    # aceita também /turno@BotUsername
    texto_limpo = re.sub(r'^/turno(?:@\w+)?', '', texto, flags=re.IGNORECASE).strip()
    caracteres = len(texto_limpo)

    conn = get_conn()
    c = conn.cursor()

    # Bloqueia segunda tentativa no mesmo dia apenas se já houver um turno VÁLIDO registrado
    c.execute("SELECT 1 FROM turnos WHERE player_id=%s AND data=%s", (uid, hoje))
    if c.fetchone():
        conn.close()
        await update.message.reply_text("Você já enviou seu turno hoje! Apenas 1 por dia é contabilizado.")
        return

    # 🚨 Caso a pessoa mande só /turno sem texto
    if not texto_limpo:
        conn.close()
        await update.message.reply_text(
            "ℹ️ Para registrar um turno, use este comando seguido do seu texto.\n\n"
            "Exemplo:\n"
            "<code>/turno O personagem caminhou pela floresta, descrevendo as árvores geladas...</code>\n\n"
            "⚠️ O texto precisa ter no mínimo 499 caracteres para ser contabilizado.",
            parse_mode="HTML"
        )
        return

    # ✅ Validação de tamanho mínimo: não salva nada quando inválido
    if caracteres < 499:
        conn.close()  # garante que a conexão não fique aberta
        await update.message.reply_text(
            f"⚠️ Seu turno precisa ter pelo menos 499 caracteres! (Atualmente: {caracteres})\n"
            "Nada foi registrado. Envie novamente com mais conteúdo."
        )
        return

    mencoes = set(re.findall(r"@(\w+)", texto_limpo))
    if username:
        mencoes.discard(username.lower())
    mencoes = list(mencoes)
    if len(mencoes) > 5:
        mencoes = mencoes[:5]
        await update.message.reply_text("⚠️ Só é possível mencionar até 5 jogadores por turno. Apenas os 5 primeiros serão considerados.")
    mencoes_str = ",".join(mencoes) if mencoes else ""

    xp = xp_por_caracteres(caracteres)

    c.execute("SELECT data FROM turnos WHERE player_id=%s AND data >= %s ORDER BY data", (uid, semana))
    dias = [row[0] for row in c.fetchall()]
    streak_atual = 1
    if dias:
        prev = dias[-1]
        if (hoje - prev).days == 1:
            streak_atual = len(dias) + 1
        else:
            streak_atual = 1

    bonus_streak = 0
    if streak_atual == 3:
        bonus_streak = 5
    elif streak_atual == 5:
        bonus_streak = 10
    elif streak_atual == 7:
        bonus_streak = 20

    xp_dia = min(xp + bonus_streak, 25)

    # Só insere porque já passou na validação (>= 499)
    c.execute(
        "INSERT INTO turnos (player_id, data, caracteres, mencoes) VALUES (%s, %s, %s, %s)",
        (uid, hoje, caracteres, mencoes_str)
    )
    c.execute(
        "INSERT INTO xp_semana (player_id, semana_inicio, xp_total, streak_atual) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (player_id, semana_inicio) DO UPDATE SET xp_total = xp_semana.xp_total + %s, streak_atual = %s",
        (uid, semana, xp_dia, streak_atual, xp_dia, streak_atual)
    )

    # Interação mútua diária
    interacoes_bonificadas = set()
    for mencionado in mencoes:
        mencionado_id = username_to_id(f"@{mencionado}")
        if mencionado_id and mencionado_id != uid:
            c.execute("SELECT mencoes FROM turnos WHERE player_id=%s AND data=%s", (mencionado_id, hoje))
            row = c.fetchone()
            if row and row[0]:
                mencoes_do_outra_pessoa = set(row[0].split(","))
                if username and username.lower() in mencoes_do_outra_pessoa:
                    par = tuple(sorted([uid, mencionado_id]))
                    if par not in interacoes_bonificadas:
                        c.execute("UPDATE xp_semana SET xp_total = xp_total + 5 WHERE player_id=%s AND semana_inicio=%s", (uid, semana))
                        c.execute("UPDATE xp_semana SET xp_total = xp_total + 5 WHERE player_id=%s AND semana_inicio=%s", (mencionado_id, semana))
                        interacoes_bonificadas.add(par)
                        try:
                            await context.bot.send_message(uid, f"🎉 Você e @{mencionado} mencionaram um ao outro no turno de hoje! Ambos ganharam +5 XP de interação mútua.", parse_mode="HTML")
                            await context.bot.send_message(mencionado_id, f"🎉 Você e @{username} mencionaram um ao outro no turno de hoje! Ambos ganharam +5 XP de interação mútua.", parse_mode="HTML")
                        except Exception as e:
                            logger.warning(f"Falha ao enviar mensagem privada de bônus: {e}")

    conn.commit()
    conn.close()

    msg = f"Turno registrado!\nCaracteres: {caracteres}\nXP ganho hoje: {xp}"
    if bonus_streak:
        msg += f"\nBônus de streak: +{bonus_streak} XP"
    msg += f"\nStreak atual: {streak_atual} dias"
    await update.message.reply_text(msg)

def ranking_semanal(context=None):
    semana = semana_atual()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT player_id, xp_total FROM xp_semana WHERE semana_inicio=%s ORDER BY xp_total DESC LIMIT 3", (semana,))
    top = c.fetchall()
    players = {pid: get_player(pid) for pid, _ in top}
    lines = ["🏆 Ranking Final da Semana:"]
    medals = ['🥇', '🥈', '🥉']
    for idx, (pid, xp) in enumerate(top):
        nome = players[pid]['nome'] if players.get(pid) else f"ID:{pid}"
        lines.append(f"{medals[idx]} <b>{nome}</b> – XP: {xp}")
    texto = "\n".join(lines)

    if context:
        for admin_id in ADMIN_IDS:
            try:
                context.bot.send_message(admin_id, texto, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Falha ao enviar ranking para admin {admin_id}: {e}")

    c.execute("DELETE FROM xp_semana WHERE semana_inicio=%s", (semana,))
    conn.commit()
    conn.close()

def thread_reset_xp():
    while True:
        now = datetime.now()
        proxima = now.replace(hour=6, minute=0, second=0, microsecond=0)
        while proxima.weekday() != 0:
            proxima += timedelta(days=1)
        if now >= proxima:
            proxima += timedelta(days=7)
        wait = (proxima - now).total_seconds()
        time.sleep(wait)
        ranking_semanal()

# ================== COMANDOS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    nome = update.effective_user.first_name
    username = update.effective_user.username
    if not get_player(uid):
        create_player(uid, nome, username)
        register_username(uid, username, nome)
        update_player_field(uid, 'hp_max', 40)
        update_player_field(uid, 'sp_max', 40)
    await update.message.reply_text(
    f"\u200B\n 𐚁  𝗕𝗼𝗮𝘀 𝘃𝗶𝗻𝗱𝗮𝘀, {nome} ! \n\n"
    "Este bot gerencia seus Dados, Ficha, Inventário, Vida e Sanidade, além de diversos outros sistemas que você poderá explorar.\n\n"
    "Use o comando <b>/ficha</b> para visualizar sua ficha atual. "
    "Para editá-la, use o comando <b>/editarficha</b>.\n\n"
    "Outros comandos úteis: <b>/roll</b>, <b>/inventario</b>, <b>/dar</b>, <b>/abandonar</b>, <b>/dano</b>, <b>/cura</b>, <b>/terapia</b>.\n\n"
    " 𝗔𝗽𝗿𝗼𝘃𝗲𝗶𝘁𝗲!\n\u200B",
    parse_mode="HTML"
)

async def ficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Você precisa usar /start primeiro!")
        return
    text = "\u200B\n「  ཀ  𝗗𝗘𝗔𝗗𝗟𝗜𝗡𝗘, ficha.  」​\u200B\n\n ✦︎  𝗔𝘁𝗿𝗶𝗯𝘂𝘁𝗼𝘀  \n"
    for a in ATRIBUTOS_LISTA:
        val = player["atributos"].get(a, 0)
        text += f" — {a}﹕{val}\n"
    text += "\n ✦︎  𝗣𝗲𝗿𝗶𝗰𝗶𝗮𝘀  \n"
    for p in PERICIAS_LISTA:
        val = player["pericias"].get(p, 0)
        text += f" — {p}﹕{val}\n"
    text += f"\n 𖹭  𝗛𝗣  (Vida)  ▸  {player['hp']} / 40\n 𖦹  𝗦𝗣  (Sanidade)  ▸  {player['sp']} / 40\n"
    total_peso = peso_total(player)
    sobre = "  ⚠︎  Você está com <b>SOBRECARGA</b>!" if penalidade(player) else ""
    text += f"\n 𖠩  𝗣𝗲𝘀𝗼 𝗧𝗼𝘁𝗮𝗹 ﹕ {total_peso:.1f} / {player['peso_max']}{sobre}\n\n"
    penal = penalidade_sobrecarga(player)
    if penal:
        text += f"⚠︎ Penalidade ativa: {penal} em Força, Destreza e Furtividade!\n"
    text += "<blockquote>Para editar Atributos e Perícias, utilize o comando /editarficha.</blockquote>\n<blockquote>Para gerenciar seu Inventário, utilize o comando /inventario.</blockquote>\n\u200B"
    await update.message.reply_text(text, parse_mode="HTML")

async def editarficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return

    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return

    EDIT_PENDING[uid] = True
    
    # Cancelar timer anterior se existir
    if uid in EDIT_TIMERS:
        EDIT_TIMERS[uid].cancel()
    
    # Criar timer de 5 minutos para timeout
    def timeout_edit():
        EDIT_PENDING.pop(uid, None)
        EDIT_TIMERS.pop(uid, None)
        logger.info(f"Timeout de edição para usuário {uid}")
    
    EDIT_TIMERS[uid] = threading.Timer(300.0, timeout_edit)
    EDIT_TIMERS[uid].start()
    
    text = (
        "\u200B\nPara editar os pontos em sua ficha, responda em apenas uma mensagem todas as alterações que deseja realizar. Você pode mudar quantos Atributos e Perícias quiser de uma só vez! \n\n"
        " ⤷ <b>EXEMPLO</b>\n\n<blockquote>Força: 3\nPersuasão: 2\nMedicina: 1</blockquote>\n\n"
        "TODOS os Atributos e Perícias, é só copiar, colar, preencher e enviar!\n"
        "\n<pre>Força: \nDestreza: \nConstituição: \nInteligência: \nSabedoria: \nCarisma: \nPercepção: \nPersuasão: \nMedicina: \nFurtividade: \nIntimidação: \nInvestigação: \nPontaria: \nLuta: \nSobrevivência: \nCultura: \nIntuição: \nTecnologia: </pre>\n\n"
        " ⓘ <b>ATENÇÃO</b>\n\n<blockquote> ▸ Cada Atributo e Perícia deve conter, sem exceção, entre 1 e 6 pontos.</blockquote>\n"
        "<blockquote> ▸ A soma de todos o pontos de Atributos deve totalizar 20</blockquote>\n"
        "<blockquote> ▸ A soma de todos o pontos de Perícia deve totalizar 40.</blockquote>\n"
        "<blockquote> ▸ Você tem 5 minutos para enviar as alterações.</blockquote>\n\u200B"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def receber_edicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in EDIT_PENDING:
        register_username(uid, update.effective_user.username, update.effective_user.first_name)
        return

    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
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
            key = normalizar(key)
            val = int(val.strip())
        except:
            await update.message.reply_text(f"❌ Remova esta parte: ({linha}) e envie novamente.")
            return

        if key in ATRIBUTOS_NORMAL:
            key_real = ATRIBUTOS_NORMAL[key]
            if val < 1 or val > 6:
                await update.message.reply_text("❌ Formato inválido! Atributos devem estar entre 1 e 6.")
                return
            soma_atributos = sum(EDIT_TEMP.get(a, 0) for a in ATRIBUTOS_LISTA if a != key_real) + val
            if soma_atributos > MAX_ATRIBUTOS:
                await update.message.reply_text("❌ Total de pontos em atributos excede 20.")
                return
            EDIT_TEMP[key_real] = val

        elif key in PERICIAS_NORMAL:
            key_real = PERICIAS_NORMAL[key]
            if val < 1 or val > 6:
                await update.message.reply_text("❌ Formato inválido! Perícias devem estar entre 1 e 6.")
                return
            soma_pericias = sum(EDIT_TEMP.get(p, 0) for p in PERICIAS_LISTA if p != key_real) + val
            if soma_pericias > MAX_PERICIAS:
                await update.message.reply_text("❌ Total de pontos em perícias excede 40.")
                return
            EDIT_TEMP[key_real] = val

        else:
            await update.message.reply_text(f"❌ Campo não reconhecido: {key}")
            return

    player["atributos"] = {k: EDIT_TEMP[k] for k in ATRIBUTOS_LISTA}
    player["pericias"] = {k: EDIT_TEMP[k] for k in PERICIAS_LISTA}

    for atr in ATRIBUTOS_LISTA:
        update_atributo(uid, atr, player["atributos"][atr])
    for per in PERICIAS_LISTA:
        update_pericia(uid, per, player["pericias"][per])
    ensure_peso_max_by_forca(uid)

    await update.message.reply_text(" ✅ Ficha atualizada com sucesso!")
    
    # Limpar estado de edição e cancelar timer
    EDIT_PENDING.pop(uid, None)
    if uid in EDIT_TIMERS:
        EDIT_TIMERS[uid].cancel()
        EDIT_TIMERS.pop(uid, None)
    
async def verficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    
    uid = update.effective_user.id
    
    # Verifica se é admin
    if not is_admin(uid):
        await update.message.reply_text("❌ Apenas administradores podem usar este comando.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /verficha @jogador")
        return
    
    user_tag = context.args[0]
    target_id = username_to_id(user_tag)
    if not target_id:
        await update.message.reply_text("❌ Jogador não encontrado. Peça para a pessoa usar /start pelo menos uma vez.")
        return
    
    player = get_player(target_id)
    if not player:
        await update.message.reply_text("❌ Jogador não encontrado no sistema.")
        return
    
    # Monta a ficha (mesmo formato do comando /ficha)
    text = f"\u200B\n 「  ཀ  𝗗𝗘𝗔𝗗𝗟𝗜𝗡𝗘, ficha de {player['nome']}.  」​\u200B\n\n ✦︎  𝗔𝘁𝗿𝗶𝗯𝘂𝘁𝗼𝘀  \n"
    for a in ATRIBUTOS_LISTA:
        val = player["atributos"].get(a, 0)
        text += f" — {a}﹕{val}\n"
    text += "\n ✦︎  𝗣𝗲𝗿𝗶𝗰𝗶𝗮𝘀  \n"
    for p in PERICIAS_LISTA:
        val = player["pericias"].get(p, 0)
        text += f" — {p}﹕{val}\n"
    text += f"\n 𖹭  𝗛𝗣  (Vida)  ▸  {player['hp']} / 40\n 𖦹  𝗦𝗣  (Sanidade)  ▸  {player['sp']} / 40\n"
    
    total_peso = peso_total(player)
    sobre = "  ⚠︎  Jogador está com <b>SOBRECARGA</b>!" if penalidade(player) else ""
    text += f"\n 𖠩  𝗣𝗲𝘀𝗼 𝗧𝗼𝘁𝗮𝗹 ﹕ {total_peso:.1f} / {player['peso_max']}{sobre}\n"
    
    # Adiciona informações extras para admin
    text += f"\n📊 <b>Info Admin:</b>\n"
    text += f" — ID: {player['id']}\n"
    text += f" — Username: @{player['username'] or 'N/A'}\n"
    text += f" — Rerolls: {player['rerolls']}/3\n\u200B"
    
    await update.message.reply_text(text, parse_mode="HTML")

async def inventario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return
    lines = [f"\u200B\n「 📦 」 Inventário de {player['nome']}\n"]
    if not player['inventario']:
        lines.append("  Vazio.")
    else:
        for i in sorted(player['inventario'], key=lambda x: x['nome'].lower()):
            lines.append(f"  — {i['nome']} x{i['quantidade']} ({i['peso']:.2f} kg cada)")
    total_peso = peso_total(player)
    lines.append(f"\n  𝗣𝗲𝘀𝗼 𝗧𝗼𝘁𝗮𝗹﹕{total_peso:.1f}/{player['peso_max']} kg\n\u200B")
    if penalidade(player):
        excesso = total_peso - player['peso_max']
        lines.append(f" ⚠︎ {excesso:.1f} kg de <b>SOBRECARGA</b>!")
    penal = penalidade_sobrecarga(player)
    if penal:
        lines.append(f"  ⚠︎ Penalidade ativa: {penal} em Força, Destreza e Furtividade!\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def itens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    data = list_catalog()
    if not data:
        await update.message.reply_text("\u200B\n ☰  Catálogo\n Vazio.\n Use /additem Nome Peso para adicionar.\n\u200B")
        return
    lines = ["\u200B\n ☰  Catálogo de Itens\n\n"]
    for nome, peso, consumivel in data:
        cflag = " (consumível)" if consumivel else ""
        lines.append(f" — {nome} ({peso:.2f} kg){cflag}")
    await update.message.reply_text("\n".join(lines))

async def additem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Apenas administradores podem usar este comando.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /additem NomeDoItem Peso [consumivel]")
        return
    consumivel = False
    if context.args[-1].lower() in ("consumivel", "consumível"):
        consumivel = True
        peso_str = context.args[-2]
        nome = " ".join(context.args[:-2])
    else:
        peso_str = context.args[-1]
        nome = " ".join(context.args[:-1])
    peso = parse_float_br(peso_str)
    if not peso:
        await update.message.reply_text("❌ Peso inválido. Use algo como 2,5")
        return
    add_catalog_item(nome, peso, consumivel)
    await update.message.reply_text(f"✅ Item '{nome}' adicionado ao catálogo com {peso:.2f} kg. Consumível: {'sim' if consumivel else 'não'}")
    
async def delitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Apenas administradores podem usar este comando.")
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

# ========================= DAR =========================
async def dar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Anti-spam
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /dar @jogador Nome do item xquantidade (opcional)")
        return

    uid_from = update.effective_user.id
    register_username(uid_from, update.effective_user.username, update.effective_user.first_name)

    user_tag = context.args[0]
    target_id = username_to_id(user_tag)
    if not target_id:
        await update.message.reply_text("❌ Jogador não encontrado. Peça para a pessoa usar /start pelo menos uma vez.")
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

    # Checa item no inventário
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT nome, peso, quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
        (uid_from, item_input)
    )
    row = c.fetchone()

    if row:
        item_nome, item_peso, qtd_doador = row
        if qtd > qtd_doador:
            conn.close()
            await update.message.reply_text(f"❌ Quantidade indisponível. Você tem {qtd_doador}x '{item_nome}'.")
            return
    else:
        if is_admin(uid_from):
            item_info = get_catalog_item(item_input)
            if not item_info:
                conn.close()
                await update.message.reply_text(f"❌ Item '{item_input}' não encontrado no catálogo.")
                return
            item_nome = item_info["nome"]
            item_peso = item_info["peso"]
        else:
            conn.close()
            await update.message.reply_text(f"❌ Você não possui '{item_input}' no seu inventário.")
            return
    conn.close()

    # Checa sobrecarga do alvo, mas não cancela, só avisa
    target_before = get_player(target_id)
    total_depois_target = peso_total(target_before) + item_peso * qtd
    aviso_sobrecarga = ""
    if total_depois_target > target_before['peso_max']:
        excesso = total_depois_target - target_before['peso_max']
        aviso_sobrecarga = f"  ⚠️ Atenção! {target_before['nome']} ficará com sobrecarga de {excesso:.1f} kg."

    # Criar chave única com timestamp para evitar conflitos
    timestamp = int(time.time())
    transfer_key = f"{uid_from}_{timestamp}_{quote(item_nome)}"
    
    # Salva transferência pendente com expiração
    TRANSFER_PENDING[transfer_key] = {
        "item": item_nome,
        "qtd": qtd,
        "doador": uid_from,
        "alvo": target_id,
        "expires": timestamp + 300  # 5 minutos
    }

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_dar_{transfer_key}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_dar_{transfer_key}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"{user_tag}, {update.effective_user.first_name} quer te dar {item_nome} x{qtd}.\n"
        f"{aviso_sobrecarga}\nAceita a transferência?",
        reply_markup=reply_markup
    )

# ========================= CALLBACK DAR =========================
async def transfer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data.startswith("confirm_dar_"):
        transfer_key = data.replace("confirm_dar_", "")
        transfer = TRANSFER_PENDING.get(transfer_key)
        if not transfer:
            await query.edit_message_text("❌ Transferência não encontrada ou expirada.")
            return
        # SOMENTE o ALVO pode confirmar
        if transfer['alvo'] != user_id:
            await query.answer("Só quem vai receber pode confirmar!", show_alert=True)
            return
            
        if user_id not in (transfer['doador'], transfer['alvo']):
            await query.answer("Só quem está envolvido pode cancelar!", show_alert=True)
            return
        
        if time.time() > transfer['expires']:
            TRANSFER_PENDING.pop(transfer_key, None)
            await query.edit_message_text("❌ Transferência expirada.")
            return

        doador = transfer['doador']
        alvo = transfer['alvo']
        item = transfer['item']
        qtd = transfer['qtd']

        conn = get_conn()
        c = conn.cursor()
        try:
            # Debita do doador
            c.execute(
                "SELECT quantidade, peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                (doador, item)
            )
            row = c.fetchone()

            if row:
                qtd_doador, peso_item = row
                nova_qtd_doador = qtd_doador - qtd
                if nova_qtd_doador <= 0:
                    c.execute(
                        "DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                        (doador, item)
                    )
                else:
                    c.execute(
                        "UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                        (nova_qtd_doador, doador, item)
                    )
            else:
                if is_admin(doador):
                    item_info = get_catalog_item(item)
                    if not item_info:
                        conn.close()
                        await query.edit_message_text("❌ Item não encontrado no catálogo.")
                        TRANSFER_PENDING.pop(transfer_key, None)
                        return
                    peso_item = item_info["peso"]
                else:
                    conn.close()
                    await query.edit_message_text("❌ O doador não tem mais o item.")
                    TRANSFER_PENDING.pop(transfer_key, None)
                    return

            # SEMPRE stacka no inventário do alvo, vindo do catálogo ou não!
            c.execute(
                "SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                (alvo, item)
            )
            row_tgt = c.fetchone()
            if row_tgt:
                nova_qtd_tgt = row_tgt[0] + qtd
                c.execute(
                    "UPDATE inventario SET quantidade=%s, peso=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                    (nova_qtd_tgt, peso_item, alvo, item)
                )
            else:
                c.execute(
                    "INSERT INTO inventario(player_id, nome, peso, quantidade) VALUES(%s,%s,%s,%s)",
                    (alvo, item, peso_item, qtd)
                )

            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            logger.error(f"Erro na transferência: {e}")
            await query.edit_message_text("❌ Ocorreu um erro ao transferir o item.")
            TRANSFER_PENDING.pop(transfer_key, None)
            return
        finally:
            conn.close()

        TRANSFER_PENDING.pop(transfer_key, None)

        # Atualiza pesos e sobrecarga
        giver_after = get_player(doador)
        target_after = get_player(alvo)
        total_giver = peso_total(giver_after)
        total_target = peso_total(target_after)
        excesso = max(0, total_target - target_after['peso_max'])
        aviso_sobrecarga = f"\n  ⚠️ {target_after['nome']} está com sobrecarga de {excesso:.1f} kg!" if excesso else ""

        await query.edit_message_text(
            f"✅ Transferência confirmada! {item} x{qtd} entregue.\n"
            f"📦 {giver_after['nome']}: {total_giver:.1f}/{giver_after['peso_max']} kg\n"
            f"📦 {target_after['nome']}: {total_target:.1f}/{target_after['peso_max']} kg"
            f"{aviso_sobrecarga}"
        )

    # ================= CANCELAMENTO =================
    elif data.startswith("cancel_dar_"):
        transfer_key = data.replace("cancel_dar_", "")
        transfer = TRANSFER_PENDING.get(transfer_key)
        if not transfer:
            await query.edit_message_text("❌ Transferência não encontrada.")
            return
        # Só o doador OU o alvo podem cancelar
        if user_id not in (transfer['doador'], transfer['alvo']):
            return  # Ignora o clique, não cancela nem muda nada!
        TRANSFER_PENDING.pop(transfer_key, None)
        await query.edit_message_text("❌ Transferência cancelada.")

# ========================= COMANDO ABANDONAR =========================
async def abandonar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /abandonar Nome do item xquantidade (opcional)")
        return

    uid = update.effective_user.id

    args = context.args
    if len(args) >= 2 and args[-2].lower() == 'x' and args[-1].isdigit():
        qtd = int(args[-1])
        item_input = " ".join(args[:-2])
    elif len(args) >= 2 and args[-1].isdigit():
        qtd = int(args[-1])
        item_input = " ".join(args[:-1])
    else:
        qtd = 1
        item_input = " ".join(args)

    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT nome, peso, quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
        (uid, item_input.lower())
    )
    row = c.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"❌ Você não possui '{item_input}' no seu inventário.")
        return

    item_nome, item_peso, qtd_inv = row
    if qtd < 1 or qtd > qtd_inv:
        conn.close()
        await update.message.reply_text(f"❌ Quantidade inválida. Você tem {qtd_inv} '{item_nome}'.")
        return

    conn.close()

    # Botões com uid do dono em ambos
    keyboard = [[
        InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_abandonar_{uid}_{quote(item_nome)}_{qtd}"),
        InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_abandonar_{uid}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"⚠️ Você está prestes a abandonar '{item_nome}' x{qtd}. Confirma?",
        reply_markup=reply_markup
    )

# ========================= CALLBACK ABANDONAR =========================
async def callback_abandonar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("confirm_abandonar_"):
        parts = data.split("_", 4)
        if len(parts) < 5:
            await query.edit_message_text("❌ Dados inválidos.")
            return
        _, _, uid_str, item_nome, qtd = parts
        uid = int(uid_str)
        item_nome = unquote(item_nome)
        qtd = int(qtd)
        
        # Só o dono pode confirmar
        if query.from_user.id != uid:
            await query.answer("Só o dono pode confirmar!", show_alert=True)
            return

        conn = get_conn()
        c = conn.cursor()
        try:
            c.execute(
                "SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                (uid, item_nome)
            )
            row = c.fetchone()
            if not row:
                await query.edit_message_text("❌ Item não encontrado no inventário.")
                return
            qtd_inv = row[0]
            if qtd >= qtd_inv:
                c.execute(
                    "DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                    (uid, item_nome)
                )
            else:
                c.execute(
                    "UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                    (qtd_inv - qtd, uid, item_nome)
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Erro ao abandonar item: {e}")
            await query.edit_message_text("❌ Erro ao abandonar o item.")
            return
        finally:
            conn.close()

        jogador = get_player(uid)
        total_peso = peso_total(jogador)

        await query.edit_message_text(
            f"✅ '{item_nome}' x{qtd} foi abandonado.\n"
            f"📦 Inventário agora: {total_peso:.1f}/{jogador['peso_max']} kg"
        )

    # ================= CANCELAR =================
    elif data.startswith("cancel_abandonar_"):
        try:
            uid = int(data.split("_")[-1])  # cancel_abandonar_<uid>
        except ValueError:
            await query.edit_message_text("❌ Dados inválidos.")
            return

        # Só o dono pode cancelar
        if query.from_user.id != uid:
            await query.answer("Só o dono pode cancelar!", show_alert=True)
            return

        await query.answer()
        await query.edit_message_text("❌ Ação cancelada.")

    else:
        await query.answer("Callback inválido.", show_alert=True)


async def consumir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /consumir Nome do item xquantidade (opcional)")
        return

    uid = update.effective_user.id

    args = context.args
    if len(args) >= 2 and args[-2].lower() == 'x' and args[-1].isdigit():
        qtd = int(args[-1])
        item_input = " ".join(args[:-2])
    elif len(args) >= 2 and args[-1].isdigit():
        qtd = int(args[-1])
        item_input = " ".join(args[:-1])
    else:
        qtd = 1
        item_input = " ".join(args)

    # Checa no inventário para exibir nome correto
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT nome, quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
        (uid, item_input.lower())
    )
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"❌ Você não possui '{item_input}' no seu inventário.")
        return

    item_nome, qtd_inv = row

    # Checa consumível
    cat = get_catalog_item(item_nome)
    if not cat or not cat.get("consumivel"):
        await update.message.reply_text(f"❌ '{item_nome}' não é um item consumível.")
        return

    if qtd < 1 or qtd > qtd_inv:
        await update.message.reply_text(f"❌ Quantidade inválida. Você tem {qtd_inv} '{item_nome}'.")
        return

    keyboard = [
        [
            InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_consumir_{uid}_{quote(item_nome)}_{qtd}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_consumir_{uid}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Você está prestes a consumir '{item_nome}' x{qtd}. Confirma?",
        reply_markup=reply_markup
    )

async def callback_consumir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("confirm_consumir_"):
        _, _, uid_str, item_nome, qtd = data.split("_", 4)
        uid = int(uid_str)
        item_nome = unquote(item_nome)
        qtd = int(qtd)
        # Só o dono pode confirmar
        if query.from_user.id != uid:
            await query.answer("Só o dono pode confirmar!", show_alert=True)
            return

        # Confirma no inventário
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, item_nome))
        row = c.fetchone()
        if not row or row[0] < qtd:
            conn.close()
            await query.edit_message_text(f"❌ Quantidade inválida ou item não está mais no inventário.")
            return

        # Checa se continua sendo consumível no catálogo
        cat = get_catalog_item(item_nome)
        if not cat or not cat.get("consumivel"):
            conn.close()
            await query.edit_message_text(f"❌ '{item_nome}' não é mais um item consumível.")
            return

        if qtd == row[0]:
            c.execute(
                "DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                (uid, item_nome)
            )
        else:
            c.execute(
                "UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)",
                (row[0] - qtd, uid, item_nome)
            )
        conn.commit()
        conn.close()
        await query.edit_message_text(f"🍽️ Você consumiu '{item_nome}' x{qtd}!")

    elif data.startswith("cancel_consumir_"):
        _, _, uid_str = data.split("_", 2)
        uid = int(uid_str)
        if query.from_user.id != uid:
            await query.answer("Só o dono pode cancelar!", show_alert=True)
            return
        await query.edit_message_text("❌ Consumo cancelado.")

async def dano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
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
        await update.message.reply_text("❌ Alvo não encontrado. Peça para a pessoa usar /start.")
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
            trauma = random.choice(TRAUMAS)
            msg += f"\n😵 Trauma severo! {trauma}"
        await update.message.reply_text(msg)
        
async def autodano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
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
        await update.message.reply_text("Use /start primeiro!")
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
            trauma = random.choice(TRAUMAS)
            msg += f"\n😵 Trauma severo! {trauma}"
        await update.message.reply_text(msg)

async def cura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /cura @jogador NomeDoKit")
        return
    alvo_tag = context.args[0]
    alvo_id = username_to_id(alvo_tag)
    if not alvo_id:
        await update.message.reply_text("❌ Jogador não encontrado. Peça para a pessoa usar /start.")
        return

    kit_nome = " ".join(context.args[1:]).strip()
    key = kit_nome.lower()
    bonus_kit = KIT_BONUS.get(key)
    if bonus_kit is None:
        await update.message.reply_text("❌ Kit inválido. Use: Kit Básico, Kit Intermediário ou Kit Avançado.")
        return

    healer = get_player(uid)
    cat = get_catalog_item(kit_nome)
    inv_nome = cat['nome'] if cat else kit_nome
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade,peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
    row = c.fetchone()
    if not row or row[0] <= 0:
        await update.message.reply_text(f"❌ Você não possui '{kit_nome}' no inventário.")
        conn.close()
        return
    nova = row[0] - 1
    if nova <= 0:
        c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
    else:
        c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (nova, uid, inv_nome))
    conn.commit()
    conn.close()

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

async def autocura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /autocura NomeDoKit")
        return

    kit_nome = " ".join(context.args).strip()
    key = kit_nome.lower()
    bonus_kit = KIT_BONUS.get(key)
    if bonus_kit is None:
        await update.message.reply_text("❌ Kit inválido. Use: Kit Básico, Kit Intermediário ou Kit Avançado.")
        return

    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return

    # Verifica se tem o kit no inventário
    cat = get_catalog_item(kit_nome)
    inv_nome = cat['nome'] if cat else kit_nome
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade,peso FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
    row = c.fetchone()
    if not row or row[0] <= 0:
        await update.message.reply_text(f"❌ Você não possui '{kit_nome}' no inventário.")
        conn.close()
        return
    
    # Consome o kit
    nova = row[0] - 1
    if nova <= 0:
        c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
    else:
        c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (nova, uid, inv_nome))
    conn.commit()
    conn.close()

    # Calcula a cura
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

async def terapia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    if len(context.args) < 1:
        await update.message.reply_text("Uso: /terapia @jogador")
        return
    alvo_tag = context.args[0]
    alvo_id = username_to_id(alvo_tag)
    if not alvo_id:
        await update.message.reply_text("❌ Jogador não encontrado. Peça para a pessoa usar /start.")
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

async def coma(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Ei! Espere um instante antes de usar outro comando.")
        return

    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
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

async def ajudar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /ajudar @jogador NomeDoKit (Básico/Intermediário/Avançado)")
        return
    alvo_tag = context.args[0]
    alvo_id = username_to_id(alvo_tag)
    if not alvo_id:
        await update.message.reply_text("❌ Jogador não encontrado. Peça para a pessoa usar /start.")
        return

    alvo = get_player(alvo_id)
    if alvo['hp'] > 0:
        await update.message.reply_text("❌ O alvo não está em coma no momento.")
        return

    kit_nome = " ".join(context.args[1:]).strip()
    key = kit_nome.lower()
    bonus = KIT_BONUS.get(key)
    if bonus is None:
        await update.message.reply_text("❌ Kit inválido. Use: Kit Básico, Kit Intermediário ou Kit Avançado.")
        return

    cat = get_catalog_item(kit_nome)
    inv_nome = cat['nome'] if cat else kit_nome
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
    row = c.fetchone()
    if not row or row[0] <= 0:
        await update.message.reply_text(f"❌ Você não possui '{kit_nome}' no inventário.")
        conn.close()
        return
    nova = row[0] - 1
    if nova <= 0:
        c.execute("DELETE FROM inventario WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (uid, inv_nome))
    else:
        c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND LOWER(nome)=LOWER(%s)", (nova, uid, inv_nome))
    conn.commit()
    conn.close()

    add_coma_bonus(alvo_id, bonus)
    await update.message.reply_text(
        f"🤝 {mention(update.effective_user)} usou {kit_nome} em {alvo_tag}!\nBônus aplicado ao próximo teste de coma: +{bonus}.")

async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE, consumir_reroll=False):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return False

    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    player = get_player(uid)
    if not player or len(context.args) < 1:
        await update.message.reply_text("Uso: /roll nome_da_pericia_ou_atributo")
        return False

    key = " ".join(context.args)
    key_norm = normalizar(key)

    bonus = 0
    found = False
    real_key = key
    penal = 0

    if key_norm in ATRIBUTOS_NORMAL:
        real_key = ATRIBUTOS_NORMAL[key_norm]
        bonus += player['atributos'].get(real_key, 0)
        found = True
        if real_key in ("Força", "Destreza"):
            penal = penalidade_sobrecarga(player)
            bonus += penal
    elif key_norm in PERICIAS_NORMAL:
        real_key = PERICIAS_NORMAL[key_norm]
        bonus += player['pericias'].get(real_key, 0)
        found = True
        if real_key == "Furtividade":
            penal = penalidade_sobrecarga(player)
            bonus += penal
    else:
        await update.message.reply_text(
            "❌ Perícia/atributo não encontrado.\nVeja os nomes válidos em /ficha."
        )
        return False

    dados = roll_dados()
    total = sum(dados) + bonus
    res = resultado_roll(sum(dados))
    penal_msg = f" (Penalidade de sobrecarga: {penal})" if penal else ""
    await update.message.reply_text(
        f"🎲 /roll {real_key}\nRolagens: {dados} → {sum(dados)}\nBônus: +{bonus}{penal_msg}\nTotal: {total} → {res}"
    )
    return True

async def reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return

    if player['rerolls'] <= 0:
        await update.message.reply_text("❌ Você não tem rerolls disponíveis hoje!")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /reroll nome_da_pericia_ou_atributo")
        return

    # Executa a rolagem normal
    ok = await roll(update, context, consumir_reroll=True)

    if ok:
        # Diminui 1 reroll
        novos_rerolls = player['rerolls'] - 1
        update_player_field(uid, 'rerolls', novos_rerolls)

        await update.message.reply_text(
            f"🔄 Reroll usado! Rerolls restantes: {novos_rerolls}"
        )

async def xp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    semana = semana_atual()
    conn = get_conn()
    c = conn.cursor()
    # XP total + streak
    c.execute("SELECT xp_total, streak_atual FROM xp_semana WHERE player_id=%s AND semana_inicio=%s", (uid, semana))
    row = c.fetchone()
    xp_total = row[0] if row else 0
    streak = row[1] if row else 0
    # Turnos por dia
    c.execute("SELECT data, caracteres, mencoes FROM turnos WHERE player_id=%s AND data >= %s ORDER BY data", (uid, semana))
    dias = c.fetchall()
    lines = [f"📊 <b>Seu XP semanal:</b> {xp_total} XP", f"Streak atual: {streak} dias"]
    for d in dias:
        data, chars, menc = d
        xp_chars = xp_por_caracteres(chars)
        lines.append(f"📅 {data.strftime('%d/%m')}: {xp_chars} XP ({chars} caracteres)" + (f" | Menções: {menc}" if menc else ""))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    keyboard = [[InlineKeyboardButton("Ver ranking semanal 🏆", callback_data="ver_ranking")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Veja o ranking semanal:", reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "ver_ranking":
        await ranking(update, context)
        await query.answer()

async def ranking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    semana = semana_atual()
    conn = get_conn()
    c = conn.cursor()

    # Top 10 da semana
    c.execute("""
        SELECT player_id, xp_total, streak_atual
        FROM xp_semana
        WHERE semana_inicio=%s
        ORDER BY xp_total DESC
        LIMIT 10
    """, (semana,))
    top = c.fetchall()

    # Ranking completo para achar posição do player
    c.execute("""
        SELECT player_id, xp_total, streak_atual
        FROM xp_semana
        WHERE semana_inicio=%s
        ORDER BY xp_total DESC
    """, (semana,))
    ranking_full = c.fetchall()
    conn.close()

    players = {pid: get_player(pid) for pid, _, _ in ranking_full}

    uid = update.effective_user.id
    lines = ["🏆 <b>Ranking semanal (Top 10)</b>"]
    medals = ['🥇', '🥈', '🥉']

    for idx, (pid, xp, streak) in enumerate(top):
        nome = players[pid]['nome'] if players.get(pid) else f"ID:{pid}"
        medal = medals[idx] if idx < len(medals) else f"{idx+1}."
        highlight = " <b>(Você)</b>" if pid == uid else ""
        lines.append(f"{medal} <b>{nome}</b> — {xp} XP | 🔥 Streak: {streak}d{highlight}")

    if not top:
        lines.append("Ninguém tem XP ainda nesta semana!")

    # Se o jogador não estiver no Top 10, mostra posição separada
    if uid not in [pid for pid, _, _ in top]:
        for pos, (pid, xp, streak) in enumerate(ranking_full, start=1):
            if pid == uid:
                nome = players[pid]['nome'] if players.get(pid) else f"ID:{pid}"
                lines.append(
                    f"\n➡️ Sua posição: {pos}º — <b>{nome}</b> — {xp} XP | 🔥 Streak: {streak}d"
                )
                break

    text = "\n".join(lines)

    # Responde certo dependendo da origem
    if update.message:  # comando /ranking
        await update.message.reply_text(text, parse_mode="HTML")
    elif update.callback_query:  # botão
        await update.callback_query.message.reply_text(text, parse_mode="HTML")

# ================== FLASK ==================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot online!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=10000)

# ========== MAIN ==========
def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=reset_diario_rerolls, daemon=True).start()
    threading.Thread(target=cleanup_expired_transfers, daemon=True).start()
    threading.Thread(target=thread_reset_xp, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ficha", ficha))
    app.add_handler(CommandHandler("verficha", verficha))
    app.add_handler(CommandHandler("inventario", inventario))
    app.add_handler(CommandHandler("itens", itens))
    app.add_handler(CommandHandler("additem", additem))
    app.add_handler(CommandHandler("delitem", delitem))
    app.add_handler(CommandHandler("dar", dar))
    app.add_handler(CallbackQueryHandler(transfer_callback, pattern=r'^(confirm_dar_|cancel_dar_)'))
    app.add_handler(CommandHandler("abandonar", abandonar))
    app.add_handler(CallbackQueryHandler(callback_abandonar, pattern=r'^confirm_abandonar_|^cancel_abandonar_'))
    app.add_handler(CommandHandler("consumir", consumir))
    app.add_handler(CallbackQueryHandler(callback_consumir, pattern=r'^confirm_consumir_|^cancel_consumir_'))
    app.add_handler(CommandHandler("dano", dano))
    app.add_handler(CommandHandler("autodano", autodano))
    app.add_handler(CommandHandler("cura", cura))
    app.add_handler(CommandHandler("autocura", autocura))
    app.add_handler(CommandHandler("terapia", terapia))
    app.add_handler(CommandHandler("coma", coma))
    app.add_handler(CommandHandler("ajudar", ajudar))
    app.add_handler(CommandHandler("roll", roll))
    app.add_handler(CommandHandler("reroll", reroll))
    app.add_handler(CommandHandler("editarficha", editarficha))
    app.add_handler(CommandHandler("turno", turno))
    app.add_handler(CommandHandler("xp", xp))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^ver_ranking$"))
    app.add_handler(CommandHandler("ranking", ranking))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receber_edicao))
    app.run_polling()

if __name__ == "__main__":
    main()
