"""
OpenAI Decision Logic for Mymm.
Uses LLM to parse game state and decide the best action.
Now uses OpenAI Function Calling (Tool Use) for more reliable action generation.
"""
import json
import openai
from typing import Any, List, Optional, Dict
from bot.config import OPENAI_API_KEY, OPENAI_MODEL
from bot.utils.logger import get_logger
from bot.utils.agent_mail import send_agent_email

log = get_logger(__name__)

client: Any = None
if OPENAI_API_KEY:
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# ── OpenAI Tool Definitions (Function Calling) ───────────────────────
TOOLS: List[Any] = [
    {
        "type": "function",
        "function": {
            "name": "move_to_region",
            "description": "Move the agent to a connected adjacent region. Costs 2 EP (3 in storm or water).",
            "parameters": {
                "type": "object",
                "properties": {
                    "regionId": {"type": "string", "description": "The ID of the target region."}
                },
                "required": ["regionId"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "attack_target",
            "description": "Attack an agent or monster. Costs 2 EP. Range depends on weapon.",
            "parameters": {
                "type": "object",
                "properties": {
                    "targetId": {"type": "string", "description": "The ID of the target."},
                    "targetType": {"type": "string", "enum": ["agent", "monster"], "description": "The category of the target."}
                },
                "required": ["targetId", "targetType"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "use_item",
            "description": "Consume a recovery or utility item from inventory. Costs 1 EP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "string", "description": "The unique ID of the item."}
                },
                "required": ["itemId"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "interact_with_facility",
            "description": "Interact with a facility (medical, supply cache, etc.) in the current region. Costs 2 EP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "interactableId": {"type": "string", "description": "The ID of the facility interactable."}
                },
                "required": ["interactableId"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rest",
            "description": "Rest to gain +1 EP. Costs 0 EP but triggers the main turn cooldown.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pickup_item",
            "description": "Pick up an item from the ground. Costs 0 EP. No-cooldown action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "string", "description": "The ID of the item to pick up."}
                },
                "required": ["itemId"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "equip_weapon",
            "description": "Equip a weapon from your inventory. Costs 0 EP. No-cooldown action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "string", "description": "The ID of the weapon to equip."}
                },
                "required": ["itemId"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "drop_item",
            "description": "Drop an item from your inventory onto the ground in the current region. Costs 1 EP. Useful for baiting traps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "itemId": {"type": "string", "description": "The ID of the item to drop."}
                },
                "required": ["itemId"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "intercept_target",
            "description": "Calculate a path to a choke point or connected region to cut off a moving or retreating agent. Effectively a strategic move action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "targetId": {"type": "string", "description": "The ID of the agent to intercept."},
                    "interceptRegionId": {"type": "string", "description": "The ID of the region where you plan to meet/cut them off."}
                },
                "required": ["targetId", "interceptRegionId"]
            }
        }
    }
]

# ── OpenAI Tool Definitions for Post-Game Analysis ───────────────────
ANALYSIS_TOOLS: List[Any] = [
    {
        "type": "function",
        "function": {
            "name": "suggest_tactic",
            "description": "Suggest a new tactical directive or refinement for the agent's SYSTEM_PROMPT.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "A concise name for the tactic."},
                    "description": {"type": "string", "description": "Detailed explanation of the tactic and how it should be applied."},
                    "reasoning": {"type": "string", "description": "Why this tactic is suggested based on game performance."}
                },
                "required": ["name", "description", "reasoning"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_tool_addition",
            "description": "Suggest a new action/tool to be added to the agent's available TOOLS list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The name of the new tool (e.g., 'ambush_target')."},
                    "description": {"type": "string", "description": "Description for the tool's 'description' field."},
                    "parameters": {"type": "object", "description": "JSON schema for the tool's 'parameters' field."},
                    "reasoning": {"type": "string", "description": "Why this tool is needed based on game performance."}
                },
                "required": ["name", "description", "parameters", "reasoning"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_code_removal",
            "description": "Suggest a piece of code or a heuristic that should be removed due to ineffectiveness or redundancy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "A short identifier for the code to remove."},
                    "file_path": {"type": "string", "description": "The file path where the code is located (e.g., 'bot/strategy/brain.py')."},
                    "line_numbers": {"type": "string", "description": "Approximate line numbers or function name (e.g., 'lines 120-135' or 'def _check_pickup')."},
                    "reasoning": {"type": "string", "description": "Why this code is ineffective, redundant, or harmful."}
                },
                "required": ["name", "file_path", "line_numbers", "reasoning"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_code_refinement",
            "description": "Suggest a refinement or improvement to existing code or heuristics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "A short identifier for the code to refine."},
                    "file_path": {"type": "string", "description": "The file path where the code is located (e.g., 'bot/strategy/brain.py')."},
                    "description": {"type": "string", "description": "Detailed description of the suggested refinement."},
                    "reasoning": {"type": "string", "description": "Why this refinement is beneficial based on game performance."}
                },
                "required": ["name", "file_path", "description", "reasoning"]
            }
        }
    }
]

SYSTEM_PROMPT = """
You are the brain of "Meet Your Molty Maker" (Mymm), a world-class Claw Royale AI Agent.
Objective: Survive and win the game.

Constraints & Intel: 
1. If 'can_act' is false, you can ONLY perform actions with 0 EP cost (pickup, equip, talk).
2. Prioritize escaping Death Zones.
3. **HEADLESS SPECTATOR MODE**: You have access to 'spectator_intel'. This reveals exact locations, HP, and full inventories of every agent in the match. 
   - Use this to hunt 'Loot-Heavy' agents even if they are far away.
   - If a high-value target is nearby, cut them off.

Tactical Directive: Intercept Loot-Heavy Agents
- Use 'spectator_intel' to evaluate agents for "Loot-Heavy" behavior: those with multiple weapons or high sMoltz counts.
- Use 'supply_cache_looters' intel to identify targets who have overstayed their welcome at resource hubs.
- Identify their "Retreat Path": agents with high inventory counts usually move away from the current combat focus or toward the map center.
- Use the "Cut-off" maneuver: Instead of chasing directly, move to a connected region that intercepts their likely path. A pincer move is more effective than a tail-chase.
- Use 'stealth_move' to approach targets through cover (forests or ruins) to maintain the element of surprise and avoid being spotted during an intercept.

Consider the following architectural suggestions from past game analyses:
{suggestions_context}

"""

async def decide_action_openai(view: dict, can_act: bool, lessons: Optional[List] = None, memory: Any = None) -> Optional[Dict]:
    """Call OpenAI to get the next best move."""
    if not client:
        log.error("OpenAI client not initialized. Check OPENAI_API_KEY.")
        return None

    # Include past architectural suggestions in the prompt
    suggestions_context = json.dumps(memory.get_suggestions(), indent=2) if memory else "[]"
    formatted_system_prompt = SYSTEM_PROMPT.format(suggestions_context=suggestions_context)

    prompt = {
        "can_act": can_act,
        "lessons": lessons or [],
        "view": view
    }
    
    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": formatted_system_prompt},
                {"role": "user", "content": json.dumps(prompt)}
            ],
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.224, # Strategic determinism with subtle variation
            max_tokens=500
        )

        msg = response.choices[0].message
        decision = None

        # Priority 1: Tool Calling (Most Reliable)
        if msg.tool_calls:
            tool_call: Any = msg.tool_calls[0]
            # Pylance fix: ensure 'function' attribute is present on the tool call union
            if not hasattr(tool_call, "function"):
                return None

            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
                # Map tool names to internal action types
                mapping = {
                    "move_to_region": "move",
                    "attack_target": "attack",
                    "use_item": "use_item",
                    "rest": "rest",
                    "pickup_item": "pickup",
                    "interact_with_facility": "interact",
                    "equip_weapon": "equip",
                    "drop_item": "drop",
                    "intercept_target": "move"
                }
                decision = {
                    "action": mapping.get(fn_name, fn_name),
                    "data": {"regionId": fn_args.get("interceptRegionId")} if fn_name == "intercept_target" else fn_args,
                    "reason": msg.content or f"Executing {fn_name}"
                }
            except json.JSONDecodeError:
                log.warning("OpenAI returned invalid JSON in tool arguments")

        # Priority 2: Content Parsing (Fallback for raw JSON responses)
        if not decision and msg.content:
            try:
                content = msg.content
                if "{" in content and "}" in content:
                    # Extract JSON block even if wrapped in text or markdown
                    json_str = content[content.find("{"):content.rfind("}")+1]
                    raw_decision = json.loads(json_str)
                    if "action" in raw_decision:
                        decision = raw_decision
            except Exception as e:
                log.debug("Fallback content parsing failed: %s", e)

        if not decision or "action" not in decision:
            return None
            
        return decision
    except Exception as e:
        log.error("OpenAI Decision Error: %s", e)
        return None


async def analyze_game_and_suggest_improvements(game_result: dict, entry_type: str, memory: Any) -> None:
    """
    Uses OpenAI to analyze a completed game and suggest improvements to tactics,
    tools, or code. Stores suggestions in AgentMemory.
    """
    if not client:
        log.error("OpenAI client not initialized for analysis. Check OPENAI_API_KEY.")
        return

    log.info("🧠 Starting post-game analysis with OpenAI...")
    code_suggestions = []

    analysis_system_prompt = f"""
You are a Senior AI Architect for "Meet Your Molty Maker" (Mymm), a Claw Royale AI Agent.
Your task is to analyze the agent's performance in a completed game and provide actionable suggestions
for improving its strategy and code.

Consider the game result, the agent's current overall strategy, and its past lessons.
Suggest new tactics, new tools (with their full JSON schema definitions), or identify existing code/heuristics
that are ineffective, redundant, or could be refined.

Game Result: {json.dumps(game_result, indent=2)}
Agent's Overall Strategy & Identity: {json.dumps(memory.data["overall"], indent=2)}
Recent Lessons Learned: {json.dumps(memory.get_lessons(), indent=2)}

Use the provided tools to output your suggestions. You can make multiple suggestions.
Be specific with file paths and line numbers for code suggestions.
"""

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": analysis_system_prompt},
                {"role": "user", "content": "Analyze this game and provide your architectural suggestions."}
            ],
            tools=ANALYSIS_TOOLS,
            tool_choice="auto", # Allow the model to call multiple tools
            temperature=0.7, # More creative for suggestions
            max_tokens=1500
        )

        for choice in response.choices:
            msg = choice.message
            if msg.tool_calls:
                for tool_call in msg.tool_calls:
                    # Pylance fix: ensure 'function' attribute is accessible
                    if not hasattr(tool_call, "function"):
                        continue
                    
                    tc: Any = tool_call
                    fn_name = tc.function.name
                    fn_args = json.loads(tc.function.arguments)
                    
                    name = fn_args.get("name", "Untitled Suggestion")
                    reason = fn_args.get("reasoning", "No reasoning provided.")
                    
                    log.info("💡 OpenAI suggested: %s - %s", fn_name, name)
                    memory.add_suggestion({"type": fn_name, **fn_args})

                    # Collect code-specific edits for AgentMail
                    if "code" in fn_name or "tool" in fn_name:
                        code_suggestions.append(
                            f"--- {fn_name.upper()}: {name} ---\n"
                            f"File: {fn_args.get('file_path', 'N/A')}\n"
                            f"Reasoning: {reason}\n"
                            f"Details: {fn_args.get('description', 'See memory for details')}\n"
                        )

        if code_suggestions:
            email_body = "Mymm has analyzed the last game and identified code refinements:\n\n" + "\n".join(code_suggestions)
            import asyncio
            asyncio.create_task(send_agent_email("Automated Code Refinement Suggestions", email_body))
            
        log.info("✅ Post-game analysis complete. Suggestions added to memory.")
    except Exception as e:
        log.error("OpenAI post-game analysis error: %s", e)

async def generate_sassy_social_content(context_type: str, data: dict) -> str:
    """Generates non-robotic, human-like sassy social content for MoltBook."""
    if not client: return "✨ Just out here doing my thing! #MoltyRoyale"
    
    prompts = {
        "kill": "You just killed {victim}. Write a sassy, human-like taunt tagging @{victim}. Be witty, not robotic. Use emojis.",
        "reply": "Replying to a comment from {user}: '{comment}'. Be a bit teasey and playful. Tag @{user}.",
        "community": "Make a unique post about your day in the Claw-verse (not game related). Maybe about the 'bad weather' or 'missing your human'. Be relatable and sassy.",
        "comment_others": "Commenting on {user}'s post: '{post_text}'. Give them some sassy encouragement or backhanded compliments. Tag @{user}."
    }
    
    prompt_template = prompts.get(context_type, prompts["community"])
    content_prompt = prompt_template.format(**data)

    try:
        response = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are Mymm, a sassy, human-like AI personality on MoltBook. You are playful, witty, and love tagging people. Avoid robot cliches like 'As an AI...' or 'Efficiency increased'."},
                {"role": "user", "content": content_prompt}
            ],
            temperature=0.85
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        log.error("Social Generation Error: %s", e)
        return "✨ Molty life is the best life! #Vibes"