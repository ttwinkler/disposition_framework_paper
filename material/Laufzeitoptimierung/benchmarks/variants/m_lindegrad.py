"""Keep the SoC-weighted aging, but carry it as a MILP instead of a bilinear objective.

This is the root cause of the model's difficulty. The aging of a discharged kWh is scaled
by where in the SoC window it was taken from, and that is written as

    weighted_thp = (E_neg / eta_dis) * soc_aging_w

with both factors variables. Five bevs times 47 steps is 235 such products, and Gurobi
reports exactly that: "Model has 235 quadratic objective terms ... Solving non-convex
MIQCP". A non-convex MIQCP is not a harder MILP, it is a different and much more expensive
class: the solver has to branch *spatially* on continuous variables and relax each product
with a McCormick envelope whose quality depends entirely on how tightly the two factors
are bounded.

The product of two continuous variables cannot be linearized exactly - one of the factors
has to be discretized. Here it is the SoC, which enters only through the weight:

    - the SoC window [0, 1] is split into K bins, one binary picking the bin the step
      starts in (exactly one, and the SoC has to lie inside it)
    - E_neg is disaggregated into a share per bin, all but the chosen one forced to zero
    - the weight of a bin is the parabola at its midpoint, a constant, so the objective
      term becomes a plain sum of constant x variable

The result is linear and exact *given the binning*. What changes is the shape of the
weight: the model carries it as the upper envelope of tangents to the parabola, this
carries it as a step function. The feature is intact - a kWh taken at the extremes of the
window still ages the battery more than one taken in the middle - but the numbers move a
little, so the objective is reported alongside the runtime rather than assumed equal.

The cost is 2 x K new variables per bev-step, half of them binary. Whether trading 235
bilinear terms for ~1900 binaries is a good trade is precisely what this measures; it is
entirely possible that it is not.
"""
import bench_core

BINS = 8

_ANCHOR = """                if t == 0 or soc_weight_factor == 0:
                    soc_w = soc_aging_weight(initial_soc_fraction if t == 0 else 0.5)
                else:
                    soc_w_var = model.addVar(lb=1.0, ub=1.0 + soc_weight_factor,
                                             name=f"soc_aging_w_{m}_{t}")
                    for x_i, w_i, slope_i in soc_weight_tangents:
                        model.addLConstr(
                            soc_w_var >= w_i + slope_i * (x_m_SoC[m, t-1] / cap - x_i))
                    soc_w = soc_w_var
                weighted_thp = throughput * soc_w"""

_PATCH = """                # --- benchmark container m_lindegrad: the same weighting, linearized ---
                if t == 0 or soc_weight_factor == 0 or v2g_status_iteration != 'on':
                    soc_w = soc_aging_weight(initial_soc_fraction if t == 0 else 0.5)
                    weighted_thp = throughput * soc_w
                else:
                    edges = [k / LINDEGRAD_BINS for k in range(LINDEGRAD_BINS + 1)]
                    pick = model.addVars(LINDEGRAD_BINS, vtype=gp.GRB.BINARY,
                                         name=f"soc_bin_{m}_{t}")
                    share = model.addVars(LINDEGRAD_BINS, lb=0.0,
                                          name=f"e_neg_bin_{m}_{t}")
                    model.addLConstr(pick.sum() == 1)
                    # the step starts inside exactly one bin
                    start_fraction = x_m_SoC[m, t-1] / cap
                    model.addLConstr(start_fraction >= gp.quicksum(
                        edges[k] * pick[k] for k in range(LINDEGRAD_BINS)))
                    model.addLConstr(start_fraction <= gp.quicksum(
                        edges[k+1] * pick[k] for k in range(LINDEGRAD_BINS)))
                    # ... and all of the discharge is booked against that bin
                    discharge_cap = vehicle_v2g_power[m] * STEP_HOURS
                    model.addLConstr(share.sum() == E_neg[m, t])
                    for k in range(LINDEGRAD_BINS):
                        model.addLConstr(share[k] <= discharge_cap * pick[k])
                    weighted_thp = gp.quicksum(
                        soc_aging_weight(0.5 * (edges[k] + edges[k+1])) * share[k]
                        for k in range(LINDEGRAD_BINS)) / discharging_efficiency"""

CONTAINER = [
    (_ANCHOR, _PATCH),
    # the bin count. Anchored on a whole line of its own, so the insertion cannot end up
    # swallowing the trailing comment of a parameter line
    ("def soc_aging_weight_tangents(weight_factor=None, breakpoints=None):",
     f"LINDEGRAD_BINS = {BINS}\n\n\n"
     "def soc_aging_weight_tangents(weight_factor=None, breakpoints=None):"),
]


def apply(hdv):
    return {'idea': 'SoC aging weight linearized by binning the SoC',
            'bins': BINS, 'removes': 'non-convex MIQCP -> MILP'}
