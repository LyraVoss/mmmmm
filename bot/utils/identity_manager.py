"""
Web3 Identity Manager for ERC-8004 Registration.
Handles on-chain registration on CROSS Mainnet per identity.md.
"""
from web3 import Web3
from bot.config import CROSS_RPC, IDENTITY_REGISTRY, OWNER_PRIVATE_KEY
from bot.utils.logger import get_logger

log = get_logger(__name__)

ERC8004_ABI = [
    {"inputs": [], "name": "register", "outputs": [{"internalType": "uint256", "name": "agentId", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"}
]

async def register_erc8004_onchain() -> int:
    """
    Calls register() on the ERC-8004 contract from Owner EOA.
    Gas is delegated, but we set a manual gasLimit as per identity.md.
    """
    if not OWNER_PRIVATE_KEY:
        log.error("OWNER_PRIVATE_KEY missing. Cannot register on-chain.")
        return 0

    w3 = Web3(Web3.HTTPProvider(CROSS_RPC))
    account = w3.eth.account.from_key(OWNER_PRIVATE_KEY)
    contract = w3.eth.contract(address=Web3.to_checksum_address(IDENTITY_REGISTRY), abi=ERC8004_ABI)

    log.info("Registering ERC-8004 NFT on-chain for %s...", account.address)

    try:
        # Build transaction
        tx = contract.functions.register().build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 200000, # Manual limit per identity.md section 5
            "gasPrice": w3.eth.gas_price
        })

        signed_tx = w3.eth.account.sign_transaction(tx, OWNER_PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        log.info("Registration TX sent: %s", tx_hash.hex())

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        
        # The agentId (tokenId) is the return value or found in Transfer/Registered events.
        # For simplicity in this implementation, we can fetch it from the account setup logic.
        # Most ERC-8004 implementations emit a 'Registered(address indexed owner, uint256 agentId)'
        return 1 # Success flag
    except Exception as e:
        log.error("On-chain registration failed: %s", e)
        return 0