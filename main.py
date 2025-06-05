import os
import time
import requests
from datetime import datetime, timedelta
from decimal import Decimal

from threading import Thread

from web3 import Web3
from dotenv import load_dotenv

from telegram import Bot
from telegram.request import HTTPXRequest

# ─── 1) CHARGEMENT DES VARIABLES D’ENVIRONNEMENT ─────────────────────────
load_dotenv()

PRIVATE_KEY       = os.getenv("PRIVATE_KEY")         # Clé privée (sans "0x")
WALLET_ADDRESS    = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS"))
INFURA_URL        = os.getenv("INFURA_URL")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")

# Instanciation du Bot Telegram (utilisé dans send_telegram())
telegram_bot = Bot(token=TELEGRAM_TOKEN, request=HTTPXRequest())

# ─── DEBUG RAPIDE (pour s’assurer que tout est bien chargé) ───────────────
print("DEBUG → PRIVATE_KEY loaded :", PRIVATE_KEY is not None)
print("DEBUG → WALLET_ADDRESS    :", WALLET_ADDRESS)
print("DEBUG → INFURA_URL        :", INFURA_URL and INFURA_URL.startswith("https://"))
print("DEBUG → TELEGRAM_TOKEN    :", bool(TELEGRAM_TOKEN))
print("DEBUG → TELEGRAM_CHAT_ID  :", TELEGRAM_CHAT_ID)
print("DEBUG → ETHERSCAN_API_KEY :", ETHERSCAN_API_KEY is not None)

# ─── 2) INITIALISATION DE WEB3 ────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à Infura. Vérifie INFURA_URL.")

# ─── 3) INITIALISATION DU ROUTER UNISWAP V2 ────────────────────────────────
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

# ─── 4) LISTE DES WHALES À SURVEILLER ────────────────────────────────────
WHALES = [
    "0x4d2468bEf1e33e17f7b017430deD6F7c169F7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080"
]
last_processed_block = {whale: 0 for whale in WHALES}

# ─── 5) PARAMÉTRAGE DU BUDGET ET CONVERSION EUR → ETH ────────────────────
MONTHLY_BUDGET_EUR = Decimal('100')   # 100 € par mois
ETH_PRICE_USD      = Decimal('3500')  # estimation fixe
EUR_USD_RATE       = Decimal('1.10')  # taux fixe €→$

def eur_to_eth(eur_amount: Decimal) -> Decimal:
    usd_amount = eur_amount * EUR_USD_RATE
    return (usd_amount / ETH_PRICE_USD).quantize(Decimal('0.000001'))

monthly_budget_eth   = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE        = (monthly_budget_eth / MAX_TRADES_PER_MONTH).quantize(Decimal('0.000001'))

print(f"Budget mensuel → {MONTHLY_BUDGET_EUR} € ≃ {monthly_budget_eth} ETH")
print(f"→ {MAX_TRADES_PER_MONTH} trades/mois → {ETH_PER_TRADE} ETH par trade")

# ─── 6) CONSTANTES TAKE-PROFIT / STOP-LOSS ────────────────────────────────
TP_THRESHOLD = Decimal('0.30')   # Take-profit à +30 %
SL_THRESHOLD = Decimal('0.15')   # Stop-loss à −15 %

# Liste globale pour stocker les positions ouvertes
# Chaque position = { "token": str, "token_amount_wei": int, "entry_eth": Decimal, "entry_ratio": Decimal }
positions: list[dict] = []

# ─── 7) UTILITAIRES POUR PARSER L’INPUT HEX UNISWAP ───────────────────────
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

# ─── 8) ACHAT (BUY) SUR UNISWAP + STOCKAGE DE POSITION ───────────────────
def buy_token(token_address: str, eth_amount: Decimal) -> str | None:
    """
    Mirror BUY : swapExactETHForTokens pour 'eth_amount' ETH,
    puis stocke la position dans `positions` (token, quantité, prix d’entrée).
    """
    balance_wei = w3.eth.get_balance(WALLET_ADDRESS)
    balance_eth = w3.from_wei(balance_wei, 'ether')
    if balance_eth < eth_amount:
        send_telegram(f"🚨 Solde insuffisant : {balance_eth:.6f} ETH dispo, il faut {eth_amount:.6f} ETH.")
        return None

    weth_address = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    path_buy = [weth_address, Web3.to_checksum_address(token_address)]
    amount_in_wei = w3.to_wei(eth_amount, 'ether')

    # 1) Estimer la quantité de tokens obtenue
    try:
        amounts_out = router_contract.functions.getAmountsOut(amount_in_wei, path_buy).call()
    except Exception as e:
        send_telegram(f"Erreur getAmountsOut (buy): {e}")
        return None

    token_amount_estimate_wei = amounts_out[1]
    token_amount_estimate = Decimal(token_amount_estimate_wei) / Decimal(10**18)
    entry_eth = eth_amount
    entry_ratio = (entry_eth / token_amount_estimate).quantize(Decimal('0.000000000001'))

    # 2) Construction + envoi de la transaction swapExactETHForTokens
    deadline = int(time.time()) + 300
    nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
    try:
        txn = router_contract.functions.swapExactETHForTokens(
            0,               # amountOutMin = 0
            path_buy,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'value': amount_in_wei,
            'gas': 300_000,
            'gasPrice': w3.to_wei('30', 'gwei'),
            'nonce': nonce
        })
    except Exception as e:
        send_telegram(f"Erreur build_transaction (buy): {e}")
        return None

    try:
        signed_txn = w3.eth.account.sign_transaction(txn, private_key=PRIVATE_KEY)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        tx_hash = tx_hash_bytes.hex()
    except Exception as e:
        send_telegram(f"Erreur send_raw_transaction (buy): {e}")
        return None

    # 3) Stockage de la position pour TP/SL futur
    positions.append({
        "token": token_address,
        "token_amount_wei": token_amount_estimate_wei,
        "entry_eth": entry_eth,
        "entry_ratio": entry_ratio
    })

    send_telegram(
        f"[BUY] Mirror achat whale → {eth_amount:.6f} ETH → "
        f"{token_amount_estimate:.6f} tokens ({token_address}) | Tx: {tx_hash}"
    )
    return tx_hash

# ─── 9) VENTE (SELL) SUR UNISWAP — MIRROR DE LA VENTE DE LA WHALE ────────
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

def sell_all_token(token_address: str) -> str | None:
    """
    Mirror SELL : vend toute la balance du token 'token_address'.
    1) Approve du token → 2) swapExactTokensForETH.
    """
    token_address = Web3.to_checksum_address(token_address)
    token_contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)

    try:
        balance_token = token_contract.functions.balanceOf(WALLET_ADDRESS).call()
    except Exception as e:
        send_telegram(f"Erreur balanceOf pour vente ({token_address}): {e}")
        return None

    if balance_token == 0:
        send_telegram(f"⚠️ Pas de balance à vendre pour {token_address}.")
        return None

    # 9.a) Approve du token pour le Router Uniswap
    try:
        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        approve_txn = token_contract.functions.approve(
            UNISWAP_ROUTER_ADDRESS, balance_token
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 100_000,
            'gasPrice': w3.to_wei('30', 'gwei'),
            'nonce': nonce
        })
        signed_approve = w3.eth.account.sign_transaction(approve_txn, private_key=PRIVATE_KEY)
        tx_hash_a = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        tx_a = tx_hash_a.hex()
        send_telegram(f"[APPROVE] {token_address} → Router. Tx: {tx_a}")
        time.sleep(15)  # attente pour que l’approve soit miné
    except Exception as e:
        send_telegram(f"Erreur Approve (sell): {e}")
        return None

    # 9.b) swapExactTokensForETH du solde complet
    path_sell = [
        token_address,
        Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
    ]
    deadline = int(time.time()) + 300
    try:
        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        swap_txn = router_contract.functions.swapExactTokensForETH(
            balance_token,
            0,  # amountOutMin = 0
            path_sell,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            'from': WALLET_ADDRESS,
            'gas': 300_000,
            'gasPrice': w3.to_wei('30', 'gwei'),
            'nonce': nonce
        })
    except Exception as e:
        send_telegram(f"Erreur build_transaction (sell): {e}")
        return None

    try:
        signed_swap = w3.eth.account.sign_transaction(swap_txn, private_key=PRIVATE_KEY)
        tx_hash_s_bytes = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
        tx_hash_s = tx_hash_s_bytes.hex()
    except Exception as e:
        send_telegram(f"Erreur send_raw_transaction (sell): {e}")
        return None

    send_telegram(
        f"[SELL] Mirror vente whale → vend {balance_token/1e18:.6f} tokens de {token_address}. Tx: {tx_hash_s}"
    )
    return tx_hash_s

# ─── 10) FONCTION DE CHECK TP / SL ────────────────────────────────────────
def check_positions_and_maybe_sell():
    """
    Parcourt la liste `positions` et vend si TP (+30 %) ou SL (−15 %) atteint.
    """
    global positions

    WETH_ADDRESS = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27e756Cc2")
    nouvelles_positions = []

    for pos in positions:
        token_address    = pos["token"]
        token_amount_wei = pos["token_amount_wei"]
        entry_eth        = pos["entry_eth"]      # Decimal
        entry_ratio      = pos["entry_ratio"]    # Decimal

        # 1) Récupère la valeur ETH actuelle en vendant tout le token
        path_to_eth = [token_address, WETH_ADDRESS]
        try:
            amounts_out = router_contract.functions.getAmountsOut(token_amount_wei, path_to_eth).call()
        except Exception as e:
            # Si échec, on conserve la position pour réessayer plus tard
            print(f"⚠️ Warning getAmountsOut (check) pour {token_address}: {e}")
            nouvelles_positions.append(pos)
            continue

        current_eth_value = Decimal(amounts_out[1]) / Decimal(10**18)
        ratio = (current_eth_value / entry_eth).quantize(Decimal('0.0001'))

        # 2) TAKE-PROFIT (+30 %)
        if ratio >= (Decimal('1.0') + TP_THRESHOLD):
            send_telegram(
                f"✅ TAKE-PROFIT pour {token_address} : valeur actuelle = {current_eth_value:.6f} ETH "
                f"(+{(ratio - 1) * 100:.1f}% ), revente automatique…"
            )
            sell_all_token(token_address)

        # 3) STOP-LOSS (−15 %)
        elif ratio <= (Decimal('1.0') - SL_THRESHOLD):
            send_telegram(
                f"⚠️ STOP-LOSS pour {token_address} : valeur actuelle = {current_eth_value:.6f} ETH "
                f"(−{(1 - ratio) * 100:.1f}% ), revente automatique…"
            )
            sell_all_token(token_address)

        else:
            # 4) Sinon, on conserve la position pour la prochaine vérif
            nouvelles_positions.append(pos)

    positions = nouvelles_positions

# ─── 11) RÉCUPÉRATION DES TRANSACTIONS D’UNE WHALE VIA ETHERSCAN ─────────
def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    """
    Interroge l'API Etherscan (module=account, action=tokentx) pour toutes les tx ERC-20
    de la whale à partir de start_block (inclus).
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
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("status") == "1" and res.get("message") == "OK":
            return res.get("result", [])
        else:
            print(f"⚠️ Etherscan returned status {res.get('status')}, message {res.get('message')}")
            return []
    except Exception as e:
        print("Erreur HTTP Etherscan :", e)
        return []

# ─── 12) FONCTION D’ENVOI DE MESSAGE SUR TELEGRAM ────────────────────────
def send_telegram(msg: str):
    """
    Envoie un message Telegram en utilisant directement l'API HTTP.
    Cela évite le warning "coroutine 'Bot.send_message' was never awaited".
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg
    }
    try:
        # Envoi synchrone via requests
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Erreur Telegram (HTTP) :", e)

# ─── 13) BOUCLE PRINCIPALE DU BOT (TP/SL, scan Whale, résumé quotidien…) ───
def main_loop():
    trades_this_month = 0
    last_month_checked = datetime.utcnow().month
    # On veut envoyer le résumé chaque jour à 18 h heure de Paris (16 h UTC),
    # c’est-à-dire 18 h complicité locale. Ici, on fixe 16 h UTC (puisqu’on est en GMT+2).
    next_summary_time = datetime.utcnow().replace(hour=16, minute=0, second=0, microsecond=0)
    if datetime.utcnow() >= next_summary_time:
        # Si on est déjà passé au-delà de 16 h UTC aujourd’hui, on cible demain.
        next_summary_time += timedelta(days=1)

    send_telegram("🚀 Bot copytrade whales (Mirror + TP/SL) démarre.")
    last_heartbeat_time = time.time()

    while True:
        try:
            now = datetime.utcnow()

            # 🔄 Ping toutes les heures pour prouver que le bot est toujours actif
            if time.time() - last_heartbeat_time > 3600:
                send_telegram(f"✅ Bot actif à {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
                last_heartbeat_time = time.time()

            # ─── 13.a) Exemple de check TP/SL
            check_positions_and_maybe_sell()

            # ─── 13.b) Exemple de scan des whales (vous pouvez adapter selon votre logique)
            for whale in WHALES:
                start_block = last_processed_block.get(whale, 0)
                txns = fetch_etherscan_txns(whale, start_block)
                for tx in txns:
                    # Si c’est un swapExactETHForTokens (achat) on le “mirror”
                    input_hex = tx.get("input", "")
                    block_number = int(tx.get("blockNumber", 0))
                    if block_number <= last_processed_block[whale]:
                        continue

                    # 1) Whale a acheté un token
                    if est_uniswap_swap_exact_eth_for_tokens(input_hex):
                        token_addr = extract_token_from_swap_eth_for_tokens(input_hex)
                        # On effectue un buy “mirror” du même montant alloué
                        buy_token(token_addr, ETH_PER_TRADE)

                    # 2) Whale a vendu un token
                    elif est_uniswap_swap_exact_tokens_for_eth(input_hex):
                        token_addr = extract_token_from_swap_tokens_for_eth(input_hex)
                        # On effectue un sell “mirror” (toute la position)
                        sell_all_token(token_addr)

                    last_processed_block[whale] = block_number

            # ─── 13.c) Résumé quotidien à 18 h (heure de Paris / 16 h UTC)
            if datetime.utcnow() >= next_summary_time:
                nb_positions = len(positions)
                trades_restants = MAX_TRADES_PER_MONTH - trades_this_month
                eth_investi = trades_this_month * ETH_PER_TRADE

                summary_msg = (
                    f"🧾 Résumé du jour ({datetime.utcnow().strftime('%Y-%m-%d')}):\n"
                    f"🔹 Positions ouvertes : {nb_positions}\n"
                    f"🔹 Trades restants : {trades_restants}/{MAX_TRADES_PER_MONTH}\n"
                    f"🔹 Total investi : {eth_investi:.6f} ETH"
                )
                send_telegram(summary_msg)

                # On planifie le prochain résumé pour demain à la même heure
                next_summary_time += timedelta(days=1)

            # Petite pause avant la prochaine itération
            time.sleep(30)

        except Exception as e:
            print(f"Erreur dans la boucle principale : {e}")
            send_telegram(f"❌ Erreur bot : {e}")
            time.sleep(60)

# ─── 14) LANCEMENT DE LA BOUCLE PRINCIPALE (PUISQUE PAS DE POLLING) ─────────
if __name__ == "__main__":
    # On exécute main_loop() dans le thread principal directement
    # (pas de ApplicationBuilder ni de /status)
    main_loop()
