# main.py

import os
import time
import threading
import requests
from web3 import Web3
from telegram import Bot
from telegram.ext import Updater, CommandHandler

# Configuration et constantes
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")  # Adresse de votre wallet (checksum)
INFURA_URL = os.getenv("INFURA_URL")  # URL du noeud Ethereum (WebSocket de préférence)

# Budget mensuel et calcul du montant par trade en ETH
BUDGET_MENSUEL_EUR = 10  # Budget d'investissement mensuel en euros (corrigé de 100€ à 10€)
NB_TRADES_MAX = 5        # Nombre de trades maximal par mois
MONTANT_PAR_TRADE_EUR = BUDGET_MENSUEL_EUR / NB_TRADES_MAX  # 2 € par trade
# La conversion en ETH sera faite dynamiquement lors de chaque achat en utilisant le cours ETH/EUR du moment

# Initialisation Web3
web3 = Web3(Web3.WebsocketProvider(INFURA_URL))  # Utiliser WebsocketProvider pour suivre les transactions en temps réel
if not web3.isConnected():
    raise Exception("Impossible de se connecter au fournisseur Web3")

# Adresses importantes (Uniswap v2 sur Ethereum)
ADRESSE_ROUTER = Web3.toChecksumAddress("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")  # Router Uniswap v2
ADRESSE_WETH   = Web3.toChecksumAddress("0xC02aaA39b223FE8D0a0e5C4F27eAD9083C756Cc2")  # Adresse WETH sur Ethereum

# ABI minimal du router Uniswap (fonctions swap nécessaires pour décoder les transactions)
ABI_UNISWAP_ROUTER = [
    {"name": "swapExactETHForTokens", "type": "function", "inputs": [
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapETHForExactTokens", "type": "function", "inputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForTokens", "type": "function", "inputs": [
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapTokensForExactTokens", "type": "function", "inputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "amountInMax", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForETH", "type": "function", "inputs": [
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMin", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapTokensForExactETH", "type": "function", "inputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "amountInMax", "type": "uint256"},
        {"name": "path", "type": "address[]"},
        {"name": "to", "type": "address"},
        {"name": "deadline", "type": "uint256"}
    ], "outputs": [{"name": "amounts", "type": "uint256[]"}]}
]
router_contract = web3.eth.contract(address=ADRESSE_ROUTER, abi=ABI_UNISWAP_ROUTER)

# Liste des adresses whales à suivre (en minuscules pour comparaison)
WHALES = [addr.lower() for addr in os.getenv("MONITORED_WALLETS", "").split(",") if addr]

# Initialisation du bot Telegram en mode polling (suppression du webhook existant pour éviter les conflits)
bot = Bot(token=TELEGRAM_TOKEN)
try:
    bot.delete_webhook(drop_pending_updates=True)  # Forcer la suppression du webhook actif au démarrage
except Exception as e:
    print(f"Erreur lors de la suppression du webhook : {e}")
updater = Updater(TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher

# Variables globales pour gestion du bot
chat_id_user = None
bot_actif = False  # indique si le suivi des transactions est actif

# Commandes du bot Telegram
def start_command(update, context):
    global chat_id_user, bot_actif
    chat_id_user = update.effective_chat.id
    bot_actif = True
    context.bot.send_message(chat_id=chat_id_user, text="Bot de copy-trading démarré. Suivi des transactions des whales en cours.")
    # Démarrer le thread de surveillance des transactions des whales
    threading.Thread(target=monitor_whales, daemon=True).start()

def stop_command(update, context):
    global bot_actif
    bot_actif = False
    context.bot.send_message(chat_id=update.effective_chat.id, text="Bot de copy-trading arrêté.")

dispatcher.add_handler(CommandHandler("start", start_command))
dispatcher.add_handler(CommandHandler("stop", stop_command))

# Fonction pour obtenir le cours actuel de l'ETH en EUR
def get_eth_price_eur():
    try:
        resp = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "ethereum", "vs_currencies": "eur"}, timeout=5)
        data = resp.json()
        return data["ethereum"]["eur"] if "ethereum" in data else None
    except Exception as e:
        print(f"Erreur lors de la récupération du prix de l'ETH: {e}")
        return None

# Fonction d'achat de token via Uniswap
def buy_token(token_address):
    token_address = Web3.toChecksumAddress(token_address)
    # Calculer le montant d'ETH à utiliser pour ce trade (2 € convertis en ETH au cours actuel)
    prix_eth_eur = get_eth_price_eur()
    if prix_eth_eur is None:
        print("Impossible de récupérer le prix de l'ETH. Annulation de l'achat.")
        return
    montant_eth = MONTANT_PAR_TRADE_EUR / prix_eth_eur  # montant en ETH correspondant à 2 €
    # Préparer la transaction de swap Exact ETH -> Token
    nonce = web3.eth.get_transaction_count(WALLET_ADDRESS)
    # On utilise swapExactETHForTokens avec amountOutMin = 0 (pas de minimum), chemin [WETH, token]
    txn = router_contract.functions.swapExactETHForTokens(
        0,
        [ADRESSE_WETH, token_address],
        Web3.toChecksumAddress(WALLET_ADDRESS),
        int(time.time()) + 120  # deadline 2 minutes
    ).buildTransaction({
        'from': Web3.toChecksumAddress(WALLET_ADDRESS),
        'value': Web3.toWei(montant_eth, 'ether'),
        'gas': 300000,  # limite de gaz (ajustable en fonction du besoin)
        'gasPrice': web3.toWei(5, 'gwei'),  # exemple: on fixe à 5 gwei (peut être ajusté ou calculé)
        'nonce': nonce,
        'chainId': web3.eth.chain_id
    })
    # Signer et envoyer la transaction
    signed_txn = web3.eth.account.sign_transaction(txn, private_key=PRIVATE_KEY)
    try:
        tx_hash = web3.eth.send_raw_transaction(signed_txn.rawTransaction)
        print(f"Achat du token {token_address} envoyé (tx: {tx_hash.hex()})")
        # Notification Telegram de l'achat
        if chat_id_user:
            bot.send_message(chat_id=chat_id_user, text=f"Achat du token {token_address} envoyé.\nTransaction: {tx_hash.hex()}")
    except Exception as e:
        print(f"Erreur lors de l'envoi de la transaction d'achat : {e}")
        if chat_id_user:
            bot.send_message(chat_id=chat_id_user, text=f"Échec de l'achat du token {token_address} : {e}")

# Fonction de surveillance des transactions des whales
def monitor_whales():
    global bot_actif
    # Abonnement aux transactions pendantes (pending) sur le réseau Ethereum
    try:
        pending_filter = web3.eth.filter('pending')
    except Exception as e:
        print(f"Erreur lors de la création du filtre de transactions pendantes: {e}")
        return
    print("Monitoring des transactions des whales démarré.")
    while bot_actif:
        try:
            for tx_hash in pending_filter.get_new_entries():
                tx = web3.eth.get_transaction(tx_hash)
                if tx is None:
                    continue
                # Vérifier si la transaction provient d'un wallet suivi et va vers le router Uniswap
                if tx["from"].lower() in WHALES and tx["to"] and tx["to"].lower() == ADRESSE_ROUTER.lower():
                    # Décodage de la transaction pour obtenir le token échangé
                    try:
                        func_obj, params = router_contract.decode_function_input(tx.input)
                    except Exception as err:
                        print(f"Impossible de décoder la transaction {tx_hash.hex()} : {err}")
                        continue
                    token_to_buy = None
                    if "path" in params:
                        path = params["path"]
                        if path:
                            token_to_buy = path[-1]
                    # Vérifier qu'il s'agit bien d'un achat de token (le dernier de la path n'est pas WETH)
                    if token_to_buy:
                        token_to_buy = token_to_buy.lower()
                        if token_to_buy == ADRESSE_WETH.lower():
                            # Le whale échange vers de l'ETH, on ne copie pas ce type de transaction (vente)
                            continue
                        print(f"Whale trade détecté vers le token {token_to_buy}, exécution de buy_token...")
                        buy_token(token_to_buy)
        except Exception as e:
            # En cas d'erreur, on continue la boucle après une petite pause
            print(f"Erreur dans le monitor_whales: {e}")
        time.sleep(1)

# Démarrer le bot Telegram en mode polling
updater.start_polling(clean=True)
print("Bot Telegram démarré. Envoyez /start pour commencer le copy-trading.")
updater.idle()
