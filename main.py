import os
import time
import requests
import asyncio
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
# 0) ON SUPPRIME LE WEBHOOK EXISTANT
# ───────────────────────────────────────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ TELEGRAM_TOKEN non défini !")

# Ce GET supprime le webhook **et** vide les anciennes updates
resp = requests.get(
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    "/deleteWebhook?drop_pending_updates=true"
)
if resp.ok:
    print("✅ Webhook supprimé et pending updates vidés", flush=True)
else:
    print("⚠️ Échec suppression webhook:", resp.text, flush=True)


# ───────────────────────────────────────────────────────────────────────────────
# 1) ENV & WEB3 INIT
# ───────────────────────────────────────────────────────────────────────────────
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
PRIVATE_KEY       = os.getenv("PRIVATE_KEY")
RAW_WALLET        = os.getenv("WALLET_ADDRESS", "").strip()
INFURA_URL        = os.getenv("INFURA_URL")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

for name, val in [
    ("TELEGRAM_CHAT_ID",  TELEGRAM_CHAT_ID),
    ("PRIVATE_KEY",       PRIVATE_KEY),
    ("WALLET_ADDRESS",    RAW_WALLET),
    ("INFURA_URL",        INFURA_URL),
    ("ETHERSCAN_API_KEY", ETHERSCAN_API_KEY),
]:
    if not val:
        raise RuntimeError(f"❌ {name} non défini !")

try:
    WALLET_ADDRESS = Web3.to_checksum_address(RAW_WALLET)
except Exception as e:
    raise RuntimeError(f"❌ WALLET_ADDRESS invalide : {e}")

w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("❌ Échec connexion Infura")

print("✅ Variables chargées", flush=True)
print("✅ Web3 connecté"    , flush=True)


# ───────────────────────────────────────────────────────────────────────────────
# 2) UNISWAP ROUTER & ERC20
# ───────────────────────────────────────────────────────────────────────────────
UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address(
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
)
UNISWAP_ROUTER_ABI = [
    # swapExactETHForTokens
    {
        "inputs":[
            {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
            {"internalType":"address","name":"to","type":"address"},
            {"internalType":"uint256","name":"deadline","type":"uint256"},
        ],
        "name":"swapExactETHForTokens",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"payable","type":"function"
    },
    # swapExactTokensForETH
    {
        "inputs":[
            {"internalType":"uint256","name":"amountIn","type":"uint256"},
            {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
            {"internalType":"address","name":"to","type":"address"},
            {"internalType":"uint256","name":"deadline","type":"uint256"},
        ],
        "name":"swapExactTokensForETH",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"nonpayable","type":"function"
    },
    # getAmountsOut
    {
        "inputs":[
            {"internalType":"uint256","name":"amountIn","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
        ],
        "name":"getAmountsOut",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"view","type":"function"
    },
]
router = w3.eth.contract(
    address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI
)

ERC20_ABI = [
    {"constant":False,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":True, "inputs":[{"name":"_owner","type":"address"}],
     "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
]
WETH_ADDRESS = Web3.to_checksum_address(
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)


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
print(f"✅ Whales suivies : {len(WHALES)}", flush=True)

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
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=txlist"
        f"&address={whale}&startblock={start_block}&endblock=latest"
        f"&sort=asc&apikey={ETHERSCAN_API_KEY}"
    )
    res = send_http_request(url)
    if res.get("status")=="1" and res.get("message")=="OK":
        return res["result"]
    return []

def is_buy(inp: str) -> bool:
    return inp.startswith("0x7ff36ab5")

def is_sell(inp: str) -> bool:
    return inp.startswith("0x18cbafe5")

def extract_path_token(input_data: str) -> str | None:
    try:
        _, params = router.decode_function_input(input_data)
        return params["path"][1]
    except:
        return None

def buy_token(token_address: str, eth_amount: Decimal):
    bal = w3.from_wei(w3.eth.get_balance(WALLET_ADDRESS), "ether")
    if bal < eth_amount:
        return
    amt_wei = w3.to_wei(eth_amount, "ether")
    path = [WETH_ADDRESS, token_address]
    try:
        router.functions.getAmountsOut(amt_wei, path).call()
    except:
        return
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    tx = router.functions.swapExactETHForTokens(
        0, path, WALLET_ADDRESS, int(time.time())+300
    ).build_transaction({
        "from":    WALLET_ADDRESS,
        "value":   amt_wei,
        "gas":     300_000,
        "gasPrice":w3.to_wei("30","gwei"),
        "nonce":   nonce
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    w3.eth.send_raw_transaction(signed.raw_transaction)
    positions.append({"token":token_address,"token_amount_wei":amt_wei,"entry_eth":eth_amount})

def sell_all_token(token_address: str):
    token = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    bal   = token.functions.balanceOf(WALLET_ADDRESS).call()
    if bal==0:
        return
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    tx1 = token.functions.approve(UNISWAP_ROUTER_ADDRESS, bal).build_transaction({
        "from":    WALLET_ADDRESS,
        "gas":     100_000,
        "gasPrice":w3.to_wei("30","gwei"),
        "nonce":   nonce
    })
    signed1 = w3.eth.account.sign_transaction(tx1,PRIVATE_KEY)
    w3.eth.send_raw_transaction(signed1.raw_transaction)
    time.sleep(12)
    nonce2 = w3.eth.get_transaction_count(WALLET_ADDRESS)
    tx2 = router.functions.swapExactTokensForETH(
        bal,0,[token_address,WETH_ADDRESS],WALLET_ADDRESS,int(time.time())+300
    ).build_transaction({
        "from":    WALLET_ADDRESS,
        "gas":     300_000,
        "gasPrice":w3.to_wei("30","gwei"),
        "nonce":   nonce2
    })
    signed2 = w3.eth.account.sign_transaction(tx2,PRIVATE_KEY)
    w3.eth.send_raw_transaction(signed2.raw_transaction)


# ───────────────────────────────────────────────────────────────────────────────
# 5) COPYTRADE + TP/SL
# ───────────────────────────────────────────────────────────────────────────────
async def copytrade_task(ctx: ContextTypes.DEFAULT_TYPE):
    # TP/SL
    new = []
    for pos in positions:
        try:
            out    = router.functions.getAmountsOut(pos["token_amount_wei"], [pos["token"],WETH_ADDRESS]).call()
            cur_eth= Decimal(out[1]) / Decimal(10**18)
        except:
            new.append(pos)
            continue
        entry = pos["entry_eth"]
        ratio = (cur_eth/entry).quantize(Decimal("0.0001"))
        if ratio>=1+TP_THRESHOLD:
            await safe_send(ctx.application, f"✅ TP {pos['token']} → {cur_eth:.6f} ETH (+{(ratio-1)*100:.1f}%)")
            sell_all_token(pos["token"])
        elif ratio<=1-SL_THRESHOLD:
            await safe_send(ctx.application, f"⚠ SL {pos['token']} → {cur_eth:.6f} ETH (−{(1-ratio)*100:.1f}%)")
            sell_all_token(pos["token"])
        else:
            new.append(pos)
    positions[:] = new

    # Copy whales
    for whale in WHALES:
        txs = fetch_etherscan_txns(whale, last_processed_block[whale])
        for tx in txs:
            blk = int(tx["blockNumber"])
            last_processed_block[whale] = blk
            if tx["to"].lower()!=UNISWAP_ROUTER_ADDRESS.lower():
                continue
            token = extract_path_token(tx["input"])
            if not token:
                continue
            if is_buy(tx["input"]):
                await safe_send(ctx.application, f"👀 Whale {whale[:8]} BUY → {token}")
                buy_token(token, ETH_PER_TRADE)
            elif is_sell(tx["input"]):
                await safe_send(ctx.application, f"👀 Whale {whale[:8]} SELL → {token}")
                sell_all_token(token)


# ───────────────────────────────────────────────────────────────────────────────
# 6) DAILY SUMMARY 18h UTC
# ───────────────────────────────────────────────────────────────────────────────
async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    now   = datetime.utcnow()
    total = sum(pos["entry_eth"] for pos in positions)
    txt   = f"🧾 {now:%Y-%m-%d} → open:{len(positions)} invest:{total:.6f} ETH"
    await safe_send(ctx.application, txt)


# ───────────────────────────────────────────────────────────────────────────────
# 7) TELEGRAM HANDLERS
# ───────────────────────────────────────────────────────────────────────────────
async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Copytrade bot démarré ! /status")

async def status_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total = sum(pos["entry_eth"] for pos in positions)
    await update.message.reply_text(f"📊 Open:{len(positions)}  Invest:{total:.6f} ETH")


# ───────────────────────────────────────────────────────────────────────────────
# 8) LANCEMENT
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("▶️ Lancement du bot…", flush=True)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start_handler))
    app.add_handler(CommandHandler("status", status_handler))

    app.job_queue.run_repeating(copytrade_task, interval=30, first=5)
    app.job_queue.run_daily(daily_summary, time=dt_time(hour=18, minute=0))

    # **Important** : redéployez avec `python -u main.py` pour unbuffered
    app.run_polling(drop_pending_updates=True)
