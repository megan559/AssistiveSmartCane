[app]
title = Assistant
package.name = assistant
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 0.1

# kivy + http + plyer (gps/tts) + pyjnius (android speech) + oscpy
# (app <-> service messaging)
requirements = python3,kivy,requests,plyer,pyjnius,android,oscpy,zeroconf,certifi

orientation = portrait
fullscreen = 1

# Foreground service that runs service.py.
#   :foreground  -> startForeground + persistent notification (screen-off survival)
#   :sticky      -> Android restarts it if it gets killed
# NOTE: buildozer 1.6's bundled p4a ignores android.targetapi and clamps
# targetSdk to 34, and does NOT emit android:foregroundServiceType. On
# Android 14 that makes a :foreground service crash with
# MissingForegroundServiceTypeException. So for now this is a PLAIN
# (background) service: it does NOT survive screen-off, but it starts
# and the whole flow works with the app open. Re-add :foreground once
# the manifest service-type is sorted (newer p4a or a manifest patch).
services = waiter:service.py:sticky

# Permissions:
#   INTERNET                          - talk to the server
#   ACCESS_FINE/COARSE_LOCATION       - GPS for navigation
#   RECORD_AUDIO                      - microphone for speech-to-text
#   FOREGROUND_SERVICE(+_DATA_SYNC)   - run the long-poll with screen off
#   WAKE_LOCK                         - CPU stays awake between polls
#   REQUEST_IGNORE_BATTERY_OPTIMIZATIONS - pop the battery-exemption dialog
android.permissions = INTERNET,ACCESS_FINE_LOCATION,ACCESS_COARSE_LOCATION,RECORD_AUDIO,FOREGROUND_SERVICE,FOREGROUND_SERVICE_DATA_SYNC,WAKE_LOCK,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,CHANGE_WIFI_MULTICAST_STATE

# Force the TARGET sdk down. Android 14 (targetSdk 34) requires every
# foreground service to declare a foregroundServiceType in the manifest,
# which buildozer 1.6's bundled p4a does not emit -> the service crashes
# with MissingForegroundServiceTypeException. Targeting 28 makes Android
# use legacy foreground-service behaviour (no type required) and the
# service starts normally. Fine for a sideloaded personal app.
android.api = 33
android.minapi = 24
android.targetapi = 28
android.archs = arm64-v8a,armeabi-v7a
android.allow_backup = True

# Google Play Services location library: provides the Fused Location
# Provider (WiFi+cell+GPS, works indoors). Required by the fused
# LocationProvider in main.py.
android.gradle_dependencies = com.google.android.gms:play-services-location:21.3.0
android.enable_androidx = True

# Compile our real Java LocationListener (java/org/example/assistant/
# LocationBridge.java) into the app. Required because pyjnius dynamic
# proxies are rejected by requestLocationUpdates; a genuine compiled
# class is not. Path is relative to this spec / source.dir.
android.add_src = java

[buildozer]
log_level = 2
warn_on_root = 1
