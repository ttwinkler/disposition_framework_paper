"""2 threads instead of sixteen - a point on the thread-scaling curve.

Not really about one run. The study sweeps 132 days, and those days are independent, so
the machine can either give one day all its cores or run several days at once with a
share each. Which is faster end to end depends entirely on how well a single solve scales
with threads, and branch and bound scales sublinearly: past a handful of threads the node
LPs contend for memory bandwidth and the extra workers mostly duplicate each other's work.

Measuring one day at 2, 4, 8 and 16 threads gives that curve directly, and the curve says
how many parallel workers the sweep should use - without spending hours running a sweep.
"""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, Threads=2)
