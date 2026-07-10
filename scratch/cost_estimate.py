import csv
import sys
from pathlib import Path
from datetime import datetime

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

log = Path("killfeed_log.csv")
rows = list(csv.DictReader(log.open(encoding="utf-8", errors="replace")))

total = len(rows)
kills = [r for r in rows if r.get("event_type") == "Kill"]

low_conf = [
    r for r in kills
    if float(r.get("victim_conf", 1.0)) < 0.45 or float(r.get("attacker_conf", 1.0)) < 0.45
]

timestamps = [r["timestamp"] for r in rows if r.get("timestamp")]

first_ts = timestamps[0] if timestamps else None
last_ts  = timestamps[-1] if timestamps else None

if first_ts and last_ts:
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        t0 = datetime.strptime(first_ts, fmt)
        t1 = datetime.strptime(last_ts, fmt)
        hours = max((t1 - t0).total_seconds() / 3600, 0.001)
    except Exception:
        hours = None
else:
    hours = None

print(f"Total events logged:      {total:,}")
print(f"Kill events:              {len(kills):,}  ({100*len(kills)/total:.1f}% of all events)")
if kills:
    print(f"Low-conf kills (triggers Gemini): {len(low_conf):,}  ({100*len(low_conf)/len(kills):.1f}% of kills)")
print(f"First event: {first_ts}")
print(f"Last event:  {last_ts}")
if hours:
    print(f"Log spans:   {hours:.1f} hours")
    gemini_calls_per_hour = len(low_conf) / hours
    print(f"\nGemini calls/hour (raw):  {gemini_calls_per_hour:.1f}")

    # But the 5s global cooldown means max 12 calls/min = 720/hour
    effective_rate = min(gemini_calls_per_hour, 720)
    print(f"Gemini calls/hour (with 5s cooldown cap at 12/min): {effective_rate:.1f}")

    # ---- Pricing model: gemini-2.5-flash-lite ----
    # Input:  $0.10 / 1M tokens — each crop ~319 tokens (258 image + 61 text)
    # Output: $0.40 / 1M tokens — each response ~12 tokens
    INPUT_COST_PER_M  = 0.10
    OUTPUT_COST_PER_M = 0.40
    INPUT_TOKENS      = 319
    OUTPUT_TOKENS     = 12

    for label, hours_period in [("Per day (24h)", 24), ("Per week (168h)", 168)]:
        calls = effective_rate * hours_period
        input_cost  = calls * INPUT_TOKENS  / 1_000_000 * INPUT_COST_PER_M
        output_cost = calls * OUTPUT_TOKENS / 1_000_000 * OUTPUT_COST_PER_M
        total_cost  = input_cost + output_cost
        print(f"\n{label}:")
        print(f"  Gemini calls:    {calls:,.0f}")
        print(f"  Input cost:      ${input_cost:.4f}")
        print(f"  Output cost:     ${output_cost:.4f}")
        print(f"  Total:           ${total_cost:.4f}")

    # Also show raw (without cooldown) for comparison
    print("\n--- Without 5s cooldown (raw demand) ---")
    for label, hours_period in [("Per day", 24), ("Per week", 168)]:
        calls = gemini_calls_per_hour * hours_period
        input_cost  = calls * INPUT_TOKENS  / 1_000_000 * INPUT_COST_PER_M
        output_cost = calls * OUTPUT_TOKENS / 1_000_000 * OUTPUT_COST_PER_M
        total_cost  = input_cost + output_cost
        print(f"\n  {label}: {calls:,.0f} calls -> ${total_cost:.4f}")
