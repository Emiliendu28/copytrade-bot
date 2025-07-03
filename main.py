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
# 0) DELETE ANY EXISTING WEBHOOK BEFORE POLLING
# ───────────────────────────────────────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("❌ TELEGRAM_TOKEN missing")

# This will delete the webhook and drop any pending updates
resp = requests.get(
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    "/deleteWebhook?drop_pending_updates=true"
)
print("🔄 Telegram webhook cleared:", resp.ok, resp.text, flush=True)

# ───────────────────────────────────────────────────────────────────────────────
# 1) ENV & WEB3 SETUP
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
        raise RuntimeError(f"❌ {name} not defined!")

# checksum the wallet
try:
    WALLET_ADDRESS = Web3.to_checksum_address(RAW_WALLET)
except Exception as e:
    raise RuntimeError(f"❌ Invalid WALLET_ADDRESS: {e}")

w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("❌ Cannot connect to Infura")

print("✅ Web3 connected", flush=True)

# ───────────────────────────────────────────────────────────────────────────────
# 2) UNISWAP ROUTER + ERC20
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
router = w3.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI)

ERC20_ABI = [
    {"constant":False,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
     "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"_owner","type":"address"}],
     "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
]
WETH_ADDRESS = Web3.to_checksum_address(
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)

# ───────────────────────────────────────────────────────────────────────────────
# 3) WHALES & STATE
# ───────────────────────────────────────────────────────────────────────────────
RAW_WHALES = [
    "0x4d2468bef1e33e17f7b017430ded6f7c169f7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080",
    "0x3004892cf2946356e8e4570a94748afdff86681c",
    "0x6e4141d3a87f6d42ffb50ec64b6a009946e3d446",
]
WHALES = [Web3.to_checksum_address(w) for w in RAW_WHALES]
last_processed_block = {w: 0 for w in WHALES}

# spend a fixed amount per trade
ETH_PER_TRADE = Web3.to_wei(Decimal("0.02"), "ether")  # par exemple 0.02 ETH

# ───────────────────────────────────────────────────────────────────────────────
# 4) HELPERS
# ───────────────────────────────────────────────────────────────────────────────
def send_json_get(url: str, timeout: int = 10) -> dict:
    try:
        return requests.get(url, timeout=timeout).json()
    except:
        return {}

def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    url = (
        "https://api.etherscan.io/api"
        f"?module=account&action=txlist"
        f"&address={whale}&startblock={start_block}&endblock=latest"
        f"&sort=asc&apikey={ETHERSCAN_API_KEY}"
    )
    res = send_json_get(url)
    if res.get("status")=="1" and res.get("message")=="OK":
        return res["result"]
    return []

def extract_token(input_data: str) -> str | None:
    try:
        _, params = router.decode_function_input(input_data)
        # path = [WETH, token] ou [token, WETH]
        path: list[str] = params.get("path", [])
        # si c'est un BUY (ETH→token), token = path[1]
        # si SELL, token = path[0]
        return path[1] if input_data.startswith("0x7ff36ab5") else path[0]
    except:
        return None

async def safe_notify(app, txt: str):
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=txt)

def buy(token: str):
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    tx = router.functions.swapExactETHForTokens(
        0, [WETH_ADDRESS, token], WALLET_ADDRESS, int(time.time()) + 300
    ).build_transaction({
        "from": WALLET_ADDRESS,
        "value": ETH_PER_TRADE,
        "gas": 300_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce": nonce
    })
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    w3.eth.send_raw_transaction(signed.raw_transaction)

def sell(token: str):
    token_contract = w3.eth.contract(address=token, abi=ERC20_ABI)
    bal = token_contract.functions.balanceOf(WALLET_ADDRESS).call()
    if bal == 0:
        return
    # approve + swap
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    tx1 = token_contract.functions.approve(UNISWAP_ROUTER_ADDRESS, bal).build_transaction({
        "from": WALLET_ADDRESS, "gas":100_000, "gasPrice":w3.to_wei("30","gwei"), "nonce":nonce
    })
    s1 = w3.eth.account.sign_transaction(tx1, PRIVATE_KEY)
    w3.eth.send_raw_transaction(s1.raw_transaction)
    time.sleep(12)
    nonce2 = w3.eth.get_transaction_count(WALLET_ADDRESS)
    tx2 = router.functions.swapExactTokensForETH(
        bal, 0, [token, WETH_ADDRESS], WALLET_ADDRESS, int(time.time())+300
    ).build_transaction({
        "from": WALLET_ADDRESS, "gas":300_000, "gasPrice":w3.to_wei("30","gwei"), "nonce":nonce2
    })
    s2 = w3.eth.account.sign_transaction(tx2, PRIVATE_KEY)
    w3.eth.send_raw_transaction(s2.raw_transaction)

# ───────────────────────────────────────────────────────────────────────────────
# 5) COPY TRADE JOB
# ───────────────────────────────────────────────────────────────────────────────
async def copy_trade(ctx: ContextTypes.DEFAULT_TYPE):
    for whale in WHALES:
        txs = fetch_etherscan_txns(whale, last_processed_block[whale])
        for tx in txs:
            blk = int(tx["blockNumber"])
            last_processed_block[whale] = blk
            if tx["to"].lower() != UNISWAP_ROUTER_ADDRESS.lower():
                continue

            inp   = tx["input"]
            token = extract_token(inp)
            if not token:
                continue

            if inp.startswith("0x7ff36ab5"):
                # BUY
                await safe_notify(ctx.application, f"🟢 Whale {whale[:8]} BUY → {token}")
                buy(token)
            elif inp.startswith("0x18cbafe5"):
                # SELL
                await safe_notify(ctx.application, f"🔴 Whale {whale[:8]} SELL → {token}")
                sell(token)

# ───────────────────────────────────────────────────────────────────────────────
# 6) TELEGRAM COMMANDS & LAUNCH
# ───────────────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Copy-bot démarré! /status")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ En veille, je copie les whales...")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("status", status))

    # toutes les 30s on regarde les trades
    app.job_queue.run_repeating(copy_trade, interval=30, first=5)

    print("▶️ Démarrage polling...", flush=True)
    app.run_polling(drop_pending_updates=True)
