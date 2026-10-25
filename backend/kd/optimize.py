"""Layer 4 · Optimization & Recommendation.

What-if: any driver override -> re-forecast (shortfall.forecast(mine, overrides)).

Prescriptive: a budget-constrained greedy knapsack over a catalogue of corrective actions.
Every candidate is scored by re-running the forecaster with that action applied, so the
recommendation is only as good as (and exactly consistent with) the prediction model.
Objective: minimise total expected month-end shortfall across mines, i.e. E[max(0, target - output)].
Constraints: budget, regulatory limits (monsoon blasting cap, EC annual production ceiling),
one instance of each action per mine, dumper donors must stay above their own target.
"""

from . import shortfall, storage
from .config import AS_OF, MINE_BY_ID, MINES

ACTIONS = [
    dict(key="maintenance", title="Expedite maintenance", category="Re-deploy equipment",
         detail="OEM crew + critical spares to restore excavator availability to 92% within 3 days",
         cost_lakh=12, applies=lambda m: True),
    dict(key="redeploy", title="Re-deploy dumpers", category="Re-deploy equipment",
         detail="Move 2 dumpers from the mine with the largest projected surplus",
         cost_lakh=3, applies=lambda m: True),
    dict(key="overtime", title="Overtime / extra shift crew", category="Adjust mine schedule",
         detail="Bring effective attendance to 100% for the rest of the period",
         cost_lakh=8, applies=lambda m: True),
    dict(key="blasting", title="Increase blasting frequency", category="Optimize blasting",
         detail="One extra blast round per day on dry days", cost_lakh=4, applies=lambda m: True),
    dict(key="dewatering", title="Additional dewatering pumps", category="Alternative extraction strategy",
         detail="Halve the effect of rain and waterlogging on pit operations",
         cost_lakh=6, applies=lambda m: m["method"] == "opencast"),
    dict(key="resequence", title="Re-sequence to higher-yield faces", category="Alternative extraction strategy",
         detail="Advance developed stopes / benches with better recovery (+3% output)",
         cost_lakh=5, applies=lambda m: True),
]
ACTION_BY_KEY = {a["key"]: a for a in ACTIONS}


def _regulatory():
    reg = storage.read_table("regulatory_limits").set_index("mine_id")
    md = (AS_OF.month, AS_OF.day)
    monsoon = (6, 15) <= md <= (9, 15)
    return reg, monsoon


def _overrides_for(mine_id, applied, state):
    """Translate the set of actions applied to a mine into forecaster overrides."""
    m = MINE_BY_ID[mine_id]
    o = {}
    base = state["base"][mine_id]
    if "maintenance" in applied:
        o.update(excavator_avail=0.92, equip_from_day=3)
    if "overtime" in applied:
        o["manpower_ratio"] = 1.0
    if "blasting" in applied:
        o["blasts"] = (2 if m["method"] == "opencast" else 3) + 1
    if "dewatering" in applied:
        o["rain_mult"] = 0.5
    if "resequence" in applied:
        o["extra_eff"] = 0.03
    delta = state["fleet_delta"].get(mine_id, 0)
    if delta:
        o["fleet_ratio"] = max(0.2, min(1.0, base["fleet_ratio"] + delta / m["dumpers"]))
    return o


def _evaluate(mine_id, applied, state):
    key = (mine_id, frozenset(applied), state["fleet_delta"].get(mine_id, 0))
    memo = state.setdefault("memo", {})
    if key not in memo:  # most candidates repeat between greedy rounds
        memo[key] = shortfall.forecast(mine_id, _overrides_for(mine_id, applied, state))["month"]
    return memo[key]


def recommend(budget_lakh=40.0, mines=None):
    mines = mines or [m["id"] for m in MINES]
    reg, monsoon = _regulatory()
    state = dict(base={}, fleet_delta={}, memo={})
    for mid in mines:
        f = shortfall.forecast(mid)
        state["base"][mid] = dict(fleet_ratio=f["daily"][0]["fleet_ratio"])
    applied = {mid: set() for mid in mines}
    current = {mid: _evaluate(mid, applied[mid], state) for mid in mines}
    before = {k: dict(v) for k, v in current.items()}
    plan, notes, spent = [], [], 0.0

    if monsoon:
        notes.append("Monsoon window: opencast blasting capped by regulatory limit; blasting actions skipped there.")

    while True:
        best = None
        for mid in mines:
            m = MINE_BY_ID[mid]
            if current[mid]["expected_shortfall_t"] < 1:
                continue
            for a in ACTIONS:
                if a["key"] in applied[mid] or not a["applies"](m) or spent + a["cost_lakh"] > budget_lakh:
                    continue
                if a["key"] == "blasting" and monsoon and m["method"] == "opencast":
                    continue
                trial_state = dict(base=state["base"], fleet_delta=dict(state["fleet_delta"]), memo=state["memo"])
                donor = None
                if a["key"] == "redeploy":
                    donors = [d for d in mines if d != mid and current[d]["projected_t"] - current[d]["target_t"] > 0
                              and MINE_BY_ID[d]["dumpers"] + trial_state["fleet_delta"].get(d, 0) > 4]
                    if not donors:
                        continue
                    donor = max(donors, key=lambda d: current[d]["projected_t"] - current[d]["target_t"])
                    trial_state["fleet_delta"][mid] = trial_state["fleet_delta"].get(mid, 0) + 2
                    trial_state["fleet_delta"][donor] = trial_state["fleet_delta"].get(donor, 0) - 2
                new = _evaluate(mid, applied[mid] | {a["key"]}, trial_state)
                gain = current[mid]["expected_shortfall_t"] - new["expected_shortfall_t"]
                donor_new = None
                if donor:
                    donor_new = _evaluate(donor, applied[donor], trial_state)
                    gain -= donor_new["expected_shortfall_t"] - current[donor]["expected_shortfall_t"]
                    if donor_new["projected_t"] < donor_new["target_t"]:
                        continue
                if gain <= 0:
                    continue
                score = gain / a["cost_lakh"]
                if best is None or score > best["score"]:
                    best = dict(score=score, mine=mid, action=a, gain=gain, new=new, donor=donor,
                                donor_new=donor_new, state=trial_state)
        if best is None:
            break
        mid, a = best["mine"], best["action"]
        applied[mid].add(a["key"])
        state = best["state"]
        current[mid] = best["new"]
        if best["donor"]:
            current[best["donor"]] = best["donor_new"]
        spent += a["cost_lakh"]
        plan.append(dict(
            step=len(plan) + 1, mine_id=mid, mine=MINE_BY_ID[mid]["name"], action=a["key"], title=a["title"],
            category=a["category"],
            detail=a["detail"] + (f" (donor: {MINE_BY_ID[best['donor']]['name']})" if best["donor"] else ""),
            cost_lakh=a["cost_lakh"], recovered_t=round(best["gain"]), t_per_lakh=round(best["score"], 1),
            prob_miss_after=round(best["new"]["prob_miss"], 3)))

    # EC ceiling: full-year projection plus what this plan adds must stay within the clearance.
    fy = {r["mine_id"]: r for r in shortfall.fiscal_summary()["mines"]}
    for mid in mines:
        cap = float(reg.loc[mid, "ec_cap_tpa"])
        projected = fy[mid]["fy_projected_t"] + current[mid]["projected_t"] - before[mid]["projected_t"]
        if projected > cap:
            notes.append(f"{MINE_BY_ID[mid]['name']}: FY projection {projected:,.0f} t would exceed EC cap {cap:,.0f} tpa.")

    def summary(d):
        return {mid: dict(target_t=v["target_t"], projected_t=round(v["projected_t"]), prob_miss=round(v["prob_miss"], 3),
                          expected_shortfall_t=round(v["expected_shortfall_t"])) for mid, v in d.items()}

    tb, ta = sum(v["expected_shortfall_t"] for v in before.values()), sum(v["expected_shortfall_t"] for v in current.values())
    return dict(as_of=str(AS_OF), month=str(AS_OF)[:7], budget_lakh=budget_lakh, spent_lakh=spent, plan=plan,
                before=summary(before), after=summary(current), expected_shortfall_before_t=round(tb),
                expected_shortfall_after_t=round(ta), recovered_t=round(tb - ta),
                constraints_checked=["budget", "monsoon blasting limit", "EC annual ceiling", "donor stays on target",
                                     "one instance of each action per mine"], notes=notes)


def whatif(mine_id, overrides):
    base = shortfall.forecast(mine_id)
    new = shortfall.forecast(mine_id, overrides)
    return dict(base=base, scenario=new, delta_month_t=new["month"]["projected_t"] - base["month"]["projected_t"],
                delta_horizon_t=new["horizon"]["forecast_t"] - base["horizon"]["forecast_t"])
