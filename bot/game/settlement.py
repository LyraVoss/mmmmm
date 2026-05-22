"""
Game settlement — Phase 3: process game end, update memory, prepare for next game.

v1.5.3: more detailed lessons recorded for cross-game brain adaptation.
v1.7.2: Post-game OpenAI analysis for strategic and code suggestions.
"""
from bot.memory.agent_memory import AgentMemory
from bot.dashboard.state import dashboard_state
from bot.utils.logger import get_logger

log = get_logger(__name__)



async def settle_game(game_result: dict, entry_type: str, memory: AgentMemory):
    """
    Process game end:
    1. Extract final stats
    2. Update memory (overall history + lessons)
    3. Clear temp memory
    """
    result = game_result.get("result", game_result)
    is_winner = result.get("isWinner", False)
    final_rank = result.get("finalRank", 0)
    kills = result.get("kills", 0)
    rewards = result.get("rewards", {})
    smoltz_earned = rewards.get("sMoltz", 0)
    moltz_earned = rewards.get("moltz", 0)

    log.info("═══ GAME SETTLEMENT ═══")
    log.info("  Winner: %s | Rank: %d | Kills: %d", "YES" if is_winner else "No", final_rank, kills)
    log.info("  Rewards: %d sMoltz, %d Moltz", smoltz_earned, moltz_earned)

    memory.record_game_end(
        is_winner=is_winner,
        final_rank=final_rank,
        kills=kills,
        smoltz_earned=smoltz_earned,
    )

    # ── Record lessons for cross-game brain adaptation (v1.5.3) ──────
    lesson_base = {
        "game_id": game_result.get("gameId", "unknown"),
        "entry_type": entry_type,
        "final_rank": final_rank,
        "kills": kills,
        "smoltz_earned": smoltz_earned,
        "is_winner": is_winner,
    }

    if is_winner:
        memory.add_lesson({**lesson_base, "reason": f"DIRECTIVE:MAINTAIN_PACE - {kills} kills - stay aggro"})
    elif final_rank <= 3:
        memory.add_lesson({**lesson_base, "reason": f"DIRECTIVE:CONSERVATIVE - rank {final_rank} - prioritize survival"})
    elif kills == 0:
        memory.add_lesson({**lesson_base, "reason": "DIRECTIVE:LOW_AGGRESSION - zero kills - hunt guardians more"})
    elif kills >= 5:
        memory.add_lesson({**lesson_base, "reason": f"DIRECTIVE:STRIKER - {kills} kills confirmed"})

    # Intercept Tactic Lesson
    if smoltz_earned > 250:
        memory.add_lesson({**lesson_base, "reason": "DIRECTIVE:INTERCEPT_SUCCESS - High sMoltz yield suggests successful interception of loot-heavy agent."})

    if final_rank > 30:
        memory.add_lesson({**lesson_base, "reason": f"DIRECTIVE:EARLY_EXIT - rank {final_rank} - focus on ruins/looting"})

    # Rank-based lessons
    if final_rank > 20 and kills == 0:
        memory.add_lesson({**lesson_base, "reason": "DIRECTIVE:EARLY_EXIT - died early - prioritize equipment over exploration"})
    elif final_rank <= 10 and kills > 0:
        memory.add_lesson({**lesson_base, "reason": "DIRECTIVE:EFFECTIVE_ROAM - top 10 finish - mobility strategy effective"})

    # Add a general lesson for every game if not already covered by specific conditions
    if not any(l.get("game_id") == lesson_base["game_id"] for l in memory.get_lessons()):
        memory.add_lesson({**lesson_base, "reason": "GAME_COMPLETED - general experience gained"})

    memory.clear_temp()
    await memory.save()

    # ── Post-game OpenAI Analysis (NEW v1.7.2) ──────────────────────
    from bot.config import POST_GAME_ANALYSIS_ENABLED
    if POST_GAME_ANALYSIS_ENABLED:
        from bot.utils.openai_logic import analyze_game_and_suggest_improvements
        await analyze_game_and_suggest_improvements(game_result, entry_type, memory)
    # v1.7.0: update dashboard memory stats
    dashboard_state.record_game_result(
        kills=kills,
        is_winner=is_winner,
        final_rank=final_rank,
        smoltz_earned=smoltz_earned,
        moltz_earned=moltz_earned,
    )
    dashboard_state.update_memory(memory.data)

    log.info("Settlement complete. Ready for next game.")
