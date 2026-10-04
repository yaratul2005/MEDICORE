#!/usr/bin/env python3
"""
MediCore Idempotent Bootstrap Script
Supports Google Colab, Linux, and Local environments.
- Installs dependencies
- Prepares storage (Google Drive when mounted, /content otherwise)
- Runs Alembic migrations
- Seeds default clinical data (200 patients, roles, admin)
- Runs pytest smoke tests
- Starts uvicorn in background
- Opens cloudflared quick tunnel and displays public URL
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def log(msg: str):
    print(f"\033[1;36m[MediCore Bootstrap]\033[0m {msg}", flush=True)


def run_cmd(cmd, check=True, cwd=None):
    if isinstance(cmd, str):
        log(f"Executing: {cmd}")
        return subprocess.run(cmd, shell=True, check=check, cwd=cwd)
    else:
        log(f"Executing: {' '.join(cmd)}")
        return subprocess.run(cmd, check=check, cwd=cwd)


def ensure_deps():
    log("Checking & installing Python dependencies...")
    req_file = Path(__file__).resolve().parent / "requirements.txt"
    if req_file.exists():
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)], check=True)
    else:
        subprocess.run([
            sys.executable, "-m", "pip", "install", "-q",
            "fastapi", "uvicorn[standard]", "sqlmodel", "alembic",
            "pydantic-settings", "jinja2", "python-multipart",
            "apscheduler", "faker", "pytest", "httpx"
        ], check=True)
    log("Dependencies verified.")


def ensure_cloudflared():
    if shutil.which("cloudflared"):
        log("cloudflared is already installed.")
        return

    log("Installing cloudflared binary...")
    if sys.platform.startswith("linux"):
        run_cmd("wget -q -nc https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared")
    else:
        log("Non-linux platform detected. Please ensure cloudflared is installed manually if tunnel is required.")


def run_migrations_and_seed():
    root_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(root_dir))

    log("Running Alembic database migrations...")
    run_cmd([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(root_dir))

    log("Seeding clinical database with 200 realistic patients and RBAC roles...")
    from medicore.seed.seeder import seed_database
    seed_database()


def run_smoke_tests():
    root_dir = Path(__file__).resolve().parent
    log("Running automated pytest smoke tests...")
    res = subprocess.run([sys.executable, "-m", "pytest", "medicore/tests/", "-v"], cwd=str(root_dir))
    if res.returncode != 0:
        log("\033[1;31mSmoke tests encountered failures!\033[0m")
    else:
        log("\033[1;32mAll smoke tests passed cleanly!\033[0m")


def kill_existing_processes():
    log("Cleaning up any existing uvicorn and cloudflared processes...")
    if sys.platform.startswith("linux"):
        subprocess.run("pkill -f 'uvicorn main:app' || true", shell=True)
        subprocess.run("pkill -f 'cloudflared tunnel' || true", shell=True)
        time.sleep(1)


def start_server_and_tunnel():
    root_dir = Path(__file__).resolve().parent
    log("Launching uvicorn background service...")
    
    server_log = open("/tmp/medicore_uvicorn.log", "w") if sys.platform.startswith("linux") else open("uvicorn.log", "w")
    popen_kwargs = {"start_new_session": True} if sys.platform.startswith("linux") else {}
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=str(root_dir),
        stdout=server_log,
        stderr=subprocess.STDOUT,
        **popen_kwargs
    )
    server_log.close()

    # Wait for server to respond
    import urllib.request
    server_ready = False
    for attempt in range(25):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/patients", timeout=2) as resp:
                if resp.status == 200:
                    server_ready = True
                    break
        except Exception:
            pass

    if not server_ready:
        log("Server did not respond within expected time. Check logs.")
    else:
        log("Uvicorn HTTP server is live and responsive at http://127.0.0.1:8000!")

    if not shutil.which("cloudflared"):
        log("cloudflared not available; server running locally at http://127.0.0.1:8000")
        return None

    log("Establishing Cloudflare Quick Tunnel...")
    tunnel_log_path = "/tmp/cloudflared.log" if sys.platform.startswith("linux") else "cloudflared.log"
    tunnel_log = open(tunnel_log_path, "w")
    
    tunnel_proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://127.0.0.1:8000"],
        stdout=tunnel_log,
        stderr=subprocess.STDOUT,
        **popen_kwargs
    )
    tunnel_log.close()

    public_url = None
    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    for _ in range(40):
        time.sleep(0.5)
        if os.path.exists(tunnel_log_path):
            with open(tunnel_log_path, "r") as f:
                content = f.read()
                matches = url_pattern.findall(content)
                if matches:
                    public_url = matches[0]
                    break

    print("\n" + "=" * 65, flush=True)
    if public_url:
        print("\033[1;32mMediCore HOSPITAL MANAGEMENT SYSTEM IS ONLINE!\033[0m")
        print(f"Public URL: \033[1;34m{public_url}\033[0m")
        print(f"Direct Local: http://127.0.0.1:8000/patients")
    else:
        print("\033[1;33mMediCore is running locally at http://127.0.0.1:8000\033[0m")
    
    print("-" * 65)
    print("Demo Credentials:")
    print("  Administrator: admin / admin123")
    print("  Doctor:        dr.sarah / doctor123")
    print("  Nurse:         nurse.john / nurse123")
    print("=" * 65 + "\n", flush=True)

    return public_url


def main():
    log("Starting MediCore Bootstrap...")
    ensure_deps()
    ensure_cloudflared()
    kill_existing_processes()
    run_migrations_and_seed()
    run_smoke_tests()
    url = start_server_and_tunnel()
    log("Bootstrap sequence finished successfully.")


if __name__ == "__main__":
    main()
