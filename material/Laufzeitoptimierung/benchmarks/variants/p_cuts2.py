"""Aggressive cuts. The bound is what is slow, and cuts are what move the bound."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, Cuts=2)
