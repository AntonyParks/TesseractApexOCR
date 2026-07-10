import os
import sys
from pathlib import Path
import requests
import pprint

# Load .env file
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    print("Error: GEMINI_API_KEY not set")
    sys.exit(1)

url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

try:
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    models = response.json().get("models", [])
    print("Available Models:")
    for model in models:
        print(f"- {model['name']} (supported methods: {model.get('supportedGenerationMethods')})")
except Exception as e:
    print(f"Error listing models: {e}")
