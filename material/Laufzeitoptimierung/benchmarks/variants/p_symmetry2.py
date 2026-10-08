"""Aggressive symmetry detection.

The fleet is five ice trucks identical in every coefficient and five bevs in three groups
by battery warranty. Any schedule can be permuted within a group, so the search can
explore whole subtrees that differ only in which interchangeable truck did what. The
model breaks this only with an objective tie-break - penalty_vehicle_id_order - which
orders *which* vehicles get used but not which of them gets which trip."""
import bench_core


def apply(hdv):
    return bench_core.with_parameters(hdv, Symmetry=2)
