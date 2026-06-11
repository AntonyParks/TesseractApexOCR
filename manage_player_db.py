"""
Player Database Management Tool
Run this after OCR sessions to review, merge, and clean player name data.
"""

import json
import time
from pathlib import Path
from difflib import SequenceMatcher

from config import PLAYER_DB_PATH, LEGEND_TYPO_DB_PATH


def load_database():
    """Load player database."""
    if not PLAYER_DB_PATH.exists():
        print("No player database found.")
        return {}

    with PLAYER_DB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_database(db):
    """Save player database."""
    with PLAYER_DB_PATH.open("w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def load_legend_typos():
    """Load legend typo database."""
    if not LEGEND_TYPO_DB_PATH.exists():
        return {}
    with LEGEND_TYPO_DB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def fuzzy_ratio(s1, s2):
    """Calculate similarity ratio between two strings."""
    return SequenceMatcher(None, s1.lower(), s2.lower()).ratio()


def show_stats(db):
    """Display database statistics."""
    print(f"\n{'=' * 70}")
    print("=== Player Database Statistics ===")
    print('=' * 70)

    players = {k: v for k, v in db.items() if not v.get("protected", False)}
    legends = {k: v for k, v in db.items() if v.get("protected", False)}

    print(f"\n📊 Overview:")
    print(f"  Total players: {len(players)}")
    print(f"  Total legends (protected): {len(legends)}")
    print(f"  Total player observations: {sum(d['total_seen'] for d in players.values())}")

    # Show legend typos
    legend_typos = load_legend_typos()
    print(f"\n📝 Legend Typo Mappings:")
    print(f"  Learned mappings: {len(legend_typos)}")
    if legend_typos:
        print("\n  Recent typo corrections:")
        for typo, correct in sorted(legend_typos.items())[:20]:
            print(f"    '{typo}' → '{correct}'")

    # Show players with multiple variants
    multi_variant = [(name, data) for name, data in players.items() if len(data['variants']) > 1]
    print(f"\n🔄 Data Quality:")
    print(f"  Players with multiple variants: {len(multi_variant)}")

    if multi_variant:
        print("\n  Top 10 players with most variants:")
        multi_variant.sort(key=lambda x: len(x[1]['variants']), reverse=True)
        for name, data in multi_variant[:10]:
            variant_count = len(data['variants'])
            print(f"    {name:25s} - {variant_count} variants, {data['total_seen']} sightings")

    # Show top players
    sorted_players = sorted(players.items(), key=lambda x: x[1]['total_seen'], reverse=True)
    print(f"\n🏆 Top 20 Most Seen Players:")
    for i, (name, data) in enumerate(sorted_players[:20], 1):
        variants = len(data['variants'])
        last_seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get('last_seen', 0)))
        print(f"  {i:2d}. {name:25s} - {data['total_seen']:4d} sightings, {variants:2d} variants (last: {last_seen})")


def review_variants(db):
    """Review and merge similar player names."""
    print("\n" + "=" * 70)
    print("=== Reviewing Similar Names ===")
    print("=" * 70)

    # Only review non-protected players
    players = {k: v for k, v in db.items() if not v.get("protected", False)}
    names = list(players.keys())
    potential_duplicates = []

    print("\n🔍 Scanning for potential duplicates...")

    for i, name1 in enumerate(names):
        for name2 in names[i + 1:]:
            ratio = fuzzy_ratio(name1, name2)
            if ratio > 0.85:  # Similar enough to review
                potential_duplicates.append((name1, name2, ratio))

    potential_duplicates.sort(key=lambda x: x[2], reverse=True)

    if not potential_duplicates:
        print("\n✅ No potential duplicates found!")
        return

    print(f"\n📋 Found {len(potential_duplicates)} potential duplicate pairs:\n")

    merged_count = 0
    skipped_count = 0

    for idx, (name1, name2, ratio) in enumerate(potential_duplicates, 1):
        # Skip if either name was already merged
        if name1 not in db or name2 not in db:
            continue

        data1 = db[name1]
        data2 = db[name2]

        print(f"\n{'─' * 70}")
        print(f"Pair {idx}/{len(potential_duplicates)} | Similarity: {ratio:.1%}")
        print(f"  1️⃣  {name1:30s} - seen {data1['total_seen']:3d}x")
        print(f"     Variants: {', '.join(list(data1['variants'].keys())[:5])}")
        print(f"  2️⃣  {name2:30s} - seen {data2['total_seen']:3d}x")
        print(f"     Variants: {', '.join(list(data2['variants'].keys())[:5])}")

        choice = input("\n  Merge? [1=keep first | 2=keep second | c=custom name | s=skip | q=quit]: ").strip().lower()

        if choice == "q":
            print("\n⏹️  Exiting review...")
            break

        elif choice == "1":
            # Merge data2 into data1
            db[name1]['total_seen'] += data2['total_seen']
            for variant, count in data2['variants'].items():
                db[name1]['variants'][variant] = db[name1]['variants'].get(variant, 0) + count
            db[name1]['last_seen'] = max(data1['last_seen'], data2['last_seen'])
            del db[name2]
            print(f"  ✅ Merged '{name2}' into '{name1}'")
            merged_count += 1

        elif choice == "2":
            # Merge data1 into data2
            db[name2]['total_seen'] += data1['total_seen']
            for variant, count in data1['variants'].items():
                db[name2]['variants'][variant] = db[name2]['variants'].get(variant, 0) + count
            db[name2]['last_seen'] = max(data1['last_seen'], data2['last_seen'])
            del db[name1]
            print(f"  ✅ Merged '{name1}' into '{name2}'")
            merged_count += 1

        elif choice == "c":
            # Custom name entry
            custom_name = input("  ✏️  Enter correct name: ").strip()

            if not custom_name:
                print("  ❌ Empty name entered. Skipping.")
                skipped_count += 1
                continue

            # Check if custom name already exists in database
            if custom_name in db:
                print(f"  ⚠️  '{custom_name}' already exists in database.")
                merge_into = input(f"  Merge both into existing '{custom_name}'? (y/n): ").strip().lower()

                if merge_into != "y":
                    print("  ⏭️  Skipped")
                    skipped_count += 1
                    continue

                # Merge both into existing custom name
                db[custom_name]['total_seen'] += data1['total_seen'] + data2['total_seen']
                for variant, count in data1['variants'].items():
                    db[custom_name]['variants'][variant] = db[custom_name]['variants'].get(variant, 0) + count
                for variant, count in data2['variants'].items():
                    db[custom_name]['variants'][variant] = db[custom_name]['variants'].get(variant, 0) + count
                db[custom_name]['last_seen'] = max(
                    db[custom_name]['last_seen'],
                    data1['last_seen'],
                    data2['last_seen']
                )
                del db[name1]
                del db[name2]
                print(f"  ✅ Merged '{name1}' and '{name2}' into existing '{custom_name}'")

            else:
                # Create new entry with custom name
                db[custom_name] = {
                    'variants': {},
                    'total_seen': data1['total_seen'] + data2['total_seen'],
                    'last_seen': max(data1['last_seen'], data2['last_seen']),
                    'protected': False
                }

                # Merge all variants from both entries
                for variant, count in data1['variants'].items():
                    db[custom_name]['variants'][variant] = count
                for variant, count in data2['variants'].items():
                    db[custom_name]['variants'][variant] = db[custom_name]['variants'].get(variant, 0) + count

                # Add the custom name as a variant too
                db[custom_name]['variants'][custom_name] = db[custom_name]['variants'].get(custom_name, 0) + 1

                # Delete old entries
                del db[name1]
                del db[name2]
                print(f"  ✅ Merged '{name1}' and '{name2}' into new name '{custom_name}'")

            merged_count += 1

        else:
            print("  ⏭️  Skipped")
            skipped_count += 1

    print(f"\n{'─' * 70}")
    print(f"📊 Review Summary:")
    print(f"  Merged: {merged_count}")
    print(f"  Skipped: {skipped_count}")
    print(f"  Remaining duplicates: {len(potential_duplicates) - merged_count - skipped_count}")


def export_clean_names(db):
    """Export clean list of player names."""
    output_path = Path("clean_player_names.txt")

    # Only export non-protected players
    players = {k: v for k, v in db.items() if not v.get("protected", False)}
    sorted_players = sorted(players.keys())

    with output_path.open("w", encoding="utf-8") as f:
        for name in sorted_players:
            f.write(f"{name}\n")

    print(f"\n✅ Exported {len(sorted_players)} player names to {output_path}")


def view_player_details(db):
    """View detailed information about a specific player."""
    players = {k: v for k, v in db.items() if not v.get("protected", False)}

    search = input("\n🔍 Enter player name to search: ").strip()

    if not search:
        print("❌ No search term entered.")
        return

    matches = []
    for name, data in players.items():
        if search.lower() in name.lower():
            matches.append((name, data))

    if not matches:
        print(f"❌ No matches found for '{search}'")
        return

    # Sort by relevance (exact match first, then by total_seen)
    matches.sort(key=lambda x: (search.lower() != x[0].lower(), -x[1]['total_seen']))

    print(f"\n✅ Found {len(matches)} match(es):\n")

    for name, data in matches[:10]:
        print(f"\n{'=' * 70}")
        print(f"👤 Player: {name}")
        print(f"📊 Total sightings: {data['total_seen']}")

        last_seen_ts = data.get('last_seen', 0)
        if last_seen_ts:
            last_seen_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_seen_ts))
            time_ago = time.time() - last_seen_ts
            hours_ago = int(time_ago / 3600)
            if hours_ago < 24:
                print(f"🕐 Last seen: {last_seen_str} ({hours_ago} hours ago)")
            else:
                days_ago = int(hours_ago / 24)
                print(f"🕐 Last seen: {last_seen_str} ({days_ago} days ago)")
        else:
            print(f"🕐 Last seen: Unknown")

        print(f"\n🔄 All variants ({len(data['variants'])}):")
        sorted_variants = sorted(data['variants'].items(), key=lambda x: x[1], reverse=True)
        for variant, count in sorted_variants:
            percentage = (count / data['total_seen']) * 100
            bar = '█' * int(percentage / 5)  # Simple bar chart
            print(f"  {variant:35s} - {count:3d}x ({percentage:5.1f}%) {bar}")

    if len(matches) > 10:
        print(f"\n... and {len(matches) - 10} more matches (showing top 10)")


def delete_player(db):
    """Delete a player from the database."""
    players = {k: v for k, v in db.items() if not v.get("protected", False)}

    search = input("\n🗑️  Enter player name to delete: ").strip()

    if not search:
        print("❌ No name entered.")
        return

    if search not in players:
        print(f"❌ Player '{search}' not found.")

        # Suggest similar names
        similar = []
        for name in players.keys():
            if fuzzy_ratio(search, name) > 0.7:
                similar.append(name)

        if similar:
            print(f"\n💡 Did you mean one of these?")
            for name in similar[:5]:
                print(f"  - {name}")
        return

    data = players[search]
    print(f"\n⚠️  About to delete:")
    print(f"  Player: {search}")
    print(f"  Sightings: {data['total_seen']}")
    print(f"  Variants: {len(data['variants'])}")

    confirm = input(f"\nAre you sure? Type 'DELETE' to confirm: ").strip()

    if confirm == "DELETE":
        del db[search]
        print(f"✅ Deleted '{search}' from database.")
    else:
        print("❌ Deletion canceled.")


def bulk_operations(db):
    """Perform bulk operations on the database."""
    print("\n" + "=" * 70)
    print("=== Bulk Operations ===")
    print("=" * 70)

    players = {k: v for k, v in db.items() if not v.get("protected", False)}

    print("\n1. 🧹 Remove players with fewer than X sightings")
    print("2. 🧹 Remove players not seen in X days")
    print("3. 🔙 Back to main menu")

    choice = input("\n➤ Choice: ").strip()

    if choice == "1":
        try:
            min_sightings = int(input("\n  Minimum sightings to keep: ").strip())
        except ValueError:
            print("❌ Invalid number.")
            return

        to_delete = [name for name, data in players.items() if data['total_seen'] < min_sightings]

        if not to_delete:
            print(f"\n✅ No players found with fewer than {min_sightings} sightings.")
            return

        print(f"\n⚠️  Found {len(to_delete)} players with fewer than {min_sightings} sightings.")
        confirm = input(f"Delete all? Type 'DELETE' to confirm: ").strip()

        if confirm == "DELETE":
            for name in to_delete:
                del db[name]
            print(f"✅ Deleted {len(to_delete)} players.")
            save_database(db)
        else:
            print("❌ Operation canceled.")

    elif choice == "2":
        try:
            days = int(input("\n  Remove players not seen in how many days: ").strip())
        except ValueError:
            print("❌ Invalid number.")
            return

        cutoff_time = time.time() - (days * 24 * 3600)
        to_delete = [name for name, data in players.items() if data.get('last_seen', 0) < cutoff_time]

        if not to_delete:
            print(f"\n✅ No players found not seen in {days} days.")
            return

        print(f"\n⚠️  Found {len(to_delete)} players not seen in {days} days.")
        confirm = input(f"Delete all? Type 'DELETE' to confirm: ").strip()

        if confirm == "DELETE":
            for name in to_delete:
                del db[name]
            print(f"✅ Deleted {len(to_delete)} players.")
            save_database(db)
        else:
            print("❌ Operation canceled.")


def main():
    print("\n" + "=" * 70)
    print("███╗   ███╗ █████╗ ███╗   ██╗ █████╗  ██████╗ ███████╗██████╗ ")
    print("████╗ ████║██╔══██╗████╗  ██║██╔══██╗██╔════╝ ██╔════╝██╔══██╗")
    print("██╔████╔██║███████║██╔██╗ ██║███████║██║  ███╗█████╗  ██████╔╝")
    print("██║╚██╔╝██║██╔══██║██║╚██╗██║██╔══██║██║   ██║██╔══╝  ██╔══██╗")
    print("██║ ╚═╝ ██║██║  ██║██║ ╚████║██║  ██║╚██████╔╝███████╗██║  ██║")
    print("╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝")
    print("=" * 70)
    print("Player Database Management Tool")
    print("=" * 70)

    db = load_database()

    if not db:
        return

    while True:
        print("\n" + "─" * 70)
        print("📋 Main Menu")
        print("─" * 70)
        print("1. 📊 Show statistics")
        print("2. 🔄 Review and merge similar names")
        print("3. 🔍 View player details")
        print("4. 📤 Export clean name list")
        print("5. 🗑️  Delete a player")
        print("6. 🧹 Bulk operations")
        print("7. 💾 Save and exit")
        print("8. 🚪 Exit without saving")

        choice = input("\n➤ Choice: ").strip()

        if choice == "1":
            show_stats(db)
        elif choice == "2":
            review_variants(db)
            save_database(db)
            print("\n💾 Changes saved.")
        elif choice == "3":
            view_player_details(db)
        elif choice == "4":
            export_clean_names(db)
        elif choice == "5":
            delete_player(db)
            save_database(db)
            print("\n💾 Changes saved.")
        elif choice == "6":
            bulk_operations(db)
        elif choice == "7":
            save_database(db)
            print("\n💾 Saved and exiting. Goodbye! 👋")
            break
        elif choice == "8":
            confirm = input("\n⚠️  Exit without saving? (y/n): ").strip().lower()
            if confirm == "y":
                print("\n🚪 Exiting without saving. Goodbye! 👋")
                break
        else:
            print("❌ Invalid choice. Please try again.")


if __name__ == "__main__":
    main()
