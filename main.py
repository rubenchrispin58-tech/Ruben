import os
import sqlite3
import threading
import telebot
from flask import Flask
from telebot import types

# ====================================================
# CONFIGURATION
# ====================================================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("ACADEMY_DB", "academy_b2k.db")

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown", threaded=True)

# Dictionnaire pour la capture de photo admin : chat_id -> level_id
awaiting_admin_photo = {}

# ====================================================
# SERVEUR FLASK (Uptime Render)
# ====================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "B2K Academy Bot - Opérationnel !"

def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ====================================================
# BASE DE DONNÉES SQLITE
# ====================================================
db_lock = threading.Lock()

def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_lock:
        conn = db()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_progress (
                chat_id INTEGER PRIMARY KEY,
                current_level INTEGER DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS level_content (
                level_id INTEGER PRIMARY KEY,
                title TEXT,
                course_text TEXT,
                photo_id TEXT DEFAULT NULL,
                question TEXT,
                option_a TEXT,
                option_b TEXT,
                option_c TEXT,
                option_d TEXT,
                option_e TEXT,
                correct_option TEXT,
                explanation TEXT
            )
        ''')
        conn.commit()
        conn.close()

init_db()

# ====================================================
# INITIALISATION DES NIVEAUX DE BASE
# ====================================================
def seed_initial_levels():
    levels = [
        (
            1,
            "Niveau 1 : Les Actifs (BTCUSD vs XAUUSD vs EURUSD)",
            "📘 **NIVEAU 1 : Les Marchés Financiers**\n\n"
            "• **BTCUSD (Bitcoin) :** Crypto-monnaie active 24/7. Très forte volatilité, idéale pour le scalping le week-end.\n"
            "• **XAUUSD (Or) :** Valeur refuge par excellence. Mouvance rapide en semaine pendant les sessions de Londres et New York.\n"
            "• **EURUSD (Euro / Dollar) :** Le marché Forex classique. Plus stable, parfait pour comprendre les bases sans risque extrême.",
            None,
            "Quel actif reste ouvert le week-end 24h/24 et 7j/7 ?",
            "EURUSD", "XAUUSD", "BTCUSD", "GBPUSD", "USDJPY",
            "c",
            "💡 Le BTCUSD (Bitcoin) est une crypto-monnaie, c'est le seul actif ouvert 24/7 même le week-end !"
        ),
        (
            2,
            "Niveau 2 : Petit Capital (20$ - 30$) & Micro-Lots",
            "📘 **NIVEAU 2 : Trader avec un Petit Capital**\n\n"
            "Avec 20 $ ou 30 $ de dépôt sur ton broker (ex: XM), **la gestion du risque est vitale**.\n\n"
            "• **Micro-lot (0.01) :** C'est la taille de position minimale obligatoire pour protéger ton compte.\n"
            "• **Règle d'or :** Ne risque jamais plus de 1 $ à 2 $ par trade (1-2 % de ton capital).\n"
            "• Si tu utilises un lot trop gros (ex: 0.10 ou 1.00), ton compte sera cramé en quelques pips de variation négative !",
            None,
            "Avec un capital de 20$, quelle taille de lot dois-tu utiliser sur MT5 ?",
            "1.00 (Standard)", "0.50 (Moyen)", "0.10 (Mini)", "0.01 (Micro-lot)", "5.00 (Max)",
            "d",
            "💡 Avec 20$, l'utilisation du lot 0.01 (micro-lot) est impérative pour ne pas exploser ta marge !"
        ),
        (
            3,
            "Niveau 3 : Choix de Plateforme & Dépôts Locaux",
            "📘 **NIVEAU 3 : MT5/XM vs Pocket Option & Paiements Haïti**\n\n"
            "• **XM / MetaTrader 5 :** Vrai courtier régulé avec accès aux marchés réels. Analyse technique précise, spread garanti.\n"
            "• **Pocket Option :** Plateforme d'options binaires (haut risque). Rapidité extrême mais moins d'analyse professionnelle.\n\n"
            "💳 **Dépôts / Retraits en Haïti :**\n"
            "Pour alimenter ton compte XM ou retirer tes gains en toute sécurité, utilise des services locaux fiables via **MonCash**, **NatCash** ou des transactions USDT (Crypto).",
            None,
            "Quel broker professionnel est recommandé pour appliquer nos stratégies MT5 ?",
            "Casino Online", "XM / MT5", "Pocket Option", "Bank Transfer", "Local Pay",
            "b",
            "💡 XM sur MetaTrader 5 est le choix professionnel recommandé pour analyser le marché proprement."
        ),
        (
            4,
            "Niveau 4 : Anatomie d'une Bougie Japonaise",
            "📘 **NIVEAU 4 : Les Bougies Japonaises**\n\n"
            "Chaque bougie raconte une histoire entre Acheteurs (Verts) et Vendeurs (Rouges).\n\n"
            "• **Corps :** L'écart entre le prix d'ouverture (Open) et de fermeture (Close).\n"
            "• **Mèches (Ombres) :** Le prix le plus haut (High) et le plus bas (Low) atteint pendant le timeframe (ex: M5).\n"
            "• **Bougie Haussière :** Clôture plus haut que l'ouverture.\n"
            "• **Bougie Baissière :** Clôture plus bas que l'ouverture.",
            None,
            "Que représentent les mèches en haut et en bas d'une bougie ?",
            "Le volume", "Les prix extrêmes (High/Low)", "Le spread broker", "Le levier", "La marge libre",
            "b",
            "💡 Les mèches indiquent le plus haut (High) et le plus bas (Low) atteints par le prix durant la période."
        )
    ]
    with db_lock:
        conn = db()
        for lvl in levels:
            conn.execute('''
                INSERT OR IGNORE INTO level_content 
                (level_id, title, course_text, photo_id, question, option_a, option_b, option_c, option_d, option_e, correct_option, explanation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', lvl)
        conn.commit()
        conn.close()

seed_initial_levels()

# ====================================================
# GESTION UTILISATEUR & PROGRESSION
# ====================================================
def get_user_level(chat_id):
    with db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT current_level FROM user_progress WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO user_progress (chat_id, current_level) VALUES (?, 1)", (chat_id,))
            conn.commit()
            lvl = 1
        else:
            lvl = row[0]
        conn.close()
        return lvl

def set_user_level(chat_id, level):
    with db_lock:
        conn = db()
        conn.execute("UPDATE user_progress SET current_level=? WHERE chat_id=?", (level, chat_id))
        conn.commit()
        conn.close()

def get_level_data(level_id):
    with db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM level_content WHERE level_id=?", (level_id,))
        row = cur.fetchone()
        conn.close()
        return row

# ====================================================
# COMMANDES ADMIN (AJOUT DE PHOTOS)
# ====================================================
@bot.message_handler(commands=['setphoto'])
def set_photo_cmd(message):
    chat_id = message.chat.id
    if ADMIN_ID and chat_id != ADMIN_ID:
        return bot.send_message(chat_id, "⛔ Commande réservée à l'administrateur.")
    
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return bot.send_message(chat_id, "⚠️ **Usage :** `/setphoto <numéro_du_niveau>` (Ex: `/setphoto 4`)")
    
    target_level = int(args[1])
    data = get_level_data(target_level)
    if not data:
        return bot.send_message(chat_id, f"❌ Le niveau {target_level} n'existe pas encore dans la BDD.")
    
    awaiting_admin_photo[chat_id] = target_level
    bot.send_message(chat_id, f"📸 **Envoie maintenant la photo** pour le **Niveau {target_level}** :")

@bot.message_handler(content_types=['photo'], func=lambda m: m.chat.id in awaiting_admin_photo)
def receive_level_photo(message):
    chat_id = message.chat.id
    level_id = awaiting_admin_photo.pop(chat_id)
    photo_file_id = message.photo[-1].file_id

    with db_lock:
        conn = db()
        conn.execute("UPDATE level_content SET photo_id=? WHERE level_id=?", (photo_file_id, level_id))
        conn.commit()
        conn.close()

    bot.send_message(
        chat_id, 
        f"✅ **PHOTO ENREGISTRÉE AVEC SUCCÈS !**\n"
        f"La photo a été liée au **Niveau {level_id}**."
    )

# ====================================================
# NAVIGATION COURS & QUIZ
# ====================================================
@bot.message_handler(commands=['start', 'academie'])
def start_academie(message):
    chat_id = message.chat.id
    user_lvl = get_user_level(chat_id)
    send_level_course(chat_id, user_lvl)

def send_level_course(chat_id, level_id):
    lvl_data = get_level_data(level_id)
    if not lvl_data:
        bot.send_message(
            chat_id,
            f"🏆 **FÉLICITATIONS !**\n\nTu as terminé tous les niveaux disponibles dans l'Académie B2K !"
        )
        return

    _, title, course_text, photo_id, question, opt_a, opt_b, opt_c, opt_d, opt_e, correct_opt, explanation = lvl_data

    header = f"🎓 **B2K ACADEMY | NIVEAU {level_id}/30**\n📌 *{title}*\n───────────────────────────\n\n"
    full_text = header + course_text

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📝 Passer le Quiz de validation", callback_data=f"start_quiz_{level_id}"))

    if photo_id:
        bot.send_photo(chat_id, photo_id, caption=full_text, reply_markup=markup)
    else:
        bot.send_message(chat_id, full_text, reply_markup=markup)

def send_quiz_question(chat_id, level_id):
    lvl_data = get_level_data(level_id)
    if not lvl_data:
        return

    _, title, _, _, question, opt_a, opt_b, opt_c, opt_d, opt_e, _, _ = lvl_data

    q_text = (
        f"❓ **QUIZ - NIVEAU {level_id}**\n"
        f"───────────────────────────\n"
        f"**Question :** {question}\n\n"
        f"🇦 {opt_a}\n"
        f"🇧 {opt_b}\n"
        f"🇨 {opt_c}\n"
        f"🇩 {opt_d}\n"
        f"🇪 {opt_e}\n\n"
        f"👇 *Choisis la bonne réponse :*"
    )

    markup = types.InlineKeyboardMarkup(row_width=5)
    markup.add(
        types.InlineKeyboardButton("🇦", callback_data=f"ans_{level_id}_a"),
        types.InlineKeyboardButton("🇧", callback_data=f"ans_{level_id}_b"),
        types.InlineKeyboardButton("🇨", callback_data=f"ans_{level_id}_c"),
        types.InlineKeyboardButton("🇩", callback_data=f"ans_{level_id}_d"),
        types.InlineKeyboardButton("🇪", callback_data=f"ans_{level_id}_e")
    )
    bot.send_message(chat_id, q_text, reply_markup=markup)

# ====================================================
# CALLBACKS & CORRECTION DES QUIZ
# ====================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_academy_callbacks(call):
    chat_id = call.message.chat.id
    data = call.data

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data.startswith("start_quiz_"):
        level_id = int(data.split("_")[2])
        send_quiz_question(chat_id, level_id)
        return

    if data.startswith("ans_"):
        _, level_str, user_ans = data.split("_")
        level_id = int(level_str)
        lvl_data = get_level_data(level_id)

        if not lvl_data:
            return

        correct_opt = lvl_data[10].lower()
        explanation = lvl_data[11]

        if user_ans == correct_opt:
            next_level = level_id + 1
            set_user_level(chat_id, next_level)

            res_text = (
                f"🟢 **BONNE RÉPONSE !** 👏\n\n"
                f"{explanation}\n\n"
                f"🎉 **Niveau {level_id} validé !** Le **Niveau {next_level}** est débloqué."
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(f"🚀 Passer au Niveau {next_level}", callback_data=f"next_lvl_{next_level}"))
            bot.send_message(chat_id, res_text, reply_markup=markup)
        else:
            res_text = (
                f"🔴 **RÉPONSE INCORRECTE !**\n\n"
                f"Relis bien le cours et retente ta chance !\n\n"
                f"{explanation}"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("📖 Relire le cours", callback_data=f"next_lvl_{level_id}"),
                types.InlineKeyboardButton("🔄 Retenter le Quiz", callback_data=f"start_quiz_{level_id}")
            )
            bot.send_message(chat_id, res_text, reply_markup=markup)
        return

    if data.startswith("next_lvl_"):
        next_lvl = int(data.split("_")[2])
        send_level_course(chat_id, next_lvl)
        return

if __name__ == '__main__':
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
