# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets>=12"]
# ///
"""Get an E.ON Next refresh token for the eon_next_prepay integration.

Opens Chrome/Chromium with a throwaway profile at the E.ON Next login page. You
log in yourself (including the CAPTCHA); once the dashboard loads, the refresh
token is read from the page's storage and the browser is closed.

Closing the browser without logging out matters: logging out revokes the token,
and a browser that keeps refreshing would race the integration for it.

Usage:  uv run tools/get_refresh_token.py [--browser PATH] [--print]
Then:   HA > Settings > Devices & services > E.ON Next Prepay > Reconfigure
        (or add the integration) and paste the token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

LOGIN_URL = "https://www.eonnext.com/dashboard/login"
BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
)
TIMEOUT = 600  # seconds to finish logging in
READ_TOKEN_JS = """(() => {
  for (const key of Object.keys(localStorage)) {
    if (!key.startsWith('@@auth0spajs@@')) continue;
    try {
      const token = JSON.parse(localStorage.getItem(key)).body?.refresh_token;
      if (token) return token;
    } catch (e) {}
  }
  return null;
})()"""


def find_browser(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in BROWSERS:
        if path := shutil.which(name):
            return path
    sys.exit(f"No Chromium-based browser found (tried {', '.join(BROWSERS)}); use --browser")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def dashboard_pages(port: int) -> list[dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as resp:
            targets = json.load(resp)
    except OSError:
        return []
    return [
        t
        for t in targets
        if t.get("type") == "page"
        and t.get("url", "").startswith("https://www.eonnext.com/dashboard")
        and "/login" not in t["url"]
        and "/redirect" not in t["url"]
    ]


async def read_token(ws_url: str) -> str | None:
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": READ_TOKEN_JS, "returnByValue": True},
                }
            )
        )
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == 1:
                return message.get("result", {}).get("result", {}).get("value")


def copy_to_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--browser", help="path to a Chromium-based browser")
    parser.add_argument("--print", action="store_true", help="print the token instead of copying it")
    args = parser.parse_args()

    browser = find_browser(args.browser)
    port = free_port()
    profile = tempfile.mkdtemp(prefix="eon-next-login-")
    proc = subprocess.Popen(
        [
            browser,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=http://127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            LOGIN_URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("Log in to E.ON Next in the browser window that just opened...")

    token = None
    try:
        deadline = time.monotonic() + TIMEOUT
        while token is None and time.monotonic() < deadline:
            if proc.poll() is not None:
                sys.exit("Browser closed before login finished")
            for page in dashboard_pages(port):
                token = asyncio.run(read_token(page["webSocketDebuggerUrl"]))
                if token:
                    break
            time.sleep(2)
    finally:
        # close without logging out so the token stays valid, and drop the profile
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    if not token:
        sys.exit("Timed out waiting for login")
    if not args.print and copy_to_clipboard(token):
        print(f"Refresh token copied to clipboard ({token[:4]}…{token[-4:]}).")
    else:
        print(token)
    print("Paste it into HA: Settings > Devices & services > E.ON Next Prepay > Reconfigure.")


if __name__ == "__main__":
    main()
