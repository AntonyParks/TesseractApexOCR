import os
from pathlib import Path

# Add project root to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import db_log
    import elo_db
except Exception as e:
    print(f"Imports failed: {e}")

def wipe_file(filepath: Path):
    if filepath.exists():
        try:
            # Delete DB, WAL, and SHM files
            os.remove(filepath)
            print(f"[Wipe] Deleted: {filepath.name}")
        except Exception as e:
            print(f"[Error] Could not delete {filepath.name}: {e}")
            
    # Also delete WAL and SHM files
    for ext in ["-wal", "-shm"]:
        p = Path(str(filepath) + ext)
        if p.exists():
            try:
                os.remove(p)
                print(f"[Wipe] Deleted: {p.name}")
            except Exception as e:
                print(f"[Error] Could not delete {p.name}: {e}")

def wipe_dir(dirpath: Path):
    if dirpath.exists() and dirpath.is_dir():
        try:
            import shutil
            shutil.rmtree(dirpath)
            print(f"[Wipe] Deleted directory: {dirpath.name}")
        except Exception as e:
            print(f"[Error] Could not delete directory {dirpath.name}: {e}")

def main():
    print("=" * 80)
    # Target databases and directories
    killfeed_path = Path("killfeed.db")
    elo_path = Path("elo.db")
    crops_path = Path("crops")
    crops_noise_path = Path("crops_noise")
    gemini_corrections_path = Path("labels/gemini_corrections")
    gemini_confirmed_path = Path("labels/gemini_confirmed")
    
    print("[*] Scrubbing historical database and OCR crop/label records...")
    print("=" * 80)
    
    # 1. Wipe files and folders
    wipe_file(killfeed_path)
    wipe_file(elo_path)
    wipe_dir(crops_path)
    wipe_dir(crops_noise_path)
    wipe_dir(gemini_corrections_path)
    wipe_dir(gemini_confirmed_path)
    
    # 2. Re-initialize empty databases and folders
    print("\n[*] Re-initializing empty databases and directories...")
    try:
        db_log.init_db(str(killfeed_path))
        print("  - killfeed.db successfully re-created and initialized.")
    except Exception as e:
        print(f"  - [Error] Failed to initialize killfeed.db: {e}")
        
    try:
        elo_db.init_db(elo_path)
        print("  - elo.db successfully re-created and initialized.")
    except Exception as e:
        print(f"  - [Error] Failed to initialize elo.db: {e}")
        
    for folder in [crops_path, crops_noise_path, gemini_corrections_path, gemini_confirmed_path]:
        folder.mkdir(parents=True, exist_ok=True)
        print(f"  - {folder.name} successfully re-created empty.")
        
    print("\n[+] Success: All historical leaderboard, event, and crop data has been scrubbed.")
    print("=" * 80)

if __name__ == "__main__":
    main()
