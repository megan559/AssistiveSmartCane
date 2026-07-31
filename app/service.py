"""
Background foreground-service (python-for-android).

Holds the long-poll to the server's /wait so the ESP32 button works
with the screen off. Finds the server by mDNS (config.locator) rather
than a fixed IP. Runs in its own process, so it acquires its OWN WiFi
multicast lock and wake lock.
"""

import time
import requests

from oscpy.client import OSCClient

from config import locator, WAIT_TIMEOUT, OSC_PORT

osc = OSCClient("127.0.0.1", OSC_PORT)

_locks = []  # keep wake/multicast locks alive


def _service():
    from jnius import autoclass
    return autoclass("org.kivy.android.PythonService").mService


def acquire_locks():
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        service = _service()

        pm = service.getSystemService(Context.POWER_SERVICE)
        PowerManager = autoclass("android.os.PowerManager")
        wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                            "assistant::waiter")
        wl.acquire()
        _locks.append(wl)

        wifi = service.getSystemService(Context.WIFI_SERVICE)
        ml = wifi.createMulticastLock("assistant-mdns-svc")
        ml.setReferenceCounted(True)
        ml.acquire()
        _locks.append(ml)
        print("service locks acquired")
    except Exception as e:
        print("service locks failed:", e)


def main():
    acquire_locks()
    since = 0
    while True:
        base = locator.base_url()
        if not base:
            locator.refresh()
            time.sleep(3)
            continue
        try:
            r = requests.get(
                f"{base}/wait",
                params={"since": since},
                timeout=WAIT_TIMEOUT + 10,
            )
            data = r.json()
            since = data.get("count", since)
            if data.get("pressed"):
                osc.send_message(b"/press", [])
        except Exception as e:
            print("service wait error:", e)
            locator.refresh()       # network may have changed
            time.sleep(3)


if __name__ == "__main__":
    main()
