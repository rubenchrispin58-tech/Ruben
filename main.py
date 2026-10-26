import os
import time
import sqlite3
import threading
import requests
from flask import Flask
import telebot
from telebot import types

# ====================================================
# CONFIGURATION (variables d'environnement Render)
# ====================================================
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "trading_v21.db")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)       # TON chat_id Telegram

# 1 pip = 0.1 $ (20 $ = 200 pips)
PIP_SIZE = float(os.getenv("PIP_SIZE", "0.1"))
PIP_SIZES = {"BTCUSD": PIP_SIZE, "XAUUSD": float(os.getenv("PIP_SIZE_XAU", "0.1"))}

# Spread XM en $ (BUY - SELL sur ton MT5). Il est compté dans le TP et le SL.
SPREAD = float(os.getenv("SPREAD", "15"))                       # BTCUSD
SPREADS = {"BTCUSD": SPREAD, "XAUUSD": float(os.getenv("SPREAD_XAU", "1.3"))}
# Unités par 1 lot chez XM (BTCUSD : 1 lot = 1 BTC ; Or : 1 lot = 100 onces)
CONTRACTS = {"BTCUSD": 1.0, "XAUUSD": 100.0}
# Distances minimales du TP et du SL, en multiples du spread (le TP doit rester > 1 spread pour gagner)
MIN_TP_SPREADS = max(float(os.getenv("MIN_TP_SPREADS", "2.0")), 1.2)
MIN_SL_SPREADS = float(os.getenv("MIN_SL_SPREADS", "0.5"))

# L'Or est suivi via PAXGUSDT (jeton adossé à l'or, coté 24h/24 sur Binance)
SYMBOLS = {"BTCUSD": "BTCUSDT", "XAUUSD": "PAXGUSDT"}

# Durée de vie d'un signal selon le timeframe (le suivi automatique le marque "expiré" après)
TF_EXPIRY_S = {"M1": 900, "M5": 3600, "M15": 10800, "H1": 43200}
# Sur H1, l'ATR est énorme : on rapproche le TP et le SL pour qu'ils soient atteignables
TF_ATR_CAP = {"H1": {"sl_atr": 1.0, "tp_atr": 2.0}}

TF_MAP = {
    "M1": {"binance": "1m", "bybit": "1"},
    "M5": {"binance": "5m", "bybit": "5"},
    "M15": {"binance": "15m", "bybit": "15"},
    "H1": {"binance": "1h", "bybit": "60"},
}

# min_score : écart minimum entre indicateurs haussiers et baissiers pour donner un signal
#   Express = sensible (1 point d'avance suffit), Intra-Day = plus sélectif (3 points)
MODES = {
    "express": {"sl_atr": 0.8, "tp_atr": 2.0, "expiry_s": 900,
                "min_score": int(os.getenv("MIN_SCORE_EXPRESS", "1")),
                "tiebreak": os.getenv("TIEBREAK_EXPRESS", "1") == "1"},
    "intraday": {"sl_atr": 1.2, "tp_atr": 3.0, "expiry_s": 3600,
                 "min_score": int(os.getenv("MIN_SCORE_INTRADAY", "3"))},
}

DISCLAIMER = "⚠️ _Aucun indicateur n'est fiable à 100 %. Risque max 1-2 % du capital par trade._"

# ====================================================
# 0. FORMATION
# ====================================================
TRAINING_CHAPTERS = {
    "ch1": (
        "📘 **CHAPITRE 1 : C'est quoi le Trading ?**\n"
        "───────────────────────────\n"
        "Le trading consiste à spéculer sur la hausse (BUY) ou la baisse (SELL) "
        "d'un actif (comme BTCUSD) pour encaisser la différence en pips."
    ),
    "ch2": (
        "⏱️ **CHAPITRE 2 : Les Timeframes MT5**\n"
        "───────────────────────────\n"
        "• **M1 / M5 :** Micro-scalping dynamique.\n"
        "• **M15 / H1 :** Analyse des niveaux clés et tendances de fond."
    ),
    "ch3": (
        "🚀 **CHAPITRE 3 : Discipline de Scalpeur**\n"
        "───────────────────────────\n"
        "1. Entre le prix BUY affiché sur ton MT5 XM.\n"
        "2. Place ton Take Profit et ton Stop Loss directement.\n"
        "3. Si le marché se retourne, utilise 🔄 Réanalyser et suis le verdict."
    ),
}

# ====================================================
# 1. SERVEUR FLASK
# ====================================================
app = Flask(__name__)


@app.route('/')
def home():
    return "B2K Algo MT5 Pro v25.0 - Opérationnel !"


def run_flask():
    port = int(os.getenv("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


threading.Thread(target=run_flask, daemon=True).start()

# ====================================================
# 2. BASE DE DONNÉES
# ====================================================
db_lock = threading.Lock()


def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with db_lock:
        conn = db()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                trials_left INTEGER DEFAULT 3,
                is_paid INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS b2k_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                symbol TEXT,
                mode TEXT,
                tf TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp REAL,
                offset REAL,
                rr REAL,
                created_ms INTEGER,
                expiry_ms INTEGER,
                status TEXT DEFAULT 'OPEN',
                closed_ms INTEGER,
                details TEXT,
                manual INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()


init_db()

# ====================================================
# 3. BOT TELEGRAM
# ====================================================
bot = telebot.TeleBot(TOKEN, parse_mode="Markdown", threaded=True, num_threads=16)

testing_as_user = {}
awaiting_price = {}


def clean_delete_message(chat_id, message_id):
    def _delete():
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    threading.Thread(target=_delete, daemon=True).start()


def safe_send(chat_id, text, **kwargs):
    """Envoie en Markdown ; si Telegram refuse le format, renvoie en texte simple."""
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception:
        try:
            plain = text.replace("*", "").replace("_", "").replace("`", "")
            return bot.send_message(chat_id, plain, parse_mode=None,
                                    **{k: v for k, v in kwargs.items() if k != "parse_mode"})
        except Exception as e:
            print("Erreur envoi:", e)


def set_native_commands():
    commands = [
        types.BotCommand("start", "Menu principal 📊"),
        types.BotCommand("menu", "Tableau de bord B2K ⚡"),
        types.BotCommand("payment", "Abonnement VIP (20$) 💳"),
        types.BotCommand("test", "Mode Test Admin ⚙️"),
        types.BotCommand("stats", "Statistiques réelles 📈"),
        types.BotCommand("annonce", "Diffuser une annonce 📣"),
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        print("Erreur commandes:", e)


set_native_commands()


def get_user(chat_id):
    with db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT trials_left, is_paid, is_admin FROM users WHERE chat_id=?", (chat_id,))
        u = cur.fetchone()
        if not u:
            cur.execute("INSERT INTO users (chat_id, trials_left, is_paid, is_admin) VALUES (?, 3, 0, 0)", (chat_id,))
            conn.commit()
            u = (3, 0, 0)
        if ADMIN_ID and chat_id == ADMIN_ID and (u[1] != 1 or u[2] != 1):
            cur.execute("UPDATE users SET is_admin=1, is_paid=1 WHERE chat_id=?", (chat_id,))
            conn.commit()
            u = (u[0], 1, 1)
        conn.close()
    return u


def is_admin(chat_id):
    return get_user(chat_id)[2] == 1


def has_access(chat_id):
    trials, is_paid, admin = get_user(chat_id)
    if testing_as_user.get(chat_id, False):
        return trials > 0
    if is_paid or admin:
        return True
    return trials > 0


def consume_trial(chat_id):
    trials, is_paid, admin = get_user(chat_id)
    if testing_as_user.get(chat_id, False) or (not is_paid and not admin):
        with db_lock:
            conn = db()
            conn.execute("UPDATE users SET trials_left = MAX(trials_left - 1, 0) WHERE chat_id=?", (chat_id,))
            conn.commit()
            conn.close()


def deny_admin(chat_id):
    safe_send(chat_id, "⛔ Commande réservée à l'administrateur.")

# ====================================================
# 4. DONNÉES DE MARCHÉ
# ====================================================
BINANCE_BASES = ("https://api.binance.com", "https://data-api.binance.vision")


def fetch_candles(symbol, tf, limit=300):
    """Retourne (highs, lows, closes) du plus ancien au plus récent, ou None."""
    market_symbol = SYMBOLS[symbol]
    cfg = TF_MAP[tf]

    for base in BINANCE_BASES:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": market_symbol, "interval": cfg["binance"], "limit": limit},
                timeout=8,
            )
            if r.status_code == 200:
                rows = r.json()
                if len(rows) >= 60:
                    return ([float(x[2]) for x in rows], [float(x[3]) for x in rows], [float(x[4]) for x in rows])
        except Exception:
            pass

    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": market_symbol, "interval": cfg["bybit"], "limit": limit},
            timeout=8,
        )
        if r.status_code == 200:
            rows = list(reversed(r.json()["result"]["list"]))
            if len(rows) >= 60:
                return ([float(x[2]) for x in rows], [float(x[3]) for x in rows], [float(x[4]) for x in rows])
    except Exception:
        pass
    return None


def fetch_range(symbol, start_ms):
    """Bougies M1 depuis start_ms : liste de (open_time_ms, high, low), ou None si échec."""
    market_symbol = SYMBOLS[symbol]
    for base in BINANCE_BASES:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": market_symbol, "interval": "1m", "startTime": int(start_ms), "limit": 1000},
                timeout=8,
            )
            if r.status_code == 200:
                return [(int(x[0]), float(x[2]), float(x[3])) for x in r.json()]
        except Exception:
            pass
    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": market_symbol, "interval": "1",
                    "start": int(start_ms), "limit": 1000},
            timeout=8,
        )
        if r.status_code == 200:
            rows = r.json()["result"]["list"]
            return sorted((int(x[0]), float(x[2]), float(x[3])) for x in rows)
    except Exception:
        pass
    return None

# ====================================================
# 5. INDICATEURS
# ====================================================


def sma(values, n):
    return sum(values[-n:]) / n


def ema_series(values, n):
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    out = [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd(closes, fast=12, slow=26, sig=9):
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    ef = ef[len(ef) - len(es):]
    line = [a - b for a, b in zip(ef, es)]
    sg = ema_series(line, sig)
    hist_now = line[-1] - sg[-1]
    hist_prev = line[-2] - sg[-2] if len(sg) >= 2 else hist_now
    return line[-1], sg[-1], hist_now, hist_prev


def bollinger(closes, n=20, k=2):
    window = closes[-n:]
    mid = sum(window) / n
    sd = (sum((x - mid) ** 2 for x in window) / n) ** 0.5
    upper, lower = mid + k * sd, mid - k * sd
    pct_b = 0.5 if upper == lower else (closes[-1] - lower) / (upper - lower)
    return pct_b, mid


def stochastic(highs, lows, closes, n=14, d=3):
    ks = []
    for end in range(len(closes) - d + 1, len(closes) + 1):
        hh = max(highs[end - n:end])
        ll = min(lows[end - n:end])
        ks.append(50.0 if hh == ll else (closes[end - 1] - ll) / (hh - ll) * 100)
    return ks[-1], sum(ks) / len(ks)


def true_ranges(highs, lows, closes):
    return [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))]


def atr(highs, lows, closes, n=14):
    trs = true_ranges(highs, lows, closes)
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def adx_di(highs, lows, closes, n=14):
    trs = true_ranges(highs, lows, closes)
    pdm, mdm = [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0)
        mdm.append(dn if (dn > up and dn > 0) else 0)
    s_tr, s_p, s_m = sum(trs[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    pdi = mdi = 0.0
    for i in range(n, len(trs) + 1):
        if i > n:
            s_tr = s_tr - s_tr / n + trs[i - 1]
            s_p = s_p - s_p / n + pdm[i - 1]
            s_m = s_m - s_m / n + mdm[i - 1]
        pdi = 100 * s_p / s_tr if s_tr else 0
        mdi = 100 * s_m / s_tr if s_tr else 0
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
    adx_v = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        adx_v = (adx_v * (n - 1) + dx) / n
    return adx_v, pdi, mdi


def cci(highs, lows, closes, n=20):
    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    w = tp[-n:]
    m = sum(w) / n
    md = sum(abs(x - m) for x in w) / n
    return 0.0 if md == 0 else (tp[-1] - m) / (0.015 * md)


def williams_r(highs, lows, closes, n=14):
    hh, ll = max(highs[-n:]), min(lows[-n:])
    return -50.0 if hh == ll else (hh - closes[-1]) / (hh - ll) * -100


def momentum_roc(closes, n=10):
    return (closes[-1] - closes[-1 - n]) / closes[-1 - n] * 100


def analyze(highs, lows, closes):
    """Retourne (liste de (nom, vote, détail), atr). vote: +1 achat, -1 vente, 0 neutre."""
    votes = []

    r = rsi(closes)
    if r < 30:
        v = 1
    elif r > 70:
        v = -1
    else:
        v = 1 if r > 55 else (-1 if r < 45 else 0)
    votes.append(("RSI 14", v, f"{r:.1f}"))

    m_line, m_sig, h_now, h_prev = macd(closes)
    v = 1 if (m_line > m_sig and h_now >= h_prev) else (-1 if (m_line < m_sig and h_now <= h_prev) else 0)
    votes.append(("MACD", v, f"hist {h_now:+.2f}"))

    pb, _mid = bollinger(closes)
    v = 1 if pb < 0.2 else (-1 if pb > 0.8 else 0)
    votes.append(("Bollinger", v, f"%B {pb:.2f}"))

    e9 = ema_series(closes, 9)[-1]
    e21 = ema_series(closes, 21)[-1]
    v = 1 if e9 > e21 else -1
    votes.append(("EMA 9/21", v, "haussier" if v == 1 else "baissier"))

    e50 = ema_series(closes, 50)[-1]
    e200 = ema_series(closes, 200)[-1] if len(closes) >= 210 else sma(closes, min(100, len(closes)))
    v = 1 if (closes[-1] > e50 and e50 > e200) else (-1 if (closes[-1] < e50 and e50 < e200) else 0)
    votes.append(("Tendance EMA 50/200", v, "haussière" if v == 1 else ("baissière" if v == -1 else "mixte")))

    k, d = stochastic(highs, lows, closes)
    if k > 80:
        v = -1
    elif k < 20:
        v = 1
    else:
        v = 1 if k > d else -1
    votes.append(("Stochastique", v, f"K {k:.0f} / D {d:.0f}"))

    adx_v, pdi, mdi = adx_di(highs, lows, closes)
    v = (1 if pdi > mdi else -1) if adx_v > 20 else 0
    votes.append(("ADX / DI", v, f"ADX {adx_v:.0f}"))

    c = cci(highs, lows, closes)
    v = 1 if c < -100 else (-1 if c > 100 else 0)
    votes.append(("CCI 20", v, f"{c:.0f}"))

    w = williams_r(highs, lows, closes)
    v = 1 if w < -80 else (-1 if w > -20 else 0)
    votes.append(("Williams %R", v, f"{w:.0f}"))

    roc = momentum_roc(closes)
    v = 1 if roc > 0.05 else (-1 if roc < -0.05 else 0)
    votes.append(("Momentum ROC", v, f"{roc:+.2f}%"))

    return votes, atr(highs, lows, closes)

# ====================================================
# 6. SUIVI DES SIGNAUX ET STATISTIQUES
# ====================================================


def evaluate_signal(side, sl, tp, offset, candles, created_ms, expiry_ms):
    """Retourne 'TP', 'SL' ou None. TP et SL dans la même bougie : SL (prudent)."""
    for t, hi, lo in candles:
        if t <= created_ms:
            continue
        if t > created_ms + expiry_ms:
            break
        hi += offset
        lo += offset
        if side == "BUY":
            sl_hit, tp_hit = lo <= sl, hi >= tp
        else:
            sl_hit, tp_hit = hi >= sl, lo <= tp
        if sl_hit:
            return "SL"
        if tp_hit:
            return "TP"
    return None


def save_signal(chat_id, symbol, mode, tf, info):
    now_ms = int(time.time() * 1000)
    with db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO b2k_signals (chat_id, symbol, mode, tf, side, entry, sl, tp, offset, rr, "
            "created_ms, expiry_ms, details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chat_id, symbol, mode, tf, info["side"], info["entry"], info["sl"], info["tp"],
             info["offset"], info["rr"], now_ms,
             info.get("expiry_s", MODES[mode]["expiry_s"]) * 1000, info.get("details", "")),
        )
        sid = cur.lastrowid
        conn.commit()
        conn.close()
    return sid


SIGNAL_KEYS = ["id", "chat_id", "symbol", "mode", "tf", "side", "entry", "sl", "tp", "offset", "rr",
               "created_ms", "expiry_ms", "status", "details"]


def get_signal(sid):
    with db_lock:
        conn = db()
        row = conn.execute(
            "SELECT id, chat_id, symbol, mode, tf, side, entry, sl, tp, offset, rr, created_ms, expiry_ms, "
            "status, details FROM b2k_signals WHERE id=?", (sid,)).fetchone()
        conn.close()
    return dict(zip(SIGNAL_KEYS, row)) if row else None


def set_result(sid, result):
    """Enregistre le résultat réel confirmé par l'utilisateur (prioritaire sur le suivi automatique)."""
    with db_lock:
        conn = db()
        conn.execute("UPDATE b2k_signals SET status=?, closed_ms=?, manual=1 WHERE id=?",
                     (result, int(time.time() * 1000), sid))
        conn.commit()
        conn.close()


def track_once():
    with db_lock:
        conn = db()
        rows = conn.execute(
            "SELECT id, symbol, side, sl, tp, offset, created_ms, expiry_ms FROM b2k_signals WHERE status='OPEN'"
        ).fetchall()
        conn.close()
    now_ms = int(time.time() * 1000)
    for sid, symbol, side, sl, tp, offset, created_ms, expiry_ms in rows:
        candles = fetch_range(symbol, created_ms)
        if candles is None:
            continue
        res = evaluate_signal(side, sl, tp, offset, candles, created_ms, expiry_ms)
        if res is None and now_ms > created_ms + expiry_ms + 120000:
            res = "EXPIRED"
        if res:
            with db_lock:
                conn = db()
                conn.execute("UPDATE b2k_signals SET status=?, closed_ms=? WHERE id=? AND status='OPEN'",
                             (res, now_ms, sid))
                conn.commit()
                conn.close()


def tracker_loop():
    while True:
        try:
            track_once()
        except Exception as e:
            print("Erreur suivi:", e)
        time.sleep(45)


def stats_text():
    with db_lock:
        conn = db()
        rows = conn.execute(
            "SELECT mode, status, COUNT(*), COALESCE(SUM(rr), 0) FROM b2k_signals GROUP BY mode, status").fetchall()
        manual = conn.execute(
            "SELECT COUNT(*) FROM b2k_signals WHERE manual=1").fetchone()[0]
        conn.close()
    if not rows:
        return "📈 **STATISTIQUES B2K**\n───────────────────────────\nAucun signal enregistré pour le moment."

    per_mode = {}
    for mode, status, n, rr_sum in rows:
        d = per_mode.setdefault(mode, {"TP": 0, "SL": 0, "OPEN": 0, "EXPIRED": 0, "rr_tp": 0.0})
        d[status] = n
        if status == "TP":
            d["rr_tp"] = rr_sum

    def block(d):
        closed = d["TP"] + d["SL"]
        win = (d["TP"] / closed * 100) if closed else 0
        return closed, win, d["rr_tp"] - d["SL"]

    tot = {"TP": 0, "SL": 0, "OPEN": 0, "EXPIRED": 0, "rr_tp": 0.0}
    for d in per_mode.values():
        for k in tot:
            tot[k] += d[k]
    closed, win, net_r = block(tot)

    text = (
        "📈 **STATISTIQUES RÉELLES B2K**\n"
        "───────────────────────────\n"
        f"✅ Gagnés (TP touché) : **{tot['TP']}**\n"
        f"❌ Perdus (SL touché) : **{tot['SL']}**\n"
        f"⏳ En cours : {tot['OPEN']}  |  ⌛ Expirés : {tot['EXPIRED']}\n\n"
        f"🎯 **Winrate réel :** {win:.1f} % sur {closed} trades clôturés\n"
        f"💰 **Résultat net :** {net_r:+.1f} R  (spread compris, 1 R = la perte si SL)\n"
        f"📊 **Par trade :** {(net_r / closed if closed else 0):+.2f} R\n"
        f"✋ Résultats confirmés à la main : {manual}\n\n"
        "**Par mode :**\n"
    )
    names = {"express": "⚡ Express", "intraday": "🚀 Intra-Day"}
    for mode in ("express", "intraday"):
        if mode in per_mode:
            c, w, nr = block(per_mode[mode])
            text += f"• {names[mode]} : {per_mode[mode]['TP']}✅ / {per_mode[mode]['SL']}❌ — {w:.0f} % — {nr:+.1f} R\n"
    text += (
        "\n_Le résultat net en R compte plus que le taux de réussite. "
        "Attends au moins 30 trades clôturés par mode avant de conclure._"
    )
    return text

# ====================================================
# 7. GÉNÉRATION DU SIGNAL
# ====================================================


def fmt(x):
    return f"{x:,.2f}".replace(",", " ")


def vote_lines(votes):
    lines = []
    for name, v, detail in votes:
        icon = "🟢" if v == 1 else ("🔴" if v == -1 else "⚪")
        lines.append(f"{icon} {name} : {detail}")
    return "\n".join(lines)


def micro_bias(closes):
    """Direction à très court terme (+1 hausse, -1 baisse, 0 pas clair).
    Sert uniquement à départager une égalité d'indicateurs. Les 3 conditions doivent être d'accord :
    pente de l'EMA 9, variation des 3 dernières bougies, prix par rapport à l'EMA 9."""
    e9 = ema_series(closes, 9)
    slope = e9[-1] - e9[-4]
    move3 = closes[-1] - closes[-4]
    if slope > 0 and move3 > 0 and closes[-1] > e9[-1]:
        return 1
    if slope < 0 and move3 < 0 and closes[-1] < e9[-1]:
        return -1
    return 0


def market_note(symbol):
    """Avertissement (sans blocage) quand le marché de l'Or est probablement fermé chez XM."""
    if symbol != "XAUUSD":
        return ""
    t = time.gmtime()
    closed = t.tm_wday == 5 or (t.tm_wday == 4 and t.tm_hour >= 21) or (t.tm_wday == 6 and t.tm_hour < 22)
    if closed:
        return "\n⚠️ _L'Or est probablement fermé chez XM (week-end) : ton MT5 peut être figé._"
    return ""


def compute_levels(side, ask0, atr_v, cfg, market_price, spread):
    """TP/SL en tenant compte du spread.

    ask0 = prix BUY affiché sur MT5, bid0 = ask0 - spread (prix SELL).
    BUY entre à l'ask et sort au bid ; SELL entre au bid et sort à l'ask.
    Les distances sont mesurées depuis le prix de SORTIE, donc le spread est déjà payé dans les niveaux.
    Retourne (entry, tp, sl, gain_net, perte_nette, offset_de_suivi).
    """
    bid0 = ask0 - spread
    sld = max(atr_v * cfg["sl_atr"], spread * MIN_SL_SPREADS)
    tpd = max(atr_v * cfg["tp_atr"], spread * MIN_TP_SPREADS)
    if side == "BUY":
        entry = ask0
        tp, sl = bid0 + tpd, bid0 - sld
        gain, loss = tp - entry, entry - sl
        offset = bid0 - market_price
    else:
        entry = bid0
        tp, sl = ask0 - tpd, ask0 + sld
        gain, loss = entry - tp, sl - entry
        offset = ask0 - market_price
    return entry, tp, sl, gain, loss, offset


def build_signal(symbol, mode, tf, mt5_price=None):
    """Retourne (texte, info, erreur). info est None s'il n'y a pas de signal."""
    data = fetch_candles(symbol, tf)
    if not data:
        return None, None, "❌ Impossible de récupérer les données du marché. Réessaie dans un instant."
    highs, lows, closes = data
    market_price = closes[-1]

    spread = SPREADS[symbol]
    pip = PIP_SIZES[symbol]
    # ask0 = prix BUY de ton MT5 (saisi), sinon estimation à partir du marché
    ask0 = mt5_price if mt5_price else market_price + spread / 2

    votes, atr_v = analyze(highs, lows, closes)
    buys = sum(1 for _, v, _ in votes if v == 1)
    sells = sum(1 for _, v, _ in votes if v == -1)
    score = buys - sells
    cfg = dict(MODES[mode])
    for k, cap in TF_ATR_CAP.get(tf, {}).items():
        cfg[k] = min(cfg[k], cap)
    counts = f"🧮 Indicateurs : 🟢 {buys} achat / 🔴 {sells} vente / ⚪ {10 - buys - sells} neutre"

    head = f"🎯 **SIGNAL B2K (XM) | {symbol} {tf}**\n───────────────────────────\n"

    # Égalité d'indicateurs (ex. 5 contre 5) : en Express, on départage avec le momentum des dernières bougies.
    # Ce n'est pas du hasard : si le momentum n'est pas clair non plus, il n'y a toujours pas de signal.
    tie_break = False
    if score == 0 and cfg.get("tiebreak"):
        mb = micro_bias(closes)
        if mb != 0:
            score = mb
            tie_break = True

    if abs(score) < cfg["min_score"]:
        if score > 0:
            lean = f"↗️ Léger penchant **ACHAT** ({buys} contre {sells}) — confiance très faible."
        elif score < 0:
            lean = f"↘️ Léger penchant **VENTE** ({sells} contre {buys}) — confiance très faible."
        else:
            lean = "⚖️ Indicateurs à égalité et momentum pas clair : aucune direction lisible."
        text = (head + "⏸️ **PAS DE SIGNAL CLAIR — ATTENDS**\n" + lean
                + "\nAucun essai gratuit n'est décompté.\n\n" + counts + "\n\n" + DISCLAIMER)
        return text, None, None

    side = "BUY" if score > 0 else "SELL"
    entry, tp, sl, gain, loss, offset = compute_levels(side, ask0, atr_v, cfg, market_price, spread)
    rr = gain / loss if loss > 0 else 0.0
    breakeven = loss / (gain + loss) * 100 if (gain + loss) > 0 else 100.0

    agree, against = max(buys, sells), min(buys, sells)
    net = abs(score)
    if tie_break:
        force = "TRÈS FAIBLE ⚠️ (égalité départagée par le momentum)"
    elif net >= 5:
        force = "FORT 🔥"
    elif net >= 3:
        force = "MOYEN"
    else:
        force = "FAIBLE ⚠️ (penchant léger)"
    order_str = "🟢 **ACHAT (BUY)**" if side == "BUY" else "🔴 **VENTE (SELL)**"
    price_label = "prix BUY" if side == "BUY" else "prix SELL"
    lot_k = CONTRACTS[symbol] * 0.01
    expiry_s = max(MODES[mode]["expiry_s"], TF_EXPIRY_S.get(tf, 0))

    text = (
        head
        + f"{order_str}\n\n"
        + f"📍 **Entrée XM :** `{entry:.2f}` *({price_label})*\n"
        + f"🎯 **Take Profit :** `{tp:.2f}` *(gain net +{gain / pip:,.0f} pips)*\n"
        + f"🛑 **Stop Loss :** `{sl:.2f}` *(perte nette -{loss / pip:,.0f} pips)*\n\n"
        + f"📊 **{agree} indicateurs contre {against}** — Force : **{force}**\n"
        + f"💸 Spread de {spread:g} $ déjà compté dans le TP et le SL.\n"
        + f"🧾 À **0.01 lot** : gain ≈ +{gain * lot_k:.2f} $ / perte ≈ -{loss * lot_k:.2f} $\n"
        + f"🔒 À **+{gain / 2:.2f} $** de gain (moitié du chemin), remonte ton SL au prix d'entrée.\n\n"
        + "💡 *Place ton TP et ton SL sur MT5. Si le marché change, appuie sur 🔄 Réanalyser.*"
        + market_note(symbol) + "\n"
        + DISCLAIMER
    )
    details = (
        f"📋 **DÉTAILS DES INDICATEURS | {symbol} {tf}**\n"
        "───────────────────────────\n"
        + vote_lines(votes) + "\n\n" + counts + "\n"
        + (f"🧭 Égalité départagée par le momentum court terme ({'hausse' if side == 'BUY' else 'baisse'}).\n"
           if tie_break else "")
        + f"📏 ATR {tf} : {fmt(atr_v)} $\n"
        + f"💸 Spread : {spread:.2f} $\n"
        + f"💰 Gain net si TP : +{gain:.2f} $  |  Perte nette si SL : -{loss:.2f} $\n"
        + f"⚖️ R/R net : 1:{rr:.2f}  |  Winrate nécessaire pour ne pas perdre : {breakeven:.0f} %\n"
        + f"⏳ Suivi automatique pendant {expiry_s // 3600 if expiry_s >= 3600 else expiry_s // 60} "
        + f"{'h' if expiry_s >= 3600 else 'min'}"
    )
    info = {"side": side, "entry": entry, "sl": sl, "tp": tp, "offset": offset, "rr": rr,
            "details": details, "expiry_s": expiry_s}
    return text, info, None


def signal_markup(symbol, mode, tf, sid=None, status="OPEN"):
    markup = types.InlineKeyboardMarkup(row_width=1)
    if sid:
        if status == "OPEN":
            markup.add(types.InlineKeyboardButton("🔄 Réanalyser le marché", callback_data=f"rean_{sid}"))
        markup.add(types.InlineKeyboardButton("📋 Voir les détails", callback_data=f"det_{sid}"))
        if status == "OPEN":
            markup.row(
                types.InlineKeyboardButton("✅ TP touché", callback_data=f"res_{sid}_TP"),
                types.InlineKeyboardButton("❌ SL touché", callback_data=f"res_{sid}_SL"),
            )
    markup.add(
        types.InlineKeyboardButton("📊 Nouvelle Analyse", callback_data=f"sig_{symbol}_{mode}_{tf}"),
        types.InlineKeyboardButton("Menu Principal", callback_data="back_home"),
    )
    return markup


def run_signal(chat_id, symbol, mode, tf, mt5_price=None):
    if not has_access(chat_id):
        send_trial_over(chat_id)
        return
    wait_msg = safe_send(chat_id, "⏳ Analyse des 10 indicateurs en cours...")
    try:
        text, info, err = build_signal(symbol, mode, tf, mt5_price)
    except Exception as e:
        print("Erreur analyse:", e)
        text, info, err = None, None, "❌ Erreur pendant l'analyse. Réessaie."
    if wait_msg:
        clean_delete_message(chat_id, wait_msg.message_id)
    if err:
        safe_send(chat_id, err, reply_markup=signal_markup(symbol, mode, tf))
        return

    sid = None
    if info:
        consume_trial(chat_id)
        sid = save_signal(chat_id, symbol, mode, tf, info)
        trials, is_paid, admin = get_user(chat_id)
        if testing_as_user.get(chat_id, False) or (not is_paid and not admin):
            text += f"\n\n🎁 **Essais restants :** `{trials}/3`"
    safe_send(chat_id, text, reply_markup=signal_markup(symbol, mode, tf, sid))

# ====================================================
# 8. RÉANALYSE
# ====================================================


def build_reanalysis(sig):
    """Retourne (texte, renversement, erreur)."""
    data = fetch_candles(sig["symbol"], sig["tf"])
    if not data:
        return None, False, "❌ Impossible de récupérer les données du marché. Réessaie dans un instant."
    highs, lows, closes = data
    votes, _atr = analyze(highs, lows, closes)
    buys = sum(1 for _, v, _ in votes if v == 1)
    sells = sum(1 for _, v, _ in votes if v == -1)
    score = buys - sells
    side = sig["side"]
    support = score if side == "BUY" else -score
    min_score = MODES[sig["mode"]]["min_score"]
    reversal_at = max(2, min_score)

    exit_price = closes[-1] + sig["offset"]          # prix auquel tu fermerais maintenant
    pnl = (exit_price - sig["entry"]) * (1 if side == "BUY" else -1)
    pnl_pips = pnl / PIP_SIZES.get(sig["symbol"], PIP_SIZE)
    total_gain = abs(sig["tp"] - sig["entry"])
    progress = pnl / total_gain if total_gain > 0 else 0.0
    side_str = "🟢 ACHAT (BUY)" if side == "BUY" else "🔴 VENTE (SELL)"
    opposite = "SELL" if side == "BUY" else "BUY"

    head = (
        f"🔄 **RÉANALYSE | {sig['symbol']} {sig['tf']}**\n"
        "───────────────────────────\n"
        f"📌 **Ta position :** {side_str} à `{sig['entry']:.2f}`\n"
        f"💱 **Si tu fermes maintenant :** `{pnl:+.2f} $` *(≈ {pnl_pips:+,.0f} pips, spread compris)*\n"
        f"🎯 TP `{sig['tp']:.2f}`  |  🛑 SL `{sig['sl']:.2f}`\n\n"
    )

    reversal = False
    if sig["status"] == "TP":
        verdict = "✅ **TRADE GAGNANT** — ton Take Profit est enregistré comme touché."
    elif sig["status"] == "SL":
        verdict = "❌ **TRADE PERDANT** — ton Stop Loss est enregistré comme touché."
    elif sig["status"] == "EXPIRED":
        verdict = "⌛ **SIGNAL EXPIRÉ** — il date trop pour être suivi. Fais une nouvelle analyse."
    elif support >= min_score:
        verdict = ("✅ **TENDANCE CONFIRMÉE**\n"
                   "👉 *Garde ta position. Ne touche ni au SL ni au TP.*")
    elif support <= -reversal_at:
        reversal = True
        verdict = (f"🚨 **RENVERSEMENT DÉTECTÉ**\n"
                   f"Les indicateurs poussent maintenant dans l'autre sens ({opposite}).\n"
                   "👉 *Ferme ta position sur MT5 maintenant pour limiter la perte ou sécuriser le gain. "
                   f"N'ajoute pas de position {side}.*\n"
                   f"🎯 Pour un trade {opposite}, lance un nouveau signal ci-dessous.")
    else:
        verdict = ("⚠️ **SIGNAL AFFAIBLI — MARCHÉ INDÉCIS**\n"
                   "👉 *Si tu es en gain, remonte ton SL au prix d'entrée (break-even). "
                   "Si tu es en perte, laisse le SL en place et attends. N'ajoute rien.*")

    secure = ""
    if sig["status"] == "OPEN" and pnl > 0:
        pct = progress * 100
        if progress >= 0.8:
            secure = (f"\n\n🔒 **SÉCURISE TON GAIN** — tu as fait {pct:.0f} % du chemin vers le TP.\n"
                      "👉 *Ferme ta position (ou au moins la moitié), ou resserre ton SL juste sous le prix actuel.*")
        elif progress >= 0.5:
            secure = (f"\n\n🔒 **PROTÈGE TON GAIN** — tu as fait {pct:.0f} % du chemin vers le TP.\n"
                      f"👉 *Remonte ton SL au prix d'entrée `{sig['entry']:.2f}` : tu ne devrais plus perdre (sauf saut de prix).*")

    text = (
        head + verdict + secure
        + f"\n\n🧮 Indicateurs : 🟢 {buys} achat / 🔴 {sells} vente / ⚪ {10 - buys - sells} neutre\n\n"
        + DISCLAIMER
    )
    return text, reversal, None


def run_reanalysis(chat_id, sid):
    sig = get_signal(sid)
    if not sig or (sig["chat_id"] != chat_id and not is_admin(chat_id)):
        safe_send(chat_id, "❌ Signal introuvable. Fais une nouvelle analyse.")
        return
    wait_msg = safe_send(chat_id, "⏳ Réanalyse du marché en cours...")
    try:
        text, reversal, err = build_reanalysis(sig)
    except Exception as e:
        print("Erreur réanalyse:", e)
        text, reversal, err = None, False, "❌ Erreur pendant la réanalyse. Réessaie."
    if wait_msg:
        clean_delete_message(chat_id, wait_msg.message_id)
    if err:
        safe_send(chat_id, err)
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    if reversal or sig["status"] in ("TP", "SL", "EXPIRED"):
        markup.add(types.InlineKeyboardButton(f"🎯 Nouveau signal {sig['symbol']} {sig['tf']}",
                                              callback_data=f"sig_{sig['symbol']}_{sig['mode']}_{sig['tf']}"))
    if sig["status"] == "OPEN":
        markup.add(types.InlineKeyboardButton("🔄 Réanalyser encore", callback_data=f"reanb_{sid}"))
    markup.add(types.InlineKeyboardButton("Menu Principal", callback_data="back_home"))
    safe_send(chat_id, text, reply_markup=markup)

# ====================================================
# 9. COMMANDES ET ADMINISTRATION
# ====================================================


@bot.message_handler(commands=['adminrubenb2k'])
def admin_secret_cmd(message):
    chat_id = message.chat.id
    if ADMIN_ID:
        safe_send(chat_id, "⛔ L'admin est défini par variable d'environnement.")
        return
    with db_lock:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM users WHERE is_admin=1")
        row = cur.fetchone()
        if row and row[0] != chat_id:
            conn.close()
            safe_send(chat_id, "⛔ Clé administrative déjà verrouillée.")
            return
        exists = cur.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if not exists:
            cur.execute("INSERT INTO users (chat_id, trials_left, is_paid, is_admin) VALUES (?, 3, 0, 0)", (chat_id,))
        cur.execute("UPDATE users SET is_admin=1, is_paid=1 WHERE chat_id=?", (chat_id,))
        conn.commit()
        conn.close()
    safe_send(chat_id, "🔑 **ACCÈS ADMINISTRATEUR ACTIVÉ !**\n\nBienvenue Ruben (@b2k_ruben).")


@bot.message_handler(commands=['vip'])
def activate_vip_user(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        deny_admin(chat_id)
        return
    try:
        target_id = int(message.text.split()[1])
        get_user(target_id)
        with db_lock:
            conn = db()
            conn.execute("UPDATE users SET is_paid=1 WHERE chat_id=?", (target_id,))
            conn.commit()
            conn.close()
        safe_send(chat_id, f"✅ **Utilisateur ID {target_id} activé VIP !**")
        try:
            bot.send_message(target_id, "⭐ **Ton accès VIP B2K Algo est activé !** Tape /menu pour commencer.")
        except Exception:
            pass
    except Exception:
        safe_send(chat_id, "Format : `/vip CHAT_ID`")


@bot.message_handler(commands=['test'])
def toggle_test_mode(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        deny_admin(chat_id)
        return
    now = not testing_as_user.get(chat_id, False)
    testing_as_user[chat_id] = now
    if now:
        with db_lock:
            conn = db()
            conn.execute("UPDATE users SET trials_left=3 WHERE chat_id=?", (chat_id,))
            conn.commit()
            conn.close()
    status = "ACTIVÉ (Simulation 3 essais client)" if now else "DÉSACTIVÉ (Mode Admin)"
    safe_send(chat_id, f"⚙️ **MODE TEST UTILISATEUR :** {status}")


@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        deny_admin(chat_id)
        return
    safe_send(chat_id, stats_text())


@bot.message_handler(commands=['annonce'])
def broadcast_annonce(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        deny_admin(chat_id)
        return
    msg = safe_send(chat_id, "📢 **ENVOI D'ANNONCE**\n\nEnvoie le message ou la photo à diffuser à tous les utilisateurs :\n(Tape /menu pour annuler)")
    if msg:
        bot.register_next_step_handler(msg, process_broadcast)


def process_broadcast(message):
    if message.text and message.text.startswith("/"):
        safe_send(message.chat.id, "↩️ Annonce annulée.")
        return
    with db_lock:
        conn = db()
        all_users = conn.execute("SELECT chat_id FROM users").fetchall()
        conn.close()
    count = 0
    for (uid,) in all_users:
        try:
            if message.photo:
                bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption, parse_mode=None)
            else:
                bot.send_message(uid, message.text, parse_mode=None)
            count += 1
        except Exception:
            pass
        time.sleep(0.05)
    safe_send(message.chat.id, f"✅ **Annonce diffusée avec succès à {count} utilisateur(s).**")


@bot.message_handler(commands=['payment'])
def payment_info(message):
    send_payment_message(message.chat.id)


def send_payment_message(chat_id):
    text = (
        "💳 **ACTIVATION ACCÈS VIP B2K ALGO**\n"
        "───────────────────────────\n"
        "💰 **Tarif Abonnement :** **20 USD** ou **2 700 HTG**\n\n"
        "📌 **Moyens de paiement :**\n"
        "• **Crypto :** USDT (TRC20 / BEP20)\n"
        "• **Local Haïti :** MonCash / NatCash\n\n"
        "📲 **Pour activer votre compte :**\n"
        "📩 **Telegram Direct :** [@b2k_ruben](https://t.me/b2k_ruben)\n"
        "📱 **WhatsApp Direct :** +50943528461\n\n"
        f"🆔 Ton ID à communiquer : `{chat_id}`"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📲 Contacter @b2k_ruben", url="https://t.me/b2k_ruben"))
    markup.add(types.InlineKeyboardButton("Menu Principal", callback_data="back_home"))
    safe_send(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


def send_trial_over(chat_id):
    text = (
        "⛔ **VOTRE ESSAI GRATUIT EST TERMINÉ !**\n"
        "───────────────────────────\n"
        "Débloquez votre accès VIP illimité pour 20 $ USD / 2 700 HTG.\n\n"
        "📩 Telegram : [@b2k_ruben](https://t.me/b2k_ruben)\n"
        "📱 WhatsApp : +50943528461"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💳 Payer mon accès (20$)", callback_data="show_payment"))
    markup.add(types.InlineKeyboardButton("Menu Principal", callback_data="back_home"))
    safe_send(chat_id, text, reply_markup=markup, disable_web_page_preview=True)

# ====================================================
# 10. MENU PRINCIPAL
# ====================================================


@bot.message_handler(commands=['start', 'menu'])
def start_cmd(message):
    chat_id = message.chat.id
    awaiting_price.pop(chat_id, None)
    get_user(chat_id)
    show_main_menu(chat_id)


def show_main_menu(chat_id):
    trials, is_paid, admin = get_user(chat_id)
    is_testing = testing_as_user.get(chat_id, False)

    text = (
        "⚡ **B2K ALGO SCALPING PRO (XM EDITION)**\n"
        "───────────────────────────\n"
        "👇 Sélectionne ton actif pour démarrer :"
    )
    if is_testing or (not is_paid and not admin):
        text += f"\n\n🎁 **Essais gratuits restants :** `{trials}/3`"
    else:
        text += "\n\n⭐ **Statut :** `Abonné VIP Actif`"

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("₿ BTCUSD (Bitcoin) - Saisie XM", callback_data="asset_BTCUSD"),
        types.InlineKeyboardButton("🥇 XAUUSD (Or) - Saisie XM", callback_data="asset_XAUUSD"),
        types.InlineKeyboardButton("📚 Formation B2K", callback_data="show_book"),
        types.InlineKeyboardButton("💳 Abonnement VIP (20$)", callback_data="show_payment"),
    )
    safe_send(chat_id, text, reply_markup=markup)

# ====================================================
# 11. CALLBACKS
# ====================================================


@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    # --- Actions qui gardent le message du signal à l'écran ---
    if data == "close":
        clean_delete_message(chat_id, message_id)
        return

    if data.startswith("rean_") or data.startswith("reanb_"):
        if data.startswith("reanb_"):
            clean_delete_message(chat_id, message_id)
        try:
            sid = int(data.split("_")[1])
        except ValueError:
            return
        threading.Thread(target=run_reanalysis, args=(chat_id, sid), daemon=True).start()
        return

    if data.startswith("det_"):
        try:
            sig = get_signal(int(data.split("_")[1]))
        except ValueError:
            return
        if sig and (sig["chat_id"] == chat_id or is_admin(chat_id)):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✖ Fermer", callback_data="close"))
            safe_send(chat_id, sig["details"] or "Aucun détail disponible.", reply_markup=markup)
        return

    if data.startswith("res_"):
        try:
            _, sid_s, result = data.split("_")
            sid = int(sid_s)
        except ValueError:
            return
        sig = get_signal(sid)
        if result not in ("TP", "SL") or not sig or (sig["chat_id"] != chat_id and not is_admin(chat_id)):
            return
        set_result(sid, result)
        try:
            bot.edit_message_reply_markup(chat_id, message_id,
                                          reply_markup=signal_markup(sig["symbol"], sig["mode"], sig["tf"], sid, result))
        except Exception:
            pass
        if result == "TP":
            safe_send(chat_id, "✅ **Résultat enregistré : Take Profit touché.** Bien joué 🎯")
        else:
            safe_send(chat_id, "❌ **Résultat enregistré : Stop Loss touché.** Ça fait partie du jeu, respecte ton plan de risque.")
        return

    # --- Navigation : on remplace le message précédent ---
    clean_delete_message(chat_id, message_id)
    awaiting_price.pop(chat_id, None)

    if data == "back_home":
        show_main_menu(chat_id)
        return

    if data == "show_payment":
        send_payment_message(chat_id)
        return

    if data == "show_book":
        text = "📖 **ACADÉMIE B2K | FORMATION**\nChoisis un module :"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("📘 1. C'est quoi le Trading ?", callback_data="book_ch1"),
            types.InlineKeyboardButton("⏱️ 2. Les Timeframes", callback_data="book_ch2"),
            types.InlineKeyboardButton("🚀 3. Discipline de Scalpeur", callback_data="book_ch3"),
            types.InlineKeyboardButton("Menu Principal", callback_data="back_home"),
        )
        safe_send(chat_id, text, reply_markup=markup)
        return

    if data.startswith("book_"):
        ch_key = data.replace("book_", "")
        text = TRAINING_CHAPTERS.get(ch_key, "Contenu en cours d'édition.")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📖 Formation", callback_data="show_book"),
                   types.InlineKeyboardButton("Menu", callback_data="back_home"))
        safe_send(chat_id, text, reply_markup=markup)
        return

    if data.startswith("asset_"):
        symbol = data.split("_")[1]

        if symbol not in SYMBOLS:
            show_main_menu(chat_id)
            return

        text = f"📌 **ACTIF : {symbol}**\nSélectionne ton mode :"
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("⚡ Micro-Scalping Express (30s - 1 min)", callback_data=f"mode_{symbol}_express"),
            types.InlineKeyboardButton("🚀 Scalping Intra-Day (15 - 20 min)", callback_data=f"mode_{symbol}_intraday"),
            types.InlineKeyboardButton("Menu Principal", callback_data="back_home"),
        )
        safe_send(chat_id, text, reply_markup=markup)
        return

    if data.startswith("mode_"):
        _, symbol, mode = data.split("_")
        text = f"📌 **ACTIF : {symbol} | Mode : {mode.upper()}**\nSélectionne le Timeframe :"
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("⏱️ M1", callback_data=f"sig_{symbol}_{mode}_M1"),
            types.InlineKeyboardButton("⏱️ M5", callback_data=f"sig_{symbol}_{mode}_M5"),
            types.InlineKeyboardButton("⏱️ M15", callback_data=f"sig_{symbol}_{mode}_M15"),
            types.InlineKeyboardButton("⏱️ H1", callback_data=f"sig_{symbol}_{mode}_H1"),
            types.InlineKeyboardButton("Menu Principal", callback_data="back_home"),
        )
        safe_send(chat_id, text, reply_markup=markup)
        return

    if data.startswith("sig_"):
        _, symbol, mode, tf = data.split("_")
        if not has_access(chat_id):
            send_trial_over(chat_id)
            return
        awaiting_price[chat_id] = (symbol, mode, tf)
        text = (
            f"🎯 **SAISIE DU PRIX XM | {symbol} {tf}**\n"
            "───────────────────────────\n"
            "📱 Regarde ton application MT5 XM.\n"
            "✍️ **Entre le prix BUY (celui de droite)** (Ex: `80819.85`) :"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("🌐 Sans saisie (prix du marché)", callback_data=f"auto_{symbol}_{mode}_{tf}"),
            types.InlineKeyboardButton("Menu Principal", callback_data="back_home"),
        )
        safe_send(chat_id, text, reply_markup=markup)
        return

    if data.startswith("auto_"):
        _, symbol, mode, tf = data.split("_")
        threading.Thread(target=run_signal, args=(chat_id, symbol, mode, tf, None), daemon=True).start()
        return


# Garder ce handler en DERNIER
@bot.message_handler(func=lambda m: m.chat.id in awaiting_price, content_types=['text'])
def receive_price(message):
    chat_id = message.chat.id
    symbol, mode, tf = awaiting_price[chat_id]
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        price = float(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        safe_send(chat_id, "⚠️ **Format invalide !** Entre uniquement un nombre correct (Ex: `80819.85`). Réessaie :")
        return

    data = fetch_candles(symbol, tf, limit=100)
    if data and abs(price - data[2][-1]) / data[2][-1] > 0.02:
        safe_send(chat_id, f"⚠️ Ton prix `{price}` est à plus de 2 % du marché (`{data[2][-1]:.2f}`). "
                           "Vérifie ta saisie et renvoie-le.")
        return

    awaiting_price.pop(chat_id, None)
    threading.Thread(target=run_signal, args=(chat_id, symbol, mode, tf, price), daemon=True).start()


if __name__ == '__main__':
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass
    threading.Thread(target=tracker_loop, daemon=True).start()
    bot.infinity_polling(skip_pending=True)
