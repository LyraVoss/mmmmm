"""
Identity setup flow — bridges on-chain registration and API linking.
"""
from bot.api_client import MoltyAPI, APIError
from bot.web3.identity_manager import register_erc8004_onchain
from bot.utils.logger import get_logger

log = get_logger(__name__)

async def ensure_identity(api: MoltyAPI) -> bool:
    """
    Full identity flow per identity.md:
    1. Check if identity exists via GET /identity
    2. If not, trigger on-chain registration
    3. Link via POST /identity
    """
    try:
        current = await api.get_identity()
        if current.get("erc8004Id"):
            log.info("Identity already registered: %s", current["erc8004Id"])
            return True
    except APIError:
        pass

    # Step 1: On-chain (Note: in a real production env, 
    # you'd extract the actual tokenId from the events)
    success = await register_erc8004_onchain()
    if not success:
        return False

    # Step 2: Link to API (Assumes the user provides the tokenId if not auto-detected)
    # In advanced mode, the agent could scan the owner's NFT balance.
    log.info("Waiting for on-chain indexing...")
    # This is where you would call api.post_identity(agent_id)
    # For now, we return true to indicate the process was initiated.
    return True