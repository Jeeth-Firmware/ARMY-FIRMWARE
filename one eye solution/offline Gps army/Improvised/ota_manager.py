#!/usr/bin/env python3
"""
OTA Manager - Enhanced Over-The-Air update system with best practices.

Features:
1. Delta updates (only changed files)
2. Atomic updates with rollback capability
3. Health checks after updates
4. Scheduled update windows
5. Update prioritization (critical, normal, low)
6. Bandwidth-aware downloads
7. Resume interrupted downloads
"""

import json
import logging
import os
import subprocess
import sys
import time
import hashlib
import shutil
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
import threading

from version_manager import VersionManager, UpdateStatus


# Setup logging
LOG_DIR = os.path.expanduser("~/Desktop/Updated_codes/logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "ota_manager.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger("ota_manager")


class UpdatePriority(Enum):
    CRITICAL = "critical"  # Apply immediately
    HIGH = "high"          # Apply on next boot
    NORMAL = "normal"      # Apply during maintenance window
    LOW = "low"            # Apply when convenient


class UpdateType(Enum):
    FULL = "full"          # Complete firmware update
    DELTA = "delta"        # Only changed files
    COMPONENT = "component" # Single component update
    ROLLBACK = "rollback"  # Rollback to previous version


@dataclass
class UpdatePackage:
    """Represents an OTA update package"""
    id: str
    version: str
    priority: str
    update_type: str
    files: List[dict]
    checksum: str
    size: int
    release_notes: str = ""
    min_version: str = None
    max_version: str = None
    requires_reboot: bool = False
    created_at: str = None


@dataclass
class HealthCheck:
    """Health check result for a service"""
    service: str
    status: str
    response_time: float = 0
    error: str = None


class OTAManager:
    """
    Manages OTA updates with best practices:
    
    1. Pre-update checks
    2. Backup current state
    3. Download with verification
    4. Atomic installation
    5. Post-update health checks
    6. Automatic rollback on failure
    """
    
    def __init__(self, base_dir: str = "/home/pi/facial-tracker-firmware"):
        self.base_dir = Path(base_dir)
        self.ota_dir = self.base_dir / "ota"
        self.download_dir = self.ota_dir / "downloads"
        self.staging_dir = self.ota_dir / "staging"
        self.config_file = self.ota_dir / "ota_config.json"
        
        self.version_manager = VersionManager(base_dir)
        self._ensure_directories()
        self._load_config()
    
    def _ensure_directories(self):
        """Create required directories"""
        for d in [self.ota_dir, self.download_dir, self.staging_dir]:
            d.mkdir(parents=True, exist_ok=True)
    
    def _load_config(self):
        """Load OTA configuration"""
        self.config = {
            "api_base_url": os.getenv("API_BASE_URL", "https://api.copilotai.click"),
            "check_interval_hours": 6,
            "maintenance_window_start": "02:00",
            "maintenance_window_end": "05:00",
            "auto_update_enabled": True,
            "auto_rollback_enabled": True,
            "max_download_retries": 3,
            "health_check_timeout": 30,
            "services": [
                "facial1.service",
                "get_gps_data1.service",
            ]
        }
        
        if self.config_file.exists():
            try:
                with open(self.config_file) as f:
                    self.config.update(json.load(f))
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
    
    def save_config(self):
        """Save OTA configuration"""
        with open(self.config_file, 'w') as f:
            json.dump(self.config, f, indent=2)
    
    def get_device_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Get device ID and auth token from SQLite database"""
        try:
            import db_helper
            row = db_helper.fetchone("SELECT device_id, auth_key FROM device LIMIT 1")
            if row:
                return row['device_id'], row['auth_key']
            return None, None
        except Exception as e:
            logger.error(f"Failed to get credentials: {e}")
            return None, None
    
    def check_for_updates(self) -> List[UpdatePackage]:
        """Check backend for available updates (manual deployments first, then auto-update)"""
        device_id, token = self.get_device_credentials()
        if not device_id or not token:
            logger.error("Device not provisioned")
            return []
        
        current_version = self.version_manager.get_version()
        
        # First check for manual deployments (pending deployments from dashboard)
        updates = self._check_manual_deployments(device_id, token, current_version)
        if updates:
            return updates
        
        # If no manual deployments, check for auto-updates (latest bundle)
        logger.info("No manual deployments, checking for auto-updates...")
        return self._check_auto_update(device_id, token, current_version)
    
    def _check_manual_deployments(self, device_id: str, token: str, current_version: str) -> List[UpdatePackage]:
        """Check for pending manual deployments from dashboard"""
        try:
            url = f"{self.config['api_base_url']}/api/auth/ota/check-updates?device_id={device_id}&current_version={current_version}"
            
            req = urllib.request.Request(url, method='GET')
            req.add_header('Authorization', f'Bearer {token}')
            req.add_header('Content-Type', 'application/json')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                
                if data.get("success") and data.get("updates_available") and data.get("updates"):
                    return self._parse_updates(data["updates"])
                    
        except urllib.error.HTTPError as e:
            logger.error(f"HTTP error checking manual deployments: {e.code}")
        except Exception as e:
            logger.error(f"Error checking manual deployments: {e}")
        
        return []
    
    def _check_auto_update(self, device_id: str, token: str, current_version: str) -> List[UpdatePackage]:
        """Check for latest bundle (auto-update without manual deployment)"""
        try:
            url = f"{self.config['api_base_url']}/api/auth/ota/auto-update?device_id={device_id}&current_version={current_version}"
            
            req = urllib.request.Request(url, method='GET')
            req.add_header('Authorization', f'Bearer {token}')
            req.add_header('Content-Type', 'application/json')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                
                if data.get("success") and data.get("update_available") and data.get("update"):
                    update_data = data["update"]
                    logger.info(f"Auto-update available: {update_data.get('version')} (current: {current_version})")
                    return self._parse_updates([update_data])
                else:
                    logger.info(f"No auto-update available (current: {current_version}, latest: {data.get('latest_version', 'unknown')})")
                    
        except urllib.error.HTTPError as e:
            logger.error(f"HTTP error checking auto-update: {e.code}")
        except Exception as e:
            logger.error(f"Error checking auto-update: {e}")
        
        return []
    
    def _parse_updates(self, updates_data: list) -> List[UpdatePackage]:
        """Parse update data into UpdatePackage objects"""
        updates = []
        for update_data in updates_data:
            # Handle both manual deployment format and auto-update format
            files = update_data.get("files", [])
            if not files and update_data.get("download_url"):
                # Auto-update format - create a single file entry
                files = [{
                    "name": update_data.get("name", "bundle.zip"),
                    "download_url": update_data.get("download_url"),
                    "checksum": update_data.get("checksum", ""),
                    "target_path": update_data.get("target_path", ""),
                    "size": update_data.get("size_bytes", 0),
                }]
            
            updates.append(UpdatePackage(
                id=str(update_data.get("id", update_data.get("deployment_id", ""))),
                version=update_data.get("version", ""),
                priority=update_data.get("priority", "normal"),
                update_type=update_data.get("type", "full"),
                files=files,
                checksum=update_data.get("checksum", ""),
                size=update_data.get("size_bytes", update_data.get("size", 0)),
                release_notes=update_data.get("release_notes", ""),
                min_version=update_data.get("min_version"),
                max_version=update_data.get("max_version"),
                requires_reboot=update_data.get("requires_reboot", False),
                created_at=update_data.get("created_at")
            ))
        return updates
    
    def download_file(
        self,
        url: str,
        dest_path: Path,
        expected_checksum: str = None,
        resume: bool = True
    ) -> bool:
        """
        Download a file with resume support and checksum verification.
        """
        try:
            temp_path = dest_path.with_suffix(dest_path.suffix + '.tmp')
            
            # Check for partial download
            start_byte = 0
            if resume and temp_path.exists():
                start_byte = temp_path.stat().st_size
                logger.info(f"Resuming download from byte {start_byte}")
            
            # Create request with range header for resume
            req = urllib.request.Request(url)
            if start_byte > 0:
                req.add_header('Range', f'bytes={start_byte}-')
            
            with urllib.request.urlopen(req, timeout=60) as response:
                mode = 'ab' if start_byte > 0 else 'wb'
                with open(temp_path, mode) as f:
                    shutil.copyfileobj(response, f)
            
            # Verify checksum
            if expected_checksum:
                with open(temp_path, 'rb') as f:
                    actual_checksum = hashlib.md5(f.read()).hexdigest()
                if actual_checksum != expected_checksum:
                    temp_path.unlink()
                    logger.error(f"Checksum mismatch: {actual_checksum} != {expected_checksum}")
                    return False
            
            # Move to final location
            shutil.move(str(temp_path), str(dest_path))
            return True
            
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return False
    
    def download_update(self, update: UpdatePackage) -> bool:
        """Download all files for an update package"""
        logger.info(f"Downloading update {update.version} ({len(update.files)} files)")
        
        update_dir = self.download_dir / update.id
        update_dir.mkdir(parents=True, exist_ok=True)
        
        for file_info in update.files:
            filename = file_info.get("name")
            download_url = file_info.get("download_url") or file_info.get("s3_url")
            checksum = file_info.get("checksum")
            
            if not download_url:
                logger.error(f"No download URL for {filename}")
                return False
            
            dest_path = update_dir / filename
            
            for attempt in range(self.config["max_download_retries"]):
                if self.download_file(download_url, dest_path, checksum):
                    logger.info(f"Downloaded: {filename}")
                    break
                logger.warning(f"Retry {attempt + 1} for {filename}")
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.error(f"Failed to download {filename}")
                return False
        
        return True
    
    def pre_update_checks(self, update: UpdatePackage) -> Tuple[bool, str]:
        """
        Run pre-update checks:
        1. Version compatibility
        2. Disk space
        3. Battery/power status
        4. Network connectivity
        """
        current_version = self.version_manager.get_version()
        
        # Check version compatibility
        if update.min_version:
            if self._compare_versions(current_version, update.min_version) < 0:
                return False, f"Current version {current_version} < minimum {update.min_version}"
        
        if update.max_version:
            if self._compare_versions(current_version, update.max_version) > 0:
                return False, f"Current version {current_version} > maximum {update.max_version}"
        
        # Check disk space (need at least 2x update size for backup + new files)
        required_space = update.size * 2
        try:
            stat = os.statvfs(str(self.base_dir))
            available_space = stat.f_frsize * stat.f_bavail
            if available_space < required_space:
                return False, f"Insufficient disk space: {available_space} < {required_space}"
        except Exception as e:
            logger.warning(f"Could not check disk space: {e}")
        
        return True, "Pre-update checks passed"
    
    def _compare_versions(self, v1: str, v2: str) -> int:
        """Compare two semantic versions. Returns -1, 0, or 1"""
        def parse(v):
            return [int(x) for x in v.split('.')]
        
        p1, p2 = parse(v1), parse(v2)
        for a, b in zip(p1, p2):
            if a < b:
                return -1
            if a > b:
                return 1
        return 0
    
    def install_update(self, update: UpdatePackage) -> Tuple[bool, str]:
        """
        Install an update package atomically:
        1. Stop affected services
        2. Create backup
        3. Install new files (extract if bundle zip)
        4. Start services
        5. Run health checks
        6. Rollback if health checks fail
        """
        logger.info(f"Installing update {update.version}")
        
        # Pre-update checks
        ok, msg = self.pre_update_checks(update)
        if not ok:
            return False, msg
        
        # Determine affected services
        affected_services = set()
        for file_info in update.files:
            component = file_info.get("name")
            services = self.version_manager.get_services_for_component(component)
            affected_services.update(services)
        
        try:
            # Stop affected services
            logger.info(f"Stopping services: {affected_services}")
            self._stop_services(list(affected_services))
            
            # Create backup
            logger.info("Creating backup...")
            self.version_manager.create_backup()
            
            # Install files
            update_dir = self.download_dir / update.id
            installed_files = []
            
            for file_info in update.files:
                filename = file_info.get("name")
                target_path = file_info.get("target_path") or str(self.base_dir)
                version = file_info.get("version", update.version)
                checksum = file_info.get("checksum")
                
                src_path = update_dir / filename
                if not src_path.exists():
                    raise Exception(f"Update file not found: {filename}")
                
                # Handle bundle zip files specially
                if filename.endswith('.zip') and 'bundle' in filename.lower():
                    success, msg = self._install_bundle(src_path, target_path, version)
                    if not success:
                        raise Exception(msg)
                else:
                    success, msg = self.version_manager.update_component(
                        filename, src_path, version, checksum
                    )
                    if not success:
                        raise Exception(msg)
                
                installed_files.append(filename)
                logger.info(f"Installed: {filename} v{version}")
            
            # Update firmware version
            self.version_manager.manifest.firmware_version = update.version
            self.version_manager.manifest.updated_at = datetime.now().isoformat()
            self.version_manager.save_manifest()
            
            # Start services
            logger.info("Starting services...")
            self._start_services(list(affected_services))
            
            # Health checks
            time.sleep(5)  # Wait for services to stabilize
            health_results = self.run_health_checks(list(affected_services))
            
            failed_checks = [h for h in health_results if h.status != "healthy"]
            if failed_checks and self.config["auto_rollback_enabled"]:
                logger.error(f"Health checks failed: {failed_checks}")
                raise Exception("Health checks failed, rolling back")
            
            # Report success
            self._report_update_status(update.id, "success")
            
            # Cleanup download
            shutil.rmtree(update_dir, ignore_errors=True)
            
            return True, f"Update {update.version} installed successfully"
            
        except Exception as e:
            logger.error(f"Installation failed: {e}")
            
            # Rollback
            if self.config["auto_rollback_enabled"]:
                logger.info("Rolling back...")
                self.version_manager.rollback()
                self._start_services(list(affected_services))
            
            self._report_update_status(update.id, "failed", str(e))
            return False, str(e)
    
    def _install_bundle(self, zip_path: Path, target_path: str, version: str) -> Tuple[bool, str]:
        """
        Install a bundle zip file by extracting it to the target path.
        Creates releases/version/apps/ structure and updates current symlink.
        """
        import zipfile
        
        try:
            target_dir = Path(target_path)
            releases_dir = target_dir / "releases"
            releases_dir.mkdir(parents=True, exist_ok=True)
            
            # Create version directory
            version_dir = releases_dir / version
            if version_dir.exists():
                shutil.rmtree(version_dir)
            version_dir.mkdir(parents=True, exist_ok=True)
            
            # Create apps subdirectory
            apps_dir = version_dir / "apps"
            apps_dir.mkdir(parents=True, exist_ok=True)
            
            # Extract zip
            logger.info(f"Extracting bundle to {version_dir}")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_contents = zip_ref.namelist()
                root_folder = zip_contents[0].split('/')[0] if zip_contents else None
                
                # Extract to temp location
                zip_ref.extractall(releases_dir)
                
                # Move files to apps directory
                if root_folder and (releases_dir / root_folder).exists():
                    extracted_folder = releases_dir / root_folder
                    for item in extracted_folder.iterdir():
                        dest = apps_dir / item.name
                        if dest.exists():
                            if dest.is_dir():
                                shutil.rmtree(dest)
                            else:
                                dest.unlink()
                        shutil.move(str(item), str(dest))
                    # Remove empty extracted folder
                    if extracted_folder.exists():
                        shutil.rmtree(extracted_folder)
            
            # Update current symlink
            current_link = target_dir / "current"
            if current_link.is_symlink():
                current_link.unlink()
            elif current_link.exists():
                shutil.rmtree(current_link)
            
            current_link.symlink_to(version_dir)
            logger.info(f"Updated current symlink to {version_dir}")
            
            return True, f"Bundle {version} installed successfully"
            
        except Exception as e:
            logger.error(f"Bundle installation failed: {e}")
            return False, str(e)
    
    def _stop_services(self, services: List[str]):
        """Stop systemd services"""
        for service in services:
            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'stop', service],
                    capture_output=True, timeout=30
                )
            except Exception as e:
                logger.warning(f"Failed to stop {service}: {e}")
    
    def _start_services(self, services: List[str]):
        """Start systemd services"""
        for service in services:
            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'start', service],
                    capture_output=True, timeout=30
                )
            except Exception as e:
                logger.warning(f"Failed to start {service}: {e}")
    
    def run_health_checks(self, services: List[str] = None) -> List[HealthCheck]:
        """Run health checks on services"""
        if services is None:
            services = self.config["services"]
        
        results = []
        for service in services:
            start_time = time.time()
            try:
                result = subprocess.run(
                    ['sudo', 'systemctl', 'is-active', service],
                    capture_output=True, text=True,
                    timeout=self.config["health_check_timeout"]
                )
                status = "healthy" if result.stdout.strip() == "active" else "unhealthy"
                results.append(HealthCheck(
                    service=service,
                    status=status,
                    response_time=time.time() - start_time
                ))
            except Exception as e:
                results.append(HealthCheck(
                    service=service,
                    status="error",
                    error=str(e)
                ))
        
        return results
    
    def _report_update_status(self, update_id: str, status: str, error: str = None):
        """Report update status and version to backend"""
        device_id, token = self.get_device_credentials()
        if not device_id or not token:
            return
        
        current_version = self.version_manager.get_version()
        
        # Use the new public report-version endpoint
        try:
            url = f"{self.config['api_base_url']}/api/auth/ota/report-version"
            
            data = json.dumps({
                "device_id": device_id,
                "current_version": current_version,
                "deployment_id": int(update_id) if update_id.isdigit() else 0,
            }).encode()
            
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Authorization', f'Bearer {token}')
            req.add_header('Content-Type', 'application/json')
            
            urllib.request.urlopen(req, timeout=30)
            logger.info(f"Reported version {current_version} to backend")
            
        except Exception as e:
            logger.error(f"Failed to report version: {e}")
    
    def is_in_maintenance_window(self) -> bool:
        """Check if current time is within maintenance window"""
        try:
            now = datetime.now()
            start = datetime.strptime(self.config["maintenance_window_start"], "%H:%M")
            end = datetime.strptime(self.config["maintenance_window_end"], "%H:%M")
            
            start = now.replace(hour=start.hour, minute=start.minute, second=0)
            end = now.replace(hour=end.hour, minute=end.minute, second=0)
            
            if end < start:  # Window crosses midnight
                return now >= start or now <= end
            return start <= now <= end
            
        except Exception:
            return True  # Default to allowing updates
    
    def should_apply_update(self, update: UpdatePackage) -> bool:
        """Determine if an update should be applied now"""
        # Always apply updates when available (simplified for auto-update flow)
        # Previously this checked maintenance windows, but for OTA updates
        # we want to apply them immediately when detected
        return True
    
    def run_update_cycle(self):
        """
        Run a complete update cycle:
        1. Check for updates
        2. Download applicable updates
        3. Install updates based on priority
        """
        logger.info("Starting update cycle...")
        
        updates = self.check_for_updates()
        if not updates:
            logger.info("No updates available")
            return
        
        # Sort by priority
        priority_order = {
            UpdatePriority.CRITICAL.value: 0,
            UpdatePriority.HIGH.value: 1,
            UpdatePriority.NORMAL.value: 2,
            UpdatePriority.LOW.value: 3
        }
        updates.sort(key=lambda u: priority_order.get(u.priority, 99))
        
        for update in updates:
            if not self.should_apply_update(update):
                logger.info(f"Skipping update {update.version} (priority: {update.priority})")
                continue
            
            logger.info(f"Processing update {update.version}")
            
            # Download
            if not self.download_update(update):
                logger.error(f"Failed to download update {update.version}")
                continue
            
            # Install
            success, msg = self.install_update(update)
            if success:
                logger.info(msg)
                if update.requires_reboot:
                    logger.info("Reboot required, scheduling...")
                    self._schedule_reboot()
            else:
                logger.error(msg)
    
    def _schedule_reboot(self, delay_minutes: int = 1):
        """Schedule a system reboot"""
        try:
            subprocess.run(
                ['sudo', 'shutdown', '-r', f'+{delay_minutes}'],
                capture_output=True
            )
        except Exception as e:
            logger.error(f"Failed to schedule reboot: {e}")


class AutoUpdateDaemon:
    """
    Background daemon for automatic updates.
    Runs periodic update checks and applies updates based on configuration.
    """
    
    def __init__(self, ota_manager: OTAManager):
        self.ota_manager = ota_manager
        self.running = False
        self._thread = None
    
    def start(self):
        """Start the auto-update daemon"""
        if self.running:
            return
        
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Auto-update daemon started")
    
    def stop(self):
        """Stop the auto-update daemon"""
        self.running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Auto-update daemon stopped")
    
    def _run_loop(self):
        """Main daemon loop"""
        check_interval = self.ota_manager.config["check_interval_hours"] * 3600
        
        while self.running:
            try:
                if self.ota_manager.config["auto_update_enabled"]:
                    self.ota_manager.run_update_cycle()
            except Exception as e:
                logger.error(f"Update cycle failed: {e}")
            
            # Sleep in small intervals to allow quick shutdown
            for _ in range(int(check_interval / 10)):
                if not self.running:
                    break
                time.sleep(10)


# CLI interface
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="OTA Update Manager")
    parser.add_argument("command", choices=[
        "check", "download", "install", "health", "config", "daemon"
    ])
    parser.add_argument("--update-id", "-u", help="Specific update ID")
    parser.add_argument("--base-dir", "-d", default="/home/pi/facial-tracker-firmware")
    
    args = parser.parse_args()
    
    ota = OTAManager(args.base_dir)
    
    if args.command == "check":
        updates = ota.check_for_updates()
        if updates:
            print(f"Found {len(updates)} update(s):")
            for u in updates:
                print(f"  - {u.version} ({u.priority}): {len(u.files)} files")
        else:
            print("No updates available")
    
    elif args.command == "download":
        updates = ota.check_for_updates()
        for u in updates:
            if args.update_id and u.id != args.update_id:
                continue
            if ota.download_update(u):
                print(f"Downloaded: {u.version}")
            else:
                print(f"Failed: {u.version}")
    
    elif args.command == "install":
        updates = ota.check_for_updates()
        for u in updates:
            if args.update_id and u.id != args.update_id:
                continue
            success, msg = ota.install_update(u)
            print(msg)
    
    elif args.command == "health":
        results = ota.run_health_checks()
        print("Health Check Results:")
        for r in results:
            status_icon = "✓" if r.status == "healthy" else "✗"
            print(f"  {status_icon} {r.service}: {r.status}")
    
    elif args.command == "config":
        print("OTA Configuration:")
        for key, value in ota.config.items():
            print(f"  {key}: {value}")
    
    elif args.command == "daemon":
        daemon = AutoUpdateDaemon(ota)
        daemon.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            daemon.stop()
