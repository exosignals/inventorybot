import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
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
PESO_MAX = {1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 30}
LAST_COMMAND = {}
COOLDOWN = 1

MAX_ATRIBUTOS = 24
MAX_PERICIAS = 42
ATRIBUTOS_LISTA = ["Força","Destreza","Constituição","Inteligência","Sabedoria","Carisma"]
PERICIAS_LISTA = ["Percepção","Persuasão","Medicina","Furtividade","Intimidação","Investigação",
                  "Armas de fogo","Armas brancas","Sobrevivência","Cultura","Intuição","Tecnologia"]
ATRIBUTOS_NORMAL = {normalizar(a): a for a in ATRIBUTOS_LISTA}
PERICIAS_NORMAL = {normalizar(p): p for p in PERICIAS_LISTA}

EDIT_PENDING = {}

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
                    peso_max INTEGER DEFAULT 15,
                    hp INTEGER DEFAULT 20,
                    sp INTEGER DEFAULT 20,
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
    conn.commit()
    c.execute("UPDATE players SET rerolls=3")
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
        "sp": row["sp"],
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
    c.execute("INSERT INTO inventario(player_id,nome,peso,quantidade) VALUES(%s,%s,%s,%s) ON CONFLICT (player_id, nome) DO UPDATE SET peso=%s, quantidade=%s",
        (uid, item['nome'], item['peso'], item['quantidade'], item['peso'], item['quantidade']))
    conn.commit()
    conn.close()

def adjust_item_quantity(uid, item_nome, delta):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade, peso FROM inventario WHERE player_id=%s AND nome=%s", (uid, item_nome))
    row = c.fetchone()
    if not row:
        conn.close()
        return False
    qtd, peso = row
    nova = qtd + delta
    if nova <= 0:
        c.execute("DELETE FROM inventario WHERE player_id=%s AND nome=%s", (uid, item_nome))
    else:
        c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND nome=%s", (nova, uid, item_nome))
    conn.commit()
    conn.close()
    return True

def get_catalog_item(nome: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT nome,peso FROM catalogo WHERE LOWER(nome)=LOWER(%s)", (nome,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {"nome": row[0], "peso": row[1]}

def add_catalog_item(nome: str, peso: float):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO catalogo(nome,peso) VALUES(%s,%s) ON CONFLICT (nome) DO UPDATE SET peso=%s", (nome, peso, peso))
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
    c.execute("SELECT nome,peso FROM catalogo ORDER BY nome COLLATE \"C\"")
    data = c.fetchall()
    conn.close()
    return data

def remove_item(uid, item_nome):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM inventario WHERE player_id=%s AND nome=%s", (uid, item_nome))
    conn.commit()
    conn.close()

def peso_total(player):
    return sum(i['peso'] * i.get('quantidade', 1) for i in player.get("inventario", []))

def penalidade(player):
    return peso_total(player) > player["peso_max"]

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
    elif valor_total <= 10:
        return "Falha simples"
    elif valor_total <= 15:
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
        novo = PESO_MAX.get(valor_forca, 15)
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

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def mention(user):
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Jogador"

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    nome = update.effective_user.first_name
    username = update.effective_user.username
    if not get_player(uid):
        create_player(uid, nome, username)
    register_username(uid, username, nome)
    await update.message.reply_text(
        f"🎲 Bem-vindo, {nome}!\n"
        "Este bot gerencia sua ficha de RPG, inventário, HP e SP.\n"
        "Use /ficha para ver sua ficha. Para editar, use /editarficha.\n"
        "Comandos úteis: /inventario, /itens, /dar, /cura, /terapia, /coma, /ajudar.\n"
        "Boa aventura!"
    )

async def ficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Você precisa usar /start primeiro!")
        return
    text = "📝 Ficha de RPG\n\n🔹 Atributos (máx 24 pontos):\n"
    for a in ATRIBUTOS_LISTA:
        val = player["atributos"].get(a, 0)
        text += f"- {a} (1-6): {val}\n"
    text += "\n🔹 Perícias (máx 42 pontos):\n"
    for p in PERICIAS_LISTA:
        val = player["pericias"].get(p, 0)
        text += f"- {p} (1-6): {val}\n"
    text += f"\n❤️ HP: {player['hp']}\n🧠 SP: {player['sp']}\n"
    total_peso = peso_total(player)
    sobre = " ⚠️ Sobrecarregado!" if penalidade(player) else ""
    text += f"\n📦 Peso total do inventário: {total_peso:.1f}/{player['peso_max']}{sobre}\n\n"
    text += "Para editar a ficha, use /editarficha"
    await update.message.reply_text(text)

async def editarficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return

    EDIT_PENDING[uid] = True
    text = "✏️ Edite sua ficha respondendo apenas os valores que deseja alterar no formato:\n"
    text += "Força: 3\nDestreza: 4\n...\nPercepção: 5\n...\n\nLimites: atributos somam até 24, perícias até 42; cada campo entre 1–6."
    await update.message.reply_text(text)

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
            await update.message.reply_text(f"❌ Formato inválido na linha: {linha}")
            return

        if key in ATRIBUTOS_NORMAL:
            key_real = ATRIBUTOS_NORMAL[key]
            if val < 1 or val > 6:
                await update.message.reply_text("❌ Atributos devem estar entre 1 e 6.")
                return
            soma_atributos = sum(EDIT_TEMP.get(a, 0) for a in ATRIBUTOS_LISTA if a != key_real) + val
            if soma_atributos > MAX_ATRIBUTOS:
                await update.message.reply_text("❌ Total de pontos de atributos excede 24.")
                return
            EDIT_TEMP[key_real] = val

        elif key in PERICIAS_NORMAL:
            key_real = PERICIAS_NORMAL[key]
            if val < 1 or val > 6:
                await update.message.reply_text("❌ Perícias devem estar entre 1 e 6.")
                return
            soma_pericias = sum(EDIT_TEMP.get(p, 0) for p in PERICIAS_LISTA if p != key_real) + val
            if soma_pericias > MAX_PERICIAS:
                await update.message.reply_text("❌ Total de pontos de perícias excede 42.")
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

    await update.message.reply_text("✅ Ficha atualizada com sucesso!")
    EDIT_PENDING.pop(uid, None)

async def inventario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return
    lines = [f"📦 Inventário de {player['nome']}:"]
    if not player['inventario']:
        lines.append("(vazio)")
    else:
        for i in sorted(player['inventario'], key=lambda x: x['nome'].lower()):
            lines.append(f"- {i['nome']} x{i['quantidade']} ({i['peso']:.2f} kg cada)")
    total_peso = peso_total(player)
    lines.append(f"\nPeso total: {total_peso:.1f}/{player['peso_max']} kg")
    if penalidade(player):
        excesso = total_peso - player['peso_max']
        lines.append(f"⚠️ Sobrecarregado em {excesso:.1f} kg!")
    await update.message.reply_text("\n".join(lines))

async def itens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    data = list_catalog()
    if not data:
        await update.message.reply_text("📚 Catálogo vazio. Use /additem Nome Peso para adicionar.")
        return
    lines = ["📚 Catálogo de Itens:"]
    for nome, peso in data:
        lines.append(f"- {nome} ({peso:.2f} kg)")
    await update.message.reply_text("\n".join(lines))

async def additem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Apenas administradores podem usar este comando.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /additem NomeDoItem Peso\nEx.: /additem Escopeta 3,5 kg")
        return
    peso_str = context.args[-1]
    nome = " ".join(context.args[:-1])
    peso = parse_float_br(peso_str)
    if not peso:
        await update.message.reply_text("❌ Peso inválido. Use algo como 2,5 kg.")
        return
    add_catalog_item(nome, peso)
    await update.message.reply_text(f"✅ Item '{nome}' adicionado ao catálogo com {peso:.2f} kg. (Inventário de mestre é virtual e inesgotável.)")

async def delitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
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

async def dar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /dar @jogador Nome_do_item [x quantidade]")
        return
    uid_from = update.effective_user.id
    register_username(uid_from, update.effective_user.username, update.effective_user.first_name)
    user_tag = context.args[0]
    target_id = username_to_id(user_tag)
    if not target_id:
        await update.message.reply_text("❌ Jogador não encontrado. Peça para a pessoa usar /start pelo menos uma vez.")
        return

    qtd = 1
    tail = context.args[1:]
    if len(tail) >= 2 and tail[-2].lower() == 'x' and tail[-1].isdigit():
        qtd = int(tail[-1])
        item_nome = " ".join(tail[:-2])
    elif len(tail) >= 1 and tail[-1].isdigit():
        qtd = int(tail[-1])
        item_nome = " ".join(tail[:-1])
    else:
        item_nome = " ".join(tail)

    if qtd < 1:
        await update.message.reply_text("❌ Quantidade inválida.")
        return

    cat = get_catalog_item(item_nome)
    if not cat:
        await update.message.reply_text("❌ Item não está no catálogo. Use /itens para ver os disponíveis.")
        return

    target = get_player(target_id)
    if not target:
        await update.message.reply_text("❌ O alvo ainda não iniciou o bot (/start).")
        return

    peso_add = cat['peso'] * qtd
    total_depois = peso_total(target) + peso_add
    if total_depois > target['peso_max']:
        excesso = total_depois - target['peso_max']
        await update.message.reply_text(
            f"⚠️ {target['nome']} ficaria sobrecarregado em {excesso:.1f} kg. Item não foi adicionado.")
        return

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT quantidade FROM inventario WHERE player_id=%s AND nome=%s", (target_id, cat['nome']))
    row = c.fetchone()
    if row:
        nova = row[0] + qtd
        c.execute("UPDATE inventario SET quantidade=%s WHERE player_id=%s AND nome=%s", (nova, target_id, cat['nome']))
    else:
        c.execute("INSERT INTO inventario(player_id,nome,peso,quantidade) VALUES(%s,%s,%s,%s)",
                  (target_id, cat['nome'], cat['peso'], qtd))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Entregue: {cat['nome']} x{qtd} para {user_tag}. Peso total agora: {total_depois:.1f}/{target['peso_max']} kg.")

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
    after = min(20, before + total)
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
    after = min(20, before + total)
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
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
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

    if total <= 5:
        status = "☠️ Morte."
    elif total <= 10:
        status = "Ainda em coma."
    elif total <= 15:
        status = "Sinais de recuperação (permanece em coma, mas melhora)."
    else:
        status = "🌅 Você acorda! (HP passa a 1)"
        update_player_field(uid, 'hp', 1)

    await update.message.reply_text(
        "\n".join([
            "🧊 Teste de Coma",
            f"Rolagens: {dados} → {soma}",
            f"Bônus de ajuda: +{bonus_ajuda}",
            f"Total: {total}",
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

async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    register_username(uid, update.effective_user.username, update.effective_user.first_name)

    player = get_player(uid)
    if not player or len(context.args) < 1:
        await update.message.reply_text("Uso: /roll nome_da_pericia_ou_atributo")
        return

    key = " ".join(context.args)
    key_norm = normalizar(key)

    bonus = 0
    found = False
    real_key = key

    if key_norm in ATRIBUTOS_NORMAL:
        real_key = ATRIBUTOS_NORMAL[key_norm]
        bonus += player['atributos'].get(real_key, 0)
        found = True
    elif key_norm in PERICIAS_NORMAL:
        real_key = PERICIAS_NORMAL[key_norm]
        bonus += player['pericias'].get(real_key, 0)
        found = True
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
    
async def reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not anti_spam(update.effective_user.id):
        await update.message.reply_text("⏳ Espere um instante antes de usar outro comando.")
        return
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return
    if player['rerolls'] <= 0:
        await update.message.reply_text("Você não tem rerolls disponíveis hoje!")
        return
    update_player_field(uid, 'rerolls', player['rerolls'] - 1)
    await roll(update, context)

# ================== FLASK ==================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot online!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=10000)

# ================== MAIN ==================
def main():
    init_db()
    threading.Thread(target=run_flask).start()
    threading.Thread(target=reset_diario_rerolls, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ficha", ficha))
    app.add_handler(CommandHandler("inventario", inventario))
    app.add_handler(CommandHandler("itens", itens))
    app.add_handler(CommandHandler("additem", additem))
    app.add_handler(CommandHandler("delitem", delitem))
    app.add_handler(CommandHandler("dar", dar))
    app.add_handler(CommandHandler("dano", dano))
    app.add_handler(CommandHandler("cura", cura))
    app.add_handler(CommandHandler("terapia", terapia))
    app.add_handler(CommandHandler("coma", coma))
    app.add_handler(CommandHandler("ajudar", ajudar))
    app.add_handler(CommandHandler("roll", roll))
    app.add_handler(CommandHandler("reroll", reroll))
    app.add_handler(CommandHandler("editarficha", editarficha))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receber_edicao))
    app.run_polling()

if __name__ == "__main__":
    main()
