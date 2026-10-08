"""MIPFocus=0 and Cuts=2, without the thread change.

p_combo bundled three settings and the thread points turned out to be noise, so this
separates the two that have a reason behind them from the one that does not.
"""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, MIPFocus=0, Cuts=2)
