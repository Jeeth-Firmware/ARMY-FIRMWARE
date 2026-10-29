#!/usr/bin/env python3
"""
OTA Startup Script - Runs on device boot
Checks for updates, applies them, runs migrations, and starts services.

Flow:
1. Check for pending OTA updates from backend
2. Download and apply updates if available
3. Run database migrations if needed
4. Start all required services

Enhanced with:
- Integration with VersionManager for proper version tracking
- Integration with OTAManager for robust update handling
- Automatic rollback on failure
- Health checks after updates
"""

import json
import logging
import os
import subprocess
import sys
import time
import hashlib
import shutil
from pathlib import Path
from datetime import datetime

# Setup logging - use user-writable location
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "ota_startup.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger("ota_startup")

# Try to import enhanced managers
try:
    from version_manager import VersionManager
    from ota_manager import OTAManager
    USE_ENHANCED_OTA = True
    logger.info("Using enhanced OTA system")
except ImportError:
    USE_ENHANCED_OTA = False
    logger.info("Using legacy OTA system")

# Configuration
class Config:
    # Backend API
    API_BASE_URL = os.getenv("API_BASE_URL", "https://api.copilotai.click")
    
    # Paths
    FIRMWARE_DIR = os.getenv("FIRMWARE_DIR", "/home/pi/facial-tracker-firmware")
    BACKUP_DIR = os.getenv("BACKUP_DIR", "/home/pi/facial-tracker-firmware/backups")
    OTA_DIR = os.getenv("OTA_DIR", "/home/pi/facial-tracker-firmware/ota")
    DB_PATH = os.getenv("DB_PATH", "/home/pi/facial-tracker-firmware/data/blinksmart.db")
    MIGRATIONS_DIR = os.getenv("MIGRATIONS_DIR", "/home/pi/facial-tracker-firmware/migrations")
    VERSION_FILE = os.getenv("VERSION_FILE", "/home/pi/facial-tracker-firmware/ota/version.json")
    
    # Services to manage
    SERVICES = [
        "facial1.service",
        "get_gps_data1.service",
        "pi-control.service",
    ]
    
    # Retry settings
    MAX_RETRIES = 3
    RETRY_DELAY = 5


def get_device_id():
    """Get device ID from SQLite database"""
    try:
        import db_helper
        row = db_helper.fetchone("SELECT device_id FROM device LIMIT 1")
        if row:
            return row['device_id']
        return None
    except Exception as e:
        logger.error(f"Failed to get device ID: {e}")
        return None


def get_auth_key():
    """Get auth key from SQLite database"""
    try:
        import db_helper
        row = db_helper.fetchone("SELECT auth_key FROM device LIMIT 1")
        if row:
            return row['auth_key']
        return None
    except Exception as e:
        logger.error(f"Failed to get auth key: {e}")
        return None


def get_access_token():
    """Get access token from SQLite database (legacy - for backward compatibility)"""
    try:
        import db_helper
        row = db_helper.fetchone("SELECT access_token FROM user_info LIMIT 1")
        if row:
            return row['access_token']
        return None
    except Exception as e:
        logger.error(f"Failed to get access token: {e}")
        return None


def get_current_version():
    """Get current firmware version"""
    try:
        version_file = Path(Config.VERSION_FILE)
        if version_file.exists():
            with open(version_file) as f:
                data = json.load(f)
                return data.get("version", "1.0.0")
    except Exception as e:
        logger.warning(f"Failed to read version file: {e}")
    return "1.0.0"


def save_version(version, installed_files=None):
    """Save current version to file"""
    try:
        version_file = Path(Config.VERSION_FILE)
        version_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": version,
            "updated_at": datetime.now().isoformat(),
            "installed_files": installed_files or {}
        }
        with open(version_file, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save version: {e}")


def check_for_updates(device_id, token):
    """Check backend for pending OTA updates (manual deployments)"""
    import urllib.request
    import urllib.error
    
    try:
        current_version = get_current_version()
        url = f"{Config.API_BASE_URL}/api/auth/ota/check-updates?device_id={device_id}&current_version={current_version}"
        
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            
            if data.get("success") and data.get("updates_available"):
                logger.info(f"Updates available: {len(data.get('updates', []))} file(s)")
                return data.get("updates", [])
            else:
                logger.info("No updates available")
                return []
                
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP error checking updates: {e.code} - {e.reason}")
    except urllib.error.URLError as e:
        logger.error(f"URL error checking updates: {e.reason}")
    except Exception as e:
        logger.error(f"Error checking updates: {e}")
    
    return []


def check_auto_update(device_id, token):
    """Check backend for latest bundle (auto-update without manual deployment)"""
    import urllib.request
    import urllib.error
    
    try:
        current_version = get_current_version()
        url = f"{Config.API_BASE_URL}/api/auth/ota/auto-update?device_id={device_id}&current_version={current_version}"
        
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode())
            
            if data.get("success") and data.get("update_available"):
                update = data.get("update", {})
                logger.info(f"Auto-update available: {update.get('version')} (current: {current_version})")
                return [update]  # Return as list for compatibility with download_update
            else:
                logger.info(f"No auto-update available (current: {current_version}, latest: {data.get('latest_version', 'unknown')})")
                return []
                
    except urllib.error.HTTPError as e:
        logger.error(f"HTTP error checking auto-update: {e.code} - {e.reason}")
    except urllib.error.URLError as e:
        logger.error(f"URL error checking auto-update: {e.reason}")
    except Exception as e:
        logger.error(f"Error checking auto-update: {e}")
    
    return []


def download_update(update, token):
    """Download an OTA update file"""
    import urllib.request
    
    try:
        filename = update.get("name")
        download_url = update.get("download_url") or update.get("s3_url")
        checksum = update.get("checksum")
        target_path = update.get("target_path")
        
        if not download_url:
            logger.error(f"No download URL for {filename}")
            return False
        
        logger.info(f"Downloading: {filename}")
        
        # Determine save path
        if target_path:
            save_path = Path(target_path)
            if save_path.is_dir():
                save_path = save_path / filename
        else:
            save_path = Path(Config.FIRMWARE_DIR) / filename
        
        # Create backup if file exists
        if save_path.exists():
            backup_name = f"{save_path.name}.backup_{int(time.time())}"
            backup_path = Path(Config.BACKUP_DIR) / backup_name
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(save_path, backup_path)
            logger.info(f"Created backup: {backup_path}")
        
        # Download to temp file
        temp_path = save_path.with_suffix(save_path.suffix + '.tmp')
        save_path.parent.mkdir(parents=True, exist_ok=True)
        
        urllib.request.urlretrieve(download_url, temp_path)
        
        # Verify checksum
        if checksum:
            with open(temp_path, 'rb') as f:
                actual_checksum = hashlib.md5(f.read()).hexdigest()
            if actual_checksum != checksum:
                temp_path.unlink()
                logger.error(f"Checksum mismatch for {filename}")
                return False
        
        # Move to final location
        shutil.move(str(temp_path), str(save_path))
        logger.info(f"Downloaded: {filename} -> {save_path}")
        
        return True
        
    except Exception as e:
        logger.error(f"Error downloading {update.get('name')}: {e}")
        return False


def report_update_status(device_id, token, deployment_id, status, error_message=None):
    """Report update status back to backend"""
    import urllib.request
    
    try:
        url = f"{Config.API_BASE_URL}/api/ota/deployments/{deployment_id}/status"
        
        data = json.dumps({
            "device_id": device_id,
            "status": status,
            "error_message": error_message
        }).encode()
        
        req = urllib.request.Request(url, data=data, method='PUT')
        req.add_header('Authorization', f'Bearer {token}')
        req.add_header('Content-Type', 'application/json')
        
        with urllib.request.urlopen(req, timeout=30) as response:
            logger.info(f"Reported status: {status} for deployment {deployment_id}")
            
    except Exception as e:
        logger.error(f"Error reporting status: {e}")


def run_migrations():
    """Run database migrations if any"""
    migrations_dir = Path(Config.MIGRATIONS_DIR)
    
    if not migrations_dir.exists():
        logger.info("No migrations directory found")
        return True
    
    # Get list of migration files
    migrations = sorted(migrations_dir.glob("*.sql"))
    
    if not migrations:
        logger.info("No migrations to run")
        return True
    
    # Track applied migrations
    applied_file = migrations_dir / ".applied"
    applied = set()
    if applied_file.exists():
        applied = set(applied_file.read_text().strip().split('\n'))
    
    # Run pending migrations using SQLite
    import db_helper

    try:
        for migration in migrations:
            if migration.name in applied:
                continue

            logger.info(f"Running migration: {migration.name}")

            try:
                sql = migration.read_text()
                for statement in sql.split(';'):
                    statement = statement.strip()
                    if statement:
                        db_helper.execute_commit(statement)
                applied.add(migration.name)
                logger.info(f"Migration completed: {migration.name}")
            except Exception as e:
                logger.error(f"Migration failed: {migration.name} - {e}")
                return False

        # Save applied migrations
        applied_file.write_text('\n'.join(sorted(applied)))

        return True

    except Exception as e:
        logger.error(f"Database error: {e}")
        return False


def stop_services():
    """Stop all managed services"""
    logger.info("Stopping services...")
    for service in Config.SERVICES:
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'stop', service],
                capture_output=True, timeout=30
            )
            logger.info(f"Stopped: {service}")
        except Exception as e:
            logger.warning(f"Failed to stop {service}: {e}")


def start_services():
    """Start all managed services"""
    logger.info("Starting services...")
    for service in Config.SERVICES:
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'start', service],
                capture_output=True, timeout=30
            )
            # Check if started
            result = subprocess.run(
                ['sudo', 'systemctl', 'is-active', service],
                capture_output=True, text=True, timeout=10
            )
            if result.stdout.strip() == 'active':
                logger.info(f"Started: {service}")
            else:
                logger.warning(f"Service not active: {service}")
        except Exception as e:
            logger.warning(f"Failed to start {service}: {e}")


def restart_service(service):
    """Restart a specific service"""
    try:
        subprocess.run(
            ['sudo', 'systemctl', 'restart', service],
            capture_output=True, timeout=30
        )
        time.sleep(2)
        result = subprocess.run(
            ['sudo', 'systemctl', 'is-active', service],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() == 'active'
    except Exception as e:
        logger.error(f"Failed to restart {service}: {e}")
        return False


def auto_restart_pi_control_service():
    """Auto restart pi-control.service to ensure it's running with latest changes"""
    service = "pi-control.service"
    logger.info(f"Auto-restarting {service}...")
    
    try:
        # First, reload systemd daemon in case service file changed
        subprocess.run(
            ['sudo', 'systemctl', 'daemon-reload'],
            capture_output=True, timeout=30
        )
        
        # Restart the service
        subprocess.run(
            ['sudo', 'systemctl', 'restart', service],
            capture_output=True, timeout=30
        )
        
        # Wait for service to stabilize
        time.sleep(3)
        
        # Check if service is active
        result = subprocess.run(
            ['sudo', 'systemctl', 'is-active', service],
            capture_output=True, text=True, timeout=10
        )
        
        if result.stdout.strip() == 'active':
            logger.info(f"✓ {service} restarted successfully")
            return True
        else:
            logger.warning(f"✗ {service} is not active after restart")
            # Try to get status for debugging
            status_result = subprocess.run(
                ['sudo', 'systemctl', 'status', service],
                capture_output=True, text=True, timeout=10
            )
            logger.warning(f"Service status: {status_result.stdout}")
            return False
            
    except Exception as e:
        logger.error(f"Failed to auto-restart {service}: {e}")
        return False


def main_enhanced():
    """Enhanced main startup sequence using OTAManager"""
    logger.info("=" * 50)
    logger.info("OTA Startup Script - Enhanced Mode")
    logger.info("=" * 50)
    
    try:
        ota_manager = OTAManager(Config.FIRMWARE_DIR)
        version_manager = VersionManager(Config.FIRMWARE_DIR)
        
        logger.info(f"Current firmware version: {version_manager.get_version()}")
        
        # Verify integrity of current installation
        integrity = version_manager.verify_integrity()
        failed_checks = [k for k, v in integrity.items() if not v]
        if failed_checks:
            logger.warning(f"Integrity check failed for: {failed_checks}")
        
        # Run update cycle
        ota_manager.run_update_cycle()
        
        # Ensure services are running
        health_results = ota_manager.run_health_checks()
        for result in health_results:
            if result.status == "healthy":
                logger.info(f"✓ {result.service}: healthy")
            else:
                logger.warning(f"✗ {result.service}: {result.status}")
        
        # Auto restart pi-control service
        auto_restart_pi_control_service()
        
    except Exception as e:
        logger.error(f"Enhanced OTA failed: {e}")
        # Fall back to legacy mode
        main_legacy()


def main_legacy():
    """Legacy main startup sequence"""
    logger.info("=" * 50)
    logger.info("OTA Startup Script - Legacy Mode")
    logger.info("=" * 50)
    
    # Get device credentials
    device_id = get_device_id()
    auth_key = get_auth_key()
    
    # Use auth_key as primary authentication, fall back to access_token for legacy
    token = auth_key if auth_key else get_access_token()
    
    if not device_id:
        logger.error("No device ID found - device not provisioned")
        logger.error("Run: python device_provisioning.py")
    elif not token:
        logger.error("No auth_key or access_token found - device not provisioned")
        logger.error("Run: python device_provisioning.py")
    else:
        logger.info(f"Device ID: {device_id}")
        logger.info(f"Current version: {get_current_version()}")
        
        # Check for manual deployments first
        updates = []
        for attempt in range(Config.MAX_RETRIES):
            updates = check_for_updates(device_id, token)
            if updates is not None:
                break
            logger.warning(f"Retry {attempt + 1}/{Config.MAX_RETRIES}...")
            time.sleep(Config.RETRY_DELAY)
        
        # If no manual deployments, check for auto-updates (latest bundle)
        if not updates:
            logger.info("No manual deployments, checking for auto-updates...")
            for attempt in range(Config.MAX_RETRIES):
                updates = check_auto_update(device_id, token)
                if updates is not None:
                    break
                logger.warning(f"Auto-update retry {attempt + 1}/{Config.MAX_RETRIES}...")
                time.sleep(Config.RETRY_DELAY)
        
        # Apply updates if available
        if updates:
            logger.info(f"Applying {len(updates)} update(s)...")
            
            # Stop services before updating
            stop_services()
            
            success_count = 0
            installed_files = {}
            
            for update in updates:
                deployment_id = update.get("deployment_id")
                
                # Report downloading status
                if deployment_id:
                    report_update_status(device_id, token, deployment_id, "downloading")
                
                # Download and apply
                if download_update(update, token):
                    success_count += 1
                    installed_files[update.get("name")] = update.get("version", "unknown")
                    
                    if deployment_id:
                        report_update_status(device_id, token, deployment_id, "success")
                else:
                    if deployment_id:
                        report_update_status(device_id, token, deployment_id, "failed", "Download failed")
            
            logger.info(f"Updates applied: {success_count}/{len(updates)}")
            
            # Run migrations after updates
            if success_count > 0:
                logger.info("Running database migrations...")
                if run_migrations():
                    logger.info("Migrations completed successfully")
                else:
                    logger.error("Some migrations failed")
                
                # Update version
                if updates:
                    new_version = updates[0].get("version", get_current_version())
                    save_version(new_version, installed_files)
            
            # Start services after updates
            start_services()
            
            # Auto restart pi-control service after updates
            auto_restart_pi_control_service()
        else:
            logger.info("No updates to apply, ensuring services are running...")
            start_services()
            
            # Auto restart pi-control service
            auto_restart_pi_control_service()
    
    logger.info("OTA Startup Script - Complete")
    logger.info("=" * 50)


def main():
    """Main entry point - uses enhanced or legacy mode based on availability"""
    logger.info("=" * 50)
    logger.info("OTA Startup Script - Starting")
    logger.info("=" * 50)
    
    if USE_ENHANCED_OTA:
        main_enhanced()
    else:
        main_legacy()
    
    logger.info("OTA Startup Script - Complete")
    logger.info("=" * 50)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Startup script failed: {e}")
        # Ensure services start even if OTA fails
        start_services()
        sys.exit(1)
