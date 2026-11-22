import os
import re
import glob
import time
import base64
import threading
from typing import Optional, Callable, Dict, Any


def find_lockfile(search_paths):
    for base in search_paths:
        if not os.path.exists(base):
            continue
        for pattern in [
            os.path.join(base, "*", "LeagueClient", "lockfile"),
            os.path.join(base, "*", "LeagueClient.exe"),
        ]:
            matches = glob.glob(pattern, recursive=True)
            if matches:
                if "lockfile" in pattern:
                    return matches[0]
                exe_dir = os.path.dirname(matches[0])
                lf = os.path.join(exe_dir, "lockfile")
                if os.path.exists(lf):
                    return lf
    return None


def find_leagueclient_dir(search_paths):
    for base in search_paths:
        if not os.path.exists(base):
            continue
        for pattern in [
            os.path.join(base, "*", "LeagueClient", "LeagueClient.exe"),
            os.path.join(base, "*", "LeagueClient.exe"),
        ]:
            for exe in glob.glob(pattern, recursive=True):
                return os.path.dirname(exe)
    return None


def parse_lockfile(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 5:
        return None
    name, pid, port, password, protocol = parts
    return {
        "name": name,
        "pid": int(pid),
        "port": int(port),
        "password": password,
        "protocol": protocol,
    }


def extract_auth_from_logs(lc_dir):
    if not lc_dir or not os.path.exists(lc_dir):
        return None
    lcu_url_pattern = re.compile(
        r'https://riot:([A-Za-z0-9_\-]+)@127\.0\.0\.1:(\d+)/'
    )
    log_files = sorted(
        glob.glob(os.path.join(lc_dir, "*_LeagueClientUx.log")),
        key=os.path.getmtime,
        reverse=True,
    )
    for lf in log_files[:5]:
        try:
            with open(lf, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = lcu_url_pattern.search(content)
            if match:
                return {
                    "port": int(match.group(2)),
                    "password": match.group(1),
                    "source": "log",
                }
        except Exception:
            continue
    return None


def discover_connection(search_paths):
    lf_path = find_lockfile(search_paths)
    if lf_path:
        info = parse_lockfile(lf_path)
        if info and info.get("protocol", "").lower() in ("https", "http"):
            info["source"] = "lockfile"
            return info
        lc_dir_from_lockfile = os.path.dirname(lf_path)
        info = extract_auth_from_logs(lc_dir_from_lockfile)
        if info:
            return info

    lc_dir = find_leagueclient_dir(search_paths)
    if lc_dir:
        info = extract_auth_from_logs(lc_dir)
        if info:
            return info

    return None


class LCUClient:
    def __init__(self, search_paths, on_event: Optional[Callable] = None):
        self.search_paths = search_paths
        self.on_event = on_event
        self.info = None
        self._http = None
        self._connected = False
        self._stop = threading.Event()

    @property
    def connected(self) -> bool:
        return self._connected and self.info is not None

    def get_http(self):
        if self._http is None or self._http.is_closed:
            raise RuntimeError("LCU not connected")
        return self._http

    async def connect(self, timeout: float = 15.0) -> bool:
        import asyncio
        import httpx

        deadline = time.time() + timeout
        last_info = None
        while time.time() < deadline:
            info = discover_connection(self.search_paths)
            if info:
                port_changed = last_info is None or info["port"] != last_info.get("port")
                last_info = info
                if port_changed or not self._connected:
                    self.info = info
                    auth = base64.b64encode(
                        f"riot:{info['password']}".encode()
                    ).decode()
                    base_url = f"https://127.0.0.1:{info['port']}"
                    self._http = httpx.AsyncClient(
                        base_url=base_url,
                        headers={"Authorization": f"Basic {auth}"},
                        verify=False,
                        timeout=httpx.Timeout(5.0),
                    )
                    try:
                        resp = await self._http.get("/help")
                        if resp.status_code < 500:
                            self._connected = True
                            return True
                    except Exception:
                        pass
                else:
                    self._connected = True
                    return True
            await asyncio.sleep(1)
        return False

    async def close(self):
        import asyncio
        self._stop.set()
        if self._http and not self._http.is_closed:
            await self._http.aclose()
        self._connected = False

    async def get(self, path: str) -> Optional[Any]:
        if not self.connected:
            return None
        try:
            resp = await self._http.get(path)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None

    async def wait_for_connection(self, poll_interval: float = 3.0):
        import asyncio

        while not self._stop.is_set():
            if self.connected:
                try:
                    resp = await self._http.get("/help")
                    if resp.status_code < 500:
                        return
                    self._connected = False
                    await self._http.aclose()
                    self._http = None
                except Exception:
                    self._connected = False
                    try:
                        await self._http.aclose()
                    except Exception:
                        pass
                    self._http = None

            if not self.connected:
                await self.connect(timeout=poll_interval)
            else:
                await asyncio.sleep(poll_interval)
