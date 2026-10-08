"""MIPFocus 0: Gurobi's balanced default, i.e. drop the model's crewed override entirely.
The control for p_mipfocus2/3 - it separates "2 is better than 1" from "anything is."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, MIPFocus=0)
