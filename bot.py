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

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== SQLITE ==================
DB_FILE = "players.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Jogadores
    c.execute('''CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY,
                    nome TEXT,
                    peso_max INTEGER DEFAULT 15,
                    hp INTEGER DEFAULT 20,
                    sp INTEGER DEFAULT 20,
                    rerolls INTEGER DEFAULT 3
                )''')
    # Atributos
    c.execute('''CREATE TABLE IF NOT EXISTS atributos (
                    player_id INTEGER,
                    nome TEXT,
                    valor INTEGER DEFAULT 0,
                    PRIMARY KEY(player_id,nome)
                )''')
    # Perícias
    c.execute('''CREATE TABLE IF NOT EXISTS pericias (
                    player_id INTEGER,
                    nome TEXT,
                    valor INTEGER DEFAULT 0,
                    PRIMARY KEY(player_id,nome)
                )''')
    # Inventário
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
    # Atributos
    c.execute("SELECT nome, valor FROM atributos WHERE player_id=?", (uid,))
    for a,v in c.fetchall():
        player["atributos"][a] = v
    # Perícias
    c.execute("SELECT nome, valor FROM pericias WHERE player_id=?", (uid,))
    for a,v in c.fetchall():
        player["pericias"][a] = v
    # Inventário
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

# ----- Função de reset diário às 6h -----
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
    if not get_player(uid):
        create_player(uid, nome)
    await update.message.reply_text(
        f"🎲 Bem-vindo, {nome}!\n"
        "Este bot gerencia sua ficha de RPG, inventário, HP e SP.\n"
        "Use /ficha para preencher seus atributos e perícias.\n"
        "Após criar a ficha, você poderá usar /roll, /reroll, /dar, /dano, /cura e /terapia.\n"
        "Boa aventura!"
    )

async def ficha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Você precisa usar /start primeiro!")
        return
    text = "📝 Ficha de RPG\n\n🔹 Atributos (máx 24 pontos):\n"
    for a in ATRIBUTOS_LISTA:
        val = player["atributos"].get(a,0)
        text += f"- {a} (1-6): {val}\n"
    text += "\n🔹 Perícias (máx 42 pontos):\n"
    for p in PERICIAS_LISTA:
        val = player["pericias"].get(p,0)
        text += f"- {p} (1-6): {val}\n"
    text += f"\nHP: {player['hp']}\nSP: {player['sp']}\n"
    total_peso = peso_total(player)
    text += f"\n📦 Peso total do inventário: {total_peso}/{player['peso_max']}"
    if penalidade(player):
        text += " ⚠️ Sobrecarregado!"
    await update.message.reply_text(text)

# Comando /editar - envia ficha atual para edição e processa respostas
async def editar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return
    EDIT_PENDING[uid] = True
    text = "✏️ Edite sua ficha respondendo apenas os valores que deseja alterar no formato:\n"
    text += "Força: 3\nDestreza: 4\n...\nPercepção: 5\n..."
    await update.message.reply_text(text)

async def receber_edicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in EDIT_PENDING:
        return
    text = update.message.text
    player = get_player(uid)
    for line in text.splitlines():
        if ':' in line:
            key,val = line.split(':')
            key = key.strip()
            try:
                val = int(val.strip())
            except:
                continue
            if key in ATRIBUTOS_LISTA:
                player["atributos"][key] = val
                update_atributo(uid,key,val)
            elif key in PERICIAS_LISTA:
                player["pericias"][key] = val
                update_pericia(uid,key,val)
    EDIT_PENDING.pop(uid)
    await update.message.reply_text("✅ Ficha atualizada!")

# /inv
async def inv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return
    text = f"📦 Inventário de {player['nome']}:\n"
    for i in player['inventario']:
        text += f"- {i['nome']} x{i['quantidade']} ({i['peso']}kg cada)\n"
    text += f"Peso total: {peso_total(player)}/{player['peso_max']}"
    if penalidade(player):
        text += " ⚠️ Sobrecarregado!"
    await update.message.reply_text(text)

# /dar @jogador item qtd
async def dar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args)<3:
        await update.message.reply_text("Use /dar @jogador nome_do_item quantidade")
        return
    uid_from = update.effective_user.id
    item_nome = ' '.join(context.args[1:-1])
    try:
        qtd = int(context.args[-1])
    except:
        await update.message.reply_text("Quantidade inválida!")
        return
    user_tag = context.args[0]
    if not user_tag.startswith('@'):
        await update.message.reply_text("Mencione o jogador corretamente (@nome)")
        return
    # Buscar player destino pelo username
    await update.message.reply_text(f"💡 Transferência simulada: {item_nome} x{qtd} para {user_tag}\n(Implementar mapeamento real de username para UID)")
    # Aqui você faria a lógica real de transferência

# /dano e /cura
async def dano(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        await update.message.reply_text("Use /start primeiro!")
        return
    if len(context.args)==0:
        await update.message.reply_text("Use /dano hp ou /dano sp")
        return
    tipo = context.args[0].lower()
    dado = random.randint(1,6)
    if tipo=='hp':
        before = player['hp']
        player['hp'] = max(0, before - dado)
        update_player_field(uid,'hp',player['hp'])
        msg = f"{player['nome']}: HP {before} → {player['hp']} (-{dado})"
        if player['hp']==0:
            msg += "\n💀 Desmaiou! Está em coma. Use /coma."
        await update.message.reply_text(msg)
    elif tipo=='sp':
        before = player['sp']
        player['sp'] = max(0, before - dado)
        update_player_field(uid,'sp',player['sp'])
        msg = f"{player['nome']}: SP {before} → {player['sp']} (-{dado})"
        if player['sp']==0:
            msg += "\n😵 Trauma severo! Use /trauma."
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("Tipo inválido! Use hp ou sp.")

async def cura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player or len(context.args)<1:
        await update.message.reply_text("Use /cura @jogador Kit_Básico +2")
        return
    dado = random.randint(1,6)
    bonus = 0
    if "Medicina" in player['pericias']:
        bonus = player['pericias']["Medicina"]
    total = dado + bonus
    before = player['hp']
    player['hp'] = min(20, before + total)
    update_player_field(uid,'hp',player['hp'])
    await update.message.reply_text(f"{player['nome']}: HP {before} → {player['hp']} (+{total})")

async def terapia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player or len(context.args)<1:
        await update.message.reply_text("Use /terapia @jogador +3")
        return
    dado = random.randint(1,6)
    bonus = 0
    if "Persuasão" in player['pericias']:
        bonus = player['pericias']["Persuasão"]
    total = dado + bonus
    before = player['sp']
    player['sp'] = min(20, before + total)
    update_player_field(uid,'sp',player['sp'])
    await update.message.reply_text(f"{player['nome']}: SP {before} → {player['sp']} (+{total})")

# /roll e /reroll
async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player or len(context.args)<1:
        await update.message.reply_text("Use /roll nome_da_pericia_ou_atributo")
        return
    key = ' '.join(context.args)
    bonus = player['atributos'].get(key,0) + player['pericias'].get(key,0)
    total = sum(roll_dados()) + bonus
    res = resultado_roll(total)
    await update.message.reply_text(f"{key}: {total} → {res}")

async def reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    player = get_player(uid)
    if not player:
        return
    if player['rerolls']<=0:
        await update.message.reply_text("Você não tem rerolls disponíveis hoje!")
        return
    await roll(update, context)
    update_player_field(uid,'rerolls',player['rerolls']-1)

# ================== FLASK ==================
flask_app = Flask(__name__)
@flask_app.route("/")
def home(): return "Bot online!"
def run_flask(): flask_app.run(host="0.0.0.0",port=10000)

# ================== MAIN ==================
def main():
    init_db()
    threading.Thread(target=run_flask).start()
    threading.Thread(target=reset_diario_rerolls, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ficha", ficha))
    app.add_handler(CommandHandler("inv", inv))
    app.add_handler(CommandHandler("dar", dar))
    app.add_handler(CommandHandler("dano", dano))
    app.add_handler(CommandHandler("cura", cura))
    app.add_handler(CommandHandler("terapia", terapia))
    app.add_handler(CommandHandler("roll", roll))
    app.add_handler(CommandHandler("reroll", reroll))
    app.add_handler(CommandHandler("editar", editar))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), receber_edicao))
    app.run_polling()

if __name__=="__main__":
    main()
