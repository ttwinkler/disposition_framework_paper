"""Push the cut families that exist for the bilinear terms.

The model is a non-convex MIQCP, not a MILP: the SoC-weighted degradation multiplies
E_neg by soc_aging_w. RLT and BQP cuts are what tighten a spatial relaxation, and the log
shows both firing (RLT 42, BQP 3) at default effort."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, RLTCuts=2, BQPCuts=2, PSDCuts=2)
