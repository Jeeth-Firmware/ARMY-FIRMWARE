#!/usr/bin/env python3
"""
Version Manager - Centralized version management for all firmware components.

Best Practices Implemented:
1. Semantic Versioning (SemVer) for all components
2. Individual component versioning with global firmware version
3. Version history tracking for rollback capability
4. Checksum verification for integrity
5. Atomic updates with backup/restore
"""

import json
import os
import hashlib
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum


class UpdateStatus(Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    INSTALLING = "installing"
    SUCCESS = "success"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class ComponentVersion:
    """Version info for a single component/file"""
    name: str
    version: str
    checksum: str
    updated_at: str
    file_path: str
    backup_path: Optional[str] = None


@dataclass
class FirmwareManifest:
    """Complete firmware manifest with all component versions"""
    firmware_version: str
    updated_at: str
    device_id: Optional[str] = None
    components: Dict[str, dict] = None
    update_history: List[dict] = None
    
    def __post_init__(self):
        if self.components is None:
            self.components = {}
        if self.update_history is None:
            self.update_history = []


class VersionManager:
    """
    Manages versions for all firmware components.
    
    Directory Structure:
    /home/pi/facial-tracker-firmware/
    ├── ota/
    │   ├── manifest.json          # Main version manifest
    │   ├── version.json           # Legacy compatibility
    │   └── history/               # Update history logs
    ├── backups/
    │   ├── current/               # Current backup (for quick rollback)
    │   └── archive/               # Archived backups (timestamped)
    └── [component files]
    """
    
    # Components to track with their relative paths
    TRACKED_COMPONENTS = {
        "facil_updated_1.py": "facil_updated_1.py",
        "get_gps_data.py": "get_gps_data.py",
        "device_provisioning.py": "device_provisioning.py",
        "provisioning_ui.py": "provisioning_ui.py",
        "ota_startup.py": "ota_startup.py",
        "store_locally.py": "store_locally.py",
        "get_user_info.py": "get_user_info.py",
        "get_configure.py": "get_configure.py",
    }
    
    # Systemd services mapping
    SERVICE_COMPONENTS = {
        "facial1.service": ["facil_updated_1.py"],
        "get_gps_data1.service": ["get_gps_data.py"],
        "device_provisioning.service": ["device_provisioning.py"],
        "provisioning_ui.service": ["provisioning_ui.py"],
        "ota-startup.service": ["ota_startup.py"],
    }
    
    def __init__(self, base_dir: str = "/home/pi/facial-tracker-firmware"):
        self.base_dir = Path(base_dir)
        self.ota_dir = self.base_dir / "ota"
        self.manifest_file = self.ota_dir / "manifest.json"
        self.version_file = self.ota_dir / "version.json"  # Legacy
        self.history_dir = self.ota_dir / "history"
        self.backup_dir = self.base_dir / "backups"
        self.current_backup_dir = self.backup_dir / "current"
        self.archive_backup_dir = self.backup_dir / "archive"
        
        self._ensure_directories()
        self._manifest: Optional[FirmwareManifest] = None
    
    def _ensure_directories(self):
        """Create required directories"""
        for d in [self.ota_dir, self.history_dir, self.current_backup_dir, self.archive_backup_dir]:
            d.mkdir(parents=True, exist_ok=True)
    
    @property
    def manifest(self) -> FirmwareManifest:
        """Load or create manifest"""
        if self._manifest is None:
            self._manifest = self._load_manifest()
        return self._manifest
    
    def _load_manifest(self) -> FirmwareManifest:
        """Load manifest from file or create new one"""
        if self.manifest_file.exists():
            try:
                with open(self.manifest_file) as f:
                    data = json.load(f)
                return FirmwareManifest(**data)
            except Exception:
                pass
        
        # Try legacy version.json
        if self.version_file.exists():
            try:
                with open(self.version_file) as f:
                    data = json.load(f)
                return FirmwareManifest(
                    firmware_version=data.get("version", "1.0.0"),
                    updated_at=data.get("updated_at", datetime.now().isoformat()),
                    components=data.get("installed_files", {})
                )
            except Exception:
                pass
        
        # Create new manifest with current file checksums
        return self._create_initial_manifest()
    
    def _create_initial_manifest(self) -> FirmwareManifest:
        """Create initial manifest by scanning current files"""
        components = {}
        for name, rel_path in self.TRACKED_COMPONENTS.items():
            file_path = self.base_dir / rel_path
            if file_path.exists():
                components[name] = asdict(ComponentVersion(
                    name=name,
                    version="1.0.0",
                    checksum=self._calculate_checksum(file_path),
                    updated_at=datetime.now().isoformat(),
                    file_path=str(file_path)
                ))
        
        return FirmwareManifest(
            firmware_version="1.0.0",
            updated_at=datetime.now().isoformat(),
            components=components
        )
    
    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate MD5 checksum of a file"""
        if not file_path.exists():
            return ""
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    
    def save_manifest(self):
        """Save manifest to file"""
        with open(self.manifest_file, 'w') as f:
            json.dump(asdict(self.manifest), f, indent=2)
        
        # Also save legacy version.json for compatibility
        legacy_data = {
            "version": self.manifest.firmware_version,
            "updated_at": self.manifest.updated_at,
            "installed_files": {
                name: comp.get("version", "unknown")
                for name, comp in self.manifest.components.items()
            }
        }
        with open(self.version_file, 'w') as f:
            json.dump(legacy_data, f, indent=2)
    
    def get_version(self) -> str:
        """Get current firmware version"""
        return self.manifest.firmware_version
    
    def get_component_version(self, component: str) -> Optional[str]:
        """Get version of a specific component"""
        comp = self.manifest.components.get(component)
        return comp.get("version") if comp else None
    
    def get_all_versions(self) -> Dict[str, str]:
        """Get all component versions"""
        return {
            name: comp.get("version", "unknown")
            for name, comp in self.manifest.components.items()
        }
    
    def create_backup(self, component: str = None) -> bool:
        """
        Create backup of component(s).
        If component is None, backup all tracked components.
        """
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            if component:
                components = [component]
            else:
                components = list(self.TRACKED_COMPONENTS.keys())
            
            for comp_name in components:
                rel_path = self.TRACKED_COMPONENTS.get(comp_name)
                if not rel_path:
                    continue
                
                src_path = self.base_dir / rel_path
                if not src_path.exists():
                    continue
                
                # Current backup (quick rollback)
                current_backup = self.current_backup_dir / comp_name
                if src_path.exists():
                    shutil.copy2(src_path, current_backup)
                
                # Archive backup (timestamped)
                archive_dir = self.archive_backup_dir / timestamp
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_backup = archive_dir / comp_name
                shutil.copy2(src_path, archive_backup)
                
                # Update manifest with backup path
                if comp_name in self.manifest.components:
                    self.manifest.components[comp_name]["backup_path"] = str(current_backup)
            
            self.save_manifest()
            return True
            
        except Exception as e:
            print(f"Backup failed: {e}")
            return False
    
    def rollback(self, component: str = None) -> bool:
        """
        Rollback component(s) to previous version.
        If component is None, rollback all components.
        """
        try:
            if component:
                components = [component]
            else:
                components = list(self.TRACKED_COMPONENTS.keys())
            
            rolled_back = []
            for comp_name in components:
                backup_path = self.current_backup_dir / comp_name
                if not backup_path.exists():
                    continue
                
                rel_path = self.TRACKED_COMPONENTS.get(comp_name)
                if not rel_path:
                    continue
                
                dest_path = self.base_dir / rel_path
                shutil.copy2(backup_path, dest_path)
                rolled_back.append(comp_name)
                
                # Update manifest
                if comp_name in self.manifest.components:
                    self.manifest.components[comp_name]["checksum"] = self._calculate_checksum(dest_path)
                    self.manifest.components[comp_name]["updated_at"] = datetime.now().isoformat()
            
            # Log rollback in history
            self._log_update({
                "action": "rollback",
                "components": rolled_back,
                "timestamp": datetime.now().isoformat(),
                "status": UpdateStatus.ROLLED_BACK.value
            })
            
            self.save_manifest()
            return True
            
        except Exception as e:
            print(f"Rollback failed: {e}")
            return False
    
    def update_component(
        self,
        component: str,
        new_file_path: Path,
        new_version: str,
        checksum: str = None
    ) -> Tuple[bool, str]:
        """
        Update a component with a new version.
        Returns (success, message)
        """
        try:
            rel_path = self.TRACKED_COMPONENTS.get(component)
            if not rel_path:
                return False, f"Unknown component: {component}"
            
            dest_path = self.base_dir / rel_path
            
            # Verify checksum if provided
            if checksum:
                actual_checksum = self._calculate_checksum(new_file_path)
                if actual_checksum != checksum:
                    return False, f"Checksum mismatch for {component}"
            
            # Create backup before update
            self.create_backup(component)
            
            # Copy new file
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(new_file_path, dest_path)
            
            # Update manifest
            self.manifest.components[component] = asdict(ComponentVersion(
                name=component,
                version=new_version,
                checksum=self._calculate_checksum(dest_path),
                updated_at=datetime.now().isoformat(),
                file_path=str(dest_path),
                backup_path=str(self.current_backup_dir / component)
            ))
            
            self.save_manifest()
            return True, f"Updated {component} to {new_version}"
            
        except Exception as e:
            return False, f"Update failed: {e}"
    
    def bump_version(self, bump_type: str = "patch") -> str:
        """
        Bump firmware version.
        bump_type: major, minor, or patch
        """
        current = self.manifest.firmware_version
        parts = current.split(".")
        
        if len(parts) != 3:
            parts = ["1", "0", "0"]
        
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        
        if bump_type == "major":
            major += 1
            minor = 0
            patch = 0
        elif bump_type == "minor":
            minor += 1
            patch = 0
        else:  # patch
            patch += 1
        
        new_version = f"{major}.{minor}.{patch}"
        self.manifest.firmware_version = new_version
        self.manifest.updated_at = datetime.now().isoformat()
        self.save_manifest()
        
        return new_version
    
    def _log_update(self, update_info: dict):
        """Log update to history"""
        self.manifest.update_history.append(update_info)
        
        # Also save to history file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_file = self.history_dir / f"update_{timestamp}.json"
        with open(history_file, 'w') as f:
            json.dump(update_info, f, indent=2)
    
    def get_update_history(self, limit: int = 10) -> List[dict]:
        """Get recent update history"""
        return self.manifest.update_history[-limit:]
    
    def verify_integrity(self) -> Dict[str, bool]:
        """Verify integrity of all tracked components"""
        results = {}
        for name, rel_path in self.TRACKED_COMPONENTS.items():
            file_path = self.base_dir / rel_path
            if not file_path.exists():
                results[name] = False
                continue
            
            comp = self.manifest.components.get(name)
            if not comp:
                results[name] = True  # Not tracked yet
                continue
            
            expected_checksum = comp.get("checksum", "")
            actual_checksum = self._calculate_checksum(file_path)
            results[name] = expected_checksum == actual_checksum
        
        return results
    
    def get_services_for_component(self, component: str) -> List[str]:
        """Get systemd services that use a component"""
        services = []
        for service, components in self.SERVICE_COMPONENTS.items():
            if component in components:
                services.append(service)
        return services


# CLI interface
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Firmware Version Manager")
    parser.add_argument("command", choices=[
        "version", "list", "backup", "rollback", "verify", "bump", "history"
    ])
    parser.add_argument("--component", "-c", help="Specific component name")
    parser.add_argument("--type", "-t", choices=["major", "minor", "patch"], default="patch")
    parser.add_argument("--base-dir", "-d", default="/home/pi/facial-tracker-firmware")
    
    args = parser.parse_args()
    
    vm = VersionManager(args.base_dir)
    
    if args.command == "version":
        if args.component:
            print(f"{args.component}: {vm.get_component_version(args.component)}")
        else:
            print(f"Firmware Version: {vm.get_version()}")
    
    elif args.command == "list":
        print("Component Versions:")
        for name, version in vm.get_all_versions().items():
            print(f"  {name}: {version}")
    
    elif args.command == "backup":
        if vm.create_backup(args.component):
            print("Backup created successfully")
        else:
            print("Backup failed")
    
    elif args.command == "rollback":
        if vm.rollback(args.component):
            print("Rollback successful")
        else:
            print("Rollback failed")
    
    elif args.command == "verify":
        results = vm.verify_integrity()
        print("Integrity Check:")
        for name, valid in results.items():
            status = "✓" if valid else "✗"
            print(f"  {status} {name}")
    
    elif args.command == "bump":
        new_version = vm.bump_version(args.type)
        print(f"Version bumped to: {new_version}")
    
    elif args.command == "history":
        history = vm.get_update_history()
        print("Update History:")
        for entry in history:
            print(f"  {entry.get('timestamp')}: {entry.get('action')} - {entry.get('status')}")
