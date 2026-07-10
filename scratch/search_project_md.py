import os
from pathlib import Path

def search_project_md():
    user_dir = Path("c:/Users/anton")
    print(f"Searching for project-related markdown files under {user_dir}...")
    
    found_files = []
    max_depth = 5
    
    keywords = {"tesseract", "apex", "ocr", "leaderboard", "killfeed"}
    
    for root, dirs, files in os.walk(user_dir):
        depth = len(Path(root).relative_to(user_dir).parts)
        if depth > max_depth:
            dirs[:] = []
            continue
            
        skip_dirs = {
            "AppData", "Local Settings", "My Documents", "NetHood", "PrintHood", 
            "Templates", "SendTo", "Start Menu", "Cookies", "Recent", "Application Data",
            ".venv", "node_modules", ".git", ".next", ".idea", "cloudflared"
        }
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        
        for f in files:
            if f.endswith(".md"):
                f_lower = f.lower()
                # Check if file name has keywords
                if any(kw in f_lower for kw in keywords):
                    found_files.append((Path(root) / f, "filename match"))
                    continue
                
                # Check content
                full_path = Path(root) / f
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as file_handle:
                        content = file_handle.read(2000).lower()  # read first 2000 chars
                        if any(kw in content for kw in keywords):
                            found_files.append((full_path, "content match"))
                except Exception:
                    pass
                    
    print(f"\nFound {len(found_files)} matches:")
    for path, match_type in found_files:
        print(f"  {path} ({match_type})")

if __name__ == "__main__":
    search_project_md()
