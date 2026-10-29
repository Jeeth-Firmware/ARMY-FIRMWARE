#!/usr/bin/env python3
"""
WiFi Access Point Manager for Raspberry Pi
Provides fallback AP mode when no known WiFi networks are available.

Features:
- Scan for known WiFi networks
- Start AP mode (hostapd + dnsmasq) if no networks found
- Stop AP mode and reconnect to WiFi
- Captive portal support for easy device discovery

Usage:
    from wifi_ap_manager import WiFiAPManager
    
    ap_manager = WiFiAPManager()
    if not ap_manager.has_known_networks():
        ap_manager.start_ap_mode()
"""

import os
import subprocess
import time
import logging
import socket
import re
from pathlib import Path
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger("wifi_ap_manager")


class WiFiAPConfig:
    """Configuration for WiFi AP mode"""
    # AP Settings
    AP_SSID = os.getenv("AP_SSID", "copilotai")
    AP_PASSWORD = os.getenv("AP_PASSWORD", "sapience123")  # Min 8 chars for WPA2
    AP_CHANNEL = int(os.getenv("AP_CHANNEL", "6"))
    AP_INTERFACE = os.getenv("AP_INTERFACE", "wlan0")
    
    # Network Settings for AP mode
    AP_IP = os.getenv("AP_IP", "192.168.4.1")
    AP_NETMASK = os.getenv("AP_NETMASK", "255.255.255.0")
    AP_DHCP_START = os.getenv("AP_DHCP_START", "192.168.4.10")
    AP_DHCP_END = os.getenv("AP_DHCP_END", "192.168.4.50")
    AP_DHCP_LEASE = os.getenv("AP_DHCP_LEASE", "12h")
    
    # Config file paths
    HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
    DNSMASQ_CONF = "/etc/dnsmasq.d/ap-mode.conf"
    WPA_SUPPLICANT_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
    
    # Timeouts
    WIFI_CONNECT_TIMEOUT = int(os.getenv("WIFI_CONNECT_TIMEOUT", "30"))
    NETWORK_CHECK_TIMEOUT = int(os.getenv("NETWORK_CHECK_TIMEOUT", "10"))


class WiFiAPManager:
    """Manages WiFi AP mode for device provisioning"""
    
    def __init__(self, device_id: str = None):
        self.device_id = device_id or self._get_device_id()
        self._ap_active = False
        self._original_wpa_conf = None
        
        # Customize SSID with device ID suffix for uniqueness (format: copilotai-xxxx)
        if self.device_id:
            short_id = self.device_id[-4:].lower() if len(self.device_id) >= 4 else self.device_id.lower()
            self.ap_ssid = f"{WiFiAPConfig.AP_SSID}-{short_id}"
        else:
            self.ap_ssid = f"{WiFiAPConfig.AP_SSID}-0000"
    
    def _get_device_id(self) -> str:
        """Get device ID from serial number or MAC address"""
        # Try to get from Pi serial
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('Serial'):
                        return line.split(':')[1].strip()[-8:]
        except:
            pass
        
        # Fallback to MAC address
        try:
            result = subprocess.run(
                ['cat', f'/sys/class/net/{WiFiAPConfig.AP_INTERFACE}/address'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                mac = result.stdout.strip().replace(':', '')
                return mac[-8:]
        except:
            pass
        
        return "0000"
    
    def get_known_networks(self) -> List[str]:
        """Get list of known/configured WiFi networks from wpa_supplicant.conf"""
        networks = []
        try:
            wpa_conf = Path(WiFiAPConfig.WPA_SUPPLICANT_CONF)
            if wpa_conf.exists():
                content = wpa_conf.read_text()
                # Parse SSID from network blocks
                ssid_pattern = r'ssid="([^"]+)"'
                matches = re.findall(ssid_pattern, content)
                networks = list(set(matches))
                logger.info(f"Found {len(networks)} known networks: {networks}")
        except Exception as e:
            logger.error(f"Error reading wpa_supplicant.conf: {e}")
        return networks
    
    def scan_available_networks(self) -> List[Dict]:
        """Scan for available WiFi networks"""
        networks = []
        try:
            # Ensure interface is up
            subprocess.run(['sudo', 'ip', 'link', 'set', WiFiAPConfig.AP_INTERFACE, 'up'],
                          capture_output=True, timeout=5)
            time.sleep(1)
            
            result = subprocess.run(
                ['sudo', 'iwlist', WiFiAPConfig.AP_INTERFACE, 'scan'],
                capture_output=True, text=True, timeout=30
            )
            
            current_network = {}
            for line in result.stdout.split('\n'):
                line = line.strip()
                if 'Cell' in line and 'Address' in line:
                    if current_network.get('ssid'):
                        networks.append(current_network)
                    current_network = {'signal': 0}
                elif 'ESSID:' in line:
                    ssid = line.split('ESSID:')[1].strip('"')
                    current_network['ssid'] = ssid
                elif 'Signal level=' in line:
                    try:
                        if 'dBm' in line:
                            dbm = int(line.split('Signal level=')[1].split(' ')[0])
                            current_network['signal'] = min(100, max(0, 2 * (dbm + 100)))
                        else:
                            level = line.split('Signal level=')[1].split('/')[0]
                            current_network['signal'] = int(level)
                    except:
                        current_network['signal'] = 50
            
            if current_network.get('ssid'):
                networks.append(current_network)
            
            # Remove duplicates and sort by signal
            seen = set()
            unique_networks = []
            for n in networks:
                if n['ssid'] and n['ssid'] not in seen:
                    seen.add(n['ssid'])
                    unique_networks.append(n)
            
            unique_networks.sort(key=lambda x: x['signal'], reverse=True)
            logger.info(f"Found {len(unique_networks)} available networks")
            return unique_networks
            
        except Exception as e:
            logger.error(f"Error scanning networks: {e}")
            return []
    
    def has_known_networks_available(self) -> Tuple[bool, List[str]]:
        """Check if any known networks are currently available"""
        known = self.get_known_networks()
        if not known:
            logger.info("No known networks configured")
            return False, []
        
        available = self.scan_available_networks()
        available_ssids = [n['ssid'] for n in available]
        
        found = [ssid for ssid in known if ssid in available_ssids]
        if found:
            logger.info(f"Found known networks available: {found}")
            return True, found
        
        logger.info(f"No known networks available. Known: {known}, Available: {available_ssids}")
        return False, []
    
    def is_connected_to_wifi(self) -> Tuple[bool, str]:
        """Check if currently connected to a WiFi network"""
        try:
            result = subprocess.run(
                ['iwgetid', '-r'],
                capture_output=True, text=True, timeout=5
            )
            ssid = result.stdout.strip()
            if ssid:
                return True, ssid
            return False, ""
        except:
            return False, ""
    
    def get_current_ip(self) -> str:
        """Get current IP address"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return WiFiAPConfig.AP_IP if self._ap_active else "unknown"
    
    def is_ap_mode_active(self) -> bool:
        """Check if AP mode is currently active"""
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', 'hostapd'],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip() == 'active'
        except:
            return False
    
    def _create_hostapd_config(self) -> str:
        """Generate hostapd configuration"""
        config = f"""# Auto-generated hostapd config for AP mode
interface={WiFiAPConfig.AP_INTERFACE}
driver=nl80211
ssid={self.ap_ssid}
hw_mode=g
channel={WiFiAPConfig.AP_CHANNEL}
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase={WiFiAPConfig.AP_PASSWORD}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""
        return config
    
    def _create_dnsmasq_config(self) -> str:
        """Generate dnsmasq configuration for DHCP in AP mode"""
        config = f"""# Auto-generated dnsmasq config for AP mode
interface={WiFiAPConfig.AP_INTERFACE}
dhcp-range={WiFiAPConfig.AP_DHCP_START},{WiFiAPConfig.AP_DHCP_END},{WiFiAPConfig.AP_NETMASK},{WiFiAPConfig.AP_DHCP_LEASE}
address=/#/{WiFiAPConfig.AP_IP}
"""
        return config
    
    def _setup_network_interface(self) -> bool:
        """Configure network interface for AP mode"""
        try:
            interface = WiFiAPConfig.AP_INTERFACE
            ip = WiFiAPConfig.AP_IP
            netmask = WiFiAPConfig.AP_NETMASK
            
            # Stop NetworkManager and wpa_supplicant to release the interface
            # NetworkManager must be stopped first as it manages wpa_supplicant
            if self._is_networkmanager_active():
                logger.info("Stopping NetworkManager to release wlan0 for AP mode...")
                subprocess.run(['sudo', 'nmcli', 'device', 'set', interface, 'managed', 'no'],
                              capture_output=True, timeout=10)
                subprocess.run(['sudo', 'systemctl', 'stop', 'NetworkManager'],
                              capture_output=True, timeout=10)
            
            subprocess.run(['sudo', 'systemctl', 'stop', 'wpa_supplicant'], 
                          capture_output=True, timeout=10)
            subprocess.run(['sudo', 'killall', 'wpa_supplicant'], 
                          capture_output=True, timeout=5)
            time.sleep(1)
            
            # Flush existing IP and bring interface down
            subprocess.run(['sudo', 'ip', 'addr', 'flush', 'dev', interface],
                          capture_output=True, timeout=5)
            subprocess.run(['sudo', 'ip', 'link', 'set', interface, 'down'],
                          capture_output=True, timeout=5)
            time.sleep(0.5)
            
            # Set static IP for AP mode
            subprocess.run(['sudo', 'ip', 'addr', 'add', f'{ip}/24', 'dev', interface],
                          capture_output=True, timeout=5)
            subprocess.run(['sudo', 'ip', 'link', 'set', interface, 'up'],
                          capture_output=True, timeout=5)
            
            logger.info(f"Configured {interface} with IP {ip}")
            return True
            
        except Exception as e:
            logger.error(f"Error setting up network interface: {e}")
            return False
    
    def _restore_network_interface(self) -> bool:
        """Restore network interface for normal WiFi mode"""
        try:
            interface = WiFiAPConfig.AP_INTERFACE
            
            # Flush AP mode IP
            subprocess.run(['sudo', 'ip', 'addr', 'flush', 'dev', interface],
                          capture_output=True, timeout=5)
            
            # Check if NetworkManager should be used
            # Try to restart NetworkManager first (preferred on modern systems)
            nm_result = subprocess.run(['sudo', 'systemctl', 'start', 'NetworkManager'],
                                       capture_output=True, timeout=10)
            if nm_result.returncode == 0:
                # NetworkManager is available, let it manage the interface
                subprocess.run(['sudo', 'nmcli', 'device', 'set', interface, 'managed', 'yes'],
                              capture_output=True, timeout=10)
                logger.info("Restored network interface using NetworkManager")
            else:
                # Fallback to wpa_supplicant
                subprocess.run(['sudo', 'systemctl', 'start', 'wpa_supplicant'],
                              capture_output=True, timeout=10)
                subprocess.run(['sudo', 'systemctl', 'restart', 'dhcpcd'],
                              capture_output=True, timeout=10)
                logger.info("Restored network interface using wpa_supplicant")
            
            return True
            
        except Exception as e:
            logger.error(f"Error restoring network interface: {e}")
            return False
    
    def start_ap_mode(self, force_restart: bool = False) -> Dict:
        """Start WiFi Access Point mode"""
        if not force_restart and (self._ap_active or self.is_ap_mode_active()):
            return {
                "success": True,
                "message": "AP mode already active",
                "ssid": self.ap_ssid,
                "ip": WiFiAPConfig.AP_IP,
                "password": WiFiAPConfig.AP_PASSWORD,
                "url": f"http://{WiFiAPConfig.AP_IP}:5000",
            }
        
        try:
            logger.info(f"Starting AP mode with SSID: {self.ap_ssid}")
            
            # Step 1: Install required packages if not present
            self._ensure_packages_installed()
            
            # Step 2: Create configuration files
            hostapd_conf = self._create_hostapd_config()
            dnsmasq_conf = self._create_dnsmasq_config()
            
            # Write hostapd config
            subprocess.run(
                ['sudo', 'bash', '-c', f'echo "{hostapd_conf}" > {WiFiAPConfig.HOSTAPD_CONF}'],
                capture_output=True, timeout=10
            )
            
            # Write dnsmasq config
            subprocess.run(
                ['sudo', 'bash', '-c', f'echo "{dnsmasq_conf}" > {WiFiAPConfig.DNSMASQ_CONF}'],
                capture_output=True, timeout=10
            )
            
            # Step 3: Configure network interface
            if not self._setup_network_interface():
                return {"success": False, "error": "Failed to configure network interface"}
            
            # Step 4: Stop conflicting services
            subprocess.run(['sudo', 'systemctl', 'stop', 'dnsmasq'], capture_output=True, timeout=10)
            subprocess.run(['sudo', 'systemctl', 'stop', 'hostapd'], capture_output=True, timeout=10)
            time.sleep(1)
            
            # Step 5: Start dnsmasq for DHCP
            result = subprocess.run(
                ['sudo', 'systemctl', 'start', 'dnsmasq'],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                logger.warning(f"dnsmasq start warning: {result.stderr}")
            
            # Step 6: Start hostapd
            # First unmask if masked
            subprocess.run(['sudo', 'systemctl', 'unmask', 'hostapd'], capture_output=True, timeout=5)
            
            result = subprocess.run(
                ['sudo', 'systemctl', 'start', 'hostapd'],
                capture_output=True, text=True, timeout=15
            )
            
            time.sleep(2)
            
            # Verify AP is running
            if self.is_ap_mode_active():
                self._ap_active = True
                logger.info(f"AP mode started successfully. SSID: {self.ap_ssid}, IP: {WiFiAPConfig.AP_IP}")
                return {
                    "success": True,
                    "message": "AP mode started",
                    "ssid": self.ap_ssid,
                    "ip": WiFiAPConfig.AP_IP,
                    "password": WiFiAPConfig.AP_PASSWORD,
                    "url": f"http://{WiFiAPConfig.AP_IP}:5000",
                }
            else:
                # Check hostapd status for error details
                status = subprocess.run(
                    ['sudo', 'systemctl', 'status', 'hostapd'],
                    capture_output=True, text=True, timeout=10
                )
                logger.error(f"hostapd failed to start: {status.stdout}\n{status.stderr}")
                return {"success": False, "error": "Failed to start hostapd", "details": status.stdout}
            
        except Exception as e:
            logger.error(f"Error starting AP mode: {e}")
            return {"success": False, "error": str(e)}
    
    def stop_ap_mode(self) -> Dict:
        """Stop AP mode and restore normal WiFi"""
        try:
            logger.info("Stopping AP mode...")
            
            # Stop hostapd and dnsmasq
            subprocess.run(['sudo', 'systemctl', 'stop', 'hostapd'], capture_output=True, timeout=10)
            subprocess.run(['sudo', 'systemctl', 'stop', 'dnsmasq'], capture_output=True, timeout=10)
            
            # Remove AP mode dnsmasq config
            subprocess.run(['sudo', 'rm', '-f', WiFiAPConfig.DNSMASQ_CONF], capture_output=True, timeout=5)
            
            # Restore network interface
            self._restore_network_interface()
            
            self._ap_active = False
            
            # Wait for WiFi to reconnect
            time.sleep(5)
            connected, ssid = self.is_connected_to_wifi()
            
            return {
                "success": True,
                "message": "AP mode stopped",
                "wifi_connected": connected,
                "wifi_ssid": ssid,
                "ip": self.get_current_ip(),
            }
            
        except Exception as e:
            logger.error(f"Error stopping AP mode: {e}")
            return {"success": False, "error": str(e)}
    
    def _ensure_packages_installed(self):
        """Ensure hostapd and dnsmasq are installed"""
        try:
            # Check if hostapd is installed
            result = subprocess.run(['which', 'hostapd'], capture_output=True, timeout=5)
            if result.returncode != 0:
                logger.info("Installing hostapd...")
                subprocess.run(['sudo', 'apt-get', 'update'], capture_output=True, timeout=60)
                subprocess.run(['sudo', 'apt-get', 'install', '-y', 'hostapd'], 
                              capture_output=True, timeout=120)
            
            # Check if dnsmasq is installed
            result = subprocess.run(['which', 'dnsmasq'], capture_output=True, timeout=5)
            if result.returncode != 0:
                logger.info("Installing dnsmasq...")
                subprocess.run(['sudo', 'apt-get', 'install', '-y', 'dnsmasq'],
                              capture_output=True, timeout=120)
                
        except Exception as e:
            logger.warning(f"Could not verify/install packages: {e}")
    
    def add_wifi_network(self, ssid: str, password: str, priority: int = 10) -> Dict:
        """Add a new WiFi network to wpa_supplicant.conf"""
        try:
            network_block = f'''
network={{
    ssid="{ssid}"
    psk="{password}"
    key_mgmt=WPA-PSK
    priority={priority}
}}
'''
            # Append to wpa_supplicant.conf
            subprocess.run(
                ['sudo', 'bash', '-c', f'echo \'{network_block}\' >> {WiFiAPConfig.WPA_SUPPLICANT_CONF}'],
                capture_output=True, timeout=10
            )
            
            logger.info(f"Added WiFi network: {ssid}")
            return {"success": True, "message": f"Added network: {ssid}"}
            
        except Exception as e:
            logger.error(f"Error adding WiFi network: {e}")
            return {"success": False, "error": str(e)}
    
    def connect_to_wifi(self, ssid: str, password: str) -> Dict:
        """Connect to a WiFi network (adds it and connects)"""
        was_in_ap_mode = self._ap_active or self.is_ap_mode_active()
        
        try:
            # If in AP mode, stop it first
            if was_in_ap_mode:
                logger.info("Stopping AP mode to connect to WiFi...")
                self.stop_ap_mode()
                time.sleep(2)
            
            # Check if NetworkManager is active (it's started by stop_ap_mode)
            if self._is_networkmanager_active():
                logger.info(f"Using NetworkManager to connect to {ssid}...")
                
                # Remember current connection for recovery
                _, previous_ssid = self.is_connected_to_wifi()
                if previous_ssid:
                    logger.info(f"Currently connected to: {previous_ssid}")
                
                # Also add to wpa_supplicant.conf for future boots without NetworkManager
                self.add_wifi_network(ssid, password)
                
                # Rescan WiFi networks to ensure the target network is visible
                logger.info("Rescanning WiFi networks...")
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'wifi', 'rescan'],
                    capture_output=True, text=True, timeout=10
                )
                time.sleep(3)  # Give more time for scan to complete
                
                # Check if target network is visible
                scan_result = subprocess.run(
                    ['nmcli', '-t', '-f', 'SSID', 'device', 'wifi', 'list'],
                    capture_output=True, text=True, timeout=10
                )
                available_networks = [s.strip() for s in scan_result.stdout.strip().split('\n') if s.strip()]
                if ssid not in available_networks:
                    logger.warning(f"Target network {ssid} not visible in scan. Available: {available_networks[:5]}")
                    # Still try to connect - it might work
                
                # Delete existing connection if any (to avoid WPA3/SAE issues)
                subprocess.run(
                    ['sudo', 'nmcli', 'connection', 'delete', ssid],
                    capture_output=True, text=True, timeout=10
                )
                
                # Create connection with WPA-PSK (WPA2) explicitly to avoid WPA3/SAE compatibility issues
                logger.info(f"Creating WiFi connection for {ssid} with WPA-PSK...")
                create_result = subprocess.run(
                    ['sudo', 'nmcli', 'connection', 'add', 'type', 'wifi', 
                     'con-name', ssid, 'ssid', ssid,
                     'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password],
                    capture_output=True, text=True, timeout=30
                )
                if create_result.returncode != 0:
                    logger.warning(f"Failed to create connection: {create_result.stderr}")
                
                # Use nmcli to connect
                result = subprocess.run(
                    ['sudo', 'nmcli', 'connection', 'up', ssid],
                    capture_output=True, text=True, timeout=30
                )
                
                if result.returncode == 0:
                    # Wait a moment for connection to establish
                    time.sleep(3)
                    
                    # Verify we're actually connected to the target SSID
                    connected, current_ssid = self.is_connected_to_wifi()
                    if connected and current_ssid == ssid:
                        ip = self.get_current_ip()
                        logger.info(f"Connected to {ssid} with IP {ip}")
                        return {
                            "success": True,
                            "message": f"Connected to {ssid}",
                            "ssid": ssid,
                            "ip": ip,
                        }
                    else:
                        # nmcli returned success but we're not on the target network
                        logger.warning(f"nmcli returned success but connected to {current_ssid} instead of {ssid}")
                        # Try again with explicit connection activation
                        logger.info(f"Trying nmcli connection up for {ssid}...")
                        retry_result = subprocess.run(
                            ['sudo', 'nmcli', 'connection', 'up', ssid],
                            capture_output=True, text=True, timeout=30
                        )
                        time.sleep(3)
                        connected, current_ssid = self.is_connected_to_wifi()
                        if connected and current_ssid == ssid:
                            ip = self.get_current_ip()
                            logger.info(f"Connected to {ssid} with IP {ip} (after retry)")
                            return {
                                "success": True,
                                "message": f"Connected to {ssid}",
                                "ssid": ssid,
                                "ip": ip,
                            }
                        else:
                            error_msg = f"Failed to switch to {ssid}, still on {current_ssid or 'nothing'}"
                            logger.error(error_msg)
                            
                            # If disconnected, try to reconnect to previous network
                            if not current_ssid and previous_ssid:
                                logger.info(f"Trying to reconnect to previous network: {previous_ssid}")
                                subprocess.run(
                                    ['sudo', 'nmcli', 'connection', 'up', previous_ssid],
                                    capture_output=True, text=True, timeout=30
                                )
                                time.sleep(3)
                                connected, recovered_ssid = self.is_connected_to_wifi()
                                if connected:
                                    logger.info(f"Reconnected to {recovered_ssid}")
                                    return {"success": False, "error": error_msg, "recovered": True, "recovered_ssid": recovered_ssid}
                            
                            # Return failure
                            if current_ssid:
                                return {"success": False, "error": error_msg, "current_ssid": current_ssid}
                else:
                    error_msg = result.stderr.strip() or result.stdout.strip() or "nmcli connection failed"
                    logger.error(f"nmcli failed: {error_msg}")
                    
                    # If disconnected, try to reconnect to previous network first
                    if previous_ssid:
                        logger.info(f"Trying to reconnect to previous network: {previous_ssid}")
                        subprocess.run(
                            ['sudo', 'nmcli', 'connection', 'up', previous_ssid],
                            capture_output=True, text=True, timeout=30
                        )
                        time.sleep(3)
                        connected, recovered_ssid = self.is_connected_to_wifi()
                        if connected:
                            logger.info(f"Reconnected to {recovered_ssid}")
                            return {"success": False, "error": error_msg, "recovered": True, "recovered_ssid": recovered_ssid}
                
                # Connection failed - try to recover to any known network
                recovery_result = self._try_recover_connection()
                if recovery_result:
                    return {"success": False, "error": error_msg, "recovered": True, "recovered_ssid": recovery_result}
                
                # If recovery failed, start AP mode so device remains accessible
                logger.info("Connection and recovery failed, starting AP mode...")
                self.start_ap_mode()
                
                return {"success": False, "error": error_msg, "ap_started": True}
            else:
                # Fallback to wpa_supplicant method
                logger.info(f"Using wpa_supplicant to connect to {ssid}...")
                
                # Add network to wpa_supplicant
                self.add_wifi_network(ssid, password)
                
                # Reconfigure wpa_supplicant to load new network
                subprocess.run(['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'reconfigure'],
                              capture_output=True, timeout=10)
                time.sleep(1)
                
                # Find the network ID for the target SSID and select it
                list_result = subprocess.run(
                    ['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'list_networks'],
                    capture_output=True, text=True, timeout=10
                )
                target_network_id = None
                for line in list_result.stdout.strip().split('\n')[1:]:  # Skip header
                    parts = line.split('\t')
                    if len(parts) >= 2 and parts[1] == ssid:
                        target_network_id = parts[0]
                        break
                
                if target_network_id:
                    logger.info(f"Found network {ssid} with ID {target_network_id}, selecting it...")
                    # Disconnect from current network first
                    subprocess.run(
                        ['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'disconnect'],
                        capture_output=True, timeout=5
                    )
                    time.sleep(1)
                    # Select the target network
                    subprocess.run(
                        ['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'select_network', target_network_id],
                        capture_output=True, timeout=10
                    )
                else:
                    logger.warning(f"Could not find network ID for {ssid}, using reassociate...")
                    subprocess.run(
                        ['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'reassociate'],
                        capture_output=True, timeout=10
                    )
                
                # Wait for connection to the target SSID
                logger.info(f"Connecting to {ssid}...")
                for i in range(WiFiAPConfig.WIFI_CONNECT_TIMEOUT):
                    time.sleep(1)
                    connected, current_ssid = self.is_connected_to_wifi()
                    if connected and current_ssid == ssid:
                        ip = self.get_current_ip()
                        logger.info(f"Connected to {current_ssid} with IP {ip}")
                        return {
                            "success": True,
                            "message": f"Connected to {current_ssid}",
                            "ssid": current_ssid,
                            "ip": ip,
                        }
                
                # Connection failed - try to recover
                recovery_result = self._try_recover_connection()
                if recovery_result:
                    return {"success": False, "error": "Connection timeout", "recovered": True, "recovered_ssid": recovery_result}
                
                # If recovery failed, start AP mode so device remains accessible
                logger.info("Connection and recovery failed, starting AP mode...")
                self.start_ap_mode()
                
                return {"success": False, "error": "Connection timeout", "ap_started": True}
            
        except subprocess.TimeoutExpired as e:
            logger.error(f"Timeout connecting to WiFi: {e}")
            # Try to recover on timeout
            recovery_result = self._try_recover_connection()
            if recovery_result:
                return {"success": False, "error": f"Connection timeout: {str(e)}", "recovered": True, "recovered_ssid": recovery_result}
            logger.info("Timeout and recovery failed, starting AP mode...")
            self.start_ap_mode()
            return {"success": False, "error": f"Connection timeout: {str(e)}", "ap_started": True}
        except Exception as e:
            logger.error(f"Error connecting to WiFi: {e}")
            # Try to recover on error
            recovery_result = self._try_recover_connection()
            if recovery_result:
                return {"success": False, "error": str(e), "recovered": True, "recovered_ssid": recovery_result}
            logger.info("Error and recovery failed, starting AP mode...")
            self.start_ap_mode()
            return {"success": False, "error": str(e), "ap_started": True}
    
    def _try_recover_connection(self) -> Optional[str]:
        """Try to reconnect to any known network. Returns connected SSID or None."""
        logger.info("Attempting to recover connection to a known network...")
        
        try:
            if self._is_networkmanager_active():
                # Get list of saved connections
                result = subprocess.run(
                    ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
                    capture_output=True, text=True, timeout=10
                )
                
                saved_networks = []
                for line in result.stdout.strip().split('\n'):
                    if ':802-11-wireless' in line:
                        name = line.split(':')[0]
                        saved_networks.append(name)
                
                logger.info(f"Found {len(saved_networks)} saved WiFi connections: {saved_networks}")
                
                # Try to connect to each saved network
                for network in saved_networks:
                    logger.info(f"Trying to connect to saved network: {network}")
                    result = subprocess.run(
                        ['sudo', 'nmcli', 'connection', 'up', network],
                        capture_output=True, text=True, timeout=15
                    )
                    if result.returncode == 0:
                        time.sleep(2)
                        connected, current_ssid = self.is_connected_to_wifi()
                        if connected:
                            logger.info(f"Recovered connection to {current_ssid}")
                            return current_ssid
            
            # Fallback: try wpa_cli reassociate
            subprocess.run(
                ['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'reassociate'],
                capture_output=True, timeout=5
            )
            time.sleep(5)
            connected, current_ssid = self.is_connected_to_wifi()
            if connected:
                logger.info(f"Recovered connection to {current_ssid} via wpa_cli")
                return current_ssid
                
        except Exception as e:
            logger.error(f"Error during connection recovery: {e}")
        
        logger.warning("Failed to recover connection to any known network")
        return None
    
    def get_status(self) -> Dict:
        """Get current WiFi/AP status"""
        ap_active = self.is_ap_mode_active()
        wifi_connected, wifi_ssid = self.is_connected_to_wifi()
        
        return {
            "mode": "ap" if ap_active else "client",
            "ap_active": ap_active,
            "ap_ssid": self.ap_ssid if ap_active else None,
            "ap_password": WiFiAPConfig.AP_PASSWORD if ap_active else None,
            "ap_ip": WiFiAPConfig.AP_IP if ap_active else None,
            "wifi_connected": wifi_connected,
            "wifi_ssid": wifi_ssid if wifi_connected else None,
            "ip": self.get_current_ip(),
            "known_networks": self.get_known_networks(),
        }
    
    def _is_networkmanager_active(self) -> bool:
        """Check if NetworkManager is managing WiFi"""
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', 'NetworkManager'],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip() == 'active'
        except:
            return False
    
    def _get_nmcli_connections(self) -> List[str]:
        """Get list of saved WiFi connections from NetworkManager"""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'NAME,TYPE', 'connection', 'show'],
                capture_output=True, text=True, timeout=10
            )
            connections = []
            for line in result.stdout.strip().split('\n'):
                if ':802-11-wireless' in line or ':wifi' in line:
                    name = line.split(':')[0]
                    if name:
                        connections.append(name)
            return connections
        except Exception as e:
            logger.error(f"Error getting nmcli connections: {e}")
            return []
    
    def reset_wifi(self, start_ap: bool = True) -> Dict:
        """
        Reset WiFi configuration - forget all networks and optionally start AP mode.
        This allows re-provisioning the device with a new WiFi network.
        Supports both NetworkManager (nmcli) and wpa_supplicant.
        """
        try:
            logger.info("Resetting WiFi configuration...")
            backup_path = None
            
            # Check if NetworkManager is active
            if self._is_networkmanager_active():
                logger.info("Using NetworkManager to reset WiFi...")
                
                # Step 1: Disconnect from current WiFi
                subprocess.run(
                    ['sudo', 'nmcli', 'device', 'disconnect', WiFiAPConfig.AP_INTERFACE],
                    capture_output=True, timeout=10
                )
                
                # Step 2: Delete all saved WiFi connections
                connections = self._get_nmcli_connections()
                logger.info(f"Found {len(connections)} saved WiFi connections to delete")
                for conn_name in connections:
                    logger.info(f"Deleting connection: {conn_name}")
                    subprocess.run(
                        ['sudo', 'nmcli', 'connection', 'delete', conn_name],
                        capture_output=True, timeout=10
                    )
                
                time.sleep(2)
            else:
                # Fallback to wpa_supplicant method
                logger.info("Using wpa_supplicant to reset WiFi...")
                
                # Step 1: Disconnect from current WiFi
                subprocess.run(['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'disconnect'],
                              capture_output=True, timeout=10)
                
                # Step 2: Backup current wpa_supplicant.conf
                backup_path = f"{WiFiAPConfig.WPA_SUPPLICANT_CONF}.backup"
                subprocess.run(['sudo', 'cp', WiFiAPConfig.WPA_SUPPLICANT_CONF, backup_path],
                              capture_output=True, timeout=5)
                logger.info(f"Backed up wpa_supplicant.conf to {backup_path}")
                
                # Step 3: Create minimal wpa_supplicant.conf (remove all network blocks)
                minimal_conf = '''ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=US

'''
                subprocess.run(
                    ['sudo', 'bash', '-c', f'echo "{minimal_conf}" > {WiFiAPConfig.WPA_SUPPLICANT_CONF}'],
                    capture_output=True, timeout=10
                )
                logger.info("Cleared all known WiFi networks")
                
                # Step 4: Reconfigure wpa_supplicant
                subprocess.run(['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'reconfigure'],
                              capture_output=True, timeout=10)
                
                time.sleep(2)
            
            # Step 5: Start AP mode if requested
            if start_ap:
                logger.info("Starting AP mode after reset...")
                ap_result = self.start_ap_mode()
                return {
                    "success": True,
                    "message": "WiFi reset complete. AP mode started.",
                    "backup_path": backup_path,
                    "ap_started": ap_result.get("success", False),
                    "ap_ssid": ap_result.get("ssid"),
                    "ap_password": ap_result.get("password"),
                    "ap_ip": ap_result.get("ip"),
                }
            else:
                return {
                    "success": True,
                    "message": "WiFi reset complete. All networks forgotten.",
                    "backup_path": backup_path,
                    "ap_started": False,
                }
            
        except Exception as e:
            logger.error(f"Error resetting WiFi: {e}")
            return {"success": False, "error": str(e)}
    
    def forget_network(self, ssid: str) -> Dict:
        """Forget a specific WiFi network"""
        try:
            # Read current config
            result = subprocess.run(
                ['sudo', 'cat', WiFiAPConfig.WPA_SUPPLICANT_CONF],
                capture_output=True, text=True, timeout=5
            )
            content = result.stdout
            
            # Remove the network block for this SSID
            import re
            pattern = rf'\nnetwork=\{{[^}}]*ssid="{re.escape(ssid)}"[^}}]*\}}'
            new_content = re.sub(pattern, '', content)
            
            if new_content == content:
                return {"success": False, "error": f"Network '{ssid}' not found"}
            
            # Write updated config
            subprocess.run(
                ['sudo', 'bash', '-c', f'echo "{new_content}" > {WiFiAPConfig.WPA_SUPPLICANT_CONF}'],
                capture_output=True, timeout=10
            )
            
            # Reconfigure
            subprocess.run(['sudo', 'wpa_cli', '-i', WiFiAPConfig.AP_INTERFACE, 'reconfigure'],
                          capture_output=True, timeout=10)
            
            logger.info(f"Forgot network: {ssid}")
            return {"success": True, "message": f"Forgot network: {ssid}"}
            
        except Exception as e:
            logger.error(f"Error forgetting network: {e}")
            return {"success": False, "error": str(e)}


def check_and_start_ap_if_needed(timeout: int = 30) -> Dict:
    """
    Main entry point: Check for known networks and start AP mode if none available.
    Call this on device boot.
    
    Returns status dict with mode info.
    """
    manager = WiFiAPManager()
    
    # First check if already connected
    connected, ssid = manager.is_connected_to_wifi()
    if connected:
        logger.info(f"Already connected to WiFi: {ssid}")
        return {
            "mode": "client",
            "connected": True,
            "ssid": ssid,
            "ip": manager.get_current_ip(),
        }
    
    # Wait a bit for WiFi to connect on boot
    logger.info(f"Waiting up to {timeout}s for WiFi connection...")
    for i in range(timeout):
        time.sleep(1)
        connected, ssid = manager.is_connected_to_wifi()
        if connected:
            logger.info(f"Connected to WiFi: {ssid}")
            return {
                "mode": "client",
                "connected": True,
                "ssid": ssid,
                "ip": manager.get_current_ip(),
            }
    
    # Check if any known networks are available
    has_known, found_networks = manager.has_known_networks_available()
    
    if has_known:
        # Known networks available but not connected - try to connect
        logger.info(f"Known networks found: {found_networks}. Waiting for connection...")
        for i in range(15):
            time.sleep(1)
            connected, ssid = manager.is_connected_to_wifi()
            if connected:
                return {
                    "mode": "client",
                    "connected": True,
                    "ssid": ssid,
                    "ip": manager.get_current_ip(),
                }
    
    # No known networks available or couldn't connect - start AP mode
    # Force restart to ensure hostapd is actually broadcasting
    logger.info("No known networks available. Starting AP mode...")
    result = manager.start_ap_mode(force_restart=True)
    
    if result.get("success"):
        return {
            "mode": "ap",
            "connected": False,
            "ap_ssid": result["ssid"],
            "ap_password": result["password"],
            "ap_ip": result["ip"],
            "url": result.get("url"),
        }
    else:
        logger.error(f"Failed to start AP mode: {result.get('error')}")
        return {
            "mode": "failed",
            "connected": False,
            "error": result.get("error"),
        }


if __name__ == "__main__":
    # Setup logging for standalone testing
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    )
    
    import sys
    
    manager = WiFiAPManager()
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        
        if cmd == "status":
            print(manager.get_status())
            
        elif cmd == "start-ap":
            result = manager.start_ap_mode()
            print(result)
            
        elif cmd == "stop-ap":
            result = manager.stop_ap_mode()
            print(result)
            
        elif cmd == "scan":
            networks = manager.scan_available_networks()
            for n in networks:
                print(f"  {n['ssid']}: {n['signal']}%")
                
        elif cmd == "known":
            networks = manager.get_known_networks()
            print(f"Known networks: {networks}")
            
        elif cmd == "check":
            result = check_and_start_ap_if_needed(timeout=10)
            print(result)
            
        elif cmd == "connect" and len(sys.argv) >= 4:
            ssid = sys.argv[2]
            password = sys.argv[3]
            result = manager.connect_to_wifi(ssid, password)
            print(result)
            
        elif cmd == "reset":
            result = manager.reset_wifi(start_ap=True)
            print(result)
            
        elif cmd == "forget" and len(sys.argv) >= 3:
            ssid = sys.argv[2]
            result = manager.forget_network(ssid)
            print(result)
            
        else:
            print("Usage: wifi_ap_manager.py [status|start-ap|stop-ap|scan|known|check|reset|forget <ssid>|connect <ssid> <password>]")
    else:
        # Default: check and start AP if needed
        result = check_and_start_ap_if_needed()
        print(result)
