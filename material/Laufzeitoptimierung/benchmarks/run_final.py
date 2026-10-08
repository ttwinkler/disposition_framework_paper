"""The two measurements left, both on day 2: what the feature costs, and the gap curve.

1. r_socweight_off across seeds. The screening put it at 6.37x, but that was against the
   default-seed baseline, which replication showed to be a bad draw. The number is
   therefore known to be inflated and has to be re-measured against the seed median before
   it can be quoted as "what SoC-weighted aging costs".

2. The anytime curve: when each gap was first reached. That is the basis for any decision
   about optimization_MIPGap, which is the one lever that trades answer quality for time
   openly rather than trying to get the same answer sooner.
"""

import sys
import json
import time
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
SEEDS = [1, 7, 13]
DAY = 2


def main():
    out_path = HERE / f'results_day{DAY}_feature.jsonl'
    with out_path.open('w', encoding='utf-8') as sink:
        for name in ('baseline', 'r_socweight_off'):
            for seed in SEEDS:
                started = time.time()
                print(f"{name} seed {seed} ...", flush=True)
                proc = subprocess.run(
                    [sys.executable, '-u', '-X', 'utf8', str(HERE / 'bench_core.py'),
                     name, str(DAY), '1500', str(seed)],
                    capture_output=True, text=True, encoding='utf-8', errors='replace')
                line = next((l for l in proc.stdout.splitlines()
                             if l.startswith('@@JSON@@')), None)
                record = (json.loads(line.replace('@@JSON@@', '')) if line
                          else {'variant': f'{name}#{seed}', 'error': 'no output',
                                'stderr': proc.stderr[-600:]})
                record['family'] = name
                record['wall'] = time.time() - started
                sink.write(json.dumps(record, default=str) + '\n')
                sink.flush()
                print(f"    total {record.get('total', 0):.0f}s "
                      f"gap {record.get('gap')}", flush=True)

    print('\n--- anytime curve ---', flush=True)
    subprocess.run([sys.executable, '-u', '-X', 'utf8', str(HERE / 'anytime.py'),
                    str(DAY), '1500'])


if __name__ == '__main__':
    main()
