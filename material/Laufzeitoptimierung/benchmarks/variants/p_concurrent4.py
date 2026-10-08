"""Four independent MIP solves in parallel, different seeds, first one to finish wins.

The seed replication showed the search path, not the formulation, is what dominates this
model's runtime: the same configuration ranges from 182 to 313 seconds across three seeds,
and the default seed happens to be the worst of them. No amount of reformulating fixes
that, because it is not a property of the model.

Concurrent MIP is the technique aimed at exactly this. Gurobi runs several independent
solves with different seeds and returns the first to hit the target, so the result is the
*minimum* over paths rather than a draw from them. The cost is that each copy gets a share
of the threads, so a lucky path is found with less horsepower behind it.
"""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, ConcurrentMIP=4)
