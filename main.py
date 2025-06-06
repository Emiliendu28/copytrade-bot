import os
import time
import threading
import requests
import asyncio

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

# ─── 1) CHARGEMENT DES VARIABLES D’ENVIRONNEMENT ────────────────────────────

load_dotenv()

INFURA_URL        = os.getenv("INFURA_URL")          # ex. https://mainnet.infura.io/v3/xxxxxxx
PRIVATE_KEY       = os.getenv("PRIVATE_KEY")         # Clé privée (sans "0x")
RAW_WALLET        = os.getenv("WALLET_ADDRESS")      # ex. 0x32a7fA3b… (public)
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")      # token de votre bot Telegram
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")    # ID du chat où envoyer les alertes
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY")   # (optionnel) pour scruter transactions Whale

if not (INFURA_URL and PRIVATE_KEY and RAW_WALLET and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
    raise RuntimeError(
        "Il manque au moins une variable d'environnement essentielle :\n"
        "INFURA_URL, PRIVATE_KEY, WALLET_ADDRESS, TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID."
    )

WALLET_ADDRESS = Web3.to_checksum_address(RAW_WALLET.strip().lower())

# ─── 2) INITIALISATION DE WEB3 ────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(INFURA_URL))
if not w3.is_connected():
    raise ConnectionError("Impossible de se connecter à Infura.")
CHAIN_ID = w3.eth.chain_id  # 1 = Mainnet, 5 = Goerli, etc.

# ─── 3) CONFIGURATION DU BOT TELEGRAM ────────────────────────────────────

application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def send_telegram(msg: str):
    """
    Envoie un message au chat Telegram configuré.
    """
    try:
        await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)
    except Exception as e:
        # En cas d’erreur, on logge en console, mais on ne relance pas l’exception
        print(f"⚠️ Erreur Telegram → {e}")

def safe_send(msg: str):
    """
    Permet de lancer `send_telegram(msg)` depuis un thread externe
    en s'assurant qu'il sera awaité sur l'event loop de `application.bot`.
    """
    try:
        loop = application.bot.loop
        asyncio.run_coroutine_threadsafe(send_telegram(msg), loop)
    except Exception as e:
        print(f"⚠️ safe_send() → {e}")

# ─── 4) CONSTANTES DE PROTECTION (SLIPPAGE, GAS, BUDGET, FILTRES) ──────────

SLIPPAGE_TOLERANCE   = Decimal("0.02")    # 2 % de slippage max
MAX_GAS_GWEI         = 50                # gwei maximum autorisé
MIN_ETH_COPY         = Decimal("0.005")   # 0.005 ETH minimum copié
MIN_TOKEN_VOLUME_USD = Decimal("1000")    # volume Whale ≥ $1 000
MIN_LIQUIDITY_POOL_ETH = Decimal("10")    # pool doit avoir ≥ 10 ETH
COOLDOWN_TIME        = 60                # 60 s entre deux trades
TX_RECEIPT_TIMEOUT   = 180               # 3 minutes max pour obtenir un receipt
MAX_TOTAL_FEES_USD   = Decimal("10")     # ne pas dépasser 10 $ de frais gaz ce mois

MONTHLY_BUDGET_EUR   = Decimal("100")     # 100 € par mois
ETH_PRICE_USD        = Decimal("3500")    # estimation fixe
EUR_USD_RATE         = Decimal("1.10")    # estimation fixe

def eur_to_eth(eur_amount: Decimal) -> Decimal:
    usd = eur_amount * EUR_USD_RATE
    return (usd / ETH_PRICE_USD).quantize(Decimal("0.000001"))

monthly_budget_eth   = eur_to_eth(MONTHLY_BUDGET_EUR)
MAX_TRADES_PER_MONTH = 5
ETH_PER_TRADE        = (monthly_budget_eth / MAX_TRADES_PER_MONTH).quantize(Decimal("0.000001"))

TP_THRESHOLD = Decimal("0.30")  # +30 %
SL_THRESHOLD = Decimal("0.15")  # −15 %

# ─── 5) CONSTANTES UNISWAP / WETH / ERC20 ──────────────────────────────────

UNISWAP_ROUTER_ADDRESS = Web3.to_checksum_address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
router_contract = w3.eth.contract(
    address=UNISWAP_ROUTER_ADDRESS,
    abi=[
        # swapExactETHForTokens
        {
            "inputs":[
                {"internalType":"uint256","name":"amountOutMin","type":"uint256"},
                {"internalType":"address[]","name":"path","type":"address[]"},
                {"internalType":"address","name":"to","type":"address"},
                {"internalType":"uint256","name":"deadline","type":"uint256"}
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
                {"internalType":"uint256","name":"deadline","type":"uint256"}
            ],
            "name":"swapExactTokensForETH",
            "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
            "stateMutability":"nonpayable","type":"function"
        },
        # getAmountsOut
        {
            "inputs":[
                {"internalType":"uint256","name":"amountIn","type":"uint256"},
                {"internalType":"address[]","name":"path","type":"address[]"}
            ],
            "name":"getAmountsOut",
            "outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],
            "stateMutability":"view","type":"function"
        }
    ]
)

WETH_ADDRESS = Web3.to_checksum_address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")

ERC20_ABI_FULL = [
    {
        "constant":True,
        "inputs":[{"name":"_owner","type":"address"}],
        "name":"balanceOf",
        "outputs":[{"name":"balance","type":"uint256"}],
        "type":"function"
    },
    {
        "constant":False,
        "inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],
        "name":"approve",
        "outputs":[{"name":"","type":"bool"}],
        "type":"function"
    },
    {
        "constant":True,
        "inputs":[],
        "name":"symbol",
        "outputs":[{"name":"","type":"string"}],
        "type":"function"
    },
    {
        "constant":True,
        "inputs":[],
        "name":"decimals",
        "outputs":[{"name":"","type":"uint8"}],
        "type":"function"
    }
]

PAIR_FACTORY_ADDRESS = Web3.to_checksum_address("0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f")
PAIR_FACTORY_ABI = [
    {
        "inputs":[
            {"internalType":"address","name":"tokenA","type":"address"},
            {"internalType":"address","name":"tokenB","type":"address"}
        ],
        "name":"getPair",
        "outputs":[{"internalType":"address","name":"pair","type":"address"}],
        "stateMutability":"view",
        "type":"function"
    }
]
factory_contract = w3.eth.contract(address=PAIR_FACTORY_ADDRESS, abi=PAIR_FACTORY_ABI)

PAIR_ABI = [
    {
        "inputs":[],
        "name":"getReserves",
        "outputs":[
            {"internalType":"uint112","name":"_reserve0","type":"uint112"},
            {"internalType":"uint112","name":"_reserve1","type":"uint112"},
            {"internalType":"uint32","name":"_blockTimestampLast","type":"uint32"}
        ],
        "stateMutability":"view","type":"function"
    },
    {
        "inputs":[],
        "name":"token0",
        "outputs":[{"internalType":"address","name":"","type":"address"}],
        "stateMutability":"view","type":"function"
    },
    {
        "inputs":[],
        "name":"token1",
        "outputs":[{"internalType":"address","name":"","type":"address"}],
        "stateMutability":"view","type":"function"
    }
]

# ─── 6) LISTE DES WHALES À SURVEILLER ────────────────────────────────────

WHALES = [
    "0x4d2468bEf1e33e17f7b017430deD6F7c169F7054",
    "0xdbf5e9c5206d0db70a90108bf936da60221dc080"
]
last_processed_block = {whale: 0 for whale in WHALES}

# ─── 7) STRUCTURE POUR LES POSITIONS OUVERTES ──────────────────────────────

positions: list[dict] = []
trades_this_month      = 0
last_month_checked     = datetime.utcnow().month
total_fees_spent_usd   = Decimal("0")
stop_trading           = False
dernier_trade_timestamp = 0

# ─── 8) UTILITAIRES POUR PARSER LE HEX D’UN SWAP ───────────────────────────

def est_uniswap_swap_exact_eth_for_tokens(input_hex: str) -> bool:
    return input_hex.startswith("0x7ff36ab5")

def extract_path_from_input(input_hex: str) -> list[str]:
    raw = input_hex[2:]
    path_offset = 8 + 64 + 64
    length_hex = raw[path_offset : path_offset + 64]
    N = int(length_hex, 16)
    path_addresses: list[str] = []
    for i in range(N):
        start = path_offset + 64 + i*64
        token_hex = raw[start + 24 : start + 64]
        addr = Web3.to_checksum_address("0x" + token_hex)
        path_addresses.append(addr)
    return path_addresses

def est_adresse_valide(addr: str) -> bool:
    try:
        _ = Web3.to_checksum_address(addr)
        return True
    except:
        return False

# ─── 9) MÉTADATA TOKEN (symbol, decimals) ────────────────────────────────

def get_token_metadata(token_address: str) -> tuple[str, int]:
    try:
        token_contract = w3.eth.contract(address=token_address, abi=ERC20_ABI_FULL)
        symbol = token_contract.functions.symbol().call()
        decimals = token_contract.functions.decimals().call()
        return symbol, decimals
    except Exception:
        return "", 18

# ─── 10) VÉRIFICATIONS (LIQUIDITÉ MIN, GAS) ───────────────────────────────

def verifier_liquidite_minimale(token_address: str) -> bool:
    try:
        paire_addr = factory_contract.functions.getPair(WETH_ADDRESS, token_address).call()
        if int(paire_addr, 16) == 0:
            return False
        pair_contract = w3.eth.contract(address=paire_addr, abi=PAIR_ABI)
        r0, r1, _ = pair_contract.functions.getReserves().call()
        tok0 = pair_contract.functions.token0().call().lower()
        reserve_weth = (
            Decimal(r0) / Decimal(10**18)
            if tok0 == WETH_ADDRESS.lower()
            else Decimal(r1) / Decimal(10**18)
        )
        return reserve_weth >= MIN_LIQUIDITY_POOL_ETH
    except Exception as e:
        print(f"⚠️ Erreur vérif liquidité ({token_address}) : {e}")
        return False

def verifier_gas_price() -> bool:
    try:
        prix_gaz = w3.eth.gas_price
        prix_gaz_gwei = w3.from_wei(prix_gaz, "gwei")
        return prix_gaz_gwei <= MAX_GAS_GWEI
    except:
        return False

# ─── 11) FONCTION D’ACHAT (BUY) ─────────────────────────────────────────────

def buy_token(token_address: str, eth_amount: Decimal) -> str | None:
    global total_fees_spent_usd, trades_this_month

    try:
        balance_wei = w3.eth.get_balance(WALLET_ADDRESS)
        balance_eth = w3.from_wei(balance_wei, "ether")
        if balance_eth < eth_amount:
            safe_send(f"🚨 Solde insuffisant : {balance_eth:.6f} ETH, requis {eth_amount:.6f} ETH.")
            return None

        token_addr = Web3.to_checksum_address(token_address)
        path = [WETH_ADDRESS, token_addr]
        amount_in_wei = w3.to_wei(eth_amount, "ether")

        try:
            amounts_out = router_contract.functions.getAmountsOut(amount_in_wei, path).call()
            token_out_est_wei = amounts_out[1]
            token_out_est = Decimal(token_out_est_wei) / Decimal(10**18)
            entry_eth = eth_amount
            entry_ratio = (entry_eth / token_out_est).quantize(Decimal("0.000000000001"))
        except Exception:
            token_out_est_wei = 0
            token_out_est = Decimal("0")
            entry_ratio = Decimal("0")
            entry_eth = eth_amount

        if token_out_est_wei > 0:
            min_tokens_wei = int((token_out_est * (Decimal("1") - SLIPPAGE_TOLERANCE)) * Decimal(10**18))
        else:
            min_tokens_wei = 0

        prix_gaz = w3.eth.gas_price
        prix_gaz_gwei = w3.from_wei(prix_gaz, "gwei")
        if prix_gaz_gwei > MAX_GAS_GWEI:
            safe_send(f"⛔ GasPrice trop élevé ({prix_gaz_gwei:.1f} gwei > {MAX_GAS_GWEI}). Achat annulé.")
            return None

        gas_price_to_use = w3.to_wei(min(prix_gaz_gwei, MAX_GAS_GWEI), "gwei")
        deadline = int(time.time()) + 300
        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        txn = router_contract.functions.swapExactETHForTokens(
            min_tokens_wei,
            path,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            "from": WALLET_ADDRESS,
            "value": amount_in_wei,
            "gas": 300_000,
            "gasPrice": gas_price_to_use,
            "nonce": nonce,
            "chainId": CHAIN_ID
        })

        signed = w3.eth.account.sign_transaction(txn, private_key=PRIVATE_KEY)
        tx_hash_bytes = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash = tx_hash_bytes.hex()

        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_RECEIPT_TIMEOUT)
            frais_eth = Decimal(receipt.gasUsed * receipt.effectiveGasPrice) / Decimal(10**18)
            frais_usd = (frais_eth * ETH_PRICE_USD).quantize(Decimal("0.01"))
            total_fees_spent_usd += frais_usd
        except Exception:
            safe_send(f"⏱ Timeout confirmation achat {token_addr} → frais non comptabilisés.")

        if total_fees_spent_usd > MAX_TOTAL_FEES_USD:
            safe_send(f"⚠️ Frais gaz ce mois ({total_fees_spent_usd:.2f}$) > {MAX_TOTAL_FEES_USD}$. Stop trades.")
            return None

        positions.append({
            "token": token_addr,
            "token_amount_wei": token_out_est_wei,
            "entry_eth": entry_eth,
            "entry_ratio": entry_ratio
        })

        symbol, decimals = get_token_metadata(token_addr)
        token_value_display = (
            f"{token_out_est:.6f} {symbol}" if symbol else f"{token_out_est:.6f} tokens"
        )
        msg_achat = (
            f"▶️ [BUY] Whale-copytrade → {eth_amount:.6f} ETH → {token_addr}\n"
            f"    ~ {token_value_display}\n"
            f"    • Contrat : `{token_addr}`\n"
            f"    • Symbole : {symbol or '–'}\n"
            f"    • Décimales : {decimals}\n"
            f"    • TxHash : `{tx_hash}`\n"
            f"    • Slippage toléré : {SLIPPAGE_TOLERANCE * 100:.1f}%\n"
            f"    • Frais gaz : {frais_eth:.6f} ETH ({frais_usd:.2f} $)"
        )
        safe_send(msg_achat)

        return tx_hash

    except Exception as e:
        safe_send(f"🚨 Erreur BUY Uniswap : {e}")
        return None

# ─── 12) FONCTION DE VENTE (SELL) ─────────────────────────────────────────

def sell_all_token(token_address: str) -> str | None:
    global total_fees_spent_usd

    try:
        token_addr = Web3.to_checksum_address(token_address)
        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI_FULL)

        balance_token_wei = token_contract.functions.balanceOf(WALLET_ADDRESS).call()
        if balance_token_wei == 0:
            safe_send(f"⚠️ Pas de balance de {token_addr} à vendre.")
            return None

        prix_gaz = w3.eth.gas_price
        prix_gaz_gwei = w3.from_wei(prix_gaz, "gwei")
        if prix_gaz_gwei > MAX_GAS_GWEI:
            safe_send(f"⛔ GasPrice trop élevé ({prix_gaz_gwei:.1f} gwei). Vente annulée.")
            return None

        nonce = w3.eth.get_transaction_count(WALLET_ADDRESS)
        approve_txn = token_contract.functions.approve(
            UNISWAP_ROUTER_ADDRESS, balance_token_wei
        ).build_transaction({
            "from": WALLET_ADDRESS,
            "gas": 100_000,
            "gasPrice": w3.to_wei(min(prix_gaz_gwei, MAX_GAS_GWEI), "gwei"),
            "nonce": nonce,
            "chainId": CHAIN_ID
        })
        signed_approve = w3.eth.account.sign_transaction(approve_txn, private_key=PRIVATE_KEY)
        tx_hash_a = w3.eth.send_raw_transaction(signed_approve.raw_transaction).hex()

        try:
            receipt_a = w3.eth.wait_for_transaction_receipt(tx_hash_a, timeout=TX_RECEIPT_TIMEOUT)
            frais_eth_a = Decimal(receipt_a.gasUsed * receipt_a.effectiveGasPrice) / Decimal(10**18)
            frais_usd_a = (frais_eth_a * ETH_PRICE_USD).quantize(Decimal("0.01"))
            total_fees_spent_usd += frais_usd_a
        except Exception:
            safe_send(f"⏱ Timeout confirmation APPROVE {token_addr}.")

        safe_send(f"🔒 [APPROVE] {token_addr} → Router | Tx: {tx_hash_a}")

        path = [token_addr, WETH_ADDRESS]
        deadline = int(time.time()) + 300
        nonce2 = w3.eth.get_transaction_count(WALLET_ADDRESS)

        prix_gaz2 = w3.eth.gas_price
        prix_gaz2_gwei = w3.from_wei(prix_gaz2, "gwei")
        if prix_gaz2_gwei > MAX_GAS_GWEI:
            safe_send(f"⛔ GasPrice trop élevé ({prix_gaz2_gwei:.1f} gwei). Vente annulée.")
            return None

        swap_txn = router_contract.functions.swapExactTokensForETH(
            balance_token_wei,
            0,
            path,
            WALLET_ADDRESS,
            deadline
        ).build_transaction({
            "from": WALLET_ADDRESS,
            "gas": 300_000,
            "gasPrice": w3.to_wei(min(prix_gaz2_gwei, MAX_GAS_GWEI), "gwei"),
            "nonce": nonce2,
            "chainId": CHAIN_ID
        })

        signed_swap = w3.eth.account.sign_transaction(swap_txn, private_key=PRIVATE_KEY)
        tx_hash_s = w3.eth.send_raw_transaction(signed_swap.raw_transaction).hex()

        try:
            receipt_s = w3.eth.wait_for_transaction_receipt(tx_hash_s, timeout=TX_RECEIPT_TIMEOUT)
            frais_eth_s = Decimal(receipt_s.gasUsed * receipt_s.effectiveGasPrice) / Decimal(10**18)
            frais_usd_s = (frais_eth_s * ETH_PRICE_USD).quantize(Decimal("0.01"))
            total_fees_spent_usd += frais_usd_s
        except Exception:
            safe_send(f"⏱ Timeout confirmation SELL {token_addr}.")

        safe_send(f"🔻 [SELL] {token_addr} → Tx: {tx_hash_s}")
        return tx_hash_s

    except Exception as e:
        safe_send(f"🚨 Erreur SELL Uniswap : {e}")
        return None

# ─── 13) CHECK TP / SL (Take-Profit / Stop-Loss) ───────────────────────────

def check_positions_and_maybe_sell():
    global positions
    nouvelles_positions: list[dict] = []

    for pos in positions:
        token_addr    = pos["token"]
        token_amt     = pos["token_amount_wei"]
        entry_eth     = pos["entry_eth"]
        entry_ratio   = pos["entry_ratio"]

        if token_amt == 0:
            continue

        path_to_eth = [token_addr, WETH_ADDRESS]
        try:
            amounts_out = router_contract.functions.getAmountsOut(token_amt, path_to_eth).call()
            current_eth_value = Decimal(amounts_out[1]) / Decimal(10**18)
        except Exception as e:
            print(f"⚠️ Erreur getAmountsOut (check) pour {token_addr} : {e}")
            nouvelles_positions.append(pos)
            continue

        ratio = (current_eth_value / entry_eth).quantize(Decimal("0.0001"))

        if ratio >= (Decimal("1.0") + TP_THRESHOLD):
            safe_send(
                f"✅ TAKE-PROFIT → {token_addr}\n"
                f"   Valeur actuelle : {current_eth_value:.6f} ETH (+{(ratio-1)*100:.1f}%)."
            )
            sell_all_token(token_addr)

        elif ratio <= (Decimal("1.0") - SL_THRESHOLD):
            safe_send(
                f"⚠️ STOP-LOSS → {token_addr}\n"
                f"   Valeur actuelle : {current_eth_value:.6f} ETH (−{(1-ratio)*100:.1f}%)."
            )
            sell_all_token(token_addr)

        else:
            nouvelles_positions.append(pos)

    positions = nouvelles_positions

# ─── 14) FETCH TRANSACTIONS D’UNE WHALE VIA ETHERSCAN ─────────────────────

def fetch_etherscan_txns(whale: str, start_block: int) -> list[dict]:
    if not ETHERSCAN_API_KEY:
        return []
    url = "https://api.etherscan.io/api"
    params = {
        "module": "account",
        "action": "txlist",
        "address": whale,
        "startblock": start_block,
        "endblock": 99999999,
        "sort": "asc",
        "apikey": ETHERSCAN_API_KEY
    }
    try:
        res = requests.get(url, params=params, timeout=10).json()
        if res.get("status") == "1" and res.get("message") == "OK":
            return res.get("result", [])
        else:
            print(f"⚠️ Etherscan API returned status {res.get('status')} / message {res.get('message')}")
            return []
    except Exception as e:
        print(f"⚠️ Erreur HTTP Etherscan : {e}")
        return []

# ─── 15) BOUCLE PRINCIPALE (THREAD) ──────────────────────────────────────────

def main_loop():
    global trades_this_month, last_month_checked, stop_trading, total_fees_spent_usd, dernier_trade_timestamp

    next_summary_time = datetime.utcnow().replace(hour=18, minute=0, second=0, microsecond=0)
    if datetime.utcnow() >= next_summary_time:
        next_summary_time += timedelta(days=1)

    safe_send("🚀 Bot copytrade whales démarre.")

    while True:
        try:
            now = datetime.utcnow()

            if stop_trading:
                time.sleep(60)
                continue

            # Heartbeat horaire (minute = 0, seconde = 0)
            if now.minute == 0 and now.second == 0:
                safe_send(f"✅ Bot actif à {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")

            # Scruter patients Whales
            for whale in WHALES:
                start_block = last_processed_block.get(whale, 0) or 0
                txns = fetch_etherscan_txns(whale, start_block)
                for txn in txns:
                    block_number = int(txn.get("blockNumber", "0"))
                    if block_number <= start_block:
                        continue

                    to_addr = txn.get("to", "").lower()
                    input_data = txn.get("input", "")

                    if to_addr != UNISWAP_ROUTER_ADDRESS.lower():
                        continue

                    if est_uniswap_swap_exact_eth_for_tokens(input_data):
                        path = extract_path_from_input(input_data)
                        if len(path) < 2:
                            continue
                        token_bought = path[-1]
                        if not est_adresse_valide(token_bought):
                            continue
                        token_bought = Web3.to_checksum_address(token_bought)

                        eth_used_whale = Decimal(w3.from_wei(int(txn.get("value", "0")), "ether"))
                        if eth_used_whale < MIN_ETH_COPY:
                            safe_send(f"ℹ️ Whale a dépensé {eth_used_whale:.6f} ETH (<MIN_ETH_COPY). Ignoré.")
                            continue
                        if (eth_used_whale * ETH_PRICE_USD) < MIN_TOKEN_VOLUME_USD:
                            safe_send(f"ℹ️ Volume ${(eth_used_whale * ETH_PRICE_USD):.2f} < MIN_TOKEN_VOLUME_USD. Ignoré.")
                            continue

                        if not verifier_liquidite_minimale(token_bought):
                            safe_send(f"ℹ️ Pool WETH-{token_bought} < {MIN_LIQUIDITY_POOL_ETH} ETH. Ignoré.")
                            continue

                        if not verifier_gas_price():
                            safe_send("ℹ️ GasPrice > MAX_GAS_GWEI. Ignoré.")
                            continue

                        your_eth = min(eth_used_whale, ETH_PER_TRADE)
                        if your_eth == 0:
                            continue

                        now_ts = int(time.time())
                        if now_ts - dernier_trade_timestamp < COOLDOWN_TIME:
                            continue
                        dernier_trade_timestamp = now_ts

                        if trades_this_month < MAX_TRADES_PER_MONTH:
                            txh = buy_token(token_bought, your_eth)
                            if txh:
                                trades_this_month += 1
                        else:
                            safe_send("⚠️ Limite mensuelle atteinte, trade ignoré.")

                    last_processed_block[whale] = max(last_processed_block.get(whale, 0), block_number)

            # CHECK TP / SL
            check_positions_and_maybe_sell()

            # RÉSUMÉ QUOTIDIEN À 18h UTC
            if now >= next_summary_time:
                nb_pos = len(positions)
                trades_left = MAX_TRADES_PER_MONTH - trades_this_month
                eth_inv = (trades_this_month * ETH_PER_TRADE).quantize(Decimal("0.000001"))
                summary_msg = (
                    f"🧾 Résumé du {now.strftime('%Y-%m-%d')} :\n"
                    f"• Positions ouvertes : {nb_pos}\n"
                    f"• Trades restants : {trades_left}/{MAX_TRADES_PER_MONTH}\n"
                    f"• Total investi ce mois : {eth_inv:.6f} ETH\n"
                    f"• Frais gaz dépensés ce mois : {total_fees_spent_usd:.2f} $"
                )
                safe_send(summary_msg)
                next_summary_time += timedelta(days=1)

            # RAZ MENSUEL LE 1er JOUR DU MOIS
            if now.month != last_month_checked:
                trades_this_month = 0
                total_fees_spent_usd = Decimal("0")
                stop_trading = False
                last_month_checked = now.month

            time.sleep(30)

        except Exception as e:
            print(f"🔴 Erreur boucle principale : {e}")
            safe_send(f"❌ Erreur Bot : {e}")
            time.sleep(60)

# ─── 16) COMMANDE /portfolio (DÉTAIL DES POSITIONS) ───────────────────────

async def portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not positions:
        await update.message.reply_text("📭 Aucune position ouverte actuellement.")
        return

    msg = "📦 Récapitulatif des positions :\n\n"
    for pos in positions:
        tok = pos["token"]
        amt_wei = pos["token_amount_wei"]
        if amt_wei == 0:
            continue

        symbol, decimals = get_token_metadata(tok)
        amt = Decimal(amt_wei) / Decimal(10**(decimals if decimals else 18))

        try:
            path = [tok, WETH_ADDRESS]
            amounts_out = router_contract.functions.getAmountsOut(amt_wei, path).call()
            value_eth = Decimal(amounts_out[1]) / Decimal(10**18)
        except:
            value_eth = Decimal("0")

        msg += (
            f"• {symbol or 'TOKEN'} ({tok})\n"
            f"    • Quantité : {amt:.6f} {symbol or ''}\n"
            f"    • Valeur approx. : {value_eth:.6f} ETH\n"
            f"    • MetaMask : adresse={tok}, symbol={symbol}, décimales={decimals}\n\n"
        )

    await update.message.reply_text(msg)

# ─── 17) COMMANDE /status (STATUT BREF) ───────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_trades = len(positions)
    invested = sum(p["entry_eth"] for p in positions)
    msg = (
        f"📊 Statut actuel du bot :\n\n"
        f"🔁 Positions ouvertes : {total_trades}\n"
        f"💰 Investi : {invested:.6f} ETH\n"
    )
    if total_trades > 0:
        msg += "Pour voir le détail, tapez /portfolio\n"
    await update.message.reply_text(msg)

# ─── 18) POINT D’ENTRÉE PRINCIPAL ───────────────────────────────────────────

if __name__ == "__main__":
    # 18.a) Enregistrer les commandes Telegram
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("portfolio", portfolio))

    # 18.b) LANCER LA BOUCLE DE TRADING DANS UN THREAD SÉPARÉ
    threading.Thread(target=main_loop, daemon=True).start()

    # ─── 18.c) SUPPRIMER LE WEBHOOK S’IL EXISTE, AVANT PASSER EN POLLING
    # Sur version python-telegram-bot v20, delete_webhook est une coroutine
    # Donc on exécute cela dans l'event loop temporairement via asyncio.run(...)
    asyncio.run(application.bot.delete_webhook(drop_pending_updates=True))

    # 18.d) Lancer le Bot Telegram EN mode POLLING (écoute des commandes)
    application.run_polling()
