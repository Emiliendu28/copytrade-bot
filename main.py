# ───────────────────────────────────────────────────────────────────────────────
# main.py – Bot Copy-Trading Uniswap via Telegram (polling + budget 10 €)
# ───────────────────────────────────────────────────────────────────────────────

import os
import time
import threading
import asyncio
import requests
from decimal import Decimal
from datetime import datetime, timedelta

from web3 import Web3
from dotenv import load_dotenv

from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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
# 1b) SUPPRESSION DU WEBHOOK EXISTANT
# ───────────────────────────────────────────────────────────────────────────────
# (await via asyncio.run pour éviter warnings)
_temp_bot = Bot(token=TELEGRAM_TOKEN)
try:
    asyncio.run(_temp_bot.delete_webhook(drop_pending_updates=True))
    print("✅ Webhook Telegram supprimé au démarrage")
except Exception:
    pass

# ───────────────────────────────────────────────────────────────────────────────
# 2) INITIALISATION WEB3 + UNISWAP ROUTER
# ───────────────────────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à INFURA_URL !")

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
        "stateMutability":"payable","type":"function",
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
        "stateMutability":"nonpayable","type":"function",
    },
    # getAmountsOut
    {
        "inputs":[
            {"internalType":"uint256","name":"amountIn","type":"uint256"},
            {"internalType":"address[]","name":"path","type":"address[]"},
        ],
        "name":"getAmountsOut",
        "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
        "stateMutability":"view","type":"function",
    }
]
router_contract = w3.eth.contract(
    address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI
)

ERC20_ABI = [
    {
        "constant": False,
        "inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
        "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function",
    },
    {
        "constant": True,
        "inputs":[{"name":"_owner","type":"address"}],
        "name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function",
    },
]
WETH_ADDRESS = Web3.to_checksum_address(
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
)

# ───────────────────────────────────────────────────────────────────────────────
# 3) COPY-TRADING & BUDGET
# ───────────────────────────────────────────────────────────────────────────────
RAW_WHALES = [
    # Hot wallets d’échange (beaucoup de volume)
    "0x28C6c06298d514Db089934071355E5743bf21d60",  # Binance hot wallet
    "0xf977814e90da44bfa03b6295a0616a897441acec",  # Huobi hot wallet

    # Whales privées plus opportunistes
    "0xdf89a69a6a6d3df7d823076e0124a222538c5133",
    "0x858da48232ea6731f22573dc711c0cc415c334c5",
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

def safe_send(text: str):
    """Envoi par HTTP direct, pas de second getUpdates."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"⚠️ safe_send(): {e}")

def est_swap_eth_for_tokens(inp: str) -> bool:
    return inp.startswith("0x7ff36ab5")

def est_swap_tokens_for_eth(inp: str) -> bool:
    return inp.startswith("0x18cbafe5")

def extract_token_from_buy(inp: str) -> str:
    base = 2 + (8 + 64 + 64) + 64 + 24
    return Web3.to_checksum_address("0x" + inp[base:base+40])

def extract_token_from_sell(inp: str) -> str:
    base = 2 + (8 + 64 + 64 + 64) + 64 + 24
    return Web3.to_checksum_address("0x" + inp[base:base+40])

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

def buy_token(token_address: str, eth_amount: Decimal) -> str|None:
    balance = w3.from_wei(w3.eth.get_balance(WALLET_ADDRESS), "ether")
    if balance < eth_amount:
        safe_send(f"🚨 SOLDE insuffisant: {balance:.6f} ETH, besoin de {eth_amount:.6f} ETH")
        return None

    amt_wei = w3.to_wei(eth_amount, "ether")
    path = [WETH_ADDRESS, token_address]
    try:
        out = router_contract.functions.getAmountsOut(amt_wei, path).call()
    except Exception as e:
        safe_send(f"Erreur getAmountsOut (buy): {e}")
        return None

    token_est_wei = out[1]
    token_est = Decimal(token_est_wei) / Decimal(10**18)
    deadline = int(time.time()) + 300
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    txn = router_contract.functions.swapExactETHForTokens(
        0, path, WALLET_ADDRESS, deadline
    ).build_transaction({
        "from": WALLET_ADDRESS,
        "value": amt_wei,
        "gas": 300_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce": nonce,
    })
    signed = w3.eth.account.sign_transaction(txn, PRIVATE_KEY)
    try:
        txh = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        safe_send(f"Erreur envoi buy tx: {e}")
        return None

    positions.append({
        "token": token_address,
        "token_amount_wei": token_est_wei,
        "entry_eth": eth_amount,
        "entry_ratio": (eth_amount / token_est).quantize(Decimal("0.000000000001")),
    })
    safe_send(f"[BUY] {eth_amount:.6f} ETH → ~{token_est:.6f} tokens ({token_address})\nTx: {txh}")
    return txh

def sell_all_token(token_address: str) -> str|None:
    token = w3.eth.contract(address=token_address, abi=ERC20_ABI)
    bal = token.functions.balanceOf(WALLET_ADDRESS).call()
    if bal == 0:
        safe_send(f"⚠️ Pas de token à vendre pour {token_address}")
        return None

    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    ap = token.functions.approve(UNISWAP_ROUTER_ADDRESS, bal).build_transaction({
        "from": WALLET_ADDRESS,
        "gas": 100_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce": nonce,
    })
    sap = w3.eth.account.sign_transaction(ap, PRIVATE_KEY)
    try:
        tx_ap = w3.eth.send_raw_transaction(sap.raw_transaction).hex()
        safe_send(f"[APPROVE] {token_address} → Router: {tx_ap}")
        time.sleep(12)
    except Exception as e:
        safe_send(f"Erreur approve: {e}")
        return None

    path = [token_address, WETH_ADDRESS]
    deadline = int(time.time()) + 300
    nonce2 = w3.eth.get_transaction_count(WALLET_ADDRESS)
    stx = router_contract.functions.swapExactTokensForETH(
        bal, 0, path, WALLET_ADDRESS, deadline
    ).build_transaction({
        "from": WALLET_ADDRESS,
        "gas": 300_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce": nonce2,
    })
    sstx = w3.eth.account.sign_transaction(stx, PRIVATE_KEY)
    try:
        tx_sell = w3.eth.send_raw_transaction(sstx.raw_transaction).hex()
    except Exception as e:
        safe_send(f"Erreur sell: {e}")
        return None

    safe_send(f"[SELL] {bal/10**18:.6f} tokens → Tx: {tx_sell}")
    return tx_sell

def check_positions_and_maybe_sell():
    global positions
    new = []
    for pos in positions:
        addr, amt, entry = pos["token"], pos["token_amount_wei"], pos["entry_eth"]
        try:
            out = router_contract.functions.getAmountsOut(amt, [addr, WETH_ADDRESS]).call()
        except:
            new.append(pos)
            continue

        cur_eth = Decimal(out[1]) / Decimal(10**18)
        rate    = (cur_eth / entry).quantize(Decimal("0.0001"))
        if rate >= (Decimal("1.0")+TP_THRESHOLD):
            safe_send(f"✅ TAKE-PROFIT → {addr} | {cur_eth:.6f} ETH (+{(rate-1)*100:.1f} %)")
            sell_all_token(addr)
        elif rate <= (Decimal("1.0")-SL_THRESHOLD):
            safe_send(f"⚠️ STOP-LOSS → {addr} | {cur_eth:.6f} ETH (−{(1-rate)*100:.1f} %)")
            sell_all_token(addr)
        else:
            new.append(pos)
    positions = new

def process_whale_txns(whale: str):
    global last_processed_block
    start = last_processed_block[whale]
    txns = fetch_etherscan_txns(whale, start)
    for tx in txns:
        blk = int(tx.get("blockNumber", 0))
        inp = tx.get("input", "")
        to_ = Web3.to_checksum_address(tx.get("to","0x0"))
        if to_.lower() != UNISWAP_ROUTER_ADDRESS.lower():
            continue

        if est_swap_eth_for_tokens(inp):
            token = extract_token_from_buy(inp)
            safe_send(f"👀 Whale {whale[:8]}… → BUY détecté ({token})")
            buy_token(token, ETH_PER_TRADE)
        elif est_swap_tokens_for_eth(inp):
            token = extract_token_from_sell(inp)
            safe_send(f"👀 Whale {whale[:8]}… → SELL détecté ({token})")
            sell_all_token(token)

        last_processed_block[whale] = blk

# ───────────────────────────────────────────────────────────────────────────────
# 5) BOUCLE PRINCIPALE COPY-TRADE + TP/SL + RÉSUMÉ
# ───────────────────────────────────────────────────────────────────────────────
def main_loop():
    trades = 0
    month = datetime.utcnow().month
    next_summary = datetime.utcnow().replace(hour=18,minute=0,second=0,microsecond=0)
    if datetime.utcnow() > next_summary:
        next_summary += timedelta(days=1)

    safe_send("🚀 Bot copytrade démarre en arrière-plan.")
    main_loop.last_heartbeat = time.time()

    while True:
        try:
            now = datetime.utcnow()
            # Heartbeat horaire
            if time.time() - main_loop.last_heartbeat > 3600:
                safe_send(f"✅ Bot actif à {now:%Y-%m-%d %H:%M:%S} UTC")
                main_loop.last_heartbeat = time.time()

            # Reset mensuel
            if now.month != month:
                trades = 0
                month = now.month

            # TP/SL
            check_positions_and_maybe_sell()

            # Copy-trade
            for w in WHALES:
                process_whale_txns(w)

            # Résumé quotidien
            if now >= next_summary:
                nb   = len(positions)
                rem  = MAX_TRADES_PER_MONTH - trades
                inv  = trades * ETH_PER_TRADE
                safe_send((
                    f"🧾 Résumé {now:%Y-%m-%d}:\n"
                    f"• Positions ouvertes : {nb}\n"
                    f"• Trades restants    : {rem}/{MAX_TRADES_PER_MONTH}\n"
                    f"• Investi total      : {inv:.6f} ETH"
                ))
                next_summary += timedelta(days=1)

            time.sleep(30)

        except Exception as e:
            print(f"❌ main_loop → {e}")
            safe_send(f"❌ Erreur main_loop → {e}")
            time.sleep(60)

# ───────────────────────────────────────────────────────────────────────────────
# 6) HANDLERS TELEGRAM + POLLING
# ───────────────────────────────────────────────────────────────────────────────
async def start_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Bot copytrade whales en ligne.\nTapez /status")

async def status_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total    = len(positions)
    invested = sum(p["entry_eth"] for p in positions)
    msg = (
        f"📊 Statut :\n"
        f"• Positions ouvertes : {total}\n"
        f"• Investi total      : {invested:.6f} ETH\n"
    )
    if total > 0:
        for p in positions:
            msg += f"→ {p['token']} | Entrée {p['entry_eth']:.6f} ETH\n"
    await update.message.reply_text(msg)

def run_telegram_polling():
    # 1) Créer un nouveau loop et l’installer
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 2) Construire l’application
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  start_handler))
    app.add_handler(CommandHandler("status", status_handler))

    # 3) Supprimer webhook dans ce loop
    loop.run_until_complete(
        app.bot.delete_webhook(drop_pending_updates=True)
    )

    # 4) Démarrer le polling (bloquant ici dans le main thread)
    app.run_polling(drop_pending_updates=True)

# ───────────────────────────────────────────────────────────────────────────────
# 7) DÉMARRAGE
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Thread “daemon” pour la boucle copy-trading
    threading.Thread(target=main_loop, daemon=True).start()
    # Polling Telegram dans le main thread (besoin de signaux)
    run_telegram_polling()
