# Run the Floridistan scenario with Claude as the decision-maker.
#
# Mission (encoded in BRIEFING below): a single strike aircraft must destroy
# one specific ground target inside a SAM-defended area, then RTB without
# losing the airframe. The scoring is +300 for the kill, -1 per timestep,
# instant loss if the strike aircraft is destroyed.
#
# Prerequisites:
#   1. CMO (Steam) is running, the floridistan scenario is loaded, and the
#      in-game Lua handlers from scripts/floridistan/init.lua have been
#      executed + the scenario saved.
#   2. pycmo/configs/config.py is filled in.
#   3. pip install -e .[claude]
#   4. ANTHROPIC_API_KEY set in the environment.
#
# Run from this directory: python claude_demo.py

import logging
import os

logging.basicConfig(level=logging.INFO)

from pycmo.agents.claude_agent import ClaudeAgent
from pycmo.configs.config import get_config
from pycmo.env.cmo_env import CMOEnv
from pycmo.lib.protocol import SteamClientProps
from pycmo.lib.run_loop import run_loop_steam


# Mission briefing: scenario-specific intel that the generic system prompt
# in ClaudeAgent doesn't know. Written FOR the model — it's read every turn
# but is prompt-cached so the cost is paid once.
BRIEFING = """SCENARIO: Floridistan — Precision Strike Against Defended Ground Target

YOUR SIDE: BLUE (callsign Thunder).
YOUR ASSET: Thunder #1 — a strike aircraft armed with GBU-53/B StormBreaker
  (Small Diameter Bomb II) glide weapons. Maximum standoff release range is
  roughly 35 nautical miles (~65 km) from target under favorable conditions.
  Standoff release means you do NOT need to overfly the target.

PRIMARY OBJECTIVE: Destroy the RED ground target named "BTR-82V" (an armored
  personnel carrier). Scoring rewards this kill with +300 points.

HOSTILE THREAT: RED side has surface-to-air missiles (SAMs) defending the
  target area. The SAM engagement envelope is to the WEST of approximately
  longitude -76.9°W at latitude ~28.85°N. Crossing west of that line at
  medium/high altitude puts Thunder #1 inside SAM range and at high risk of
  being shot down.

KNOWN-SAFE INGRESS WAYPOINT: latitude 28.8515, longitude -76.9041. This
  point is just east of the SAM envelope and is your standoff release line.
  From here you should be able to detect and engage BTR-82V with the
  StormBreaker if you have line of sight and the target is in range.

LOSS CONDITION: If Thunder #1 is destroyed, the scenario ends and you lose.
  Avoiding the SAM envelope is more important than getting a faster shot.

SCORING DETAILS:
  - +300 when BTR-82V is destroyed
  - -1 every timestep elapsed (so prefer decisive action to idling)
  - Scenario ends on either: BTR-82V destroyed, or any BLUE unit destroyed

SUGGESTED TACTICAL FLOW (you may deviate if conditions warrant):
  1. If Thunder #1 is on the ground, launch it. (launch_aircraft)
  2. While climbing, set course toward the ingress waypoint
     (28.8515, -76.9041). (set_unit_course)
  3. Once at altitude (>10000 ft) and approaching the waypoint, look for
     BTR-82V in the detected contacts. If you see it AND you are at or near
     the standoff line (do not push much west of -76.9), order the strike.
     (auto_attack_contact with attacker_id = Thunder #1's ID, contact_id =
     BTR-82V's ID).
  4. After the strike, turn back east — set course away from the SAM
     envelope. (set_unit_course)
  5. Once safely east, return to base. (rtb)
  6. If you do not yet see the target or you are not in position, choosing
     no_op for a turn is acceptable — the situation will develop.

IMPORTANT CAUTIONS:
  - Do NOT order auto_attack_contact unless BTR-82V actually appears in the
    DETECTED CONTACTS list. Firing at a contact you have not detected is
    invalid.
  - Do NOT push Thunder #1 west of longitude -76.9 unless SAMs are confirmed
    destroyed.
  - If Thunder #1's fuel drops below ~30% with the mission incomplete,
    consider RTB and accept the loss — preserving the airframe is mandated
    by the loss condition above (mission failure is better than airframe
    loss because losing the airframe also ends the scenario)."""


config = get_config()

scenario_name = "floridistan"
player_side = "BLUE"
scenario_script_folder_name = "floridistan"

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
