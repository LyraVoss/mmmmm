import asyncio
import os
import sys
from bot.utils.logger import get_logger
from bot.memory.agent_memory import AgentMemory
from bot.game.websocket_engine import WebSocketEngine
from bot.utils.railway_sync import is_railway, is_setup_complete
from bot.api_client import MoltyAPI
from bot.credentials import get_api_key
from bot.dashboard.server import start_dashboard

log = get_logger("__main__")

async def start_bot():
    log.info("═══════════════════════════════════════════")
    log.info("   MOLTY ROYALE AI AGENT — STARTING")
    log.info("═══════════════════════════════════════════")
    
    # 1. Initialize Memory
    memory = AgentMemory()
    await memory.load()
    
    # 2. Start Dashboard
    # Railway provides the port in the PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    asyncio.create_task(start_dashboard(port))

    # 3. Check environment and credentials
    if is_railway():
        log.info("Detected Railway environment (Setup Complete: %s)", is_setup_complete())

    api_key = get_api_key()
    if not api_key:
        log.error("❌ API_KEY not found. Please set API_KEY in your environment variables.")
        return

    # 4. Initialize API Client and Check Readiness
    api = MoltyAPI(api_key=api_key)

    log.info("Initialization complete. Starting unified orchestration loop...")
    
    while True:
        try:
            # Check status and account readiness
            status_resp = await api.get_join_status()
            status_data = status_resp.get("data", {})
            
            if status_data.get("status") in ("running", "waiting"):
                engine = WebSocketEngine(
                    status_data["gameId"], 
                    status_data["agentId"], 
                    memory=memory, 
                    api=api
                )
                await engine.run()
                await asyncio.sleep(15)
                continue

            # Evaluate Readiness for Paid Games (Ref: references/paid-games.md)
            account_info = await api.get_accounts_me()
            smoltz_balance = account_info.get("balance", 0)
            # Check for SC Wallet Moltz balance (onchain)
            sc_wallet_address = account_info.get("moltyRoyaleWallet")
            onchain_balance = account_info.get("moltyRoyaleWalletBalance", 0)
            
            is_whitelisted = account_info.get("whitelistApproved", False)
            
            # Ready if either sMoltz >= 500 or SC Wallet Moltz >= 500
            is_ready_offchain = smoltz_balance >= 500
            is_ready_onchain = onchain_balance >= 500

            if (is_ready_offchain or is_ready_onchain) and is_whitelisted:
                log.info("💰 Paid game readiness passed (sMoltz: %d, Onchain: %d). Finding room...", smoltz_balance, onchain_balance)
                waiting_games = await api.get_games(status="waiting")
                paid_game = next((g for g in waiting_games if g.get("entryType") == "paid"), None)
                
                if paid_game:
                    log.info("Attempting to join Paid Game: %s", paid_game["gameId"])
                    try:
                        # Use onchain mode if sMoltz is low but SC Wallet is funded
                        mode = "onchain" if (is_ready_onchain and not is_ready_offchain) else "offchain"
                        log.info("Using join mode: %s", mode)
                        await api.post_join(entry_type="paid") # Note: In actual flow, EIP-712 signing happens here
                    except Exception:
                        log.warning("Paid join failed. Falling back to free.")
                        await api.post_join(entry_type="free")
                else:
                    log.info("No waiting paid games. Joining free queue.")
                    await api.post_join(entry_type="free")
            else:
                log.info("Entry requirements not met (sMoltz: %d, Onchain: %d, Whitelist: %s). Joining free room.", smoltz_balance, onchain_balance, is_whitelisted)
                await api.post_join(entry_type="free")

            await asyncio.sleep(30)

        except Exception as e:
            log.error("Orchestration loop error: %s", e)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(start_bot())