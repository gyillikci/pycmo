# Run the steam_demo scenario with Claude as the decision-maker.
#
# Prerequisites:
#   1. CMO (Steam edition) is launched, the steam_demo.scen scenario is loaded,
#      and the in-game Lua handlers from init.lua have been set up + saved.
#   2. pycmo/configs/config.py is filled in (see config_template.py).
#   3. The Anthropic SDK is installed: `pip install anthropic`
#   4. ANTHROPIC_API_KEY is set in the environment.
#
# Run from this directory:  python claude_demo.py

import logging
import os

logging.basicConfig(level=logging.INFO)

from pycmo.agents.claude_agent import ClaudeAgent
from pycmo.configs.config import get_config
from pycmo.env.cmo_env import CMOEnv
from pycmo.lib.protocol import SteamClientProps
from pycmo.lib.run_loop import run_loop_steam


BRIEFING = """You command Israeli forces in a small scenario. Your asset of interest is
'Sufa #1' (an F-16I aircraft). Keep it alive, conserve fuel, and engage hostile
contacts only when you have a clear advantage. If no enemies are detected and
the unit is on a reasonable course, no_op is fine."""


config = get_config()

scenario_name = "Steam demo"
player_side = "Israel"
scenario_script_folder_name = "steam_demo"

command_version = config["command_mo_version"]
observation_path = os.path.join(config["steam_observation_folder_path"], f"{scenario_name}.inst")
action_path = os.path.join(config["scripts_path"], scenario_script_folder_name, "agent_action.lua")
scen_ended_path = os.path.join(config["steam_observation_folder_path"], f"{scenario_name}_scen_has_ended.inst")
steam_client_props = SteamClientProps(
    scenario_name=scenario_name,
    agent_action_filename=action_path,
    command_version=command_version,
)

env = CMOEnv(
    player_side=player_side,
    steam_client_props=steam_client_props,
    observation_path=observation_path,
    action_path=action_path,
    scen_ended_path=scen_ended_path,
)

agent = ClaudeAgent(
    player_side=player_side,
    briefing=BRIEFING,
    model=os.environ.get("PYCMO_CLAUDE_MODEL", "claude-opus-4-7"),
)

run_loop_steam(env=env, agent=agent, max_steps=None)
