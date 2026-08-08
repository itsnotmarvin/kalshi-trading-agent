import json
from collections import Counter

def parse_trades():
    trades = []
    analyses = []
    resolutions = []
    scans = 0

    with open("trades.jsonl", "r") as f:
        for line in f:
            try:
                entry = json.loads(line)
                etype = entry.get("type")
                if etype == "trade":
                    trades.append(entry)
                elif etype == "analysis":
                    analyses.append(entry)
                elif etype == "resolution":
                    resolutions.append(entry)
                elif etype == "scan":
                    scans += 1
            except Exception as e:
                pass

    print(f"Total Scans: {scans}")
    print(f"Total Analyses: {len(analyses)}")
    print(f"Total Trade Log Entries: {len(trades)}")
    print(f"Total Resolutions Log Entries: {len(resolutions)}")

    approved_trades = [t for t in trades if t.get("approved")]
    rejected_trades = [t for t in trades if not t.get("approved")]

    print(f"Approved Trades: {len(approved_trades)}")
    print(f"Rejected Trades: {len(rejected_trades)}")

    # Unique approved markets
    unique_approved = {}
    for t in approved_trades:
        mid = t.get("market_id")
        if mid not in unique_approved:
            unique_approved[mid] = t

    print("\n--- UNIQUE APPROVED TRADES ---")
    for mid, t in unique_approved.items():
        print(f"- {t.get('timestamp')[:10]}: {mid} | Direction: {t.get('direction')} | Price: {t.get('price')} | Size: ${t.get('size_usd')} | Edge: {t.get('edge'):.2f}")

    # Rejection reasons summary
    rejection_reasons = []
    for t in rejected_trades:
        rejection_reasons.extend(t.get("rejection_reasons", []))
    
    rejection_counts = Counter(rejection_reasons)
    print("\n--- REJECTION REASONS SUMMARY ---")
    for reason, count in rejection_counts.most_common():
        print(f"- {reason}: {count} times")

    # Categories from analyses
    categories = Counter([a.get("category", "unknown") for a in analyses])
    print("\n--- CATEGORIES ANALYZED ---")
    for cat, count in categories.most_common():
        print(f"- {cat}: {count} times")

if __name__ == "__main__":
    parse_trades()
