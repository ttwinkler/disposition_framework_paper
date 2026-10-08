"""Drop the crew warm start and see whether it is paying for itself.

The relaxed solve costs about a second here and hands over a schedule at a 30 % gap. The
question is whether a start that far from optimal helps the constrained solve or anchors
it: a poor incumbent still has to be improved away, and MIPFocus=1 plus a mediocre start
is exactly the combination that spends its time polishing the wrong solution.
"""


def apply(hdv):
    hdv.driver_warm_start = 'off'
    return {'idea': 'no relaxed warm start'}
