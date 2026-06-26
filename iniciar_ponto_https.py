import os
import runpy
import sys


os.environ["PONTO_HOST"] = "0.0.0.0"
os.environ["PONTO_PORT"] = "5443"
os.environ["PONTO_HTTPS"] = "1"

log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = open(os.path.join(log_dir, "https_server.log"), "a", encoding="utf-8", buffering=1)
sys.stdout = log_file
sys.stderr = log_file

runpy.run_path("app.py", run_name="__main__")
