"""
Phone app (Kivy) for the blind-assistance assistant.

Server location is discovered via mDNS (config.locator) instead of a
hardcoded IP, so switching WiFi networks needs no rebuild -- provided
the network allows multicast. On Android, mDNS only works if a WiFi
multicast lock is held, so we acquire one.

Flow is unchanged: ESP32 button -> server /update -> service long-poll
released -> app listens -> STT -> /assist (+GPS) -> speak -> /navigate.
"""

import threading
import time
import requests

from kivy.app import App
from kivy.uix.button import Button
from kivy.clock import Clock
from kivy.utils import platform

from config import locator, WAIT_TIMEOUT, OSC_PORT

# ---------------- CONFIG ----------------
NAV_POLL_SECONDS = 8
DESKTOP_TEST_LOCATION = (25.1972, 55.2744)

ANDROID = platform == "android"

# keep a reference so the multicast lock isn't garbage-collected
_multicast_lock = None


def acquire_multicast_lock_activity():
    """Android drops multicast (mDNS) packets unless a lock is held."""
    global _multicast_lock
    try:
        from jnius import autoclass
        Context = autoclass("android.content.Context")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        activity = PythonActivity.mActivity
        wifi = activity.getSystemService(Context.WIFI_SERVICE)
        _multicast_lock = wifi.createMulticastLock("assistant-mdns")
        _multicast_lock.setReferenceCounted(True)
        _multicast_lock.acquire()
        print("multicast lock acquired")
    except Exception as e:
        print("multicast lock failed:", e)


# ---------------- LOCATION ----------------
# Uses Google Play Services' Fused Location Provider (the same mechanism
# Maps uses): WiFi + cell + GPS fused, so it returns a position INDOORS
# and gets a fix in ~1-2s. Falls back to plyer.gps if fused is somehow
# unavailable, and to a fixed coord on desktop so `python main.py` works.
class LocationProvider:
    def __init__(self):
        self.lat = None
        self.lng = None
        self._started = False
        self._listener = None  # keep a ref so it isn't GC'd

    def start(self):
        if self._started:
            return
        self._started = True
        if not ANDROID:
            self.lat, self.lng = DESKTOP_TEST_LOCATION
            return
        if not self._start_fused():
            self._start_plyer_fallback()

    # ---- Fused Location via real compiled Java class ----
    def _start_fused(self):
        try:
            from jnius import autoclass
            from android.runnable import run_on_ui_thread

            # Real Java class compiled into the APK (java/.../LocationBridge.java).
            # pyjnius dynamic proxies are rejected natively by
            # requestLocationUpdates; a genuine compiled LocationListener
            # is accepted. Python just POLLS this class for coordinates.
            self._bridge = autoclass("org.example.assistant.LocationBridge")
            PythonActivity = autoclass(
                "org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity

            @run_on_ui_thread
            def _start():
                self._bridge.start(activity)

            _start()

            # poll the Java side every 2s on a background thread
            def _poll():
                while True:
                    try:
                        if self._bridge.hasFix():
                            self.lat = self._bridge.getLat()
                            self.lng = self._bridge.getLng()
                    except Exception as e:
                        print("location poll error:", e)
                    time.sleep(2)

            threading.Thread(target=_poll, daemon=True).start()
            print("Fused location (Java bridge) started")
            return True
        except Exception as e:
            print("Fused location unavailable, falling back:", e)
            return False

    # ---- plyer GPS (fallback only) ----
    def _start_plyer_fallback(self):
        try:
            from plyer import gps
            gps.configure(on_location=self._on_plyer_location)
            gps.start(minTime=2000, minDistance=2)
            print("plyer GPS fallback started")
        except Exception as e:
            print("GPS start failed:", e)

    def _on_plyer_location(self, **kwargs):
        if "lat" in kwargs and "lon" in kwargs:
            self.lat = kwargs["lat"]
            self.lng = kwargs["lon"]

    def get(self):
        return self.lat, self.lng


# ---------------- TEXT TO SPEECH ----------------
def speak(text):
    if not text:
        return
    try:
        from plyer import tts
        tts.speak(str(text))
    except Exception as e:
        print("TTS:", text, "(", e, ")")


# ---------------- SPEECH TO TEXT ----------------
def recognize_speech(callback):
    if ANDROID:
        threading.Thread(target=_android_stt, args=(callback,),
                         daemon=True).start()
    else:
        _desktop_stt(callback)


def _android_stt(callback):
    try:
        from jnius import autoclass, PythonJavaClass, java_method
        from android.runnable import run_on_ui_thread

        SpeechRecognizer = autoclass('android.speech.SpeechRecognizer')
        RecognizerIntent = autoclass('android.speech.RecognizerIntent')
        Intent = autoclass('android.content.Intent')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')

        result_holder = {}
        done = threading.Event()

        class Listener(PythonJavaClass):
            __javainterfaces__ = ['android/speech/RecognitionListener']

            @java_method('(Landroid/os/Bundle;)V')
            def onResults(self, results):
                key = SpeechRecognizer.RESULTS_RECOGNITION
                arr = results.getStringArrayList(key)
                if arr and arr.size() > 0:
                    result_holder["text"] = arr.get(0)
                done.set()

            @java_method('(I)V')
            def onError(self, error):
                done.set()

            @java_method('(Landroid/os/Bundle;)V')
            def onReadyForSpeech(self, params): pass
            @java_method('()V')
            def onBeginningOfSpeech(self): pass
            @java_method('(F)V')
            def onRmsChanged(self, rms): pass
            @java_method('([B)V')
            def onBufferReceived(self, buf): pass
            @java_method('()V')
            def onEndOfSpeech(self): pass
            @java_method('(ILandroid/os/Bundle;)V')
            def onEvent(self, t, p): pass
            @java_method('(Landroid/os/Bundle;)V')
            def onPartialResults(self, p): pass

        @run_on_ui_thread
        def start_listening():
            recognizer = SpeechRecognizer.createSpeechRecognizer(
                PythonActivity.mActivity)
            recognizer.setRecognitionListener(Listener())
            intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
            intent.putExtra(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            recognizer.startListening(intent)

        start_listening()
        done.wait(timeout=12)
        callback(result_holder.get("text"))
    except Exception as e:
        print("Android STT failed:", e)
        callback(None)


def _desktop_stt(callback):
    from kivy.uix.popup import Popup
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.textinput import TextInput
    from kivy.uix.button import Button as Btn

    box = BoxLayout(orientation="vertical", spacing=10, padding=10)
    ti = TextInput(hint_text="Type what you'd say...", multiline=False)
    ok = Btn(text="Send", size_hint_y=0.4)
    box.add_widget(ti)
    box.add_widget(ok)
    popup = Popup(title="Desktop test input", content=box,
                  size_hint=(0.9, 0.5))

    def submit(_):
        popup.dismiss()
        callback(ti.text.strip() or None)

    ok.bind(on_release=submit)
    popup.open()


# ---------------- DESKTOP BUTTON WAITER ----------------
class DesktopButtonWaiter:
    def __init__(self, on_press):
        self.on_press = on_press
        self._run = True
        self._since = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._run = False

    def _loop(self):
        while self._run:
            base = locator.base_url()
            if not base:
                locator.refresh()
                time.sleep(2)
                continue
            try:
                r = requests.get(f"{base}/wait",
                                 params={"since": self._since},
                                 timeout=WAIT_TIMEOUT + 10)
                data = r.json()
                self._since = data.get("count", self._since)
                if data.get("pressed"):
                    Clock.schedule_once(lambda dt: self.on_press(), 0)
            except Exception as e:
                print("wait poll error:", e)
                locator.refresh()      # network may have changed
                time.sleep(3)


# ---------------- ANDROID INTEGRATION ----------------
def start_foreground_service():
    try:
        from jnius import autoclass
        service = autoclass("org.example.assistant.ServiceWaiter")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        service.start(activity, "")
        print("Foreground service started")
    except Exception as e:
        print("Could not start service:", e)


def request_battery_exemption():
    try:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Context = autoclass("android.content.Context")
        Settings = autoclass("android.provider.Settings")
        Uri = autoclass("android.net.Uri")
        Intent = autoclass("android.content.Intent")

        activity = PythonActivity.mActivity
        pkg = activity.getPackageName()
        pm = activity.getSystemService(Context.POWER_SERVICE)
        if not pm.isIgnoringBatteryOptimizations(pkg):
            intent = Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            intent.setData(Uri.parse("package:" + pkg))
            activity.startActivity(intent)
    except Exception as e:
        print("battery exemption request failed:", e)


# ---------------- APP ----------------
class AssistantApp(App):
    def build(self):
        self.location = LocationProvider()
        self.busy = False
        self.nav_session = None
        self.nav_event = None
        self.osc = None
        self.desktop_waiter = None

        self.btn = Button(
            text="Finding server...",
            font_size="34sp",
            halign="center",
            background_normal="",
            background_color=(0.05, 0.35, 0.85, 1),
            color=(1, 1, 1, 1)
        )
        self.btn.bind(on_release=self.trigger_listen)
        return self.btn

    def on_start(self):
        if ANDROID:
            self._request_runtime_permissions()
        else:
            self.location.start()
            threading.Thread(target=self._discover_then,
                             args=(self._start_desktop,), daemon=True).start()

    # ---- discovery ----
    def _discover_then(self, after):
        # mDNS needs the lock on Android; harmless to call only there
        if ANDROID:
            acquire_multicast_lock_activity()
        while locator.base_url() is None:
            if locator.refresh():
                break
            Clock.schedule_once(
                lambda dt: self.set_status("Searching for server..."), 0)
            time.sleep(4)
        Clock.schedule_once(
            lambda dt: self.set_status("Waiting for button\n(tap to speak)"),
            0)
        Clock.schedule_once(lambda dt: after(), 0)

    def _start_desktop(self):
        self.desktop_waiter = DesktopButtonWaiter(self.trigger_listen)
        self.desktop_waiter.start()

    # ---- android permission gate ----
    def _request_runtime_permissions(self):
        try:
            from android.permissions import (request_permissions,
                                              Permission)
            needed = [
                Permission.RECORD_AUDIO,
                Permission.ACCESS_FINE_LOCATION,
                Permission.ACCESS_COARSE_LOCATION,
            ]

            def after(perms, grants):
                Clock.schedule_once(lambda dt: self._post_perms(), 0)

            request_permissions(needed, after)
        except Exception as e:
            print("permission request failed:", e)
            self._post_perms()

    def _post_perms(self):
        if getattr(self, "_started_android", False):
            return
        self._started_android = True
        self.location.start()
        threading.Thread(target=self._discover_then,
                         args=(self._android_startup,), daemon=True).start()

    def _android_startup(self):
        self._start_osc_receiver()
        request_battery_exemption()
        start_foreground_service()

    def on_stop(self):
        if self.desktop_waiter:
            self.desktop_waiter.stop()

    # ---- OSC: service tells us the button was pressed ----
    def _start_osc_receiver(self):
        try:
            from oscpy.server import OSCThreadServer
            self.osc = OSCThreadServer()
            self.osc.listen(address="127.0.0.1", port=OSC_PORT,
                            default=True)

            @self.osc.address(b"/press")
            def _on_press(*args):
                Clock.schedule_once(lambda dt: self.trigger_listen(), 0)
        except Exception as e:
            print("OSC receiver failed:", e)

    # ---- trigger ----
    def trigger_listen(self, *_):
        if self.busy:
            return
        if locator.base_url() is None:
            speak("Still looking for the server.")
            return
        self.busy = True
        self.set_status("Listening...")
        speak("I'm listening.")
        recognize_speech(self._on_speech)

    def _on_speech(self, text):
        Clock.schedule_once(lambda dt: self._handle_speech(text), 0)

    def _handle_speech(self, text):
        if not text:
            speak("Sorry, I did not catch that.")
            self.reset()
            return
        self.set_status("Thinking...")
        threading.Thread(target=self._send_assist, args=(text,),
                         daemon=True).start()

    # ---- server calls ----
    def _send_assist(self, text):
        base = locator.base_url()
        lat, lng = self.location.get()
        payload = {"text": text, "lat": lat, "lng": lng}
        try:
            r = requests.post(f"{base}/assist", json=payload, timeout=20)
            data = r.json()
        except Exception as e:
            print("assist error:", e)
            locator.refresh()      # maybe the network/IP changed
            Clock.schedule_once(lambda dt: self._after_error(), 0)
            return
        Clock.schedule_once(lambda dt: self._after_assist(data), 0)

    def _after_error(self):
        speak("Sorry, I could not reach the server.")
        self.reset()

    def _after_assist(self, data):
        speak(data.get("speech", ""))
        if data.get("type") == "directions" and data.get("session"):
            self.nav_session = data["session"]
            self.set_status("Navigating")
            self.nav_event = Clock.schedule_interval(
                self._poll_navigate, NAV_POLL_SECONDS)
            self.busy = False
        elif data.get("needs_reply"):
            # the assistant asked a question and is waiting for an answer.
            # Reopen the mic automatically so the user doesn't have to
            # find the button again. Small delay so TTS isn't captured.
            self.busy = False
            self.set_status("Listening...")
            Clock.schedule_once(lambda dt: self._relisten(), 2.5)
        else:
            self.reset()

    def _relisten(self):
        if self.busy:
            return
        self.busy = True
        recognize_speech(self._on_speech)

    # ---- navigation polling ----
    def _poll_navigate(self, *_):
        if not self.nav_session:
            return False
        threading.Thread(target=self._do_navigate_request,
                         daemon=True).start()

    def _do_navigate_request(self):
        base = locator.base_url()
        lat, lng = self.location.get()
        payload = {"session": self.nav_session, "lat": lat, "lng": lng}
        try:
            r = requests.post(f"{base}/navigate", json=payload, timeout=20)
            data = r.json()
        except Exception as e:
            print("navigate error:", e)
            locator.refresh()
            return
        Clock.schedule_once(lambda dt: self._after_navigate(data), 0)

    def _after_navigate(self, data):
        if data.get("speak"):
            speak(data["speak"])
        if data.get("finished"):
            self._stop_navigation()

    def _stop_navigation(self):
        if self.nav_event:
            self.nav_event.cancel()
            self.nav_event = None
        self.nav_session = None
        self.reset()

    # ---- ui helpers ----
    def reset(self):
        self.busy = False
        self.set_status("Waiting for button\n(tap to speak)")

    def set_status(self, text):
        self.btn.text = text


if __name__ == "__main__":
    AssistantApp().run()
