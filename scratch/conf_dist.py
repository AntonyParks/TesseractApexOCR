import csv, sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

rows = list(csv.DictReader(open('killfeed_log.csv', encoding='utf-8', errors='replace')))
kills = [r for r in rows if r.get('event_type') == 'Kill']

buckets = {'0.0-0.3': 0, '0.3-0.45': 0, '0.45-0.6': 0, '0.6-0.8': 0, '0.8-1.0': 0}
all_confs = []
for r in kills:
    vc = float(r.get('victim_conf', 1.0))
    ac = float(r.get('attacker_conf', 1.0))
    lowest = min(vc, ac)
    all_confs.append(lowest)
    if lowest < 0.3:
        buckets['0.0-0.3'] += 1
    elif lowest < 0.45:
        buckets['0.3-0.45'] += 1
    elif lowest < 0.6:
        buckets['0.45-0.6'] += 1
    elif lowest < 0.8:
        buckets['0.6-0.8'] += 1
    else:
        buckets['0.8-1.0'] += 1

print('Kill confidence distribution (lowest of attacker/victim):')
for b, n in buckets.items():
    pct = 100 * n / len(kills) if kills else 0
    bar = '#' * int(pct / 2)
    print(f'  {b}: {n:4d} ({pct:5.1f}%) {bar}')

print(f'\nTotal kills: {len(kills)}')
if all_confs:
    print(f'Average lowest conf: {sum(all_confs)/len(all_confs):.3f}')
    print(f'Min conf seen:       {min(all_confs):.3f}')

# Threshold analysis
for thresh in [0.3, 0.45, 0.6]:
    triggered = sum(1 for c in all_confs if c < thresh)
    print(f'Would trigger Gemini at conf < {thresh}: {triggered} ({100*triggered/len(kills):.1f}%)')
