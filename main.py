"""Bot Telegram - achat/revente d'iPhones (Haiti). Calcul par colis."""
import json
import logging
import os
import re
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from google.genai import types
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, TypeHandler, filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("iphone-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
HIST_FILE = os.environ.get("HIST_FILE", "historique.json")

G_PAR_HT = 5  # 1 HT = 5 gourdes
TAUX = {"achat": 127, "vente": 140, "ebay": 127}  # gourdes pour 1 $
FRAIS_EBAY = 0.02  # 2 % de frais a la recharge eBay

CCS_ENTREPOT = 15
CCS_SERVICE = 20
CCS_PAR_LB = 2

MODELES = [
    "iPhone XS", "iPhone XS Max", "iPhone XR",
    "iPhone 11", "iPhone 12", "iPhone 12 Pro",
    "iPhone 12 Pro Max", "iPhone 13", "iPhone 13 Pro", "iPhone 13 Pro Max",
]
ETATS = {"neuf": "Neuf", "bon": "Bon état", "mauvais": "Mauvais état"}
PROFIT_MIN = {"iPhone XS": 40, "iPhone XS Max": 40, "iPhone XR": 40}  # $ ; 50 par defaut


def profit_min(modele):
    return PROFIT_MIN.get(modele, 50)


# Grille indicative : nombre = HT ; texte "220$" = dollars US (converti au taux de vente)
PRIX = {
    "iPhone XS": {"neuf": 3500, "bon": 3000, "mauvais": 2500},
    "iPhone XS Max": {"neuf": 3800, "bon": 3300, "mauvais": 2800},
    "iPhone XR": {"neuf": 4000, "bon": 4000, "mauvais": 3700},
    "iPhone 11": {"neuf": 5000, "bon": 4500, "mauvais": 3500},
    "iPhone 12": {"neuf": "220$", "bon": "200$", "mauvais": "190$"},
    "iPhone 12 Pro": {"neuf": "220$", "bon": "200$", "mauvais": "190$"},
    "iPhone 12 Pro Max": {"neuf": "250$", "bon": "220$", "mauvais": "200$"},
    "iPhone 13": {"neuf": "290$", "bon": "270$", "mauvais": "250$"},
    "iPhone 13 Pro": {"neuf": "330$", "bon": "300$", "mauvais": "280$"},
    "iPhone 13 Pro Max": {"neuf": "380$", "bon": "350$", "mauvais": "320$"},
}


def appliquer(data):
    for k, v in data.get("taux", {}).items():
        if k in TAUX:
            TAUX[k] = float(v)
    for m, p in data.get("prix", {}).items():
        if m in PRIX and isinstance(p, dict):
            for e, v in p.items():
                if e in ETATS and isinstance(v, (int, float, str)):
                    PRIX[m][e] = v


def charger():
    """DATA_JSON (variable Render) puis data.json (local). Ne plante jamais."""
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
        log.warning("Sauvegarde locale impossible: %s", e)


charger()

# ---------------- CALCULS ----------------


def usd_vers_ht(usd, taux):
    return usd * taux / G_PAR_HT


def ht_en_usd(ht, taux):
    return ht * G_PAR_HT / taux


def arrondi_usd(x):
    """On oublie les centimes. 148/149 -> 150 ; 147 reste 147."""
    base = int(x)
    return base + (5 - base % 5) if base % 5 >= 3 else base


def fmt_ht(x):
    return f"{round(x):,}".replace(",", " ") + " HT"


def prix_vente(modele, etat):
    v = PRIX.get(modele, {}).get(etat)
    if v is None:
        return None
    if isinstance(v, str):
        return usd_vers_ht(float(v.rstrip("$")), TAUX["vente"])
    return float(v)


def parse_montant(txt, defaut="ht"):
    """'2300' -> HT ; '150$' -> dollars ; '2300ht' -> HT."""
    t = txt.lower().replace(" ", "").replace(",", ".")
    unite = defaut
    for suffixe, u in (("usd", "usd"), ("$", "usd"), ("ht", "ht")):
        if t.endswith(suffixe):
            t, unite = t[: -len(suffixe)], u
            break
    val = float(t)
    if not 0 <= val < 1e9:
        raise ValueError
    return val, unite


def nombre(txt):
    m = re.search(r"\d+(?:[.,]\d+)?", txt)
    if not m:
        raise ValueError
    return float(m.group().replace(",", "."))


def norm(s):
    return s.lower().replace("iphone", "").replace(" ", "")


def trouver_modele(txt):
    return next((m for m in MODELES if norm(m) == norm(txt)), None)


def frais_import(poids, douane):
    """Frais d'importation total du colis, en $ (CCS + douane)."""
    return CCS_ENTREPOT + CCS_SERVICE + CCS_PAR_LB * poids + douane


def cout_achat_ht(achat):
    """achat = [valeur, 'ht'|'usd']. En $ : achat eBay avec 2 % de frais de recharge."""
    v, u = achat
    if u == "usd":
        return usd_vers_ht(v / (1 - FRAIS_EBAY), TAUX["ebay"])
    return v


def calculer_lot(f):
    nb = f["nb"]
    total = frais_import(f["poids"], f["douane"])
    part_usd = total / nb
    part_ht = usd_vers_ht(part_usd, TAUX["achat"])
    tels = []
    for t in f["tels"][:nb]:
        achat_ht = cout_achat_ht(t["achat"])
        cout = achat_ht + part_ht + t["rep"]
        profit_ht = t["vente"] - cout
        tels.append({
            "modele": t["modele"], "etat": t["etat"],
            "vente_ht": round(t["vente"]), "achat_ht": round(achat_ht), "rep_ht": round(t["rep"]),
            "import_ht": round(part_ht), "cout_ht": round(cout), "profit_ht": round(profit_ht),
            "profit_usd": round(ht_en_usd(profit_ht, TAUX["vente"])),
            "marge": round(100 * profit_ht / t["vente"]) if t["vente"] else 0,
        })
    return {
        "nb": nb, "poids": f["poids"], "douane": f["douane"], "import_usd": total,
        "part_usd": part_usd, "tels": tels, "profit_usd": sum(t["profit_usd"] for t in tels),
        "profit_ht": sum(t["profit_ht"] for t in tels),
    }


def recu(r):
    g = lambda x: f"{x:g}"
    lignes = [f"📦 Colis · {r['nb']} tél.", ""]
    alerte = False
    for k, t in enumerate(r["tels"], 1):
        bas = t["profit_usd"] < profit_min(t["modele"])
        alerte = alerte or bas
        lignes += [
            f"#{k} {t['modele']} · {ETATS[t['etat']]}",
            f"Vente {fmt_ht(t['vente_ht'])}",
            f"Achat {fmt_ht(t['achat_ht'])} + réparations {fmt_ht(t['rep_ht'])} + import {fmt_ht(t['import_ht'])}",
            f"Coût total {fmt_ht(t['cout_ht'])}",
            f"💰 Profit {fmt_ht(t['profit_ht'])} ≈ {t['profit_usd']:+d} $ · marge {t['marge']} %"
            + (" ⚠️" if bas else " ✅"),
            "",
        ]
    lignes.append(
        f"🚢 Import {g(r['import_usd'])} $ (15+20+2×{g(r['poids'])}lb+{g(r['douane'])}) "
        f"÷ {r['nb']} = {r['part_usd']:.2f} $ chacun")
    lignes.append(f"✅ PROFIT TOTAL : {fmt_ht(r['profit_ht'])} ≈ {r['profit_usd']:+d} $")
    if alerte:
        lignes.append("⚠️ = sous le profit minimum")
    return "\n".join(lignes)


# ---------------- HISTORIQUE ----------------


def maintenant():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Port-au-Prince"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-5)))


def lire_hist():
    try:
        with open(HIST_FILE, encoding="utf-8") as f:
            h = json.load(f)
        return h if isinstance(h, list) else []
    except Exception:
        return []


def ecrire_hist(h):
    try:
        with open(HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        log.warning("Ecriture %s impossible: %s", HIST_FILE, e)
        return False


def vue_hist():
    h = lire_hist()
    if not h:
        return "📜 Historique vide.", [[("🏠 Menu", "go:menu")]]
    lignes = ["📜 Historique", ""]
    for e in reversed(h[-15:]):
        d = e.get("date", "")
        quand = f"{d[8:10]}/{d[5:7]} {d[11:16]}"
        noms = " + ".join(t["modele"].replace("iPhone ", "") for t in e.get("tels", []))
        lignes.append(f"{quand} · {noms} → {e.get('profit_usd', 0):+d} $")
    total = sum(e.get("profit_usd", 0) for e in h)
    lignes += ["", f"Total ({len(h)} lot{'s' if len(h) > 1 else ''}) : {total:+d} $"]
    return "\n".join(lignes), [[("🗑 Vider", "h:vider"), ("🏠 Menu", "go:menu")]]


def vue_confirmation():
    n = len(lire_hist())
    return (f"⚠️ Effacer tout l'historique ({n} lot{'s' if n > 1 else ''}) ?",
            [[("✅ Oui, effacer", "h:oui"), ("↩️ Non", "h:non")]])


# ---------------- GRILLE DES PRIX ----------------


def texte_prix():
    lignes = ["📊 Grille des prix de vente (HT)", "Neuf · Bon · Mauvais", ""]
    for m in MODELES:
        vals = [prix_vente(m, e) for e in ETATS]
        lignes.append(f"{m.replace('iPhone ', '')} : " + " · ".join(
            f"{round(x):,}".replace(",", " ") if x is not None else "—" for x in vals))
    lignes.append("\nIndicatif : au calcul tu entres ton propre prix.\nModifier : /prix 13 bon 270$")
    return "\n".join(lignes)


def texte_taux():
    return (f"💱 Taux (gourdes pour 1 $)\n\nAchat : {TAUX['achat']:g}\nVente : {TAUX['vente']:g}\n"
            f"eBay : {TAUX['ebay']:g} (+2 % recharge)\n\nModifier : /taux vente 140")


# ---------------- GEMINI ----------------
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
CHAT = defaultdict(lambda: deque(maxlen=10))


def system_prompt():
    return f"""Tu es l'assistant d'un revendeur d'iPhones en Haïti.
STYLE : français, UN SEUL message de 1 à 3 phrases courtes (60 mots max), sans markdown, sans liste.
CALCULS : affichage en HT (1 HT = 5 gourdes), jamais de gourdes. HT vers $ : HT x 5 / taux.
Taux achat = {TAUX['achat']}, vente = {TAUX['vente']}, eBay = {TAUX['ebay']} (+2 % de frais de recharge).
Frais d'importation d'un colis = 15 + 20 + 2 x poids(lb) + douane($), divisés par le nombre de téléphones.
Arrondi des $ : on oublie les centimes ; 148 et 149 deviennent 150, 147 reste 147.
Utilise toujours l'exécution de code pour calculer, jamais de chiffre inventé.
Pour un calcul complet de profit, dis d'utiliser /calcul."""


def nettoyer(texte):
    texte = re.sub(r"[*_`#]+", "", texte).strip()
    if len(texte) > 700:
        texte = texte[:700].rsplit(" ", 1)[0] + "…"
    return texte


async def gemini_repond(user_id, texte):
    if not client:
        return "Gemini non configuré. Tape /menu."
    hist = CHAT[user_id]
    contents = list(hist) + [types.Content(role="user", parts=[types.Part(text=texte)])]
    reponse = ""
    for outils in ([types.Tool(code_execution=types.ToolCodeExecution())], None):
        try:
            cfg = types.GenerateContentConfig(
                system_instruction=system_prompt(), temperature=0.2, max_output_tokens=500, tools=outils)
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


# ---------------- TELEGRAM : INTERFACE ----------------
MENU_TXT = "📱 Menu principal\n\n/ht 4000 → $   ·   /usd 150 → HT"
MENU_ROWS = [
    [("📦 Nouveau calcul", "go:calc"), ("📊 Grille des prix", "go:table")],
    [("📜 Historique", "go:hist"), ("💱 Taux", "go:taux")],
]
MENU_BTN = [[("🏠 Menu", "go:menu")]]


def kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


async def show(update, context, texte, rows=None, flow=False):
    """Modifie le message du bouton, ou en envoie un nouveau (texte simple, sans Markdown)."""
    markup = kb(rows) if rows else None
    f = context.user_data.get("f")
    q = update.callback_query
    if q:
        mid = q.message.message_id
        try:
            await q.message.edit_text(texte, reply_markup=markup)
        except Exception as e:
            if "not modified" not in str(e).lower():
                mid = (await q.message.reply_text(texte, reply_markup=markup)).message_id
    else:
        if flow and f and f.get("mid"):  # retire les boutons de la question precedente
            try:
                await context.bot.edit_message_reply_markup(update.effective_chat.id, f["mid"], reply_markup=None)
            except Exception:
                pass
        mid = (await update.message.reply_text(texte, reply_markup=markup)).message_id
    if flow and f is not None:
        f["mid"] = mid


async def garde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED and update.effective_user and update.effective_user.id not in ALLOWED:
        raise ApplicationHandlerStop


# ---------------- CALCUL PAR LOT (machine a etapes) ----------------
# Ordre par telephone : modele, etat, prix de VENTE, prix d'ACHAT, reparations. Puis poids + douane du colis.


def sequence(f):
    seq = [("nb", 0)]
    nb = f.get("nb")
    if nb:
        for i in range(nb):
            seq += [("modele", i), ("etat", i), ("vente", i), ("achat", i), ("rep", i)]
        seq += [("poids", 0), ("douane", 0)]
    return seq


def repondu(f, step, i):
    if step == "nb":
        return "nb" in f
    if step in ("poids", "douane"):
        return step in f
    return step in f["tels"][i]


def courant(f):
    seq = sequence(f)
    pos = min(f["pos"], len(seq))
    return (seq[pos] if pos < len(seq) else ("result", 0)), pos, seq


def stocker(f, step, i, v):
    if step == "nb":
        f["nb"] = int(v)
        while len(f["tels"]) < f["nb"]:
            f["tels"].append({})
    elif step in ("poids", "douane"):
        f[step] = v
    else:
        f["tels"][i][step] = v


def avancer_pos(f):
    seq = sequence(f)
    for idx, (s, i) in enumerate(seq):
        if not repondu(f, s, i):
            f["pos"] = idx
            return
    f["pos"] = len(seq)


def question(f, step, i):
    tag = f"Tél #{i + 1} · " if f.get("nb", 1) > 1 else ""
    if step == "nb":
        return "📦 Combien de téléphones dans ce colis ?", [[(str(n), f"f:nb:{n}") for n in range(1, 6)]]
    t = f["tels"][i]
    if step == "modele":
        btn = [(m.replace("iPhone ", ""), f"f:mod:{k}") for k, m in enumerate(MODELES)]
        return f"{tag}Modèle ?", [btn[k:k + 3] for k in range(0, len(btn), 3)]
    if step == "etat":
        return f"{tag}{t['modele']} · État ?", [[(label, f"f:etat:{k}") for k, label in ETATS.items()]]
    if step == "vente":
        base = prix_vente(t["modele"], t["etat"])
        txt = (f"{tag}💰 À combien vends-tu ce {t['modele']} ({ETATS[t['etat']]}) ?\n"
               "2300 = HT · 150$ = dollars")
        rows = [[(f"Grille : {fmt_ht(base)}", "f:grille")]] if base else []
        return txt, rows
    if step == "achat":
        return (f"{tag}🛒 À combien l'as-tu acheté ?\n"
                "2300 = HT · 150$ = dollars (eBay, +2 % recharge)"), []
    if step == "rep":
        return f"{tag}🔧 Réparations ? (HT, 0 si aucune)", [[("0", "f:zero")]]
    if step == "poids":
        return "⚖️ Poids total du colis en lbs ?", []
    return "🚚 Douane estimée en $ ? (ex 20 ou 30)", [[("0", "f:zero")]]


async def montrer(update, context):
    f = context.user_data["f"]
    (step, i), pos, _ = courant(f)
    if step == "result":
        rows = [[("✅ Enregistrer", "f:save")], [("⬅️ Retour", "f:back"), ("✖ Annuler", "f:cancel")]]
        await show(update, context, recu(calculer_lot(f)), rows, flow=True)
        return
    texte_q, rows = question(f, step, i)
    rows = list(rows) + [([("⬅️ Retour", "f:back")] if pos > 0 else []) + [("✖ Annuler", "f:cancel")]]
    await show(update, context, texte_q, rows, flow=True)


async def avancer(update, context):
    avancer_pos(context.user_data["f"])
    await montrer(update, context)


async def demarrer(update, context):
    context.user_data["f"] = {"pos": 0, "tels": [], "mid": None}
    await montrer(update, context)


async def cb_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    f = context.user_data.get("f")
    parts = q.data.split(":")
    act = parts[1]
    if not f:
        if act == "save":
            await q.answer("Déjà enregistré ou session expirée.", show_alert=True)
        else:
            await show(update, context, "Session expirée.", MENU_BTN)
        return
    (step, i), pos, _ = courant(f)
    if act == "cancel":
        context.user_data.pop("f", None)
        await show(update, context, "✖ Calcul annulé.", MENU_BTN)
        return
    if act == "back":
        f["pos"] = max(0, pos - 1)
        await montrer(update, context)
        return
    if act == "save" and step == "result":
        r = calculer_lot(f)
        h = lire_hist()
        h.append({"date": maintenant().strftime("%Y-%m-%d %H:%M"), **r})
        ok = ecrire_hist(h)
        context.user_data.pop("f", None)
        suite = "\n\n✔ Enregistré dans l'historique" if ok else "\n\n⚠️ Écriture de l'historique impossible"
        await show(update, context, recu(r) + suite, [[("📦 Nouveau calcul", "go:calc"), ("🏠 Menu", "go:menu")]])
        return
    if act == "nb" and step == "nb":
        v = int(parts[2])
    elif act == "mod" and step == "modele":
        v = MODELES[int(parts[2])]
    elif act == "etat" and step == "etat":
        v = parts[2]
    elif act == "grille" and step == "vente":
        v = prix_vente(f["tels"][i]["modele"], f["tels"][i]["etat"])
        if v is None:
            return
    elif act == "zero" and step in ("rep", "douane"):
        v = 0.0
    else:
        return  # vieux bouton : on ignore
    stocker(f, step, i, v)
    await avancer(update, context)


async def texte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    f = context.user_data.get("f")
    if not f:
        await update.message.chat.send_action("typing")
        await update.message.reply_text(await gemini_repond(update.effective_user.id, update.message.text))
        return
    (step, i), _, _ = courant(f)
    if step in ("modele", "etat", "result"):
        await update.message.reply_text("Utilise les boutons du message précédent.")
        return
    txt = update.message.text
    try:
        if step == "nb":
            v = int(txt.strip())
            if not 1 <= v <= 20:
                raise ValueError
        elif step == "vente":
            val, u = parse_montant(txt)
            if val <= 0:
                raise ValueError
            v = usd_vers_ht(val, TAUX["vente"]) if u == "usd" else val
        elif step == "achat":
            val, u = parse_montant(txt)
            if val <= 0:
                raise ValueError
            v = [val, u]
        elif step == "rep":
            val, u = parse_montant(txt)
            v = usd_vers_ht(val, TAUX["achat"]) if u == "usd" else val
        else:  # poids, douane
            v = nombre(txt)
            if step == "poids" and v <= 0:
                raise ValueError
    except ValueError:
        await update.message.reply_text("Valeur invalide. Exemple : 2300 (HT) ou 150$")
        return
    stocker(f, step, i, v)
    await avancer(update, context)


# ---------------- MENU / COMMANDES ----------------


async def cb_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    act = update.callback_query.data.split(":")[1]
    context.user_data.pop("f", None)
    if act == "calc":
        await demarrer(update, context)
    elif act == "hist":
        await show(update, context, *vue_hist())
    elif act == "table":
        await show(update, context, texte_prix(), MENU_BTN)
    elif act == "taux":
        await show(update, context, texte_taux(), MENU_BTN)
    else:
        await show(update, context, MENU_TXT, MENU_ROWS)


async def cb_hist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    act = update.callback_query.data.split(":")[1]
    if act == "vider":
        await show(update, context, *vue_confirmation())
    elif act == "oui":
        ok = ecrire_hist([])
        await show(update, context, "🗑 Historique vidé." if ok else "⚠️ Impossible de vider.", MENU_BTN)
    else:
        await show(update, context, *vue_hist())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("f", None)
    await show(update, context, MENU_TXT, MENU_ROWS)


async def cmd_calcul(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await demarrer(update, context)


async def cmd_annuler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("f", None)
    await show(update, context, "✖ Calcul annulé.", MENU_BTN)


async def cmd_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show(update, context, *vue_hist())


async def cmd_vider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show(update, context, *vue_confirmation())


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) >= 3:
        etat = a[-2].lower()
        modele = trouver_modele(" ".join(a[:-2]))
        if etat not in ETATS or not modele:
            await update.message.reply_text("Usage : /prix 13 bon 270$\n(états : neuf, bon, mauvais)")
            return
        try:
            val, u = parse_montant(a[-1])
        except ValueError:
            await update.message.reply_text("Montant invalide. Ex : 3000 ou 270$")
            return
        PRIX[modele][etat] = f"{val:g}$" if u == "usd" else val
        sauver()
        await update.message.reply_text(
            f"✅ {modele} · {ETATS[etat]} = {fmt_ht(prix_vente(modele, etat))}\n"
            "Garder après un redémarrage : /export")
        return
    await show(update, context, texte_prix(), MENU_BTN)


async def cmd_taux(update: Update, context: ContextTypes.DEFAULT_TYPE):
    a = context.args
    if len(a) == 2 and a[0].lower() in TAUX:
        try:
            v = float(a[1].replace(",", "."))
            if v <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Usage : /taux vente 140")
            return
        TAUX[a[0].lower()] = v
        sauver()
        await update.message.reply_text("✅ Taux mis à jour.\nGarder après un redémarrage : /export")
    await show(update, context, texte_taux(), MENU_BTN)


async def cmd_ht(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /ht 4000")
        return
    brut = ht_en_usd(val, TAUX["achat"])
    await update.message.reply_text(f"{val:g} HT × 5 ÷ {TAUX['achat']:g} = {brut:.2f} $ → {arrondi_usd(brut)} $")


async def cmd_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0].replace(",", "."))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage : /usd 150")
        return
    await update.message.reply_text(
        f"{val:g} $ → achat {fmt_ht(usd_vers_ht(val, TAUX['achat']))} · vente {fmt_ht(usd_vers_ht(val, TAUX['vente']))}")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Pour garder prix et taux après un redémarrage : Render > Environment > variable DATA_JSON, "
        "colle le message suivant dans Value.")
    await update.message.reply_text(json.dumps({"taux": TAUX, "prix": PRIX}, ensure_ascii=False))


# ---------------- SERVEUR DE SANTE (Render) ----------------


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
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), Sante).serve_forever()


# ---------------- MAIN ----------------


def main():
    threading.Thread(target=lancer_sante, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(TypeHandler(Update, garde), group=-1)

    for noms, fn in (
        (["start", "menu"], cmd_menu), ("calcul", cmd_calcul), ("annuler", cmd_annuler),
        ("historique", cmd_historique), ("vider_historique", cmd_vider),
        ("prix", cmd_prix), ("taux", cmd_taux), ("ht", cmd_ht), ("usd", cmd_usd), ("export", cmd_export),
    ):
        app.add_handler(CommandHandler(noms, fn))

    app.add_handler(CallbackQueryHandler(cb_flow, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(cb_go, pattern=r"^go:"))
    app.add_handler(CallbackQueryHandler(cb_hist, pattern=r"^h:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texte))

    log.info("Bot démarré (Gemini : %s)", GEMINI_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
