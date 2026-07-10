import sqlite3
import subprocess
import sys
from pathlib import Path

# Add root folder to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.append(str(root_dir))

def main():
    db_path = Path("killfeed.db")
    if not db_path.exists():
        print("killfeed.db not found.")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Targets to replace with 'ash9009'
    typos = ['asbqde', 'asb00b', 'asf80be', 'ash909']

    print("Updating killfeed.db events...")
    for typo in typos:
        # Update attacker
        cursor.execute(
            "UPDATE events SET attacker = 'ash9009' WHERE attacker = ?",
            (typo,)
        )
        atk_updated = cursor.rowcount

        # Update victim
        cursor.execute(
            "UPDATE events SET victim = 'ash9009' WHERE victim = ?",
            (typo,)
        )
        vic_updated = cursor.rowcount

        print(f"  Merged typo '{typo}' -> 'ash9009' ({atk_updated} attacker, {vic_updated} victim rows updated)")

    conn.commit()
    conn.close()

    print("\nDatabase updated successfully.")

if __name__ == "__main__":
    main()
