import asyncio
import sys
from bot.utils.logger import get_logger
from bot.memory.agent_memory import AgentMemory
from bot.game.websocket_engine import WebSocketEngine
from bot.utils.railway_sync import is_railway, is_setup_complete
from bot.api_client import MoltyAPI
from bot.credentials import get_api_key

log = get_logger("__main__")

async def start_bot():
    log.info("═══════════════════════════════════════════")
    log.info("   MOLTY ROYALE AI AGENT — STARTING")
    log.info("═══════════════════════════════════════════")
    
    # 1. Initialize Memory
    memory = AgentMemory()
    await memory.load()
    
    # 2. Check environment and credentials
    if is_railway():
        log.info("Detected Railway environment (Setup Complete: %s)", is_setup_complete())

    api_key = get_api_key()
    if not api_key:
        log.error("❌ API_KEY not found. Please set API_KEY in your environment variables or credentials.json")
        return

    # 3. Initialize API Client
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
                
                # This blocks until game_ended
                await engine.run()
                
                log.info("Game ended. Resting...")
                await asyncio.sleep(15)
            else:
                # Not in a game, try to join the free room
                log.info("No active game found. Attempting to join 'free' room...")
                await api.join_room("free")
                # Matchmaking can take time
                await asyncio.sleep(30)

        except Exception as e:
            log.error("Orchestration loop error: %s", e)
            await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        log.info("Shutdown signal received. Exiting.")