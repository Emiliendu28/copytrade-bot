import os
import time
import requests
from decimal import Decimal
from datetime import datetime, time as dt_time, timedelta

from web3 import Web3
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ─── 1) ENV ────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN    = os.getenv("TELEGRAM_TOKEN")
CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
KEY      = os.getenv("PRIVATE_KEY")
WALLET   = os.getenv("WALLET_ADDRESS", "").strip().lower()
INFURA   = os.getenv("INFURA_URL")
ETHERSCAN_KEY = os.getenv("ETHERSCAN_API_KEY")
for k,v in [("TOKEN",TOKEN),("CHAT_ID",CHAT_ID),("KEY",KEY),
            ("WALLET",WALLET),("INFURA",INFURA),("ETHERSCAN",ETHERSCAN_KEY)]:
    if not v: raise RuntimeError(f"Il manque la var env {k}")
try:
    WALLET = Web3.to_checksum_address(WALLET)
except Exception as e:
    raise RuntimeError(f"Adresse wallet invalide : {e}")

# ─── 2) WEB3 & UNISWAP ─────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à Infura.")

ROUTER = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
WETH   = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
ABI_ROUTER = [ … same as before … ]
router = w3.eth.contract(address=ROUTER, abi=ABI_ROUTER)
ERC20_ABI = [ … same as before … ]

# ─── 3) COPY-TRADING & BUDGET ─────────────────────────────────────────────────
RAW = [
    "0xdf8efb8a522561ea9bd8c55874dca4536ee5c618",  # Smart Dex Trader
    "0x6e90ae41af1dea6f0006aa7752d9db2cf5e6a49f",  # Smart Dex Trader
    "0xdf89a69a6a6d3df7d823076e0124a222538c5133",  # Private whale
    "0x858da48232ea6731f22573dc711c0cc415c334c5",  # Private whale
]
WHALES = [Web3.to_checksum_address(w) for w in RAW]
last_block = {w: 0 for w in WHALES}

MONTHLY_EUR = Decimal("10")
USD_PER_ETH = Decimal("3500")
EUR_USD     = Decimal("1.10")
def eur_to_eth(x: Decimal) -> Decimal:
    return ((x * EUR_USD) / USD_PER_ETH).quantize(Decimal("0.000001"))
monthly_eth  = eur_to_eth(MONTHLY_EUR)
MAX_TRADES   = 5
ETH_TRADE    = (monthly_eth / MAX_TRADES).quantize(Decimal("0.000001"))
TP, SL       = Decimal("0.30"), Decimal("0.15")

positions = []

# ─── 4) UTILITAIRES ────────────────────────────────────────────────────────────
def get_json(url: str, timeout=10):
    try:
        return requests.get(url, timeout=timeout).json()
    except:
        return {}

async def safe_send(app, text: str):
    """Envoi via le même bot, pas de conflit polling."""
    await app.bot.send_message(chat_id=CHAT_ID, text=text)

def fetch_txs(whale, start):
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=tokentx"
        f"&address={whale}&startblock={start}&endblock=latest"
        f"&sort=asc&apikey={ETHERSCAN_KEY}"
    )
    res = get_json(url)
    return res.get("result", []) if res.get("status")=="1" else []

def is_buy(inp):   return inp.startswith("0x7ff36ab5")
def is_sell(inp):  return inp.startswith("0x18cbafe5")
def extract_buy(inp):
    b = 2+(8+64+64)+64+24
    return Web3.to_checksum_address("0x"+inp[b:b+40])
def extract_sell(inp):
    b = 2+(8+64+64+64)+64+24
    return Web3.to_checksum_address("0x"+inp[b:b+40])

def buy_token(token, eth_amt):
    # identique à ton code… build+send, position.append, return txh
    …

def sell_all(token):
    # identique à ton code… approve+swap, return txh
    …

# ─── 5) LOGIQUE COPY-TRADE EN TÂCHE RÉPÉTÉE ────────────────────────────────────
async def copytrade_task(ctx: ContextTypes.DEFAULT_TYPE):
    # 1) TP/SL
    new_pos = []
    for pos in positions:
        # getAmountsOut, calcul ratio, sell si TP/SL, safe_send notifications
        …
    positions[:] = new_pos

    # 2) Parcours whales
    for w in WHALES:
        txs = fetch_txs(w, last_block[w])
        for tx in txs:
            blk = int(tx["blockNumber"])
            inp = tx["input"]
            to  = Web3.to_checksum_address(tx["to"])
            if to.lower() != ROUTER.lower(): continue

            if is_buy(inp):
                tk = extract_buy(inp)
                await safe_send(ctx.application, f"👀 Whale {w[:8]}… BUY {tk}")
                buy_token(tk, ETH_TRADE)
            elif is_sell(inp):
                tk = extract_sell(inp)
                await safe_send(ctx.application, f"👀 Whale {w[:8]}… SELL {tk}")
                sell_all(tk)

            last_block[w] = blk

# ─── 6) RÉSUMÉ QUOTIDIEN À 18 h UTC ──────────────────────────────────────────────
async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    invested = sum(p["entry_eth"] for p in positions)
    txt = (
        f"🧾 Résumé {now:%Y-%m-%d}:\n"
        f"• Positions ouvertes : {len(positions)}\n"
        f"• Investi total      : {invested:.6f} ETH"
    )
    await safe_send(ctx.application, txt)

# ─── 7) HANDLERS TELEGRAM ──────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot copytrade whales en ligne.")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    inv = sum(p["entry_eth"] for p in positions)
    msg = (
        f"📊 Statut actuel :\n"
        f"• Positions ouvertes : {len(positions)}\n"
        f"• Investi total      : {inv:.6f} ETH"
    )
    await update.message.reply_text(msg)

# ─── 8) DÉMARRAGE ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # supprime webhook avant polling
    app.bot.delete_webhook(drop_pending_updates=True)

    # register handlers
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("status", status))

    # job every 30s
    app.job_queue.run_repeating(copytrade_task, interval=30, first=5)

    # daily summary à 18 h UTC
    app.job_queue.run_daily(daily_summary, time=dt_time(hour=18, minute=0))

    # démarre le polling (unique)
    app.run_polling(drop_pending_updates=True)
