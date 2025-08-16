# Bot de Inventário RPG (Telegram)

Um bot de Telegram para gerenciar inventário de personagens em RPG.

## 🚀 Funções
- `/start` → registra o jogador
- `/forca <1-6>` → define a força do personagem
- `/additem <nome> <peso>` → adiciona item
- `/inv` → mostra inventário
- `/droparitem <nome>` → descarta item
- `/trocar <@usuario> <nome_do_item>` → transfere item para outro jogador

## 🏋️‍♂️ Peso máximo
| Força | Peso Máx |
|-------|----------|
| 1 | 5kg |
| 2 | 10kg |
| 3 | 15kg |
| 4 | 20kg |
| 5 | 25kg |
| 6 | 30kg |

## 🛠️ Como rodar no Render
1. Crie um repositório no GitHub e suba os arquivos (`bot.py`, `requirements.txt`, `README.md`).
2. Vá no [Render](https://render.com) e crie um **Web Service**.
3. Conecte ao seu repositório.
4. Em **Environment Variables**, adicione:
   - `BOT_TOKEN` = token do seu bot (pegue no [BotFather](https://t.me/BotFather)).
5. No campo **Start Command**, coloque:
   ```bash
   python bot.py
