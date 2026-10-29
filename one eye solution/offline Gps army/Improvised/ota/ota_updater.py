#!/usr/bin/env python3
"""
OTA Updater - Versioned Bundle Update System

Architecture (from ota_guild.md):
- OTA updates operate on versioned bundles
- `current` symlink defines the active software
- systemd services remain static (point to /current)
- OTA updater controls update, validation, and rollback

Directory Structure:
/home/pi/facial-tracker-firmware/
├── releases/
│   ├── v0.0.1/
│   │   ├── apps/
│   │   │   ├── facil_updated_1.py
│   │   │   └── get_gps_data.py
│   │   ├── libs/
│   │   │   ├── facial_tracking/
│   │   │   └── ...
│   │   ├── migrate.py (optional)
│   │   └── version.json
│   ├── v1.0.0/
│   │   └── ...
├── current -> /home/pi/facial-tracker-firmware/releases/v0.0.1
├── shared/
│   ├── config/
│   └── database/
└── logs/
"""

import json
import logging
import os
import subprocess
import sys
import time

# Add repo root to path so we can import db_helper
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hashlib
import shutil
import tarfile
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

# Setup logging
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "ota_updater.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger("ota_updater")


@dataclass
class UpdateBundle:
    """Represents a versioned update bundle"""
    version: str
    download_url: str
    checksum: str
    size: int
    release_notes: str = ""
    requires_migration: bool = False
    requires_reboot: bool = False


class OTAUpdater:
    """
    OTA Updater following versioned bundle architecture.
    
    Key principles:
    1. Updates are versioned bundles, not individual files
    2. `current` symlink points to active version
    3. Services always reference /current (never specific version)
    4. Automatic rollback on failure
    5. Health checks validate entire system
    """
    
    # Services managed by OTA (must all be healthy after update)
    MANAGED_SERVICES = [
        "facial1.service",
        "get_gps_data1.service",
    ]
    
    def __init__(self, base_dir: str = "/home/pi/facial-tracker-firmware"):
        self.base_dir = Path(base_dir)
        self.releases_dir = self.base_dir / "releases"
        self.current_link = self.base_dir / "current"
        self.shared_dir = self.base_dir / "shared"
        self.logs_dir = self.base_dir / "logs"
        self.ota_dir = self.base_dir / "ota"
        self.downloads_dir = self.ota_dir / "downloads"
        
        self._ensure_directories()
        self._load_config()
    
    def _ensure_directories(self):
        """Create required directory structure"""
        for d in [self.releases_dir, self.shared_dir, self.logs_dir, 
                  self.ota_dir, self.downloads_dir]:
            d.mkdir(parents=True, exist_ok=True)
        
        # Initialize with v0.0.1 if no releases exist
        if not any(self.releases_dir.iterdir()):
            self._initialize_first_release()
    
    def _initialize_first_release(self):
        """Create initial v0.0.1 release from existing files"""
        logger.info("Initializing first release v0.0.1")
        
        v001_dir = self.releases_dir / "v0.0.1"
        apps_dir = v001_dir / "apps"
        libs_dir = v001_dir / "libs"
        
        apps_dir.mkdir(parents=True, exist_ok=True)
        libs_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy existing app files
        app_files = [
            "facil_updated_1.py",
            "get_gps_data.py", 
            "get_user_info.py",
            "store_locally.py",
        ]
        
        for f in app_files:
            src = self.base_dir / f
            if src.exists():
                shutil.copy2(src, apps_dir / f)
        
        # Copy facial_tracking library
        facial_src = self.base_dir / "facial_tracking"
        if facial_src.exists():
            shutil.copytree(facial_src, libs_dir / "facial_tracking", dirs_exist_ok=True)
        
        # Create version.json
        version_info = {
            "version": "0.0.1",
            "created_at": datetime.now().isoformat(),
            "apps": app_files,
            "libs": ["facial_tracking"]
        }
        with open(v001_dir / "version.json", 'w') as f:
            json.dump(version_info, f, indent=2)
        
        # Create current symlink
        self._switch_version("v0.0.1")
        logger.info("Initialized v0.0.1 release")
    
    def _load_config(self):
        """Load OTA configuration"""
        config_file = self.ota_dir / "ota_config.json"
        self.config = {
            "api_base_url": os.getenv("API_BASE_URL", "https://api.copilotai.click"),
            "check_interval_hours": 6,
            "auto_update": True,
            "auto_rollback": True,
            "health_check_timeout": 60,
            "health_check_retries": 3,
        }
        
        if config_file.exists():
            try:
                with open(config_file) as f:
                    self.config.update(json.load(f))
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
    
    def get_current_version(self) -> str:
        """Get currently active version from symlink"""
        if not self.current_link.exists():
            return "0.0.0"
        
        try:
            target = self.current_link.resolve()
            return target.name.lstrip('v')
        except Exception:
            return "0.0.0"
    
    def get_installed_versions(self) -> List[str]:
        """Get list of all installed versions"""
        versions = []
        for d in self.releases_dir.iterdir():
            if d.is_dir() and d.name.startswith('v'):
                versions.append(d.name.lstrip('v'))
        return sorted(versions, key=lambda v: [int(x) for x in v.split('.')])
    
    def get_device_credentials(self) -> Tuple[Optional[str], Optional[str]]:
        """Get device ID and auth key from local SQLite database"""
        try:
            import db_helper
            row = db_helper.fetchone("SELECT device_id, auth_key FROM device LIMIT 1")
            if row:
                return row['device_id'], row['auth_key']
            return None, None
        except Exception as e:
            logger.error(f"Failed to get credentials: {e}")
            return None, None
    
    def check_for_updates(self) -> Optional[UpdateBundle]:
        """Check backend for available updates"""
        device_id, auth_key = self.get_device_credentials()
        if not device_id or not auth_key:
            logger.error("Device not provisioned")
            return None
        
        try:
            current_version = self.get_current_version()
            url = f"{self.config['api_base_url']}/api/auth/ota/check-updates"
            
            request_data = json.dumps({
                "device_id": device_id,
                "current_version": current_version,
            }).encode()
            
            req = urllib.request.Request(url, data=request_data, method='GET')
            req.add_header('X-Device-ID', device_id)
            req.add_header('X-Auth-Key', auth_key)
            req.add_header('Content-Type', 'application/json')
            
            # Use GET with query params instead
            url_with_params = f"{url}?device_id={device_id}&current_version={current_version}"
            req = urllib.request.Request(url_with_params)
            req.add_header('X-Device-ID', device_id)
            req.add_header('X-Auth-Key', auth_key)
            
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode())
                
                if data.get("has_update") and data.get("pending_updates"):
                    update = data["pending_updates"][0]
                    return UpdateBundle(
                        version=update.get("version", ""),
                        download_url=update.get("download_url", ""),
                        checksum=update.get("checksum", ""),
                        size=update.get("size_bytes", 0),
                        release_notes=update.get("description", ""),
                        requires_reboot=update.get("requires_reboot", False),
                    )
                else:
                    logger.info("No updates available")
                    
        except urllib.error.HTTPError as e:
            logger.error(f"HTTP error checking updates: {e.code}")
        except Exception as e:
            logger.error(f"Error checking updates: {e}")
        
        return None
    
    def download_bundle(self, update: UpdateBundle) -> Optional[Path]:
        """Download update bundle to staging area"""
        logger.info(f"Downloading update bundle v{update.version}")
        
        bundle_file = self.downloads_dir / f"v{update.version}.tar.gz"
        
        try:
            # Download with progress
            req = urllib.request.Request(update.download_url)
            
            with urllib.request.urlopen(req, timeout=300) as response:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                
                with open(bundle_file, 'wb') as f:
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress = (downloaded / total_size) * 100
                            logger.debug(f"Download progress: {progress:.1f}%")
            
            # Verify checksum
            if update.checksum:
                with open(bundle_file, 'rb') as f:
                    actual_checksum = hashlib.md5(f.read()).hexdigest()
                if actual_checksum != update.checksum:
                    logger.error(f"Checksum mismatch: {actual_checksum} != {update.checksum}")
                    bundle_file.unlink()
                    return None
            
            logger.info(f"Downloaded bundle: {bundle_file}")
            return bundle_file
            
        except Exception as e:
            logger.error(f"Download failed: {e}")
            if bundle_file.exists():
                bundle_file.unlink()
            return None
    
    def extract_bundle(self, bundle_file: Path, version: str) -> bool:
        """Extract bundle to releases directory"""
        version_dir = self.releases_dir / f"v{version}"
        
        try:
            # Remove existing version directory if exists
            if version_dir.exists():
                shutil.rmtree(version_dir)
            
            version_dir.mkdir(parents=True)
            
            # Extract tarball
            with tarfile.open(bundle_file, 'r:gz') as tar:
                tar.extractall(version_dir)
            
            # Verify required structure
            if not (version_dir / "apps").exists():
                logger.error("Invalid bundle: missing apps directory")
                shutil.rmtree(version_dir)
                return False
            
            logger.info(f"Extracted bundle to {version_dir}")
            return True
            
        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            if version_dir.exists():
                shutil.rmtree(version_dir)
            return False
    
    def stop_services(self) -> bool:
        """Stop all managed services before update"""
        logger.info("Stopping services...")
        
        for service in self.MANAGED_SERVICES:
            try:
                subprocess.run(
                    ["sudo", "systemctl", "stop", service],
                    capture_output=True,
                    timeout=30
                )
                logger.info(f"Stopped {service}")
            except Exception as e:
                logger.warning(f"Failed to stop {service}: {e}")
        
        time.sleep(2)  # Wait for services to fully stop
        return True
    
    def start_services(self) -> bool:
        """Start all managed services after update"""
        logger.info("Starting services...")
        
        for service in self.MANAGED_SERVICES:
            try:
                subprocess.run(
                    ["sudo", "systemctl", "start", service],
                    capture_output=True,
                    timeout=30
                )
                logger.info(f"Started {service}")
            except Exception as e:
                logger.error(f"Failed to start {service}: {e}")
                return False
        
        time.sleep(5)  # Wait for services to initialize
        return True
    
    def run_migration(self, version_dir: Path) -> bool:
        """Run migration script if exists"""
        migrate_script = version_dir / "migrate.py"
        
        if not migrate_script.exists():
            logger.info("No migration script found")
            return True
        
        logger.info("Running migration script...")
        
        try:
            result = subprocess.run(
                [sys.executable, str(migrate_script)],
                capture_output=True,
                timeout=120,
                cwd=str(version_dir)
            )
            
            if result.returncode != 0:
                logger.error(f"Migration failed: {result.stderr.decode()}")
                return False
            
            logger.info("Migration completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Migration error: {e}")
            return False
    
    def _switch_version(self, version: str) -> bool:
        """Atomically switch current symlink to new version"""
        version_dir = self.releases_dir / version
        
        if not version_dir.exists():
            logger.error(f"Version directory not found: {version_dir}")
            return False
        
        try:
            # Create temporary symlink
            temp_link = self.base_dir / "current.tmp"
            if temp_link.exists():
                temp_link.unlink()
            
            temp_link.symlink_to(version_dir)
            
            # Atomic rename
            temp_link.rename(self.current_link)
            
            logger.info(f"Switched to version {version}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to switch version: {e}")
            return False
    
    def health_check(self) -> bool:
        """
        Perform health checks on all services.
        
        Checks:
        1. All services are active
        2. No crash loops
        3. Services respond correctly
        """
        logger.info("Running health checks...")
        
        all_healthy = True
        
        for service in self.MANAGED_SERVICES:
            try:
                # Check if service is active
                result = subprocess.run(
                    ["systemctl", "is-active", service],
                    capture_output=True,
                    timeout=10
                )
                
                is_active = result.stdout.decode().strip() == "active"
                
                if not is_active:
                    logger.error(f"Service {service} is not active")
                    all_healthy = False
                else:
                    logger.info(f"Service {service} is healthy")
                    
            except Exception as e:
                logger.error(f"Health check failed for {service}: {e}")
                all_healthy = False
        
        return all_healthy
    
    def rollback(self) -> bool:
        """Rollback to previous version"""
        versions = self.get_installed_versions()
        current = self.get_current_version()
        
        # Find previous version
        try:
            current_idx = versions.index(current)
            if current_idx == 0:
                logger.error("No previous version to rollback to")
                return False
            
            previous_version = versions[current_idx - 1]
            
        except ValueError:
            if len(versions) > 0:
                previous_version = versions[-1]
            else:
                logger.error("No versions available for rollback")
                return False
        
        logger.info(f"Rolling back from v{current} to v{previous_version}")
        
        self.stop_services()
        
        if not self._switch_version(f"v{previous_version}"):
            return False
        
        self.start_services()
        
        # Verify rollback
        if not self.health_check():
            logger.error("Rollback failed - services unhealthy")
            return False
        
        logger.info(f"Rollback to v{previous_version} successful")
        return True
    
    def apply_update(self, update: UpdateBundle) -> bool:
        """
        Apply an update following the OTA guide flow:
        
        1. Download bundle
        2. Verify bundle
        3. Stop services
        4. Run migration (if exists)
        5. Switch version (atomic symlink)
        6. Start services
        7. Health check
        8. Rollback on failure
        """
        logger.info(f"Applying update to v{update.version}")
        
        previous_version = self.get_current_version()
        
        # Step 1: Download
        bundle_file = self.download_bundle(update)
        if not bundle_file:
            return False
        
        # Step 2: Extract and verify
        if not self.extract_bundle(bundle_file, update.version):
            return False
        
        version_dir = self.releases_dir / f"v{update.version}"
        
        # Step 3: Stop services
        self.stop_services()
        
        # Step 4: Run migration
        if not self.run_migration(version_dir):
            logger.error("Migration failed, aborting update")
            self.start_services()
            return False
        
        # Step 5: Switch version (atomic)
        if not self._switch_version(f"v{update.version}"):
            logger.error("Failed to switch version")
            self.start_services()
            return False
        
        # Step 6: Start services
        self.start_services()
        
        # Step 7: Health check
        time.sleep(10)  # Wait for services to stabilize
        
        for attempt in range(self.config.get("health_check_retries", 3)):
            if self.health_check():
                logger.info(f"Update to v{update.version} successful!")
                
                # Cleanup old download
                bundle_file.unlink(missing_ok=True)
                
                # Report success to backend
                self._report_update_status(update.version, "success")
                return True
            
            logger.warning(f"Health check failed, attempt {attempt + 1}")
            time.sleep(5)
        
        # Step 8: Rollback on failure
        logger.error("Health checks failed, initiating rollback")
        
        if self.config.get("auto_rollback", True):
            self.stop_services()
            self._switch_version(f"v{previous_version}")
            self.start_services()
            
            self._report_update_status(update.version, "failed", "Health check failed, rolled back")
        
        return False
    
    def _report_update_status(self, version: str, status: str, error: str = None):
        """Report update status to backend"""
        device_id, auth_key = self.get_device_credentials()
        if not device_id:
            return
        
        try:
            url = f"{self.config['api_base_url']}/api/auth/ota/report-status"
            
            data = json.dumps({
                "device_id": device_id,
                "version": version,
                "status": status,
                "error": error
            }).encode()
            
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('X-Device-ID', device_id)
            req.add_header('X-Auth-Key', auth_key)
            req.add_header('Content-Type', 'application/json')
            
            urllib.request.urlopen(req, timeout=10)
            
        except Exception as e:
            logger.warning(f"Failed to report status: {e}")
    
    def run_daemon(self):
        """Run as background daemon, checking for updates periodically"""
        logger.info("Starting OTA updater daemon")
        
        check_interval = self.config.get("check_interval_hours", 6) * 3600
        
        while True:
            try:
                update = self.check_for_updates()
                
                if update and self.config.get("auto_update", True):
                    logger.info(f"Found update: v{update.version}")
                    self.apply_update(update)
                
            except Exception as e:
                logger.error(f"Daemon error: {e}")
            
            time.sleep(check_interval)


def main():
    """CLI interface for OTA updater"""
    import argparse
    
    parser = argparse.ArgumentParser(description="OTA Updater")
    parser.add_argument("command", choices=[
        "check", "update", "rollback", "status", "daemon", "init"
    ])
    parser.add_argument("--version", help="Specific version for operations")
    
    args = parser.parse_args()
    
    updater = OTAUpdater()
    
    if args.command == "check":
        update = updater.check_for_updates()
        if update:
            print(f"Update available: v{update.version}")
            print(f"  Size: {update.size} bytes")
            print(f"  Notes: {update.release_notes}")
        else:
            print("No updates available")
    
    elif args.command == "update":
        update = updater.check_for_updates()
        if update:
            success = updater.apply_update(update)
            sys.exit(0 if success else 1)
        else:
            print("No updates available")
    
    elif args.command == "rollback":
        success = updater.rollback()
        sys.exit(0 if success else 1)
    
    elif args.command == "status":
        print(f"Current version: v{updater.get_current_version()}")
        print(f"Installed versions: {', '.join(['v' + v for v in updater.get_installed_versions()])}")
        print(f"Current symlink: {updater.current_link} -> {updater.current_link.resolve() if updater.current_link.exists() else 'N/A'}")
    
    elif args.command == "daemon":
        updater.run_daemon()
    
    elif args.command == "init":
        print("Initializing OTA structure...")
        print(f"Current version: v{updater.get_current_version()}")


if __name__ == "__main__":
    main()
