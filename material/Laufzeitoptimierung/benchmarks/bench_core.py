"""Runtime assessment harness. Nothing here is imported by the model.

Every option under test is a *patch applied at run time* to a freshly imported copy of
src/hdv_disposition_optimization.py. The file on disk is never edited, and each variant
runs in its own process, so an option that corrupts module state cannot reach the next
one or the production code.

What is measured, per run:

    pre       seconds in run_optimization() before the model is built: loading the day,
              clustering locations, building chain candidates and the timetable
    build     seconds inside model_build(), summed over both calls (the crew warm start
              builds the model twice)
    warm      seconds inside the relaxed warm-start solve
    solve     seconds inside the final, crew-constrained solve
    post      seconds inside postprocess()
    total     wall clock for the whole run_optimization() call

and, so that a speed-up can be told apart from a change of answer:

    objective the objective value reached
    gap       the MIP gap it stopped at
    status    Gurobi status of the final solve
    vars/cons size of the final model

A variant that is faster but returns a different objective has not saved time, it has
solved a different problem. That distinction is the entire point of reporting both.
"""

import io
import json
import re
import sys
import time
import inspect
import pathlib
import contextlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))


def load_model():
    """A fresh import of the model module, with the benchmark defaults applied."""
    import hdv_disposition_optimization as hdv
    hdv.show_outputs = 'off'            # no figures, no csvs: they are not what is timed
    return hdv


# --- mirroring the model's own solver setup -------------------------------------------
#
# gurobipy.Model is a C type, so solve_model() - which configures the solver and calls
# optimize() in one go - cannot be intercepted between the two. The parameters it sets are
# therefore repeated here so a variant can override them. That is a copy, and a copy can
# drift, so it is checked against the function's own source before use: if the model gains
# or loses a setParam call the benchmark refuses to run, rather than quietly comparing
# against a configuration the model no longer has.

BASELINE_PARAMS = {'OutputFlag', 'LogToConsole', 'MIPGap', 'Threads', 'TimeLimit',
                   'Presolve', 'Aggregate', 'Cuts', 'Method', 'MIPFocus'}


def check_mirror(hdv):
    source = inspect.getsource(hdv.__dict__['solve_model'])
    found = set(re.findall(r"setParam\(\s*'([A-Za-z]+)'", source))
    if found != BASELINE_PARAMS:
        raise AssertionError(
            "solve_model() parameters have drifted from the benchmark mirror. "
            "only in the model: %s; only in the benchmark: %s"
            % (sorted(found - BASELINE_PARAMS), sorted(BASELINE_PARAMS - found)))


def baseline_parameters(hdv, model, gap, threads):
    model.setParam('OutputFlag', 0)
    model.setParam('LogToConsole', 0)
    model.setParam('MIPGap', gap)
    model.setParam('Threads', threads)
    model.setParam('TimeLimit', hdv.gp.GRB.INFINITY
                   if hdv.optimization_time_limit_s is None
                   else float(hdv.optimization_time_limit_s))
    model.setParam('Presolve', -1)
    model.setParam('Aggregate', 1)
    model.setParam('Cuts', -1)
    model.setParam('Method', -1)
    # read from the module rather than hard-coded, so this half of the mirror cannot drift
    # in *value* the way it could drift in *name*. check_mirror only compares the set of
    # parameter names; a literal here would have silently kept measuring against the old
    # MIPFocus=1 after the model changed to a parameter.
    model.setParam('MIPFocus', hdv.optimization_MIPFocus)


def with_parameters(hdv, **overrides):
    """Solve with the model's own parameters, plus these overrides and nothing else."""
    check_mirror(hdv)

    def solve(model, gap, threads):
        baseline_parameters(hdv, model, gap, threads)
        for name, value in overrides.items():
            model.setParam(name, value)
        model.optimize()

    hdv.solve_model = solve
    return dict(overrides)


def after_build(hdv, transform):
    """Let a variant alter the model model_build() produced, before it is solved.

    transform(model, crew_rules) is called on both builds - the relaxed warm-start model
    and the final one - so a change is applied consistently to the pair.
    """
    real_build = hdv.model_build

    def build(*a, **k):
        out = real_build(*a, **k)
        transform(out[0], k.get('crew_rules'))
        return out

    hdv.model_build = build


# --- measurement -----------------------------------------------------------------------
def instrument(hdv):
    """Wrap the phases so each reports its own wall clock."""
    timings = {'build': 0.0, 'warm': 0.0, 'solve': 0.0, 'post': 0.0}
    facts = {}
    real_build, real_solve, real_post = hdv.model_build, hdv.solve_model, hdv.postprocess
    state = {'solves': 0}

    def timed_build(*a, **k):
        start = time.perf_counter()
        out = real_build(*a, **k)
        timings['build'] += time.perf_counter() - start
        model = out[0]
        model.update()
        facts['vars'] = model.NumVars
        facts['cons'] = model.NumConstrs
        facts['ints'] = model.NumIntVars          # Gurobi counts binaries in here too
        facts['nzs'] = model.NumNZs
        # non-zero quadratic objective terms: > 0 means Gurobi solves this as a non-convex
        # MIQCP rather than a MILP, which is the single biggest fact about its runtime
        facts['qnzs'] = model.NumQNZs
        return out

    def timed_solve(model, *a, **k):
        start = time.perf_counter()
        out = real_solve(model, *a, **k)
        elapsed = time.perf_counter() - start
        state['solves'] += 1
        warm_first = (hdv.fleet_operation_mode == 'crewed' and hdv.driver_warm_start == 'on')
        timings['warm' if (warm_first and state['solves'] == 1) else 'solve'] += elapsed
        facts['status'] = model.Status
        facts['gap'] = None if model.SolCount == 0 else float(model.MIPGap)
        facts['objective'] = None if model.SolCount == 0 else float(model.ObjVal)
        facts['bound'] = float(model.ObjBound) if model.SolCount else None
        facts['nodes'] = float(model.NodeCount)
        return out

    def timed_post(*a, **k):
        start = time.perf_counter()
        out = real_post(*a, **k)
        timings['post'] += time.perf_counter() - start
        return out

    hdv.model_build, hdv.solve_model, hdv.postprocess = timed_build, timed_solve, timed_post
    return timings, facts


def add_seed(hdv, seed):
    """Re-run the same configuration down a different search path.

    Changing Seed changes nothing about the model or the answer it is allowed to return -
    only which of many equally valid branching and heuristic decisions Gurobi makes. The
    spread across seeds is therefore the noise floor of this whole study: a variant whose
    advantage is smaller than that spread has not been shown to be faster at all.

    Set before the variant's own solve, and never overridden by it, because neither the
    model's solve_model() nor any variant here sets Seed.
    """
    inner = hdv.solve_model

    def solve(model, gap, threads):
        model.setParam('Seed', seed)
        inner(model, gap, threads)

    hdv.solve_model = solve


def run_case(variant=None, day=1, time_limit=900, quiet=True, seed=None):
    """One full run_optimization() under an optional patch. Returns the measurements.

    `day` is a day_ID of results/trips.csv, not the model's re-indexed day: the window in
    order_data_days is moved onto it and the context rebuilt, so any day of the dataset
    can be used as an instance without touching the file on disk.
    """
    # a variant is a module: optional CONTAINER (source patches, applied to a private copy)
    # and optional apply(hdv) (parameter or post-build patches, applied to the live module)
    patches = getattr(variant, 'CONTAINER', None)
    hdv = make_container(variant.__name__, patches) if patches else load_model()
    with contextlib.redirect_stdout(io.StringIO()):
        hdv.order_data_days = [day, day]
        hdv.build_runtime_context()
    hdv.optimization_time_limit_s = time_limit
    notes = {}
    if variant is not None and hasattr(variant, 'apply'):
        notes = variant.apply(hdv) or {}
    if seed is not None:
        add_seed(hdv, seed)
        notes = dict(notes, seed=seed)
    timings, facts = instrument(hdv)

    sink = io.StringIO()
    start = time.perf_counter()
    error = None
    try:
        with contextlib.redirect_stdout(sink if quiet else sys.stdout):
            # scenario / scenario_year / v2g_status are sweep *lists*; the model's own main
            # block unpacks them with itertools.product. Passing a list through would make
            # 'on' == ['on'] false and silently benchmark a model with V2G switched off -
            # a different problem, and a far easier one.
            hdv.run_optimization((hdv.scenario[0], hdv.scenario_year[0],
                                  hdv.v2g_status[0], 1))
    except Exception as exc:                       # a variant that breaks must say so
        error = f"{type(exc).__name__}: {exc}"
    total = time.perf_counter() - start

    measured = {'total': total, **timings, **facts, 'notes': notes, 'error': error}
    # whatever is left is the preprocessing inside run_optimization
    measured['pre'] = total - sum(timings.values())
    return measured


# --- source-level containers -----------------------------------------------------------
def make_container(name, replacements):
    """Write a patched copy of the model into benchmarks/containers/<name>/ and import it.

    Parameter and post-build options can be applied to the live module, but an option that
    changes how the model is *formulated* has to reach inside model_build(). Rather than
    edit src/, the module is copied, the copy is patched textually, and the copy is
    imported from its own directory. src/hdv_disposition_optimization.py is never touched,
    and every replacement must match exactly once or the container refuses to build - a
    silently-unapplied patch would otherwise be reported as a variant that did nothing.
    """
    source_file = ROOT / 'src' / 'hdv_disposition_optimization.py'
    text = source_file.read_text(encoding='utf-8')

    # the copy lives elsewhere, so the two paths it derives from __file__ must be pinned
    # back to the real project, or it would look for data/ and results/ beside itself
    text = text.replace(
        "sys.path.insert(0, str(Path(__file__).resolve().parent))",
        f"sys.path.insert(0, {str(ROOT / 'src')!r})", 1)
    text = text.replace(
        "PROJECT_ROOT              = Path(__file__).resolve().parent.parent",
        f"PROJECT_ROOT              = Path({str(ROOT)!r})", 1)

    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise AssertionError(f"container {name}: anchor matched {count} times, not 1:\n"
                                 f"{old[:160]}")
        text = text.replace(old, new, 1)

    folder = ROOT / 'benchmarks' / 'containers' / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'hdv_disposition_optimization.py').write_text(text, encoding='utf-8')

    for module in ('hdv_disposition_optimization',):
        sys.modules.pop(module, None)
    sys.path.insert(0, str(folder))
    import hdv_disposition_optimization as patched
    if pathlib.Path(patched.__file__).parent != folder:
        raise AssertionError(f"container {name}: imported {patched.__file__}, not the copy")
    patched.show_outputs = 'off'
    return patched


if __name__ == '__main__':
    # One variant per process, so a module-level patch cannot leak into the next one.
    name, day, limit = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else None
    variant = None
    if name != 'baseline':
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / 'variants'))
        variant = __import__(name)
    measured = run_case(variant, day=day, time_limit=limit, seed=seed)
    measured['variant'] = name if seed is None else f"{name}#{seed}"
    print('@@JSON@@' + json.dumps(measured, default=str))
