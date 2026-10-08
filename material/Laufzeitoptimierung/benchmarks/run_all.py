"""Run every variant to the model's own 10 % target and record what it took.

Serially, one subprocess each. Serially because the point of the exercise is wall clock:
Gurobi is given every thread on the machine, so two runs at once would contend for the
same cores and both would be measured slower than they are. One subprocess each because a
variant patches module globals, and a patch that leaked into the next run would quietly
invalidate it.

The target is the model's configured MIPGap, so "runtime" means *time to the answer the
model would have returned anyway*, not time to some fixed budget. A variant that hits the
cap instead is reported by the gap it had got to.
"""

import io
import sys
import json
import time
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
CAP_S = 1200            # generous: the baseline needs 782 s, so a 1.5x regression still lands

ORDER = [
    'baseline',
    'p_combo',
    'c_lin_focus',
    'c_best',
    'p_threads4',
    'p_threads2',
    'baseline',
]


def main(day):
    # a suffix, so a second batch cannot truncate the first one's measurements
    suffix = sys.argv[2] if len(sys.argv) > 2 else ''
    out_path = HERE / f'results_day{day}{suffix}.jsonl'
    done = []
    with out_path.open('w', encoding='utf-8') as sink:
        for index, name in enumerate(ORDER, 1):
            started = time.time()
            print(f"[{index}/{len(ORDER)}] {name} ...", flush=True)
            proc = subprocess.run(
                [sys.executable, '-u', '-X', 'utf8', str(HERE / 'bench_core.py'),
                 name, str(day), str(CAP_S)],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
            line = next((l for l in proc.stdout.splitlines() if l.startswith('@@JSON@@')),
                        None)
            if line is None:
                record = {'variant': name, 'error': 'no output',
                          'stderr': proc.stderr[-600:]}
            else:
                record = json.loads(line.replace('@@JSON@@', ''))
            record['wall'] = time.time() - started
            record['repeat'] = sum(1 for d in done if d == name)
            done.append(name)
            sink.write(json.dumps(record, default=str) + '\n')
            sink.flush()
            print(f"    total {record.get('total', 0):.0f}s  "
                  f"gap {record.get('gap')}  obj {record.get('objective')}", flush=True)
    print('written to', out_path)


if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2)
