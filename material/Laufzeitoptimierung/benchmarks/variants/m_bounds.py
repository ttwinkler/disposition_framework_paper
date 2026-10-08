"""Give the energy variables the finite bounds the constraints already imply.

x_m_t_E, E_pos, E_neg, E_private, E_public and x_m_SoC are all declared with an infinite
upper bound. Every one of them is in fact bounded - by the truck's charging power, by the
inverter, by the battery - but only through constraints, and a bound reached through a
constraint is worth much less to a solver than a bound on the variable itself. Presolve
uses variable bounds to tighten coefficients and to derive implied bounds elsewhere;
bound-based cut generators need them; and the spatial branching this model needs for its
bilinear terms *requires* finite bounds on both factors, so an infinite one is filled in
with whatever the relaxation happens to allow.

Nothing here can change the optimal solution: each bound is a value the model already
proves. If any of them cut, the run would come back with a different objective - which is
why the objective is reported next to the runtime for every variant.
"""
import bench_core


def apply(hdv):
    fleet = hdv.fleet_dataset
    power = dict(zip(fleet['vehicle_id'], fleet['vehicle_charging_power']))
    storage = dict(zip(fleet['vehicle_id'], fleet['vehicle_energy_storage']))
    step_h = hdv.STEP_HOURS
    chg_eff, dis_eff = hdv.charging_efficiency, hdv.discharging_efficiency
    counted = {'n': 0}

    def vehicle_of(name):
        return int(name[name.index('[') + 1:name.index(',')])

    def tighten(model, _crew_rules):
        model.update()      # lazy updates: without this the new variables are not there yet
        for var in model.getVars():
            name = var.VarName
            try:
                if name.startswith('x_m_SoC['):
                    var.UB = float(storage[vehicle_of(name)])
                elif name.startswith(('E_pos[', 'E_private[', 'E_public[')):
                    var.UB = float(power[vehicle_of(name)]) * step_h
                elif name.startswith('E_neg['):
                    # v2g power is taken equal to charging power in the model (1668)
                    var.UB = float(power[vehicle_of(name)]) * step_h
                elif name.startswith('x_m_t_E['):
                    kw = float(power[vehicle_of(name)])
                    var.UB = chg_eff * kw * step_h          # what reaches the pack
                    var.LB = -kw * step_h / dis_eff         # what leaving it costs the pack
                else:
                    continue
            except (KeyError, ValueError):
                continue
            counted['n'] += 1
        model.update()

    bench_core.after_build(hdv, tighten)
    return {'idea': 'finite bounds on the energy variables', 'bounded': counted}
