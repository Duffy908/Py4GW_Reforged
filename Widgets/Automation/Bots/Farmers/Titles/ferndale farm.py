from __future__ import annotations

from typing import Callable, List, Optional, Tuple
import os
import PySystem

from Py4GWCoreLib import HeroType, Map
from Py4GWCoreLib.BottingTree import BottingTree
from Py4GWCoreLib.py4gwcorelib_src.BehaviorTree import BehaviorTree
from Py4GWCoreLib.routines_src.BehaviourTrees import BT as CoreBT
from Sources.ApoSource.ApoBottingLib import wrappers as BT
from Sources.aC_Scripts.PyQuishAI_maps.Factions_EchovaldForest.Ferndale import (
    Ferndale as KILL_PATH,
    Ferndale_outpost_path,
    Ferndale_ids,
)

MODULE_NAME = "Ferndale Kurzick Farmer BT"
MODULE_ICON = "Assets/Textures/Skill_Icons/[1813] - Lightbringer.jpg"
ROUTINE_NAME = "FerndaleKurzickSequence"

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
HERO_AGGRESSIVE_MODE = 1

HERO_TEAM: List[Tuple[int, str]] = [
    (HeroType.Norgu.value, "OQBDAawDSvAIgcQ5ZkAFgZAEBA"),
    (HeroType.Gwen.value, "OQhkAsC8gFKzJIHM9MdDBcaG4iB"),
    (HeroType.Vekk.value, "OgVDI8gsS5AnATPmOHgCAZAFBA"),
    (HeroType.MasterOfWhispers.value, "OABDUshnSyBVBoBKgbhVVfCWCA"),
    (HeroType.Olias.value, "OAhjQoGYIP3hhWVVaO5EeDTqNA"),
    (HeroType.Razah.value, "OAWjMMgMJPYTr3jLcCNdmZgeAA"),
    (HeroType.Ogden.value, "OwUUMsG/E4SNgbE3N3ETfQgZAMEA"),
]

botting_tree: Optional[BottingTree] = None
initialized: bool = False


def _hero_party_already_formed(hero_ids: List[int]) -> bool:
    if not hero_ids:
        return False
    from Py4GWCoreLib import Party

    try:
        if not Map.IsOutpost() or not Party.IsPartyLoaded() or int(Party.GetPartySize() or 0) <= 1:
            return False
        existing_ids = {
            int(getattr(h, "hero_id", 0) or 0)
            for h in (Party.GetHeroes() or [])
            if int(getattr(h, "hero_id", 0) or 0) > 0
        }
        return all(int(hid) in existing_ids for hid in hero_ids)
    except Exception:
        return False


def _setup_hero_team() -> BehaviorTree:
    hero_ids = [hid for hid, _ in HERO_TEAM]
    skillbars = [(pos, tpl) for pos, (_, tpl) in enumerate(HERO_TEAM, start=1) if tpl]

    def _team_present(_node: BehaviorTree.Node) -> BehaviorTree.NodeState:
        return (
            BehaviorTree.NodeState.SUCCESS
            if _hero_party_already_formed(hero_ids)
            else BehaviorTree.NodeState.FAILURE
        )

    return BT.Selector(
        name="HeroPartySetup",
        children=[
            BehaviorTree(BehaviorTree.ActionNode(name="HeroTeamAlreadyFormed?", action_fn=_team_present)),
            BT.Sequence(
                name="Form Hero Team",
                children=[
                    BT.CreateParty(hero_ids=hero_ids, log=False),
                    BT.Wait(1000),
                    *[BT.LoadHeroSkillbar(pos, tpl) for pos, tpl in skillbars],
                    CoreBT.Party.ForceHeroState(HERO_AGGRESSIVE_MODE),
                ],
            ),
        ],
    )


def _ferndale_run() -> BehaviorTree:
    """Start at House zu Heltzer, clear Ferndale in HM, and resign back."""
    take_blessing_once = BehaviorTree(
        BehaviorTree.SelectorNode(
            name="Kurzick Blessing (One-Shot)",
            children=[
                BT.TakeBlessing(BLESSING_POS, faction="kurzick", multi_account=False).root,
                BehaviorTree.SucceederNode(name="Blessing Skipped/Consumed"),
            ],
        )
    )

    children: List[BehaviorTree] = [
        BT.Travel(target_map_id=OUTPOST_ID, random_travel=True, hard_mode=True),
        _setup_hero_team(),
        BT.EqualizeGold(target_gold=BLESSING_GOLD, log=True),
        *[BT.Move(pt, tolerance=150.0, log=True) for pt in OUTPOST_PRE_PATH],
        BT.MoveAndExitMap(EXIT_POS, target_map_id=EXPLORABLE_ID),
        ensure_botting_tree().Config.Aggressive(multi_account=False, auto_loot=True),
        take_blessing_once,
        BT.VanquishNode(
            steps=list(KILL_PATH),
            name="Ferndale Kill Path",
            flag_heroes_to_waypoint=False,
            log=True,
        ),
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
        log=True,
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
        enable_party_wipe_recovery=True,
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


def main() -> None:
    global initialized
    if not initialized:
        ensure_botting_tree()
        initialized = True
    tree = ensure_botting_tree()
    tree.tick()
    tree.UI.draw_window(
        icon_path=os.path.join(PySystem.Console.get_projects_path(), MODULE_ICON),
        main_child_dimensions=(420, 360),
    )


def tooltip() -> str:
    return MODULE_NAME + " - Ferndale Kurzick farm from House zu Heltzer with auto-donate."