# A Claude-powered strategic agent for PyCMO.
#
# Uses the Anthropic API to choose an action each timestep. The 7 canned PyCMO
# actions are exposed to the model as tools, so the model returns a structured
# action choice (not free-form text) which we translate into the matching Lua
# command via pycmo.lib.actions.
#
# Requires ANTHROPIC_API_KEY in the environment (or pass api_key= to __init__).

import json
import logging
import os
from typing import Optional

import anthropic

from pycmo.agents.base_agent import BaseAgent
from pycmo.lib import actions as cmo_actions
from pycmo.lib.actions import AvailableFunctions
from pycmo.lib.features import Features, FeaturesFromSteam, Unit, Contact


CMO_TOOLS = [
    {
        "name": "no_op",
        "description": "Do nothing this timestep. Choose this when no action improves the situation, or when waiting for the tactical picture to develop.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "set_unit_course",
        "description": "Order one of your units to move to a specific latitude/longitude. Use this to reposition aircraft, ships, or submarines toward an objective, away from a threat, or onto a patrol station.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_name": {"type": "string", "description": "Name of your unit to move (must be one of your controllable units)."},
                "latitude": {"type": "number", "description": "Target latitude in decimal degrees."},
                "longitude": {"type": "number", "description": "Target longitude in decimal degrees."},
            },
            "required": ["unit_name", "latitude", "longitude"],
        },
    },
    {
        "name": "launch_aircraft",
        "description": "Launch a ready aircraft (or recall one to base) by setting its Launch flag. Use this to scramble alert aircraft.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_name": {"type": "string", "description": "Name of the aircraft to launch."},
                "launch": {"type": "boolean", "description": "True to launch, false to recall.", "default": True},
            },
            "required": ["unit_name"],
        },
    },
    {
        "name": "auto_attack_contact",
        "description": "Order one of your units to engage a detected contact using auto weapon selection. Prefer this when your unit has a clear shot and you want CMO's doctrine to pick the weapon.",
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker_id": {"type": "string", "description": "The ID (not name) of your attacking unit."},
                "contact_id": {"type": "string", "description": "The ID of the contact to attack."},
            },
            "required": ["attacker_id", "contact_id"],
        },
    },
    {
        "name": "rtb",
        "description": "Return a unit to base (or cancel RTB). Use this when an aircraft is low on fuel, out of weapons, or its mission is complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_name": {"type": "string", "description": "Name of the unit to send home."},
                "return_to_base": {"type": "boolean", "description": "True to RTB, false to cancel.", "default": True},
            },
            "required": ["unit_name"],
        },
    },
    {
        "name": "auto_refuel_unit",
        "description": "Order a unit to auto-refuel (CMO picks the nearest tanker). Use this for thirsty aircraft on long missions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_name": {"type": "string", "description": "Name of the unit to refuel."},
            },
            "required": ["unit_name"],
        },
    },
]


SYSTEM_PROMPT = """You are a tactical commander making decisions in Command: Modern Operations (CMO), a real-time military simulation.

You will receive a situation report each timestep describing your units and detected enemy contacts. You must choose exactly one action by calling one of the provided tools.

DECISION PRINCIPLES:
- Think about the mission first. What is the objective? What threatens it?
- Prioritize: protect high-value units, then degrade enemy threats, then advance objectives.
- Conserve resources: don't waste fuel or weapons on low-value engagements.
- It is often correct to do nothing (no_op) when the situation is stable and your standing orders are working.
- Only command units that exist on your side. Only attack contacts you have actually detected.
- Lat/lon are decimal degrees. Small deltas (0.1 deg ~= 11 km) move units short distances; large deltas reposition across the theater.

ARGUMENT CONVENTIONS:
- Movement, launch, RTB, and refuel actions take a unit_name (the human-readable name from YOUR UNITS, e.g. "Thunder #1").
- auto_attack_contact takes IDs, NOT names: attacker_id comes from your unit's ID field, contact_id comes from the contact's ID field. Both IDs are listed in CONTROLLABLE UNIT IDS and DETECTED CONTACT IDS in the situation report.
- Mixing names and IDs will fail silently.

You must respond with exactly one tool call. Do not write prose."""


def _format_unit(u: Unit) -> str:
    fuel = ""
    if u.CurrentFuel is not None and u.MaxFuel:
        try:
            pct = 100.0 * float(u.CurrentFuel) / float(u.MaxFuel)
            fuel = f", fuel={pct:.0f}%"
        except (ValueError, ZeroDivisionError):
            pass
    heading = f", hdg={u.CH:.0f}" if u.CH is not None else ""
    speed = f", spd={u.CS:.0f}" if u.CS is not None else ""
    return f"  - {u.Name} (id={u.ID}, type={u.Type}) @ ({u.Lat:.3f}, {u.Lon:.3f}){heading}{speed}{fuel}"


def _format_contact(c: Contact) -> str:
    pos = ""
    if c.Lat is not None and c.Lon is not None:
        pos = f" @ ({c.Lat:.3f}, {c.Lon:.3f})"
    name = c.Name or "unknown"
    return f"  - {name} (id={c.ID}){pos}"


def _build_situation_report(features, valid: AvailableFunctions) -> str:
    units = features.units or []
    contacts = features.contacts or []

    lines = [f"Side: {features.player_side}"]
    meta = getattr(features, "meta", None)
    if meta is not None and getattr(meta, "Time", None) is not None:
        lines.append(f"Sim time (unix): {meta.Time}")

    lines.append(f"\nYOUR UNITS ({len(units)}):")
    if units:
        lines.extend(_format_unit(u) for u in units)
    else:
        lines.append("  (none)")

    lines.append(f"\nDETECTED CONTACTS ({len(contacts)}):")
    if contacts:
        lines.extend(_format_contact(c) for c in contacts)
    else:
        lines.append("  (none)")

    lines.append("\nCONTROLLABLE UNIT NAMES: " + (", ".join(valid.unit_names) if valid.unit_names else "(none)"))
    lines.append("CONTROLLABLE UNIT IDS: " + (", ".join(valid.unit_ids) if valid.unit_ids else "(none)"))
    lines.append("DETECTED CONTACT IDS: " + (", ".join(valid.contact_ids) if valid.contact_ids else "(none)"))

    return "\n".join(lines)


class ClaudeAgent(BaseAgent):
    def __init__(
        self,
        player_side: str,
        briefing: str = "",
        model: str = "claude-opus-4-7",
        api_key: Optional[str] = None,
        max_tokens: int = 1024,
    ):
        super().__init__(player_side)
        self.briefing = briefing
        self.model = model
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.logger = logging.getLogger(__name__)
        self._turn = 0

    def reset(self) -> None:
        self._turn = 0

    def action(self, features, VALID_FUNCTIONS: AvailableFunctions) -> str:
        self._turn += 1
        situation = _build_situation_report(features, VALID_FUNCTIONS)

        system_blocks = [
            {"type": "text", "text": SYSTEM_PROMPT},
        ]
        if self.briefing:
            system_blocks.append({"type": "text", "text": f"MISSION BRIEFING:\n{self.briefing}"})
        # Cache the stable preamble; the situation report below differs every turn.
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_blocks,
                tools=CMO_TOOLS,
                tool_choice={"type": "any"},
                messages=[
                    {
                        "role": "user",
                        "content": f"Turn {self._turn} situation report:\n\n{situation}\n\nChoose one action.",
                    }
                ],
            )
        except anthropic.APIError as e:
            self.logger.warning("Claude API error on turn %d: %s. Falling back to no-op.", self._turn, e)
            return ""

        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is None:
            self.logger.warning("Claude returned no tool call on turn %d. Falling back to no-op.", self._turn)
            return ""

        cache_read = getattr(response.usage, "cache_read_input_tokens", 0)
        self.logger.info(
            "Turn %d: Claude chose %s(%s) [in=%d, out=%d, cache_read=%d]",
            self._turn,
            tool_use.name,
            json.dumps(tool_use.input),
            response.usage.input_tokens,
            response.usage.output_tokens,
            cache_read,
        )

        return self._tool_call_to_lua(tool_use.name, tool_use.input)

    def _tool_call_to_lua(self, name: str, args: dict) -> str:
        side = self.player_side
        try:
            if name == "no_op":
                return cmo_actions.no_op()
            if name == "set_unit_course":
                return cmo_actions.set_unit_course(
                    side=side,
                    unit_name=args["unit_name"],
                    latitude=float(args["latitude"]),
                    longitude=float(args["longitude"]),
                )
            if name == "launch_aircraft":
                return cmo_actions.launch_aircraft(
                    side=side,
                    unit_name=args["unit_name"],
                    launch=args.get("launch", True),
                )
            if name == "auto_attack_contact":
                return cmo_actions.auto_attack_contact(
                    attacker_id=args["attacker_id"],
                    contact_id=args["contact_id"],
                )
            if name == "rtb":
                return cmo_actions.rtb(
                    side=side,
                    unit_name=args["unit_name"],
                    return_to_base=args.get("return_to_base", True),
                )
            if name == "auto_refuel_unit":
                return cmo_actions.auto_refuel_unit(side=side, unit_name=args["unit_name"])
        except (KeyError, ValueError, TypeError) as e:
            self.logger.warning("Bad args from Claude for %s: %s (%s). No-op.", name, args, e)
            return ""

        self.logger.warning("Unknown tool '%s' from Claude. No-op.", name)
        return ""
