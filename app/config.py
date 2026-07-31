"""
Shared config + server discovery, used by BOTH main.py and service.py.

Instead of a hardcoded server IP, the phone discovers the server on the
local network via mDNS (the server advertises itself as the
'_assistant._tcp' service / 'assistant.local'). This means the IP can
change when you switch WiFi networks and the phone still finds it, with
no rebuild -- AS LONG AS the network allows multicast/mDNS between
devices. Many public/guest networks block this; FALLBACK_SERVER_URL is
the manual escape hatch for those.
"""

import socket
import threading

# ---- mDNS service identity (must match what server.py registers) ----
SERVICE_TYPE = "_assistant._tcp.local."
SERVICE_NAME = "assistant._assistant._tcp.local."

# ---- Manual override if a network blocks mDNS. Leave "" to rely on
#      discovery only. Example: "http://192.168.70.103:5000"
FALLBACK_SERVER_URL = ""

# ---- cross-process constants ----
WAIT_TIMEOUT = 30          # phone long-poll timeout (must be > server's)
OSC_PORT = 3001            # loopback port: service -> app press signal


def discover_server(timeout=4.0):
    """
    Active mDNS query for the server. Returns 'http://<ip>:<port>' or
    the fallback (or None). Safe to call repeatedly.
    """
    try:
        from zeroconf import Zeroconf
        zc = Zeroconf()
        try:
            info = zc.get_service_info(
                SERVICE_TYPE, SERVICE_NAME, timeout=int(timeout * 1000))
        finally:
            zc.close()
        if info and info.addresses:
            ip = socket.inet_ntoa(info.addresses[0])
            return f"http://{ip}:{info.port}"
    except Exception as e:
        print("mDNS discovery failed:", e)
    return FALLBACK_SERVER_URL or None


class ServerLocator:
    """
    Thread-safe holder for the discovered base URL. base_url() returns
    the current best URL (or None); refresh() re-runs discovery and is
    called again whenever a request fails so a network change recovers.
    """

    def __init__(self):
        self._url = None
        self._lock = threading.Lock()

    def base_url(self):
        with self._lock:
            return self._url

    def refresh(self, timeout=4.0):
        url = discover_server(timeout)
        with self._lock:
            if url:
                self._url = url
        return self._url


# single shared instance imported by main.py and service.py
locator = ServerLocator()
