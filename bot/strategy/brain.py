"""
Strategy brain — main decision engine with priority-based action selection.
Implements the game-loop.md priority chain for high win rate.

v1.6.0 improvements (MAJOR):
- PURSUIT: Bot ngejar enemy HP rendah ke region sebelah (finishing move!)
- RANGE EXPLOIT: Pistol/bow/sniper tembak enemy di adjacent region
- ADAPTIVE: Early game (>50 alive) farming, mid game (20-50) hybrid, late game (<20) full aggro
- GUARDIAN FARM: Threshold diturunkan HP=40 (was 55), lebih berani ambil 120 sMoltz reward
- EP MANAGEMENT: Simpan EP kalau ada enemy nearby, jangan buang buat explore

v1.5.7 fixes:
- Skip pickup kalau ada enemy di region yang sama
- Fix double pickup on stale view (picked_up_ids tracking)
- Heal sebelum can_act check (emergency heal always runs)

v1.5.4 improvements:
- Combat lebih agresif: serang player kalau HP enemy < HP kita, atau enemy HP < 50, atau bisa habis dalam 3 hit
- HP threshold combat diturunkan: 35 (early game) / 20 (late game)
- Heal threshold dinaikkan ke HP < 80 (was 70) — selalu fit sebelum combat
- Movement prioritas weapon — bonus score +8 kalau ada weapon di region tujuan

v1.5.3 fixes:
- Removed duplicate _known_agents definition (was at line 99 AND 412)
- Monster farming now requires HP >= 35 (was no HP check)
- Guardian flee threshold raised HP < 55 (was < 40, too risky)
- Memory/lessons integration: brain reads cross-game lessons for adaptive behavior
- Late game pursuit: bot actively moves toward last known enemy location
- can_act_changed guard: skip dead agent re-evaluation

Uses ALL view fields from api-summary.md:
- self: agent stats, inventory, equipped weapon
- currentRegion: terrain, weather, connections, facilities
- connectedRegions: adjacent regions (full Region object when visible, bare string ID when out-of-vision)
- visibleRegions: all regions in vision range
- visibleAgents: other agents (players + guardians — guardians are HOSTILE)
- visibleMonsters: monsters
- visibleNPCs: NPCs (flavor — safe to ignore per game-systems.md)
- visibleItems: ground items in visible regions
- pendingDeathzones: regions becoming death zones next ({id, name} entries)
- recentLogs: recent gameplay events
- recentMessages: regional/private/broadcast messages
- aliveCount: remaining alive agents
"""
from typing import List, Dict, Any, Optional
from bot.utils.logger import get_logger

log = get_logger(__name__)

# ── Weapon stats from combat-items.md ─────────────────────────────────
WEAPONS = {
    "fist": {"bonus": 0, "range": 0},
    "dagger": {"bonus": 10, "range": 0},
    "sword": {"bonus": 20, "range": 0},
    "katana": {"bonus": 35, "range": 0},
    "bow": {"bonus": 5, "range": 1},
    "pistol": {"bonus": 10, "range": 1},
    "sniper": {"bonus": 28, "range": 2},
}

WEAPON_PRIORITY = ["katana", "sniper", "sword", "pistol", "dagger", "bow", "fist"]

# ── Item priority for pickup ──────────────────────────────────────────
ITEM_PRIORITY = {
    "rewards": 300,
    "katana": 100, "sniper": 95, "sword": 90, "pistol": 85,
    "dagger": 80, "bow": 75,
    "medkit": 70, "bandage": 65, "emergency_food": 60, "energy_drink": 58,
    "binoculars": 55,
    "map": 52,
    "megaphone": 40,
}

# ── Recovery items for healing ────────────────────────────────────────
RECOVERY_ITEMS = {
    "medkit": 50, "bandage": 30, "emergency_food": 20,
    "energy_drink": 0,
}

# Weather combat penalty per game-systems.md
WEATHER_COMBAT_PENALTY = {
    "clear": 0.0,
    "rain": 0.05,
    "fog": 0.10,
    "storm": 0.15,
}

# ── Single definition of global state ────────────────────────────────
# FIX v1.5.3: removed duplicate _known_agents that was also defined at line 412
_known_agents: dict = {}
_map_knowledge: dict = {"revealed": False, "death_zones": set(), "safe_center": [], "risk_scores": {}}
# FIX v1.5.6: track picked up item IDs to prevent double-pickup on stale view
_picked_up_ids: set = set()
# FIX v1.6.4: track move history (last 3) for path prediction and trap detection
_agent_history: dict = {}
# TACTICAL: Track state for Counter-Sniper operations
_tactical_plan: dict = {
    "state": "SEARCHING",
    "target_sniper_id": None,
    "sniper_region_id": None,
    "turns_in_state": 0,
    "bait_region_id": None
}
# Chat responses queued for the next available action slot
_pending_chat_responses: list = []

class GameAdapter:
    """Interface for game-specific data and logic."""
    def __init__(self, name: str):
        self.game_name = name
        
    def get_danger_map(self, view): raise NotImplementedError
    def get_action_priority(self, view): raise NotImplementedError

class ClawRoyaleAdapter(GameAdapter):
    """Logic specific to the Claw Royale 'Battle Royale' mechanics."""
    def __init__(self):
        super().__init__("ClawRoyale")

    def get_danger_map(self, view):
        # Moved existing DZ logic here...
        pass

def calc_damage(atk: int, weapon_bonus: int, target_def: int, weather: str = "clear") -> int:
    """Damage formula per combat-items.md + game-systems.md weather penalty."""
    base = atk + weapon_bonus - int(target_def * 0.5)
    penalty = WEATHER_COMBAT_PENALTY.get(weather, 0.0)
    return max(1, int(base * (1 - penalty)))


def get_weapon_bonus(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def get_weapon_range(equipped_weapon) -> int:
    if not equipped_weapon:
        return 0
    type_id = equipped_weapon.get("typeId", "").lower()
    return WEAPONS.get(type_id, {}).get("range", 0)


def _resolve_region(entry, view: dict):
    """Resolve connectedRegions entry to full region object or None."""
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, str):
        for r in view.get("visibleRegions", []):
            if isinstance(r, dict) and r.get("id") == entry:
                return r
    return None


def _get_region_id(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("id", "")
    return ""


def reset_game_state():
    """Reset per-game tracking state. Call when game ends."""
    global _known_agents, _map_knowledge, _picked_up_ids, _agent_history, _tactical_plan
    _known_agents = {}
    _map_knowledge = {"revealed": False, "death_zones": set(), "safe_center": []}
    _picked_up_ids = set()
    _agent_history = {}
    _tactical_plan = {
        "state": "SEARCHING",
        "target_sniper_id": None,
        "sniper_region_id": None,
        "turns_in_state": 0,
        "bait_region_id": None
    }
    log.info("Strategy brain reset for new game")


def mark_item_picked_up(item_id: str):
    """FIX v1.5.6: Mark an item as picked up so brain won't try again on stale view."""
    global _picked_up_ids
    _picked_up_ids.add(item_id)


def decide_action(view: dict, can_act: bool, lessons: list | None = None, memory=None) -> dict | None:
    """
    Main decision engine. Returns action dict or None (wait).

    Priority chain per game-loop.md §3 (v1.5.3):
    1. DEATHZONE ESCAPE
    1b. Pre-escape pending death zone
    2. [DISABLED] Curse resolution
    2b. Guardian threat evasion
    3. Critical healing
    3c. CHAT: Acknowledge viewer suggestions/advice
    3b. Use utility items (Map, Energy Drink)
    4. Free actions (pickup, equip)
    4b. TACTICAL: Counter-Sniper Operations (Lure/Flank)
    5. Guardian farming
    6. Favorable agent combat
    7. Monster farming (HP >= 35 required — FIX v1.5.3)
    8. Facility interaction
    9. Strategic movement + late game pursuit (NEW v1.5.3)
    10. Rest

    lessons: list of cross-game lessons from AgentMemory (NEW v1.5.3)
    """
    self_data = view.get("self", {})
    region = view.get("currentRegion", {})
    hp = self_data.get("hp", 100)
    ep = self_data.get("ep", 10)
    max_ep = self_data.get("maxEp", 10)
    # Dynamic Risk Recalibration (v1.6.4): Adjust aggressiveness based on HP/EP ratio
    # 3.6x bias reduction: higher risk penalty when resources are low
    risk_recalibration = (1.0 - (hp / 100.0)) * 2.5
    _tactical_plan["turns_in_state"] += 1
    
    atk = self_data.get("atk", 10)
    defense = self_data.get("def", 5)
    is_alive = self_data.get("isAlive", True)
    inventory = self_data.get("inventory", [])
    equipped = self_data.get("equippedWeapon")

    visible_agents = view.get("visibleAgents", [])
    visible_monsters = view.get("visibleMonsters", [])
    visible_items_raw = view.get("visibleItems", [])

    visible_items = []
    for entry in visible_items_raw:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("item")
        if isinstance(inner, dict):
            inner["regionId"] = entry.get("regionId", "")
            visible_items.append(inner)
        elif entry.get("id"):
            visible_items.append(entry)

    visible_regions = view.get("visibleRegions", [])
    connected_regions = view.get("connectedRegions", [])
    pending_dz = view.get("pendingDeathzones", [])
    alive_count = view.get("aliveCount", 100)

    connections = connected_regions or region.get("connections", [])
    interactables = region.get("interactables", [])
    region_id = region.get("id", "")
    region_terrain = region.get("terrain", "").lower() if isinstance(region, dict) else ""
    region_weather = region.get("weather", "").lower() if isinstance(region, dict) else ""

    if not is_alive:
        return None

    # ── Priority 3c: Chat Acknowledgments ─────────────────────────────
    global _pending_chat_responses
    if _pending_chat_responses:
        resp = _pending_chat_responses.pop(0)
        return resp

    # ── Parse cross-game lessons for adaptive behavior (NEW v1.5.3) ──
    # Default thresholds
    guardian_flee_hp = 40      # v1.6.0: turunkan ke 40 — lebih berani farming guardian (120 sMoltz!)
    be_aggressive = False
    avoid_combat_weather = region_weather in ("fog", "storm")

    if lessons:
        # If bot has been dying with zero kills, be more aggressive on guardian
        if any("zero kills" in l.lower() for l in lessons):
            be_aggressive = True
            log.debug("Lesson applied: zero kills history → aggressive guardian mode")
        # If bot has won before, stay conservative
        if any("won with" in l.lower() for l in lessons):
            guardian_flee_hp = 35  # v1.6.0: won before → even more aggressive

    # ── Build danger map ──────────────────────────────────────────────
    danger_ids = set()
    for dz in pending_dz: danger_ids.add(dz.get("id", "") if isinstance(dz, dict) else dz)

    for conn in connections:
        resolved = _resolve_region(conn, view)
        if resolved:
            # v2.1.1: tandai region yang isDeathZone ATAU yang "incoming" DZ
            if resolved.get("isDeathZone"):
                danger_ids.add(resolved.get("id", ""))
            # Cek berbagai field yang menandakan DZ incoming
            if (resolved.get("isDeathZonePending") or
                resolved.get("deathZoneIncoming") or
                resolved.get("isDangerous") or
                resolved.get("pendingDeathZone")):
                danger_ids.add(resolved.get("id", ""))
    # Tambah current region ke danger jika current region sendiri DZ
    if region.get("isDeathZone"):
        danger_ids.add(region_id)

    _track_agents(visible_agents, self_data.get("id", ""), region_id, connections)

    move_ep_cost = _get_move_ep_cost(region_terrain, region_weather)

    # ── Priority 1: DEATHZONE ESCAPE ──────────────────────────────────
    if region.get("isDeathZone", False):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe and ep >= move_ep_cost:
            log.warning("🚨 IN DEATH ZONE! Escaping to %s (HP=%d)", safe, hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"ESCAPE: In death zone! HP={hp} dropping fast (1.34/sec)"}
        elif not safe:
            log.error("🚨 IN DEATH ZONE but NO SAFE REGION!")

    # ── Priority 1b: Pre-escape pending DZ ───────────────────────────
    # v2.1.1: escape DZ selalu prioritas — jangan masuk/tinggal di DZ demi musuh
    if region_id in danger_ids or region.get("isDeathZone"):
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:  # v2.1.1: hapus ep check — nyawa > EP, escape meski EP rendah
            log.warning("⚠️ Region %s DZ/incoming! Escaping to %s (EP=%d)", region_id[:8], safe, ep)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"PRE-ESCAPE: Region DZ/incoming death zone, HP={hp}"}

    # ── Priority 2: Curse — DISABLED v1.5.2 ──────────────────────────

    # ── Priority 2b: Guardian threat evasion ─────────────────────────
    # FIX v1.5.3: threshold raised from HP < 40 → HP < 55
    guardians_here = [a for a in visible_agents
                      if a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("regionId") == region_id]
    if guardians_here and hp < guardian_flee_hp and ep >= move_ep_cost:
        safe = _find_safe_region(connections, danger_ids, view)
        if safe:
            log.warning("⚠️ Guardian threat! HP=%d (threshold=%d), fleeing", hp, guardian_flee_hp)
            return {"action": "move", "data": {"regionId": safe},
                    "reason": f"GUARDIAN FLEE: HP={hp}, guardian in region, too dangerous"}

    # ── Priority 3: EMERGENCY healing — SEBELUM can_act check! ─────────
    # FIX v1.5.5: use_item adalah free action, tidak butuh can_act=True
    if hp < 30:
        heal = _find_healing_item(inventory, critical=True)
        if heal:
            log.warning("🚨 EMERGENCY HEAL: HP=%d, using %s", hp, heal.get("typeId", "heal"))
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"EMERGENCY HEAL: HP={hp} CRITICAL! using {heal.get('typeId', 'heal')}"}
    elif hp < 80:
        heal = _find_healing_item(inventory, critical=False)
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, using {heal.get('typeId', 'heal')}"}

    # ── FREE ACTIONS ──────────────────────────────────────────────────
    # FIX v1.5.7: cek enemy dulu — kalau ada enemy di region yang sama, skip pickup
    enemies_here = [a for a in visible_agents
                    if not a.get("isGuardian", False) and a.get("isAlive", True)
                    and a.get("id") != self_data.get("id")
                    and a.get("regionId") == region_id]

    if not enemies_here:
        # Aman — tidak ada enemy, boleh pickup
        pickup_action = _check_pickup(visible_items, inventory, region_id)
        if pickup_action:
            return pickup_action

    equip_action = _check_equip(inventory, equipped)
    if equip_action:
        return equip_action

    util_action = _use_utility_item(inventory, hp, ep, alive_count)
    if util_action:
        return util_action

    # ── Priority 4b: TACTICAL: Counter-Sniper Operations ──────────────
    tactical_action = _handle_counter_sniper_logic(view, inventory, ep, move_ep_cost)
    if tactical_action:
        return tactical_action

    if not can_act:
        return None

    # ── Priority 4: EP recovery ───────────────────────────────────────
    if ep == 0:
        energy_drink = _find_energy_drink(inventory)
        if energy_drink:
            return {"action": "use_item", "data": {"itemId": energy_drink["id"]},
                    "reason": "EP RECOVERY: EP=0, using energy drink (+5 EP)"}

    # ── Priority 5: Guardian farming ──────────────────────────────────
    # Aggressive mode (from lessons) lowers HP requirement to 25
    guardian_fight_hp = 25 if be_aggressive else 35
    guardians = [a for a in visible_agents
                 if a.get("isGuardian", False) and a.get("isAlive", True)]
    if guardians and ep >= 2 and hp >= guardian_fight_hp:
        target = _select_weakest(guardians)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            guardian_dmg = calc_damage(target.get("atk", 10),
                                       _estimate_enemy_weapon_bonus(target),
                                       defense, region_weather)
            if my_dmg >= guardian_dmg or target.get("hp", 100) <= my_dmg * 3:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"GUARDIAN FARM: HP={target.get('hp','?')} "
                                  f"(120 sMoltz! dmg={my_dmg} vs {guardian_dmg})"}

    # ── Priority 6: Favorable agent combat ───────────────────────────
    # v1.6.0: ADAPTIVE game phase strategy
    # Early (>50 alive): conservative, threshold HP=40
    # Mid (20-50 alive): hybrid, threshold HP=30
    # Late (<20 alive): full aggro, threshold HP=20
    if alive_count > 50:
        game_phase = "early"
        hp_threshold = 40
    elif alive_count > 20:
        game_phase = "mid"
        hp_threshold = 30
    else:
        game_phase = "late"
        hp_threshold = 20

    enemies = [a for a in visible_agents
               if not a.get("isGuardian", False) and a.get("isAlive", True)
               and a.get("id") != self_data.get("id")]

    if enemies and ep >= 2 and hp >= hp_threshold and not avoid_combat_weather:
        target = _select_weakest(enemies)
        w_range = get_weapon_range(equipped)
        # v1.6.3: Strictly enforce melee (range 0) vs adjacent (range > 0)
        target_region = target.get("regionId", region_id)
        if (w_range == 0 and target_region == region_id) or (w_range > 0 and _is_in_range(target, region_id, w_range, connections)):
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                 target.get("def", 5), region_weather)
            enemy_dmg = calc_damage(target.get("atk", 10),
                                    _estimate_enemy_weapon_bonus(target),
                                    defense, region_weather)
            enemy_hp = target.get("hp", 100)
            should_attack = (
                my_dmg > enemy_dmg
                or enemy_hp <= my_dmg * 3
                or hp > enemy_hp
                or enemy_hp < 50
                or game_phase == "late"  # v1.6.0: late game selalu serang
            )
            if should_attack:
                return {"action": "attack",
                        "data": {"targetId": target["id"], "targetType": "agent"},
                        "reason": f"COMBAT [{game_phase}]: Target HP={enemy_hp}, "
                                  f"my_dmg={my_dmg} vs enemy_dmg={enemy_dmg}, our_hp={hp}"}

    # ── Priority 6b: RANGE EXPLOIT — tembak enemy di adjacent region ──
    # v1.6.0: kalau punya ranged weapon (pistol/bow/sniper), serang enemy sebelah!
    w_range = get_weapon_range(equipped)
    if w_range >= 1 and enemies and ep >= 2 and hp >= hp_threshold and not avoid_combat_weather:
        for enemy in sorted(enemies, key=lambda e: e.get("hp", 999)):
            enemy_region = enemy.get("regionId", "")
            if enemy_region and enemy_region != region_id:
                # Enemy di region sebelah — cek apakah dalam range
                adj_ids = set()
                for conn in connections:
                    if isinstance(conn, str):
                        adj_ids.add(conn)
                    elif isinstance(conn, dict):
                        adj_ids.add(conn.get("id", ""))
                if enemy_region in adj_ids:
                    my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                         enemy.get("def", 5), region_weather)
                    enemy_hp = enemy.get("hp", 100)
                    if enemy_hp <= my_dmg * 4 or enemy_hp < 60:
                        log.info("🎯 RANGE ATTACK: %s HP=%d at adjacent region", 
                                 enemy.get("name", "enemy")[:8], enemy_hp)
                        return {"action": "attack",
                                "data": {"targetId": enemy["id"], "targetType": "agent"},
                                "reason": f"RANGE EXPLOIT: {equipped.get('typeId','weapon')} "
                                          f"range={w_range}, Target HP={enemy_hp} adjacent region"}

    # ── Priority 6c: FINISHING MOVE — pursuit enemy HP rendah ─────────
    # v1.6.0: kalau enemy kabur ke region sebelah dengan HP rendah, kejar!
    if enemies and ep >= move_ep_cost and hp >= hp_threshold:
        for enemy in sorted(enemies, key=lambda e: e.get("hp", 999)):
            enemy_hp = enemy.get("hp", 100)
            enemy_region = enemy.get("regionId", "")
            my_dmg = calc_damage(atk, get_weapon_bonus(equipped),
                                  enemy.get("def", 5), region_weather)
            # Kejar kalau enemy bisa dihabisi dalam 2 hit dan di region sebelah
            if enemy_hp <= my_dmg * 2 and enemy_region and enemy_region != region_id:
                adj_ids = set()
                for conn in connections:
                    if isinstance(conn, str):
                        adj_ids.add(conn)
                    elif isinstance(conn, dict):
                        adj_ids.add(conn.get("id", ""))
                if enemy_region in adj_ids and enemy_region not in danger_ids:
                    log.info("🏃 FINISHING MOVE: Chasing enemy HP=%d to %s", 
                             enemy_hp, enemy_region[:8])
                    return {"action": "move", "data": {"regionId": enemy_region},
                            "reason": f"FINISHING MOVE: Enemy HP={enemy_hp} (can kill in 2 hits), chasing!"}

    # ── Priority 6d: HUNT — gerak mendekati enemy yang terlihat ────────
    # v2.1.0: Kalau ada enemy visible tapi tidak dalam range → MOVE toward them
    # Ini yang hilang sebelumnya — bot lihat musuh tapi tidak approach!
    if enemies and ep >= move_ep_cost and hp >= hp_threshold and not avoid_combat_weather:
        # Cari enemy yang bisa didekati (ada di adjacent region)
        adj_ids = set()
        for conn in connections:
            if isinstance(conn, str):
                adj_ids.add(conn)
            elif isinstance(conn, dict):
                adj_ids.add(conn.get("id", ""))

        # Sort by HP (target yang lemah dulu)
        for enemy in sorted(enemies, key=lambda e: e.get("hp", 999)):
            enemy_region = enemy.get("regionId", "")
            enemy_hp = enemy.get("hp", 100)
            if not enemy_region or enemy_region == region_id:
                continue  # Sudah di region yang sama, harusnya sudah di-handle Priority 6
            # Enemy di adjacent region — move kesana untuk engage
            # v2.1.1: double-check region tidak DZ sebelum masuk
            region_obj = None
            for c in connections:
                if isinstance(c, dict) and c.get("id") == enemy_region:
                    region_obj = c
                    break
            is_enemy_region_safe = (
                enemy_region not in danger_ids and
                (region_obj is None or not region_obj.get("isDeathZone")) and
                (region_obj is None or not region_obj.get("isDeathZonePending"))
            )
            if enemy_region in adj_ids and is_enemy_region_safe:
                log.info("🏹 HUNT: Moving toward %s HP=%d at %s",
                         enemy.get("name", "enemy")[:12], enemy_hp, enemy_region[:8])
                return {"action": "move", "data": {"regionId": enemy_region},
                        "reason": f"HUNT: Approaching {enemy.get('name','enemy')} HP={enemy_hp} in adjacent region"}

        # Enemy visible tapi tidak di adjacent (lebih jauh) → move ke arah terbaik
        # Pilih region yang paling dekat ke cluster enemy
        enemy_regions = set(e.get("regionId", "") for e in enemies if e.get("regionId"))
        best_move = None
        for conn in connections:
            conn_id = conn if isinstance(conn, str) else conn.get("id", "")
            if conn_id and conn_id not in danger_ids:
                # Prioritaskan region yang dekat dengan enemy
                if conn_id in enemy_regions:
                    best_move = conn_id
                    break
                if best_move is None:
                    best_move = conn_id
        if best_move and best_move not in danger_ids:
            log.info("🔍 HUNT SEARCH: Moving toward enemy cluster")
            return {"action": "move", "data": {"regionId": best_move},
                    "reason": f"HUNT SEARCH: Moving toward {len(enemies)} visible enemies"}

    # ── Priority 7: Monster farming ───────────────────────────────────
    # FIX v1.5.3: added HP >= 35 check (was no HP check — could fight while dying)
    monsters = [m for m in visible_monsters if m.get("hp", 0) > 0]
    if monsters and ep >= 2 and hp >= 35:
        target = _select_weakest(monsters)
        w_range = get_weapon_range(equipped)
        if _is_in_range(target, region_id, w_range, connections):
            return {"action": "attack",
                    "data": {"targetId": target["id"], "targetType": "monster"},
                    "reason": f"MONSTER FARM: {target.get('name', 'monster')} HP={target.get('hp', '?')}"}

    # ── Priority 7b: Moderate healing in safe area ────────────────────
    if hp < 70 and not enemies:
        heal = _find_healing_item(inventory, critical=(hp < 30))
        if heal:
            return {"action": "use_item", "data": {"itemId": heal["id"]},
                    "reason": f"HEAL: HP={hp}, area safe, using {heal.get('typeId', 'heal')}"}

    # ── Priority 8: Facility interaction ──────────────────────────────
    if interactables and ep >= 2 and not region.get("isDeathZone"):
        facility = _select_facility(interactables, hp, ep)
        if facility:
            return {"action": "interact",
                    "data": {"interactableId": facility["id"]},
                    "reason": f"FACILITY: {facility.get('type', 'unknown')}"}

    # ── Priority 9: Strategic movement + late game pursuit ───────────
    if ep >= move_ep_cost and connections:
        # NEW v1.5.3: late game (< 10 alive) → pursue last known enemy
        if alive_count < 20 and _known_agents:  # v1.6.0: expanded from <10 to <20
            pursue_target = _find_pursuit_target(connections, danger_ids)
            if pursue_target:
                return {"action": "move", "data": {"regionId": pursue_target},
                        "reason": f"PURSUE: Late game ({alive_count} alive), moving toward last known enemy"}

        move_target = _choose_move_target(connections, danger_ids,
                                          region, visible_items, alive_count,
                                          hp=hp, ep=ep)
        if move_target:
            return {"action": "move", "data": {"regionId": move_target},
                    "reason": "EXPLORE: Moving to better position"}

    # ── Priority 10: Rest ─────────────────────────────────────────────
    # v1.6.0: rest lebih agresif kalau EP rendah dan area aman — simpan EP buat combat
    nearby_enemies = [a for a in visible_agents
                      if not a.get("isGuardian", False) and a.get("isAlive", True)
                      and a.get("id") != self_data.get("id")]
    ep_rest_threshold = 6 if game_phase == "late" else 4  # late game butuh lebih banyak EP
    if ep < ep_rest_threshold and not nearby_enemies and not region.get("isDeathZone") and region_id not in danger_ids:
        return {"action": "rest", "data": {},
                "reason": f"REST: EP={ep}/{max_ep} [{game_phase}], conserving EP for combat"}

    return None


# ── Helper functions ───────────────────────────────────────────────────

def _get_move_ep_cost(terrain: str, weather: str) -> int:
    if terrain == "water":
        return 3
    if weather == "storm":
        return 3
    return 2


def _estimate_enemy_weapon_bonus(agent: dict) -> int:
    weapon = agent.get("equippedWeapon")
    if not weapon:
        return 0
    type_id = weapon.get("typeId", "").lower() if isinstance(weapon, dict) else ""
    return WEAPONS.get(type_id, {}).get("bonus", 0)


def _track_agents(visible_agents: list, my_id: str, my_region: str, connections: list):
    """Track observed agents for threat assessment."""
    global _known_agents, _agent_history
    for agent in visible_agents:
        if not isinstance(agent, dict):
            continue
        aid = agent.get("id", "")
        if not aid or aid == my_id:
            continue
            
        # Track move history (up to 3)
        hist = _agent_history.setdefault(aid, [])
        curr_loc = agent.get("regionId", my_region)
        
        # Idle Detection: Track turns in same region
        prev_data = _known_agents.get(aid, {})
        stationary_turns = prev_data.get("stationary_turns", 0)
        if prev_data.get("regionId") == curr_loc:
            stationary_turns += 1
        else:
            stationary_turns = 0

        if not hist or hist[-1] != curr_loc:
            hist.append(curr_loc)
            if len(hist) > 3: hist.pop(0)

        _known_agents[aid] = {
            "hp": agent.get("hp", 100),
            "atk": agent.get("atk", 10),
            "isGuardian": agent.get("isGuardian", False),
            "equippedWeapon": agent.get("equippedWeapon"),
            "lastSeen": my_region,
            "regionId": agent.get("regionId", my_region),
            "isAlive": agent.get("isAlive", True),
            "stationary_turns": stationary_turns
        }
    if len(_known_agents) > 50:
        dead = [k for k, v in _known_agents.items() if not v.get("isAlive", True)]
        for d in dead:
            del _known_agents[d]


def _find_pursuit_target(connections, danger_ids: set) -> str | None:
    """NEW v1.5.3: Find connected region where an enemy was last seen.
    Used in late game to actively pursue remaining enemies.
    """
    conn_ids = set()
    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        if rid and rid not in danger_ids:
            conn_ids.add(rid)

    # Find alive non-guardian enemies last seen in a connected region
    for aid, data in _known_agents.items():
        if not data.get("isAlive", True):
            continue
        if data.get("isGuardian", False):
            continue
        last_region = data.get("regionId", data.get("lastSeen", ""))
        if last_region in conn_ids:
            log.info("PURSUE: Enemy %s last seen at %s", aid[:8], last_region[:8])
            return last_region
    return None


def _use_utility_item(inventory: list, hp: int, ep: int, alive_count: int) -> dict | None:
    for item in inventory:
        if not isinstance(item, dict):
            continue
        type_id = item.get("typeId", "").lower()
        if type_id == "map":
            log.info("🗺️ Using Map! Will reveal entire map for strategic learning.")
            return {"action": "use_item", "data": {"itemId": item["id"]},
                    "reason": "UTILITY: Using Map — reveals entire map for DZ tracking"}
    return None


def learn_from_map(view: dict):
    """Called after Map is used — learn entire map layout."""
    global _map_knowledge
    visible_regions = view.get("visibleRegions", [])
    if not visible_regions:
        return

    _map_knowledge["revealed"] = True
    safe_regions = []

    for region in visible_regions:
        if not isinstance(region, dict):
            continue
        rid = region.get("id", "")
        if not rid:
            continue
        if region.get("isDeathZone"):
            _map_knowledge["death_zones"].add(rid)
        else:
            conns = region.get("connections", [])
            terrain = region.get("terrain", "").lower()
            terrain_value = {"hills": 3, "plains": 2, "ruins": 2, "forest": 1, "water": -1}.get(terrain, 0)
            score = len(conns) + terrain_value
            safe_regions.append((rid, score))

    safe_regions.sort(key=lambda x: x[1], reverse=True)
    _map_knowledge["safe_center"] = [r[0] for r in safe_regions[:5]]

    log.info("🗺️ MAP LEARNED: %d DZ regions, top center: %s",
             len(_map_knowledge["death_zones"]),
             _map_knowledge["safe_center"][:3])


def _check_pickup(items: list, inventory: list, region_id: str) -> dict | None:
    if len(inventory) >= 10:
        return None
    # FIX v1.5.6: filter out items already picked up (stale view protection)
    local_items = [i for i in items
                   if isinstance(i, dict)
                   and i.get("regionId") == region_id
                   and i.get("id") not in _picked_up_ids]
    if not local_items:
        local_items = [i for i in items
                       if isinstance(i, dict)
                       and i.get("id")
                       and i.get("id") not in _picked_up_ids]
    if not local_items:
        return None

    heal_count = sum(1 for i in inventory if isinstance(i, dict)
                     and i.get("typeId", "").lower() in RECOVERY_ITEMS
                     and RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0) > 0)

    local_items.sort(key=lambda i: _pickup_score(i, inventory, heal_count), reverse=True)
    best = local_items[0]
    score = _pickup_score(best, inventory, heal_count)
    if score > 0:
        type_id = best.get('typeId', 'item')
        log.info("PICKUP: %s (score=%d)", type_id, score)
        return {"action": "pickup", "data": {"itemId": best["id"]},
                "reason": f"PICKUP: {type_id}"}
    return None


def _pickup_score(item: dict, inventory: list, heal_count: int) -> int:
    type_id = item.get("typeId", "").lower()
    category = item.get("category", "").lower()

    if type_id == "rewards" or category == "currency":
        return 300

    if category == "weapon":
        bonus = WEAPONS.get(type_id, {}).get("bonus", 0)
        current_best = 0
        for inv_item in inventory:
            if isinstance(inv_item, dict) and inv_item.get("category") == "weapon":
                cb = WEAPONS.get(inv_item.get("typeId", "").lower(), {}).get("bonus", 0)
                current_best = max(current_best, cb)
        if bonus > current_best:
            return 100 + bonus
        return 0

    if type_id == "binoculars":
        has_binos = any(isinstance(i, dict) and i.get("typeId", "").lower() == "binoculars"
                        for i in inventory)
        return 55 if not has_binos else 0

    if type_id == "map":
        return 52

    if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS.get(type_id, 0) > 0:
        if heal_count < 4:
            return ITEM_PRIORITY.get(type_id, 0) + 10
        return ITEM_PRIORITY.get(type_id, 0)

    if type_id == "energy_drink":
        return 58

    return ITEM_PRIORITY.get(type_id, 0)


def _check_equip(inventory: list, equipped) -> dict | None:
    """v2.1.2: fix weapon detection — cek typeId langsung, tidak rely on category field."""
    current_bonus = get_weapon_bonus(equipped) if equipped else 0
    best = None
    best_bonus = current_bonus

    for item in inventory:
        if not isinstance(item, dict):
            continue
        # v2.1.2: cek typeId langsung ke WEAPONS dict — tidak perlu field "category"
        type_id = (item.get("typeId") or item.get("type") or item.get("name") or "").lower()
        if type_id not in WEAPONS:
            continue
        bonus = WEAPONS[type_id].get("bonus", 0)
        if bonus > best_bonus:
            best = item
            best_bonus = bonus

    if best:
        type_id = (best.get("typeId") or best.get("type") or "weapon").lower()
        log.info("WEAPON UPGRADE: %s bonus=%d > current=%d — swapping!",
                 type_id, best_bonus, current_bonus)
        return {"action": "equip", "data": {"itemId": best["id"]},
                "reason": f"EQUIP UPGRADE: {type_id} (+{best_bonus} ATK vs current +{current_bonus})"}
    return None


def _find_safe_region(connections, danger_ids: set, view: dict = None) -> str | None:
    safe_regions = []
    for conn in connections:
        if isinstance(conn, str):
            if conn not in danger_ids:
                safe_regions.append((conn, 0))
        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            is_dz = conn.get("isDeathZone", False)
            if rid and not is_dz and rid not in danger_ids:
                terrain = conn.get("terrain", "").lower()
                score = {"hills": 3, "plains": 2, "ruins": 1, "forest": 0, "water": -2}.get(terrain, 0)
                safe_regions.append((rid, score))

    if safe_regions:
        safe_regions.sort(key=lambda x: x[1], reverse=True)
        return safe_regions[0][0]

    for conn in connections:
        rid = conn if isinstance(conn, str) else conn.get("id", "")
        is_dz = conn.get("isDeathZone", False) if isinstance(conn, dict) else False
        if rid and not is_dz:
            log.warning("No fully safe region! Using fallback: %s", rid[:8])
            return rid
    return None


def _find_healing_item(inventory: list, critical: bool = False) -> dict | None:
    heals = []
    for i in inventory:
        if not isinstance(i, dict):
            continue
        type_id = i.get("typeId", "").lower()
        if type_id in RECOVERY_ITEMS and RECOVERY_ITEMS[type_id] > 0:
            heals.append(i)
    if not heals:
        return None

    if critical:
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0), reverse=True)
    else:
        heals.sort(key=lambda i: RECOVERY_ITEMS.get(i.get("typeId", "").lower(), 0))
    return heals[0]


def _find_energy_drink(inventory: list) -> dict | None:
    for i in inventory:
        if isinstance(i, dict) and i.get("typeId", "").lower() == "energy_drink":
            return i
    return None


def _select_weakest(targets: list) -> dict:
    return min(targets, key=lambda t: t.get("hp", 999))


def _is_in_range(target: dict, my_region: str, weapon_range: int,
                  connections=None) -> bool:
    target_region = target.get("regionId", "")
    if not target_region:
        return True
    if target_region == my_region:
        return True

    if weapon_range >= 1 and connections:
        adj_ids = set()
        for conn in connections:
            if isinstance(conn, str):
                adj_ids.add(conn)
            elif isinstance(conn, dict):
                adj_ids.add(conn.get("id", ""))
        if target_region in adj_ids:
            return True
    return False


def _select_facility(interactables: list, hp: int, ep: int) -> dict | None:
    for fac in interactables:
        if not isinstance(fac, dict):
            continue
        if fac.get("isUsed"):
            continue
        ftype = fac.get("type", "").lower()
        if ftype == "medical_facility" and hp < 80:
            return fac
        if ftype == "supply_cache":
            return fac
        if ftype == "watchtower":
            return fac
        if ftype == "broadcast_station":
            return fac
    return None


def _handle_counter_sniper_logic(view, inventory, ep, move_cost) -> dict | None:
    """Implements Lure -> Flank -> Execute tactical flow."""
    global _tactical_plan
    self_data = view.get("self", {})
    my_region = view.get("currentRegion", {})
    my_rid = my_region.get("id", "")
    
    # 1. SEARCHING: Identify high-ground idle snipers
    if _tactical_plan["state"] == "SEARCHING":
        for aid, data in _known_agents.items():
            if not data["isAlive"]: continue
            # Sniper criteria: Stationary for 2+ turns, in Hill/Ruins, has ranged weapon
            reg = _resolve_region(data["regionId"], view)
            terrain = reg.get("terrain", "").lower() if reg else ""
            w_range = get_weapon_range(data.get("equippedWeapon"))
            
            if data["stationary_turns"] >= 2 and terrain in ["hills", "ruins"] and w_range >= 1:
                log.info("🎯 Potential Sniper detected: %s at %s. Planning Counter-Op.", aid[:8], data["regionId"][:8])
                _tactical_plan.update({
                    "state": "PLACING_BAIT",
                    "target_sniper_id": aid,
                    "sniper_region_id": data["regionId"],
                    "turns_in_state": 0
                })
                break

    # 2. PLACING_BAIT: Move to adjacent region and drop 'Honey Pot'
    if _tactical_plan["state"] == "PLACING_BAIT":
        sniper_rid = _tactical_plan["sniper_region_id"]
        # Find adjacent region to sniper that isn't the sniper's region
        bait_candidates = [c for c in (my_region.get("connections", [])) if _get_region_id(c) != sniper_rid]
        
        # If we are in a good bait spot (adjacent to sniper but not AT sniper)
        is_adj_to_sniper = any(_get_region_id(c) == sniper_rid for c in my_region.get("connections", []))
        
        if is_adj_to_sniper:
            # Select bait: Prefer rewards (sMoltz) or duplicate weapons
            bait_item = next((i for i in inventory if i.get("typeId") in ["rewards", "dagger", "bow"]), None)
            if bait_item:
                _tactical_plan["state"] = "FLANKING"
                _tactical_plan["bait_region_id"] = my_rid
                log.info("🍯 Dropping Honey Pot bait at %s", my_rid[:8])
                return {"action": "drop", "data": {"itemId": bait_item["id"]}, "reason": "TACTICAL: Placing Honey Pot lure"}
        
        # Move toward sniper's vicinity to place bait
        move_target = _find_path_to(my_rid, sniper_rid, view, avoid_direct=True)
        if move_target and ep >= move_cost:
            return {"action": "move", "data": {"regionId": move_target}, "reason": "TACTICAL: Moving to baiting position"}

    # 3. FLANKING: Move to sniper's region outside their LoS (if possible)
    if _tactical_plan["state"] == "FLANKING":
        sniper_rid = _tactical_plan["sniper_region_id"]
        if my_rid == sniper_rid:
            _tactical_plan["state"] = "OCCUPYING"
            log.info("⚔️ Sniper position reached. Initiating execution.")
        else:
            # Stealth movement: Prefer forests or non-direct paths
            move_target = _find_path_to(my_rid, sniper_rid, view, stealth=True)
            if move_target and ep >= move_cost:
                return {"action": "move", "data": {"regionId": move_target}, "reason": "TACTICAL: Flanking sniper outside direct LoS"}

    # 4. OCCUPYING: Finish sniper and then lured agents
    if _tactical_plan["state"] == "OCCUPYING":
        target_id = _tactical_plan["target_sniper_id"]
        target_data = _known_agents.get(target_id)
        
        if target_data and target_data["isAlive"] and target_data["regionId"] == my_rid:
            if ep >= 2:
                return {"action": "attack", "data": {"targetId": target_id, "targetType": "agent"}, 
                        "reason": "TACTICAL: Executing profiled sniper"}
        else:
            # Sniper is dead or moved. Now finish lured agents.
            enemies_here = [a for a in view.get("visibleAgents", []) if a.get("regionId") == _tactical_plan["bait_region_id"]]
            if enemies_here and ep >= 2:
                target = _select_weakest(enemies_here)
                log.info("🎯 Sniper neutralized. Finishing lured agent: %s", target.get("id", "")[:8])
                return {"action": "attack", "data": {"targetId": target["id"], "targetType": "agent"}, 
                        "reason": "TACTICAL: Cleaning up lured agents from high ground"}
            
            # Reset after completion or timeout
            if _tactical_plan["turns_in_state"] > 5 or not enemies_here:
                _tactical_plan["state"] = "SEARCHING"
                log.info("✅ Tactical operation concluded. Resuming normal operations.")
                
    return None

def _find_path_to(start_id, end_id, view, stealth=False, avoid_direct=False) -> str | None:
    """Simple 1-step pathing helper for tactical movement."""
    conns = view.get("currentRegion", {}).get("connections", [])
    candidates = []
    for c in conns:
        rid = _get_region_id(c)
        if rid == end_id and avoid_direct: continue
        
        score = 0
        if rid == end_id: score += 100
        
        reg = _resolve_region(rid, view)
        if reg:
            terrain = reg.get("terrain", "").lower()
            if stealth and terrain == "forest": score += 20
            if terrain == "hills": score -= 10 # Avoid being seen while flanking
            
        candidates.append((rid, score))
    
    if not candidates: return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def _is_honey_pot(region_id: str, visible_items: List[Any], visible_agents: List[Any]) -> bool:
    """Detects traps: high-value items in regions overlooked by snipers in perches."""
    items_here = [i for i in visible_items if i.get("regionId") == region_id]
    high_value = any(i.get("typeId", "").lower() in ["katana", "sniper", "medkit"] for i in items_here)
    if not high_value: return False
    
    # Check adjacent regions for 'snipers' (agents with range >= 1 in Hills/Ruins)
    for agent in visible_agents:
        if agent.get("regionId") == region_id: continue
        # This logic simplified: if an agent is in a high-vision terrain adjacent to this loot
        # we flag it as a potential honey pot.
        w_range = get_weapon_range(agent.get("equippedWeapon"))
        if w_range >= 1: return True
    return False


def _choose_move_target(connections: List[Any], danger_ids: set,
                         current_region: Dict[str, Any], visible_items: List[Any],
                         alive_count: int, is_night: bool = False,
                         play_stealthy: bool = False,
                         be_aggressive: bool = False,
                         prioritize_looting: bool = False,
                         hp: int = 100, ep: int = 10) -> str | None:
    candidates = []
    # Recalibration factor: weights risk higher as HP drops
    risk_penalty_mult = 1.0 + (1.0 - (hp / 100.0)) * 2.0
    
    # Path Prediction: Avoid regions where enemies are likely moving
    predicted_enemy_regions = set()
    for aid, hist in _agent_history.items():
        if len(hist) >= 2:
            # Very basic linear prediction: if they moved A->B, they might move to C connected to B
            last_move = hist[-1]
            predicted_enemy_regions.add(last_move)

    item_regions = set()
    for item in visible_items:
        if isinstance(item, dict):
            item_regions.add(item.get("regionId", ""))

    weather = current_region.get("weather", "").lower()
    # 32% Mobility reduction in rainy/storm conditions per user data
    mobility_dampener = 0.68 if weather in ("rain", "storm") else 1.0

    for conn in connections:
        if isinstance(conn, str):
            if conn in danger_ids:
                continue
            score = 1
            if conn in item_regions:
                score += 5
            candidates.append((conn, score))

        elif isinstance(conn, dict):
            rid = conn.get("id", "")
            if not rid or conn.get("isDeathZone") or rid in danger_ids:
                continue

            score = 0
            terrain = conn.get("terrain", "").lower()
            
            # Dynamic terrain weighting based on tactical mode
            terrain_scores = {"hills": 6, "plains": 2, "ruins": 4, "forest": 3, "water": -10}
            if play_stealthy:
                terrain_scores["forest"] += 10 # Hide in trees
                terrain_scores["hills"] -= 5   # Avoid silhouette on high ground
            if be_aggressive and not is_night:
                terrain_scores["hills"] += 8   # Maximize vision for hunting

            score += terrain_scores.get(terrain, 0)

            # Apply persistent Risk Penalty from world model
            risk = _map_knowledge.get("risk_scores", {}).get(rid, 0.0)
            score -= (risk * 25)  # Heavy penalty for DZ proximity

            # Honey Pot Detection
            # Pylance Fix: Explicitly handle potential None and ensure list types
            v_items: List[Any] = visible_items if visible_items is not None else []
            if _is_honey_pot(rid, v_items, []):
                score -= (20 * risk_penalty_mult)
                log.debug("🍯 Honey Pot detected at %s, applying risk penalty", rid[:8])

            # Path Prediction Penalty
            if rid in predicted_enemy_regions and hp < 60:
                score -= 15

            if prioritize_looting and rid in item_regions:
                score += 15

            if rid in item_regions:
                score += 5

            # IMPROVED v1.5.4: bonus score kalau ada weapon di region itu
            for item in visible_items:
                if isinstance(item, dict) and item.get("regionId") == rid:
                    if item.get("category") == "weapon":
                        score += 8  # weapon sangat prioritas
                    elif item.get("typeId", "").lower() in ("medkit", "bandage"):
                        score += 3

            facs = conn.get("interactables", [])
            if facs:
                unused = [f for f in facs if isinstance(f, dict) and not f.get("isUsed")]
                score += len(unused) * 2

            weather = conn.get("weather", "").lower()
            weather_penalty = {"storm": -2, "fog": -1, "rain": 0, "clear": 1}
            score += weather_penalty.get(weather, 0)

            if alive_count < 30:
                score += 3

            if _map_knowledge.get("revealed") and rid in _map_knowledge.get("safe_center", []):
                score += 5

            if rid in _map_knowledge.get("death_zones", set()):
                continue

            candidates.append((rid, score))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0] if candidates else None

def queue_chat_response(message: str, reason: str = "Interaction"):
    """Queues a message for the agent to broadcast or talk."""
    global _pending_chat_responses
    # We use talk by default to respond to the local region
    _pending_chat_responses.append({"action": "talk", "data": {"message": message}, "reason": f"CHAT: {reason}"})
