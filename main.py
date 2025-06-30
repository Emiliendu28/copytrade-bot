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

# ───────────────────────────────────────────────────────────────────────────────
# 1) VARIABLES D’ENVIRONNEMENT
# ───────────────────────────────────────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
PRIVATE_KEY       = os.getenv("PRIVATE_KEY")
RAW_WALLET        = os.getenv("WALLET_ADDRESS", "").strip().lower()
INFURA_URL        = os.getenv("INFURA_URL")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

for name, val in [
    ("TELEGRAM_TOKEN",    TELEGRAM_TOKEN),
    ("TELEGRAM_CHAT_ID",  TELEGRAM_CHAT_ID),
    ("PRIVATE_KEY",       PRIVATE_KEY),
    ("WALLET_ADDRESS",    RAW_WALLET),
    ("INFURA_URL",        INFURA_URL),
    ("ETHERSCAN_API_KEY", ETHERSCAN_API_KEY),
]:
    if not val:
        raise RuntimeError(f"ERREUR : la variable d’environnement {name} n’est pas définie !")

try:
    WALLET_ADDRESS = Web3.to_checksum_address(RAW_WALLET)
except Exception as e:
    raise RuntimeError(f"Adresse wallet invalide : {e}")

# ───────────────────────────────────────────────────────────────────────────────
# 2) WEB3 + ROUTER UNISWAP V2
# ───────────────────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à INFURA_URL !")

UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address(
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
)
UNISWAP_ROUTER_ABI = [
    # ... (même ABI que précédemment pour swapExactETHForTokens, swapExactTokensForETH, getAmountsOut)
]
router = w3.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI)

ERC20_ABI = [
    # ... (approve + balanceOf)
]

WETH_ADDRESS = Web3.to_checksum_address(
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)

# ───────────────────────────────────────────────────────────────────────────────
# 3) WHALES, BUDGET & TP/SL
# ───────────────────────────────────────────────────────────────────────────────
RAW_WHALES = [
    "0x9E60A105c3D8DCb87fd10277cB2765439a7935f3",
    "0x3b7443Cc9a4E4C4cE435B873F4e1dDe36929ce71",
    "0x3004892CF2946356e8e4570A94748afDFF86681C",
    "0x000461A73d3985eef4923655782aA5d0De75C111",
]
WHALES = [Web3.to_checksum_address(w) for w in RAW_WHALES]
last_processed_block = {w: 0 for w in WHALES}

MONTHLY_BUDGET_EUR = Decimal("10")
ETH_PRICE_USD      = Decimal("3500")
EUR_USD_RATE       = Decimal("1.10")

def eur_to_eth(eur: Decimal) -> Decimal:
    return ((eur * EUR_USD_RATE) / ETH_PRICE_USD).quantize(Decimal("0.000001"))

monthly_budget_eth   = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE        = (monthly_budget_eth / MAX_TRADES_PER_MONTH).quantize(Decimal("0.000001"))
TP_THRESHOLD = Decimal("0.30")
SL_THRESHOLD = Decimal("0.15")

positions: list[dict] = []

# ───────────────────────────────────────────────────────────────────────────────
# 4) UTILITAIRES
# ───────────────────────────────────────────────────────────────────────────────
def send_http_request(url: str, timeout: int = 10) -> dict:
    try:
        return requests.get(url, timeout=timeout).json()
    except:
        return {}

async def safe_send(app, text: str):
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    """
    Récupère toutes les tx normales (txlist) pour l’adresse “whale” depuis “start_block”.
    """
    url = (
        "https://api.etherscan.io/api"
        f"?module=account"
        f"&action=txlist"                     # <-- on passe à txlist !
        f"&address={whale}"
        f"&startblock={start_block}&endblock=latest"
        f"&sort=asc"
        f"&apikey={ETHERSCAN_API_KEY}"
    )
    res = send_http_request(url)
    if res.get("status") == "1" and res.get("message") == "OK":
        return res.get("result", [])
    else:
        print(f"⚠️ Etherscan txlist error: {res.get('message')}")
        return []

def is_buy(hex_in: str) -> bool:
    return hex_in.startswith("0x7ff36ab5")

def is_sell(hex_in: str) -> bool:
    return hex_in.startswith("0x18cbafe5")

def extract_token_from_buy(hex_in: str) -> str:
    base = 2 + (8+64+64) + 64 + 24
    return Web3.to_checksum_address("0x" + hex_in[base:base+40])

def extract_token_from_sell(hex_in: str) -> str:
    base = 2 + (8+64+64+64) + 64 + 24
    return Web3.to_checksum_address("0x" + hex_in[base:base+40])

# (les fonctions buy_token() et sell_all_token() restent inchangées)

# ───────────────────────────────────────────────────────────────────────────────
# 5) JOB : copy-trade + TP/SL
# ───────────────────────────────────────────────────────────────────────────────
async def copytrade_task(ctx: ContextTypes.DEFAULT_TYPE):
    # 1) TP/SL sur positions existantes
    # 2) Pour chaque whale, fetch txlist, filtre router & input, buy_token / sell_all_token
    # (logique identique à celle que tu as déjà)

# ───────────────────────────────────────────────────────────────────────────────
# 6) JOB : résumé quotidien à 18h UTC
# ───────────────────────────────────────────────────────────────────────────────
async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    # idem

# ───────────────────────────────────────────────────────────────────────────────
# 7) Handlers /start & /status
# ───────────────────────────────────────────────────────────────────────────────
async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot copytrade whales en ligne. Tapez /status")

async def status_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    invested = sum(pos["entry_eth"] for pos in positions)
    msg = (
        f"📊 Statut actuel :\n"
        f"• Positions ouvertes : {len(positions)}\n"
        f"• Investi total      : {invested:.6f} ETH\n"
    )
    await update.message.reply_text(msg)

# ───────────────────────────────────────────────────────────────────────────────
# 8) DÉMARRAGE UNIQUE
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Enregistre handlers
    app.add_handler(CommandHandler("start",  start_handler))
    app.add_handler(CommandHandler("status", status_handler))

    # Programme les tâches
    app.job_queue.run_repeating(copytrade_task, interval=30, first=5)
    app.job_queue.run_daily(daily_summary, time=dt_time(hour=18, minute=0))

    # Démarre un seul polling, en dropant les anciennes mises à jour
    app.run_polling(drop_pending_updates=True)
