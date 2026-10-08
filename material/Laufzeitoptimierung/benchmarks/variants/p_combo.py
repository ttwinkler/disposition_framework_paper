"""The three parameter options that paid, together.

m_bounds is deliberately not in here: it was the idea I expected most from and it came
back 20 % slower, so it has no place in a recommended setting however good the reasoning
behind it sounded.
"""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, MIPFocus=0, Cuts=2, Threads=8)
