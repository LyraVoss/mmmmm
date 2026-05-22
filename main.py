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

    # 4. Initialize API Client
    api = MoltyAPI()

    log.info("Initialization complete. Starting orchestration loop...")
    
    while True:
        try:
            # Check current join status
            status_resp = await api.get_join_status()
            status_data = status_resp.get("data", {})
            status = status_data.get("status")

            if status in ("running", "waiting"):
                game_id = status_data.get("gameId")
                agent_id = status_data.get("agentId")
                
                log.info("Entering Game Session: %s", game_id)
                engine = WebSocketEngine(game_id, agent_id, memory=memory, api=api)
                
                # Blocks until game_ended
                await engine.run()
                log.info("Game session finished. Checking for next match in 15s...")
                await asyncio.sleep(15)
            else:
                log.info("Idle. Attempting to join 'free' room matchmaking...")
                await api.join_room("free")
                await asyncio.sleep(30)

        except Exception as e:
            log.error("Orchestration loop error: %s", e)
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(start_bot())