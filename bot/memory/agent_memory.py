"""
Agent memory — persistent cross-game learning via molty-royale-context.json.
Two sections: `overall` (persistent) and `temp` (per-game).

v1.6.0: Persistent memory via Railway Variables API — lessons tidak hilang saat redeploy!
Memory disimpen di 2 tempat:
1. File lokal (temp, hilang saat redeploy)
2. Railway Variables BOT_MEMORY (permanen, sync setiap game selesai)
"""
import json
import os
import urllib.request
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from pathlib import Path
from typing import Optional
from bot.config import MEMORY_DIR, MEMORY_FILE, MONGODB_URI, MAX_LESSONS_TO_REMEMBER
from bot.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_MEMORY = {
    "overall": {
        "identity": {"name": "", "playstyle": "adaptive guardian hunter"},
        "strategy": {
            "deathzone": "move inward before turn 5",
            "guardians": "engage immediately — highest sMoltz value",
            "weather": "avoid combat in fog or storm",
            "ep_management": "rest when EP < 4 before engaging",
        },
        "history": {
            "totalGames": 0,
            "wins": 0,
            "avgKills": 0.0,
            "lessons": [],
            "suggestions": [],
        },
    },
    "temp": {},
}


class AgentMemory:
    """Read/write molty-royale-context.json with overall + temp sections."""

    def __init__(self):
        self.data = dict(DEFAULT_MEMORY)
        self._loaded = False
        self._db_client = None
        self._collection = None

        if MONGODB_URI:
            try:
                self._db_client = AsyncIOMotorClient(MONGODB_URI)
                self._collection = self._db_client["openclaw"]["agent_memory"]
            except Exception as e:
                log.warning("MongoDB init failed: %s", e)

    async def load(self):
        """Load memory: MongoDB is primary. Local disk is secondary/backup."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        # HARDCODED PRIORITY: Check MongoDB first to prevent "Starting fresh" logs on redeploy
        if self._collection is not None:
            db_loaded = await self.load_from_mongodb()
            if db_loaded:
                self._loaded = True
                return

        if MEMORY_FILE.exists():
            raw = MEMORY_FILE.read_text(encoding="utf-8")
            self.data = json.loads(raw)
            self._loaded = True
            log.info("Memory loaded from local disk backup")
        else:
            log.info("No memory found in DB or Disk — initializing fresh brain")

    async def save(self):
        """Persist memory to MongoDB primary and local disk fallback."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)

        # Priority 1: MongoDB Sync
        if self._collection is not None:
            await self.sync_to_mongodb()
            # We still write to disk as a local cache/fallback

        MEMORY_FILE.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.debug("Memory saved to %s", MEMORY_FILE)
        # HARDCODE: sync_to_railway is disabled. It causes infinite redeploy loops.
        # MongoDB is now the sole source of persistent memory.
        # await self.sync_to_railway()

    def set_agent_name(self, name: str):
        self.data["overall"]["identity"]["name"] = name

    def get_strategy(self) -> dict:
        return self.data.get("overall", {}).get("strategy", {})

    def get_lessons(self) -> list:
        return self.data.get("overall", {}).get("history", {}).get("lessons", [])

    def get_suggestions(self) -> list:
        return self.data.get("overall", {}).get("history", {}).get("suggestions", [])

    # ── Temp (per-game) ───────────────────────────────────────────────

    def set_temp_game(self, game_id: str):
        self.data["temp"] = {
            "gameId": game_id,
            "currentStrategy": "adaptive",
            "knownAgents": [],
            "notes": "",
        }

    def update_temp_note(self, note: str):
        if "temp" not in self.data:
            self.data["temp"] = {}
        existing = self.data["temp"].get("notes", "")
        self.data["temp"]["notes"] = f"{existing}\n{note}".strip()

    def clear_temp(self):
        self.data["temp"] = {}

    # ── History update (after game end) ───────────────────────────────

    def record_game_end(self, is_winner: bool, final_rank: int,
                        kills: int, smoltz_earned: int = 0):
        history = self.data["overall"]["history"]
        history["totalGames"] += 1
        if is_winner:
            history["wins"] += 1

        # Rolling average kills
        total = history["totalGames"]
        old_avg = history["avgKills"]
        history["avgKills"] = round(((old_avg * (total - 1)) + kills) / total, 2)

    def add_lesson(self, lesson: dict, max_lessons: int = MAX_LESSONS_TO_REMEMBER):
        """Append a new lesson, keeping max_lessons most recent."""
        lessons = self.data["overall"]["history"]["lessons"]
        # Check if a similar lesson already exists to avoid duplicates
        # For structured lessons, this might involve a more complex comparison
        if not any(l.get("reason") == lesson.get("reason") for l in lessons):
            lessons.append({
                "timestamp": datetime.now().isoformat(),
                **lesson
            })
            if len(lessons) > max_lessons:
                lessons.pop(0)

    # ── MongoDB persistence (v1.7.1) ────────────────────────────────

    async def sync_to_mongodb(self):
        """Upsert the overall memory state to MongoDB cluster."""
        if self._collection is None:
            return
        try:
            # Singleton document for the agent's brain
            await self._collection.replace_one(
                {"_id": "mymm_brain_v1"},
                { # Store the entire overall section
                    "overall": self.data["overall"], 
                    "last_updated": datetime.now(timezone.utc).isoformat()
                },
                upsert=True
            )
            log.info("✅ Brain synced to MongoDB cluster")
        except Exception as e:
            log.warning("MongoDB sync failed: %s", e)

    async def load_from_mongodb(self) -> bool:
        """Restore the agent's brain from the MongoDB cluster."""
        if self._collection is None:
            return False
        doc = await self._collection.find_one({"_id": "mymm_brain_v1"})
        if doc and "overall" in doc:
            # Deep merge overall data, prioritizing MongoDB for history/lessons/suggestions
            self.data["overall"]["identity"].update(doc["overall"].get("identity", {}))
            self.data["overall"]["strategy"].update(doc["overall"].get("strategy", {}))
            self.data["overall"]["history"].update(doc["overall"].get("history", {}))
            # Ensure lessons are merged and deduplicated if both local and DB have them
            self.data["overall"]["history"]["lessons"] = list(dict.fromkeys(self.data["overall"]["history"]["lessons"] + doc["overall"]["history"].get("lessons", [])))[-MAX_LESSONS_TO_REMEMBER:]
            log.info("✅ Brain restored from MongoDB cluster")
            return True
        return False
