"""MIPFocus 3: bound only. The extreme of p_mipfocus2 - worth separating, because
focusing entirely on the bound can starve the heuristics that keep the incumbent moving."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, MIPFocus=3)
