"""Turn the measurement files into the table that answers the question.

Runtime is reported as time to the model's own 10 % target, because that is what a run
costs in practice. A variant that hit the cap instead has no such time, so it is reported
by the gap it reached - and it is a regression, not a saving.

The objective column is not decoration. Two of these options change what is being solved,
and a speed-up bought by solving something else is not a speed-up. Any variant whose
objective moves is flagged, and the size of the move is what decides whether the option is
usable.
"""

import sys
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
TARGET_GAP = 0.10


def load(path):
    rows = []
    for line in pathlib.Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def converged(row):
    gap = row.get('gap')
    return gap is not None and gap <= TARGET_GAP + 1e-6


def main(path):
    rows = load(path)
    base = [r for r in rows if r['variant'] == 'baseline' and converged(r)]
    base_time = sum(r['total'] for r in base) / len(base) if base else None
    base_obj = base[0]['objective'] if base else None

    print(f"baseline: {base_time:.0f} s" if base_time else "baseline did not converge")
    if len(base) > 1:
        spread = max(r['total'] for r in base) - min(r['total'] for r in base)
        print(f"  run-to-run spread over {len(base)} baseline runs: {spread:.0f} s "
              f"({spread / base_time:.0%})")
    print()
    header = (f"{'variant':<18}{'total s':>9}{'solve s':>9}{'speed-up':>10}"
              f"{'gap':>8}{'objective':>12}{'d obj':>9}{'nodes':>10}{'qnzs':>7}")
    print(header)
    print('-' * len(header))

    seen = set()
    for row in rows:
        name = row['variant']
        if name == 'baseline' and name in seen:
            name = 'baseline (repeat)'
        seen.add(row['variant'])
        if row.get('error'):
            print(f"{name:<18}  FAILED: {str(row['error'])[:70]}")
            continue
        gap = row.get('gap')
        obj = row.get('objective')
        if converged(row):
            speed = f"{base_time / row['total']:.2f}x" if base_time else '-'
        else:
            speed = 'cap hit'
        delta = '' if (obj is None or base_obj is None) else f"{(obj - base_obj) / base_obj:+.2%}"
        print(f"{name:<18}{row['total']:>9.0f}{row.get('solve', 0):>9.0f}{speed:>10}"
              f"{gap if gap is None else round(gap, 4):>8}{obj or 0:>12.1f}{delta:>9}"
              f"{row.get('nodes', 0):>10.0f}{row.get('qnzs', 0):>7}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else HERE / 'results_day2.jsonl')
