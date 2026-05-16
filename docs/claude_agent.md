# PyCMO + Claude: Notes & Runbook

This doc captures the work done on the `claude/build-and-run-VBy3L` branch:
why we ended up where we did, the design behind the Claude integration, and
how to actually run it. Written as a narrative so a teammate (or future you)
can pick it up cold.

---

## 1. Where we started: "can you build and run it?"

PyCMO is a Python wrapper around **Command: Modern Operations** (CMO), a
commercial Windows-only wargame. Initial attempt to build and run it in a
Linux sandbox surfaced the obvious blocker:

- `setup.py` pins `pywin32==306` — Windows-only.
- `pycmo/lib/protocol.py` imports `win32com.client`.
- `pycmo/lib/tools.py` imports `win32gui`.
- The engine itself only runs on Windows.

We installed the non-`win32` deps manually (`numpy`, `xmltodict==0.12.0`,
`gymnasium==0.29.1`) and confirmed the pure-Python parts of the package
import cleanly. 6 of 7 test modules can't even be collected on Linux because
of the `win32*` imports; `tests/test_config.py` runs partially.

**Conclusion:** the Python package builds on Linux with workarounds, but the
project can only meaningfully *run* on Windows with CMO installed. Everything
that followed is targeted at the user's Windows + Steam CMO setup.

---

## 2. Walkthrough: running PyCMO against a live Steam CMO

The steps we worked through, condensed:

1. **Enable Lua socket + I/O in `CPE.ini`** (in the CMO install dir or
   `%USERPROFILE%\AppData\Local\Command Modern Operations`):
   ```ini
   [Lua]
   EnableSocket = 1
   SocketPort = 7777
   AllowIO = 1
   EncodingMode = 8
   ```

2. **Clone + install PyCMO** somewhere stable (e.g. `E:\MyProjects\pycmo`):
   ```
   pip install -e .
   ```

3. **Create `pycmo/configs/config.py`** from `config_template.py`. Edit
   `pycmo_path`, `cmo_path`, `command_mo_version` (must match CMO's title
   bar text exactly — used to focus the game window when sending commands).

4. **Load a scenario in CMO** (player mode, not editor).

5. **Wire up in-game Lua handlers** once per scenario:
   - Edit `scripts/<scenario>/init.lua` to point `pycmo_path` at the install.
   - Open CMO's script console, paste `init.lua`, run.
   - This registers two repeating events: one writes observations to
     `…\Command - Modern Operations\ImportExport\<scenario>.inst`, the other
     reads `scripts/<scenario>/agent_action.lua` on a timer to execute
     agent-side actions.
   - **Save the scenario** so the events stick.

6. **Press Play** in CMO. The Lua timers only fire while the scenario is
   running.

7. **Run the agent** from another terminal:
   ```
   python scripts/<scenario>/demo.py
   ```

The closed loop is file-based on both sides:
- CMO → `.inst` XML file → `features.py` parses → Python observation.
- Python action → `agent_action.lua` → CMO's Lua timer picks it up → game
  state mutates.

Common gotchas: scenario paused (no Lua firing), `AllowIO=1` missing,
`command_mo_version` not matching the title bar, `init.lua` never saved
into the scenario, or the agent-action timer event disabled in the event
editor.

---

## 3. The RL question: what is this actually good for?

Asked whether PyCMO is useful for RL, the honest take:

**The value is access, not algorithms.** The repo ships no learning agent —
the included `RandomAgent`, `ScriptedAgent`, `RuleBasedAgent` don't learn.
What PyCMO gives you is:

1. A DeepMind-style `TimeStep(step_id, step_type, reward, observation)` loop
   over a serious commercial wargame.
2. A `Gymnasium` wrapper (`pycmo/env/cmo_gym_env.py`) so SB3/RLlib/CleanRL
   can drive it.
3. A distilled, typed action space (`pycmo/lib/actions.py`): 7 canonical
   functions over CMO's vast ScenEdit API. `AvailableFunctions.refresh()`
   re-derives the legal action set each turn — exactly the masking pattern
   RL needs in combinatorial domains.
4. Observation extraction from CMO's XML exports into typed
   `Unit`/`Contact`/`Weapon` objects (`pycmo/lib/features.py`).
5. The Python ↔ Lua ↔ game synchronization plumbing, which is the part
   that takes days to get right and is usually the blocker for academic
   work on top of CMO.
6. A curriculum-friendly scenario list (~11 historical/fictional scenarios
   of escalating complexity).

**Weaknesses for RL specifically:**
- No reward shaping helpers; you compute reward from observation diffs.
- File-based Lua bridge has second-scale latency. Throughput is
  closer to "robotics-sim RL" pacing than Atari pacing. No vectorized envs.
- CMO's RNG isn't seedable externally; reproducibility is poor.
- Opponent is CMO's scripted AI — realistic doctrine, but not a learner.
- Action space is coarse (orders, not stick-and-throttle). Good for
  command-level RL, useless for low-level flight control.

The IEEE paper Hua et al. (linked in the README) is the existence proof
that RL works end-to-end here; expect overnight runs for toy results.

---

## 4. Pivot: instead of training, use Claude as the policy

Given the latency profile (seconds per env step, no fast reset), training
an RL policy is expensive and slow. **Using Claude as the decision-maker
is a much better fit for this loop:**

- One Claude call per env step is 2–5s — well within CMO's tick cadence
  (5s observation export by default).
- The 7 canned PyCMO actions map cleanly onto Claude tool definitions,
  so the model returns a structured action choice (not free-form text
  we'd have to parse).
- System prompt + tool definitions are stable across turns →
  prompt-cached, so per-turn input-token cost is ~10% of the uncached
  baseline after turn 1.
- Scenario-specific intel (objectives, ROE, threat geometry) is encoded
  in a `briefing` string passed at construction time — same agent code
  works for any scenario, only the briefing changes.

---

## 5. What we built

### `pycmo/agents/claude_agent.py` — `ClaudeAgent(BaseAgent)`

The agent. Each `action()` call does:

1. Builds a compact situation report from `features` (your units with
   fuel/heading/position, detected contacts, controllable unit names + IDs,
   contact IDs).
2. Sends it to Claude with the 7 PyCMO actions defined as tools:
   `no_op`, `set_unit_course`, `launch_aircraft`, `auto_attack_contact`,
   `rtb`, `auto_refuel_unit`.
3. Forces a tool call (`tool_choice: {"type": "any"}`) so the response is
   always one of the legal actions with typed args.
4. Translates the tool call to the matching `ScenEdit_*` Lua string via
   `pycmo.lib.actions` helpers — same code path the scripted agent uses,
   so we know it produces well-formed commands.
5. Logs token usage and cache reads so you can watch cost.

Design choices worth noting:

- **Prompt caching:** `cache_control: {"type": "ephemeral"}` on the last
  system block. System prompt + briefing are cached as a single prefix;
  only the per-turn situation report is fresh tokens.
- **Graceful degradation:** API error, missing tool call, or bad args →
  returns `""` (no-op). A transient failure does not crash the run loop.
- **Name vs ID convention:** baked into the system prompt. Movement /
  launch / RTB / refuel take unit *names*; `auto_attack_contact` takes
  unit and contact *IDs*. Mixing fails silently in CMO, so the prompt
  is explicit about which goes where.
- **Single tool call per turn:** `tool_choice: any` returns exactly one
  action per turn. For coordinated multi-unit play we'd remove that
  constraint and merge multiple Lua strings — easy extension.
- **No memory across turns:** each `action()` is independent. Fine for
  reactive command; not fine for long-horizon plans. To add memory,
  thread previous (observation, action) pairs into `messages`.

### `scripts/steam_demo/claude_demo.py`

Drop-in replacement for the existing `demo.py`. Uses `ClaudeAgent` with a
generic Israel/Sufa #1 briefing against the `steam_demo.scen` scenario.

### `scripts/floridistan/claude_demo.py`

The **scenario-tuned** demo. Floridistan is already wired up in the repo
with proper events and scoring, so it was the natural choice:

- **Mission:** BLUE / `Thunder #1` (strike aircraft with GBU-53/B
  StormBreaker SDBs, ~35nm standoff) must destroy RED ground target
  `BTR-82V` inside a SAM-defended area.
- **Threat:** SAM envelope west of ~longitude `-76.9°W` at latitude
  `28.85°N` (the existing scripted agent uses `(28.8515, -76.9041)` as
  its "outside SAM radius" waypoint, so that boundary is canonical).
- **Scoring:** +300 for the kill, −1/turn, instant loss if any BLUE unit
  is destroyed.

The `BRIEFING` constant in this file encodes everything a briefed pilot
would know: target identity, weapon standoff range, SAM envelope
geometry as concrete coordinates, the known-safe ingress waypoint,
scoring rules, and a six-step suggested tactical flow (explicitly framed
as "you may deviate if conditions warrant"). The agent code is
unchanged — all scenario knowledge lives in this one string.

### `setup.py`

Adds an optional `[claude]` extra:

```
pip install -e .[claude]
```

Pulls `anthropic>=0.40.0`.

---

## 6. How to run the Claude agent end-to-end

Same Windows steps as §2, but with the `[claude]` install and an
Anthropic API key.

```powershell
# one-time
cd E:\MyProjects\pycmo
git pull
pip install -e .[claude]
set ANTHROPIC_API_KEY=sk-ant-...

# per-run
# 1. In CMO: load scen/floridistan.scen, run scripts/floridistan/init.lua
#    in the script console (after editing pycmo_path inside it), save the
#    scenario, press Play.
# 2. In a terminal:
cd scripts\floridistan
python claude_demo.py
```

Optional environment knobs:
- `PYCMO_CLAUDE_MODEL` — defaults to `claude-opus-4-7`. Set to
  `claude-sonnet-4-6` for faster/cheaper turns, or `claude-haiku-4-5`
  for cheapest.

What you should see in the log on a successful run:
- Turn 1: Claude calls `launch_aircraft(unit_name="Thunder #1")`.
- Turn 2–N: `set_unit_course` toward the ingress waypoint while
  Thunder #1 climbs.
- Once BTR-82V appears in contacts and the aircraft is on the standoff
  line: `auto_attack_contact(attacker_id=..., contact_id=...)`.
- Post-strike: `set_unit_course` east, then `rtb`.
- Token usage logged each turn; `cache_read_input_tokens` should be
  non-zero from turn 2 onward (the system+briefing prefix is cached).

---

## 7. Comparison: scripted agent vs Claude agent

|                                | `ScriptedAgent`            | `ClaudeAgent`                |
| ------------------------------ | -------------------------- | ---------------------------- |
| Logic                          | Hardcoded 5-state FSM      | Reasoned from briefing       |
| Cost per decision              | $0                         | ~$0.01–0.05 (cached)         |
| Latency per decision           | <1 ms                      | 2–5 s                        |
| Handles target relocation      | No                         | Yes (if observed)            |
| Handles new threat appearing   | No                         | Yes                          |
| Breaks if waypoint coords wrong | n/a                       | Yes (rewrite briefing)       |
| Reusable for another scenario  | Rewrite the FSM            | Rewrite the briefing         |

The scripted agent is the right choice when the scenario is fixed and you
just want a baseline. Claude is the right choice when you want one piece
of code that handles many scenarios via configuration, or when the
scenario has enough variability that hand-coding state transitions
becomes tedious.

---

## 8. Known limitations / things we deliberately did NOT do

- **No persistent memory across turns.** Easy to add: keep a rolling
  `messages` list of (observation summary, tool call) pairs and pass it
  in. Cost grows linearly with history length unless you compact.
- **No multi-unit coordination per turn.** Currently one tool call per
  turn. Remove `tool_choice: any` and merge multiple returned tool calls
  into a multi-line Lua action string to fix.
- **No reward signal back to Claude.** The agent doesn't see whether
  the previous action helped. A reward channel (computed from successive
  `features` diffs) injected into the observation would close that loop.
- **No self-play, no training, no value function.** This is an LLM
  policy, not RL. If you want RL, swap `ClaudeAgent` for a learning
  agent and use the same env wrapper.
- **No EULA review.** CMO is commercial software; extracting databases
  or publishing derived datasets has restrictions. Internal experiments
  are fine.

---

## 9. Files added on this branch

```
pycmo/agents/claude_agent.py          # the ClaudeAgent class + tool defs
scripts/steam_demo/claude_demo.py     # generic Steam demo with Claude
scripts/floridistan/claude_demo.py    # Floridistan-tuned demo with mission briefing
setup.py                              # +[claude] extra (anthropic>=0.40.0)
docs/claude_agent.md                  # this document
```

---

## 10. Branch

All work is on `claude/build-and-run-VBy3L`. Pushed to origin.
