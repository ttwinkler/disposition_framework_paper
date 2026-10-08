"""Median and spread per option across seeds - the only fair way to read this study."""
import sys, json, pathlib, statistics

rows = []
for path in sys.argv[1:]:
    for line in pathlib.Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip():
            rows.append(json.loads(line))
fam = {}
for r in rows:
    fam.setdefault(r.get('family') or r['variant'], []).append(r)
base = sorted(r['total'] for r in fam.get('baseline', []))
base_med = statistics.median(base) if base else None

print(f"{'option':<16}{'n':>3}{'min':>7}{'median':>8}{'max':>7}{'spread':>8}"
      f"{'speed-up':>10}{'obj median':>12}{'d obj':>8}")
print('-' * 79)
base_obj = statistics.median([r['objective'] for r in fam['baseline']]) if base else None
for name, runs in sorted(fam.items(), key=lambda kv: statistics.median(r['total'] for r in kv[1])):
    t = sorted(r['total'] for r in runs)
    o = statistics.median([r['objective'] for r in runs if r.get('objective')])
    med = statistics.median(t)
    speed = f"{base_med/med:.2f}x" if base_med else '-'
    d = f"{(o-base_obj)/base_obj:+.2%}" if base_obj else ''
    print(f"{name:<16}{len(t):>3}{t[0]:>7.0f}{med:>8.0f}{t[-1]:>7.0f}"
          f"{(t[-1]-t[0])/t[0]:>7.0%}{speed:>10}{o:>12.0f}{d:>8}")
