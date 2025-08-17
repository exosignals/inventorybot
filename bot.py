import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import sqlite3
import os
from flask import Flask
import random
import threading
import time
from datetime import datetime, timedelta
import re

# ================== CONFIGURAÇÕES ==================
TOKEN = os.getenv("BOT_TOKEN")

# Força → Peso Máx
PESO_MAX = {1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 30}

# Anti-spam
LAST_COMMAND = {}
COOLDOWN = 1

# Limites de ficha
MAX_ATRIBUTOS = 24
MAX_PERICIAS = 42
ATRIBUTOS_LISTA = ["Força","Destreza","Constituição","Inteligência","Sabedoria","Carisma"]
PERICIAS_LISTA = ["Percepção","Persuasão","Medicina","Furtividade","Intimidação","Investigação",
                  "Armas de fogo","Armas brancas","Sobrevivência","Cultura","Intuição","Tecnologia"]

# Edição de ficha
EDIT_PENDING = {}

# ====================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----- SQLite Setup -----
DB_FILE = "players.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY,
                    nome TEXT,
                    peso_max INTEGER DEFAULT 0,
                    hp INTEGER DEFAULT 20,
                    sp INTEGER DEFAULT 20,
                    rerolls INTEGER DEFAULT 3
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS atributos (
                    player_id INTEGER,
                    nome TEXT,
                    valor INTEGER DEFAULT 0,
                    PRIMARY KEY(player_id,nome)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS pericias (
                    player_id INTEGER,
                    nome TEXT,
                    valor INTEGER DEFAULT 0,
                    PRIMARY KEY(player_id,nome)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS inventario (
                    player_id INTEGER,
                    nome TEXT,
                    peso REAL,
                    quantidade INTEGER DEFAULT 1,
                    PRIMARY KEY(player_id,nome)
                )''')
    conn.commit()
    conn.close()

# ----- Funções utilitárias SQLite -----
def get_player(uid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE id=?", (uid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return None
    player = {
        "id": row[0],
        "nome": row[1],
        "peso_max": row[2],
        "hp": row[3],
        "sp": row[4],
        "rerolls": row[5],
        "atributos": {},
        "pericias": {},
        "inventario": []
    }
    c.execute("SELECT nome, valor FROM atributos WHERE player_id=?", (uid,))
    for a,v in c.fetchall():
        player["atributos"][a] = v
    c.execute("SELECT nome, valor FROM pericias WHERE player_id=?", (uid,))
    for a,v in c.fetchall():
        player["pericias"][a] = v
    c.execute("SELECT nome,peso,quantidade FROM inventario WHERE player_id=?", (uid,))
    for n,p,q in c.fetchall():
        player["inventario"].append({"nome": n, "peso": p, "quantidade": q})
    conn.close()
    return player

def create_player(uid, nome):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO players(id,nome) VALUES(?,?)", (uid, nome))
    for a in ATRIBUTOS_LISTA:
        c.execute("INSERT OR IGNORE INTO atributos(player_id,nome,valor) VALUES(?,?,0)", (uid,a))
    for p in PERICIAS_LISTA:
        c.execute("INSERT OR IGNORE INTO pericias(player_id,nome,valor) VALUES(?,?,0)", (uid,p))
    conn.commit()
    conn.close()

def update_player_field(uid, field, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(f"UPDATE players SET {field}=? WHERE id=?", (value, uid))
    conn.commit()
    conn.close()

def update_atributo(uid, nome, valor):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE atributos SET valor=? WHERE player_id=? AND nome=?", (valor, uid, nome))
    conn.commit()
    conn.close()

def update_pericia(uid, nome, valor):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE pericias SET valor=? WHERE player_id=? AND nome=?", (valor, uid, nome))
    conn.commit()
    conn.close()

def update_inventario(uid, item):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO inventario(player_id,nome,peso,quantidade) VALUES(?,?,?,?)",
              (uid, item['nome'], item['peso'], item['quantidade']))
    conn.commit()
    conn.close()

def remove_item(uid, item_nome):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM inventario WHERE player_id=? AND nome=?", (uid,item_nome))
    conn.commit()
    conn.close()

def peso_total(player):
    return sum(i['peso']*i.get('quantidade',1) for i in player.get("inventario",[]))

def penalidade(player):
    return peso_total(player) > player["peso_max"]

def anti_spam(user_id):
    now=time.time()
    if user_id in LAST_COMMAND and now-LAST_COMMAND[user_id]<COOLDOWN:
        return False
    LAST_COMMAND[user_id]=now
    return True

def roll_dados(qtd=4,lados=6):
    return [random.randint(1,lados) for _ in range(qtd)]

def resultado_roll(valor_total):
    if valor_total<=5: return "Fracasso crítico"
    elif valor_total<=10: return "Falha simples"
    elif valor_total<=15: return "Sucesso"
    else: return "Sucesso crítico"

# ----- Reset diário às 6h -----
def reset_diario_rerolls():
    while True:
        now = datetime.now()
        next_reset = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= next_reset:
            next_reset += timedelta(days=1)
        wait_seconds = (next_reset - now).total_seconds()
        time.sleep(wait_seconds)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE players SET rerolls=3")
        conn.commit()
        conn.close()
        logger.info("🔄 Rerolls diários resetados!")

# ================== COMANDOS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    nome = update.effective_user.first_name
    create_player(uid, nome)
    await update.message.reply_text(f"Olá {nome}! Sua ficha foi criada.")

async def ficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Você ainda não tem ficha. Use /start")
        return
    text = f"📜 Ficha de {player['nome']}:\nHP: {player['hp']}\nSP: {player['sp']}\nRerolls: {player['rerolls']}\nPeso Máx: {player['peso_max']}\nPeso Atual: {peso_total(player)}"
    text += "\n\n💪 Atributos:\n" + "\n".join([f"{a}: {v}" for a,v in player['atributos'].items()])
    text += "\n\n🧠 Perícias:\n" + "\n".join([f"{p}: {v}" for p,v in player['pericias'].items()])
    await update.message.reply_text(text)

async def inv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player: 
        await update.message.reply_text("Você ainda não tem ficha. Use /start")
        return
    if not player['inventario']:
        await update.message.reply_text("Inventário vazio.")
        return
    text = "🎒 Inventário:\n" + "\n".join([f"{i['nome']} x{i['quantidade']} (Peso: {i['peso']})" for i in player['inventario']])
    if penalidade(player):
        text += "\n⚠️ Você está carregando peso demais!"
    await update.message.reply_text(text)

async def dar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Use /dar @usuario nome_do_item")
        return
    match = re.match(r"@?(\w+)", context.args[0])
    if not match:
        await update.message.reply_text("Mencione o usuário corretamente.")
        return
    username = match.group(1)
    item_nome = " ".join(context.args[1:])
    # Pegando o usuário pelo username (simplificado: ID = hash do nome)
    uid_destino = hash(username) % (10**8)
    create_player(uid_destino, username)
    uid_origem = update.effective_user.id
    player_origem = get_player(uid_origem)
    if not player_origem:
        await update.message.reply_text("Você não tem ficha.")
        return
    item = next((i for i in player_origem['inventario'] if i['nome'].lower() == item_nome.lower()), None)
    if not item:
        await update.message.reply_text("Você não possui esse item.")
        return
    player_origem['inventario'].remove(item)
    update_inventario(uid_origem, item)
    update_inventario(uid_destino, item)
    await update.message.reply_text(f"✅ Item {item_nome} enviado para {username}.")

async def dano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args)<1: 
        await update.message.reply_text("Use /dano valor")
        return
    uid = update.effective_user.id
    player = get_player(uid)
    if not player: return
    player['hp'] -= int(context.args[0])
    update_player_field(uid,'hp',player['hp'])
    await update.message.reply_text(f"💔 Você perdeu {context.args[0]} HP. HP atual: {player['hp']}")

async def cura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args)<1: 
        await update.message.reply_text("Use /cura valor")
        return
    uid = update.effective_user.id
    player = get_player(uid)
    if not player: return
    player['hp'] += int(context.args[0])
    update_player_field(uid,'hp',player['hp'])
    await update.message.reply_text(f"💖 Você recuperou {context.args[0]} HP. HP atual: {player['hp']}")

async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dados = roll_dados()
    total = sum(dados)
    await update.message.reply_text(f"🎲 Rolagem: {dados}\nTotal: {total}\nResultado: {resultado_roll(total)}")

async def reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player: return
    if player['rerolls']<=0:
        await update.message.reply_text("❌ Você não tem rerolls disponíveis.")
        return
    player['rerolls'] -= 1
    update_player_field(uid,'rerolls',player['rerolls'])
    dados = roll_dados()
    total = sum(dados)
    await update.message.reply_text(f"🎲 Reroll: {dados}\nTotal: {total}\nResultado: {resultado_roll(total)}")

async def editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args)<2:
        await update.message.reply_text("Use /editar atributo valor")
        return
    nome_campo = context.args[0]
    valor = int(context.args[1])
    uid = update.effective_user.id
    EDIT_PENDING[uid] = (nome_campo, valor)
    await update.message.reply_text(f"Confirme a edição digitando qualquer mensagem.")

async def receber_edicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in EDIT_PENDING: return
    campo, valor = EDIT_PENDING.pop(uid)
    player = get_player(uid)
    if campo in ATRIBUTOS_LISTA:
        update_atributo(uid, campo, valor)
    elif campo in PERICIAS_LISTA:
        update_pericia(uid, campo, valor)
    else:
        await update.message.reply_text("Campo inválido.")
        return
    await update.message.reply_text(f"{campo} atualizado para {valor}.")

# ================== Flask ==================
flask_app=Flask(__name__)
@flask_app.route("/")
def home(): return "Bot online!"
def run_flask(): flask_app.run(host="0.0.0.0",port=10000)

# ================== MAIN ==================
def main():
    init_db()
    threading.Thread(target=run_flask).start()
    threading.Thread(target=reset_diario_rerolls, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ficha", ficha))
    app.add_handler(CommandHandler("inv", inv))
    app.add_handler(CommandHandler("dar", dar))
    app.add_handler(CommandHandler("dano", dano))
    app.add_handler(CommandHandler("cura", cura))
    app.add_handler(CommandHandler("roll", roll))
    app.add_handler(CommandHandler("reroll", reroll))
    app.add_handler(CommandHandler("editar", editar))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receber_edicao))
    app.run_polling()

if __name__=="__main__":
    main()
