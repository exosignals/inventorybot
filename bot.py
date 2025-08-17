import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import sqlite3
import os
from flask import Flask
import random
import threading
import asyncio
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

# ====================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----- SQLite Setup -----
DB_FILE = "players.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Jogadores
    c.execute('''CREATE TABLE IF NOT EXISTS players (
                    id INTEGER PRIMARY KEY,
                    nome TEXT,
                    peso_max INTEGER DEFAULT 0,
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

# ----- Reset diário assíncrono -----
async def reset_diario_rerolls_async():
    while True:
        now = datetime.now()
        next_reset = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= next_reset:
            next_reset += timedelta(days=1)
        wait_seconds = (next_reset - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE players SET rerolls=3")
        conn.commit()
        conn.close()
        logger.info("🔄 Rerolls diários resetados!")

# ================== COMANDOS ==================
# [Aqui você mantém todos os seus comandos: start, ficha, inv, dar, dano, cura, roll, reroll, editar, receber_edicao]
# Mantenha exatamente como já está no seu código anterior, nada muda.

# ================== Flask ==================
flask_app=Flask(__name__)
@flask_app.route("/")
def home(): return "Bot online!"
def run_flask(): flask_app.run(host="0.0.0.0",port=10000)

# ================== MAIN ==================
def main():
    init_db()
    # Inicia Flask
    threading.Thread(target=run_flask).start()
    
    # Inicia Telegram
    app = Application.builder().token(TOKEN).build()

    # Handlers
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

    # Agendar reset diário assíncrono
    asyncio.create_task(reset_diario_rerolls_async())
    
    app.run_polling()

if __name__=="__main__":
    main()
