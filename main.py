import os
import time
import requests
from decimal import Decimal
from datetime import datetime, time as dt_time

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
# 2) INITIALISATION WEB3 + ROUTER UNISWAP V2
# ───────────────────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à INFURA_URL !")

UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
UNISWAP_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256","name": "amountOutMin","type": "uint256"},
            {"internalType": "address[]","name": "path","type": "address[]"},
            {"internalType": "address","name": "to","type": "address"},
            {"internalType": "uint256","name": "deadline","type": "uint256"},
        ],
        "name": "swapExactETHForTokens",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"payable","type":"function",
    },
    {
        "inputs": [
            {"internalType":"uint256","name":"amountIn","type":"uint256"},
            {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
            {"internalType":"address","name":"to","type":"address"},
            {"internalType":"uint256","name":"deadline","type":"uint256"},
        ],
        "name":"swapExactTokensForETH",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"nonpayable","type":"function",
    },
    {
        "inputs":[
            {"internalType":"uint256","name":"amountIn","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
        ],
        "name":"getAmountsOut",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"view","type":"function",
    },
]
router = w3.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI)

ERC20_ABI = [
    {"constant":False,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
]

WETH_ADDRESS = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

# ───────────────────────────────────────────────────────────────────────────────
# 3) COPY-TRADING & BUDGET
# ───────────────────────────────────────────────────────────────────────────────
RAW_WHALES = [
    "0x4d2468bef1e33e17f7b017430ded6f7c169f7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080",
    "0x3004892cf2946356e8e4570a94748afdff86681c",
    "0x6e4141d3a87f6d42ffb50ec64b6a009946e3d446",
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
TP_THRESHOLD         = Decimal("0.30")
SL_THRESHOLD         = Decimal("0.15")

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
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=txlist"
        f"&address={whale}&startblock={start_block}&endblock=latest"
        f"&sort=asc&apikey={ETHERSCAN_API_KEY}"
    )
    res = send_http_request(url)
    if res.get("status") == "1" and res.get("message") == "OK":
        return res["result"]
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

def buy_token(token_address: str, eth_amount: Decimal) -> None:
    balance = w3.from_wei(w3.eth.get_balance(WALLET_ADDRESS), "ether")
    if balance < eth_amount:
        return
    amt_wei = w3.to_wei(eth_amount, "ether")
    path = [WETH_ADDRESS, token_address]
    try:
        out = router.functions.getAmountsOut(amt_wei, path).call()
        token_est_wei = out[1]
    except:
        return
    deadline = int(time.time()) + 300
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    txn = router.functions.swapExactETHForTokens(0, path, WALLET_ADDRESS, deadline).build_transaction({
        "from": WALLET_ADDRESS,
        "value": amt_wei,
        "gas": 300_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce": nonce,
    })
    signed = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
    w3.eth.send_raw_transaction(signed.raw_transaction)

def sell_all_token(token_address: str) -> None:
    token = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    bal = token.functions.balanceOf(WALLET_ADDRESS).call()
    if bal == 0:
        return
    # approve
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    ap = token.functions.approve(UNISWAP_ROUTER_ADDRESS, bal).build_transaction({
        "from": WALLET_ADDRESS, "gas":100_000,
        "gasPrice":w3.to_wei("30","gwei"), "nonce":nonce,
    })
    sap = w3.eth.account.sign_transaction(ap, PRIVATE_KEY)
    w3.eth.send_raw_transaction(sap.raw_transaction)
    time.sleep(12)
    # swap
    path = [token_address, WETH_ADDRESS]
    deadline = int(time.time()) + 300
    nonce2 = w3.eth.get_transaction_count(WALLET_ADDRESS)
    stx = router.functions.swapExactTokensForETH(bal,0,path,WALLET_ADDRESS,deadline).build_transaction({
        "from":WALLET_ADDRESS,"gas":300_000,
        "gasPrice":w3.to_wei("30","gwei"),"nonce":nonce2,
    })
    sstx = w3.eth.account.sign_transaction(stx, PRIVATE_KEY)
    w3.eth.send_raw_transaction(sstx.raw_transaction)

# ───────────────────────────────────────────────────────────────────────────────
# 5) JOB : copytrade_task
# ───────────────────────────────────────────────────────────────────────────────
async def copytrade_task(ctx: ContextTypes.DEFAULT_TYPE):
    # TP/SL sur positions ouvertes
    nouvelles = []
    for pos in positions:
        try:
            out = router.functions.getAmountsOut(pos["token_amount_wei"], [pos["token"], WETH_ADDRESS]).call()
            cur_eth = Decimal(out[1]) / Decimal(10**18)
            ratio   = (cur_eth / pos["entry_eth"]).quantize(Decimal("0.0001"))
        except:
            nouvelles.append(pos)
            continue

        if ratio >= (Decimal("1.0") + TP_THRESHOLD):
            await safe_send(ctx.application, f"✅ TAKE-PROFIT → {pos['token']} | {cur_eth:.6f} ETH (+{(ratio-1)*100:.1f}%)")
            sell_all_token(pos["token"])
        elif ratio <= (Decimal("1.0") - SL_THRESHOLD):
            await safe_send(ctx.application, f"⚠️ STOP-LOSS → {pos['token']} | {cur_eth:.6f} ETH (−{(1-ratio)*100:.1f}%)")
            sell_all_token(pos["token"])
        else:
            nouvelles.append(pos)
    positions[:] = nouvelles

    # Copy-trade sur chaque whale
    for whale in WHALES:
        txs = fetch_etherscan_txns(whale, last_processed_block[whale])
        for tx in txs:
            blk = int(tx.get("blockNumber",0))
            inp = tx.get("input","")
            to_ = Web3.to_checksum_address(tx.get("to","0x0"))

            if to_.lower() != UNISWAP_ROUTER_ADDRESS.lower():
                continue

            if is_buy(inp):
                token = extract_token_from_buy(inp)
                await safe_send(ctx.application, f"👀 Whale {whale[:8]} → BUY détecté ({token})")
                buy_token(token, ETH_PER_TRADE)
            elif is_sell(inp):
                token = extract_token_from_sell(inp)
                await safe_send(ctx.application, f"👀 Whale {whale[:8]} → SELL détecté ({token})")
                sell_all_token(token)

            last_processed_block[whale] = blk

# ───────────────────────────────────────────────────────────────────────────────
# 6) JOB : résumé quotidien à 18h UTC
# ───────────────────────────────────────────────────────────────────────────────
async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    invested = sum(pos["entry_eth"] for pos in positions)
    txt = (
        f"🧾 Résumé {now:%Y-%m-%d}:\n"
        f"• Positions ouvertes : {len(positions)}\n"
        f"• Investi total      : {invested:.6f} ETH"
    )
    await safe_send(ctx.application, txt)

# ───────────────────────────────────────────────────────────────────────────────
# 7) HANDLERS TELEGRAM
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
# 8) LANCEMENT
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("status", status_handler))
    app.job_queue.run_repeating(copytrade_task, interval=30, first=5)
    app.job_queue.run_daily(daily_summary, time=dt_time(hour=18, minute=0))
    app.run_polling(drop_pending_updates=True)
