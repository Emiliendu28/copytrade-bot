# ───────────────────────────────────────────────────────────────────────────────
# main.py
# ───────────────────────────────────────────────────────────────────────────────

import os
import time
import threading
import asyncio
import requests
from datetime import datetime, timedelta
from decimal import Decimal

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
    raise RuntimeError(f"Impossible de normaliser WALLET_ADDRESS (« {RAW_WALLET} ») : {e}")

# ───────────────────────────────────────────────────────────────────────────────
# 2) INITIALISATION WEB3 + UNISWAP ROUTER
# ───────────────────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à INFURA_URL !")

UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
UNISWAP_ROUTER_ABI = [
    {"inputs":[
        {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
        {"internalType":"address[]","name":"path","type":"address[]"},
        {"internalType":"address","name":"to","type":"address"},
        {"internalType":"uint256","name":"deadline","type":"uint256"},
     ],
     "name":"swapExactETHForTokens",
     "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
     "stateMutability":"payable","type":"function"},
    {"inputs":[
        {"internalType":"uint256","name":"amountIn","type":"uint256"},
        {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
        {"internalType":"address[]","name":"path","type":"address[]"},
        {"internalType":"address","name":"to","type":"address"},
        {"internalType":"uint256","name":"deadline","type":"uint256"},
     ],
     "name":"swapExactTokensForETH",
     "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[
        {"internalType":"uint256","name":"amountIn","type":"uint256"},
        {"internalType":"address[]","name":"path","type":"address[]"},
     ],
     "name":"getAmountsOut",
     "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
     "stateMutability":"view","type":"function"},
]
router_contract = w3.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI)

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value",   "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
]

WETH_ADDRESS = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

# ───────────────────────────────────────────────────────────────────────────────
# 3) COPY-TRADING & BUDGET
# ───────────────────────────────────────────────────────────────────────────────
RAW_WHALES = [
    "0xdf89a69a6a6d3df7d823076e0124a222538c5133",
    "0x858da48232ea6731f22573dc711c0cc415c334c5",
]
WHALES = [Web3.to_checksum_address(w.lower()) for w in RAW_WHALES]
last_processed_block = {w: 0 for w in WHALES}

# Budget mensuel ajusté à 10 €
MONTHLY_BUDGET_EUR = Decimal("10")
ETH_PRICE_USD      = Decimal("3500")
EUR_USD_RATE       = Decimal("1.10")

def eur_to_eth(eur: Decimal) -> Decimal:
    usd = eur * EUR_USD_RATE
    return (usd / ETH_PRICE_USD).quantize(Decimal("0.000001"))

monthly_budget_eth   = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE        = (monthly_budget_eth / MAX_TRADES_PER_MONTH).quantize(Decimal("0.000001"))

TP_THRESHOLD = Decimal("0.30")   # +30 %
SL_THRESHOLD = Decimal("0.15")   # −15 %

positions: list[dict] = []

# ───────────────────────────────────────────────────────────────────────────────
# 4) UTILITAIRES
# ───────────────────────────────────────────────────────────────────────────────
def send_http_request(url: str, timeout: int = 10) -> dict:
    try:
        return requests.get(url, timeout=timeout).json()
    except:
        return {}

def safe_send(message: str):
    """
    Envoi Telegram via HTTP direct, sans passer par python-telegram-bot.
    Évite tout conflit de getUpdates/webhook.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ safe_send(): {e}")

def est_swap_eth_for_tokens(inp: str) -> bool:
    return inp.startswith("0x7ff36ab5")

def est_swap_tokens_for_eth(inp: str) -> bool:
    return inp.startswith("0x18cbafe5")

def extract_token_from_buy(inp: str) -> str:
    base = 2 + (8 + 64 + 64) + 64 + 24
    return Web3.to_checksum_address("0x" + inp[base:base + 40])

def extract_token_from_sell(inp: str) -> str:
    base = 2 + (8 + 64 + 64 + 64) + 64 + 24
    return Web3.to_checksum_address("0x" + inp[base:base + 40])

def fetch_etherscan_txns(whale: str, start: int) -> list[dict]:
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=tokentx"
        f"&address={whale}&startblock={start}&endblock=latest"
        f"&sort=asc&apikey={ETHERSCAN_API_KEY}"
    )
    res = send_http_request(url)
    if res.get("status") == "1" and res.get("message") == "OK":
        return res.get("result", [])
    return []

# ───────────────────────────────────────────────────────────────────────────────
# Fonctions buy_token, sell_all_token, check_positions_and_maybe_sell,
# process_whale_txns…  (les mêmes que précédemment, inchangées)
# … (couve tes fonctions d’origine ici)
# ───────────────────────────────────────────────────────────────────────────────

def buy_token(token_address: str, eth_amount: Decimal) -> str | None:
    # … implémentation identique à celle fournie plus haut
    pass

def sell_all_token(token_address: str) -> str | None:
    # … implémentation identique à celle fournie plus haut
    pass

def check_positions_and_maybe_sell():
    # … idem
    pass

def process_whale_txns(whale: str):
    # … idem
    pass

# ───────────────────────────────────────────────────────────────────────────────
# 5) BOUCLE PRINCIPALE (copy-trade + TP/SL + résumé)
# ───────────────────────────────────────────────────────────────────────────────
def main_loop():
    # … implémentation identique à celle fournie plus haut
    pass

# ───────────────────────────────────────────────────────────────────────────────
# 6) HANDLERS TELEGRAM pour polling
# ───────────────────────────────────────────────────────────────────────────────
async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot copytrade whales est maintenant en ligne.\nTapez /status pour voir le statut."
    )

async def status_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total    = len(positions)
    invested = sum(pos["entry_eth"] for pos in positions)
    msg = (
        f"📊 Statut actuel :\n"
        f"• Positions ouvertes : {total}\n"
        f"• Investi total      : {invested:.6f} ETH\n"
    )
    if total > 0:
        msg += "Détails :\n"
        for p in positions:
            msg += f"→ {p['token']} | Entrée {p['entry_eth']:.6f} ETH\n"
    await update.message.reply_text(msg)

def run_telegram_polling():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start_handler))
    app.add_handler(CommandHandler("status", status_handler))

    # Supprime le webhook et purge les anciennes sessions getUpdates
    loop.run_until_complete(
        app.bot.delete_webhook(drop_pending_updates=True)
    )

    # Démarre le polling en dropant tout update précédent
    app.run_polling(stop_signals=None, drop_pending_updates=True)

# ───────────────────────────────────────────────────────────────────────────────
# 7) DÉMARRAGE
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1) Thread pour le copy-trading en arrière-plan
    threading.Thread(target=main_loop, daemon=True).start()
    # 2) Thread pour le polling Telegram
    threading.Thread(target=run_telegram_polling, daemon=True).start()
    # 3) Empêche le process principal de se terminer
    threading.Event().wait()
