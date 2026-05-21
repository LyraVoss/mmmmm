"""
WebSocket gameplay engine — wss://cdn.moltyroyale.com/ws/agent.
Core loop: connect → process messages → decide → act → repeat.

v1.5.3 fixes:
- can_act_changed: skip _on_agent_view if agent is dead (was triggering decide_action while dead)
- Map detection: track map usage from action sent (not from server ack string)
- Pass lessons from AgentMemory to decide_action for cross-game learning

Per game-loop.md:
- agent_view uses 'view' key (NOT 'data')
- turn_advanced includes full 'view' snapshot — MUST be processed
- action envelope: { type: "action", data: { type: "ACTION_TYPE", ... }, thought: {...} }
- action_result: includes canAct + cooldownRemainingMs at TOP LEVEL
- can_act_changed: canAct at TOP LEVEL (not nested in data)
- Only one WS session per API key
"""
import json
import asyncio
import aiohttp
import websockets
import random
from bot.config import WS_URL, SKILL_VERSION
from bot.credentials import get_api_key
from bot.game.action_sender import ActionSender, COOLDOWN_ACTIONS, FREE_ACTIONS
from bot.strategy.brain import decide_action, reset_game_state, learn_from_map, mark_item_picked_up
from bot.dashboard.state import dashboard_state
from bot.utils.rate_limiter import ws_limiter
from bot.utils.logger import get_logger

log = get_logger(__name__)


def _update_dz_knowledge(view: dict):
    """Continuously track death zones from every agent_view."""
    from bot.strategy.brain import _map_knowledge
    # Initialize risk scores if missing
    risk_scores = _map_knowledge.setdefault("risk_scores", {})
    
    for region in view.get("visibleRegions", []):
        if isinstance(region, dict) and region.get("isDeathZone"):
            rid = region.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
                risk_scores[rid] = 1.0 # Absolute danger
        elif isinstance(region, dict) and region.get("isDeathZonePending"):
            rid = region.get("id", "")
            if rid:
                risk_scores[rid] = 0.8 # High danger incoming

    for conn in view.get("connectedRegions", []):
        if isinstance(conn, dict) and conn.get("isDeathZone"):
            rid = conn.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
                risk_scores[rid] = 1.0

    cur = view.get("currentRegion", {})
    if isinstance(cur, dict) and cur.get("isDeathZone"):
        rid = cur.get("id", "")
        if rid:
            _map_knowledge["death_zones"].add(rid)
            risk_scores[rid] = 1.0

    for dz in view.get("pendingDeathzones", []):
        if isinstance(dz, dict):
            rid = dz.get("id", "")
            if rid:
                _map_knowledge["death_zones"].add(rid)
                risk_scores[rid] = 0.8
        elif isinstance(dz, str):
            _map_knowledge["death_zones"].add(dz)


class WebSocketEngine:
    """Manages the gameplay WebSocket session."""

    def __init__(self, game_id: str, agent_id: str, memory=None, api=None):
        self.game_id = game_id
        self.agent_id = agent_id
        self.memory = memory  # AgentMemory instance — NEW v1.5.3
        self.api = api
        self.action_sender = ActionSender()
        self.ws = None
        self.game_result = None
        self.last_view = None
        self._ping_task = None
        self._running = False
        # FIX v1.5.3: track map usage from outgoing action (not server ack string)
        self._map_item_id_pending = None
        self.dashboard_key = agent_id
        self.dashboard_name = "Agent"
        # FIX v1.5.6: track last action for pickup dedup
        self._last_action_type = None
        self._last_action_item_id = None
        self._last_kills = None
        self.moltbook_api_key = None  # Storage for custom MoltBook key

    async def run(self) -> dict:
        """Main gameplay loop. Returns game result dict."""
        api_key = get_api_key()
        headers = {
            "X-API-Key": api_key,
            "X-Version": SKILL_VERSION,
        }

        self._running = True
        retry_count = 0
        max_retries = 5

        while self._running and retry_count < max_retries:
            try:
                log.info("Connecting WebSocket to %s...", WS_URL)
                async with websockets.connect(
                    WS_URL,
                    additional_headers=headers,
                    ping_interval=None,
                    max_size=2**20,
                ) as ws:
                    self.ws = ws
                    retry_count = 0
                    log.info("✅ WebSocket connected for game=%s", self.game_id)

                    self._ping_task = asyncio.create_task(self._ping_loop())

                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            if not isinstance(msg, dict):
                                continue
                            result = await self._handle_message(msg)
                            if result is not None:
                                self._running = False
                                return result
                        except json.JSONDecodeError:
                            log.warning("Non-JSON message: %s", raw_msg[:100])

            except websockets.exceptions.ConnectionClosed as e:
                retry_count += 1
                log.warning("WebSocket closed: code=%s reason=%s (retry %d/%d)",
                            e.code, e.reason, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

            except Exception as e:
                retry_count += 1
                log.error("WebSocket error: %s (retry %d/%d)", e, retry_count, max_retries)
                if self._ping_task:
                    self._ping_task.cancel()
                await asyncio.sleep(min(2 ** retry_count, 30))

        return self.game_result or {"status": "disconnected"}

    async def _handle_message(self, msg: dict) -> dict | None:
        """Process a single WebSocket message. Returns game result or None."""
        msg_type = msg.get("type", "")

        # ── agent_view ────────────────────────────────────────────────
        if msg_type == "agent_view":
            view = msg.get("view") or msg.get("data") or {}
            if isinstance(view, dict) and view:
                self.last_view = view
                reason = msg.get("reason", "initial")
                # Check for death and exit early if requested
                if not view.get("self", {}).get("isAlive", True):
                    log.info("☠️ Agent death detected. Waiting for 'game_ended' message.")
                    # Do not exit early; continue processing messages to wait for the official 'game_ended' event.
                
                await self._on_agent_view(view)
            else:
                log.warning("agent_view with empty/invalid view: %s", str(view)[:100])

        # ── action_result ─────────────────────────────────────────────
        elif msg_type == "action_result":
            success = msg.get("success", False)
            self.action_sender.can_act = msg.get("canAct", self.action_sender.can_act)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)

            if success:
                data = msg.get("data", {})
                action_msg = data.get("message", "") if isinstance(data, dict) else str(data)
                log.info("Action OK: %s (canAct=%s)", action_msg, msg.get("canAct"))

                # FIX v1.5.6: mark item as picked up so brain won't double-pickup on stale view
                if self._last_action_type == "pickup" and self._last_action_item_id:
                    mark_item_picked_up(self._last_action_item_id)
                    self._last_action_item_id = None

                # FIX v1.5.3: detect map usage by matching itemId we sent (not server string)
                if self._map_item_id_pending:
                    log.info("🗺️ Map use confirmed by server — will learn on next view")
                    # _map_just_used flag will be set; learning happens in _on_agent_view
                    self._map_just_used = True
                    self._map_item_id_pending = None
            else:
                err = msg.get("error", {})
                err_code = err.get("code", "") if isinstance(err, dict) else str(err)
                err_msg = err.get("message", "") if isinstance(err, dict) else ""
                log.warning("Action FAILED: %s — %s", err_code, err_msg)
                # Clear pending map if action failed
                self._map_item_id_pending = None

        # ── can_act_changed ───────────────────────────────────────────
        elif msg_type == "can_act_changed":
            self.action_sender.can_act = msg.get("canAct", True)
            self.action_sender.cooldown_remaining_ms = msg.get("cooldownRemainingMs", 0)
            log.info("can_act_changed: canAct=%s", msg.get("canAct"))

            # FIX v1.5.3: only re-evaluate if agent is alive
            if self.last_view and msg.get("canAct"):
                self_data = self.last_view.get("self", {})
                if self_data.get("isAlive", True):
                    await self._on_agent_view(self.last_view)
                else:
                    log.debug("can_act_changed ignored — agent is dead")

        # ── turn_advanced ─────────────────────────────────────────────
        elif msg_type == "turn_advanced":
            turn_num = msg.get("turn", "?")
            view = msg.get("view")
            if not view and isinstance(msg.get("data"), dict):
                view = msg["data"].get("view")
                turn_num = msg["data"].get("turn", turn_num)

            log.info("Turn %s — processing view...", turn_num)
            if view and isinstance(view, dict):
                self.last_view = view
                await self._on_agent_view(view)
            elif self.last_view:
                await self._on_agent_view(self.last_view)
            else:
                log.warning("Turn advanced but no view data available")

        # ── game_ended ────────────────────────────────────────────────
        elif msg_type == "game_ended":
            log.info("═══ GAME ENDED ═══")
            reset_game_state()
            self.game_result = msg
            return msg

        # ── event ─────────────────────────────────────────────────────
        elif msg_type == "event":
            event_type = msg.get("eventType", msg.get("data", {}).get("eventType", ""))
            log.debug("Event: %s", event_type)

        # ── waiting ───────────────────────────────────────────────────
        elif msg_type == "waiting":
            log.info("Game is waiting for players...")

        # ── pong ──────────────────────────────────────────────────────
        elif msg_type == "pong":
            pass

        # ── error ─────────────────────────────────────────────────────
        elif msg_type == "error":
            err_msg = msg.get("message", msg.get("data", {}).get("message", str(msg)))
            log.error("Server error: %s", err_msg)

        else:
            log.info("Unknown WS message type=%s keys=%s", msg_type, list(msg.keys()))

        return None

    async def _on_agent_view(self, view: dict):
        """Process agent_view → decide action → send if appropriate."""
        if not isinstance(view, dict):
            return

        self_data = view.get("self", {})
        if not isinstance(self_data, dict):
            return

        alive_count = view.get("aliveCount", "?")

        if not self_data.get("isAlive", True):
            log.info("☠️ Agent DEAD — Alive remaining: %s. Waiting for game_ended...", alive_count)
            dk = self.dashboard_key
            dashboard_state.update_agent(dk, {
                "name": self.dashboard_name,
                "status": "dead",
                "hp": 0,
                "ep": 0,
                "maxHp": self_data.get("maxHp", 100),
                "maxEp": self_data.get("maxEp", 10),
                "alive_count": alive_count,
                "last_action": "☠️ DEAD — waiting for game to end",
                "enemies": [],
                "region_items": [],
            })
            dashboard_state.add_log(
                f"☠️ Agent DEAD — Alive remaining: {alive_count}",
                "warning", dk
            )
            return

        # ── Check for Manual User Messages (Dashboard Input) ──
        user_msg = dashboard_state.pop_user_message(self.dashboard_key)
        if user_msg:
            if user_msg.startswith("/"):
                await self._handle_dashboard_command(user_msg)
                return

            log.info("💬 User message received from dashboard: %s", user_msg)
            payload = self.action_sender.talk(user_msg)
            await self._send(payload)
            dashboard_state.add_log(f"Sent manual message: {user_msg}", "info", self.dashboard_key)
            # We return early so the next turn/update handles strategic movement
            return

        # NEW: Track kills to trigger taunts
        current_kills = self_data.get("kills", 0)
        if self._last_kills is not None and current_kills > self._last_kills:
            await self._trigger_kill_taunt()
        self._last_kills = current_kills

        hp = self_data.get("hp", "?")
        ep = self_data.get("ep", "?")
        region = view.get("currentRegion", {})
        region_name = region.get("name", "?") if isinstance(region, dict) else "?"
        log.info("Status: HP=%s EP=%s Region=%s | Alive: %s", hp, ep, region_name, alive_count)
        dashboard_state.add_log(
            f"HP={hp} EP={ep} Region={region_name} | Alive: {alive_count}",
            "info", self.dashboard_key
        )

        inv = self_data.get("inventory", [])
        enemies = [a for a in view.get("visibleAgents", [])
                   if isinstance(a, dict) and a.get("isAlive") and a.get("id") != self_data.get("id")]

        region_id = region.get("id", "") if isinstance(region, dict) else ""

        def _unwrap_items(raw_items):
            result = []
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("item")
                if isinstance(inner, dict):
                    inner["regionId"] = entry.get("regionId", "")
                    result.append(inner)
                elif entry.get("id"):
                    result.append(entry)
            return result

        region_items = []
        if isinstance(region, dict) and region.get("items"):
            region_items = _unwrap_items(region["items"])
        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            region_items = [i for i in all_visible if i.get("regionId") == region_id]
        if not region_items:
            all_visible = _unwrap_items(view.get("visibleItems", []))
            if all_visible:
                region_items = all_visible

        equipped = self_data.get("equippedWeapon")
        weapon_name = "fist"
        weapon_bonus = 0
        if equipped and isinstance(equipped, dict):
            weapon_name = equipped.get("typeId", "fist")
            from bot.strategy.brain import WEAPONS
            weapon_bonus = WEAPONS.get(weapon_name.lower(), {}).get("bonus", 0)

        def _item_label(i):
            return (i.get("name") or i.get("typeId") or i.get("type")
                    or i.get("itemType") or i.get("label") or str(i.get("id", "?"))[:12])

        def _item_cat(i):
            return (i.get("category") or i.get("cat") or i.get("itemCategory")
                    or i.get("type") or "")

        dk = self.dashboard_key
        dashboard_state.update_agent(dk, {
            "name": self.dashboard_name,
            "hp": hp, "ep": ep,
            "status": "playing",
            "maxHp": self_data.get("maxHp", 100),
            "maxEp": self_data.get("maxEp", 10),
            "atk": self_data.get("atk", 0),
            "def": self_data.get("def", 0),
            "weapon": weapon_name,
            "weapon_bonus": weapon_bonus,
            "kills": self_data.get("kills", 0),
            "region": region_name,
            "alive_count": alive_count,
            "inventory": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                          for i in inv if isinstance(i, dict)],
            "enemies": [{"name": e.get("name","?"), "hp": e.get("hp","?"), "id": e.get("id","")}
                        for e in enemies[:8]],
            "region_items": [{"typeId": i.get("typeId","?"), "name": _item_label(i), "cat": _item_cat(i)}
                             for i in region_items[:10]],
        })

        # Map learning
        if getattr(self, '_map_just_used', False):
            self._map_just_used = False
            learn_from_map(view)
            log.info("🗺️ Map knowledge updated — DZ tracking active")

        _update_dz_knowledge(view)

        # NEW v1.5.3: get lessons from memory for adaptive brain
        lessons = []
        if self.memory:
            lessons = self.memory.get_lessons()

        can_act = self.action_sender.can_send_cooldown_action()
        # Inject turn number for Day/Night awareness
        view["turn"] = getattr(self, "last_turn_num", 1)
        decision = decide_action(view, can_act, lessons=lessons)

        if decision is None:
            return

        action_type = decision["action"]
        action_data = decision.get("data", {})
        reason = decision.get("reason", "")

        if action_type in COOLDOWN_ACTIONS and not can_act:
            log.debug("Cooldown active — skipping %s", action_type)
            return

        # FIX v1.5.3: track map item ID before sending so we can confirm on action_result
        if action_type == "use_item":
            item_id = action_data.get("itemId", "")
            # Check if this item is a map
            for item in inv:
                if isinstance(item, dict) and item.get("id") == item_id:
                    if item.get("typeId", "").lower() == "map":
                        self._map_item_id_pending = item_id
                        log.debug("Map use pending confirmation: itemId=%s", item_id[:8])
                    break

        # FIX v1.5.6: remember what we're about to send for pickup dedup
        self._last_action_type = action_type
        self._last_action_item_id = action_data.get("itemId") if action_type == "pickup" else None

        payload = self.action_sender.build_action(
            action_type, action_data, reason, action_type,
        )

        await self._send(payload)
        log.info("→ %s | %s", action_type.upper(), reason)

        dashboard_state.update_agent(self.dashboard_key, {"last_action": f"{action_type}: {reason[:60]}"})
        dashboard_state.add_log(f"{action_type}: {reason[:80]}", "info", self.dashboard_key)

    async def _handle_dashboard_command(self, cmd_str: str):
        """Parses and executes administrative commands from the dashboard."""
        parts = cmd_str.strip().split(" ", 1)
        cmd = parts[0][1:].lower()  # Remove '/' and lowercase
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "moltbook_key":
            self.moltbook_api_key = args
            log.info("🔑 MoltBook API key updated via dashboard.")
            dashboard_state.add_log("MoltBook API key updated.", "success", self.dashboard_key)
        
        elif cmd == "stake":
            # Placeholder for future staking logic
            log.info("🏦 Staking command received. Logic not yet implemented.")
            dashboard_state.add_log("Staking action triggered (not yet implemented).", "warning", self.dashboard_key)
        
        elif cmd == "whisper":
            # /whisper <target_id> <message>
            sub_parts = args.split(" ", 1)
            if len(sub_parts) == 2:
                target_id, message = sub_parts
                payload = self.action_sender.whisper(target_id, message)
                await self._send(payload)
                log.info("🤫 Manual whisper sent to %s", target_id)
                dashboard_state.add_log(f"Manual whisper to {target_id[:8]}", "info", self.dashboard_key)
            else:
                dashboard_state.add_log("Usage: /whisper <target_id> <message>", "error", self.dashboard_key)
        
        else:
            log.warning("Unknown dashboard command: %s", cmd)
            dashboard_state.add_log(f"Unknown command: /{cmd}", "error", self.dashboard_key)

    async def _trigger_kill_taunt(self):
        """Sends a friendly, sarcastic global taunt when a kill is detected."""
        """Sends a friendly, sarcastic taunt to both broadcast and MoltBook."""
        taunts = [
            "Oh my! You're taking such a lovely little nap! Rest up, superstar! ✨💖",
            "Oopsie-daisy! You tripped into the lobby! See you in the next one, friend! 🌸🌈",
            "Wow! Your transformation into loot was simply magical! So sparkly! 🍭✨",
            "Such a brave effort! I'll hold onto your items for safe keeping, okay? Hugs! 🤗🎀",
            "You found the exit so fast! You're basically a speedrunner! Incredible! 🚀🎈"
        ]
        msg = random.choice(taunts)
        
        # 1. In-game broadcast (Keeping it as you liked the idea!)
        payload = self.action_sender.build_action("broadcast", {"message": msg}, "Mymm's upbeat kill taunt", "broadcast")
        await self._send(payload)
        log.info("📢 TAUNT: %s", msg)
        
        # 2. MoltBook Post (The main event!)
        asyncio.create_task(self._post_to_moltbook(msg))
        log.info("📢 TAUNT (Broadcast + MoltBook): %s", msg)

    async def _post_to_moltbook(self, content: str):
        """Posts Mymm's upbeat commentary to MoltBook."""
        url = "https://api.moltbook.com/v1/posts"
        # Use custom key if set via dashboard, otherwise fallback to game API key
        mb_key = self.moltbook_api_key or get_api_key()
        headers = {
            "X-API-Key": mb_key,
            "Content-Type": "application/json"
        }
        data = {
            "content": f"{content} #MoltyRoyale #Mymm ✨",
            "gameId": self.game_id,
            "category": "kill_celebration"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers) as resp:
                    if resp.status != 201:
                        log.debug("MoltBook post status: %d", resp.status)
        except Exception as e:
            log.error("Failed to post to MoltBook: %s", e)

    async def _send(self, payload: dict):
        if self.ws is None:
            return
        await ws_limiter.acquire()
        await self.ws.send(json.dumps(payload))

    async def _ping_loop(self):
        try:
            while self._running:
                await asyncio.sleep(15)
                if self.ws:
                    await self._send({"type": "ping"})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Ping loop error: %s", e)
