"""Aggressive presolve, aggregation and sparsification.

Presolve already removes about half the rows at default effort (8783 of 17338). Whether
pushing it harder pays for itself on a model this size is exactly the kind of thing that
has to be measured rather than assumed."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, Presolve=2, Aggregate=2, PreSparsify=1)
