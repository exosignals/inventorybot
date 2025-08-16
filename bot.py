import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import json
import os
from flask import Flask
import threading

# ================== CONFIGURAÇÕES ==================
TOKEN = os.getenv("BOT_TOKEN")  # Defina no Render como variável de ambiente BOT_TOKEN
DATA_FILE = "players.json"

# Força → Peso Máx
PESO_MAX = {1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 30}

# ====================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----- Funções utilitárias -----
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def peso_total(player):
    return sum(item["peso"] for item in player.get("inventario", []))

def penalidade(player):
    return peso_total(player) > player["peso_max"]

# ----- Comandos do bot -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()
    if str(user.id) not in data:
        data[str(user.id)] = {"nome": user.username or user.first_name,
                              "forca": None,
                              "peso_max": 0,
                              "inventario": []}
        save_data(data)
        await update.message.reply_text(
            "Bem-vindo ao inventário! Use /forca <número de 1 a 6> para definir sua força."
        )
    else:
        await update.message.reply_text("Você já está registrado! Use /forca para atualizar sua força.")

async def forca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()
    if str(user.id) not in data:
        await update.message.reply_text("Use /start primeiro.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /forca <1-6>")
        return
    try:
        f = int(context.args[0])
        if f < 1 or f > 6:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Digite um valor entre 1 e 6.")
        return
    data[str(user.id)]["forca"] = f
    data[str(user.id)]["peso_max"] = PESO_MAX[f]
    save_data(data)
    await update.message.reply_text(f"Força definida como {f}. Limite de carga: {PESO_MAX[f]}kg.")

async def additem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()
    if str(user.id) not in data or not data[str(user.id)]["forca"]:
        await update.message.reply_text("Use /start e /forca primeiro.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /additem <nome> <peso>")
        return
    try:
        peso = int(context.args[-1])
        nome = " ".join(context.args[:-1])
    except ValueError:
        await update.message.reply_text("O peso deve ser um número inteiro.")
        return

    data[str(user.id)]["inventario"].append({"nome": nome, "peso": peso})
    save_data(data)
    total = peso_total(data[str(user.id)])
    msg = f"Item '{nome}' adicionado ({peso}kg). Peso total: {total}/{data[str(user.id)]['peso_max']}kg."
    if penalidade(data[str(user.id)]):
        msg += "\n⚠️ Você está sobrecarregado! Penalidade aplicada."
    await update.message.reply_text(msg)

async def inv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()
    if str(user.id) not in data:
        await update.message.reply_text("Use /start primeiro.")
        return
    player = data[str(user.id)]
    if not player["inventario"]:
        await update.message.reply_text("Seu inventário está vazio.")
        return
    itens = "\n".join([f"- {i['nome']} ({i['peso']}kg)" for i in player["inventario"]])
    total = peso_total(player)
    msg = f"📦 Inventário de {player['nome']}:\n{itens}\n\nPeso total: {total}/{player['peso_max']}kg."
    if penalidade(player):
        msg += "\n⚠️ Você está sobrecarregado! Penalidade aplicada."
    await update.message.reply_text(msg)

async def droparitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()
    if str(user.id) not in data:
        await update.message.reply_text("Use /start primeiro.")
        return
    if not context.args:
        await update.message.reply_text("Uso: /droparitem <nome>")
        return
    nome = " ".join(context.args)
    player = data[str(user.id)]
    for item in player["inventario"]:
        if item["nome"].lower() == nome.lower():
            player["inventario"].remove(item)
            save_data(data)
            await update.message.reply_text(f"Você dropou '{nome}'.")
            return
    await update.message.reply_text(f"Item '{nome}' não encontrado no inventário.")

async def trocar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()
    if str(user.id) not in data:
        await update.message.reply_text("Use /start primeiro.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso: /trocar <@usuario> <nome do item>")
        return

    alvo_username = context.args[0].replace("@", "")
    nome_item = " ".join(context.args[1:])
    player = data[str(user.id)]
    item_transfer = None
    for item in player["inventario"]:
        if item["nome"].lower() == nome_item.lower():
            item_transfer = item
            break
    if not item_transfer:
        await update.message.reply_text(f"Você não tem o item '{nome_item}'.")
        return

    # Procurar pelo alvo
    alvo_id = None
    for pid, pdata in data.items():
        if pdata["nome"] == alvo_username:
            alvo_id = pid
            break
    if not alvo_id:
        await update.message.reply_text("Esse jogador não está registrado ou usou outro nome.")
        return

    # Transferência
    player["inventario"].remove(item_transfer)
    data[alvo_id]["inventario"].append(item_transfer)
    save_data(data)
    await update.message.reply_text(
        f"Você entregou '{item_transfer['nome']}' para @{alvo_username}."
    )

# ----- Flask para manter a porta aberta -----
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot online!"

def run_flask():
    flask_app.run(host="0.0.0.0", port=10000)

# Inicia o Flask em background
threading.Thread(target=run_flask).start()

# ----- Main do bot -----
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("forca", forca))
    app.add_handler(CommandHandler("additem", additem))
    app.add_handler(CommandHandler("inv", inv))
    app.add_handler(CommandHandler("droparitem", droparitem))
    app.add_handler(CommandHandler("trocar", trocar))

    app.run_polling()

if __name__ == "__main__":
    main()
