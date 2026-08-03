package org.example.assistant;

import android.app.Activity;
import android.os.Looper;
import android.location.Location;

import com.google.android.gms.location.LocationCallback;
import com.google.android.gms.location.LocationRequest;
import com.google.android.gms.location.LocationResult;
import com.google.android.gms.location.LocationServices;
import com.google.android.gms.location.FusedLocationProviderClient;

/**
 * A REAL Java LocationListener/Callback. pyjnius dynamic proxies are
 * rejected by FusedLocationProviderClient.requestLocationUpdates with a
 * native JNI abort; a genuine compiled class is accepted.
 *
 * Python does not receive callbacks. It just polls hasFix()/getLat()/
 * getLng() periodically, which keeps the Java<->Python boundary trivial.
 */
public class LocationBridge {

    private static double lat = 0.0;
    private static double lng = 0.0;
    private static boolean haveFix = false;

    private static FusedLocationProviderClient client;
    private static LocationCallback callback;

    /** Called from Python (must run on the UI thread). */
    public static void start(Activity activity) {
        if (client != null) {
            return; // already started
        }
        client = LocationServices.getFusedLocationProviderClient(activity);

        LocationRequest req = LocationRequest.create();
        req.setInterval(3000);
        req.setFastestInterval(2000);
        req.setPriority(LocationRequest.PRIORITY_HIGH_ACCURACY);

        callback = new LocationCallback() {
            @Override
            public void onLocationResult(LocationResult result) {
                if (result == null) {
                    return;
                }
                Location loc = result.getLastLocation();
                if (loc != null) {
                    lat = loc.getLatitude();
                    lng = loc.getLongitude();
                    haveFix = true;
                }
            }
        };

        client.requestLocationUpdates(req, callback,
                                      Looper.getMainLooper());
    }

    public static boolean hasFix() {
        return haveFix;
    }

    public static double getLat() {
        return lat;
    }

    public static double getLng() {
        return lng;
    }
}
