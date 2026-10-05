import urllib.request

from config import CFG

with urllib.request.urlopen(f"http://127.0.0.1:{CFG['port']}/api/health", timeout=4):
    pass
