"""Everything that paid and keeps every feature: linearized aging weight, Gurobi's own
balanced MIPFocus instead of the crewed override, aggressive cuts, eight threads, and no
relaxed warm start.

The warm start is in here because dropping it *helped* - 1.21x on its own. It costs a
second to produce and hands over a schedule at a 30 % gap, and a mediocre incumbent that
early is not free: it anchors the heuristics and, with MIPFocus=1, gives them something to
polish instead of something to beat.
"""
import bench_core
import m_lindegrad

CONTAINER = m_lindegrad.CONTAINER


def apply(hdv):
    hdv.driver_warm_start = 'off'
    notes = bench_core.with_parameters(hdv, MIPFocus=0, Cuts=2, Threads=8)
    notes['base'] = 'm_lindegrad + no warm start'
    return notes
