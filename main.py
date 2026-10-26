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
# SERVEUR FLASK (Pour garder le bot éveillé 24/7 sur Render)
# ====================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "B2K Academy Bot - Opérationnel 24/7 !"

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
# CHARGEMENT DES 30 NIVEAUX DE LA B2K ACADEMY
# ====================================================
def seed_all_levels():
    levels = [
        # --- NIVEAUX 1 à 5 : Fondations & Actifs ---
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
            "• Si tu utilises un lot trop gros, ton compte sera cramé en quelques pips !",
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
            "• **Pocket Option :** Plateforme d'options binaires (haut risque).\n\n"
            "💳 **Dépôts / Retraits en Haïti :**\n"
            "Utilise des services locaux fiables via **MonCash**, **NatCash** ou des transactions USDT (Crypto).",
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
            "• **Mèches (Ombres) :** Le prix le plus haut (High) et le plus bas (Low) atteint.\n"
            "• **Bougie Haussière :** Clôture plus haut que l'ouverture.\n"
            "• **Bougie Baissière :** Clôture plus bas que l'ouverture.",
            None,
            "Que représentent les mèches en haut et en bas d'une bougie ?",
            "Le volume", "Les prix extrêmes (High/Low)", "Le spread broker", "Le levier", "La marge libre",
            "b",
            "💡 Les mèches indiquent le plus haut (High) et le plus bas (Low) atteints par le prix durant la période."
        ),
        (
            5,
            "Niveau 5 : Les Timeframes (M1, M5, M15, H1)",
            "📘 **NIVEAU 5 : Choisir sa Unité de Temps**\n\n"
            "Le temps d'affichage change ta vision du marché :\n\n"
            "• **M1 / M5 :** Ultra short terme, parfait pour le scalping rapide.\n"
            "• **M15 / H1 :** Idéal pour repérer les tendances de la journée et placer des trades solides.",
            None,
            "Quel timeframe privilégie-t-on pour du scalping ultra rapide minute par minute ?",
            "D1", "H4", "M5", "W1", "MN",
            "c",
            "💡 Le M5 (5 minutes) est le compromis idéal pour le scalping dynamique."
        ),
        # --- NIVEAUX 6 à 10 : Supports, Résistances & Ordres ---
        (
            6,
            "Niveau 6 : Supports et Résistances Horizontaux",
            "📘 **NIVEAU 6 : Les Zones Clés**\n\n"
            "• **Support :** Plancher où les acheteurs bloquent la chute.\n"
            "• **Résistance :** Plafond où les vendeurs bloquent la hausse.",
            None,
            "Qu'est-ce qu'un Support sur un graphique ?",
            "Un plafond", "Un plancher de rebond acheteur", "Une commission", "Un type de bougie", "Le solde du compte",
            "b",
            "💡 Le support retient les prix à la baisse et déclenche souvent des achats massifs."
        ),
        (
            7,
            "Niveau 7 : Le Spread et l'Exécution des Ordres",
            "📘 **NIVEAU 7 : Comprendre le Spread**\n\n"
            "• Le spread est l'écart entre le prix d'achat (Ask) et de vente (Bid).\n"
            "• Il est prélevé par le broker dès l'ouverture de ta position.",
            None,
            "Quand est-ce que le spread est payé par le trader sur MT5 ?",
            "À la fin du mois", "Dès l'ouverture du trade", "Uniquement en cas de perte", "Lors du retrait MonCash", "Jamais",
            "b",
            "💡 Le spread est facturé instantanément à l'ouverture de chaque position."
        ),
        (
            8,
            "Niveau 8 : Ordres en Attente (Limit & Stop)",
            "📘 **NIVEAU 8 : Automatiser ses Entrées**\n\n"
            "• **Buy Limit / Sell Limit :** Pour entrer sur un rebond.\n"
            "• **Buy Stop / Sell Stop :** Pour surfer sur une cassure de prix.",
            None,
            "Quel ordre utilise-t-on pour acheter plus bas que le prix actuel ?",
            "Market Order", "Buy Limit", "Sell Stop", "Close Order", "Trailing Stop",
            "b",
            "💡 Le Buy Limit permet de placer un achat en attente plus bas que le cours actuel."
        ),
        (
            9,
            "Niveau 9 : Stop Loss (SL) et Take Profit (TP)",
            "📘 **NIVEAU 9 : Sécuriser son Capital**\n\n"
            "• **Stop Loss :** Coupe ta perte automatiquement si le marché se retourne.\n"
            "• **Take Profit :** Encaisse tes gains automatiquement lorsque l'objectif est atteint.",
            None,
            "À quoi sert principalement le Stop Loss (SL) ?",
            "À doubler les gains", "À limiter et couper une perte automatiquement", "À augmenter le levier", "À annuler le spread", "À contacter le support XM",
            "b",
            "💡 Le Stop Loss est ton bouclier de sécurité indispensable pour ne pas cramer ton compte."
        ),
        (
            10,
            "Niveau 10 : La Marge et l'Effet de Levier",
            "📘 **NIVEAU 10 : Maîtriser le Levier**\n\n"
            "• Le levier permet de trader gros avec un petit capital, mais augmente les risques si mal géré.\n"
            "• Reste toujours discipliné avec tes micro-lots.",
            None,
            "Quel est le risque d'un levier excessif avec un petit capital de 20$ ?",
            "Gagner trop lentement", "Cramer le compte en quelques secondes sur une mauvaise variation", "Baisser les frais du broker", "Changer de fuseau horaire", "Bloquer le marché",
            "b",
            "💡 Un levier trop haut vide un petit capital très rapidement."
        ),
        # --- NIVEAUX 11 à 20 : Indicateurs Pro & Stratégies ---
        (
            11,
            "Niveau 11 : L'Indicateur RSI (Surachat / Survendu)",
            "📘 **NIVEAU 11 : Le RSI**\n\n"
            "• **Au-dessus de 70 (Surachat) :** Le prix est monté trop vite, risque de chute.\n"
            "• **En dessous de 30 (Survendu) :** Le prix a chuté excessivement, risque de rebond.",
            None,
            "Que signifie un RSI supérieur à 70 ?",
            "Marché survendu", "Marché suracheté (risque de baisse)", "Spread nul", "Tendance plate", "Fermeture du marché",
            "b",
            "💡 Au-dessus de 70, l'actif est suracheté et peut corriger à la baisse."
        ),
        (
            12,
            "Niveau 12 : Les Moyennes Mobiles (EMA)",
            "📘 **NIVEAU 12 : Suivre la Tendance**\n\n"
            "• Les moyennes mobiles lissent les prix pour identifier le sens global du marché.\n"
            "• Ne trade jamais à contresens de l'EMA majeure.",
            None,
            "À quoi sert principalement une moyenne mobile comme l'EMA 200 ?",
            "Calculer le spread", "Identifier la direction de la tendance de fond", "Gérer les retraits", "Modifier le solde", "Acheter du Bitcoin physique",
            "b",
            "💡 L'EMA indique la direction globale de la tendance."
        ),
        (
            13,
            "Niveau 13 : Stratégie de Scalping M5 B2K",
            "📘 **NIVEAU 13 : Scalping Rapide**\n\n"
            "• Combine le RSI en zone extrême et la direction de la bougie M5 pour gratter quelques dollars par minute.",
            None,
            "Quel est l'objectif principal du scalping sur M5 ?",
            "Investir pour 10 ans", "Capturer de petits mouvements rapides en quelques minutes", "Acheter des actions en bourse", "Éviter d'utiliser MT5", "Modifier le levier du broker",
            "b",
            "💡 Le scalping vise des profits rapides et fréquents sur de petits laps de temps."
        ),
        (
            14,
            "Niveau 14 : Psychologie & Gestion de la Peur",
            "📘 **NIVEAU 14 : Maîtriser ses Émotions**\n\n"
            "• La peur et l'avidité sont les pires ennemis du trader.\n"
            "• Respecte ton plan de trading à la lettre.",
            None,
            "Que doit faire un trader face à une série de pertes consécutives ?",
            "Doubler la mise immédiatement", "Faire une pause pour éviter l'overtrading émotionnel", "Changer de nom", "Supprimer son application MT5", "Emprunter de l'argent",
            "b",
            "💡 Savoir s'arrêter après des pertes protège ton capital et ton mental."
        ),
        (
            15,
            "Niveau 15 : Le Candlestick Angulfing (Englobante)",
            "📘 **NIVEAU 15 : Figure de Retournement**\n\n"
            "• Une bougie englobante avale complètement le corps de la bougie précédente, signalant un retournement violent du marché.",
            None,
            "Qu'indique une bougie 'Englobante' (Engulfing) haussière après une baisse ?",
            "Une continuation de la baisse", "Un signal fort de retournement acheteur", "Une augmentation du spread", "Une panne du broker", "Un ordre en attente",
            "b",
            "💡 L'englobante montre que les acheteurs ont pris le contrôle total du marché."
        ),
        (
            16,
            "Niveau 16 : Analyse Multi-Timeframe",
            "📘 **NIVEAU 16 : Croiser les Timeframes**\n\n"
            "• Regarde d'abord la tendance en H1, puis affine ton point d'entrée précis en M5.",
            None,
            "Pourquoi analyser plusieurs unités de temps (Multi-Timeframe) ?",
            "Pour payer moins de frais", "Pour aligner la tendance globale avec un point d'entrée précis", "Pour ouvrir plus de 100 lots", "Pour ralentir le graphique", "Pour utiliser Pocket Option",
            "b",
            "💡 Combiner H1 et M5 augmente considérablement la précision de tes trades."
        ),
        (
            17,
            "Niveau 17 : Éviter l'Overtrading",
            "📘 **NIVEAU 17 : Moins c'est Mieux**\n\n"
            "• Passer 50 trades par jour mène à la catastrophe à cause des spreads.\n"
            "• Sélectionne seulement 2 ou 3 setups parfaits par jour.",
            None,
            "Quel est le danger de l'overtrading (trader non-stop toute la journée) ?",
            "Avoir trop de profits", "Exploser son capital à force de payer des spreads et accumuler de mauvais trades", "Fermer le marché mondial", "Augmenter le bonus XM", "Bloquer MonCash",
            "b",
            "💡 La sur-activité en trading détruit les comptes à cause des frais et de la fatigue mentale."
        ),
        (
            18,
            "Niveau 18 : Le rôle des Sessions de Trading (Londres / New York)",
            "📘 **NIVEAU 18 : Les Heures Chaudes**\n\n"
            "• Les meilleures opportunités de scalping se trouvent pendant l'ouverture de Londres et de New York où la volatilité explose.",
            None,
            "Pourquoi privilégier les sessions de Londres et New York pour scalper l'or (XAUUSD) ?",
            "Parce que le marché est fermé", "Parce que la liquidité et la volatilité y sont maximales", "Pour éviter les spreads", "Pour trader sans internet", "Pour utiliser le levier 1:1",
            "b",
            "💡 Plus il y a de volume et de volatilité, plus les mouvements rapides sont profitables."
        ),
        (
            19,
            "Niveau 19 : Calcul du Risk/Reward (R:R)",
            "📘 **NIVEAU 19 : Le Ratio Risque / Récompense**\n\n"
            "• Ne risque jamais 5$ pour espérer gagner 1$.\n"
            "• Vise toujours un ratio minimum de 1:2 (risquer 1$ pour en gagner 2 ou plus).",
            None,
            "Qu'est-ce qu'un bon ratio Risque/Récompense (R:R) en trading ?",
            "Risquer 10 pour gagner 1", "Risquer 1 pour gagner 2 ou plus", "Ne mettre aucun Stop Loss", "Risquer tout son capital d'un coup", "Ignorer le Take Profit",
            "b",
            "💡 Un bon R:R garantit la rentabilité globale même si tu fais quelques pertes."
        ),
        (
            20,
            "Niveau 20 : Valider ses Signaux avec les Indicateurs B2K",
            "📘 **NIVEAU 20 : Synthèse Technique Intermédiaire**\n\n"
            "• À ce stade, tu maîtrises les bougies, les supports, le RSI et le risk management.",
            None,
            "Que faut-il vérifier avant de valider un trade de scalping ?",
            "Le solde de son compte bancaire personnel", "L'alignement de la tendance, du RSI et du plan de risque", "La météo à Port-au-Prince", "Le nom du broker uniquement", "Le nombre d'abonnés Telegram",
            "b",
            "💡 Tout doit être aligné avant d'appuyer sur Buy ou Sell."
        ),
        # --- NIVEAUX 21 à 30 : Niveau Pro & Stratégies Avancées ---
        (
            21,
            "Niveau 21 : Trading de Cassure (Breakout)",
            "📘 **NIVEAU 21 : Le Breakout Pro**\n\n"
            "• Quand un support ou une résistance encaisse trop de tests, il finit par casser violemment.",
            None,
            "Comment trade-t-on une cassure (Breakout) de résistance majeure ?",
            "En vendant immédiatement en aveugle", "En achetant l'impulsion de cassure validée par le volume", "En fermant MT5", "En doublant le spread", "En changeant d'actif",
            "b",
            "💡 La cassure d'un niveau clé déclenche une forte accélération du prix."
        ),
        (
            22,
            "Niveau 22 : Le piège des Faux Retraits (Fakeouts)",
            "📘 **NIVEAU 22 : Attention aux Faux Signaux**\n\n"
            "• Le marché piège souvent les débutants en faisant croire à une cassure avant de repartir dans l'autre sens.",
            None,
            "Qu'appelle-t-on un 'Fakeout' (Fausse cassure) ?",
            "Un retrait réussi sur MonCash", "Une fausse cassure de niveau qui piège les traders avant de s'inverser", "Un bonus de dépôt", "Un indicateur parfait", "Un micro-lot",
            "b",
            "💡 Le fakeout est un piège classique des institutionnels qu'il faut savoir identifier."
        ),
        (
            23,
            "Niveau 23 : Gestion Dynamique du Trade (Trailing Stop)",
            "📘 **NIVEAU 23 : Sécuriser en Cours de Route**\n\n"
            "• Le Trailing Stop déplace ton Stop Loss automatiquement pour verrouiller tes gains à mesure que le prix avance.",
            None,
            "À quoi sert un Trailing Stop sur une position gagnante ?",
            "À augmenter la perte maximale", "À suivre le prix et sécuriser les gains automatiquement", "À annuler le trade", "À doubler le lot", "À payer le broker",
            "b",
            "💡 Le Trailing Stop protège tes profits si le marché change de direction brusquement."
        ),
        (
            24,
            "Niveau 24 : Le Price Action pur (Sans Indicateur)",
            "📘 **NIVEAU 24 : Lire le Graphique Nu**\n\n"
            "• Les pros savent trader uniquement avec les bougies et la structure du marché sans surcharger leur écran.",
            None,
            "Qu'est-ce que le 'Price Action' en trading ?",
            "Le prix des actions en Haïti", "L'analyse exclusive des mouvements du prix et des bougies sans indicateur", "Une taxe du broker", "Le coût de l'abonnement bot", "Une crypto-monnaie",
            "b",
            "💡 Le Price Action repose uniquement sur la lecture comportementale du prix."
        ),
        (
            25,
            "Niveau 25 : Analyse de la Structure du Marché (Highs & Lows)",
            "📘 **NIVEAU 25 : Les Pivots du Marché**\n\n"
            "• Une tendance haussière est caractérisée par des sommets de plus en plus hauts (Higher Highs) et des creux de plus en plus hauts (Higher Lows).",
            None,
            "Comment qualifie-t-on une structure de marché en tendance haussière saine ?",
            "Des sommets et creux de plus en plus bas",
            "Des sommets et creux de plus en plus hauts (Higher Highs & Higher Lows)",
            "Une ligne parfaitement horizontale",
            "Un marché totalement immobile",
            "Une absence de bougies",
            "b",
            "💡 La structure HH/HL est la base absolue d'une tendance haussière."
        ),
        (
            26,
            "Niveau 26 : Établir son Plan de Trading Personnel",
            "📘 **NIVEAU 26 : Le Business Plan du Trader**\n\n"
            "• Un trader sans plan est un joueur de casino. Note tes règles d'entrée, de sortie et ton capital max risqué.",
            None,
            "Quel est l'élément le plus important d'un plan de trading professionnel ?",
            "Le nom du broker", "Des règles strictes de gestion du risque et de validation des entrées", "Le nombre d'écrans de l'ordinateur", "Avoir un effet de levier de 1:2000", "Ignorer les pertes",
            "b",
            "💡 Le plan de trading élimine l'émotion et professionnalise ton activité."
        ),
        (
            27,
            "Niveau 27 : Discipline et Tenue d'un Journal de Trading",
            "📘 **NIVEAU 27 : Noter ses Erreurs**\n\n"
            "• Note chaque trade gagné ou perdu dans un carnet pour analyser tes erreurs et progresser chaque semaine.",
            None,
            "Pourquoi tenir un journal de trading rigoureux ?",
            "Pour décorer son bureau", "Pour analyser ses erreurs, ses statistiques et s'améliorer continuellement", "Pour augmenter le spread", "Pour obtenir des signaux automatiques", "Pour éviter d'utiliser MT5",
            "b",
            "💡 Le journal de trading est l'outil numéro un des traders rentables."
        ),
        (
            28,
            "Niveau 28 : Scalping Avancé sur XAUUSD (Or)",
            "📘 **NIVEAU 28 : Maîtriser le Monstre Or**\n\n"
            "• L'or bouge vite et fort. Utilise des micro-lots stricts, des stops serrés et vise les cassures de zones de consolidation.",
            None,
            "Quelle est la caractéristique principale du trading sur XAUUSD (Or) ?",
            "Une volatilité et des mouvements rapides nécessitant une grande vigilance",
            "Un marché totalement plat sans aucun mouvement",
            "Une ouverture uniquement le week-end",
            "Une absence de spread",
            "Un risque nul",
            "a",
            "💡 L'Or offre de superbes opportunités mais demande une discipline de fer."
        ),
        (
            29,
            "Niveau 29 : Passer de Démo à Réel avec un Petit Capital",
            "📘 **NIVEAU 29 : Le Passage en Live**\n\n"
            "• Après avoir validé tes stratégies en démo, dépose tes 20$ ou 30$ et trade en conditions réelles avec la même rigueur.",
            None,
            "Quelle règle suivre impérativement lors du passage du compte démo au compte réel à petit capital ?",
            "Augmenter le lot par 10", "Garder exactement la même rigueur de gestion de risque et des micro-lots", "Ne plus utiliser de Stop Loss", "Trader sans réfléchir", "Changer totalement de stratégie",
            "b",
            "💡 La transition doit se faire sans modifier ta discipline de risque."
        ),
        (
            30,
            "Niveau 30 : Le Sommet B2K - Prêt pour le Profit Réel 🚀",
            "📘 **NIVEAU 30 : Diplôme d'Élite B2K Academy**\n\n"
            "🏆 **Félicitations Trader !** Tu as complété l'intégralité des 30 niveaux.\n\n"
            "Tu possèdes désormais toutes les bases et stratégies de pro pour analyser le marché, gérer ton capital et faire des gains.\n\n"
            "🔥 **Passe à l'action dès maintenant !** Connecte-toi à notre bot officiel de signaux pour recevoir nos analyses en direct et faire du cash : 👉 [@b2kXgeminibot](https://t.me/b2kXgeminibot)",
            None,
            "Quel est ton objectif principal maintenant que tu as terminé l'Académie B2K ?",
            "Arrêter complètement de trader", "Appliquer rigoureusement les stratégies et utiliser le bot de signaux pour faire des profits", "Effacer mon compte MT5", "Attendre l'année prochaine", "Ouvrir un casino",
            "b",
            "💡 Ton aventure de trader pro commence ici avec `@b2kXgeminibot` !"
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

seed_all_levels()

# ====================================================
# GESTION UTILISATEUR & PROGRESSION SQLITE
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
# COMMANDE ADMIN : /setphoto <niveau>
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
        return bot.send_message(chat_id, f"❌ Le niveau {target_level} n'existe pas dans la BDD.")
    
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
        f"✅ **PHOTO ENREGISTRÉE ET SÉCURISÉE !**\n"
        f"La photo est liée définitivement au **Niveau {level_id}**."
    )

# ====================================================
# AFFICHAGE DES COURS AVEC ANTI-SPAM & BOUTON PRÉCÉDENT
# ====================================================
@bot.message_handler(commands=['start', 'academie'])
def start_academie(message):
    chat_id = message.chat.id
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass
    user_lvl = get_user_level(chat_id)
    send_level_course(chat_id, user_lvl, message_id_to_delete=None)

def send_level_course(chat_id, level_id, message_id_to_delete=None):
    if message_id_to_delete:
        try:
            bot.delete_message(chat_id, message_id_to_delete)
        except Exception:
            pass

    lvl_data = get_level_data(level_id)
    if not lvl_data:
        congrats_text = (
            f"🏆 **FÉLICITATIONS MONSTRE !**\n\n"
            f"Tu as terminé tous les niveaux de l'**Académie B2K** ! Tu disposes désormais de toutes les armes pour trader en pro.\n\n"
            f"💰 **Passe aux choses sérieuses :** Rejoins notre bot officiel de signaux pour faire du cash en direct :\n"
            f"👉 [@b2kXgeminibot](https://t.me/b2kXgeminibot)"
        )
        bot.send_message(chat_id, congrats_text, disable_web_page_preview=True)
        return

    _, title, course_text, photo_id, question, opt_a, opt_b, opt_c, opt_d, opt_e, correct_opt, explanation = lvl_data

    header = f"🎓 **B2K ACADEMY | NIVEAU {level_id}/30**\n📌 *{title}*\n───────────────────────────\n\n"
    full_text = header + course_text

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("📝 Passer le Quiz de validation", callback_data=f"start_quiz_{level_id}"))

    # Bouton pour revenir au niveau précédent si supérieur à 1
    if level_id > 1:
        markup.add(types.InlineKeyboardButton("⬅️ Niveau Précédent", callback_data=f"next_lvl_{level_id - 1}"))

    if level_id == 30:
        full_text += f"\n\n🔥 **Bot officiel de signaux :** [@b2kXgeminibot](https://t.me/b2kXgeminibot)"

    if photo_id:
        msg = bot.send_photo(chat_id, photo_id, caption=full_text, reply_markup=markup, parse_mode="Markdown")
    else:
        msg = bot.send_message(chat_id, full_text, reply_markup=markup, parse_mode="Markdown", disable_web_page_preview=True)
    
    return msg.message_id

def send_quiz_question(chat_id, level_id, message_id_to_delete=None):
    if message_id_to_delete:
        try:
            bot.delete_message(chat_id, message_id_to_delete)
        except Exception:
            pass

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
        f"👇 *Choisis la bonne réponse ci-dessous :*"
    )

    markup = types.InlineKeyboardMarkup(row_width=5)
    markup.add(
        types.InlineKeyboardButton("🇦", callback_data=f"ans_{level_id}_a"),
        types.InlineKeyboardButton("🇧", callback_data=f"ans_{level_id}_b"),
        types.InlineKeyboardButton("🇨", callback_data=f"ans_{level_id}_c"),
        types.InlineKeyboardButton("🇩", callback_data=f"ans_{level_id}_d"),
        types.InlineKeyboardButton("🇪", callback_data=f"ans_{level_id}_e")
    )
    msg = bot.send_message(chat_id, q_text, reply_markup=markup)
    return msg.message_id

# ====================================================
# TRAITEMENT DES INTERACTIONS & ANTI-SPAM CALLBACKS
# ====================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_academy_callbacks(call):
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    data = call.data

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data.startswith("start_quiz_"):
        level_id = int(data.split("_")[2])
        send_quiz_question(chat_id, level_id, message_id_to_delete=msg_id)
        return

    if data.startswith("ans_"):
        _, level_str, user_ans = data.split("_")
        level_id = int(level_str)
        lvl_data = get_level_data(level_id)

        if not lvl_data:
            return

        correct_opt = lvl_data[10].lower()
        explanation = lvl_data[11]

        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

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
            
            if next_level == 30:
                res_text += f"\n\n💰 **Objectif final atteint bientôt !** Rejoins notre bot de signaux : [@b2kXgeminibot](https://t.me/b2kXgeminibot)"

            bot.send_message(chat_id, res_text, reply_markup=markup, disable_web_page_preview=True)
        else:
            res_text = (
                f"🔴 **RÉPONSE INCORRECTE !**\n\n"
                f"Relis bien le cours et retente ta chance pour progresser :\n\n"
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
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        send_level_course(chat_id, next_lvl)
        return

if __name__ == '__main__':
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)
