# 🎲 InventoryBot RPG (Telegram)

Um bot para gerenciamento completo de fichas, inventário, saúde e sanidade de jogadores de RPG pelo Telegram.  
**Totalmente integrado com PostgreSQL (Neon) para persistência de dados mesmo após deploys no Render!**

## 🚀 Funcionalidades

- Registro automático do jogador: `/start`
- Visualização de ficha: `/ficha`
- Edição fácil da ficha: `/editarficha` (atributos e perícias)
- Inventário inteligente: `/inventario` (com cálculo de peso e penalidades)
- Catálogo global de itens: `/itens`
- Adição/remoção de itens (admin): `/additem`, `/delitem`
- Dar itens a outros jogadores: `/dar @jogador Nome_do_item [x quantidade]`
- Sistema de saúde (HP), sanidade (SP) e traumas mentais
  - Dano físico/mental: `/dano hp|sp [@jogador]`
  - Cura com kits médicos: `/cura @jogador NomeDoKit`
  - Terapia psicológica: `/terapia @jogador`
- Sistema de coma e recuperação: `/coma`, `/ajudar`
- Testes de perícia/atributo: `/roll nome_da_pericia_ou_atributo`
- Reroll diário (com reset automático): `/reroll`
- Anti-spam embutido para comandos

## 🧑‍💻 Ficha do Jogador

- **Atributos**: Força, Destreza, Constituição, Inteligência, Sabedoria, Carisma (máx 24 pontos, 1-6 cada)
- **Perícias**: Percepção, Persuasão, Medicina, Furtividade, Intimidação, Investigação, Armas de fogo, Armas brancas, Sobrevivência, Cultura, Intuição, Tecnologia (máx 42 pontos, 1-6 cada)
- **HP** (Vida): Máximo 20
- **SP** (Sanidade): Máximo 20

## 🏋️‍♂️ Peso Máximo por Força

| Força | Peso Máx (kg) |
|-------|--------------|
| 1     | 5            |
| 2     | 10           |
| 3     | 15           |
| 4     | 20           |
| 5     | 25           |
| 6     | 30           |

## 🛠️ Como rodar no Render com Neon

1. Suba os arquivos (`bot.py`, `requirements.txt`, `README.md`) no seu repositório GitHub.
2. Crie um banco PostgreSQL gratuito no [Neon](https://neon.tech) e copie a **Database URL**.
3. No [Render](https://render.com), crie um **Web Service** e conecte ao seu repo.
4. Adicione estas variáveis de ambiente:
   - `BOT_TOKEN` = token do seu bot (pegue no [BotFather](https://t.me/BotFather))
   - `NEON_DATABASE_URL` = URL do banco Neon/Postgres (algo como `postgres://...`)
   - `ADMINS` = ids dos administradores, separados por vírgula (ex: `123456,654321`)
5. Confirme que `psycopg2-binary` está no seu `requirements.txt`.
6. No campo **Start Command** coloque:
   ```bash
   python bot.py
   ```

## 📦 Dependências

- `python-telegram-bot`
- `psycopg2-binary`
- `flask`

## 💡 Observações

- Todos os dados dos jogadores ficam salvos no Neon/PostgreSQL, **nunca serão perdidos em deploys**.
- O catálogo de itens é global, o inventário é individual.
- Rerolls de dados são resetados automaticamente todo dia às 6h.
- O bot aceita comandos tanto por texto quanto menus do Telegram.

## 🤝 Contribuição

Pull requests e sugestões são bem-vindos!

---

Feito para RPGs por [exosignals](https://github.com/exosignals)
