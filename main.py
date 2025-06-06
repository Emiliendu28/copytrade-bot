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
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ───────────────────────────────────────────────────────────────────────────────
# 1) CHARGEMENT DES VARIABLES D’ENVIRONNEMENT
# ───────────────────────────────────────────────────────────────────────────────

load_dotenv()

# Votre token Telegram (ex : "123456789:ABCDEF…")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
# Votre chat_id (ex : "-1001234567890" pour un canal privé, ou "123456789" pour un utilisateur)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Clé privée de votre wallet (sans le « 0x » initial)
PRIVATE_KEY    = os.getenv("PRIVATE_KEY")
# Adresse checksummed de votre wallet (on la normalise en minuscules avant checksum)
RAW_WALLET     = os.getenv("WALLET_ADDRESS", "").strip().lower()
if not RAW_WALLET:
    raise RuntimeError("ERREUR : WALLET_ADDRESS non défini dans vos variables d’environnement !")
try:
    WALLET_ADDRESS = Web3.to_checksum_address(RAW_WALLET)
except Exception as e:
    raise RuntimeError(f"ERREUR : Impossible de normaliser WALLET_ADDRESS : {e}")

INFURA_URL     = os.getenv("INFURA_URL")       # ex : "https://mainnet.infura.io/v3/XXXXXXXX"
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

# Vulnérabilité : on doit être sûr que toutes ces clés soient définies
for varname, value in [
    ("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ("PRIVATE_KEY", PRIVATE_KEY),
    ("INFURA_URL", INFURA_URL),
    ("ETHERSCAN_API_KEY", ETHERSCAN_API_KEY),
]:
    if not value:
        raise RuntimeError(f"ERREUR : {varname} n’est pas défini dans l’environnement !")

# ───────────────────────────────────────────────────────────────────────────────
# 2) INITIALISATION DE WEB3 & CONTRAT UNISWAP V2
# ───────────────────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à Infura ! Vérifiez INFURA_URL.")

# Adresse du Router Uniswap V2 (Mainnet)
UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address(
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
)
UNISWAP_ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256",   "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path",         "type": "address[]"},
            {"internalType": "address",   "name": "to",           "type": "address"},
            {"internalType": "uint256",   "name": "deadline",     "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256",   "name": "amountIn",     "type": "uint256"},
            {"internalType": "uint256",   "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path",         "type": "address[]"},
            {"internalType": "address",   "name": "to",           "type": "address"},
            {"internalType": "uint256",   "name": "deadline",     "type": "uint256"}
        ],
        "name": "swapExactTokensForETH",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]
router_contract = w3.eth.contract(address=UNISWAP_ROUTER_ADDRESS, abi=UNISWAP_ROUTER_ABI)

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value",   "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]

# ───────────────────────────────────────────────────────────────────────────────
# 3) PARAMÈTRES DE COPYTRADING
# ───────────────────────────────────────────────────────────────────────────────

# Liste des whales (ex : adresses checksummed, minuscules évaluées en checksum) :
RAW_WHALES = [
    "0x4d2468bef1e33e17f7b017430ded6f7c169f7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080"
]
WHALES = [Web3.to_checksum_address(w.lower()) for w in RAW_WHALES]

# On garde en mémoire le dernier bloc traité pour chaque whale
last_processed_block = {whale: 0 for whale in WHALES}

# Budget mensuel en EUR → ETH (pour X trades/mois) :
MONTHLY_BUDGET_EUR = Decimal("100")
ETH_PRICE_USD      = Decimal("3500")  # estimation fixe
EUR_USD_RATE       = Decimal("1.10")  # taux fixe

def eur_to_eth(eur_amount: Decimal) -> Decimal:
    usd_amount = eur_amount * EUR_USD_RATE
    return (usd_amount / ETH_PRICE_USD).quantize(Decimal("0.000001"))

monthly_budget_eth = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE = (
    monthly_budget_eth / MAX_TRADES_PER_MONTH
).quantize(Decimal("0.000001"))

# Take-profit (+30 %) & Stop-loss (–15 %)
TP_THRESHOLD = Decimal("0.30")
SL_THRESHOLD = Decimal("0.15")

# Liste globale pour stocker les positions ouvertes
# Chaque position = { "token": str, "token_amount_wei": int, "entry_eth": Decimal, "entry_ratio": Decimal }
positions: list[dict] = []

# WETH « wrapped ETH » (utilisé pour fetch ou swap)
WETH_ADDRESS = Web3.to_checksum_address("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2")

# ───────────────────────────────────────────────────────────────────────────────
# 4) FONCTIONS UTILITAIRES
# ───────────────────────────────────────────────────────────────────────────────

def send_http_request(url: str, timeout: int = 10) -> dict:
    """
    Effectue une requête GET basique, renvoie la réponse JSON (ou une dict vide en cas d’erreur).
    """
    try:
        res = requests.get(url, timeout=timeout)
        return res.json()
    except Exception as e:
        print(f"Erreur HTTP GET ({url}) : {e}")
        return {}

def safe_send(message: str):
    """
    Envoie un message Telegram depuis un thread non-asynchrone.
    Ce wrapper crée un event loop temporaire pour exécuter Bot.send_message().
    """
    try:
        asyncio.run(BOT.send_message(chat_id=TELEGRAM_CHAT_ID, text=message))
    except Exception as e:
        print(f"⚠️ safe_send() error: {e}")

def est_uniswap_swap_exact_eth_for_tokens(input_hex: str) -> bool:
    return input_hex.startswith("0x7ff36ab5")

def est_uniswap_swap_exact_tokens_for_eth(input_hex: str) -> bool:
    return input_hex.startswith("0x18cbafe5")

def extract_token_from_swap_eth_for_tokens(input_hex: str) -> str:
    full = input_hex[2:]
    path_offset = 8 + 64 + 64
    token_start = 2 + path_offset + 64 + 24
    token_hex = input_hex[token_start: token_start + 40]
    return Web3.to_checksum_address("0x" + token_hex)

def extract_token_from_swap_tokens_for_eth(input_hex: str) -> str:
    full = input_hex[2:]
    path_offset = 8 + 64 + 64 + 64
    token_start = 2 + path_offset + 64 + 24
    token_hex = input_hex[token_start: token_start + 40]
    return Web3.to_checksum_address("0x" + token_hex)

def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    """
    Interroge l’API Etherscan pour récupérer toutes les tx ERC-20 (module=account, action=tokentx)
    de la whale à partir de start_block inclus.
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
        print(f"⚠️ Etherscan API returned status {res.get('status')} / {res.get('message')}")
        return []

def buy_token(token_address: str, eth_amount: Decimal) -> str | None:
    """
    Achète un token via swapExactETHForTokens (mirror de la whale) puis stocke la position.
    Retourne le hash de transaction en cas de succès, None sinon.
    """
    # 1) Check balance ETH
    balance_wei = w3.eth.get_balance(WALLET_ADDRESS)
    balance_eth = w3.from_wei(balance_wei, "ether")
    if balance_eth < eth_amount:
        safe_send(f"🚨 Solde insuffisant : {balance_eth:.6f} ETH dispo, "
                  f"il faut {eth_amount:.6f} ETH pour ce trade.")
        return None

    # 2) Estimation de tokens reçus
    path_buy = [WETH_ADDRESS, Web3.to_checksum_address(token_address)]
    amt_in_wei = w3.to_wei(eth_amount, "ether")
    try:
        amounts_out = router_contract.functions.getAmountsOut(amt_in_wei, path_buy).call()
    except Exception as e:
        safe_send(f"Erreur getAmountsOut (buy) : {e}")
        return None

    token_amt_est_wei = amounts_out[1]
    token_amt_est = Decimal(token_amt_est_wei) / Decimal(10**18)
    entry_eth = eth_amount
    entry_ratio = (entry_eth / token_amt_est).quantize(Decimal("0.000000000001"))

    # 3) Construction + envoi de la transaction swapExactETHForTokens
    deadline = int(time.time()) + 300
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    try:
        txn = router_contract.functions.swapExactETHForTokens(
            0,
            path_buy,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            "from": WALLET_ADDRESS,
            "value": amt_in_wei,
            "gas": 300_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "nonce": nonce
        })
    except Exception as e:
        safe_send(f"Erreur build_transaction (buy) : {e}")
        return None

    try:
        signed_txn = w3.eth.account.sign_transaction(txn, private_key=PRIVATE_KEY)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        tx_hash = tx_hash_bytes.hex()
    except Exception as e:
        safe_send(f"Erreur send_raw_transaction (buy) : {e}")
        return None

    # 4) Stockage de la position (mirror)
    positions.append({
        "token": token_address,
        "token_amount_wei": token_amt_est_wei,
        "entry_eth": entry_eth,
        "entry_ratio": entry_ratio
    })

    safe_send(f"[BUY] Mirror achat whale → {entry_eth:.6f} ETH → "
              f"{token_amt_est:.6f} tokens ({token_address})\nTx : {tx_hash}")
    return tx_hash

def sell_all_token(token_address: str) -> str | None:
    """
    Vend toute la balance du token_address (mirror de la whale) via swapExactTokensForETH.
    Retourne le hash de transaction ou None si erreur.
    """
    token_address = Web3.to_checksum_address(token_address)
    token_contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)

    # 1) Récupérer le solde token
    try:
        balance_token = token_contract.functions.balanceOf(WALLET_ADDRESS).call()
    except Exception as e:
        safe_send(f"Erreur balanceOf pour vente ({token_address}) : {e}")
        return None

    if balance_token == 0:
        safe_send(f"⚠️ Aucune balance à vendre pour {token_address}.")
        return None

    # 2.a) Approve du token pour le Router
    try:
        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        approve_txn = token_contract.functions.approve(
            UNISWAP_ROUTER_ADDRESS, balance_token
        ).build_transaction({
            "from": WALLET_ADDRESS,
            "gas": 100_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "nonce": nonce
        })
        signed_approve = w3.eth.account.sign_transaction(approve_txn, private_key=PRIVATE_KEY)
        tx_hash_a = w3.eth.send_raw_transaction(signed_approve.raw_transaction).hex()
        safe_send(f"[APPROVE] {token_address} → Router. Tx : {tx_hash_a}")
        time.sleep(12)  # on attend que l’approve soit minée
    except Exception as e:
        safe_send(f"Erreur Approve (sell) : {e}")
        return None

    # 2.b) swapExactTokensForETH
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
            "from": WALLET_ADDRESS,
            "gas": 300_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "nonce": nonce
        })
    except Exception as e:
        safe_send(f"Erreur build_transaction (sell) : {e}")
        return None

    try:
        signed_swap = w3.eth.account.sign_transaction(swap_txn, private_key=PRIVATE_KEY)
        tx_hash_s = w3.eth.send_raw_transaction(signed_swap.raw_transaction).hex()
    except Exception as e:
        safe_send(f"Erreur send_raw_transaction (sell) : {e}")
        return None

    safe_send(f"[SELL] Mirror vente whale → vend {balance_token/10**18:.6f} tokens ({token_address}). Tx : {tx_hash_s}")
    return tx_hash_s

def check_positions_and_maybe_sell():
    """
    Parcourt toutes les positions ouvertes et vend celles qui ont atteint TP_THRESHOLD ou SL_THRESHOLD.
    TP : +30 %, SL : –15 %.
    """
    global positions
    nouvelles_positions = []

    for pos in positions:
        token_address    = pos["token"]
        token_amount_wei = pos["token_amount_wei"]
        entry_eth        = pos["entry_eth"]
        entry_ratio      = pos["entry_ratio"]

        # Récupérer la valeur ETH actuelle pour ce token (via getAmountsOut)
        path_to_eth = [token_address, WETH_ADDRESS]
        try:
            amounts_out = router_contract.functions.getAmountsOut(
                token_amount_wei, path_to_eth
            ).call()
        except Exception as e:
            print(f"⚠️ Warning getAmountsOut (check) pour {token_address} : {e}")
            nouvelles_positions.append(pos)
            continue

        current_eth_value = Decimal(amounts_out[1]) / Decimal(10**18)
        ratio = (current_eth_value / entry_eth).quantize(Decimal("0.0001"))

        # TAKE-PROFIT ?
        if ratio >= (Decimal("1.0") + TP_THRESHOLD):
            safe_send(
                f"✅ TAKE-PROFIT pour {token_address} : valeur actuelle = {current_eth_value:.6f} ETH "
                f"(+{((ratio - 1) * 100):.1f} %). Revente automatique…"
            )
            sell_all_token(token_address)

        # STOP-LOSS ?
        elif ratio <= (Decimal("1.0") - SL_THRESHOLD):
            safe_send(
                f"⚠️ STOP-LOSS pour {token_address} : valeur actuelle = {current_eth_value:.6f} ETH "
                f"(−{((1 - ratio) * 100):.1f} %). Revente automatique…"
            )
            sell_all_token(token_address)

        else:
            # On conserve la position pour la prochaine vérif
            nouvelles_positions.append(pos)

    positions = nouvelles_positions

def process_whale_txns(whale: str):
    """
    Pour une whale donnée, on interroge Etherscan depuis last_processed_block[whale] et on "mirror" toute nouvelle swap.
    """
    global last_processed_block

    start_block = last_processed_block[whale] or 0
    txns = fetch_etherscan_txns(whale, start_block)

    for tx in txns:
        block_number = int(tx.get("blockNumber", 0))
        input_hex    = tx.get("input", "")
        from_addr    = Web3.to_checksum_address(tx.get("from", "0x0"))
        to_addr      = Web3.to_checksum_address(tx.get("to", "0x0"))

        # On ne s’intéresse qu’aux calls au Router Uniswap
        if to_addr.lower() != UNISWAP_ROUTER_ADDRESS.lower():
            continue

        # BUY ? (swapExactETHForTokens)
        if est_uniswap_swap_exact_eth_for_tokens(input_hex):
            token_addr = extract_token_from_swap_eth_for_tokens(input_hex)
            safe_send(f"👀 Whale {whale} BUY détecté : token {token_addr}, tx : {tx.get('hash')}")

            # On place un buy mirror pour ETH_PER_TRADE
            buy_token(token_addr, ETH_PER_TRADE)

        # SELL ? (swapExactTokensForETH)
        elif est_uniswap_swap_exact_tokens_for_eth(input_hex):
            token_addr = extract_token_from_swap_tokens_for_eth(input_hex)
            safe_send(f"👀 Whale {whale} SELL détecté : token {token_addr}, tx : {tx.get('hash')}")

            sell_all_token(token_addr)

        # On met à jour le last_processed_block
        last_processed_block[whale] = block_number

# ───────────────────────────────────────────────────────────────────────────────
# 5) BOUCLE PRINCIPALE (THREAD) : copytrading + TP/SL + résumé quotidien + heartbeat
# ───────────────────────────────────────────────────────────────────────────────

def main_loop():
    trades_this_month = 0
    last_month_checked = datetime.utcnow().month
    # On envoie un rapport chaque jour à 18h UTC
    next_summary_time = datetime.utcnow().replace(
        hour=18, minute=0, second=0, microsecond=0
    )
    if datetime.utcnow() > next_summary_time:
        next_summary_time += timedelta(days=1)

    # Envoi d’un message de démarrage
    safe_send("🚀 Bot copytrade whales (Mirror + TP/SL) démarre.")

    # On continue tant que le process tourne (daemon thread)
    while True:
        try:
            now = datetime.utcnow()

            # 1) Ping de maintien en vie toutes les heures
            # ------------------------------------------------------------
            if time.time() - main_loop.last_heartbeat > 3600:
                safe_send(f"✅ Bot actif à {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
                main_loop.last_heartbeat = time.time()

            # 2) SI nouveau mois, on réinitialise le compteur de trades
            # ------------------------------------------------------------
            if now.month != last_month_checked:
                trades_this_month = 0
                last_month_checked = now.month

            # 3) On check TP/SL sur toutes les positions
            # ------------------------------------------------------------
            check_positions_and_maybe_sell()

            # 4) Pour chaque whale, récupérer ses tx depuis last_processed_block
            # ------------------------------------------------------------
            for whale in WHALES:
                process_whale_txns(whale)

            # 5) Rapport quotidien à 18h UTC (22h heure de Paris)
            # ------------------------------------------------------------
            if now >= next_summary_time:
                nb_positions = len(positions)
                trades_restants = MAX_TRADES_PER_MONTH - trades_this_month
                eth_investi = trades_this_month * ETH_PER_TRADE

                summary_msg = (
                    f"🧾 Résumé du jour ({now.strftime('%Y-%m-%d')}):\n"
                    f"🔹 Positions ouvertes: {nb_positions}\n"
                    f"🔹 Trades restants: {trades_restants}/{MAX_TRADES_PER_MONTH}\n"
                    f"🔹 Total investi: {eth_investi:.6f} ETH"
                )
                safe_send(summary_msg)

                # Passage au lendemain
                next_summary_time += timedelta(days=1)

            time.sleep(30)

        except Exception as e:
            # En cas d’erreur fatale, on l’envoie sur Telegram puis on attend 60s
            print(f"Erreur dans main_loop : {e}")
            safe_send(f"❌ Erreur bot : {e}")
            time.sleep(60)

# Initialisation de l’attribut de heartbeat
main_loop.last_heartbeat = 0.0

# ───────────────────────────────────────────────────────────────────────────────
# 6) PARTIE TELEGRAM (API Asynchrone PTB v20)
# ───────────────────────────────────────────────────────────────────────────────

# Bot asynchrone « pur » pour l’ApplicationBuilder
BOT = Bot(token=TELEGRAM_TOKEN)

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Répond au /start en envoyant un message de bienvenue.
    """
    await update.message.reply_text(
        "🤖 Bot copytrade whales (Mirror + TP/SL) est en ligne.\n"
        "Tapez /status pour connaître le statut actuel."
    )

async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Répond au /status en envoyant un récapitulatif sommaire.
    """
    total_trades = len(positions)
    invested = sum(p["entry_eth"] for p in positions)
    msg = f"📊 Statut actuel du bot:\n\n"
    msg += f"🔁 Positions ouvertes: {total_trades}\n"
    msg += f"💰 Investi: {invested:.6f} ETH\n"
    if total_trades > 0:
        msg += "\nDétails:\n"
        for pos in positions:
            msg += f"→ Token {pos['token']} | {pos['entry_eth']:.6f} ETH\n"
    else:
        msg += "Aucune position ouverte actuellement."
    await update.message.reply_text(msg)

# ───────────────────────────────────────────────────────────────────────────────
# 7) DÉMARRAGE DU BOT & THREADS
# ───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # a) Création de l’Application Telegram (PTB v20)
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # b) Enregistrement des handlers Telegram (/start, /status)
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("status", status_handler))

    # c) Lancement du thread principal du copytrading
    thread_loop = threading.Thread(target=main_loop, daemon=True)
    thread_loop.start()

    # d) Supprimer un éventuel webhook existant (évite les conflits getUpdates)
    asyncio.run(application.bot.delete_webhook(drop_pending_updates=True))

    # e) Démarrage du polling Telegram (bloquant)
    application.run_polling()
