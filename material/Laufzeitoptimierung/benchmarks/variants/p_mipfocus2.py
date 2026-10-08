"""MIPFocus 2: work the dual bound instead of hunting incumbents.

The model sets MIPFocus=1 for a crewed fleet, reasoning that finding *any* feasible
schedule is the hard part. The log says otherwise once V2G is on: the incumbent is found
in the first seconds and then barely moves, while the bound crawls from 1389 to 1394 over
50 seconds. The gap that decides when the solve stops is being closed from the wrong end.
"""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, MIPFocus=2)
