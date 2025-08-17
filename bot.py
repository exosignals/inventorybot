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

# Controle de ficha completa em memória
FICHA_COMPLETA = {}

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
    c.
