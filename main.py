import os
import time
import requests
from datetime import datetime, timedelta
from decimal import Decimal

from web3 import Web3
from dotenv import load_dotenv

# ─── 1) CHARGEMENT DES VARIABLES D’ENVIRONNEMENT ─────────────────────────
load_dotenv()

# 1.a) Vérifier que WALLET_ADDRESS est bien défini dans Render → Settings → Environment → Variables
raw_wallet = os.getenv("WALLET_ADDRESS")
if raw_wallet is None or raw_wallet.strip() == "":
    raise ValueError(
        "La variable d’environnement 'WALLET_ADDRESS' n’est pas définie ou est vide.\n"
        "→ Allez dans Render → Settings → Environment → Variables,\n"
        "   créez Key=WALLET_ADDRESS et Value=0xVotreAdresseEthereum (tout en minuscules).\n"
        "   Puis redeployez."
    )
WALLET_ADDRESS = Web3.to_checksum_address(raw_wallet.strip().lower())

# 1.b) Les autres variables d’environnement obligatoires
PRIVATE_KEY       = os.getenv("PRIVATE_KEY")       # votre clé privée (sans 0x)
INFURA_URL        = os.getenv("INFURA_URL")        # ex. https://mainnet.infura.io/v3/xxxxx
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")    # token de votre bot Telegram
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")  # chat_id (nombre) où envoyer les messages
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY") # clé API Etherscan

if not PRIVATE_KEY or not INFURA_URL or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not ETHERSCAN_API_KEY:
    raise ValueError(
        "Une ou plusieurs variables d’environnement manquent :\n"
        "PRIVATE_KEY, INFURA_URL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ETHERSCAN_API_KEY\n"
        "Vérifiez dans Render → Settings → Environment → Variables."
    )

# URL de l’API Telegram pour envoi synchrone
telegram_api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# ─── 2) INITIALISATION DE WEB3 ────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à Infura. Vérifiez INFURA_URL.")

# ─── 3) ROUTER UNISWAP & WETH (version correcte en minuscules + checksum) ───

# 3.a) Uniswap V2 Router (hard‐code en minuscules → to_checksum_address)
raw_router = "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"
UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address(raw_router.lower().strip())

# 3.b) WETH (CORRIGÉ : adresse complète, en minuscules, avant d’appeler to_checksum_address)
raw_weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WETH_ADDRESS = Web3.to_checksum_address(raw_weth.lower().strip())

# ABI minimaliste pour Uniswap V2
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

router_contract = w3.eth.contract(
    address=UNISWAP_ROUTER_ADDRESS,
    abi=UNISWAP_ROUTER_ABI
)

# ─── 4) LISTE DES WHALES À SURVEILLER (string en minuscules → to_checksum_address) ───
WHALES_RAW = [
    "0x4d2468bef1e33e17f7b017430ded6f7c169f7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080"
]
WHALES = [Web3.to_checksum_address(w.strip().lower()) for w in WHALES_RAW]
last_processed_block = {whale: 0 for whale in WHALES}

# ─── 5) BUDGET & CONVERSION EUR → ETH ───────────────────────────────────────
MONTHLY_BUDGET_EUR = Decimal('100')   # 100 € par mois
ETH_PRICE_USD      = Decimal('3500')  # estimation statique (à ajuster si besoin)
EUR_USD_RATE       = Decimal('1.10')  # taux fixe €→$

def eur_to_eth(eur_amount: Decimal) -> Decimal:
    usd_amount = eur_amount * EUR_USD_RATE
    return (usd_amount / ETH_PRICE_USD).quantize(Decimal('0.000001'))

monthly_budget_eth   = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE        = (monthly_budget_eth / MAX_TRADES_PER_MONTH).quantize(Decimal('0.000001'))

print(f"Budget mensuel → {MONTHLY_BUDGET_EUR} € ≃ {monthly_budget_eth} ETH")
print(f"→ {MAX_TRADES_PER_MONTH} trades/mois → {ETH_PER_TRADE} ETH par trade")

# ─── 6) CONSTANTES TAKE‐PROFIT / STOP‐LOSS ────────────────────────────────
TP_THRESHOLD = Decimal('0.30')   # +30 % de prise de profit
SL_THRESHOLD = Decimal('0.15')   # −15 % de stop‐loss

# ─── Stocke les positions ouvertes ───────────────────────────────────────
# Chaque entrée : {
#   "token": str,
#   "token_amount_wei": int,
#   "entry_eth": Decimal,
#   "entry_ratio": Decimal
# }
positions: list[dict] = []

# ─── 7) PARSING DE L’INPUT HEX UNISWAP ───────────────────────────────────
def est_uniswap_swap_exact_eth_for_tokens(input_hex: str) -> bool:
    return input_hex.startswith("0x7ff36ab5")

def est_uniswap_swap_exact_tokens_for_eth(input_hex: str) -> bool:
    return input_hex.startswith("0x18cbafe5")

def extract_token_from_swap_eth_for_tokens(input_hex: str) -> str:
    """
    Extrait l’adresse du token depuis l’input hex d’un swapExactETHForTokens.
    On force le token_hex en minuscules avant de faire to_checksum_address.
    """
    path_offset = 8 + 64 + 64
    token_start = 2 + path_offset + 64 + 24
    token_hex   = input_hex[token_start : token_start + 40]
    token_str   = "0x" + token_hex.lower()
    return Web3.to_checksum_address(token_str)

def extract_token_from_swap_tokens_for_eth(input_hex: str) -> str:
    """
    Extrait l’adresse du token depuis l’input hex d’un swapExactTokensForETH.
    On force le token_hex en minuscules avant de faire to_checksum_address.
    """
    path_offset = 8 + 64 + 64 + 64
    token_start = 2 + path_offset + 64 + 24
    token_hex   = input_hex[token_start : token_start + 40]
    token_str   = "0x" + token_hex.lower()
    return Web3.to_checksum_address(token_str)

# ─── 8) ACHAT (BUY) SUR UNISWAP + STOCKAGE DE POSITION ───────────────────
def buy_token(token_address: str, eth_amount: Decimal) -> str | None:
    """
    Mirror BUY : swapExactETHForTokens pour 'eth_amount' ETH,
    puis stocke la position dans `positions`.
    """
    # 1) Vérifier le solde du wallet en ETH
    balance_wei = w3.eth.get_balance(WALLET_ADDRESS)
    balance_eth = w3.from_wei(balance_wei, 'ether')
    if balance_eth < eth_amount:
        send_telegram(f"🚨 Solde insuffisant : {balance_eth:.6f} ETH dispo, il faut {eth_amount:.6f} ETH.")
        return None

    # 2) Préparation du swap
    tkn_addr      = Web3.to_checksum_address(token_address.strip().lower())
    path_buy      = [WETH_ADDRESS, tkn_addr]
    amount_in_wei = w3.to_wei(eth_amount, 'ether')

    try:
        amounts_out = router_contract.functions.getAmountsOut(amount_in_wei, path_buy).call()
    except Exception as e:
        send_telegram(f"Erreur getAmountsOut (buy) : {e}")
        return None

    token_amount_estimate_wei = amounts_out[1]
    token_amount_estimate     = Decimal(token_amount_estimate_wei) / Decimal(10**18)
    entry_eth                 = eth_amount
    entry_ratio               = (entry_eth / token_amount_estimate).quantize(Decimal('0.000000000001'))

    # 3) Construire et envoyer la transaction swapExactETHForTokens
    deadline = int(time.time()) + 300
    nonce    = w3.eth.get_transaction_count(WALLET_ADDRESS)
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
        send_telegram(f"Erreur build_transaction (buy) : {e}")
        return None

    try:
        signed_txn    = w3.eth.account.sign_transaction(txn, private_key=PRIVATE_KEY)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        tx_hash       = tx_hash_bytes.hex()
    except Exception as e:
        send_telegram(f"Erreur send_raw_transaction (buy) : {e}")
        return None

    # 4) Stocker la position pour suivi TP/SL
    positions.append({
        "token": tkn_addr,
        "token_amount_wei": token_amount_estimate_wei,
        "entry_eth": entry_eth,
        "entry_ratio": entry_ratio
    })

    send_telegram(
        f"[BUY] Mirror achat whale → {eth_amount:.6f} ETH → "
        f"{token_amount_estimate:.6f} tokens ({tkn_addr}) | Tx : {tx_hash}"
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
    Mirror SELL : vend toute la balance du token 'token_address' détenu dans votre wallet.
    1) Approve → 2) swapExactTokensForETH.
    """
    tkn_addr       = Web3.to_checksum_address(token_address.strip().lower())
    token_contract = w3.eth.contract(address=tkn_addr, abi=ERC20_ABI)

    try:
        balance_token = token_contract.functions.balanceOf(WALLET_ADDRESS).call()
    except Exception as e:
        send_telegram(f"Erreur balanceOf pour vente ({tkn_addr}) : {e}")
        return None

    if balance_token == 0:
        send_telegram(f"⚠️ Pas de balance à vendre pour {tkn_addr}.")
        return None

    # 9.a) Approve pour Uniswap
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
        tx_hash_a      = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
        tx_a           = tx_hash_a.hex()
        send_telegram(f"[APPROVE] {tkn_addr} → Router. Tx : {tx_a}")
        time.sleep(15)  # attendre que l’approve soit minée
    except Exception as e:
        send_telegram(f"Erreur Approve (sell) : {e}")
        return None

    # 9.b) swapExactTokensForETH (toute la balance)
    path_sell = [tkn_addr, WETH_ADDRESS]
    deadline  = int(time.time()) + 300
    try:
        nonce    = w3.eth.get_transaction_count(WALLET_ADDRESS)
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
        send_telegram(f"Erreur build_transaction (sell) : {e}")
        return None

    try:
        signed_swap   = w3.eth.account.sign_transaction(swap_txn, private_key=PRIVATE_KEY)
        tx_hash_sbytes = w3.eth.send_raw_transaction(signed_swap.raw_transaction)
        tx_hash_s      = tx_hash_sbytes.hex()
    except Exception as e:
        send_telegram(f"Erreur send_raw_transaction (sell) : {e}")
        return None

    send_telegram(
        f"[SELL] Mirror vente whale → vend {balance_token/1e18:.6f} tokens de {tkn_addr}. Tx : {tx_hash_s}"
    )
    return tx_hash_s

# ─── 10) CHECK TP / SL ────────────────────────────────────────────────────
def check_positions_and_maybe_sell():
    """
    Parcourt `positions` et vend si TP (+30 %) ou SL (−15 %) atteint.
    """
    global positions
    nouvelles_positions: list[dict] = []

    for pos in positions:
        token_address    = pos["token"]
        token_amount_wei = pos["token_amount_wei"]
        entry_eth        = pos["entry_eth"]
        entry_ratio      = pos["entry_ratio"]

        tkn_addr    = Web3.to_checksum_address(token_address.strip().lower())
        path_to_eth = [tkn_addr, WETH_ADDRESS]

        try:
            amounts_out = router_contract.functions.getAmountsOut(token_amount_wei, path_to_eth).call()
        except Exception as e:
            # Si échec momentané, on garde la position pour réessayer plus tard
            print(f"⚠️ Warning getAmountsOut (check) pour {tkn_addr} : {e}")
            nouvelles_positions.append(pos)
            continue

        current_eth_value = Decimal(amounts_out[1]) / Decimal(10**18)
        ratio = (current_eth_value / entry_eth).quantize(Decimal('0.0001'))

        # TAKE‐PROFIT (+30 %)
        if ratio >= (Decimal('1.0') + TP_THRESHOLD):
            send_telegram(
                f"✅ TAKE-PROFIT pour {tkn_addr} : valeur actuelle = {current_eth_value:.6f} ETH "
                f"(+{(ratio - 1) * 100:.1f}% ), revente auto…"
            )
            sell_all_token(token_address)

        # STOP‐LOSS (−15 %)
        elif ratio <= (Decimal('1.0') - SL_THRESHOLD):
            send_telegram(
                f"⚠️ STOP-LOSS pour {tkn_addr} : valeur actuelle = {current_eth_value:.6f} ETH "
                f"(−{(1 - ratio) * 100:.1f}% ), revente auto…"
            )
            sell_all_token(token_address)

        else:
            nouvelles_positions.append(pos)

    positions = nouvelles_positions

# ─── 11) RÉCUPÉRATION DES TX ERC20 D’UNE WHALE VIA ETHERSCAN ─────────────
def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    """
    Interroge l’API Etherscan (module=account, action=tokentx) pour toutes les tx ERC20
    de la whale à partir de start_block (inclus), triées par ordre ascendant.
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

# ─── 12) ENVOI DE MESSAGE SUR TELEGRAM (HTTP) ─────────────────────────────
def send_telegram(msg: str):
    """
    Envoie un message Telegram via l’API HTTP (requests.post).
    Aucun warning de coroutine n’apparaît : c’est 100 % synchrone.
    """
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(telegram_api_url, json=payload, timeout=5)
    except Exception as e:
        print("Erreur Telegram (HTTP) :", e)

# ─── 13) BOUCLE PRINCIPALE DU BOT (TP/SL, scan Whale, résumé quotidien) ───
def main_loop():
    trades_this_month  = 0
    last_month_checked = datetime.utcnow().month
    # Résumé chaque jour à 18 h (Paris) → 16 h (UTC)
    next_summary_time = datetime.utcnow().replace(hour=16, minute=0, second=0, microsecond=0)
    if datetime.utcnow() >= next_summary_time:
        next_summary_time += timedelta(days=1)

    send_telegram("🚀 Bot copytrade whales (Mirror + TP/SL) démarre.")
    last_heartbeat_time = time.time()

    while True:
        try:
            now = datetime.utcnow()

            # 🔄 Ping toutes les heures pour montrer que le bot tourne
            if time.time() - last_heartbeat_time > 3600:
                send_telegram(f"✅ Bot actif à {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
                last_heartbeat_time = time.time()

            # ─── 13.a) Vérifier TP / SL
            check_positions_and_maybe_sell()

            # ─── 13.b) Scanner les transactions des whales
            for whale in WHALES:
                start_block = last_processed_block.get(whale, 0)
                txns        = fetch_etherscan_txns(whale, start_block)
                for tx in txns:
                    block_number = int(tx.get("blockNumber", 0))
                    if block_number <= last_processed_block[whale]:
                        continue

                    input_hex = tx.get("input", "")

                    # Whale a acheté un token ?
                    if est_uniswap_swap_exact_eth_for_tokens(input_hex):
                        token_addr = extract_token_from_swap_eth_for_tokens(input_hex)
                        buy_token(token_addr, ETH_PER_TRADE)

                    # Whale a vendu un token ?
                    elif est_uniswap_swap_exact_tokens_for_eth(input_hex):
                        token_addr = extract_token_from_swap_tokens_for_eth(input_hex)
                        sell_all_token(token_addr)

                    last_processed_block[whale] = block_number

            # ─── 13.c) Résumé quotidien à 18 h (Paris) / 16 h (UTC)
            if datetime.utcnow() >= next_summary_time:
                nb_positions    = len(positions)
                trades_restants = MAX_TRADES_PER_MONTH - trades_this_month
                eth_investi     = trades_this_month * ETH_PER_TRADE

                summary_msg = (
                    f"🧾 Résumé du jour ({datetime.utcnow().strftime('%Y-%m-%d')}):\n"
                    f"🔹 Positions ouvertes : {nb_positions}\n"
                    f"🔹 Trades restants    : {trades_restants}/{MAX_TRADES_PER_MONTH}\n"
                    f"🔹 Total investi      : {eth_investi:.6f} ETH"
                )
                send_telegram(summary_msg)
                next_summary_time += timedelta(days=1)

            time.sleep(30)

        except Exception as e:
            print(f"Erreur dans la boucle principale : {e}")
            send_telegram(f"❌ Erreur bot : {e}")
            time.sleep(60)

# ─── 14) LANCEMENT DE LA BOUCLE PRINCIPALE — SANS POLLING TELÉGRAM ───────
if __name__ == "__main__":
    main_loop()
