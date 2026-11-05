import json
import logging
import os
import re
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, TypeHandler, filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("iphone-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
# Fichier local (best effort). S'il disparait au redemarrage, le bot repart avec les valeurs du code.
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

G_PAR_HT = 5  # 1 HT = 5 gourdes


def usd_en_ht(v):
    return v * 140 / G_PAR_HT


# Taux = gourdes pour 1 $
# achat = achat local | vente = revente | ebay = recharge eBay | ccs = paiement des frais CCS en Haiti
TAUX = {"achat": 127, "vente": 140, "ebay": 127, "ccs": 127}
FRAIS_EBAY_PCT = 0.02  # 2 $ de frais pour 100 $ recharges (deduits du solde)

# Tarif CCS (CCPACK, maritime Miami -> Port-au-Prince)
CCS_RECEPTION = 15   # $ frais d'entrepot Miami
CCS_PAR_LB = 2       # $ par livre
CCS_SERVICE = 20     # $ frais de service / dedouanement Haiti (hors droits de douane)

# Modeles prioritaires en premier
MODELES = [
    "iPhone XS", "iPhone XS Max", "iPhone XR",
    "iPhone 11", "iPhone 12", "iPhone 12 Pro",
    "iPhone 12 Pro Max", "iPhone 13", "iPhone 13 Pro",
    "iPhone 13 Pro Max",
]
ETATS = {"neuf": "Neuf", "bon": "Occasion (bon état)", "mauvais": "Mauvais état / pièces"}

# Profit minimum strict en $ (50 par defaut)
PROFIT_MIN = {"iPhone XS": 40, "iPhone XS Max": 40}


def profit_min(modele):
    return PROFIT_MIN.get(modele, 50)


# Prix de vente en HT
PRIX = {
    "iPhone XS": {"neuf": 3500, "bon": 3000, "mauvais": 2500},
    "iPhone XS Max": {"neuf": 3500, "bon": 3000, "mauvais": 2500},
    "iPhone XR": {"neuf": 4000, "bon": 4000, "mauvais": 3700},
    "iPhone 11": {"neuf": 5000, "bon": 4500, "mauvais": 3500},
    "iPhone 12": {"neuf": usd_en_ht(220), "bon": usd_en_ht(200), "mauvais": usd_en_ht(190)},
    "iPhone 12 Pro": {"neuf": usd_en_ht(220), "bon": usd_en_ht(200), "mauvais": usd_en_ht(190)},
    "iPhone 12 Pro Max": {"neuf": usd_en_ht(220), "bon": usd_en_ht(200), "mauvais": usd_en_ht(190)},
    "iPhone 13": {"neuf": usd_en_ht(290), "bon": usd_en_ht(270), "mauvais": usd_en_ht(250)},
    "iPhone 13 Pro": {},
    "iPhone 13 Pro Max": {},
}


def appliquer(data):
    for k, v in data.get("taux", {}).items():
        if k in TAUX:
            TAUX[k] = float(v)
    for m, p in data.get("prix", {}).items():
        if m in PRIX and isinstance(p, dict):
            for e, v in p.items():
                if e in ETATS:
                    PRIX[m][e] = float(v)


def charger():
    """1) variable Render DATA_JSON (permanente)  2) fichier local (si present). Ne plante jamais."""
    raw = os.environ.get("DATA_JSON")
    if raw:
        try:
            appliquer(json.loads(raw))
        except Exception as e:
            log.warning("DATA_JSON invalide: %s", e)
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            appliquer(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Lecture %s impossible: %s", DATA_FILE, e)


def sauver():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"taux": TAUX, "prix": PRIX}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("Sauvegarde locale impossible (normal sur Render gratuit): %s", e)


charger()

# ---------------- CALCULS ----------------


def ht_en_usd(ht, taux):
    return ht * G_PAR_HT / taux


def usd_vers_ht(usd, taux):
    return usd * taux / G_PAR_HT


def arrondi_usd(x):
    """On oublie les centimes. 148/149 -> 150 ; 147 reste 147."""
    base = int(x)
    reste = base % 5
    return base + (5 - reste) if reste >= 3 else base


def fmt_ht(x):
    return f"{round(x):,}".replace(",", " ") + " HT"


def frais_ccs(poids_lb):
    """Frais CCS d'un colis entier (hors droits de douane)."""
    return CCS_RECEPTION + CCS_PAR_LB * poids_lb + CCS_SERVICE


def calculer(d):
    nb = max(int(d.get("nb", 1)), 1)
    ebay = d["achat"] == "ebay"
    transport = d.get("transport", 0)
    reparations = d["ecran"] + d["batterie"] + d["autres"]
    vente = d["vente_ht"]
    cible_ht = usd_vers_ht(d["cible_usd"], TAUX["vente"])

    r = {"nb": nb, "ccs_total": 0.0, "ccs_part": 0.0, "douane_part": 0.0,
         "cout_ccs_ht": 0.0, "transport_part": 0.0}

    if ebay:
        # livraison du vendeur -> Miami : payee sur eBay (donc avec les 2% de recharge)
        usd_ebay = d["prix_achat"] + transport
        cout_ebay_ht = usd_vers_ht(usd_ebay / (1 - FRAIS_EBAY_PCT), TAUX["ebay"])
        # CCS + douane : payes a part en Haiti (pas de frais eBay), partages entre les telephones
        poids = d.get("poids", 0)
        r["ccs_total"] = frais_ccs(poids) if poids > 0 else 0.0
        r["ccs_part"] = r["ccs_total"] / nb
        r["douane_part"] = d.get("douane", 0) / nb
        r["cout_ccs_ht"] = usd_vers_ht(r["ccs_part"] + r["douane_part"], TAUX["ccs"])
        cout_achat_ht = cout_ebay_ht + r["cout_ccs_ht"]
        r["cout_ebay_ht"] = cout_ebay_ht
    else:
        r["transport_part"] = transport / nb
        cout_achat_ht = d["prix_achat"] + r["transport_part"]

    cout_total = cout_achat_ht + reparations
    profit = vente - cout_total
    budget_achat_ht = vente - reparations - cible_ht  # achat + transport + CCS
    if ebay:
        dispo_usd = ht_en_usd(budget_achat_ht - r["cout_ccs_ht"], TAUX["ebay"]) * (1 - FRAIS_EBAY_PCT)
        prix_max = int(dispo_usd - transport)
    else:
        prix_max = int(budget_achat_ht - r["transport_part"])

    r.update({
        "cout_achat_ht": cout_achat_ht, "reparations": reparations, "cout_total": cout_total,
        "profit": profit, "profit_usd": ht_en_usd(profit, TAUX["vente"]), "prix_max": prix_max,
    })
    return r


def parse_montant(txt, defaut="ht"):
    t = txt.lower().replace(" ", "").replace(",", ".")
    unite = defaut
    for suffixe, u in (("usd", "usd"), ("$", "usd"), ("ht", "ht")):
        if t.endswith(suffixe):
            t = t[: -len(suffixe)]
            unite = u
            break
    return float(t), unite


def norm(s):
    return s.lower().replace("iphone", "").replace(" ", "")


def trouver_modele(txt):
    for m in MODELES:
        if norm(m) == norm(txt):
            return m
    return None


# ---------------- GEMINI ----------------
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
HISTORIQUE = defaultdict(lambda: deque(maxlen=10))


def liste_prix_texte():
    lignes = []
    for m in MODELES:
        p = PRIX[m]
        if not p:
            lignes.append(f"- {m}: prix non défini")
            continue
        morceaux = [f"{ETATS[e]} {fmt_ht(p[e])}" for e in ("neuf", "bon", "mauvais") if e in p]
        lignes.append(f"- {m} (profit min {profit_min(m)} $): " + " | ".join(morceaux))
    return "\n".join(lignes)


def system_prompt(dernier_calcul):
    return f"""Tu es l'assistant d'un revendeur d'iPhones en Haïti.

STYLE (très strict) :
- Réponds en français, en UN SEUL message de 1 à 3 phrases courtes (60 mots maximum).
- Pas de markdown, pas d'étoiles, pas de titres, pas de longues listes, pas d'introduction ni de conclusion.
- Donne directement le résultat. Une seule question de clarification si vraiment nécessaire.

CALCULS (stricts) :
- Affichage en HT (1 HT = 5 gourdes). Écris juste "4000 HT", jamais "dollar HT", jamais de gourdes.
- HT vers $ : HT x 5 / taux. Taux achat local = {TAUX['achat']}, taux revente = {TAUX['vente']}, taux recharge eBay = {TAUX['ebay']}, taux paiement CCS = {TAUX['ccs']}.
- Recharge eBay : on paie X $ au taux {TAUX['ebay']}, avec 2 $ de frais par 100 $ déduits du solde (100 $ rechargés = 98 $ utilisables).
- Livraison CCS (Miami vers Haïti, maritime) pour UN COLIS ENTIER : 15 $ + 2 $ par livre + 20 $ de frais de service, plus les droits de douane haïtiens en supplément (selon la valeur). Ce total est à partager entre les téléphones du colis. Exemple : 6 livres = 47 $ pour tout le colis, pas par téléphone. La livraison du vendeur eBay vers Miami est à part.
- Arrondi des $ : on oublie les centimes ; si ça finit à 3 ou 4 au-dessus d'un multiple de 5 (148, 149), on arrondit au multiple de 5 suivant (150). 147 reste 147.
- Utilise TOUJOURS l'exécution de code pour calculer. Jamais de chiffre approximatif ou inventé.
- Modèles prioritaires : XS, XS Max, XR, 11, 12, puis 13. On élimine iPhone X et plus ancien, 11 Pro, 11 Pro Max.
- Pour un calcul complet de profit, dis d'utiliser /calcul. Pour les frais CCS, dis d'utiliser /ccs.

PRIX DE REVENTE ACTUELS :
{liste_prix_texte()}

DERNIER CALCUL :
{dernier_calcul or "aucun"}"""


def nettoyer(texte):
    texte = re.sub(r"[*_`#]+", "", texte).strip()
    texte = re.sub(r"\n{3,}", "\n\n", texte)
    if len(texte) > 700:
        texte = texte[:700].rsplit(" ", 1)[0] + "…"
    return texte


async def gemini_repond(user_id, texte, dernier_calcul):
    if not client:
        return "Gemini n'est pas configuré (GEMINI_API_KEY manquante)."
    hist = HISTORIQUE[user_id]
    contents = list(hist) + [types.Content(role="user", parts=[types.Part(text=texte)])]
    sys = system_prompt(dernier_calcul)
    reponse = None
    for outils in ([types.Tool(code_execution=types.ToolCodeExecution())], None):
        try:
            cfg = types.GenerateContentConfig(
                system_instruction=sys, temperature=0.2, max_output_tokens=500, tools=outils
            )
            r = await client.aio.models.generate_content(model=GEMINI_MODEL, contents=contents, config=cfg)
            reponse = (r.text or "").strip()
            if reponse:
                break
        except Exception as e:
            log.warning("Gemini erreur (outils=%s): %s", bool(outils), e)
    if not reponse:
        return "Je n'ai pas pu répondre, réessaie."
    reponse = nettoyer(reponse)
    hist.append(types.Content(role="user", parts=[types.Part(text=texte)]))
    hist.append(types.Content(role="model", parts=[types.Part(text=reponse)]))
    return reponse


# ---------------- OUTILS TELEGRAM ----------------


def kb(rangees):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in rangee] for rangee in rangees]
    )


async def say(update, texte, markup=None):
    msg = update.callback_query.message if update.callback_query else update.message
    await msg.reply_text(texte, reply_markup=markup)


async def garde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED and update.effective_user and update.effective_user.id not in ALLOWED:
        raise ApplicationHandlerStop


# ---------------- CONVERSATION /calcul ----------------
(MODELE, ETAT, PRIX_VENTE, ACHAT_TYPE, ACHAT_PRIX, TRANSPORT, POIDS, NB, DOUANE,
 ECRAN, BATTERIE, AUTRES, CIBLE) = range(13)

TEXTE = filters.TEXT & ~filters.COMMAND
NOUVEAU = [[("📱 Nouveau calcul", "nouveau")]]
ZERO = kb([[("0 – aucun", "zero")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bot profit iPhone\n\n"
        "/calcul : achat → profit\n"
        "/ccs 6 3 : frais CCS (6 lb, 3 tél.)\n"
        "/prix : liste de prix\n"
        "/taux : taux\n"
        "/ht 4000 : HT → $\n"
        "/usd 150 : $ → HT\n"
        "/recharge 100 : recharge eBay\n"
        "/export : sauvegarder prix et taux\n\n"
        "Tu peux aussi poser une question.",
        reply_markup=kb(NOUVEAU),
    )


async def nouveau(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["d"] = {}
    boutons = [(m.replace("iPhone ", ""), f"m:{m}") for m in MODELES]
    rangees = [boutons[i:i + 3] for i in range(0, len(boutons), 3)]
    rangees.append([("Autre modèle (bon deal)", "m:autre")])
    await say(update, "Quel modèle d'iPhone ?", kb(rangees))
    return MODELE


async def choisir_modele(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cle = q.data[2:]
    d = context.user_data["d"]
    d["modele"] = "Autre modèle" if cle == "autre" else cle
    rangees = [[(label, f"e:{k}")] for k, label in ETATS.items()]
    await say(update, f"{d['modele']} — quel état ?", kb(rangees))
    return ETAT


async def ask_achat_type(update, context):
    await say(update, "Où as-tu acheté ?",
              kb([[("🛒 eBay (en $)", "a:ebay"), ("🏪 Local (en HT)", "a:local")]]))
    return ACHAT_TYPE


async def choisir_etat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data["d"]
    d["etat"] = q.data[2:]
    prix = PRIX.get(d["modele"], {}).get(d["etat"])
    if prix is None:
        await say(update, "Prix de vente non défini pour ce modèle.\n"
                          "Envoie le prix de vente (ex : 5000 ou 180$).")
        return PRIX_VENTE
    d["vente_ht"] = prix
    return await ask_achat_type(update, context)


async def saisir_prix_vente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val, unite = parse_montant(update.message.text)
    except ValueError:
        await update.message.reply_text("Pas compris. Exemple : 5000 ou 180$")
        return PRIX_VENTE
    d = context.user_data["d"]
    d["vente_ht"] = usd_vers_ht(val, TAUX["vente"]) if unite == "usd" else val
    return await ask_achat_type(update, context)


async def choisir_achat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data["d"]
    d["achat"] = q.data[2:]
    unite = "$" if d["achat"] == "ebay" else "HT"
    await say(update, f"Prix d'achat du téléphone ({unite}) ?")
    return ACHAT_PRIX


def saisie_nombre(cle, etat_courant, suite):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        d = context.user_data["d"]
        if update.callback_query:
            await update.callback_query.answer()
            val = 0.0
        else:
            try:
                val = float(update.message.text.replace(" ", "").replace(",", "."))
                if val < 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Envoie juste un nombre, ex : 150")
                return etat_courant
        d[cle] = val
        return await suite(update, context)
    return handler


async def ask_cible(update, context):
    d = context.user_data["d"]
    m = profit_min(d["modele"])
    await say(update, f"Profit visé ? (minimum strict {m} $)\nChoisis ou envoie un montant en $.",
              kb([[(f"{m} $", f"c:{m}"), (f"{m + 10} $", f"c:{m + 10}"), (f"{m + 20} $", f"c:{m + 20}")]]))
    return CIBLE


async def ask_autres(update, context):
    await say(update, "Autres réparations (HT) ? 0 si aucune.", ZERO)
    return AUTRES


async def ask_batterie(update, context):
    await say(update, "Batterie remplacée (HT) ? 0 si aucune.", ZERO)
    return BATTERIE


async def ask_ecran(update, context):
    await say(update, "Écran remplacé (HT) ? 0 si aucun.", ZERO)
    return ECRAN


async def ask_transport(update, context):
    d = context.user_data["d"]
    if d["achat"] == "ebay":
        await say(update, "Livraison du vendeur eBay jusqu'à Miami ($) ?\n"
                          "(pour ce téléphone, payée sur eBay) 0 si gratuite.", ZERO)
    else:
        await say(update, "Transport local total (HT) ? 0 si aucun.", ZERO)
    return TRANSPORT


async def apres_transport(update, context):
    d = context.user_data["d"]
    if d["achat"] == "ebay":
        await say(update, "Poids TOTAL du colis CCS (en livres) ?\n"
                          "Choisis ou envoie le poids.",
                  kb([[(f"{i} lb", f"p:{i}") for i in (2, 3, 4, 6, 8)]]))
        return POIDS
    if d["transport"] == 0:
        d["nb"] = 1
        return await ask_ecran(update, context)
    return await ask_nb(update, context)


async def choisir_poids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["d"]["poids"] = float(q.data[2:])
    return await ask_nb(update, context)


async def ask_nb(update, context):
    await say(update, "Combien de téléphones dans ce colis ?",
              kb([[(str(i), f"n:{i}") for i in range(1, 6)]]))
    return NB


async def choisir_nb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data["d"]
    d["nb"] = int(q.data[2:])
    if d["achat"] == "ebay":
        return await ask_douane(update, context)
    return await ask_ecran(update, context)


async def ask_douane(update, context):
    await say(update, "Droits de douane Haïti pour TOUT le colis ($) ?\n"
                      "0 si tu ne sais pas encore (le profit sera provisoire).", ZERO)
    return DOUANE


async def finir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["d"]
    if update.callback_query:
        await update.callback_query.answer()
        d["cible_usd"] = float(update.callback_query.data[2:])
    else:
        try:
            d["cible_usd"] = float(update.message.text.replace("$", "").replace(" ", "").replace(",", "."))
        except ValueError:
            await update.message.reply_text("Envoie un nombre, ex : 60")
            return CIBLE

    r = calculer(d)
    mini = profit_min(d["modele"])
    ebay = d["achat"] == "ebay"
    u = "$" if ebay else " HT"
    lignes = [f"📱 {d['modele']} — {ETATS[d['etat']]}", ""]

    if ebay:
        lignes.append(f"🛒 eBay : {d['prix_achat']:g} $ + livraison vendeur {d.get('transport', 0):g} $ "
                      f"(+{FRAIS_EBAY_PCT * 100:g}% recharge) = {fmt_ht(r['cout_ebay_ht'])}")
        if r["ccs_total"]:
            lignes.append(f"📦 CCS : 15 + 2×{d['poids']:g} lb + 20 = {r['ccs_total']:g} $ ÷ {r['nb']} "
                          f"= {r['ccs_part']:.2f} $")
        if d.get("douane", 0):
            lignes.append(f"🛃 Douane : {d['douane']:g} $ ÷ {r['nb']} = {r['douane_part']:.2f} $")
        lignes.append(f"   → CCS + douane : {fmt_ht(r['cout_ccs_ht'])}")
    else:
        lignes.append(f"🏪 Achat local : {d['prix_achat']:g} HT")
        if d.get("transport"):
            lignes.append(f"🚚 Transport : {d['transport']:g} HT ÷ {r['nb']} = {r['transport_part']:.0f} HT")

    lignes += [
        f"➡️ Coût d'achat : {fmt_ht(r['cout_achat_ht'])}",
        f"🔧 Réparations : {fmt_ht(r['reparations'])} "
        f"(écran {fmt_ht(d['ecran'])}, batterie {fmt_ht(d['batterie'])}, autres {fmt_ht(d['autres'])})",
        f"🧾 Coût total : {fmt_ht(r['cout_total'])}",
        f"🏷️ Prix de vente : {fmt_ht(d['vente_ht'])}",
        "",
        f"📈 Profit : {fmt_ht(r['profit'])} (≈ {r['profit_usd']:.0f} $)",
    ]
    if ebay and not d.get("douane", 0):
        lignes.append("⚠️ Douane non incluse : profit provisoire.")
    if r["profit_usd"] >= d["cible_usd"]:
        lignes.append(f"✅ Objectif {d['cible_usd']:g} $ atteint.")
    elif r["profit_usd"] >= mini:
        lignes.append(f"⚠️ Au-dessus du minimum ({mini} $), sous ton objectif de {d['cible_usd']:g} $.")
    else:
        lignes.append(f"❌ Sous le minimum strict de {mini} $. À éviter.")

    if r["prix_max"] > 0:
        lignes.append(f"👉 Prix d'achat max pour {d['cible_usd']:g} $ de profit : {r['prix_max']}{u}")
    else:
        lignes.append(f"👉 Impossible d'atteindre {d['cible_usd']:g} $ de profit.")

    texte = "\n".join(lignes)
    context.user_data["dernier_calcul"] = texte
    await say(update, texte, kb(NOUVEAU))
    return ConversationHandler.END


async def annuler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Calcul annulé.", reply_markup=kb(NOUVEAU))
    return ConversationHandler.END


# ---------------- COMMANDES RAPIDES ----------------


async def cmd_ccs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ccs 6 3 -> colis de 6 lb avec 3 telephones"""
    try:
        poids = float(context.args[0].replace(",", "."))
        nb = int(context.args[1]) if len(context.args) > 1 else 1
        if poids <= 0 or nb < 1:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /ccs 6 3  (poids en livres, nombre de téléphones)")
        return
    total = frais_ccs(poids)
    part = total / nb
    await update.message.reply_text(
        f"Colis de {poids:g} lb : 15 + 2×{poids:g} + 20 = {total:g} $\n"
        f"÷ {nb} téléphone(s) = {part:.2f} $ chacun ≈ {fmt_ht(usd_vers_ht(part, TAUX['ccs']))}\n"
        "Droits de douane en plus (selon la valeur)."
    )


async def cmd_ht(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /ht 4000")
        return
    brut = ht_en_usd(val, TAUX["achat"])
    await update.message.reply_text(
        f"{val:g} HT × 5 ÷ {TAUX['achat']} = {brut:.2f} $\n➡️ arrondi : {arrondi_usd(brut)} $"
    )


async def cmd_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /usd 150")
        return
    await update.message.reply_text(
        f"{val:g} $ à la vente ({TAUX['vente']}) = {fmt_ht(usd_vers_ht(val, TAUX['vente']))}\n"
        f"{val:g} $ à l'achat ({TAUX['achat']}) = {fmt_ht(usd_vers_ht(val, TAUX['achat']))}"
    )


async def cmd_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /recharge 100")
        return
    frais = val * FRAIS_EBAY_PCT
    await update.message.reply_text(
        f"Recharge de {val:g} $ (taux {TAUX['ebay']})\n"
        f"Tu paies : {fmt_ht(usd_vers_ht(val, TAUX['ebay']))}\n"
        f"Frais : {frais:g} $ → solde eBay : {val - frais:g} $"
    )


async def cmd_taux(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) == 2 and a[0] in TAUX:
        try:
            TAUX[a[0]] = float(a[1].replace(",", "."))
            sauver()
        except ValueError:
            await update.message.reply_text("Usage : /taux vente 140")
            return
    await update.message.reply_text(
        f"Taux (gourdes pour 1 $) :\n• achat : {TAUX['achat']}\n• vente : {TAUX['vente']}\n"
        f"• ebay : {TAUX['ebay']}\n• ccs : {TAUX['ccs']}\n\nModifier : /taux achat 127"
    )


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) >= 3:
        etat = a[-2].lower()
        modele = trouver_modele(" ".join(a[:-2]))
        if etat not in ETATS or not modele:
            await update.message.reply_text("Usage : /prix 13 bon 270$\n(états : neuf, bon, mauvais)")
            return
        try:
            val, unite = parse_montant(a[-1])
        except ValueError:
            await update.message.reply_text("Montant invalide. Ex : 7560 ou 270$")
            return
        PRIX[modele][etat] = usd_vers_ht(val, TAUX["vente"]) if unite == "usd" else val
        sauver()
        await update.message.reply_text(
            f"✅ {modele} / {ETATS[etat]} = {fmt_ht(PRIX[modele][etat])}\n"
            "Pour le garder après un redémarrage : /export"
        )
        return
    await update.message.reply_text(
        "📋 Prix de revente :\n" + liste_prix_texte() +
        "\n\nModifier : /prix 13 bon 270$  ou  /prix 11 neuf 5000"
    )


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = {
        "taux": TAUX,
        "prix": {m: {e: round(v) for e, v in p.items()} for m, p in PRIX.items() if p},
    }
    await update.message.reply_text(
        "Pour garder tes prix et taux après un redémarrage : sur Render, Environment, "
        "crée la variable DATA_JSON et colle ce texte (le message suivant) dans Value."
    )
    await update.message.reply_text(json.dumps(data, ensure_ascii=False))


# ---------------- QUESTIONS LIBRES -> GEMINI ----------------


async def texte_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    rep = await gemini_repond(
        update.effective_user.id, update.message.text, context.user_data.get("dernier_calcul")
    )
    await update.message.reply_text(rep)


# ---------------- SERVEUR DE SANTÉ (Render) ----------------


class Sante(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass


def lancer_sante():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), Sante).serve_forever()


# ---------------- MAIN ----------------


def main():
    threading.Thread(target=lancer_sante, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(TypeHandler(Update, garde), group=-1)

    def nombre(cle, etat, suite):
        h = saisie_nombre(cle, etat, suite)
        return [MessageHandler(TEXTE, h), CallbackQueryHandler(h, pattern="^zero$")]

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("calcul", nouveau),
            CallbackQueryHandler(nouveau, pattern="^nouveau$"),
        ],
        states={
            MODELE: [CallbackQueryHandler(choisir_modele, pattern="^m:")],
            ETAT: [CallbackQueryHandler(choisir_etat, pattern="^e:")],
            PRIX_VENTE: [MessageHandler(TEXTE, saisir_prix_vente)],
            ACHAT_TYPE: [CallbackQueryHandler(choisir_achat, pattern="^a:")],
            ACHAT_PRIX: [MessageHandler(TEXTE, saisie_nombre("prix_achat", ACHAT_PRIX, ask_transport))],
            TRANSPORT: nombre("transport", TRANSPORT, apres_transport),
            POIDS: [
                CallbackQueryHandler(choisir_poids, pattern="^p:"),
                MessageHandler(TEXTE, saisie_nombre("poids", POIDS, ask_nb)),
            ],
            NB: [CallbackQueryHandler(choisir_nb, pattern="^n:")],
            DOUANE: nombre("douane", DOUANE, ask_ecran),
            ECRAN: nombre("ecran", ECRAN, ask_batterie),
            BATTERIE: nombre("batterie", BATTERIE, ask_autres),
            AUTRES: nombre("autres", AUTRES, ask_cible),
            CIBLE: [CallbackQueryHandler(finir, pattern="^c:"), MessageHandler(TEXTE, finir)],
        },
        fallbacks=[
            CommandHandler("annuler", annuler),
            CallbackQueryHandler(nouveau, pattern="^nouveau$"),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler(["start", "aide"], start))
    app.add_handler(CommandHandler("ccs", cmd_ccs))
    app.add_handler(CommandHandler("ht", cmd_ht))
    app.add_handler(CommandHandler("usd", cmd_usd))
    app.add_handler(CommandHandler("recharge", cmd_recharge))
    app.add_handler(CommandHandler("taux", cmd_taux))
    app.add_handler(CommandHandler("prix", cmd_prix))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(MessageHandler(TEXTE, texte_libre))

    log.info("Bot démarré (Gemini : %s)", GEMINI_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
