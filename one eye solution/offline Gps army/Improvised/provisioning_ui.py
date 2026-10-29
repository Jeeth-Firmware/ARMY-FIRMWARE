#!/usr/bin/env python3
"""
Lightweight Device Management UI for Raspberry Pi Zero
Access via browser: http://copilot.local:5000 (mDNS) or http://<pi-ip>:5000

Features:
- Vehicle registration (offline)
- Start/Stop Services
- Camera Preview
- WiFi Configuration

RAM Usage: ~20-30MB
"""

import os
import sys
import socket
import subprocess
import shutil
import threading
import time
import re
from typing import Optional, Tuple

# Minimal Flask import
from flask import Flask, render_template_string, jsonify, request, Response, send_file

from device_provisioning import get_vehicle_info, save_vehicle_info, factory_reset
from get_configure import get_configure, set_configure, DEFAULT_CONFIG
import db_helper
import gps_trip_manager

# Import WiFi AP Manager (try local first, then pi_control)
AP_MANAGER_AVAILABLE = False
WiFiAPManager = None
try:
    from wifi_ap_manager import WiFiAPManager, check_and_start_ap_if_needed
    AP_MANAGER_AVAILABLE = True
except ImportError:
    try:
        sys.path.insert(0, '/home/pi/pi_control')
        from wifi_ap_manager import WiFiAPManager, check_and_start_ap_if_needed
        AP_MANAGER_AVAILABLE = True
    except ImportError:
        pass

# Global AP manager instance
ap_manager = None
if AP_MANAGER_AVAILABLE:
    ap_manager = WiFiAPManager()

app = Flask(__name__)
EVENTS_DIR = os.getenv(
    "EVENTS_DIR",
    "/home/pi/facial-tracker-firmware/images/events",
)
# Shown in Events tab filter even before the first capture of each type.
KNOWN_DRIVER_EVENT_TYPES = ('Sleeping', 'Yawning', 'PhoneCall', 'LookingDown',
                            'LookingAway', 'NoFace', 'Overspeeding')
DEVICE_LOCAL_HOST = os.getenv("DEVICE_LOCAL_HOST", "copilot").strip().lower() or "copilot"
UI_PORT = int(os.getenv("PROVISIONING_UI_PORT", "5000"))


# All services to manage (shown in UI with individual start/stop/restart)
SERVICES = [
    {'name': 'facial1', 'display': 'Facial Tracking', 'description': 'Driver drowsiness detection'},
    {'name': 'get_gps_data1', 'display': 'GPS Tracking', 'description': 'GPS trip logging'},
    {'name': 'ota-auto-update', 'display': 'OTA Updates', 'description': 'Auto-update daemon'},
    {'name': 'pi-control', 'display': 'Pi Control', 'description': 'Remote device control'},
]

# Camera streaming globals
camera = None
camera_lock = threading.Lock()

# Minimal HTML template (embedded to avoid file I/O)
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Device Manager</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { 
            font-family: -apple-system, sans-serif; 
            background: #1a1a2e; 
            color: #eee;
            min-height: 100vh;
            padding: 15px;
            padding-bottom: 80px;
        }
        .container { max-width: 500px; margin: 0 auto; overflow-y: auto; }
        h1 { font-size: 1.3em; margin-bottom: 15px; text-align: center; }
        h2 { font-size: 1em; margin-bottom: 10px; color: #888; }
        .card {
            background: #16213e;
            border-radius: 12px;
            padding: 15px;
            margin-bottom: 12px;
        }
        .status { 
            display: flex; 
            align-items: center; 
            gap: 10px;
            margin-bottom: 10px;
        }
        .dot {
            width: 10px; height: 10px;
            border-radius: 50%;
            background: #ff6b6b;
            flex-shrink: 0;
        }
        .dot.ok { background: #51cf66; }
        .dot.warn { background: #fcc419; }
        .label { color: #888; font-size: 0.8em; margin-bottom: 4px; }
        .value { 
            font-family: monospace; 
            font-size: 0.85em;
            word-break: break-all;
            background: #0f0f23;
            padding: 6px;
            border-radius: 6px;
        }
        .btn {
            width: 100%;
            padding: 12px;
            border: none;
            border-radius: 8px;
            font-size: 0.9em;
            font-weight: 600;
            cursor: pointer;
            margin-top: 8px;
        }
        .btn-sm {
            width: auto;
            padding: 6px 10px;
            font-size: 0.8em;
            margin: 2px;
        }
        .btn-primary { background: #4c6ef5; color: white; }
        .btn-success { background: #2f9e44; color: white; }
        .btn-danger { background: #c92a2a; color: white; }
        .btn-secondary { background: #2d3748; color: #eee; }
        .btn:disabled { background: #555; cursor: not-allowed; opacity: 0.7; }
        .msg { 
            padding: 8px; 
            border-radius: 6px; 
            margin-top: 8px;
            font-size: 0.85em;
        }
        .msg.error { background: #c92a2a; }
        .msg.success { background: #2f9e44; }
        .tabs {
            display: flex;
            gap: 5px;
            margin-bottom: 15px;
            flex-wrap: wrap;
        }
        .tab {
            flex: 1;
            min-width: 80px;
            padding: 10px;
            background: #2d3748;
            border: none;
            border-radius: 8px;
            color: #888;
            cursor: pointer;
            font-size: 0.85em;
        }
        .tab.active { background: #4c6ef5; color: white; }
        .panel { display: none; }
        .panel.active { display: block; }
        .service-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #2d3748;
        }
        .service-row:last-child { border-bottom: none; }
        .service-info { flex: 1; }
        .service-name { font-weight: 600; font-size: 0.9em; }
        .service-desc { font-size: 0.75em; color: #888; }
        .service-actions { display: flex; gap: 5px; }
        .camera-container {
            background: #000;
            border-radius: 8px;
            overflow: hidden;
            text-align: center;
        }
        .camera-container img {
            width: 100%;
            height: auto;
            display: block;
        }
        .wifi-network {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px;
            background: #0f0f23;
            border-radius: 6px;
            margin-bottom: 8px;
            cursor: pointer;
        }
        .wifi-network:hover { background: #1a1a3e; }
        .wifi-signal { font-size: 0.8em; color: #888; }
        .status-pill {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.75em;
            font-weight: 600;
        }
        .status-pill.ok { background: #2f9e44; color: #fff; }
        .status-pill.bad { background: #c92a2a; color: #fff; }
        .status-pill.warn { background: #fab005; color: #111; }
        .telemetry-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-top: 10px;
        }
        .telemetry-card {
            background: linear-gradient(145deg, #111935, #0f1630);
            border: 1px solid #2b3a6a;
            border-radius: 10px;
            padding: 10px;
        }
        .telemetry-title {
            font-size: 0.72em;
            color: #92a0c9;
            margin-bottom: 5px;
            letter-spacing: 0.2px;
        }
        .telemetry-value {
            font-size: 1.05em;
            font-weight: 700;
            color: #ffffff;
            font-family: monospace;
        }
        .telemetry-meta {
            margin-top: 10px;
            border-top: 1px solid #2d3748;
            padding-top: 10px;
            font-size: 0.8em;
            color: #a8b3d1;
            line-height: 1.5;
        }
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 10px;
            border: 1px solid #2d3748;
            border-radius: 6px;
            background: #0f0f23;
            color: #eee;
            font-size: 0.9em;
            margin-bottom: 8px;
        }
        .info { font-size: 0.75em; color: #666; margin-top: 10px; text-align: center; }
        .events-toolbar {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            margin-bottom: 12px;
        }
        .events-filter {
            flex: 1;
            min-width: 140px;
            padding: 8px 10px;
            border: 1px solid #2d3748;
            border-radius: 6px;
            background: #0f0f23;
            color: #eee;
            font-size: 0.85em;
        }
        .events-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
            gap: 10px;
        }
        .event-card {
            background: #0f0f23;
            border: 1px solid #2d3748;
            border-radius: 8px;
            overflow: hidden;
            cursor: pointer;
            transition: border-color 0.2s, transform 0.15s;
        }
        .event-card:hover {
            border-color: #4c6ef5;
            transform: translateY(-2px);
        }
        .event-card img {
            width: 100%;
            aspect-ratio: 4/3;
            object-fit: cover;
            display: block;
            background: #000;
        }
        .event-card-meta {
            padding: 8px;
            font-size: 0.72em;
        }
        .event-card-actions {
            display: flex;
            justify-content: flex-end;
            margin-top: 6px;
        }
        .event-delete-btn {
            padding: 4px 8px;
            font-size: 0.85em;
            border: none;
            border-radius: 4px;
            background: #c92a2a;
            color: #fff;
            cursor: pointer;
        }
        .event-delete-btn:hover {
            background: #e03131;
        }
        .event-type-badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 4px;
            background: #364fc7;
            color: #fff;
            font-weight: 600;
            margin-bottom: 4px;
        }
        .event-type-badge.badge-sleeping { background: #7048e8; }
        .event-type-badge.badge-yawning { background: #f59f00; color: #111; }
        .event-type-badge.badge-phonecall { background: #0c8599; }
        .event-type-badge.badge-lookingdown { background: #d9480f; }
        .event-type-badge.badge-lookingaway { background: #c2255c; }
        .event-type-badge.badge-noface { background: #364fc7; }
        .event-type-badge.badge-overspeeding { background: #c92a2a; }
        .event-modal {
            display: none;
            position: fixed;
            inset: 0;
            z-index: 1000;
            background: rgba(0, 0, 0, 0.88);
            align-items: center;
            justify-content: center;
            padding: 16px;
        }
        .event-modal.open { display: flex; }
        .event-modal-inner {
            max-width: 100%;
            max-height: 100%;
            text-align: center;
        }
        .event-modal-inner img {
            max-width: 100%;
            max-height: 78vh;
            border-radius: 8px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
        }
        .event-modal-caption {
            margin-top: 10px;
            color: #a8b3d1;
            font-size: 0.85em;
        }
        .event-modal-close {
            position: fixed;
            top: 12px;
            right: 16px;
            background: #2d3748;
            border: none;
            color: #fff;
            font-size: 1.4em;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            cursor: pointer;
            z-index: 1001;
        }
        .events-empty {
            color: #888;
            font-size: 0.85em;
            padding: 24px;
            text-align: center;
        }
        .trip-layout {
            display: grid;
            grid-template-columns: 170px 1fr;
            gap: 10px;
        }
        .trip-list {
            max-height: 360px;
            overflow-y: auto;
        }
        .trip-item {
            width: 100%;
            text-align: left;
            border: 1px solid #2d3748;
            background: #0f0f23;
            color: #eee;
            border-radius: 8px;
            padding: 9px;
            margin-bottom: 8px;
            cursor: pointer;
        }
        .trip-item.active { border-color: #4c6ef5; background: #17224a; }
        .trip-item-title { font-size: 0.82em; font-weight: 700; }
        .trip-item-meta { font-size: 0.72em; color: #9aa7c7; margin-top: 4px; line-height: 1.35; }
        .trip-map {
            width: 100%;
            height: 310px;
            background: #090d18;
            border: 1px solid #2d3748;
            border-radius: 8px;
            display: block;
        }
        .trip-stop-list {
            margin-top: 10px;
            font-size: 0.78em;
            color: #a8b3d1;
            line-height: 1.5;
        }
        @media (max-width: 640px) {
            .trip-layout { grid-template-columns: 1fr; }
            .trip-map { height: 260px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📱 Device Manager</h1>
        
        <div class="tabs">
            <button class="tab active" onclick="showTab('vehicle')">Vehicle</button>
            <button class="tab" onclick="showTab('services')">Services</button>
            <button class="tab" onclick="showTab('camera')">Camera</button>
            <button class="tab" onclick="showTab('events')">Events</button>
            <button class="tab" onclick="showTab('trips')">Trips</button>
            <button class="tab" onclick="showTab('wifi')">WiFi</button>
            <button class="tab" onclick="showTab('settings')">Settings</button>
        </div>
        
        <!-- Vehicle Panel (offline registration) -->
        <div id="vehicle" class="panel active">
            <div class="card">
                <div class="status">
                    <div class="dot" id="statusDot"></div>
                    <span id="statusText">Checking...</span>
                </div>
                <div class="label">Device Serial</div>
                <div class="value" id="deviceSerial">-</div>
            </div>
            <div class="card">
                <h2>Vehicle Details</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Printed on the USB export report and used to name the export folder.</p>
                <div class="label">Vehicle Number *</div>
                <input type="text" id="vehicleNumber" maxlength="20" placeholder="e.g. KA 01 AB 1234" style="text-transform:uppercase; margin-bottom:10px;">
                <div class="label">Driver Name (optional)</div>
                <input type="text" id="driverName" maxlength="40" placeholder="e.g. Ravi Kumar" style="margin-bottom:10px;">
                <button class="btn btn-primary" id="vehicleSaveBtn" onclick="saveVehicle()">Save Vehicle Details</button>
            </div>
        </div>
        
        <!-- Services Panel -->
        <div id="services" class="panel">
            <div class="card">
                <div id="servicesList">Loading...</div>
            </div>
            <button class="btn btn-success" onclick="controlAllServices('start')">
                Start All
            </button>
            <button class="btn btn-danger" onclick="controlAllServices('stop')">
                Stop All
            </button>
            <button class="btn btn-secondary" onclick="loadServices()">
                Refresh
            </button>
        </div>
        
        <!-- Camera Panel -->
        <div id="camera" class="panel">
            <div class="card">
                <div class="camera-container" id="cameraContainer">
                    <p style="padding:40px;color:#666;">Camera Off</p>
                </div>
            </div>
            <button class="btn btn-success" id="cameraStartBtn" onclick="startCamera()">
                📷 Start Camera
            </button>
            <button class="btn btn-danger" id="cameraStopBtn" onclick="stopCamera()" style="display:none;">
                ■ Stop Camera
            </button>
        </div>

        <!-- Events Panel (on-device folders, no cloud) -->
        <div id="events" class="panel">
            <div class="card" style="border:1px solid #364fc7;">
                <h2>Driver Events</h2>
                <p style="font-size:0.75em; color:#9fb1e6; margin-top:4px;">
                    Saved on this device (frames + metadata). Captures:
                    <strong>Sleeping</strong>, <strong>Yawning</strong>, <strong>PhoneCall</strong>,
                    <strong>LookingDown</strong>, <strong>LookingAway</strong>,
                    <strong>NoFace</strong>, and <strong>Overspeeding</strong>.
                    Tap a thumbnail for full size.
                </p>
            </div>
            <div class="card">
                <div class="events-toolbar">
                    <select id="eventsTypeFilter" class="events-filter" onchange="renderEventsGrid()">
                        <option value="">All types</option>
                    </select>
                    <button class="btn btn-secondary btn-sm" onclick="loadEvents()">Refresh</button>
                    <button class="btn btn-danger btn-sm" id="eventsDeleteAllBtn" onclick="deleteAllEvents()">Delete All</button>
                </div>
                <div id="eventsStatus" style="font-size:0.8em; color:#888; margin-bottom:8px;">Loading...</div>
                <div id="eventsGrid" class="events-grid"></div>
            </div>
        </div>

        <!-- Offline GPS Trips Panel -->
        <div id="trips" class="panel">
            <div class="card" style="border:1px solid #2f9e44;">
                <h2>Offline GPS Geo-Tracing</h2>
                <p style="font-size:0.75em; color:#9dd8ad; margin-top:4px;">
                    Local trip sessions from GPS service starts. Each restart creates a fresh route history.
                </p>
            </div>
            <div class="card">
                <div class="trip-layout">
                    <div>
                        <button class="btn btn-secondary btn-sm" onclick="loadTrips()">Refresh</button>
                        <div id="tripListStatus" style="font-size:0.78em; color:#888; margin:8px 0;">Loading...</div>
                        <div id="tripList" class="trip-list"></div>
                    </div>
                    <div>
                        <div id="tripReportStatus" style="font-size:0.8em; color:#888; margin-bottom:8px;">Select a trip.</div>
                        <canvas id="tripMap" class="trip-map" width="720" height="360"></canvas>
                        <div id="tripSummary" class="telemetry-grid"></div>
                        <div id="tripStops" class="trip-stop-list"></div>
                    </div>
                </div>
            </div>
        </div>

        <div id="eventModal" class="event-modal" onclick="closeEventModal(event)">
            <button type="button" class="event-modal-close" onclick="closeEventModal(event)" aria-label="Close">&times;</button>
            <div class="event-modal-inner" onclick="event.stopPropagation()">
                <img id="eventModalImg" src="" alt="Event frame">
                <div id="eventModalCaption" class="event-modal-caption"></div>
            </div>
        </div>
        
        <!-- WiFi Panel -->
        <div id="wifi" class="panel">
            <!-- AP Mode Status Card -->
            <div class="card" id="apModeCard" style="display:none; background:#2d1f4e;">
                <h2>📡 Access Point Mode</h2>
                <div class="status">
                    <div class="dot warn"></div>
                    <span>AP Mode Active</span>
                </div>
                <div class="label">Network Name (SSID)</div>
                <div class="value" id="apSsid">-</div>
                <div class="label" style="margin-top:8px;">Password</div>
                <div class="value" id="apPassword">-</div>
                <div class="label" style="margin-top:8px;">Connect to this IP</div>
                <div class="value" id="apIp">-</div>
                <p style="font-size:0.75em; color:#fcc419; margin-top:10px;">⚠️ Connect to the WiFi network above, then configure a new network below.</p>
            </div>
            
            <div class="card">
                <h2>Current Connection</h2>
                <div class="status">
                    <div class="dot" id="wifiDot"></div>
                    <span id="wifiStatus">Checking...</span>
                </div>
            </div>
            <div class="card">
                <h2>Available Networks</h2>
                <div id="wifiList">
                    <p style="color:#666;">Click Scan to find networks</p>
                </div>
            </div>
            <button class="btn btn-primary" onclick="scanWifi()">
                🔍 Scan Networks
            </button>
            <div class="card" style="margin-top:12px;">
                <h2>Connect to Network</h2>
                <input type="text" id="wifiSsid" placeholder="Network Name (SSID)">
                <input type="password" id="wifiPass" placeholder="Password">
                <button class="btn btn-success" onclick="connectWifi()">
                    Connect
                </button>
            </div>
            
            <!-- AP Mode Controls -->
            <div class="card" style="margin-top:12px;">
                <h2>Access Point Mode</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Start AP mode to configure WiFi when no networks are available.</p>
                <button class="btn btn-secondary" id="apStartBtn" onclick="startApMode()">
                    📡 Start AP Mode
                </button>
                <button class="btn btn-danger" id="apStopBtn" onclick="stopApMode()" style="display:none;">
                    ■ Stop AP Mode
                </button>
            </div>
            
            <!-- Reset WiFi -->
            <div class="card" style="margin-top:12px; border: 1px solid #c92a2a;">
                <h2>⚠️ Reset WiFi</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Forget all saved networks and restart AP mode for re-provisioning.</p>
                <button class="btn btn-danger" onclick="resetWifi()">
                    🔄 Reset WiFi & Start AP
                </button>
            </div>
        </div>
        
        <!-- Settings Panel -->
        <div id="settings" class="panel">
            <!-- Detection Settings -->
            <div class="card">
                <h2>Detection Settings</h2>
                <div style="display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <label style="font-size:0.85em; width:160px;">Activation Speed (km/h)</label>
                    <input type="number" id="activationSpeed" min="0" max="200" style="flex:1;" placeholder="0">
                </div>
                <div style="display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <label style="font-size:0.85em; width:160px;">Speed Limit (km/h)</label>
                    <input type="number" id="speedLimit" min="0" max="200" style="flex:1;" placeholder="0">
                    <span style="font-size:0.7em; color:#888;">buzzer if exceeded (0=off)</span>
                </div>
                <div style="display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <label style="font-size:0.85em; width:160px;">Sleep alert after (seconds)</label>
                    <input type="number" id="sleepSeconds" min="0.5" max="5" step="0.1" style="flex:1;" placeholder="1.5">
                    <span style="font-size:0.7em; color:#888;">eyes closed this long</span>
                </div>

                <!-- LED Blink Toggle -->
                <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <div>
                        <div style="font-weight:600; font-size:0.9em;">LED Blink on Alert</div>
                        <div style="font-size:0.75em; color:#888;">Blink LED (GPIO 4) during sleep/yawn detection</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:50px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="ledBlinkToggle" onchange="updateToggleUI(this, 'ledSlider', 'ledKnob')" style="opacity:0; width:0; height:0;">
                        <span id="ledSlider" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#c92a2a; border-radius:26px; transition:0.3s;"></span>
                        <span id="ledKnob" style="position:absolute; top:3px; left:3px; width:20px; height:20px; background:white; border-radius:50%; transition:0.3s;"></span>
                    </label>
                </div>

                <!-- NoFace Alert Toggle -->
                <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <div>
                        <div style="font-weight:600; font-size:0.9em;">NoFace Alert</div>
                        <div style="font-size:0.75em; color:#888;">Beep when no face detected for set duration</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:50px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="nofaceToggle" onchange="toggleNofaceUI()" style="opacity:0; width:0; height:0;">
                        <span id="nofaceSlider" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#c92a2a; border-radius:26px; transition:0.3s;"></span>
                        <span id="nofaceKnob" style="position:absolute; top:3px; left:3px; width:20px; height:20px; background:white; border-radius:50%; transition:0.3s;"></span>
                    </label>
                </div>
                <div id="nofaceSettings" style="display:none; padding:10px 0;">
                    <div style="display:flex; gap:10px; align-items:center;">
                        <label style="font-size:0.85em; width:160px;">Alert after (seconds)</label>
                        <input type="number" id="nofaceThreshold" min="1" max="30" step="1" style="flex:1;" placeholder="2">
                    </div>
                </div>

                <!-- Phone Call Detection Toggle -->
                <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <div>
                        <div style="font-weight:600; font-size:0.9em;">Phone Call Detection</div>
                        <div style="font-size:0.75em; color:#888;">Alert when hand stays near ear (MediaPipe Hands)</div>
                    </div>
                    <label style="position:relative; display:inline-block; width:50px; height:26px; cursor:pointer;">
                        <input type="checkbox" id="phoneDetectToggle" onchange="togglePhoneUI()" style="opacity:0; width:0; height:0;">
                        <span id="phoneSlider" style="position:absolute; top:0; left:0; right:0; bottom:0; background:#c92a2a; border-radius:26px; transition:0.3s;"></span>
                        <span id="phoneKnob" style="position:absolute; top:3px; left:3px; width:20px; height:20px; background:white; border-radius:50%; transition:0.3s;"></span>
                    </label>
                </div>
                <div id="phoneSettings" style="display:none; padding:10px 0; border-bottom:1px solid #2d3748;">
                    <div style="display:flex; gap:10px; align-items:center; margin-bottom:8px;">
                        <label style="font-size:0.85em; width:160px;">Alert after (seconds)</label>
                        <input type="number" id="phoneConfirmSeconds" min="0" max="10" step="0.1" style="flex:1;" placeholder="0.3">
                        <span style="font-size:0.7em; color:#888;">0 = instant</span>
                    </div>
                    <div style="display:flex; gap:10px; align-items:center; margin-bottom:8px;">
                        <label style="font-size:0.85em; width:160px;">Buzz for (seconds)</label>
                        <input type="number" id="phoneBuzzSeconds" min="0" max="60" step="1" style="flex:1;" placeholder="10">
                        <span style="font-size:0.7em; color:#888;">0 = no buzz</span>
                    </div>
                    <div style="display:flex; gap:10px; align-items:center;">
                        <label style="font-size:0.85em; width:160px;">Ear distance (x face width)</label>
                        <input type="number" id="phoneEarDistRatio" min="0.2" max="1" step="0.05" style="flex:1;" placeholder="0.5">
                        <span style="font-size:0.7em; color:#888;">higher = more sensitive</span>
                    </div>
                </div>

                <button class="btn btn-primary" style="margin-top:10px;" onclick="saveSettings()">Save Detection Settings</button>
                <div id="settingsMessage" class="msg" style="display:none;"></div>
            </div>

            <!-- Date & Time -->
            <div class="card" style="margin-top:12px;">
                <h2>Date &amp; Time</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Event folders and the time printed on event images use this clock. It is corrected from GPS automatically once the GPS has a fix; set it here when there is no GPS signal. The timezone is taken from this phone.</p>
                <div style="padding:6px 0; font-size:0.85em;">
                    Device time: <b id="deviceTimeNow">--</b> <span id="deviceTimezone" style="color:#888;"></span>
                </div>
                <div style="display:flex; gap:10px; align-items:center; padding:10px 0;">
                    <label style="font-size:0.85em; width:160px;">New date &amp; time</label>
                    <input type="datetime-local" id="deviceTimeInput" step="1" style="flex:1;">
                </div>
                <button class="btn btn-primary btn-sm" onclick="setDeviceTime(false)">Set Date &amp; Time</button>
                <button class="btn btn-primary btn-sm" onclick="setDeviceTime(true)">Use This Phone's Time</button>
                <div id="timeMessage" class="msg" style="display:none;"></div>
            </div>

            <!-- Data Retention Settings -->
            <div class="card" style="margin-top:12px; border: 1px solid #fab005;">
                <h2>Data Retention</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Auto-delete old GPS data and images. Requires password.</p>
                <div style="display:flex; gap:10px; align-items:center; margin-bottom:8px;">
                    <label style="font-size:0.85em; width:110px;">GPS Data (days)</label>
                    <input type="number" id="gpsRetention" min="1" max="365" style="flex:1;" placeholder="30">
                </div>
                <div style="display:flex; gap:10px; align-items:center; margin-bottom:8px;">
                    <label style="font-size:0.85em; width:110px;">Images (days)</label>
                    <input type="number" id="imageRetention" min="1" max="365" style="flex:1;" placeholder="15">
                </div>
                <input type="password" id="retentionPassword" placeholder="Password to save" style="margin-bottom:8px;">
                <button class="btn btn-primary btn-sm" onclick="saveRetention()">Save Retention Settings</button>
                <div id="retentionMessage" class="msg" style="display:none;"></div>
            </div>

            <!-- Factory Reset Card -->
            <div class="card" style="margin-top:12px; border: 1px solid #c92a2a;">
                <h2>Factory Reset</h2>
                <p style="font-size:0.75em; color:#888; margin-bottom:10px;">Clear vehicle details, GPS data, trips, events and images (detection settings are kept). Export to USB first. Requires password.</p>
                <input type="password" id="deletePassword" placeholder="Enter password to confirm">
                <button class="btn btn-danger" onclick="deleteDeviceId()">Factory Reset</button>
                <div id="deleteMessage" class="msg" style="display:none;"></div>
            </div>
        </div>

        <div id="message"></div>
        <div class="info">
            <a href="{{ local_url }}" style="color:#748ffc;">{{ local_host }}.local:{{ ui_port }}</a>
            <span style="color:#555;"> · </span>
            <a href="{{ ip_url }}" style="color:#888;">{{ ip_address }}:{{ ui_port }}</a>
        </div>
    </div>
    
    <script>
        let cameraActive = false;
        let eventsCache = [];
        let tripsCache = [];
        let selectedTripId = null;

        /** Retry fetch when the UI server is briefly down (e.g. after systemctl restart). */
        async function apiFetch(url, options = {}, retries = 4, baseDelayMs = 600) {
            const opts = Object.assign({ credentials: 'same-origin' }, options);
            let lastErr = null;
            for (let attempt = 0; attempt < retries; attempt++) {
                try {
                    return await fetch(url, opts);
                } catch (e) {
                    lastErr = e;
                    if (attempt < retries - 1) {
                        await new Promise(r => setTimeout(r, baseDelayMs * (attempt + 1)));
                    }
                }
            }
            throw lastErr;
        }

        function fetchErrorMessage(err, context) {
            const msg = err && err.message ? String(err.message) : String(err);
            if (err && err.name === 'TypeError' && msg.toLowerCase().includes('fetch')) {
                return (context ? context + ': ' : '') +
                    'Cannot reach the device UI — it may be restarting. Wait 10–20s and try again.';
            }
            return (context ? context + ': ' : '') + msg;
        }
        
        function showTab(name) {
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(name).classList.add('active');
            event.target.classList.add('active');
            
            if (name === 'services') loadServices();
            if (name === 'camera') { /* camera stays manual */ }
            if (name === 'events') loadEvents();
            if (name === 'trips') loadTrips();
            if (name === 'wifi') checkWifi();
            if (name === 'settings') { loadSettings(); loadRetention(); loadDeviceTime(); }
        }

        function eventFileUrl(relPath) {
            const parts = relPath.split('/').map(encodeURIComponent);
            return '/api/events/file/' + parts.join('/');
        }

        function eventBadgeClass(type) {
            const map = {
                Sleeping: 'badge-sleeping',
                Yawning: 'badge-yawning',
                PhoneCall: 'badge-phonecall',
                LookingDown: 'badge-lookingdown',
                LookingAway: 'badge-lookingaway',
                NoFace: 'badge-noface',
                Overspeeding: 'badge-overspeeding',
            };
            return map[type] || '';
        }

        function loadEvents() {
            const status = document.getElementById('eventsStatus');
            const grid = document.getElementById('eventsGrid');
            if (status) status.textContent = 'Loading events...';
            apiFetch('/api/events/list')
                .then(r => r.json())
                .then(data => {
                    if (!data || !data.success) {
                        eventsCache = [];
                        if (grid) grid.innerHTML = '';
                        if (status) status.textContent = (data && data.error) ? data.error : 'Failed to load events';
                        return;
                    }
                    eventsCache = data.events || [];
                    const filter = document.getElementById('eventsTypeFilter');
                    if (filter) {
                        const current = filter.value;
                        const types = data.event_types || [];
                        filter.innerHTML = '<option value="">All types</option>';
                        types.forEach(t => {
                            const opt = document.createElement('option');
                            opt.value = t;
                            opt.textContent = t;
                            filter.appendChild(opt);
                        });
                        if (current && types.includes(current)) filter.value = current;
                    }
                    if (status) {
                        status.textContent = eventsCache.length
                            ? `${eventsCache.length} event(s) on device`
                            : 'No events saved yet';
                    }
                    const deleteAllBtn = document.getElementById('eventsDeleteAllBtn');
                    if (deleteAllBtn) deleteAllBtn.disabled = eventsCache.length === 0;
                    renderEventsGrid();
                })
                .catch(e => {
                    eventsCache = [];
                    if (grid) grid.innerHTML = '';
                    if (status) status.textContent = fetchErrorMessage(e, 'Events');
                });
        }

        function renderEventsGrid() {
            const grid = document.getElementById('eventsGrid');
            const filterEl = document.getElementById('eventsTypeFilter');
            if (!grid) return;
            const typeFilter = filterEl ? filterEl.value : '';
            const items = typeFilter
                ? eventsCache.filter(e => e.event_type === typeFilter)
                : eventsCache;
            if (!items.length) {
                grid.innerHTML = '<div class="events-empty">No events match this filter.</div>';
                return;
            }
            grid.innerHTML = items.map(ev => {
                const thumb = ev.thumbnail ? eventFileUrl(ev.thumbnail) : '';
                const label = ev.start_time_iso || ev.folder || '';
                const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
                const badgeCls = eventBadgeClass(ev.event_type);
                return `<div class="event-card" data-thumb="${esc(ev.thumbnail || '')}" data-caption="${esc(ev.event_type)} — ${esc(label)} (${ev.frame_count} frames)" onclick="openEventModal(this)">
                    <img src="${thumb}" alt="${esc(ev.event_type)}" loading="lazy">
                    <div class="event-card-meta">
                        <span class="event-type-badge ${badgeCls}">${esc(ev.event_type)}</span>
                        <div>${esc(label)}</div>
                        <div style="color:#888;">${ev.frame_count} frame(s)</div>
                        <div class="event-card-actions">
                            <button type="button" class="event-delete-btn" data-folder="${esc(ev.folder)}" data-label="${esc(ev.event_type)} — ${esc(label)}" onclick="deleteEventFromBtn(event)">Delete</button>
                        </div>
                    </div>
                </div>`;
            }).join('');
        }

        function deleteEventFromBtn(ev) {
            ev.stopPropagation();
            const btn = ev.currentTarget;
            const folder = btn.getAttribute('data-folder');
            const label = btn.getAttribute('data-label') || folder;
            if (!folder) return;
            if (!confirm(`Delete event "${label}" and all stored frames? This cannot be undone.`)) {
                return;
            }
            btn.disabled = true;
            fetch('/api/events/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ folder })
            })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showMessage(data.message || 'Event deleted', 'success');
                        eventsCache = eventsCache.filter(e => e.folder !== folder);
                        const status = document.getElementById('eventsStatus');
                        if (status) {
                            status.textContent = eventsCache.length
                                ? `${eventsCache.length} event(s) on device`
                                : 'No events saved yet';
                        }
                        const deleteAllBtn = document.getElementById('eventsDeleteAllBtn');
                        if (deleteAllBtn) deleteAllBtn.disabled = eventsCache.length === 0;
                        renderEventsGrid();
                    } else {
                        showMessage(data.error || 'Failed to delete event', 'error');
                        btn.disabled = false;
                    }
                })
                .catch(e => {
                    showMessage(fetchErrorMessage(e, 'Delete event'), 'error');
                    btn.disabled = false;
                });
        }

        function deleteAllEvents() {
            const count = eventsCache.length;
            if (!count) {
                showMessage('No events to delete', 'error');
                return;
            }
            if (!confirm(`Delete ALL ${count} event(s) and every stored frame? This cannot be undone.`)) {
                return;
            }
            const btn = document.getElementById('eventsDeleteAllBtn');
            if (btn) btn.disabled = true;
            fetch('/api/events/delete_all', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showMessage(data.message || 'All events deleted', 'success');
                        eventsCache = [];
                        const status = document.getElementById('eventsStatus');
                        if (status) status.textContent = 'No events saved yet';
                        renderEventsGrid();
                    } else {
                        showMessage(data.error || 'Failed to delete events', 'error');
                    }
                })
                .catch(e => showMessage(fetchErrorMessage(e, 'Delete all events'), 'error'))
                .finally(() => {
                    if (btn) btn.disabled = eventsCache.length === 0;
                });
        }

        function openEventModal(cardEl) {
            const rel = cardEl.getAttribute('data-thumb');
            const caption = cardEl.getAttribute('data-caption') || '';
            if (!rel) return;
            const modal = document.getElementById('eventModal');
            const img = document.getElementById('eventModalImg');
            const cap = document.getElementById('eventModalCaption');
            if (img) img.src = eventFileUrl(rel);
            if (cap) cap.textContent = caption;
            if (modal) modal.classList.add('open');
            document.body.style.overflow = 'hidden';
        }

        function closeEventModal(ev) {
            if (ev && ev.target && ev.target.id === 'eventModalImg') return;
            const modal = document.getElementById('eventModal');
            if (modal) modal.classList.remove('open');
            const img = document.getElementById('eventModalImg');
            if (img) img.src = '';
            document.body.style.overflow = '';
        }

        document.addEventListener('keydown', e => {
            if (e.key === 'Escape') closeEventModal();
        });
        
        function showMessage(text, type) {
            const msg = document.getElementById('message');
            msg.className = 'msg ' + type;
            msg.textContent = text;
            setTimeout(() => { msg.textContent = ''; msg.className = ''; }, 4000);
        }
        
        // === Vehicle (offline registration) ===
        function checkStatus() {
            fetch('/api/vehicle')
                .then(r => r.json())
                .then(data => {
                    const dot = document.getElementById('statusDot');
                    const text = document.getElementById('statusText');
                    document.getElementById('deviceSerial').textContent = data.device_serial || '-';
                    document.getElementById('vehicleNumber').value = data.vehicle_number || '';
                    document.getElementById('driverName').value = data.driver_name || '';
                    if (data.vehicle_number) {
                        dot.classList.add('ok');
                        text.textContent = 'Vehicle: ' + data.vehicle_number;
                    } else {
                        dot.classList.remove('ok');
                        text.textContent = 'Vehicle number not set';
                    }
                });
        }

        function saveVehicle() {
            const btn = document.getElementById('vehicleSaveBtn');
            btn.disabled = true;
            fetch('/api/vehicle', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    vehicle_number: document.getElementById('vehicleNumber').value,
                    driver_name: document.getElementById('driverName').value
                })
            })
            .then(r => r.json())
            .then(data => {
                showMessage(data.success ? 'Vehicle details saved' : (data.error || 'Failed'),
                            data.success ? 'success' : 'error');
                if (data.success) checkStatus();
            })
            .catch(e => showMessage('Error: ' + e, 'error'))
            .finally(() => { btn.disabled = false; });
        }
        
        function deleteDeviceId() {
            const password = document.getElementById('deletePassword').value;
            const msgDiv = document.getElementById('deleteMessage');
            
            if (!password) {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Password is required';
                return;
            }
            
            if (!confirm('Factory reset will erase vehicle details and ALL recorded trips, events and images. Export to USB first. Continue?')) {
                return;
            }
            
            fetch('/api/device/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: password })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Factory reset complete';
                    document.getElementById('deletePassword').value = '';
                    checkStatus();
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Factory reset failed';
                }
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            });
        }
        
        // === Services ===
        function loadServices() {
            fetch('/api/services')
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('servicesList');
                    list.innerHTML = data.services.map(s => `
                        <div class="service-row">
                            <div class="service-info">
                                <div class="service-name">
                                    <span class="dot ${s.active ? 'ok' : ''}" style="display:inline-block;width:8px;height:8px;margin-right:6px;"></span>
                                    ${s.display}
                                </div>
                                <div class="service-desc">${s.description} - ${s.status}</div>
                            </div>
                            <div class="service-actions">
                                <button class="btn btn-sm btn-success" onclick="controlService('${s.name}', 'start')" ${s.active ? 'disabled' : ''} title="Start">▶</button>
                                <button class="btn btn-sm btn-danger" onclick="controlService('${s.name}', 'stop')" ${!s.active ? 'disabled' : ''} title="Stop">■</button>
                                <button class="btn btn-sm btn-primary" onclick="controlService('${s.name}', 'restart')" title="Restart">🔄</button>
                            </div>
                        </div>
                    `).join('');
                });
        }
        
        function controlService(name, action) {
            fetch('/api/services/' + name + '/' + action, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    showMessage(data.message, data.success ? 'success' : 'error');
                    setTimeout(loadServices, 1000);
                });
        }
        
        function controlAllServices(action) {
            fetch('/api/services/all/' + action, { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    showMessage(data.message, 'success');
                    setTimeout(loadServices, 1500);
                });
        }
        
        // === Camera ===
        function startCamera() {
            document.getElementById('cameraContainer').innerHTML = '<img src="/api/camera/stream" alt="Camera">';
            document.getElementById('cameraStartBtn').style.display = 'none';
            document.getElementById('cameraStopBtn').style.display = 'block';
            cameraActive = true;
        }
        
        function stopCamera() {
            fetch('/api/camera/stop', { method: 'POST' });
            document.getElementById('cameraContainer').innerHTML = '<p style="padding:40px;color:#666;">Camera Off</p>';
            document.getElementById('cameraStartBtn').style.display = 'block';
            document.getElementById('cameraStopBtn').style.display = 'none';
            cameraActive = false;
        }
        
        // === WiFi ===
        function checkWifi() {
            fetch('/api/wifi/status')
                .then(r => r.json())
                .then(data => {
                    const dot = document.getElementById('wifiDot');
                    const text = document.getElementById('wifiStatus');
                    const apCard = document.getElementById('apModeCard');
                    const apStartBtn = document.getElementById('apStartBtn');
                    const apStopBtn = document.getElementById('apStopBtn');
                    
                    // Prioritize WiFi connection status over AP mode
                    // WiFi can be connected even if hostapd is still running
                    if (data.connected) {
                        // WiFi is connected - show connected status
                        dot.classList.add('ok');
                        dot.classList.remove('warn');
                        text.textContent = data.ssid + ' (' + data.ip + ')';
                        apCard.style.display = 'none';
                        apStartBtn.style.display = 'block';
                        apStopBtn.style.display = 'none';
                    } else if (data.ap_active) {
                        // Not connected to WiFi, AP mode is active
                        apCard.style.display = 'block';
                        document.getElementById('apSsid').textContent = data.ap_ssid || '-';
                        document.getElementById('apPassword').textContent = data.ap_password || '-';
                        document.getElementById('apIp').textContent = data.ap_ip || '-';
                        apStartBtn.style.display = 'none';
                        apStopBtn.style.display = 'block';
                        dot.classList.remove('ok');
                        dot.classList.add('warn');
                        text.textContent = 'AP Mode (No WiFi)';
                    } else {
                        // Not connected and AP mode not active
                        apCard.style.display = 'none';
                        apStartBtn.style.display = 'block';
                        apStopBtn.style.display = 'none';
                        dot.classList.remove('ok');
                        dot.classList.remove('warn');
                        text.textContent = 'Not Connected';
                    }
                });
        }
        
        // === AP Mode ===
        function startApMode() {
            const btn = document.getElementById('apStartBtn');
            btn.disabled = true;
            btn.textContent = 'Starting AP...';
            showMessage('Starting Access Point...', 'success');
            
            fetch('/api/wifi/ap/start', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    btn.disabled = false;
                    btn.textContent = '📡 Start AP Mode';
                    if (data.success) {
                        showMessage('AP Mode started! SSID: ' + data.ssid, 'success');
                        setTimeout(checkWifi, 1000);
                    } else {
                        showMessage(data.error || 'Failed to start AP', 'error');
                    }
                })
                .catch(e => {
                    btn.disabled = false;
                    btn.textContent = '📡 Start AP Mode';
                    showMessage('Error: ' + e, 'error');
                });
        }
        
        function stopApMode() {
            const btn = document.getElementById('apStopBtn');
            btn.disabled = true;
            btn.textContent = 'Stopping AP...';
            showMessage('Stopping Access Point...', 'success');
            
            fetch('/api/wifi/ap/stop', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    btn.disabled = false;
                    btn.textContent = '■ Stop AP Mode';
                    if (data.success) {
                        showMessage('AP Mode stopped. Reconnecting to WiFi...', 'success');
                        setTimeout(checkWifi, 3000);
                    } else {
                        showMessage(data.error || 'Failed to stop AP', 'error');
                    }
                })
                .catch(e => {
                    btn.disabled = false;
                    btn.textContent = '■ Stop AP Mode';
                    showMessage('Error: ' + e, 'error');
                });
        }
        
        function resetWifi() {
            if (!confirm("This will forget ALL saved WiFi networks and start AP mode. You will need to reconnect to the device hotspot to configure a new network. Continue?")) {
                return;
            }
            showMessage('Resetting WiFi...', 'success');
            
            fetch('/api/wifi/reset', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        showMessage('WiFi reset! Connect to AP: ' + (data.ap_ssid || 'SapienceDevice'), 'success');
                        setTimeout(checkWifi, 2000);
                    } else {
                        showMessage(data.error || 'Reset failed', 'error');
                    }
                })
                .catch(e => {
                    showMessage('Error: ' + e, 'error');
                });
        }
        
        function scanWifi() {
            document.getElementById('wifiList').innerHTML = '<p style="color:#666;">Scanning...</p>';
            fetch('/api/wifi/scan')
                .then(r => r.json())
                .then(data => {
                    const list = document.getElementById('wifiList');
                    if (data.networks.length === 0) {
                        list.innerHTML = '<p style="color:#666;">No networks found</p>';
                        return;
                    }
                    list.innerHTML = data.networks.map(n => `
                        <div class="wifi-network" onclick="selectWifi('${n.ssid}')">
                            <span>${n.ssid}</span>
                            <span class="wifi-signal">${n.signal}%</span>
                        </div>
                    `).join('');
                });
        }
        
        function selectWifi(ssid) {
            document.getElementById('wifiSsid').value = ssid;
            document.getElementById('wifiPass').focus();
        }
        
        function connectWifi() {
            const ssid = document.getElementById('wifiSsid').value;
            const password = document.getElementById('wifiPass').value;
            if (!ssid) {
                showMessage('Enter network name', 'error');
                return;
            }
            showMessage('Connecting...', 'success');
            fetch('/api/wifi/connect', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ssid, password })
            })
            .then(r => r.json())
            .then(data => {
                showMessage(data.message, data.success ? 'success' : 'error');
                if (data.success) setTimeout(checkWifi, 3000);
            });
        }
        
        function formatTelemetryValue(val, unit='') {
            if (val === null || val === undefined) return 'N/A';
            if (typeof val === 'number') {
                const rounded = Math.round(val * 100) / 100;
                return unit ? `${rounded} ${unit}` : `${rounded}`;
            }
            return unit ? `${val} ${unit}` : `${val}`;
        }

        function formatTripTime(value) {
            if (!value) return 'N/A';
            const d = new Date(String(value).replace(' ', 'T'));
            return isNaN(d.getTime()) ? String(value) : d.toLocaleString();
        }

        function formatDuration(seconds) {
            seconds = Math.max(0, Math.round(Number(seconds) || 0));
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = seconds % 60;
            if (h) return `${h}h ${m}m`;
            if (m) return `${m}m ${s}s`;
            return `${s}s`;
        }

        function loadTrips() {
            const status = document.getElementById('tripListStatus');
            const list = document.getElementById('tripList');
            if (status) status.textContent = 'Loading trips...';
            apiFetch('/api/trips/list')
                .then(r => r.json())
                .then(data => {
                    if (!data || !data.success) {
                        tripsCache = [];
                        if (status) status.textContent = (data && data.error) ? data.error : 'Failed to load trips';
                        if (list) list.innerHTML = '';
                        drawTripMap([]);
                        return;
                    }
                    tripsCache = data.trips || [];
                    if (status) status.textContent = tripsCache.length ? `${tripsCache.length} trip(s) stored` : 'No trips stored yet';
                    renderTripList();
                    if (tripsCache.length && !selectedTripId) {
                        selectedTripId = tripsCache[0].id;
                    }
                    if (selectedTripId) loadTripReport(selectedTripId);
                })
                .catch(e => {
                    if (status) status.textContent = fetchErrorMessage(e, 'Trips');
                    drawTripMap([]);
                });
        }

        function renderTripList() {
            const list = document.getElementById('tripList');
            if (!list) return;
            if (!tripsCache.length) {
                list.innerHTML = '<div class="events-empty">No trip sessions yet.</div>';
                return;
            }
            list.innerHTML = tripsCache.map(t => {
                const active = Number(t.id) === Number(selectedTripId) ? ' active' : '';
                const stateClass = t.status === 'active' ? 'ok' : 'warn';
                return `<button class="trip-item${active}" onclick="selectTrip(${t.id})">
                    <div class="trip-item-title">Trip #${t.id} <span class="status-pill ${stateClass}">${t.status || 'closed'}</span></div>
                    <div class="trip-item-meta">${formatTripTime(t.start_time)}<br>${formatTelemetryValue(t.total_distance_km, 'km')} - ${t.point_count || 0} points</div>
                </button>`;
            }).join('');
        }

        function selectTrip(id) {
            selectedTripId = id;
            renderTripList();
            loadTripReport(id);
        }

        function loadTripReport(id) {
            const status = document.getElementById('tripReportStatus');
            if (status) status.textContent = 'Loading trip report...';
            apiFetch(`/api/trips/${id}`)
                .then(r => r.json())
                .then(data => {
                    if (!data || !data.success) {
                        if (status) status.textContent = (data && data.error) ? data.error : 'Failed to load trip';
                        drawTripMap([]);
                        return;
                    }
                    renderTripReport(data.trip || {});
                })
                .catch(e => {
                    if (status) status.textContent = fetchErrorMessage(e, 'Trip report');
                    drawTripMap([]);
                });
        }

        function renderTripReport(trip) {
            const points = trip.points || [];
            const stops = trip.stops || [];
            const status = document.getElementById('tripReportStatus');
            if (status) {
                status.textContent = points.length
                    ? `Route history: ${points.length} GPS point(s), ${stops.length} stop/idle point(s)`
                    : 'This trip has no GPS points yet.';
            }
            const summary = document.getElementById('tripSummary');
            if (summary) {
                summary.innerHTML = `
                    <div class="telemetry-card"><div class="telemetry-title">Distance</div><div class="telemetry-value">${formatTelemetryValue(trip.total_distance_km, 'km')}</div></div>
                    <div class="telemetry-card"><div class="telemetry-title">Points</div><div class="telemetry-value">${trip.point_count || points.length}</div></div>
                    <div class="telemetry-card"><div class="telemetry-title">Start</div><div class="telemetry-value" style="font-size:0.8em;">${formatTripTime(trip.start_time)}</div></div>
                    <div class="telemetry-card"><div class="telemetry-title">End</div><div class="telemetry-value" style="font-size:0.8em;">${formatTripTime(trip.end_time)}</div></div>
                `;
            }
            const stopEl = document.getElementById('tripStops');
            if (stopEl) {
                stopEl.innerHTML = stops.length
                    ? '<strong>Stop / idle points</strong>' + stops.map((s, idx) =>
                        `<div>${idx + 1}. ${Number(s.latitude).toFixed(6)}, ${Number(s.longitude).toFixed(6)} - ${formatDuration(s.duration_seconds)}</div>`
                    ).join('')
                    : '<strong>Stop / idle points</strong><div>No long idle location detected for this trip.</div>';
            }
            drawTripMap(points, stops);
        }

        function drawTripMap(points, stops = []) {
            const canvas = document.getElementById('tripMap');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');
            const w = canvas.width;
            const h = canvas.height;
            ctx.clearRect(0, 0, w, h);
            ctx.fillStyle = '#090d18';
            ctx.fillRect(0, 0, w, h);
            ctx.strokeStyle = '#1e2a44';
            ctx.lineWidth = 1;
            for (let x = 40; x < w; x += 80) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
            for (let y = 40; y < h; y += 60) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
            if (!points || points.length === 0) {
                ctx.fillStyle = '#8b9bb4';
                ctx.font = '16px sans-serif';
                ctx.fillText('No GPS route points for this trip', 24, 42);
                return;
            }
            const lats = points.map(p => Number(p.latitude));
            const lons = points.map(p => Number(p.longitude));
            const minLat = Math.min(...lats), maxLat = Math.max(...lats);
            const minLon = Math.min(...lons), maxLon = Math.max(...lons);
            const pad = 28;
            const toXY = p => {
                const lonSpan = Math.max(0.000001, maxLon - minLon);
                const latSpan = Math.max(0.000001, maxLat - minLat);
                return {
                    x: pad + ((Number(p.longitude) - minLon) / lonSpan) * (w - pad * 2),
                    y: h - pad - ((Number(p.latitude) - minLat) / latSpan) * (h - pad * 2)
                };
            };
            ctx.strokeStyle = '#51cf66';
            ctx.lineWidth = 3;
            ctx.beginPath();
            points.forEach((p, i) => {
                const xy = toXY(p);
                if (i === 0) ctx.moveTo(xy.x, xy.y);
                else ctx.lineTo(xy.x, xy.y);
            });
            ctx.stroke();
            stops.forEach(s => {
                const xy = toXY(s);
                ctx.fillStyle = '#fab005';
                ctx.beginPath();
                ctx.arc(xy.x, xy.y, 6, 0, Math.PI * 2);
                ctx.fill();
            });
            const start = toXY(points[0]);
            const end = toXY(points[points.length - 1]);
            ctx.fillStyle = '#339af0';
            ctx.beginPath();
            ctx.arc(start.x, start.y, 7, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = '#ff6b6b';
            ctx.beginPath();
            ctx.arc(end.x, end.y, 7, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = '#dbe4ff';
            ctx.font = '12px sans-serif';
            ctx.fillText('Start', start.x + 9, start.y - 8);
            ctx.fillText('End', end.x + 9, end.y - 8);
        }
        
        // === Settings ===
        function updateToggleUI(checkbox, sliderId, knobId) {
            const slider = document.getElementById(sliderId);
            const knob = document.getElementById(knobId);
            if (checkbox.checked) {
                slider.style.background = '#2f9e44';
                knob.style.left = '27px';
            } else {
                slider.style.background = '#c92a2a';
                knob.style.left = '3px';
            }
        }

        function toggleNofaceUI() {
            const toggle = document.getElementById('nofaceToggle');
            updateToggleUI(toggle, 'nofaceSlider', 'nofaceKnob');
            document.getElementById('nofaceSettings').style.display = toggle.checked ? 'block' : 'none';
        }

        function togglePhoneUI() {
            const toggle = document.getElementById('phoneDetectToggle');
            updateToggleUI(toggle, 'phoneSlider', 'phoneKnob');
            document.getElementById('phoneSettings').style.display = toggle.checked ? 'block' : 'none';
        }

        let deviceClock = null;  // {baseMs, fetchedAt} — device local wall time, ticked in the browser

        function pad2(n) { return (n < 10 ? '0' : '') + n; }

        function formatDateTime(d, sep) {
            return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + sep +
                pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
        }

        function showDeviceTime(data) {
            deviceClock = { baseMs: new Date(data.local.replace(' ', 'T')).getTime(), fetchedAt: Date.now() };
            document.getElementById('deviceTimezone').textContent = data.timezone ? '(' + data.timezone + ')' : '';
            tickDeviceTime();
        }

        function tickDeviceTime() {
            if (!deviceClock) return;
            const now = new Date(deviceClock.baseMs + (Date.now() - deviceClock.fetchedAt));
            document.getElementById('deviceTimeNow').textContent = formatDateTime(now, ' ');
        }
        setInterval(tickDeviceTime, 1000);

        function loadDeviceTime() {
            document.getElementById('deviceTimeInput').value = formatDateTime(new Date(), 'T');
            fetch('/api/system/time').then(r => r.json()).then(showDeviceTime);
        }

        function setDeviceTime(usePhone) {
            const msgDiv = document.getElementById('timeMessage');
            let epoch;
            if (usePhone) {
                epoch = Date.now() / 1000;
            } else {
                const value = document.getElementById('deviceTimeInput').value;
                const picked = new Date(value);
                if (!value || isNaN(picked.getTime())) {
                    msgDiv.style.display = 'block';
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = 'Pick a date and time first';
                    return;
                }
                epoch = picked.getTime() / 1000;
            }
            let tz = '';
            try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}
            if (tz === 'Asia/Calcutta') tz = 'Asia/Kolkata';

            fetch('/api/system/time', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ epoch: epoch, timezone: tz })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Device time set to ' + data.local +
                        (data.services_restarted ? '. Timezone changed, so detection restarted (back in ~20s).' : '. New events will use this time.');
                    showDeviceTime(data);
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Failed to set time';
                }
                setTimeout(() => { msgDiv.style.display = 'none'; }, 5000);
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
            });
        }

        function loadSettings() {
            fetch('/api/config/settings')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('activationSpeed').value = data.speed || 0;
                    document.getElementById('speedLimit').value = data.speed_limit || 0;
                    document.getElementById('sleepSeconds').value = data.sleep_seconds;
                    const ledToggle = document.getElementById('ledBlinkToggle');
                    ledToggle.checked = data.led_blink_enabled;
                    updateToggleUI(ledToggle, 'ledSlider', 'ledKnob');
                    const nofaceToggle = document.getElementById('nofaceToggle');
                    nofaceToggle.checked = data.noface_enabled;
                    updateToggleUI(nofaceToggle, 'nofaceSlider', 'nofaceKnob');
                    document.getElementById('nofaceThreshold').value = data.noface_threshold || 2;
                    document.getElementById('nofaceSettings').style.display = data.noface_enabled ? 'block' : 'none';
                    const phoneToggle = document.getElementById('phoneDetectToggle');
                    phoneToggle.checked = data.phone_detection_enabled !== false;
                    updateToggleUI(phoneToggle, 'phoneSlider', 'phoneKnob');
                    document.getElementById('phoneConfirmSeconds').value = data.phone_confirm_seconds;
                    document.getElementById('phoneBuzzSeconds').value = data.phone_buzz_seconds;
                    document.getElementById('phoneEarDistRatio').value = data.phone_ear_dist_ratio;
                    document.getElementById('phoneSettings').style.display = phoneToggle.checked ? 'block' : 'none';
                });
        }

        function saveSettings() {
            const speed = document.getElementById('activationSpeed').value;
            const speedLimit = document.getElementById('speedLimit').value;
            const sleepSeconds = parseFloat(document.getElementById('sleepSeconds').value);
            const ledEnabled = document.getElementById('ledBlinkToggle').checked;
            const nofaceEnabled = document.getElementById('nofaceToggle').checked;
            const nofaceThreshold = document.getElementById('nofaceThreshold').value;
            const phoneEnabled = document.getElementById('phoneDetectToggle').checked;
            const phoneConfirm = parseFloat(document.getElementById('phoneConfirmSeconds').value);
            const phoneBuzz = parseFloat(document.getElementById('phoneBuzzSeconds').value);
            const phoneEarRatio = parseFloat(document.getElementById('phoneEarDistRatio').value);
            const msgDiv = document.getElementById('settingsMessage');

            fetch('/api/config/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    speed: parseInt(speed) || 0,
                    speed_limit: parseInt(speedLimit) || 0,
                    sleep_seconds: isNaN(sleepSeconds) ? 1.5 : sleepSeconds,
                    led_blink_enabled: ledEnabled,
                    noface_enabled: nofaceEnabled,
                    noface_threshold: parseInt(nofaceThreshold) || 2,
                    phone_detection_enabled: phoneEnabled,
                    phone_confirm_seconds: isNaN(phoneConfirm) ? 0.3 : phoneConfirm,
                    phone_buzz_seconds: isNaN(phoneBuzz) ? 10 : phoneBuzz,
                    phone_ear_dist_ratio: isNaN(phoneEarRatio) ? 0.5 : phoneEarRatio
                })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Settings saved! Restarting Facial Tracking...';
                    fetch('/api/services/facial1/restart', { method: 'POST' })
                        .then(r => r.json())
                        .then(r => {
                            msgDiv.textContent = r.success ? 'Settings saved! Facial Tracking restarted.' : 'Settings saved! Restart failed: ' + (r.message || '');
                            setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
                        });
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Failed';
                    setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
                }
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            });
        }

        // === Data Retention ===
        function loadRetention() {
            fetch('/api/config/retention')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('gpsRetention').value = data.gps_retention_days || 30;
                    document.getElementById('imageRetention').value = data.image_retention_days || 15;
                });
        }

        function saveRetention() {
            const gpsDays = document.getElementById('gpsRetention').value;
            const imageDays = document.getElementById('imageRetention').value;
            const password = document.getElementById('retentionPassword').value;
            const msgDiv = document.getElementById('retentionMessage');

            if (!password) {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Password is required';
                setTimeout(() => { msgDiv.style.display = 'none'; }, 3000);
                return;
            }
            if (!gpsDays || gpsDays < 1 || gpsDays > 365 || !imageDays || imageDays < 1 || imageDays > 365) {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Days must be between 1 and 365';
                setTimeout(() => { msgDiv.style.display = 'none'; }, 3000);
                return;
            }

            fetch('/api/config/retention', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    gps_retention_days: parseInt(gpsDays),
                    image_retention_days: parseInt(imageDays),
                    password: password
                })
            })
            .then(r => r.json())
            .then(data => {
                msgDiv.style.display = 'block';
                if (data.success) {
                    msgDiv.className = 'msg success';
                    msgDiv.textContent = 'Retention settings saved!';
                    document.getElementById('retentionPassword').value = '';
                } else {
                    msgDiv.className = 'msg error';
                    msgDiv.textContent = data.error || 'Failed to save';
                }
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            })
            .catch(e => {
                msgDiv.style.display = 'block';
                msgDiv.className = 'msg error';
                msgDiv.textContent = 'Error: ' + e;
                setTimeout(() => { msgDiv.style.display = 'none'; }, 4000);
            });
        }

        // Init
        checkStatus();
    </script>
</body>
</html>
'''

def get_ip_address():
    """Get the Pi's primary LAN IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def get_local_host_name() -> str:
    """mDNS hostname (without .local) — e.g. copilot → copilot.local."""
    return DEVICE_LOCAL_HOST


def get_ui_access_urls() -> dict:
    """URLs for the provisioning UI (mDNS + IP)."""
    host = get_local_host_name()
    ip = get_ip_address()
    return {
        'local_host': host,
        'local_url': f'http://{host}.local:{UI_PORT}/',
        'ip_url': f'http://{ip}:{UI_PORT}/',
        'ip_address': ip,
        'ui_port': UI_PORT,
    }


# ============================================
# Service Management APIs
# ============================================

def get_service_status(service_name):
    """Get status of a systemd service"""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', f'{service_name}.service'],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except:
        return 'unknown'


def run_service_command(service_name, action):
    """Run systemctl command on a service"""
    try:
        result = subprocess.run(
            ['sudo', 'systemctl', action, f'{service_name}.service'],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0, result.stderr or result.stdout
    except Exception as e:
        return False, str(e)


# ============================================
# WiFi Management APIs
# ============================================

def get_wifi_status():
    """Get current WiFi connection status including AP mode"""
    status = {
        'connected': False,
        'ssid': '',
        'ip': get_ip_address(),
        'ap_active': False,
        'ap_ssid': None,
        'ap_password': None,
        'ap_ip': None,
    }
    
    # Check AP mode status if manager is available
    if ap_manager:
        try:
            ap_status = ap_manager.get_status()
            status['ap_active'] = ap_status.get('ap_active', False)
            if status['ap_active']:
                status['ap_ssid'] = ap_status.get('ap_ssid')
                status['ap_password'] = ap_status.get('ap_password')
                status['ap_ip'] = ap_status.get('ap_ip')
                status['ip'] = ap_status.get('ap_ip', status['ip'])
        except Exception as e:
            print(f"Error getting AP status: {e}")
    
    try:
        # Get current SSID
        result = subprocess.run(
            ['iwgetid', '-r'],
            capture_output=True, text=True, timeout=5
        )
        ssid = result.stdout.strip()
        
        if ssid:
            status['connected'] = True
            status['ssid'] = ssid
    except:
        pass
    
    return status


def scan_wifi_networks():
    """Scan for available WiFi networks"""
    try:
        result = subprocess.run(
            ['sudo', 'iwlist', 'wlan0', 'scan'],
            capture_output=True, text=True, timeout=30
        )
        
        networks = []
        current_ssid = None
        current_signal = 0
        
        for line in result.stdout.split('\n'):
            line = line.strip()
            if 'ESSID:' in line:
                ssid = line.split('ESSID:')[1].strip('"')
                if ssid and ssid not in [n['ssid'] for n in networks]:
                    networks.append({'ssid': ssid, 'signal': current_signal})
            elif 'Signal level=' in line:
                try:
                    # Parse signal level (format varies)
                    if 'dBm' in line:
                        dbm = int(line.split('Signal level=')[1].split(' ')[0])
                        current_signal = min(100, max(0, 2 * (dbm + 100)))
                    else:
                        current_signal = int(line.split('Signal level=')[1].split('/')[0])
                except:
                    current_signal = 50
        
        # Sort by signal strength
        networks.sort(key=lambda x: x['signal'], reverse=True)
        return networks[:10]  # Return top 10
    except Exception as e:
        return []


def connect_to_wifi(ssid, password):
    """Connect to a WiFi network using nmcli or wpa_supplicant"""
    debug_info = []
    
    try:
        # Check if NetworkManager is available
        nm_check = subprocess.run(
            ['systemctl', 'is-active', 'NetworkManager'],
            capture_output=True, text=True, timeout=5
        )
        nm_active = nm_check.stdout.strip() == 'active'
        debug_info.append(f"NetworkManager active: {nm_active}")
        print(f"[DEBUG] connect_to_wifi: NetworkManager active: {nm_active}")
        
        if nm_active:
            # Try nmcli first (NetworkManager)
            print(f"[DEBUG] connect_to_wifi: Trying nmcli connect to '{ssid}'")
            result = subprocess.run(
                ['sudo', 'nmcli', 'device', 'wifi', 'connect', ssid, 'password', password],
                capture_output=True, text=True, timeout=30
            )
            debug_info.append(f"nmcli returncode: {result.returncode}")
            debug_info.append(f"nmcli stdout: {result.stdout.strip()}")
            debug_info.append(f"nmcli stderr: {result.stderr.strip()}")
            print(f"[DEBUG] connect_to_wifi: nmcli result - code={result.returncode}, stdout={result.stdout.strip()}, stderr={result.stderr.strip()}")
            
            if result.returncode == 0:
                return True, 'Connected successfully'
            
            # nmcli failed - log the error
            error_msg = result.stderr.strip() or result.stdout.strip() or 'Unknown nmcli error'
            print(f"[DEBUG] connect_to_wifi: nmcli failed: {error_msg}")
        
        # Fallback: Update wpa_supplicant.conf
        print(f"[DEBUG] connect_to_wifi: Falling back to wpa_supplicant for '{ssid}'")
        debug_info.append("Falling back to wpa_supplicant")
        
        wpa_conf = f'''
network={{
    ssid="{ssid}"
    psk="{password}"
    key_mgmt=WPA-PSK
}}
'''
        # Append to wpa_supplicant.conf
        wpa_result = subprocess.run(
            ['sudo', 'bash', '-c', f'echo \'{wpa_conf}\' >> /etc/wpa_supplicant/wpa_supplicant.conf'],
            capture_output=True, text=True, timeout=10
        )
        debug_info.append(f"wpa_supplicant.conf update: {wpa_result.returncode}")
        print(f"[DEBUG] connect_to_wifi: wpa_supplicant.conf update result: {wpa_result.returncode}")
        
        # Reconfigure
        reconf_result = subprocess.run(
            ['sudo', 'wpa_cli', '-i', 'wlan0', 'reconfigure'],
            capture_output=True, text=True, timeout=10
        )
        debug_info.append(f"wpa_cli reconfigure: {reconf_result.returncode}, {reconf_result.stdout.strip()}")
        print(f"[DEBUG] connect_to_wifi: wpa_cli reconfigure result: {reconf_result.returncode}, {reconf_result.stdout.strip()}")
        
        # Wait for connection
        print(f"[DEBUG] connect_to_wifi: Waiting 5s for connection...")
        time.sleep(5)
        
        # Check if connected
        status = get_wifi_status()
        debug_info.append(f"Final status: connected={status['connected']}, ssid={status.get('ssid', 'N/A')}")
        print(f"[DEBUG] connect_to_wifi: Final status - connected={status['connected']}, ssid={status.get('ssid', 'N/A')}")
        
        if status['connected'] and status['ssid'] == ssid:
            return True, 'Connected successfully'
        
        # Connection failed - return debug info
        debug_str = '; '.join(debug_info)
        return False, f'Connection failed. Debug: {debug_str}'
        
    except subprocess.TimeoutExpired as e:
        error_msg = f'Timeout: {str(e)}'
        print(f"[DEBUG] connect_to_wifi: {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f'Error: {str(e)}'
        print(f"[DEBUG] connect_to_wifi: {error_msg}")
        return False, error_msg


# ============================================
# Camera Streaming
# ============================================

def generate_camera_frames():
    """Generator for camera frames (MJPEG stream)"""
    global camera
    
    try:
        import cv2
        
        with camera_lock:
            if camera is None:
                camera = cv2.VideoCapture(0)
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                camera.set(cv2.CAP_PROP_FPS, 10)
        
        while True:
            with camera_lock:
                if camera is None:
                    break
                success, frame = camera.read()
            
            if not success:
                break
            
            # Encode as JPEG
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 50])
            frame_bytes = buffer.tobytes()
            
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            
            time.sleep(0.1)  # ~10 FPS to save CPU
            
    except ImportError:
        yield b'--frame\r\nContent-Type: text/plain\r\n\r\nOpenCV not available\r\n'
    except Exception as e:
        yield f'--frame\r\nContent-Type: text/plain\r\n\r\nError: {e}\r\n'.encode()
    finally:
        # Runs on browser disconnect too; a held /dev/video0 stops facial1 from
        # reopening the camera and its restart limit reboots the device.
        stop_camera()


def stop_camera():
    """Stop camera and release resources"""
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None


# ============================================
# On-device driver events (folders under EVENTS_DIR)
# ============================================

def _parse_event_meta_file(meta_path: str) -> dict:
    """Parse event_meta.txt key=value lines."""
    meta = {
        'event_id': '',
        'event_type': 'Unknown',
        'start_time': '',
        'end_time': '',
        'frame_count': '0',
        'lat': '',
        'long': '',
        'speed': '',
    }
    try:
        with open(meta_path, 'r', encoding='utf-8') as handle:
            for line in handle:
                if '=' not in line:
                    continue
                key, value = line.strip().split('=', 1)
                if key in meta:
                    meta[key] = value
    except OSError:
        pass
    return meta


def _list_event_frame_files(folder_path: str) -> list:
    """Sorted frame_*.jpg paths inside an event folder."""
    try:
        names = [
            n for n in os.listdir(folder_path)
            if n.startswith('frame_') and n.endswith('.jpg')
        ]
    except OSError:
        return []
    names.sort()
    return names


def _resolve_event_file(rel_path: str) -> Optional[str]:
    """
    Map a safe relative path (folder/frame_XXXX.jpg) to an absolute file
    under EVENTS_DIR. Rejects traversal and non-JPEG frame names.
    """
    if not rel_path:
        return None
    rel_path = rel_path.replace('\\', '/').strip()
    if not rel_path or rel_path.startswith('/') or '..' in rel_path:
        return None
    parts = rel_path.split('/')
    if len(parts) != 2:
        return None
    folder, filename = parts
    if not folder or not filename:
        return None
    if not filename.startswith('frame_') or not filename.endswith('.jpg'):
        return None
    candidate = os.path.join(EVENTS_DIR, folder, filename)
    try:
        real_base = os.path.realpath(EVENTS_DIR)
        real_file = os.path.realpath(candidate)
    except OSError:
        return None
    if not real_file.startswith(real_base + os.sep):
        return None
    if not os.path.isfile(real_file):
        return None
    return real_file


_EVENT_FOLDER_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def _resolve_event_folder(folder_name: str) -> Optional[str]:
    """Map a safe event folder name to an absolute path under EVENTS_DIR."""
    if not folder_name:
        return None
    folder_name = folder_name.strip()
    if not _EVENT_FOLDER_RE.match(folder_name):
        return None
    candidate = os.path.join(EVENTS_DIR, folder_name)
    try:
        real_base = os.path.realpath(EVENTS_DIR)
        real_folder = os.path.realpath(candidate)
    except OSError:
        return None
    if not real_folder.startswith(real_base + os.sep):
        return None
    if not os.path.isdir(real_folder):
        return None
    return real_folder


def _delete_event_folder(folder_name: str) -> Tuple[bool, str]:
    """Remove one event folder and all frame JPEGs inside it."""
    folder_path = _resolve_event_folder(folder_name)
    if not folder_path:
        return False, 'Event folder not found'
    meta_path = os.path.join(folder_path, 'event_meta.txt')
    if not os.path.isfile(meta_path):
        return False, 'Not a valid event folder'
    try:
        shutil.rmtree(folder_path)
        return True, f'Deleted event {folder_name}'
    except OSError as e:
        return False, str(e)


def _delete_all_event_folders() -> Tuple[int, int, str]:
    """Remove every valid event folder under EVENTS_DIR."""
    if not os.path.isdir(EVENTS_DIR):
        return 0, 0, 'Events directory not found'

    try:
        folder_names = list(os.listdir(EVENTS_DIR))
    except OSError as e:
        return 0, 0, str(e)

    deleted = 0
    failed = 0
    for folder_name in folder_names:
        ok, _ = _delete_event_folder(folder_name)
        if ok:
            deleted += 1
        else:
            failed += 1

    if deleted == 0 and failed == 0:
        return 0, 0, 'No events to delete'
    if failed:
        return deleted, failed, f'Deleted {deleted} event(s); {failed} could not be removed'
    return deleted, failed, f'Deleted {deleted} event(s)'


def _scan_device_events() -> list:
    """Scan EVENTS_DIR for completed events (folder + event_meta.txt + frames)."""
    if not os.path.isdir(EVENTS_DIR):
        return []

    entries = []
    try:
        folder_names = os.listdir(EVENTS_DIR)
    except OSError:
        return []

    for folder_name in folder_names:
        folder_path = os.path.join(EVENTS_DIR, folder_name)
        if not os.path.isdir(folder_path):
            continue
        meta_path = os.path.join(folder_path, 'event_meta.txt')
        if not os.path.isfile(meta_path):
            continue

        frames = _list_event_frame_files(folder_path)
        if not frames:
            continue

        meta = _parse_event_meta_file(meta_path)
        frame_count = len(frames)
        try:
            meta_fc = int(meta.get('frame_count') or 0)
            if meta_fc > 0:
                frame_count = meta_fc
        except (TypeError, ValueError):
            pass

        thumb_name = frames[len(frames) // 2]
        rel_thumb = f"{folder_name}/{thumb_name}"

        start_iso = ''
        try:
            st = float(meta.get('start_time') or 0)
            if st > 0:
                start_iso = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st))
        except (TypeError, ValueError, OSError):
            start_iso = folder_name[:15].replace('_', ' ')

        try:
            mtime = os.path.getmtime(folder_path)
        except OSError:
            mtime = 0.0

        entries.append({
            'folder': folder_name,
            'event_id': meta.get('event_id') or folder_name,
            'event_type': meta.get('event_type') or 'Unknown',
            'frame_count': frame_count,
            'thumbnail': rel_thumb,
            'start_time': meta.get('start_time'),
            'start_time_iso': start_iso,
            'end_time': meta.get('end_time'),
            'speed': meta.get('speed') or '',
            'lat': meta.get('lat') or '',
            'long': meta.get('long') or '',
            '_mtime': mtime,
        })

    entries.sort(key=lambda e: e.get('_mtime', 0), reverse=True)
    for item in entries:
        item.pop('_mtime', None)
    return entries


# ============================================
# Flask Routes
# ============================================

@app.route('/')
def index():
    urls = get_ui_access_urls()
    return render_template_string(
        HTML_TEMPLATE,
        ip_address=urls['ip_address'],
        local_host=urls['local_host'],
        local_url=urls['local_url'],
        ip_url=urls['ip_url'],
        ui_port=urls['ui_port'],
    )


@app.route('/api/access')
def api_access():
    """How to open this UI (copilot.local and LAN IP)."""
    return jsonify({'success': True, **get_ui_access_urls()})


# --- Data Retention APIs ---

@app.route('/api/events/list')
def api_events_list():
    """List on-device driver event folders (type, frame count, thumbnail path)."""
    try:
        events = _scan_device_events()
        scanned_types = {e['event_type'] for e in events if e.get('event_type')}
        event_types = list(KNOWN_DRIVER_EVENT_TYPES) + sorted(
            t for t in scanned_types if t not in KNOWN_DRIVER_EVENT_TYPES
        )
        return jsonify({
            'success': True,
            'events': events,
            'event_types': event_types,
            'events_dir': EVENTS_DIR,
            'count': len(events),
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/events/file/<path:rel_path>')
def api_events_file(rel_path):
    """Stream a single event frame JPEG; path validated under EVENTS_DIR."""
    abs_path = _resolve_event_file(rel_path)
    if not abs_path:
        return jsonify({'success': False, 'error': 'Invalid or missing file'}), 404
    return send_file(abs_path, mimetype='image/jpeg', conditional=True)


@app.route('/api/events/delete', methods=['POST'])
def api_events_delete():
    """Delete one on-device event folder (all frames + event_meta.txt)."""
    try:
        data = request.get_json() or {}
        folder = (data.get('folder') or '').strip()
        if not folder:
            return jsonify({'success': False, 'error': 'Event folder name required'})

        ok, message = _delete_event_folder(folder)
        if ok:
            return jsonify({'success': True, 'message': message, 'folder': folder})
        return jsonify({'success': False, 'error': message})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/events/delete_all', methods=['POST'])
def api_events_delete_all():
    """Delete all on-device event folders (all frames + metadata)."""
    try:
        deleted, failed, message = _delete_all_event_folders()
        if deleted == 0 and failed == 0:
            return jsonify({'success': False, 'error': message})
        if failed and deleted == 0:
            return jsonify({'success': False, 'error': message, 'deleted': deleted, 'failed': failed})
        return jsonify({
            'success': True,
            'message': message,
            'deleted': deleted,
            'failed': failed,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/trips/list')
def api_trips_list():
    """List offline GPS trip sessions stored on this device."""
    try:
        limit = request.args.get('limit', '50')
        trips = gps_trip_manager.list_trips(limit=int(limit))
        return jsonify({'success': True, 'trips': trips, 'count': len(trips)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/trips/<int:trip_id>')
def api_trip_report(trip_id):
    """Full geo-tracing report for one offline GPS trip."""
    try:
        trip = gps_trip_manager.get_trip_report(trip_id)
        if not trip:
            return jsonify({'success': False, 'error': 'Trip not found'}), 404
        return jsonify({'success': True, 'trip': trip})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config/retention', methods=['GET'])
def api_get_retention():
    """Get data retention settings"""
    gps_days = get_configure('gps_retention_days')
    image_days = get_configure('image_retention_days')
    return jsonify({
        'gps_retention_days': int(gps_days) if gps_days else 30,
        'image_retention_days': int(image_days) if image_days else 15,
    })


@app.route('/api/config/retention', methods=['POST'])
def api_set_retention():
    """Set data retention settings (requires password)"""
    try:
        data = request.get_json()
        password = data.get('password', '')
        gps_days = data.get('gps_retention_days')
        image_days = data.get('image_retention_days')

        delete_password = get_delete_password()
        if not password or password != delete_password:
            return jsonify({'success': False, 'error': 'Invalid password'})

        if gps_days is not None:
            gps_days = int(gps_days)
            if gps_days < 1 or gps_days > 365:
                return jsonify({'success': False, 'error': 'GPS days must be 1-365'})
            set_configure('gps_retention_days', gps_days)

        if image_days is not None:
            image_days = int(image_days)
            if image_days < 1 or image_days > 365:
                return jsonify({'success': False, 'error': 'Image days must be 1-365'})
            set_configure('image_retention_days', image_days)

        return jsonify({'success': True})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid value'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# --- Vehicle registration (offline) ---

@app.route('/api/vehicle', methods=['GET'])
def api_get_vehicle():
    return jsonify(get_vehicle_info())


@app.route('/api/vehicle', methods=['POST'])
def api_set_vehicle():
    data = request.get_json(silent=True) or {}
    ok, error = save_vehicle_info(data.get('vehicle_number'), data.get('driver_name'))
    if not ok:
        return jsonify({'success': False, 'error': error})
    return jsonify({'success': True, **get_vehicle_info()})


# --- Configuration APIs ---

@app.route('/api/config/speed', methods=['GET'])
def api_get_speed():
    """Get activation speed configuration"""
    value = get_configure('speed')
    return jsonify({
        'value': int(value) if value else 20,
        'default': int(DEFAULT_CONFIG.get('speed', 20))
    })


@app.route('/api/config/speed', methods=['POST'])
def api_set_speed():
    """Set activation speed configuration"""
    try:
        data = request.get_json()
        value = data.get('value')

        if value is None:
            return jsonify({'success': False, 'error': 'Value is required'})

        value = int(value)
        if value < 0 or value > 200:
            return jsonify({'success': False, 'error': 'Speed must be between 0 and 200 km/h'})

        success = set_configure('speed', value)
        if success:
            return jsonify({'success': True, 'value': value})
        else:
            return jsonify({'success': False, 'error': 'Failed to save configuration'})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid speed value'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# --- Settings APIs (unified) ---

@app.route('/api/config/settings', methods=['GET'])
def api_get_settings():
    """Get all detection settings"""
    speed = get_configure('speed')
    speed_limit = get_configure('speed_limit')
    led = get_configure('led_blink_enabled')
    nf = get_configure('noface_enabled')
    nf_thresh = get_configure('noface_threshold')
    phone = get_configure('phone_detection_enabled')

    def _num(key, default):
        try:
            return float(get_configure(key))
        except (TypeError, ValueError):
            return default

    return jsonify({
        'speed': int(speed) if speed else 0,
        'speed_limit': int(speed_limit) if speed_limit else 0,
        'sleep_seconds': _num('sleep_seconds', 1.5),
        'led_blink_enabled': led != '0' if led is not None else True,
        'noface_enabled': nf == '1' if nf is not None else False,
        'noface_threshold': int(nf_thresh) if nf_thresh else 2,
        'phone_detection_enabled': phone != '0' if phone is not None else True,
        'phone_confirm_seconds': _num('phone_confirm_seconds', 0.3),
        'phone_buzz_seconds': _num('phone_buzz_seconds', 10),
        'phone_ear_dist_ratio': _num('phone_ear_dist_ratio', 0.5),
    })


@app.route('/api/config/settings', methods=['POST'])
def api_set_settings():
    """Save all detection settings"""
    try:
        data = request.get_json()

        speed = data.get('speed')
        if speed is not None:
            speed = int(speed)
            if speed < 0 or speed > 200:
                return jsonify({'success': False, 'error': 'Speed must be 0-200'})
            set_configure('speed', speed)

        speed_limit = data.get('speed_limit')
        if speed_limit is not None:
            speed_limit = int(speed_limit)
            if speed_limit < 0 or speed_limit > 200:
                return jsonify({'success': False, 'error': 'Speed Limit must be 0-200'})
            set_configure('speed_limit', speed_limit)

        led = data.get('led_blink_enabled')
        if led is not None:
            set_configure('led_blink_enabled', '1' if led else '0')

        nf = data.get('noface_enabled')
        if nf is not None:
            set_configure('noface_enabled', '1' if nf else '0')

        nf_thresh = data.get('noface_threshold')
        if nf_thresh is not None:
            nf_thresh = int(nf_thresh)
            if nf_thresh < 1 or nf_thresh > 30:
                return jsonify({'success': False, 'error': 'Threshold must be 1-30 seconds'})
            set_configure('noface_threshold', nf_thresh)

        phone = data.get('phone_detection_enabled')
        if phone is not None:
            set_configure('phone_detection_enabled', '1' if phone else '0')

        timing_ranges = (
            ('sleep_seconds', 0.5, 5.0, 'Sleep alert delay must be 0.5-5 seconds'),
            ('phone_confirm_seconds', 0.0, 10.0, 'Phone alert delay must be 0-10 seconds'),
            ('phone_buzz_seconds', 0.0, 60.0, 'Phone buzz duration must be 0-60 seconds'),
            ('phone_ear_dist_ratio', 0.2, 1.0, 'Phone ear distance must be 0.2-1.0'),
        )
        for key, lo, hi, err in timing_ranges:
            val = data.get(key)
            if val is None:
                continue
            val = float(val)
            if val < lo or val > hi:
                return jsonify({'success': False, 'error': err})
            set_configure(key, round(val, 2))

        return jsonify({'success': True})
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid value'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# --- Date & time (device is offline and the Pi Zero has no RTC) ---

_TIMEZONE_NAME = re.compile(r'^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*$')
_MIN_SET_EPOCH = 1704067200   # 2024-01-01
_MAX_SET_EPOCH = 4102444800   # 2100-01-01


def _device_timezone():
    try:
        result = subprocess.run(['timedatectl', 'show', '-p', 'Timezone', '--value'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return time.strftime('%Z')


def _device_time_payload():
    return {
        'epoch': time.time(),
        'local': time.strftime('%Y-%m-%d %H:%M:%S'),
        'timezone': _device_timezone(),
    }


@app.route('/api/system/time', methods=['GET'])
def api_get_system_time():
    return jsonify(_device_time_payload())


@app.route('/api/system/time', methods=['POST'])
def api_set_system_time():
    """Set the system clock (and optionally timezone) from the Settings page."""
    data = request.get_json() or {}
    try:
        epoch = int(float(data.get('epoch')))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid date/time'})
    if not _MIN_SET_EPOCH <= epoch < _MAX_SET_EPOCH:
        return jsonify({'success': False, 'error': 'Date must be between 2024 and 2099'})

    tz = (data.get('timezone') or '').strip()
    tz_changed = False
    if tz:
        if not _TIMEZONE_NAME.match(tz) or not os.path.isfile(f'/usr/share/zoneinfo/{tz}'):
            return jsonify({'success': False, 'error': f'Unknown timezone: {tz}'})
        if tz != _device_timezone():
            result = subprocess.run(['sudo', '-n', 'timedatectl', 'set-timezone', tz],
                                    capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                return jsonify({'success': False, 'error': result.stderr.strip() or 'Failed to set timezone'})
            tz_changed = True
            time.tzset()

    result = subprocess.run(['sudo', '-n', 'date', '-s', f'@{epoch}'],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return jsonify({'success': False, 'error': result.stderr.strip() or 'Failed to set time'})

    # Persist for the next boot: fake-hwclock restores the last saved time (no RTC battery)
    for cmd in (['sudo', '-n', 'fake-hwclock', 'save'], ['sudo', '-n', 'hwclock', '--systohc']):
        try:
            subprocess.run(cmd, capture_output=True, timeout=15)
        except Exception:
            pass

    # Running services read the clock on every frame, so a new time applies
    # immediately; the timezone is only loaded at process start.
    if tz_changed:
        for service in ('facial1', 'get_gps_data1'):
            run_service_command(service, 'restart')
    print(f"System time set to {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"({'timezone ' + tz if tz_changed else 'timezone unchanged'})")
    return jsonify({'success': True, 'services_restarted': tz_changed, **_device_time_payload()})


def get_delete_password():
    """Get delete password from .env file or environment"""
    def _normalize_password(value: str) -> str:
        """Normalize password values loaded from env/request."""
        value = (value or '').strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value.strip()

    # First try to read directly from .env file
    env_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'),
        '/home/pi/facial-tracker-firmware/.env'
    ]
    for env_file in env_paths:
        if os.path.exists(env_file):
            try:
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if line.startswith('export '):
                            line = line[len('export '):].strip()
                        if line.startswith('DELETE_DEVICE_PASSWORD='):
                            value = _normalize_password(line.split('=', 1)[1])
                            print(f"[DEBUG] get_delete_password: Found in {env_file}")
                            return value
            except Exception as e:
                print(f"[DEBUG] get_delete_password: Error reading {env_file}: {e}")
    
    # Fallback to environment variable
    env_val = _normalize_password(os.getenv("DELETE_DEVICE_PASSWORD", "Sapience@2128"))
    print("[DEBUG] get_delete_password: Using os.getenv")
    return env_val


@app.route('/api/device/delete', methods=['POST'])
def api_delete_device():
    """Factory reset: vehicle details + recorded data (requires password)"""
    try:
        data = request.get_json() or {}
        password = (data.get('password', '') or '').strip()
        
        # Read password dynamically from .env file
        delete_password = get_delete_password()
        
        if not password:
            return jsonify({
                'success': False,
                'error': 'Password is required'
            })
        
        if password != delete_password:
            return jsonify({
                'success': False,
                'error': 'Invalid password'
            })
        
        vehicle_number = get_vehicle_info().get('vehicle_number') or 'unregistered vehicle'
        success = factory_reset()
        if success:
            # Restart recorders so they drop in-memory trip/event state
            services_to_restart = [
                'facial1',
                'get_gps_data1',
            ]
            restart_results = []
            for svc in services_to_restart:
                try:
                    result = subprocess.run(
                        ['sudo', 'systemctl', 'restart', f'{svc}.service'],
                        capture_output=True, text=True, timeout=10
                    )
                    restart_results.append(f"{svc}: {'ok' if result.returncode == 0 else 'failed'}")
                except Exception as e:
                    restart_results.append(f"{svc}: error - {e}")
            
            return jsonify({
                'success': True,
                'message': f'Factory reset of {vehicle_number} complete. Services restarted: {", ".join(restart_results)}'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Factory reset failed - check logs'
            })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/services')
def api_services():
    """Get status of all services"""
    services = []
    for svc in SERVICES:
        status = get_service_status(svc['name'])
        services.append({
            'name': svc['name'],
            'display': svc['display'],
            'description': svc['description'],
            'status': status,
            'active': status == 'active'
        })
    return jsonify({'services': services})


@app.route('/api/services/<name>/<action>', methods=['POST'])
def api_service_control(name, action):
    """Start or stop a service"""
    if action not in ['start', 'stop', 'restart']:
        return jsonify({'success': False, 'message': 'Invalid action'})
    
    # Verify service is in our list
    if name not in [s['name'] for s in SERVICES]:
        return jsonify({'success': False, 'message': 'Unknown service'})
    
    success, message = run_service_command(name, action)
    return jsonify({
        'success': success,
        'message': f'{name} {action}ed' if success else message
    })


@app.route('/api/services/all/<action>', methods=['POST'])
def api_services_all(action):
    """Start or stop all services"""
    if action not in ['start', 'stop']:
        return jsonify({'success': False, 'message': 'Invalid action'})
    
    for svc in SERVICES:
        run_service_command(svc['name'], action)
    
    return jsonify({
        'success': True,
        'message': f'All services {action}ed'
    })


# --- Camera APIs ---

@app.route('/api/camera/stream')
def api_camera_stream():
    """MJPEG camera stream"""
    return Response(
        generate_camera_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/api/camera/stop', methods=['POST'])
def api_camera_stop():
    """Stop camera stream"""
    stop_camera()
    return jsonify({'success': True})


# --- WiFi APIs ---

@app.route('/api/wifi/status')
def api_wifi_status():
    """Get WiFi connection status"""
    return jsonify(get_wifi_status())


@app.route('/api/wifi/scan')
def api_wifi_scan():
    """Scan for WiFi networks"""
    networks = scan_wifi_networks()
    return jsonify({'networks': networks})


@app.route('/api/wifi/connect', methods=['POST'])
def api_wifi_connect():
    """Connect to a WiFi network"""
    data = request.get_json()
    ssid = data.get('ssid', '')
    password = data.get('password', '')
    
    if not ssid:
        return jsonify({'success': False, 'message': 'SSID required'})
    
    # If AP manager is available, use it for better handling
    if ap_manager:
        result = ap_manager.connect_to_wifi(ssid, password)
        return jsonify(result)
    
    success, message = connect_to_wifi(ssid, password)
    return jsonify({'success': success, 'message': message})


# --- AP Mode APIs ---

@app.route('/api/wifi/ap/start', methods=['POST'])
def api_ap_start():
    """Start WiFi Access Point mode"""
    if not ap_manager:
        return jsonify({
            'success': False,
            'error': 'AP Manager not available. Install wifi_ap_manager.py'
        })
    
    try:
        result = ap_manager.start_ap_mode()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/wifi/ap/stop', methods=['POST'])
def api_ap_stop():
    """Stop WiFi Access Point mode"""
    if not ap_manager:
        return jsonify({
            'success': False,
            'error': 'AP Manager not available'
        })
    
    try:
        result = ap_manager.stop_ap_mode()
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/wifi/ap/status')
def api_ap_status():
    """Get AP mode status"""
    if not ap_manager:
        return jsonify({
            'ap_available': False,
            'ap_active': False,
        })
    
    try:
        status = ap_manager.get_status()
        status['ap_available'] = True
        return jsonify(status)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/wifi/reset', methods=['POST'])
def api_wifi_reset():
    """Reset WiFi - forget all networks and start AP mode"""
    if not ap_manager:
        return jsonify({
            'success': False,
            'error': 'AP Manager not available. Install wifi_ap_manager.py'
        })
    
    try:
        result = ap_manager.reset_wifi(start_ap=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/health', methods=['GET'])
def api_health():
    """Lightweight liveness check for the UI (used after service restarts)."""
    return jsonify({'ok': True, 'service': 'provisioning_ui'})



# ============================================
# Data Retention Cleanup
# ============================================

def _run_retention_cleanup():
    """Delete old GPS data and images based on retention settings.
    GPS retention uses 'active days' — only days with data count.
    Image retention uses calendar days from folder name (YYYYMMDD format).
    """
    import shutil
    import db_helper
    from datetime import datetime, timedelta

    try:
        # Get retention settings
        gps_days_str = get_configure('gps_retention_days')
        image_days_str = get_configure('image_retention_days')
        gps_days = int(gps_days_str) if gps_days_str else 30
        image_days = int(image_days_str) if image_days_str else 15

        # --- GPS data cleanup (active days) ---
        # Get list of distinct active days, ordered newest first
        rows = db_helper.fetchall(
            "SELECT DISTINCT date(timestamp) as day FROM gps_data ORDER BY day DESC")
        if rows:
            active_days = [r['day'] for r in rows if r['day']]
            if len(active_days) > gps_days:
                # Keep only the most recent N active days
                cutoff_day = active_days[gps_days - 1]  # last day to keep
                # Timestamps are 'YYYY-MM-DD HH:MM:SS...', so a string compare
                # matches date() but can use the index. Small batches keep the
                # WAL and SD-card write bursts small.
                for table in ("gps_data", "car_data"):
                    while True:
                        cur = db_helper.execute(
                            f"DELETE FROM {table} WHERE rowid IN "
                            f"(SELECT rowid FROM {table} WHERE timestamp < ? LIMIT 5000)",
                            (cutoff_day,))
                        db_helper.get_connection().commit()
                        if cur.rowcount < 5000:
                            break
                        time.sleep(0.2)
                db_helper.execute_commit(
                    "DELETE FROM gps_trips WHERE status != 'active' AND end_time < ?",
                    (cutoff_day,))
                print(f"[Retention] GPS: kept {gps_days} active days, cutoff={cutoff_day}")

        # --- Image cleanup (calendar days from folder names) ---
        images_base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
        if os.path.exists(images_base):
            cutoff_date = datetime.now() - timedelta(days=image_days)
            for item in os.listdir(images_base):
                item_path = os.path.join(images_base, item)
                if not os.path.isdir(item_path):
                    continue
                # Skip 'events' and 'pending' folders — check date-named folders
                if item in ('events', 'pending'):
                    # For events folder, clean subfolders by date prefix (YYYYMMDD_*)
                    if item == 'events':
                        for event_dir in os.listdir(item_path):
                            try:
                                date_str = event_dir[:8]  # YYYYMMDD
                                folder_date = datetime.strptime(date_str, '%Y%m%d')
                                if folder_date < cutoff_date:
                                    shutil.rmtree(os.path.join(item_path, event_dir))
                                    print(f"[Retention] Deleted event folder: {event_dir}")
                            except (ValueError, OSError):
                                continue
                    continue
                # Date-named daily folders (e.g., 2026-02-10 or 20260210)
                try:
                    # Try YYYY-MM-DD format first, then YYYYMMDD
                    try:
                        folder_date = datetime.strptime(item, '%Y-%m-%d')
                    except ValueError:
                        folder_date = datetime.strptime(item, '%Y%m%d')
                    if folder_date < cutoff_date:
                        shutil.rmtree(item_path)
                        print(f"[Retention] Deleted image folder: {item}")
                except (ValueError, OSError):
                    continue

    except Exception as e:
        print(f"[Retention] Cleanup error: {e}")


def _retention_cleanup_loop():
    """Run cleanup every hour."""
    time.sleep(60)  # Wait 1 min after startup
    while True:
        try:
            _run_retention_cleanup()
        except Exception as e:
            print(f"[Retention] Loop error: {e}")
        time.sleep(3600)  # Every hour


# ============================================
# Main
# ============================================

if __name__ == '__main__':
    print("=" * 50)
    print("Device Manager UI")
    print("=" * 50)
    urls = get_ui_access_urls()
    print(f"Open in browser: {urls['local_url']}")
    print(f"              or: {urls['ip_url']}")
    print("=" * 50)

    # Start data retention cleanup thread
    cleanup_thread = threading.Thread(target=_retention_cleanup_loop, daemon=True)
    cleanup_thread.start()

    # Run with minimal resources
    app.run(
        host='0.0.0.0',
        port=UI_PORT,
        debug=False,
        threaded=True,  # Need threads for camera streaming
        use_reloader=False
    )
