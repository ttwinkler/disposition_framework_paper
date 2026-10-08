"""Spend 30 s in the no-relaxation heuristic before branching.

Aimed at the opposite end from p_mipfocus2: if a better incumbent were available early,
the gap would close from above instead."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, NoRelHeurTime=30)
