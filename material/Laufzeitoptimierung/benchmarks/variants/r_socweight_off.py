"""REFERENCE, NOT A RECOMMENDATION: switch the SoC weighting off.

This is the one knob that takes the model out of the non-convex class. soc_weight_factor
scales the aging of a discharged kWh by where in the SoC window it is taken from, and it
is carried as soc_aging_w * E_neg - a product of two continuous variables, 235 of them.
That is what makes Gurobi report "Solving non-convex MIQCP" and spend its time on spatial
branching, RLT and BQP cuts.

Setting the factor to zero makes the weight the constant 1.0, the products vanish and the
model is a MILP again. It also *removes a feature*, which is why this is measured rather
than recommended: the number it produces is the price the model currently pays for SoC-
weighted aging, and that price is the thing worth knowing before deciding what to do about
it. m_lindegrad keeps the feature and removes the non-convexity.
"""


def apply(hdv):
    hdv.soc_weight_factor = 0.0
    return {'idea': 'SoC aging weight off - MILP instead of non-convex MIQCP',
            'feature_loss': 'SoC-dependent aging weight (reference measurement only)'}
