"""The gap-against-time curve, for deciding what the last few percent are worth.

Every option measured elsewhere tries to reach the same answer sooner. This measures the
other axis: how good the answer already is, early. The solve is run once with a callback
that records the incumbent and the bound whenever either moves, which gives the time at
which each gap was first reached.

That curve is the honest basis for a decision about optimization_MIPGap. Raising the
target is not an optimization - it returns a worse schedule - but it is the cheapest
runtime there is, and the curve says exactly how much schedule is being given up for how
many seconds. It also shows which half of the gap is the slow one, which is what decides
whether a bound-focused or an incumbent-focused setting is the right lever.
"""

import io
import sys
import json
import pathlib
import contextlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench_core                                                  # noqa: E402


def apply(hdv, trace):
    import gurobipy as gp
    bench_core.check_mirror(hdv)
    state = {'last': None, 'final': False}

    def callback(model, where):
        if where != gp.GRB.Callback.MIP:
            return
        best = model.cbGet(gp.GRB.Callback.MIP_OBJBST)
        bound = model.cbGet(gp.GRB.Callback.MIP_OBJBND)
        if best >= gp.GRB.INFINITY:
            return
        gap = abs(best - bound) / abs(best) if best else None
        # only record when something actually moved, or the file is mostly noise
        marker = (round(best, 4), round(bound, 4))
        if marker == state['last']:
            return
        state['last'] = marker
        trace.append({'t': model.cbGet(gp.GRB.Callback.RUNTIME),
                      'incumbent': best, 'bound': bound, 'gap': gap,
                      'final': state['final']})

    def solve(model, gap, threads):
        bench_core.baseline_parameters(hdv, model, gap, threads)
        model.optimize(callback)
        state['final'] = True          # everything after this belongs to the final solve

    hdv.solve_model = solve
    return {}


def main(day=2, cap=1200):
    trace = []
    variant = type('anytime', (), {'apply': staticmethod(lambda hdv: apply(hdv, trace))})
    measured = bench_core.run_case(variant, day=day, time_limit=cap)

    # the final solve is the one that matters; the warm start is a different, easier model
    final = [p for p in trace if p['final']] or trace
    milestones = {}
    for point in final:
        if point['gap'] is None:
            continue
        for target in (0.40, 0.30, 0.25, 0.20, 0.15, 0.125, 0.11, 0.10):
            if point['gap'] <= target and target not in milestones:
                milestones[target] = point['t']

    out = {'day': day, 'measured': measured, 'milestones': milestones,
           'trace': final}
    (HERE / f'anytime_day{day}.json').write_text(json.dumps(out, default=str, indent=1),
                                                 encoding='utf-8')
    print(f"total {measured['total']:.0f} s, final gap {measured.get('gap')}")
    print('gap first reached at:')
    for target in sorted(milestones, reverse=True):
        print(f"  {target:>6.1%}   {milestones[target]:>8.1f} s")


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2,
         float(sys.argv[2]) if len(sys.argv) > 2 else 1200)
