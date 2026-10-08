# Runtime assessment

A self-contained study of how long `src/hdv_disposition_optimization.py` takes and what
can be done about it. **Nothing in this directory is imported by the model, and nothing
here edits it.** Every option is applied to a private copy or to a freshly imported module
inside a throwaway process, so the production code is untouched and an option that breaks
something cannot reach it.

```
benchmarks/
  bench_core.py          the harness: phase timing, solver-parameter mirror, containers
  run_all.py             runs every variant to the model's own target, one process each
  report.py              turns the measurements into the comparison table
  anytime.py             the gap-against-time curve, for judging optimization_MIPGap
  variants/              one file per option, one idea per file
  containers/            generated: patched private copies of the model (safe to delete)
  run_seeds.py           repeats a shortlist across seeds - see the variability section
  run_final.py           the feature-cost re-measurement and the gap curve
  summarise.py           median and spread per option across seeds
  results_day*.jsonl     generated: the measurements
```

**The finding has been applied.** `src/hdv_disposition_optimization.py` now carries
`optimization_MIPFocus = 0` (section 1.3) and `solve_model()` reads it, so the measured
setting is the model's default. That means the `baseline` rows in the stored
`results_day*.jsonl` files describe the code *before* the change, and `p_mipfocus0` now
describes what the model already does - kept as they are, because a study that quietly
re-baselines itself after acting on its own result can no longer show what it changed.
Re-run `run_seeds.py` to get a table against the current code.

Nothing here needs to be kept in order to use the result; this directory is the evidence
for it and the means to re-check it when the model changes.


Reproduce with:

```bash
python benchmarks/run_all.py 2        # day_ID 2 of results/trips.csv
python benchmarks/report.py benchmarks/results_day2.jsonl
python benchmarks/anytime.py 2
```

## How an option is applied

Three mechanisms, in increasing order of intrusiveness. A variant uses the least intrusive
one that can express it.

**Solver parameters** — `bench_core.with_parameters(hdv, ...)`. `solve_model()` configures
the solver and calls `optimize()` in a single call, and `gurobipy.Model` is a C type whose
methods cannot be patched, so there is no way to slip a parameter in between. The
parameters it sets are therefore mirrored in `bench_core.baseline_parameters`, and the
mirror is checked against the function's own source (`check_mirror`) before every use: if
the model gains or loses a `setParam` call, the benchmark refuses to run rather than
quietly comparing against a configuration the model no longer has.

**Post-build changes** — `bench_core.after_build(hdv, transform)`. The transform is handed
the model `model_build()` produced, for both the relaxed warm-start build and the final
one. Enough for bounds and extra constraints. Note that Gurobi's updates are lazy, so a
transform must call `model.update()` before it can see the variables that were just added.

**Source containers** — a variant that has to change how the model is *formulated* sets
`CONTAINER = [(old, new), ...]`. The module is copied into `containers/<variant>/`, the
copy is patched textually, and the copy is imported from its own directory with its two
`__file__`-derived paths pinned back to the real project. Every replacement must match
exactly once or the container refuses to build — a patch that silently failed to apply
would otherwise be reported as an option that did nothing.

## What is measured

Per run: `pre` (loading the day, clustering locations, building chain candidates),
`build`, `warm` (the relaxed crew warm start), `solve`, `post`, and the wall clock.
Alongside them: the objective reached, the gap, the node count, and `qnzs` — the number of
non-zero quadratic objective terms, which is what decides whether Gurobi is solving a MILP
or a non-convex MIQCP.

Runtime is reported as **time to the model's own 10 % target**, not time to a fixed budget,
because that is what a run actually costs. A variant that hits the cap instead is reported
by the gap it reached, and is a regression.

**The objective column is not decoration.** A variant that is faster but returns a
different objective has not saved time, it has solved a different problem. Two of the
options here deliberately change what is being solved; they are marked, and the size of
the change is reported next to the saving so the trade is visible rather than implied.

## Method notes

The instance is `day_ID` 2 of `results/trips.csv` — the day the model is configured for —
with the full production configuration: V2G on, crewed fleet, advanced degradation,
`MIPGap = 0.1`. Runs are serial, because Gurobi is given every thread on the machine and
two runs at once would contend for the same cores. `baseline` is run twice, first and last,
so the run-to-run spread is on the table next to the differences being claimed from it.

## Results

Day 2 is the configured day, 13 trips. Day 95 is 15 trips and materially harder. Both run
the full production configuration: V2G on, crewed fleet, advanced degradation,
`MIPGap = 0.1`. Every figure is the median of three seeds, with the min-max range beside
it, because the baseline's own seed spread is 37-72 % and anything smaller than that has
not been demonstrated.

### Day 2 - median of seeds 1, 7, 13

| option | min | median | max | speed-up | keeps every feature |
| --- | --- | --- | --- | --- | --- |
| `p_mipfocus0` - do not set `MIPFocus=1` | 121 | **126** | 141 | **2.0x** | yes |
| `p_combo` - `MIPFocus=0` + `Cuts=2` + `Threads=8` | 122 | 145 | 166 | 1.8x | yes |
| `p_focus_cuts` - `MIPFocus=0` + `Cuts=2` | 158 | 160 | 165 | 1.6x | yes |
| `baseline` | 182 | 256 | 313 | 1.0x | - |
| `m_lindegrad` - aging weight linearized | 246 | 290 | 479 | 0.9x | yes |
| `p_concurrent2` | 332 | 374 | 1122 | 0.7x | yes |
| `p_concurrent4` | 693 | 748 | 1014 | 0.3x | yes |
| *`r_socweight_off`* - *reference only* | *57* | *85* | *102* | *3.2x* | **no** |

Screened on one seed and not carried forward, all at or below the noise floor once the
baseline's spread is accounted for: `p_symmetry2` (no effect at all - identical node count
and objective, so Gurobi was already detecting what there is to detect), `m_symlex`,
`p_norelheur`, `p_nonconvex_cuts`, `a_nowarm`, `m_bounds` (0.80x), `p_presolve2` (0.75x),
and the thread settings.

### Day 95 - the result that decides it

| option | seed 1 | seed 7 | seed 13 | converged |
| --- | --- | --- | --- | --- |
| `baseline` | 1508 s, gap 11.3 % | 1509 s, gap 10.6 % | 1508 s, gap 11.4 % | **0 / 3** |
| `p_mipfocus0` | 447 s | 425 s | 474 s | **3 / 3** |

The unmodified model does not reach its own 10 % target within 1500 s on this day, on any
seed. Removing `MIPFocus=1` converges every time in about seven minutes. That is the
strongest claim in this study and the one that generalises: two instances, three seeds
each, no overlap.

### The one-line change

`src/hdv_disposition_optimization.py:2915`:

```python
    if fleet_operation_mode == 'crewed':
        model.setParam('MIPFocus', 1)
```

The comment above it is sound about the problem it was written for - under the crew rules
alone, finding *any* feasible schedule is the hard part, and `MIPFocus=1` is the right
answer to that. It is no longer the problem. With V2G on, the incumbent arrives in the
first seconds and never moves far, while the dual bound crawls; the log shows the
incumbent fixed at 1565.18 while the bound goes 1389 -> 1394 over fifty seconds. The
setting is aimed at the half of the gap that is already closed.

The cost of the change is a slightly more expensive schedule - the bound-focused settings
close the gap partly by not polishing the incumbent, worth +0.6 % to +1 % on day 2. On day
95 it is the other way round: the baseline's cheaper-looking incumbents are *not certified*
within 10 %, and `p_mipfocus0`'s are.

### The gap curve

When each gap was first reached, day 2, default seed:

| gap | first reached |
| --- | --- |
| 20 % | 12 s |
| 15 % | 28 s |
| 12.5 % | 68 s |
| 11 % | 164 s |
| **10 %** | **877 s** |

**81 % of the runtime buys the last percentage point of gap.** Relaxing
`optimization_MIPGap` from 0.10 to 0.11 is worth ~5x, to 0.125 ~13x. This is not an
optimization - it returns a worse-certified schedule - but it is by far the cheapest
runtime on offer, and the curve is the honest basis for deciding whether the last point is
worth it for a 132-day sweep.

### What the non-convexity costs

The model is a non-convex MIQCP, not a MILP: `E_neg[m,t] * soc_aging_w[m,t]`, five bevs by
47 steps, is 235 bilinear objective terms (`hdv_disposition_optimization.py:2757`).
Switching the weighting off (`r_socweight_off`) makes it a MILP and runs 3.2x faster, so
that is what the feature costs.

`m_lindegrad` was the attempt to keep the feature and drop the class, by binning the SoC
and disaggregating `E_neg` across the bins. It works as designed - `qnzs` goes 235 -> 0 -
but it is **not faster**: 0.9x at the median, with a 95 % spread. Trading 235 bilinear
terms for 1880 binaries does not pay here. Recovering that 3.2x while keeping the feature
remains open; binning is not the way.

## Performance variability, and why every number here is a median of seeds

The most important methodological fact about this model is that its runtime is dominated
by *which search path branch-and-bound happens to take*, not by the formulation. The same
unmodified model, same machine, same everything but the random seed, takes 182, 256 and
313 seconds. That is a 72 % spread on the thing being measured.

This is easy to miss, and it was missed here at first. Gurobi is deterministic for a fixed
seed and thread count, so running the identical configuration twice agrees to ~1 %. That
agreement proves the harness is *repeatable*; it says nothing about whether the
measurement is *precise*, and only varying the seed can tell the two apart. The first two
batches of this study quoted speed-ups of 3-6x against a single-seed baseline. Replication
showed the default seed happens to be a bad draw for the baseline, and roughly half of
every headline number was that artefact rather than the option being measured:

| claimed on one seed | after replication across three |
| --- | --- |
| `p_combo` 5.67x | 1.76x |
| `m_lindegrad` 3.35x | 0.88x - slower |
| `p_threads8` 1.61x | noise; the 2/4/8/16-thread points are non-monotonic |

`run_seeds.py` exists because of this. Anything claimed from `run_all.py` alone should be
treated as a screening result, not a measurement, and a difference smaller than the
baseline's own seed spread has not been demonstrated at all.

One trap worth recording: `scenario`, `scenario_year` and `v2g_status` are sweep *lists*
that the model's own `__main__` unpacks with `itertools.product`. A harness that passes
them straight to `run_optimization` makes `v2g_status_iteration == 'on'` false and
benchmarks a model with V2G switched off — which is a different problem, and about forty
times easier. The harness unpacks them; an early version of it did not, and the resulting
20-second "baseline" was wrong by more than an order of magnitude.
