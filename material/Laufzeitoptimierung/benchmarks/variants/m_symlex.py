"""Break the symmetry between interchangeable vehicles with a constraint, not a penalty.

The roster holds five ice trucks identical in every coefficient, and five bevs that fall
into three groups by battery warranty: {6,7}, {8}, {9,10}. Within a group the vehicles are
indistinguishable to the model, so every schedule exists once per permutation of the group
and the search can spend whole subtrees re-deriving the same day under a different name.

The model already prefers low ids through penalty_vehicle_id_order * (m-1) * y_m, but a
penalty only *prefers*: both orderings stay feasible and both get explored. Requiring
y_m[i] >= y_m[j] for consecutive members of a group makes the preference structural.

This cannot cut off an optimal solution. Within a group the vehicles are interchangeable,
so any solution can be relabelled to satisfy the ordering at identical cost - and because
the id penalty already makes the ordered labelling strictly cheaper, the solutions being
cut were never optimal to begin with.
"""
import bench_core

# the columns that make two vehicles interchangeable: if all of these agree, nothing in
# the model can tell the two apart
IDENTITY = ['vehicle_type', 'vehicle_consumption', 'vehicle_energy_storage',
            'vehicle_charging_power', 'vehicle_price', 'vehicle_battery_warranty']


def apply(hdv):
    fleet = hdv.fleet_dataset
    groups = {}
    for row in fleet.itertuples():
        key = tuple(str(getattr(row, column)) for column in IDENTITY)
        groups.setdefault(key, []).append(int(row.vehicle_id))

    pairs = []
    for members in groups.values():
        members.sort()
        pairs += list(zip(members, members[1:]))

    def add_ordering(model, _crew_rules):
        model.update()          # getVarByName needs the names flushed to the model first
        for lower, higher in pairs:
            a = model.getVarByName(f"y_m[{lower}]")
            b = model.getVarByName(f"y_m[{higher}]")
            if a is not None and b is not None:
                model.addLConstr(a >= b, name=f"symlex_{lower}_{higher}")
        model.update()

    bench_core.after_build(hdv, add_ordering)
    return {'idea': 'lexicographic use-order within interchangeable groups',
            'groups': [sorted(v) for v in groups.values()], 'pairs': len(pairs)}
