"""Repeat the shortlist across random seeds, to separate a real saving from a lucky path.

Batch 2 made this necessary rather than optional. Running the same model on 2, 4, 8 and 16
threads gave 729, 812, 376 and 609 seconds - not a scaling curve, because scaling curves
are monotonic. What that spread actually shows is MIP performance variability: changing
anything that perturbs the search - thread count, a parameter, the seed - sends
branch-and-bound down a different path, and paths differ by far more than most of the
effects being measured here.

Gurobi is deterministic for a fixed seed and thread count, which is why the two baseline
runs agreed to 1 %. That agreement is not evidence the measurement is precise; it is
evidence it is *repeatable*. Repeating across seeds gives the number that matters: how
much of a claimed speed-up survives when the path changes.
"""

import sys
import json
import time
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
CAP_S = 1500
SEEDS = [1, 7, 13]
SHORTLIST = ['baseline', 'p_mipfocus0']


def main(day):
    out_path = HERE / f'results_day{day}_seeds.jsonl'
    jobs = [(name, seed) for name in SHORTLIST for seed in SEEDS]
    with out_path.open('w', encoding='utf-8') as sink:
        for index, (name, seed) in enumerate(jobs, 1):
            started = time.time()
            print(f"[{index}/{len(jobs)}] {name} seed {seed} ...", flush=True)
            proc = subprocess.run(
                [sys.executable, '-u', '-X', 'utf8', str(HERE / 'bench_core.py'),
                 name, str(day), str(CAP_S), str(seed)],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
            line = next((l for l in proc.stdout.splitlines()
                         if l.startswith('@@JSON@@')), None)
            record = (json.loads(line.replace('@@JSON@@', '')) if line
                      else {'variant': f'{name}#{seed}', 'error': 'no output',
                            'stderr': proc.stderr[-600:]})
            record['wall'] = time.time() - started
            record['family'] = name
            sink.write(json.dumps(record, default=str) + '\n')
            sink.flush()
            print(f"    total {record.get('total', 0):.0f}s  gap {record.get('gap')}  "
                  f"obj {record.get('objective')}", flush=True)
    print('written to', out_path)


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2)
