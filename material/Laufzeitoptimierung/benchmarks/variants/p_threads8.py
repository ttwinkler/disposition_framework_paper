"""Eight threads instead of sixteen.

The machine has 8 physical cores and 16 logical. The model asks for all 16. Branch and
bound is memory-bandwidth bound at the node LPs, and two hyperthreads sharing one core
share its cache and its load/store units, so the second thread of a pair often adds
contention rather than throughput."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, Threads=8)
