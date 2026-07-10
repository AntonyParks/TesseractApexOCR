import os
from pathlib import Path

def search_obsidian():
    user_dir = Path("c:/Users/anton")
    print(f"Searching under {user_dir}...")
    
    # 1. Search for files named persistent.md (case-insensitive)
    found_files = []
    # 2. Search for directories named .obsidian
    obsidian_dirs = []
    
    # Limit search depth to avoid infinite scans or slow performance
    max_depth = 4
    
    for root, dirs, files in os.walk(user_dir):
        # Calculate depth
        depth = len(Path(root).relative_to(user_dir).parts)
        if depth > max_depth:
            # Clear dirs in-place to avoid going deeper
            dirs[:] = []
            continue
            
        # Avoid scanning system/cache directories
        skip_dirs = {
            "AppData", "Local Settings", "My Documents", "NetHood", "PrintHood", 
            "Templates", "SendTo", "Start Menu", "Cookies", "Recent", "Application Data",
            ".venv", "node_modules", ".git", "cloudflared", "PycharmProjects"
        }
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        
        # Check files
        for f in files:
            if f.lower() == "persistent.md":
                found_files.append(Path(root) / f)
                
        # Check if current directory has a .obsidian folder
        if ".obsidian" in dirs or os.path.exists(os.path.join(root, ".obsidian")):
            obsidian_dirs.append(Path(root))
            
    print("\n=== Search Results ===")
    print(f"Found {len(found_files)} files named 'persistent.md':")
    for f in found_files:
        print(f"  {f}")
        
    print(f"\nFound {len(obsidian_dirs)} directories containing '.obsidian':")
    for d in obsidian_dirs:
        print(f"  {d}")

if __name__ == "__main__":
    search_obsidian()
