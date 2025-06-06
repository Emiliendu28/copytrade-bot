# main.py

import os
import time
import threading
import asyncio
import requests
from datetime import datetime, timedelta
from decimal import Decimal

from web3 import Web3
from dotenv import load_dotenv

from telegram import Bot, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ───────────────────────────────────────────────────────────────────────────────
# 1) CHARGEMENT DES VARIABLES D’ENVIRONNEMENT
# ───────────────────────────────────────────────────────────────────────────────

load_dotenv()

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
PRIVATE_KEY       = os.getenv("PRIVATE_KEY")
RAW_WALLET        = os.getenv("WALLET_ADDRESS", "").strip().lower()
INFURA_URL        = os.getenv("INFURA_URL")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

# Vérification basique
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

# Normalisation (checksum) de l’adresse wallet
try:
    WALLET_ADDRESS = Web3.to_checksum_address(RAW_WALLET)
except Exception as e:
    raise RuntimeError(f"Impossible de normaliser WALLET_ADDRESS (« {RAW_WALLET} ») : {e}")

# ───────────────────────────────────────────────────────────────────────────────
# 2) INITIALISATION DE WEB3 + UNISWAP ROUTER
# ───────────────────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à l’API Infura via INFURA_URL !")

# Adresse + ABI minimale du Router Uniswap V2
UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
UNISWAP_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256",   "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path",         "type": "address[]"},
            {"internalType": "address",   "name": "to",           "type": "address"},
            {"internalType": "uint256",   "name": "deadline",     "type": "uint256"},
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256",   "name": "amountIn",     "type": "uint256"},
            {"internalType": "uint256",   "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path",         "type": "address[]"},
            {"internalType": "address",   "name": "to",           "type": "address"},
            {"internalType": "uint256",   "name": "deadline",     "type": "uint256"},
        ],
        "name": "swapExactTokensForETH",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256",   "name": "amountIn",     "type": "uint256"},
            {"internalType": "address[]", "name": "path",         "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    }
]

router_contract = w3.eth.contract(
    address=UNISWAP_ROUTER_ADDRESS,
    abi=UNISWAP_ROUTER_ABI,
)

# ABI minimal ERC20 (approve + balanceOf)
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
# 3) PARAMÈTRES DE COPYTRADING (Whales, Budget, TP/SL)
# ───────────────────────────────────────────────────────────────────────────────

# Liste des whales à copier (checksum)
RAW_WHALES = [
    "0x4d2468bef1e33e17f7b017430ded6f7c169f7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080"
]
WHALES = [Web3.to_checksum_address(w.lower()) for w in RAW_WHALES]

# On mémorise, pour chaque whale, le dernier bloc vérifié (initialement 0)
last_processed_block = {whale: 0 for whale in WHALES}

# Budget mensuel en EUR → on le convertit en ETH
MONTHLY_BUDGET_EUR = Decimal("100")
ETH_PRICE_USD      = Decimal("3500")   # à ajuster si nécessaire
EUR_USD_RATE       = Decimal("1.10")   # taux approximatif

def eur_to_eth(eur_amount: Decimal) -> Decimal:
    usd_amount = eur_amount * EUR_USD_RATE
    return (usd_amount / ETH_PRICE_USD).quantize(Decimal("0.000001"))

monthly_budget_eth   = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE        = (monthly_budget_eth / MAX_TRADES_PER_MONTH).quantize(Decimal("0.000001"))

TP_THRESHOLD = Decimal("0.30")   # +30 %
SL_THRESHOLD = Decimal("0.15")   # −15 %

# Positions ouvertes : liste de dicts
# Chaque dict contient :
#   {
#     "token": str,
#     "token_amount_wei": int,
#     "entry_eth": Decimal,
#     "entry_ratio": Decimal
#   }
positions: list[dict] = []

# ───────────────────────────────────────────────────────────────────────────────
# 4) FONCTIONS UTILITAIRES
# ───────────────────────────────────────────────────────────────────────────────

def send_http_request(url: str, timeout: int = 10) -> dict:
    """
    Envoie une requête GET et renvoie le JSON ou {} en cas d’erreur.
    """
    try:
        res = requests.get(url, timeout=timeout)
        return res.json()
    except Exception as e:
        print(f"Erreur HTTP GET pour {url} → {e}")
        return {}

# Instanciation du Bot Telegram (pour les envois asynchrones)
BOT = Bot(token=TELEGRAM_TOKEN)

def safe_send(message: str):
    """
    Envoie un message Telegram en créant un event loop isolé à chaque fois.
    Ainsi, on évite tout conflit avec le polling PTB v20.
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        loop.run_until_complete(
            BOT.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        )
        loop.close()

    except Exception as e:
        print(f"⚠️ safe_send(): {e}")

def est_uniswap_swap_exact_eth_for_tokens(input_hex: str) -> bool:
    return input_hex.startswith("0x7ff36ab5")

def est_uniswap_swap_exact_tokens_for_eth(input_hex: str) -> bool:
    return input_hex.startswith("0x18cbafe5")

def extract_token_from_swap_eth_for_tokens(input_hex: str) -> str:
    """
    Dans swapExactETHForTokens, on récupère l’adresse du token (deuxième élément du chemin).
    """
    path_offset = 8 + 64 + 64
    token_start = 2 + path_offset + 64 + 24
    token_hex = input_hex[token_start : token_start + 40]
    return Web3.to_checksum_address("0x" + token_hex)

def extract_token_from_swap_tokens_for_eth(input_hex: str) -> str:
    """
    Dans swapExactTokensForETH, on récupère l’adresse du token mis en vente (premier élément du chemin).
    """
    path_offset = 8 + 64 + 64 + 64
    token_start = 2 + path_offset + 64 + 24
    token_hex = input_hex[token_start : token_start + 40]
    return Web3.to_checksum_address("0x" + token_hex)

def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    """
    Récupère toutes les tx ERC-20 pour l’adresse “whale” depuis “start_block” (inclus).
    """
    url = (
        "https://api.etherscan.io/api"
        f"?module=account"
        f"&action=tokentx"
        f"&address={whale}"
        f"&startblock={start_block}"
        f"&endblock=latest"
        f"&sort=asc"
        f"&apikey={ETHERSCAN_API_KEY}"
    )
    res = send_http_request(url)
    if res.get("status") == "1" and res.get("message") == "OK":
        return res.get("result", [])
    else:
        print(f"⚠️ Etherscan API a renvoyé status={res.get('status')} / {res.get('message')}")
        return []

def buy_token(token_address: str, eth_amount: Decimal) -> str | None:
    """
    Mirror BUY → swapExactETHForTokens de “eth_amount” ETH pour “token_address”.
    On stocke ensuite la position dans “positions”.
    """
    # 1) Vérifier solde ETH
    balance_wei = w3.eth.get_balance(WALLET_ADDRESS)
    balance_eth = w3.from_wei(balance_wei, "ether")
    if balance_eth < eth_amount:
        safe_send(
            f"🚨 SOLDE INSUFFISANT → Vous avez {balance_eth:.6f} ETH, "
            f"il faut {eth_amount:.6f} ETH pour ce trade."
        )
        return None

    # 2) Estimer quantité de tokens (getAmountsOut)
    path_buy = [WETH_ADDRESS, Web3.to_checksum_address(token_address)]
    amt_in_wei = w3.to_wei(eth_amount, "ether")
    try:
        amounts_out = router_contract.functions.getAmountsOut(amt_in_wei, path_buy).call()
    except Exception as e:
        safe_send(f"Erreur getAmountsOut (buy) → {e}")
        return None

    token_amt_est_wei = amounts_out[1]
    token_amt_est = Decimal(token_amt_est_wei) / Decimal(10**18)
    entry_eth   = eth_amount
    entry_ratio = (entry_eth / token_amt_est).quantize(Decimal("0.000000000001"))

    # 3) Construire la transaction swapExactETHForTokens
    deadline = int(time.time()) + 300
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    try:
        txn = router_contract.functions.swapExactETHForTokens(
            0,            # amountOutMin = 0 (on assume la slippage)
            path_buy,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            "from":     WALLET_ADDRESS,
            "value":    amt_in_wei,
            "gas":      300_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "nonce":    nonce,
        })
    except Exception as e:
        safe_send(f"Erreur build_transaction (buy) → {e}")
        return None

    # 4) Signer & envoyer
    try:
        signed = w3.eth.account.sign_transaction(txn, private_key=PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    except Exception as e:
        safe_send(f"Erreur send_raw_transaction (buy) → {e}")
        return None

    # 5) Mémoriser la position
    positions.append({
        "token":            token_address,
        "token_amount_wei": token_amt_est_wei,
        "entry_eth":        entry_eth,
        "entry_ratio":      entry_ratio,
    })

    safe_send(
        f"[BUY] Mirror achat whale → {entry_eth:.6f} ETH → "
        f"{token_amt_est:.6f} tokens ({token_address})\n"
        f"TxHash: {tx_hash}"
    )
    return tx_hash

def sell_all_token(token_address: str) -> str | None:
    """
    Mirror SELL → vend toute la position “token_address” pour ETH.
    """
    token_address = Web3.to_checksum_address(token_address)
    token_contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)

    # 1) Récupérer le solde du token
    try:
        balance_token = token_contract.functions.balanceOf(WALLET_ADDRESS).call()
    except Exception as e:
        safe_send(f"Erreur balanceOf (sell) pour {token_address} → {e}")
        return None

    if balance_token == 0:
        safe_send(f"⚠️ Aucune balance à vendre pour {token_address}.")
        return None

    # 2a) Approve du token vers le Router
    try:
        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        approve_txn = token_contract.functions.approve(
            UNISWAP_ROUTER_ADDRESS, balance_token
        ).build_transaction({
            "from":     WALLET_ADDRESS,
            "gas":      100_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "nonce":    nonce,
        })
        signed_approve = w3.eth.account.sign_transaction(approve_txn, private_key=PRIVATE_KEY)
        tx_approve = w3.eth.send_raw_transaction(signed_approve.raw_transaction).hex()
        safe_send(f"[APPROVE] Autorisation {token_address} → Router. TxHash: {tx_approve}")
        # On attend la confirmation de cet approve (quelques secondes)
        time.sleep(12)
    except Exception as e:
        safe_send(f"Erreur Approve (sell) → {e}")
        return None

    # 2b) swapExactTokensForETH
    path_sell = [token_address, WETH_ADDRESS]
    deadline = int(time.time()) + 300
    try:
        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        swap_txn = router_contract.functions.swapExactTokensForETH(
            balance_token,
            0,
            path_sell,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            "from":     WALLET_ADDRESS,
            "gas":      300_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "nonce":    nonce,
        })
    except Exception as e:
        safe_send(f"Erreur build_transaction (sell) → {e}")
        return None

    try:
        signed_swap = w3.eth.account.sign_transaction(swap_txn, private_key=PRIVATE_KEY)
        tx_sell = w3.eth.send_raw_transaction(signed_swap.raw_transaction).hex()
    except Exception as e:
        safe_send(f"Erreur send_raw_transaction (sell) → {e}")
        return None

    safe_send(
        f"[SELL] Mirror vente whale → vend {balance_token/10**18:.6f} tokens ({token_address})\n"
        f"TxHash: {tx_sell}"
    )
    return tx_sell

def check_positions_and_maybe_sell():
    """
    Pour chaque position dans `positions`, on calcule la valeur ETH actuelle.
    Si on atteint TP (≥ +30 %) ou SL (≤ −15 %), on vend tout.
    """
    global positions
    nouvelles_positions: list[dict] = []

    for pos in positions:
        token_address    = pos["token"]
        token_amount_wei = pos["token_amount_wei"]
        entry_eth        = pos["entry_eth"]
        entry_ratio      = pos["entry_ratio"]

        path_to_eth = [token_address, WETH_ADDRESS]
        try:
            amounts_out = router_contract.functions.getAmountsOut(
                token_amount_wei, path_to_eth
            ).call()
        except Exception as e:
            print(f"⚠️ getAmountsOut (check) pour {token_address} → {e}")
            nouvelles_positions.append(pos)
            continue

        current_eth_val = Decimal(amounts_out[1]) / Decimal(10**18)
        ratio = (current_eth_val / entry_eth).quantize(Decimal("0.0001"))

        # TAKE-PROFIT ?
        if ratio >= (Decimal("1.0") + TP_THRESHOLD):
            safe_send(
                f"✅ TAKE-PROFIT → {token_address} | valeur actuelle "
                f"{current_eth_val:.6f} ETH (+{(ratio - 1)*100:.1f} %)."
            )
            sell_all_token(token_address)

        # STOP-LOSS ?
        elif ratio <= (Decimal("1.0") - SL_THRESHOLD):
            safe_send(
                f"⚠️ STOP-LOSS → {token_address} | valeur actuelle "
                f"{current_eth_val:.6f} ETH (−{(1 - ratio)*100:.1f} %)."
            )
            sell_all_token(token_address)

        else:
            # on conserve la position
            nouvelles_positions.append(pos)

    positions = nouvelles_positions

def process_whale_txns(whale: str):
    """
    Récupère pour cette whale ses nouvelles txs ERC-20 depuis last_processed_block[whale].
    Si swapExactETHForTokens → on appelle buy_token(); si swapExactTokensForETH → sell_all_token().
    """
    global last_processed_block

    start_block = last_processed_block[whale] or 0
    txns = fetch_etherscan_txns(whale, start_block)

    for tx in txns:
        block_number = int(tx.get("blockNumber", 0))
        input_hex    = tx.get("input", "")
        to_addr      = Web3.to_checksum_address(tx.get("to", "0x0"))

        # On garde uniquement les appels au Router Uniswap
        if to_addr.lower() != UNISWAP_ROUTER_ADDRESS.lower():
            continue

        # BUY détecté ?
        if est_uniswap_swap_exact_eth_for_tokens(input_hex):
            token_addr = extract_token_from_swap_eth_for_tokens(input_hex)
            safe_send(f"👀 Whale {whale} → BUY détecté pour token {token_addr}.")
            buy_token(token_addr, ETH_PER_TRADE)

        # SELL détecté ?
        elif est_uniswap_swap_exact_tokens_for_eth(input_hex):
            token_addr = extract_token_from_swap_tokens_for_eth(input_hex)
            safe_send(f"👀 Whale {whale} → SELL détecté pour token {token_addr}.")
            sell_all_token(token_addr)

        # Mise à jour du dernier bloc traité
        last_processed_block[whale] = block_number

# ───────────────────────────────────────────────────────────────────────────────
# 5) BOUCLE PRINCIPALE EN THREAD : copy-trading, TP/SL, résumé & heartbeat
# ───────────────────────────────────────────────────────────────────────────────

def main_loop():
    trades_this_month = 0
    last_month_checked = datetime.utcnow().month

    # Calcul du prochain résumé (daily at 18 h UTC)
    next_summary_time = datetime.utcnow().replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    if datetime.utcnow() > next_summary_time:
        next_summary_time += timedelta(days=1)

    safe_send("🚀 Bot copytrade whales (Mirror + TP/SL) démarre et tourne en arrière-plan.")

    while True:
        try:
            now = datetime.utcnow()

            # 1) Heartbeat toutes les heures
            if time.time() - main_loop.last_heartbeat > 3600:
                safe_send(f"✅ Bot encore actif à {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                main_loop.last_heartbeat = time.time()

            # 2) Si nouveau mois, réinitialisation compteur trades
            if now.month != last_month_checked:
                trades_this_month = 0
                last_month_checked = now.month

            # 3) Vérifier TP/SL sur positions ouvertes
            check_positions_and_maybe_sell()

            # 4) Pour chaque whale, traiter ses txs
            for whale_addr in WHALES:
                process_whale_txns(whale_addr)

            # 5) Résumé quotidien à 18 h UTC
            if now >= next_summary_time:
                nb_positions    = len(positions)
                trades_restants = MAX_TRADES_PER_MONTH - trades_this_month
                eth_investi     = trades_this_month * ETH_PER_TRADE

                summary = (
                    f"🧾 Résumé quotidien ({now.strftime('%Y-%m-%d')}):\n"
                    f"🔹 Positions ouvertes : {nb_positions}\n"
                    f"🔹 Trades restants    : {trades_restants}/{MAX_TRADES_PER_MONTH}\n"
                    f"🔹 Total investi      : {eth_investi:.6f} ETH"
                )
                safe_send(summary)
                next_summary_time += timedelta(days=1)

            time.sleep(30)

        except Exception as e:
            print(f"❌ ERREUR dans main_loop() → {e}")
            safe_send(f"❌ Erreur bot (main_loop) → {e}")
            time.sleep(60)

# Initialisation du timestamp de heartbeat
main_loop.last_heartbeat = 0.0

# ───────────────────────────────────────────────────────────────────────────────
# 6) HANDLERS TELEGRAM (Application PTB v20)
# ───────────────────────────────────────────────────────────────────────────────

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start → le bot indique qu’il est en ligne.
    """
    await update.message.reply_text(
        "🤖 Bot copytrade whales (Mirror + TP/SL) est maintenant en ligne.\n"
        "Tapez /status pour connaître le statut."
    )

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status → renvoie nb de positions ouvertes + total ETH investi.
    """
    total_positions = len(positions)
    invested_eth    = sum(pos["entry_eth"] for pos in positions)
    msg = f"📊 Statut actuel du bot :\n\n"
    msg += f"🔁 Positions ouvertes : {total_positions}\n"
    msg += f"💰 Investi total      : {invested_eth:.6f} ETH\n\n"
    if total_positions > 0:
        msg += "Détails des positions :\n"
        for pos in positions:
            msg += f"→ Token {pos['token']} | Entrée {pos['entry_eth']:.6f} ETH\n"
    else:
        msg += "Pas de position ouverte pour l’instant."
    await update.message.reply_text(msg)

# ───────────────────────────────────────────────────────────────────────────────
# 7) LANCEMENT DU POLLING TELEGRAM DANS UN THREAD SANS HANDLER DE SIGNAUX
# ───────────────────────────────────────────────────────────────────────────────

def run_telegram_polling():
    """
    Crée un event loop dédié, supprime tout webhook en attente, puis lance run_polling(stop_signals=None).
    Ceci tourne dans un thread distinct, sans installer de gestionnaire de signaux.
    """
    # 1) Nouvel event loop pour ce thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 2) Reconstruction de l’Application PTB
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # 3) Enregistrement des handlers
    application.add_handler(CommandHandler("start",  start_handler))
    application.add_handler(CommandHandler("status", status_handler))

    # 4) Supprimer tout webhook (drop pending updates)
    loop.run_until_complete(
        application.bot.delete_webhook(drop_pending_updates=True)
    )

    # 5) Lancer run_polling en désactivant les signaux (dans ce thread non principal)
    application.run_polling(stop_signals=None)

# ───────────────────────────────────────────────────────────────────────────────
# 8) DÉMARRAGE PRINCIPAL
# ───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # a) Thread “daemon” pour la boucle copy-trading
    thread_loop = threading.Thread(target=main_loop, daemon=True)
    thread_loop.start()

    # b) Thread “daemon” pour le polling Telegram
    thread_tele = threading.Thread(target=run_telegram_polling, daemon=True)
    thread_tele.start()

    # c) Empêche le main thread de se terminer
    threading.Event().wait()

# ───────────────────────────────────────────────────────────────────────────────
# FIN DE main.py
# ───────────────────────────────────────────────────────────────────────────────
