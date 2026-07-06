package com.cadence.agent.location

import android.annotation.SuppressLint
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.content.Context
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationCallback
import com.google.android.gms.location.LocationRequest
import com.google.android.gms.location.LocationResult
import com.google.android.gms.location.LocationServices

/**
 * Place-based presence context prior (consensus-plan.md S0.4 / Decision I): fused
 * location for geofence/place inference, plus BLE scan results as a *context* signal
 * ONLY — never person identity. **BLE MAC randomization on modern Android/iOS makes
 * passive person-identification via BLE beacons infeasible**; the plan's chosen
 * co-presence identity path is audio speaker-ID (office Pi / phone D2-trigger), not BLE.
 * This collector exists to feed place/context priors (e.g. "phone is near the office
 * Pi's fixed beacon" as an owner-presence signal for Decision I's hardware mic gate),
 * never to fingerprint nearby strangers.
 *
 * Requires `ACCESS_FINE_LOCATION` + `ACCESS_BACKGROUND_LOCATION` (location) and
 * `BLUETOOTH_SCAN` (BLE scanning, Android 12+).
 *
 * Docs: https://developer.android.com/reference/android/bluetooth/le/BluetoothLeScanner
 * Docs: https://developers.google.com/android/reference/com/google/android/gms/location/FusedLocationProviderClient
 */
class LocationBleCollector(context: Context) {

    private val fusedClient: FusedLocationProviderClient =
        LocationServices.getFusedLocationProviderClient(context)

    private var bleScanner: BluetoothLeScanner? = null

    private val locationCallback = object : LocationCallback() {
        override fun onLocationResult(result: LocationResult) {
            // TODO(device): resolve result.lastLocation to a geofence/place id (never a
            // raw lat/lon pair — see the raw-boundary note) and map via
            // EventMapper.fromLocationSample(placeId, confidence, timestampMillis,
            // deviceId, accountRef).
        }
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            // TODO(device): context-only signal (e.g. "near office Pi beacon" for
            // Decision I's owner-presence gate) — NEVER used to fingerprint/identify a
            // third party. Do not build passive person-ID from this callback.
        }

        override fun onScanFailed(errorCode: Int) {
            // TODO(device): log + backoff.
        }
    }

    /** Caller must hold ACCESS_FINE_LOCATION and/or ACCESS_BACKGROUND_LOCATION before calling. */
    @SuppressLint("MissingPermission")
    fun startLocation(request: LocationRequest) {
        // TODO(device): fusedClient.requestLocationUpdates(request, locationCallback,
        //   Looper.getMainLooper())
    }

    /** Caller must hold BLUETOOTH_SCAN before calling. */
    @SuppressLint("MissingPermission")
    fun startBleScan(scanner: BluetoothLeScanner) {
        bleScanner = scanner
        // TODO(device): scanner.startScan(filters, settings, scanCallback) — filters
        // should target only the office Pi's known beacon, not an open-ended scan.
    }

    fun stop() {
        fusedClient.removeLocationUpdates(locationCallback)
        bleScanner?.stopScan(scanCallback)
    }
}
