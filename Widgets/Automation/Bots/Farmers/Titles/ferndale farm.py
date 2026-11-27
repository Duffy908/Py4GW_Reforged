from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple
import json
import math
import os
import time
import PySystem

from Py4GWCoreLib import Agent, AgentArray, GLOBAL_CACHE, Map, Player, PyImGui
from Py4GWCoreLib.BottingTree import BottingTree
from Py4GWCoreLib.enums_src.Model_enums import GadgetModelID, ModelID
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Sources.ApoSource.ApoBottingLib import wrappers as BT
from Sources.aC_Scripts.PyQuishAI_maps.Factions_EchovaldForest.Ferndale import (
    Ferndale as KILL_PATH,
    Ferndale_outpost_path,
    Ferndale_ids,
)

MODULE_NAME = "Ferndale Kurzick Farmer BT"
MODULE_ICON = "Assets/Textures/Skill_Icons/[1813] - Lightbringer.jpg"
ROUTINE_NAME = "FerndaleKurzickSequence"
SETTINGS_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ferndale_farm_settings.json")

# ---------------------------------------------------------------------------
# Route Config (starts & donates at House zu Heltzer)
# ---------------------------------------------------------------------------
HOUSE_ZU_HELTZER = 77
OUTPOST_ID = HOUSE_ZU_HELTZER
EXPLORABLE_ID = Ferndale_ids["map_id"]        # 210 (Ferndale)
OUTPOST_PRE_PATH = Ferndale_outpost_path[:-1] # [(7810, -726), (10042, -1173)]
EXIT_POS = Ferndale_outpost_path[-1]          # (10446, -1147)
BLESSING_POS = (-12909.0, 15616.0)            # Kurzick priest in Ferndale

BLESSING_GOLD = 500
DONATE_THRESHOLD = 5_000
RUN_RETRY_TIMEOUT_MS = 30 * 60 * 1000
LEG_RETRY_TIMEOUT_MS = 15 * 60 * 1000

# 44 = Hard Mode "Locked Chest" gadget model ID
EXTRA_CHEST_MODEL_IDS: Tuple[int, ...] = (44,)

# ---------------------------------------------------------------------------
# Consumables Config (match exact names in Model_enums.py)
# ---------------------------------------------------------------------------
CONSUMABLE_ITEMS: List[Tuple[str, str, str]] = [
    ("heroes_trifectas", "Heroes' Trifectas", "Heroes_Trifectas"),
    ("empowering_feasts", "Empowering Feasts", "Empowering_Feasts"),
    ("infinite_birds_eye_compass", "Infinite Bird's Eye Compass", "Infinite_Birds_Eye_Compass"),
    ("infinite_stalkers_ration", "Infinite Stalker's Ration", "Infinite_Stalkers_Ration"),
]

_consumable_enabled: Dict[str, bool] = {
    "heroes_trifectas": True,
    "empowering_feasts": True,
    "infinite_birds_eye_compass": True,
    "infinite_stalkers_ration": True,
}

# ---------------------------------------------------------------------------
# Runtime State & Watchdogs
# ---------------------------------------------------------------------------
botting_tree: Optional[BottingTree] = None
initialized: bool = False
_verbose_logging: bool = False
_open_locked_chests: bool = True
_clear_area_radius: float = 3500.0  # Default VanquishNode is 2500 (Spirit); max compass is 5000
_consumed_chests: set = set()
_death_pos: Optional[Tuple[float, float]] = None

_last_pos: Optional[Tuple[float, float]] = None
_last_move_time: float = 0.0
_nudge_flip: int = 1
STALL_TIMEOUT_SEC: float = 15.0


def _load_saved_settings() -> None:
    """Load saved tickboxes and radius from disk on widget startup."""
    global _verbose_logging, _open_locked_chests, _clear_area_radius
    try:
        if not os.path.exists(SETTINGS_FILE_PATH):
            return
        with open(SETTINGS_FILE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return

        saved_consumables = data.get("consumables")
        if isinstance(saved_consumables, dict):
            for key, _label, _attr_name in CONSUMABLE_ITEMS:
                if key in saved_consumables:
                    _consumable_enabled[key] = bool(saved_consumables[key])

        if "open_locked_chests" in data:
            _open_locked_chests = bool(data["open_locked_chests"])
        if "verbose_logging" in data:
            _verbose_logging = bool(data["verbose_logging"])
        if "clear_area_radius" in data:
            radius_val = float(data["clear_area_radius"])
            _clear_area_radius = max(1500.0, min(5000.0, radius_val))
    except Exception:
        pass


def _save_settings() -> None:
    """Persist current tickboxes and radius to disk."""
    try:
        payload = {
            "consumables": {key: bool(_consumable_enabled.get(key, False)) for key, _label, _attr_name in CONSUMABLE_ITEMS},
            "open_locked_chests": bool(_open_locked_chests),
            "verbose_logging": bool(_verbose_logging),
            "clear_area_radius": float(_clear_area_radius),
        }
        with open(SETTINGS_FILE_PATH, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


def _is_already_in_ferndale() -> bool:
    try:
        return bool(Map.IsExplorable() and int(Map.GetMapID()) == int(EXPLORABLE_ID))
    except Exception:
        return False


def _get_kill_path_from_closest_point(steps: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Return `steps` starting from the waypoint closest to the player's current XY."""
    step_list = list(steps)
    if not step_list:
        return []
    try:
        px, py = Player.GetXY()
        best_idx = 0
        best_dist_sq = float("inf")
        for idx, (wx, wy) in enumerate(step_list):
            dx = float(wx) - float(px)
            dy = float(wy) - float(py)
            dist_sq = dx * dx + dy * dy
            if dist_sq < best_dist_sq:
                best_dist_sq = dist_sq
                best_idx = idx
        if _verbose_logging:
            nearest_pt = step_list[best_idx]
            dist = math.sqrt(best_dist_sq)
            PySystem.Console.Log(
                MODULE_NAME,
                f"Already in Ferndale: resuming from closest waypoint #{best_idx + 1}/{len(step_list)} {nearest_pt} (dist={dist:.1f}).",
                PySystem.Console.MessageType.Info,
            )
        return step_list[best_idx:]
    except Exception:
        return step_list


def _resolve_model_id(attr_name: str) -> int:
    value = getattr(ModelID, attr_name, 0)
    try:
        return int(value)
    except Exception:
        return 0


def _reset_instance_tracking() -> BehaviorTree:
    """Clear per-instance chest and death tracking when entering a fresh Ferndale map."""
    def _reset(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        global _death_pos
        _consumed_chests.clear()
        _death_pos = None
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        BehaviorTree.ActionNode(
            name="Reset Instance Tracking",
            action_fn=_reset,
        )
    )


def _wait_until_alive() -> BehaviorTree:
    """Wait at the resurrection shrine until the player is alive again."""
    def _check_alive(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        try:
            if not Map.IsExplorable():
                return BehaviorTree.NodeState.FAILURE
            if Agent.IsDead(Player.GetAgentID()):
                return BehaviorTree.NodeState.RUNNING
        except Exception:
            pass
        return BehaviorTree.NodeState.SUCCESS

    return BehaviorTree(
        BehaviorTree.ActionNode(
            name="Wait Until Respawned",
            action_fn=_check_alive,
        )
    )


def _return_to_death_spot() -> BehaviorTree:
    """If the team wiped, fight back to the saved death position before resuming the leg."""
    def _build_return(_node: BehaviorTree.Node) -> BehaviorTree:
        global _death_pos
        if _death_pos is None:
            return BT.Succeeder(name="No Death Recovery Needed")
        target = _death_pos

        def _clear_death_pos(_n: BehaviorTree.Node) -> BehaviorTree.NodeState:
            global _death_pos
            _death_pos = None
            return BehaviorTree.NodeState.SUCCESS

        return BT.Sequence(
            name="Return To Death Spot",
            children=[
                BT.Wait(1500),
                BT.MoveAndKill(
                    pos=target,
                    clear_area_radius=_clear_area_radius,
                    flag_heroes_to_waypoint=False,
                    log=_verbose_logging,
                ),
                BehaviorTree(
                    BehaviorTree.ActionNode(
                        name="Clear Death Position",
                        action_fn=_clear_death_pos,
                    )
                ),
            ],
        )

    return BT.Subtree("Return To Death Spot Subtree", _build_return)


def _use_selected_consumables() -> BehaviorTree:
    """Use all ticked consumables once Ferndale finishes loading."""
    def _make_use_node(key: str, label: str, attr_name: str) -> BehaviorTree:
        def _use_one(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
            if not _consumable_enabled.get(key, False):
                return BehaviorTree.NodeState.SUCCESS
            model_id = _resolve_model_id(attr_name)
            if model_id <= 0:
                if _verbose_logging:
                    PySystem.Console.Log(
                        MODULE_NAME,
                        f"Consumable skipped: ModelID.{attr_name} not found in Model_enums.py",
                        PySystem.Console.MessageType.Warning,
                    )
                return BehaviorTree.NodeState.SUCCESS
            try:
                item_id = GLOBAL_CACHE.Inventory.GetFirstModelID(model_id)
                if item_id:
                    GLOBAL_CACHE.Inventory.UseItem(item_id)
                    if _verbose_logging:
                        PySystem.Console.Log(
                            MODULE_NAME,
                            f"Used {label} (model_id={model_id})",
                            PySystem.Console.MessageType.Info,
                        )
                elif _verbose_logging:
                    PySystem.Console.Log(
                        MODULE_NAME,
                        f"{label} not found in inventory (model_id={model_id})",
                        PySystem.Console.MessageType.Warning,
                    )
            except Exception as e:
                if _verbose_logging:
                    PySystem.Console.Log(
                        MODULE_NAME,
                        f"Error using {label}: {e}",
                        PySystem.Console.MessageType.Error,
                    )
            return BehaviorTree.NodeState.SUCCESS

        return BehaviorTree(
            BehaviorTree.ActionNode(
                name=f"Use {label}",
                action_fn=_use_one,
            )
        )

    children: List[BehaviorTree] = [BT.Wait(1500)]
    for key, label, attr_name in CONSUMABLE_ITEMS:
        children.append(_make_use_node(key, label, attr_name))
        children.append(BT.Wait(500))
    return BT.Sequence(name="Use Selected Consumables", children=children)


def _chest_detour(radius: int = 4500) -> BehaviorTree:
    """Detour to any unconsumed locked chest within `radius`, open it, and loot."""
    target: Dict[str, float] = {}
    state: Dict[str, int] = {"id": 0}

    def _find_chest(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        if not _open_locked_chests:
            return BehaviorTree.NodeState.FAILURE
        try:
            known_chests = {
                int(e.value) for e in GadgetModelID if e.name.startswith("CHEST_")
            } | set(EXTRA_CHEST_MODEL_IDS)
            gadgets_in_range = AgentArray.Filter.ByDistance(
                AgentArray.GetGadgetArray(), Player.GetXY(), radius
            )
            gadgets_in_range = AgentArray.Sort.ByDistance(
                gadgets_in_range, Player.GetXY()
            )
            chest_id = 0
            for agent_id in gadgets_in_range:
                gadget_id = int(Agent.GetGadgetID(agent_id))
                if gadget_id not in known_chests:
                    continue
                if int(agent_id) in _consumed_chests:
                    continue
                chest_id = int(agent_id)
                break
            if chest_id == 0:
                return BehaviorTree.NodeState.FAILURE
            chest_x, chest_y = Agent.GetXY(chest_id)
            state["id"] = chest_id
            target["x"] = float(chest_x)
            target["y"] = float(chest_y)
            return BehaviorTree.NodeState.SUCCESS
        except Exception:
            return BehaviorTree.NodeState.FAILURE

    def _build_detour(_node: BehaviorTree.Node) -> BehaviorTree:
        if "x" not in target or "y" not in target:
            return BT.Succeeder()
        return BT.MoveAndInteractWithGadget((target["x"], target["y"]), log=_verbose_logging)

    def _record_chest_consumed(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        if state["id"]:
            _consumed_chests.add(state["id"])
            state["id"] = 0
        return BehaviorTree.NodeState.SUCCESS

    return BT.Selector(
        name="Chest Watchdog",
        children=[
            BT.Sequence(
                name="Detour To Locked Chest",
                children=[
                    BehaviorTree(
                        BehaviorTree.ActionNode(
                            name="Locked Chest Nearby?",
                            action_fn=_find_chest,
                        )
                    ),
                    BT.Subtree("Open Locked Chest", _build_detour),
                    BehaviorTree(
                        BehaviorTree.ActionNode(
                            name="Record Chest Consumed",
                            action_fn=_record_chest_consumed,
                        )
                    ),
                    BT.Wait(750),
                    BT.LootItems(),
                ],
            ),
            BT.Succeeder(),
        ],
    )


def _vanquish_legs_from_steps(steps_provider: Callable[[], Sequence[Tuple[float, float]]]) -> List[BehaviorTree]:
    """Chunk waypoints from `steps_provider` into 2-waypoint legs with respawn recovery."""
    max_legs = max(1, (len(KILL_PATH) + 1) // 2)
    nodes: List[BehaviorTree] = []

    for leg_index in range(1, max_legs + 1):
        start = (leg_index - 1) * 2

        def _get_leg_steps(_idx: int = leg_index, _start: int = start) -> List[Tuple[float, float]]:
            active_steps = list(steps_provider())
            if _start >= len(active_steps):
                return []
            return active_steps[_start : _start + 2]

        def _build_leg(_node: BehaviorTree.Node, _idx: int = leg_index, _get_steps=_get_leg_steps) -> BehaviorTree:
            leg_steps = _get_steps()
            if not leg_steps:
                return BT.Succeeder(name=f"Ferndale Kill Path {_idx} Complete")

            leg_sequence = BT.Sequence(
                name=f"Ferndale Leg {_idx}",
                children=[
                    _wait_until_alive(),
                    _return_to_death_spot(),
                    BT.Subtree(
                        name=f"Ferndale Kill Path {_idx}",
                        subtree_fn=lambda _n, _s=leg_steps, _i=_idx: BT.VanquishNode(
                            steps=_s,
                            clear_area_radius=_clear_area_radius,
                            name=f"Ferndale Kill Path {_i}",
                            flag_heroes_to_waypoint=False,
                            log=_verbose_logging,
                        ),
                    ),
                ],
            )
            return BehaviorTree(
                BehaviorTree.RepeaterUntilSuccessNode(
                    child=leg_sequence.root,
                    timeout_ms=LEG_RETRY_TIMEOUT_MS,
                    name=f"Ferndale Leg {_idx} Retry",
                )
            )

        nodes.append(BT.Subtree(name=f"Ferndale Leg Subtree {leg_index}", subtree_fn=_build_leg))
        nodes.append(_chest_detour())

    return nodes


def _ferndale_run() -> BehaviorTree:
    """Start at House zu Heltzer (or continue from closest waypoint if already in Ferndale)."""
    take_blessing_once = BehaviorTree(
        BehaviorTree.SelectorNode(
            name="Kurzick Blessing (One-Shot)",
            children=[
                BT.TakeBlessing(BLESSING_POS, faction="kurzick", multi_account=False).root,
                BehaviorTree.SucceederNode(name="Blessing Skipped/Consumed"),
            ],
        )
    )

    from_outpost_sequence = BT.Sequence(
        name="Start From House zu Heltzer",
        children=[
            BT.Travel(target_map_id=OUTPOST_ID, random_travel=True, hard_mode=True),
            BT.EqualizeGold(target_gold=BLESSING_GOLD, log=_verbose_logging),
            *[BT.Move(pt, tolerance=150.0, log=_verbose_logging) for pt in OUTPOST_PRE_PATH],
            BT.MoveAndExitMap(EXIT_POS, target_map_id=EXPLORABLE_ID),
            _reset_instance_tracking(),
            _use_selected_consumables(),
            take_blessing_once,
            ensure_botting_tree().Config.Aggressive(multi_account=False, auto_loot=True),
            *_vanquish_legs_from_steps(lambda: KILL_PATH),
        ],
    )

    already_in_ferndale_sequence = BT.Sequence(
        name="Continue In Ferndale From Closest Point",
        children=[
            _reset_instance_tracking(),
            _use_selected_consumables(),
            ensure_botting_tree().Config.Aggressive(multi_account=False, auto_loot=True),
            *_vanquish_legs_from_steps(lambda: _get_kill_path_from_closest_point(KILL_PATH)),
        ],
    )

    entry_branch = BehaviorTree(
        BehaviorTree.SelectorNode(
            name="Ferndale Entry Or Resume",
            children=[
                BT.Sequence(
                    name="Check Already In Ferndale",
                    children=[
                        BehaviorTree(
                            BehaviorTree.ActionNode(
                                name="Already In Ferndale?",
                                action_fn=lambda _node: (
                                    BehaviorTree.NodeState.SUCCESS
                                    if _is_already_in_ferndale()
                                    else BehaviorTree.NodeState.FAILURE
                                ),
                            )
                        ),
                        already_in_ferndale_sequence,
                    ],
                ),
                from_outpost_sequence,
            ],
        )
    )

    children: List[BehaviorTree] = [
        entry_branch,
        BT.WaitUntilOutOfCombat(),
        BT.Resign(
            wait_for_map_load=True,
            target_map_id=OUTPOST_ID,
            multi_account=False,
        ),
    ]

    run_sequence = BT.Sequence(name="Ferndale Kurzick Run", children=children)
    return BehaviorTree(
        BehaviorTree.RepeaterUntilSuccessNode(
            child=run_sequence.root,
            timeout_ms=RUN_RETRY_TIMEOUT_MS,
            name="Ferndale Run Retry",
        )
    )


def _donate_kurzick() -> BehaviorTree:
    """Donate collected Kurzick faction at House zu Heltzer after each run."""
    return BT.DonateFaction(
        faction="kurzick",
        threshold=DONATE_THRESHOLD,
        travel_map_id=HOUSE_ZU_HELTZER,
        multi_account=False,
        log=_verbose_logging,
    )


def get_execution_steps() -> List[Tuple[str, Callable[[], BehaviorTree]]]:
    return [
        ("Ferndale Run", _ferndale_run),
        ("Donate Kurzick Faction", _donate_kurzick),
    ]


def _configure_upkeep(tree: BottingTree) -> None:
    tree.Config.ConfigureUpkeep(
        looting_enabled=True,
        resurrection_scroll=True,
        auto_inventory_handler_enabled=True,
        enable_party_wipe_recovery=False,  # Stay in Ferndale and respawn at shrine on wipe
    )


def ensure_botting_tree() -> BottingTree:
    global botting_tree
    if botting_tree is None:
        botting_tree = BottingTree.Create(
            MODULE_NAME,
            main_routine=get_execution_steps(),
            routine_name=ROUTINE_NAME,
            repeat=True,
            multi_account=False,
            auto_loot=True,
            configure_fn=_configure_upkeep,
        )
    return botting_tree


def _check_movement_stall(tree: BottingTree) -> None:
    """Track death position on wipe and nudge if Move pauses indefinitely on an unpopped enemy."""
    global _last_pos, _last_move_time, _nudge_flip, _death_pos

    if not tree.IsStarted() or tree.IsPaused():
        _last_pos = None
        return
    try:
        if not Map.IsExplorable() or int(Map.GetMapID()) != int(EXPLORABLE_ID):
            _last_pos = None
            return

        if Agent.IsDead(Player.GetAgentID()):
            if _death_pos is None:
                _death_pos = Player.GetXY()
                if _verbose_logging:
                    PySystem.Console.Log(
                        MODULE_NAME,
                        f"Player died at {_death_pos} - waiting to respawn at shrine.",
                        PySystem.Console.MessageType.Warning,
                    )
            _last_move_time = time.time()
            return

        status = tree.UI._main_status_snapshot()
        if status.get("combat_active") or status.get("looting_active"):
            _last_move_time = time.time()
            return

        x, y = Player.GetXY()
        now = time.time()
        if _last_pos is None:
            _last_pos = (x, y)
            _last_move_time = now
            return

        dx = x - _last_pos[0]
        dy = y - _last_pos[1]
        if (dx * dx + dy * dy) > (150.0 * 150.0):
            _last_pos = (x, y)
            _last_move_time = now
            return

        if (now - _last_move_time) >= STALL_TIMEOUT_SEC:
            _nudge_flip *= -1
            Player.Move(x + (250.0 * _nudge_flip), y + (250.0 * _nudge_flip))
            _last_move_time = now
            if _verbose_logging:
                PySystem.Console.Log(
                    MODULE_NAME,
                    "Stall detected (15s idle) - nudging character to resume movement.",
                    PySystem.Console.MessageType.Warning,
                )
    except Exception:
        pass


def _draw_settings_tab() -> None:
    """Custom Farm Settings tab with live combat radius, chest, and chat logging controls."""
    global _verbose_logging, _open_locked_chests, _clear_area_radius

    changed = False

    PyImGui.text("Combat / Enemy Search")
    PyImGui.separator()
    new_radius = float(
        PyImGui.slider_int(
            "Enemy Search Radius##ferndale_radius",
            int(_clear_area_radius),
            1500,
            5000,
        )
    )
    if new_radius != _clear_area_radius:
        _clear_area_radius = new_radius
        changed = True
    PyImGui.text("Default: 2500 (Spirit). Recommended for Ferndale: 3000 - 4000.")
    PyImGui.text("Changes apply on the next leg without resetting the run.")

    PyImGui.spacing()
    PyImGui.text("Chests")
    PyImGui.separator()
    new_chests = PyImGui.checkbox("Open nearby Locked Chests##ferndale_chests", _open_locked_chests)
    if new_chests != _open_locked_chests:
        _open_locked_chests = new_chests
        changed = True

    PyImGui.spacing()
    PyImGui.text("Chat Logging")
    PyImGui.separator()
    new_log = PyImGui.checkbox("Enable movement/script chat logs##ferndale_log", _verbose_logging)
    if new_log != _verbose_logging:
        _verbose_logging = new_log
        changed = True

    if changed:
        _save_settings()


def _draw_consumables_tab() -> None:
    """Consumables tab with tickboxes for map-load items."""
    PyImGui.text("Map-Load Consumables")
    PyImGui.separator()
    PyImGui.text("Ticked items will be used automatically when entering Ferndale.")
    PyImGui.spacing()
    changed = False
    for key, label, _attr_name in CONSUMABLE_ITEMS:
        current_val = _consumable_enabled.get(key, False)
        new_val = PyImGui.checkbox(
            f"{label}##ferndale_{key}",
            current_val,
        )
        if new_val != current_val:
            _consumable_enabled[key] = new_val
            changed = True
    if changed:
        _save_settings()


_load_saved_settings()


def main() -> None:
    global initialized
    if not initialized:
        ensure_botting_tree()
        initialized = True
    tree = ensure_botting_tree()
    tree.tick()
    _check_movement_stall(tree)
    extra_tabs: List[Tuple[str, Callable[[], None]]] = [
        ("Farm Settings", _draw_settings_tab),
        ("Consumables", _draw_consumables_tab),
    ]
    tree.UI.draw_window(
        icon_path=os.path.join(PySystem.Console.get_projects_path(), MODULE_ICON),
        main_child_dimensions=(420, 360),
        extra_tabs=extra_tabs,
    )


def tooltip() -> str:
    return MODULE_NAME + " - Ferndale Kurzick farm from House zu Heltzer with auto-donate."