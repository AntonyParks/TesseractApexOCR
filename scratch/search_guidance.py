import os
from pathlib import Path

def search_guidance():
    project_dir = Path(__file__).resolve().parent.parent
    print(f"Searching for guidance documents under {project_dir}...")
    
    found_files = []
    
    for root, dirs, files in os.walk(project_dir):
        # Avoid scanning virtual environments and node modules
        skip_dirs = {".venv", "node_modules", ".git", ".next", ".idea"}
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        
        for f in files:
            f_lower = f.lower()
            if f_lower in ["readme.md", "claude.md", "instructions.md", "guide.md", "guidance.md", "notes.md"]:
                found_files.append(Path(root) / f)
            elif "guide" in f_lower or "instruction" in f_lower or "read" in f_lower or "handover" in f_lower:
                if f.endswith(".md") or f.endswith(".txt"):
                    found_files.append(Path(root) / f)
                    
    print(f"\nFound {len(found_files)} matches:")
    for path in found_files:
        print(f"  {path}")

if __name__ == "__main__":
    search_guidance()
