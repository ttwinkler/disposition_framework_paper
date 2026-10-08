"""The linearized aging weight, with the bound-focused setting on top.

Worth its own run because the two are not independent: MIPFocus governs how effort is
split between incumbent and bound, and m_lindegrad changes what closing the bound costs -
it turns a spatial branch-and-bound over 235 bilinear terms into ordinary MILP branching.
A setting tuned on the non-convex model need not be the right one for the linear one.
"""
import bench_core
import m_lindegrad

CONTAINER = m_lindegrad.CONTAINER


def apply(hdv):
    notes = bench_core.with_parameters(hdv, MIPFocus=0)
    notes['base'] = 'm_lindegrad'
    return notes
