import os
import subprocess
import sys
import time


BASE_DIR = os.path.abspath(os.path.dirname(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bridge.log")
PID_FILE = os.path.join(BASE_DIR, "bridge.pid")
STOP_FILE = os.path.join(BASE_DIR, "bridge.stop")


def log_line(message):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        log.write(line)
        log.flush()


def run_child():
    log = open(LOG_FILE, "a", encoding="utf-8")
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            ["node", "server.js"],
            cwd=BASE_DIR,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    finally:
        log.close()


def main():
    log_line("bridge daemon starting")
    child = None
    while not os.path.exists(STOP_FILE):
        if child is None or child.poll() is not None:
            child = run_child()
            with open(PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(child.pid))
            log_line(f"child started pid={child.pid}")
        time.sleep(2)
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except Exception:
            child.kill()
    log_line("bridge daemon stopping")


if __name__ == "__main__":
    main()
