import os
from pathlib import Path

def search_md():
    target_dir = Path("c:/Users/anton/Documents")
    print(f"Searching for .md files under {target_dir}...")
    
    found_files = []
    max_depth = 4
    
    for root, dirs, files in os.walk(target_dir):
        depth = len(Path(root).relative_to(target_dir).parts)
        if depth > max_depth:
            dirs[:] = []
            continue
            
        skip_dirs = {".venv", "node_modules", ".git", ".next", ".idea"}
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        
        for f in files:
            if f.endswith(".md"):
                found_files.append(Path(root) / f)
                
    print(f"\nFound {len(found_files)} markdown files:")
    for f in found_files[:50]:
        print(f"  {f}")
    if len(found_files) > 50:
        print(f"  ... and {len(found_files) - 50} more files")

if __name__ == "__main__":
    search_md()
