import asyncio
import sys
from bot.utils.logger import get_logger
from bot.memory.agent_memory import AgentMemory
from bot.utils.railway_sync import is_railway

log = get_logger("__main__")

async def start_bot():
    log.info("═══════════════════════════════════════════")
    log.info("   MOLTY ROYALE AI AGENT — STARTING")
    log.info("═══════════════════════════════════════════")
    
    # 1. Initialize Memory
    memory = AgentMemory()
    await memory.load()
    
    # 2. Check environment
    if is_railway():
        log.info("Detected Railway environment.")

    log.info("Initialization complete. Starting heartbeat and WebSocket engines...")
    # Here you would typically call your orchestrator or heartbeat loop.
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        log.info("Shutdown signal received.")
