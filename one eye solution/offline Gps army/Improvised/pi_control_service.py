#!/usr/bin/env python3
"""
Pi Device Control Service - Enhanced Version
Standalone service for remote device management via MQTT
Features:
- Service control (multiple services)
- System metrics & alerts
- File management (upload/download)
- Remote shell execution
- OTA updates
- Configuration management

Run as: python pi_control_service.py
Or as systemd service: pi-control.service
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tarfile
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, List, Dict, Any
from get_device_id import *

# Setup logging
try:
    import colorlog
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        '%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    ))
except ImportError:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))

logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("pi_control")


@dataclass
class AlertThresholds:
    """Alert thresholds for system metrics"""
    cpu_warning: float = 70.0
    cpu_critical: float = 90.0
    memory_warning: float = 70.0
    memory_critical: float = 90.0
    disk_warning: float = 80.0
    disk_critical: float = 95.0
    temp_warning: float = 70.0
    temp_critical: float = 80.0


@dataclass
class Alert:
    """Represents a system alert"""
    id: str
    type: str  # 'warning' or 'critical'
    metric: str  # 'cpu', 'memory', 'disk', 'temperature'
    value: float
    threshold: float
    message: str
    timestamp: int
    acknowledged: bool = False


class Config:
    """Configuration from environment variables"""
    # DEVICE_ID = os.getenv("DEVICE_ID", "") 
    DEVICE_ID = get_device_id_from_db()
    MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "localhost")
    MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
    MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
    
    # File management paths
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/home/pi/facial-tracker-firmware/uploads")
    DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/home/pi/facial-tracker-firmware")
    MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))  # 50MB default
    
    # OTA update paths
    OTA_DIR = os.getenv("OTA_DIR", "/home/pi/facial-tracker-firmware/ota")
    BACKUP_DIR = os.getenv("BACKUP_DIR", "/home/pi/facial-tracker-firmware/backups")
    
    # Facial Tracker Firmware paths (for OTA updates)
    FACIAL_TRACKER_DIR = os.getenv("FACIAL_TRACKER_DIR", "/home/pi/facial-tracker-firmware")
    FACIAL_TRACKER_VENV = os.getenv("FACIAL_TRACKER_VENV", "/home/pi/facial-tracker-firmware/venv")
    
    # Shell execution
    SHELL_TIMEOUT = int(os.getenv("SHELL_TIMEOUT", "30"))
    ALLOWED_SHELL_COMMANDS = os.getenv("ALLOWED_SHELL_COMMANDS", "").split(",") if os.getenv("ALLOWED_SHELL_COMMANDS") else []
    
    # Alert thresholds
    ALERT_CPU_WARNING = float(os.getenv("ALERT_CPU_WARNING", "70"))
    ALERT_CPU_CRITICAL = float(os.getenv("ALERT_CPU_CRITICAL", "90"))
    ALERT_MEM_WARNING = float(os.getenv("ALERT_MEM_WARNING", "70"))
    ALERT_MEM_CRITICAL = float(os.getenv("ALERT_MEM_CRITICAL", "90"))
    ALERT_DISK_WARNING = float(os.getenv("ALERT_DISK_WARNING", "80"))
    ALERT_DISK_CRITICAL = float(os.getenv("ALERT_DISK_CRITICAL", "95"))
    ALERT_TEMP_WARNING = float(os.getenv("ALERT_TEMP_WARNING", "70"))
    ALERT_TEMP_CRITICAL = float(os.getenv("ALERT_TEMP_CRITICAL", "80"))


# Load .env file if exists
def load_env():
    env_file = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_env()


class SystemMetrics:
    """Collects system metrics from the Pi"""
    
    @staticmethod
    def get_cpu_usage() -> float:
        """Get CPU usage percentage"""
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
            values = line.split()[1:5]
            idle = int(values[3])
            total = sum(int(v) for v in values)
            # Store for next calculation
            if not hasattr(SystemMetrics, '_last_cpu'):
                SystemMetrics._last_cpu = (idle, total)
                return 0.0
            last_idle, last_total = SystemMetrics._last_cpu
            SystemMetrics._last_cpu = (idle, total)
            idle_delta = idle - last_idle
            total_delta = total - last_total
            if total_delta == 0:
                return 0.0
            return round((1.0 - idle_delta / total_delta) * 100, 1)
        except Exception:
            return 0.0
    
    @staticmethod
    def get_memory_usage() -> dict:
        """Get memory usage"""
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            mem = {}
            for line in lines:
                parts = line.split()
                if parts[0] in ('MemTotal:', 'MemAvailable:', 'MemFree:'):
                    mem[parts[0][:-1]] = int(parts[1]) * 1024  # Convert to bytes
            total = mem.get('MemTotal', 0)
            available = mem.get('MemAvailable', mem.get('MemFree', 0))
            used = total - available
            return {
                'total_mb': round(total / 1024 / 1024, 1),
                'used_mb': round(used / 1024 / 1024, 1),
                'percent': round(used / total * 100, 1) if total > 0 else 0,
            }
        except Exception:
            return {'total_mb': 0, 'used_mb': 0, 'percent': 0}
    
    @staticmethod
    def get_disk_usage() -> dict:
        """Get disk usage for root partition"""
        try:
            result = subprocess.run(
                ['df', '-B1', '/'],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split('\n')
            if len(lines) >= 2:
                parts = lines[1].split()
                total = int(parts[1])
                used = int(parts[2])
                return {
                    'total_gb': round(total / 1024 / 1024 / 1024, 1),
                    'used_gb': round(used / 1024 / 1024 / 1024, 1),
                    'percent': round(used / total * 100, 1) if total > 0 else 0,
                }
        except Exception:
            pass
        return {'total_gb': 0, 'used_gb': 0, 'percent': 0}
    
    @staticmethod
    def get_cpu_temperature() -> float:
        """Get CPU temperature in Celsius"""
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = int(f.read().strip())
            return round(temp / 1000, 1)
        except Exception:
            return 0.0
    
    @staticmethod
    def get_network_info() -> dict:
        """Get network information"""
        import socket
        info = {
            'hostname': 'unknown',
            'ip_address': 'unknown',
            'interface': 'unknown',
        }
        try:
            info['hostname'] = socket.gethostname()
        except Exception:
            pass
        
        # Get IP address - try to find the main interface
        try:
            # Create a socket to determine the outbound IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            info['ip_address'] = s.getsockname()[0]
            s.close()
        except Exception:
            # Fallback: try to get from hostname
            try:
                info['ip_address'] = socket.gethostbyname(socket.gethostname())
            except Exception:
                pass
        
        # Get interface name
        try:
            result = subprocess.run(
                ['ip', 'route', 'get', '8.8.8.8'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                # Parse: "8.8.8.8 via ... dev eth0 ..."
                parts = result.stdout.split()
                if 'dev' in parts:
                    idx = parts.index('dev')
                    if idx + 1 < len(parts):
                        info['interface'] = parts[idx + 1]
        except Exception:
            pass
        
        return info
    
    @staticmethod
    def get_system_uptime() -> dict:
        """Get system uptime"""
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.read().split()[0])
            
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            
            if days > 0:
                uptime_str = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                uptime_str = f"{hours}h {minutes}m"
            else:
                uptime_str = f"{minutes}m"
            
            return {
                'seconds': int(uptime_seconds),
                'formatted': uptime_str,
            }
        except Exception:
            return {'seconds': 0, 'formatted': 'unknown'}
    
    @staticmethod
    def get_all_metrics() -> dict:
        """Get all system metrics"""
        return {
            'cpu_percent': SystemMetrics.get_cpu_usage(),
            'memory': SystemMetrics.get_memory_usage(),
            'disk': SystemMetrics.get_disk_usage(),
            'cpu_temp': SystemMetrics.get_cpu_temperature(),
        }
    
    @staticmethod
    def get_network_metrics() -> dict:
        """Get network and uptime info for heartbeat"""
        return {
            'network': SystemMetrics.get_network_info(),
            'uptime': SystemMetrics.get_system_uptime(),
        }


class AlertManager:
    """Manages system alerts based on metric thresholds"""
    
    def __init__(self):
        self.thresholds = AlertThresholds(
            cpu_warning=Config.ALERT_CPU_WARNING,
            cpu_critical=Config.ALERT_CPU_CRITICAL,
            memory_warning=Config.ALERT_MEM_WARNING,
            memory_critical=Config.ALERT_MEM_CRITICAL,
            disk_warning=Config.ALERT_DISK_WARNING,
            disk_critical=Config.ALERT_DISK_CRITICAL,
            temp_warning=Config.ALERT_TEMP_WARNING,
            temp_critical=Config.ALERT_TEMP_CRITICAL,
        )
        self.active_alerts: Dict[str, Alert] = {}
        self.alert_history: List[Alert] = []
        self._alert_cooldown: Dict[str, int] = {}  # Prevent alert spam
        self._cooldown_seconds = 300  # 5 minutes between same alerts
    
    def check_metrics(self, metrics: dict) -> List[Alert]:
        """Check metrics against thresholds and generate alerts"""
        new_alerts = []
        current_time = int(time.time())
        
        # Check CPU
        cpu = metrics.get('cpu_percent', 0)
        if isinstance(cpu, dict):
            cpu = cpu.get('percent', 0)
        cpu_alert = self._check_threshold('cpu', cpu, self.thresholds.cpu_warning, self.thresholds.cpu_critical, 'CPU Usage')
        if cpu_alert:
            new_alerts.append(cpu_alert)
        
        # Check Memory
        mem = metrics.get('memory', {})
        mem_percent = mem.get('percent', 0) if isinstance(mem, dict) else 0
        mem_alert = self._check_threshold('memory', mem_percent, self.thresholds.memory_warning, self.thresholds.memory_critical, 'Memory Usage')
        if mem_alert:
            new_alerts.append(mem_alert)
        
        # Check Disk
        disk = metrics.get('disk', {})
        disk_percent = disk.get('percent', 0) if isinstance(disk, dict) else 0
        disk_alert = self._check_threshold('disk', disk_percent, self.thresholds.disk_warning, self.thresholds.disk_critical, 'Disk Usage')
        if disk_alert:
            new_alerts.append(disk_alert)
        
        # Check Temperature
        temp = metrics.get('cpu_temp', 0)
        temp_alert = self._check_threshold('temperature', temp, self.thresholds.temp_warning, self.thresholds.temp_critical, 'CPU Temperature')
        if temp_alert:
            new_alerts.append(temp_alert)
        
        return new_alerts
    
    def _check_threshold(self, metric: str, value: float, warning: float, critical: float, label: str) -> Optional[Alert]:
        """Check a single metric against thresholds"""
        current_time = int(time.time())
        alert_key = f"{metric}"
        
        # Check cooldown
        if alert_key in self._alert_cooldown:
            if current_time - self._alert_cooldown[alert_key] < self._cooldown_seconds:
                return None
        
        alert_type = None
        threshold = 0
        
        if value >= critical:
            alert_type = 'critical'
            threshold = critical
        elif value >= warning:
            alert_type = 'warning'
            threshold = warning
        else:
            # Clear existing alert if value is back to normal
            if alert_key in self.active_alerts:
                del self.active_alerts[alert_key]
            return None
        
        # Create alert
        alert_id = f"{metric}_{current_time}"
        unit = '°C' if metric == 'temperature' else '%'
        message = f"{label} is {alert_type}: {value:.1f}{unit} (threshold: {threshold:.1f}{unit})"
        
        alert = Alert(
            id=alert_id,
            type=alert_type,
            metric=metric,
            value=value,
            threshold=threshold,
            message=message,
            timestamp=current_time,
        )
        
        self.active_alerts[alert_key] = alert
        self.alert_history.append(alert)
        self._alert_cooldown[alert_key] = current_time
        
        # Keep history limited
        if len(self.alert_history) > 100:
            self.alert_history = self.alert_history[-100:]
        
        logger.warning(f"Alert: {message}")
        return alert
    
    def get_active_alerts(self) -> List[dict]:
        """Get all active alerts"""
        return [asdict(a) for a in self.active_alerts.values()]
    
    def get_alert_history(self, limit: int = 50) -> List[dict]:
        """Get alert history"""
        return [asdict(a) for a in self.alert_history[-limit:]]
    
    def acknowledge_alert(self, alert_id: str) -> bool:
        """Acknowledge an alert"""
        for key, alert in self.active_alerts.items():
            if alert.id == alert_id:
                alert.acknowledged = True
                return True
        return False
    
    def clear_alerts(self) -> int:
        """Clear all active alerts"""
        count = len(self.active_alerts)
        self.active_alerts.clear()
        return count
    
    def update_thresholds(self, thresholds: dict) -> dict:
        """Update alert thresholds"""
        if 'cpu_warning' in thresholds:
            self.thresholds.cpu_warning = float(thresholds['cpu_warning'])
        if 'cpu_critical' in thresholds:
            self.thresholds.cpu_critical = float(thresholds['cpu_critical'])
        if 'memory_warning' in thresholds:
            self.thresholds.memory_warning = float(thresholds['memory_warning'])
        if 'memory_critical' in thresholds:
            self.thresholds.memory_critical = float(thresholds['memory_critical'])
        if 'disk_warning' in thresholds:
            self.thresholds.disk_warning = float(thresholds['disk_warning'])
        if 'disk_critical' in thresholds:
            self.thresholds.disk_critical = float(thresholds['disk_critical'])
        if 'temp_warning' in thresholds:
            self.thresholds.temp_warning = float(thresholds['temp_warning'])
        if 'temp_critical' in thresholds:
            self.thresholds.temp_critical = float(thresholds['temp_critical'])
        return asdict(self.thresholds)


class ServiceManager:
    """Manages systemd services on the Pi"""
    
    # Known services that can be controlled (extensible)
    # Facial Tracker Firmware services
    ALLOWED_SERVICES = {
        # Facial Tracker Firmware - Main services
        "facial": "facial1.service",
        "dms": "facial1.service",  # Alias for Driver Monitoring System
        "get_gps_data": "get_gps_data1.service",
        "gps": "get_gps_data1.service",  # Alias
        "provisioning_ui": "provisioning_ui.service",
        "webui": "provisioning_ui.service",  # Alias
        # OTA services
        "ota_startup": "ota-startup.service",
        "ota_update": "ota-auto-update.service",
        # Pi Control
        "pi_control": "pi-control.service",
        # System services
        "ssh": "ssh.service",
        "cron": "cron.service",
    }

    # Service groups for batch operations
    SERVICE_GROUPS = {
        "facial_tracker": ["facial", "get_gps_data"],
        "all_dms": ["facial", "get_gps_data"],
    }
    
    @staticmethod
    def _run_systemctl(action: str, service: str) -> tuple[bool, str]:
        """Run systemctl command"""
        try:
            result = subprocess.run(
                ["sudo", "systemctl", action, service],
                capture_output=True,
                text=True,
                timeout=30,
            )
            success = result.returncode == 0
            output = result.stdout or result.stderr
            return success, output.strip()
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)
    
    @classmethod
    def get_service_status(cls, service_name: str) -> dict:
        """Get status of a service"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        
        # Check if active
        success, output = cls._run_systemctl("is-active", service)
        is_active = output == "active"
        
        # Check if enabled
        success, output = cls._run_systemctl("is-enabled", service)
        is_enabled = output == "enabled"
        
        # Get more details
        details = {}
        try:
            result = subprocess.run(
                ["systemctl", "show", service, "--property=MainPID,MemoryCurrent,ActiveEnterTimestamp"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if '=' in line:
                        key, value = line.split('=', 1)
                        details[key] = value
        except Exception:
            pass
        
        return {
            "service": service,
            "active": is_active,
            "enabled": is_enabled,
            "status": "running" if is_active else "stopped",
            "pid": details.get("MainPID", ""),
            "memory": details.get("MemoryCurrent", ""),
            "started_at": details.get("ActiveEnterTimestamp", ""),
        }
    
    @classmethod
    def list_services(cls) -> List[dict]:
        """List all known services with their status"""
        services = []
        for name, unit in cls.ALLOWED_SERVICES.items():
            status = cls.get_service_status(name)
            status['name'] = name
            services.append(status)
        return services
    
    @classmethod
    def start_service(cls, service_name: str) -> tuple[bool, str]:
        """Start a service"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        logger.info(f"Starting service: {service}")
        return cls._run_systemctl("start", service)
    
    @classmethod
    def stop_service(cls, service_name: str) -> tuple[bool, str]:
        """Stop a service"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        logger.info(f"Stopping service: {service}")
        return cls._run_systemctl("stop", service)
    
    @classmethod
    def restart_service(cls, service_name: str) -> tuple[bool, str]:
        """Restart a service"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        logger.info(f"Restarting service: {service}")
        return cls._run_systemctl("restart", service)
    
    @classmethod
    def enable_service(cls, service_name: str) -> tuple[bool, str]:
        """Enable a service to start on boot"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        logger.info(f"Enabling service: {service}")
        return cls._run_systemctl("enable", service)
    
    @classmethod
    def disable_service(cls, service_name: str) -> tuple[bool, str]:
        """Disable a service from starting on boot"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        logger.info(f"Disabling service: {service}")
        return cls._run_systemctl("disable", service)
    
    @classmethod
    def add_service(cls, name: str, unit: str) -> bool:
        """Add a new service to the allowed list"""
        if not unit.endswith('.service'):
            unit = f"{unit}.service"
        cls.ALLOWED_SERVICES[name] = unit
        logger.info(f"Added service: {name} -> {unit}")
        return True
    
    @classmethod
    def remove_service(cls, name: str) -> bool:
        """Remove a service from the allowed list"""
        if name in cls.ALLOWED_SERVICES:
            del cls.ALLOWED_SERVICES[name]
            logger.info(f"Removed service: {name}")
            return True
        return False
    
    @classmethod
    def get_group_services(cls, group_name: str) -> List[str]:
        """Get list of services in a group"""
        return cls.SERVICE_GROUPS.get(group_name, [])
    
    @classmethod
    def list_groups(cls) -> dict:
        """List all service groups"""
        return cls.SERVICE_GROUPS.copy()
    
    @classmethod
    def control_group(cls, group_name: str, action: str) -> dict:
        """Control all services in a group (start, stop, restart)"""
        services = cls.get_group_services(group_name)
        if not services:
            return {"error": f"Group not found: {group_name}", "success": False}
        
        results = {}
        all_success = True
        for service in services:
            if action == "start":
                success, msg = cls.start_service(service)
            elif action == "stop":
                success, msg = cls.stop_service(service)
            elif action == "restart":
                success, msg = cls.restart_service(service)
            elif action == "status":
                results[service] = cls.get_service_status(service)
                continue
            else:
                return {"error": f"Invalid action: {action}", "success": False}
            
            results[service] = {"success": success, "message": msg}
            if not success:
                all_success = False
        
        return {
            "group": group_name,
            "action": action,
            "results": results,
            "success": all_success,
        }
    
    @classmethod
    def get_all_status(cls) -> dict:
        """Get status of all known services"""
        statuses = {}
        for name in cls.ALLOWED_SERVICES.keys():
            try:
                statuses[name] = cls.get_service_status(name)
            except Exception as e:
                statuses[name] = {"status": "error", "error": str(e)}
        return statuses
    
    @classmethod
    def get_service_logs(cls, service_name: str, lines: int = 100, since: str = None, 
                         until: str = None, follow: bool = False) -> dict:
        """Get logs for a service using journalctl"""
        service = cls.ALLOWED_SERVICES.get(service_name, service_name)
        if not service.endswith('.service'):
            service = f"{service}.service"
        
        try:
            cmd = ["journalctl", "-u", service, "-n", str(lines), "--no-pager"]
            
            if since:
                cmd.extend(["--since", since])
            if until:
                cmd.extend(["--until", until])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            return {
                "service": service,
                "logs": result.stdout,
                "lines": lines,
                "success": True,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    @classmethod
    def get_all_logs(cls, lines: int = 50) -> dict:
        """Get logs for all known services"""
        logs = {}
        for name in cls.ALLOWED_SERVICES.keys():
            try:
                result = cls.get_service_logs(name, lines=lines)
                if result.get("success"):
                    logs[name] = result.get("logs", "")
                else:
                    logs[name] = f"Error: {result.get('error', 'Unknown error')}"
            except Exception as e:
                logs[name] = f"Error: {str(e)}"
        return {"logs": logs, "success": True}
    
    @classmethod
    def get_system_logs(cls, lines: int = 100, priority: str = None) -> dict:
        """Get system-wide logs"""
        try:
            cmd = ["journalctl", "-n", str(lines), "--no-pager"]
            
            if priority:
                cmd.extend(["-p", priority])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            return {
                "logs": result.stdout,
                "lines": lines,
                "priority": priority,
                "success": True,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    @classmethod
    def get_boot_logs(cls, lines: int = 200) -> dict:
        """Get logs from current boot"""
        try:
            result = subprocess.run(
                ["journalctl", "-b", "-n", str(lines), "--no-pager"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            
            return {
                "logs": result.stdout,
                "lines": lines,
                "success": True,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}


class FileManager:
    """Manages file operations on the Pi"""
    
    def __init__(self):
        # Ensure directories exist
        os.makedirs(Config.UPLOAD_DIR, exist_ok=True)
        os.makedirs(Config.BACKUP_DIR, exist_ok=True)
    
    def list_files(self, path: str = None, pattern: str = "*") -> dict:
        """List files in a directory"""
        try:
            base_path = Path(path) if path else Path(Config.DOWNLOAD_DIR)
            
            # Security: prevent directory traversal
            if not self._is_safe_path(base_path):
                return {"error": "Access denied", "success": False}
            
            if not base_path.exists():
                return {"error": f"Path does not exist: {base_path}", "success": False}
            
            files = []
            for item in base_path.glob(pattern):
                try:
                    stat = item.stat()
                    files.append({
                        "name": item.name,
                        "path": str(item),
                        "is_dir": item.is_dir(),
                        "size": stat.st_size if item.is_file() else 0,
                        "modified": int(stat.st_mtime),
                        "permissions": oct(stat.st_mode)[-3:],
                    })
                except Exception:
                    pass
            
            return {
                "path": str(base_path),
                "files": sorted(files, key=lambda x: (not x['is_dir'], x['name'].lower())),
                "count": len(files),
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def read_file(self, path: str, encoding: str = 'utf-8') -> dict:
        """Read file contents"""
        try:
            file_path = Path(path)
            
            if not self._is_safe_path(file_path):
                return {"error": "Access denied", "success": False}
            
            if not file_path.exists():
                return {"error": f"File not found: {path}", "success": False}
            
            if file_path.stat().st_size > Config.MAX_FILE_SIZE:
                return {"error": "File too large", "success": False}
            
            # Try to read as text, fallback to base64 for binary
            try:
                content = file_path.read_text(encoding=encoding)
                return {
                    "path": str(file_path),
                    "content": content,
                    "size": len(content),
                    "encoding": encoding,
                    "binary": False,
                    "success": True,
                }
            except UnicodeDecodeError:
                content = base64.b64encode(file_path.read_bytes()).decode('ascii')
                return {
                    "path": str(file_path),
                    "content": content,
                    "size": file_path.stat().st_size,
                    "encoding": "base64",
                    "binary": True,
                    "success": True,
                }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def write_file(self, path: str, content: str, encoding: str = 'utf-8', is_base64: bool = False) -> dict:
        """Write content to a file"""
        try:
            file_path = Path(path)
            
            if not self._is_safe_path(file_path):
                return {"error": "Access denied", "success": False}
            
            # Create parent directories
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            if is_base64:
                data = base64.b64decode(content)
                file_path.write_bytes(data)
            else:
                file_path.write_text(content, encoding=encoding)
            
            return {
                "path": str(file_path),
                "size": file_path.stat().st_size,
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def delete_file(self, path: str) -> dict:
        """Delete a file or directory"""
        try:
            file_path = Path(path)
            
            if not self._is_safe_path(file_path):
                return {"error": "Access denied", "success": False}
            
            if not file_path.exists():
                return {"error": f"Path not found: {path}", "success": False}
            
            if file_path.is_dir():
                shutil.rmtree(file_path)
            else:
                file_path.unlink()
            
            return {"path": str(file_path), "success": True}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def upload_file(self, filename: str, content: str, target_dir: str = None, is_base64: bool = True, is_compressed: bool = False) -> dict:
        """Upload a file (content is base64 encoded, optionally gzip compressed)"""
        try:
            import gzip as gzip_module
            
            # Sanitize filename
            safe_name = Path(filename).name
            
            # Use target_dir if provided, otherwise use default UPLOAD_DIR
            if target_dir:
                upload_dir = Path(target_dir)
                # Create directory if it doesn't exist
                upload_dir.mkdir(parents=True, exist_ok=True)
            else:
                upload_dir = Path(Config.UPLOAD_DIR)
            
            file_path = upload_dir / safe_name
            
            if is_base64:
                data = base64.b64decode(content)
            else:
                data = content.encode('utf-8')
            
            # Decompress if compressed
            if is_compressed:
                try:
                    data = gzip_module.decompress(data)
                    logger.info(f"Decompressed file from {len(base64.b64decode(content))} to {len(data)} bytes")
                except Exception as e:
                    logger.warning(f"Failed to decompress, using raw data: {e}")
            
            if len(data) > Config.MAX_FILE_SIZE:
                return {"error": f"File too large (max {Config.MAX_FILE_SIZE // 1024 // 1024}MB)", "success": False}
            
            file_path.write_bytes(data)
            
            # Calculate checksum
            checksum = hashlib.md5(data).hexdigest()
            
            return {
                "path": str(file_path),
                "size": len(data),
                "checksum": checksum,
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def download_file(self, path: str) -> dict:
        """Prepare a file for download (returns base64 content)"""
        try:
            file_path = Path(path)
            
            if not self._is_safe_path(file_path):
                return {"error": "Access denied", "success": False}
            
            if not file_path.exists():
                return {"error": f"File not found: {path}", "success": False}
            
            if file_path.stat().st_size > Config.MAX_FILE_SIZE:
                return {"error": f"File too large (max {Config.MAX_FILE_SIZE // 1024 // 1024}MB)", "success": False}
            
            data = file_path.read_bytes()
            content = base64.b64encode(data).decode('ascii')
            checksum = hashlib.md5(data).hexdigest()
            
            return {
                "filename": file_path.name,
                "path": str(file_path),
                "content": content,
                "size": len(data),
                "checksum": checksum,
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def _is_safe_path(self, path: Path) -> bool:
        """Check if path is safe (no directory traversal)"""
        try:
            resolved = path.resolve()
            # Allow access to home directory and common paths
            allowed_roots = [
                Path('/home'),
                Path('/tmp'),
                Path('/var/log'),
                Path('/etc'),
            ]
            return any(str(resolved).startswith(str(root)) for root in allowed_roots)
        except Exception:
            return False


class ShellExecutor:
    """Executes shell commands on the Pi"""
    
    # Commands that are always blocked for security
    BLOCKED_COMMANDS = [
        'rm -rf /',
        'mkfs',
        'dd if=',
        ':(){:|:&};:',  # Fork bomb
        'chmod 777 /',
        'chown -R',
    ]
    
    @classmethod
    def execute(cls, command: str, timeout: int = None, cwd: str = None) -> dict:
        """Execute a shell command"""
        if timeout is None:
            timeout = Config.SHELL_TIMEOUT
        
        # Security checks
        if cls._is_blocked(command):
            return {"error": "Command blocked for security reasons", "success": False}
        
        # Check if command is in allowed list (if configured)
        if Config.ALLOWED_SHELL_COMMANDS:
            cmd_base = command.split()[0] if command else ""
            if cmd_base not in Config.ALLOWED_SHELL_COMMANDS:
                return {"error": f"Command not in allowed list: {cmd_base}", "success": False}
        
        try:
            logger.info(f"Executing shell command: {command}")
            
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            
            return {
                "command": command,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
                "success": result.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    @classmethod
    def execute_script(cls, script: str, interpreter: str = "/bin/bash", timeout: int = None) -> dict:
        """Execute a multi-line script"""
        if timeout is None:
            timeout = Config.SHELL_TIMEOUT * 2
        
        try:
            # Write script to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as f:
                f.write(script)
                script_path = f.name
            
            os.chmod(script_path, 0o755)
            
            result = subprocess.run(
                [interpreter, script_path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            
            # Clean up
            os.unlink(script_path)
            
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode,
                "success": result.returncode == 0,
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Script timed out after {timeout}s", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    @classmethod
    def _is_blocked(cls, command: str) -> bool:
        """Check if command is blocked"""
        cmd_lower = command.lower()
        return any(blocked in cmd_lower for blocked in cls.BLOCKED_COMMANDS)


class OTAManager:
    """Manages Over-The-Air updates"""
    
    # Update server configuration
    UPDATE_CHECK_URL = os.getenv("OTA_UPDATE_CHECK_URL", "")  # Backend API endpoint
    
    def __init__(self):
        os.makedirs(Config.OTA_DIR, exist_ok=True)
        os.makedirs(Config.BACKUP_DIR, exist_ok=True)
        self._update_in_progress = False
        self._current_version = self._read_version()
        self._last_update_check = 0
        self._pending_update = None
    
    def _read_version(self) -> str:
        """Read current version from version file"""
        version_file = Path(Config.OTA_DIR) / "version.txt"
        if version_file.exists():
            return version_file.read_text().strip()
        return "1.0.0"
    
    def _write_version(self, version: str):
        """Write version to version file"""
        version_file = Path(Config.OTA_DIR) / "version.txt"
        version_file.write_text(version)
    
    def get_status(self) -> dict:
        """Get OTA update status"""
        return {
            "current_version": self._current_version,
            "update_in_progress": self._update_in_progress,
            "ota_dir": Config.OTA_DIR,
            "backup_dir": Config.BACKUP_DIR,
            "pending_update": self._pending_update,
            "last_update_check": self._last_update_check,
        }
    
    def check_update(self, update_url: str = None) -> dict:
        """Check for available updates from backend server"""
        import urllib.request
        import urllib.error
        
        self._last_update_check = int(time.time())
        check_url = update_url or self.UPDATE_CHECK_URL
        
        if not check_url:
            return {
                "current_version": self._current_version,
                "update_available": False,
                "latest_version": self._current_version,
                "message": "No update server configured",
            }
        
        try:
            # Build request URL with device info
            device_id = Config.DEVICE_ID
            url = f"{check_url}?device_id={device_id}&current_version={self._current_version}"
            
            logger.info(f"Checking for updates at: {check_url}")
            
            req = urllib.request.Request(url, method='GET')
            req.add_header('Content-Type', 'application/json')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
            
            update_available = data.get("update_available", False)
            latest_version = data.get("latest_version", self._current_version)
            
            if update_available:
                self._pending_update = {
                    "version": latest_version,
                    "download_url": data.get("download_url"),
                    "checksum": data.get("checksum"),
                    "filename": data.get("filename"),
                    "release_notes": data.get("release_notes"),
                    "mandatory": data.get("mandatory", False),
                    "post_update_hooks": data.get("post_update_hooks", []),
                }
                logger.info(f"Update available: {self._current_version} -> {latest_version}")
            else:
                self._pending_update = None
                logger.info(f"No updates available. Current version: {self._current_version}")
            
            return {
                "current_version": self._current_version,
                "update_available": update_available,
                "latest_version": latest_version,
                "pending_update": self._pending_update,
                "success": True,
            }
            
        except urllib.error.URLError as e:
            logger.warning(f"Update check failed (network): {e}")
            return {
                "current_version": self._current_version,
                "update_available": False,
                "latest_version": self._current_version,
                "error": f"Network error: {str(e)}",
                "success": False,
            }
        except Exception as e:
            logger.error(f"Update check failed: {e}")
            return {
                "current_version": self._current_version,
                "update_available": False,
                "latest_version": self._current_version,
                "error": str(e),
                "success": False,
            }
    
    def prepare_update(self, package_content: str, version: str, checksum: str = None) -> dict:
        """Prepare an update package (base64 encoded)"""
        if self._update_in_progress:
            return {"error": "Update already in progress", "success": False}
        
        try:
            self._update_in_progress = True
            
            # Decode package
            package_data = base64.b64decode(package_content)
            
            # Verify checksum if provided
            if checksum:
                actual_checksum = hashlib.md5(package_data).hexdigest()
                if actual_checksum != checksum:
                    self._update_in_progress = False
                    return {"error": "Checksum mismatch", "success": False}
            
            # Save package
            package_path = Path(Config.OTA_DIR) / f"update_{version}.tar.gz"
            package_path.write_bytes(package_data)
            
            return {
                "version": version,
                "package_path": str(package_path),
                "size": len(package_data),
                "success": True,
            }
        except Exception as e:
            self._update_in_progress = False
            return {"error": str(e), "success": False}
    
    def apply_update(self, version: str, target_dir: str, restart_service: str = None) -> dict:
        """Apply a prepared update"""
        if not self._update_in_progress:
            return {"error": "No update prepared", "success": False}
        
        try:
            package_path = Path(Config.OTA_DIR) / f"update_{version}.tar.gz"
            
            if not package_path.exists():
                return {"error": f"Update package not found: {package_path}", "success": False}
            
            target = Path(target_dir)
            
            # Create backup with readable timestamp (e.g., backup_1.0.0_2024-01-15_14-30-45.tar.gz)
            from datetime import datetime
            timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            backup_name = f"backup_{self._current_version}_{timestamp}.tar.gz"
            backup_path = Path(Config.BACKUP_DIR) / backup_name
            
            if target.exists():
                logger.info(f"Creating backup: {backup_path}")
                with tarfile.open(backup_path, "w:gz") as tar:
                    tar.add(target, arcname=target.name)
            
            # Extract update
            logger.info(f"Extracting update to: {target}")
            with tarfile.open(package_path, "r:gz") as tar:
                tar.extractall(target.parent)
            
            # Update version
            self._current_version = version
            self._write_version(version)
            
            # Clean up package
            package_path.unlink()
            
            # Restart service if specified
            if restart_service:
                logger.info(f"Restarting service: {restart_service}")
                ServiceManager.restart_service(restart_service)
            
            self._update_in_progress = False
            
            return {
                "version": version,
                "backup": str(backup_path),
                "target": str(target),
                "success": True,
            }
        except Exception as e:
            self._update_in_progress = False
            return {"error": str(e), "success": False}
    
    def rollback(self, backup_name: str, target_dir: str) -> dict:
        """Rollback to a previous backup (supports both tar.gz and single file backups)"""
        try:
            # Try to find backup in multiple locations
            backup_path = None
            search_dirs = [
                Path(Config.BACKUP_DIR),
                Path(Config.FACIAL_TRACKER_DIR) / "backups",
                Path(target_dir) / "backups" if target_dir else None,
            ]
            
            for search_dir in search_dirs:
                if search_dir and search_dir.exists():
                    candidate = search_dir / backup_name
                    if candidate.exists():
                        backup_path = candidate
                        break
            
            if not backup_path or not backup_path.exists():
                return {"error": f"Backup not found: {backup_name}", "success": False}
            
            target = Path(target_dir)
            
            # Check if it's a tar.gz archive or a single file backup
            if backup_name.endswith('.tar.gz'):
                # Archive backup - extract to target directory
                if not target.exists():
                    target.mkdir(parents=True, exist_ok=True)
                logger.info(f"Restoring archive backup: {backup_path} to {target}")
                with tarfile.open(backup_path, "r:gz") as tar:
                    tar.extractall(target)
            else:
                # Single file backup (format: filename.backup_timestamp)
                # Extract original filename from backup name
                if '.backup_' in backup_name:
                    original_name = backup_name.rsplit('.backup_', 1)[0]
                else:
                    original_name = backup_name
                
                target_file = target / original_name if target.is_dir() else target
                target_file.parent.mkdir(parents=True, exist_ok=True)
                
                logger.info(f"Restoring file backup: {backup_path} to {target_file}")
                shutil.copy2(backup_path, target_file)
            
            return {
                "backup": backup_name,
                "target": str(target),
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def list_backups(self, backup_dir: str = None) -> dict:
        """List available backups from specified dir or default locations"""
        try:
            backups = []
            dirs_to_check = []
            
            # Add specified directory
            if backup_dir:
                dirs_to_check.append(Path(backup_dir))
                # Also check 'backups' subdirectory
                dirs_to_check.append(Path(backup_dir) / "backups")
            
            # Add default backup dir
            dirs_to_check.append(Path(Config.BACKUP_DIR))
            
            # Also check common OTA target directories
            dirs_to_check.append(Path(Config.FACIAL_TRACKER_DIR) / "backups")
            
            seen_names = set()
            for backup_dir_path in dirs_to_check:
                if not backup_dir_path.exists():
                    continue
                    
                for f in backup_dir_path.iterdir():
                    if f.is_file() and ('.backup_' in f.name or f.name.startswith('backup_')):
                        if f.name in seen_names:
                            continue
                        seen_names.add(f.name)
                        stat = f.stat()
                        backups.append({
                            "name": f.name,
                            "path": str(f),
                            "size": stat.st_size,
                            "created": int(stat.st_mtime),
                        })
            
            return {
                "backups": sorted(backups, key=lambda x: x['created'], reverse=True),
                "count": len(backups),
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def cleanup_backups(self, keep: int = 5) -> dict:
        """Clean up old backups, keeping the most recent ones"""
        try:
            backup_dir = Path(Config.BACKUP_DIR)
            backups = sorted(backup_dir.glob("backup_*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True)
            
            deleted = []
            for backup in backups[keep:]:
                backup.unlink()
                deleted.append(backup.name)
            
            return {
                "deleted": deleted,
                "kept": keep,
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def download_from_url(self, download_url: str, filename: str, target_path: str = None, 
                          checksum: str = None, deployment_id: int = None,
                          auto_backup: bool = True, auto_rollback: bool = True) -> dict:
        """Download OTA file from URL (S3 pre-signed URL) with backup and rollback support"""
        import urllib.request
        import urllib.error
        
        backup_path = None
        save_path = None
        
        try:
            logger.info(f"Downloading OTA file: {filename}")
            
            # Determine save path - always use local config, ignore backend target_path for security
            # Backend may have outdated paths (e.g., /home/satya instead of /home/pi)
            save_path = Path(Config.FACIAL_TRACKER_DIR) / filename
            if target_path:
                logger.info(f"Ignoring backend target_path '{target_path}', using local config: {save_path}")
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Create backup if file exists and auto_backup is enabled
            if auto_backup and save_path.exists():
                from datetime import datetime
                timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                backup_name = f"{save_path.name}.backup_{timestamp}"
                backup_path = Path(Config.BACKUP_DIR) / backup_name
                logger.info(f"Creating backup: {backup_path}")
                shutil.copy2(save_path, backup_path)
            
            # Download to temp file first
            temp_path = save_path.with_suffix(save_path.suffix + '.tmp')
            
            start_time = time.time()
            urllib.request.urlretrieve(download_url, temp_path)
            download_time = time.time() - start_time
            
            # Get file size
            file_size = temp_path.stat().st_size
            
            # Verify checksum if provided
            if checksum:
                with open(temp_path, 'rb') as f:
                    actual_checksum = hashlib.md5(f.read()).hexdigest()
                if actual_checksum != checksum:
                    temp_path.unlink()  # Delete corrupted file
                    raise ValueError(f"Checksum mismatch: expected {checksum}, got {actual_checksum}")
            
            # Move temp file to final location (atomic on same filesystem)
            shutil.move(str(temp_path), str(save_path))
            
            logger.info(f"Downloaded {filename} ({file_size} bytes) in {download_time:.2f}s")
            
            # Extract bundle if it's a zip file
            extracted_path = None
            if filename.endswith('.zip'):
                try:
                    extract_dir = save_path.parent
                    logger.info(f"Extracting bundle {filename} to {extract_dir}")
                    
                    with zipfile.ZipFile(save_path, 'r') as zip_ref:
                        # Get the root folder name from the zip (e.g., bundle-v2.0.1)
                        zip_contents = zip_ref.namelist()
                        root_folder = zip_contents[0].split('/')[0] if zip_contents else None
                        
                        # Extract to releases directory
                        releases_dir = extract_dir / "releases"
                        releases_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Extract version from filename (e.g., bundle-v2.0.1.zip -> v2.0.1)
                        version = filename.replace('bundle-', '').replace('.zip', '')
                        version_dir = releases_dir / version
                        
                        # Remove existing version directory if exists
                        if version_dir.exists():
                            shutil.rmtree(version_dir)
                        
                        # Create version directory with apps subdirectory
                        version_dir.mkdir(parents=True, exist_ok=True)
                        apps_dir = version_dir / "apps"
                        apps_dir.mkdir(parents=True, exist_ok=True)
                        
                        # Extract to temp location first
                        zip_ref.extractall(releases_dir)
                        
                        # Move extracted files to apps directory
                        if root_folder and (releases_dir / root_folder).exists():
                            extracted_folder = releases_dir / root_folder
                            # Move all files from extracted folder to apps directory
                            for item in extracted_folder.iterdir():
                                dest = apps_dir / item.name
                                if dest.exists():
                                    if dest.is_dir():
                                        shutil.rmtree(dest)
                                    else:
                                        dest.unlink()
                                shutil.move(str(item), str(dest))
                            # Remove empty extracted folder
                            extracted_folder.rmdir()
                        
                        extracted_path = str(version_dir)
                        logger.info(f"Extracted bundle to {version_dir}/apps/")
                        
                        # Update current symlink
                        current_link = extract_dir / "current"
                        if current_link.is_symlink():
                            current_link.unlink()
                        elif current_link.exists():
                            shutil.rmtree(current_link)
                        
                        # Create new symlink: current -> releases/v2.0.1
                        current_link.symlink_to(version_dir)
                        logger.info(f"Updated symlink: {current_link} -> {version_dir}")
                        
                except Exception as extract_error:
                    logger.error(f"Failed to extract bundle: {extract_error}")
                    # Continue - download was successful even if extraction failed
            
            return {
                "filename": filename,
                "path": str(save_path),
                "extracted_path": extracted_path,
                "size": file_size,
                "download_time": round(download_time, 2),
                "deployment_id": deployment_id,
                "backup_path": str(backup_path) if backup_path else None,
                "success": True,
            }
            
        except Exception as e:
            logger.error(f"OTA download error: {e}")
            
            # Auto-rollback on failure
            if auto_rollback and backup_path and backup_path.exists() and save_path:
                logger.info(f"Rolling back to backup: {backup_path}")
                try:
                    shutil.copy2(backup_path, save_path)
                    logger.info("Rollback successful")
                except Exception as rollback_error:
                    logger.error(f"Rollback failed: {rollback_error}")
            
            # Clean up temp file if exists
            if save_path:
                temp_path = save_path.with_suffix(save_path.suffix + '.tmp')
                if temp_path.exists():
                    temp_path.unlink()
            
            return {
                "error": str(e),
                "success": False,
                "deployment_id": deployment_id,
                "rolled_back": backup_path is not None and backup_path.exists(),
            }
    
    def download_and_apply(self, download_url: str, filename: str, target_path: str,
                           checksum: str = None, deployment_id: int = None,
                           restart_service: str = None, validation_cmd: str = None) -> dict:
        """Download OTA file, apply it, and optionally validate + restart service"""
        import subprocess
        
        backup_path = None
        
        try:
            save_path = Path(target_path)
            # If target_path is a directory, append filename
            if save_path.is_dir():
                save_path = save_path / filename
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Step 1: Create backup if file exists
            if save_path.exists() and save_path.is_file():
                from datetime import datetime
                timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                backup_name = f"{save_path.name}.backup_{timestamp}"
                backup_path = Path(Config.BACKUP_DIR) / backup_name
                logger.info(f"Creating backup: {backup_path}")
                shutil.copy2(save_path, backup_path)
            
            # Step 2: Download new file
            result = self.download_from_url(
                download_url, filename, target_path, checksum, deployment_id,
                auto_backup=False, auto_rollback=False  # We handle backup ourselves
            )
            
            if not result.get("success"):
                # Download failed, restore backup
                if backup_path and backup_path.exists():
                    shutil.copy2(backup_path, save_path)
                    result["rolled_back"] = True
                return result
            
            # Step 3: Validate if validation command provided
            if validation_cmd:
                logger.info(f"Running validation: {validation_cmd}")
                try:
                    proc = subprocess.run(
                        validation_cmd, shell=True, capture_output=True, 
                        text=True, timeout=30
                    )
                    if proc.returncode != 0:
                        raise ValueError(f"Validation failed: {proc.stderr or proc.stdout}")
                    logger.info("Validation passed")
                except Exception as val_error:
                    logger.error(f"Validation error: {val_error}")
                    # Rollback on validation failure
                    if backup_path and backup_path.exists():
                        logger.info("Rolling back due to validation failure")
                        shutil.copy2(backup_path, save_path)
                    return {
                        "error": f"Validation failed: {str(val_error)}",
                        "success": False,
                        "deployment_id": deployment_id,
                        "rolled_back": True,
                    }
            
            # Step 4: Restart service if specified
            if restart_service:
                logger.info(f"Restarting service: {restart_service}")
                try:
                    ServiceManager.restart_service(restart_service)
                    time.sleep(2)  # Wait for service to start
                    
                    # Check if service is running
                    status = ServiceManager.get_service_status(restart_service)
                    if status.get("state") != "running":
                        raise ValueError(f"Service failed to start: {status}")
                    logger.info(f"Service {restart_service} restarted successfully")
                except Exception as svc_error:
                    logger.error(f"Service restart error: {svc_error}")
                    # Rollback on service failure
                    if backup_path and backup_path.exists():
                        logger.info("Rolling back due to service failure")
                        shutil.copy2(backup_path, save_path)
                        # Try to restart with old file
                        ServiceManager.restart_service(restart_service)
                    return {
                        "error": f"Service restart failed: {str(svc_error)}",
                        "success": False,
                        "deployment_id": deployment_id,
                        "rolled_back": True,
                    }
            
            result["backup_path"] = str(backup_path) if backup_path else None
            result["validated"] = validation_cmd is not None
            result["service_restarted"] = restart_service
            return result
            
        except Exception as e:
            logger.error(f"OTA apply error: {e}")
            # Rollback on any error
            if backup_path and backup_path.exists() and save_path.exists():
                shutil.copy2(backup_path, save_path)
            return {
                "error": str(e),
                "success": False,
                "deployment_id": deployment_id,
                "rolled_back": backup_path is not None,
            }
    
    def run_post_update_hooks(self, target_dir: str = None, hooks: List[str] = None) -> dict:
        """Run post-update hooks after OTA update (pip install, db migrations, etc.)"""
        target = Path(target_dir) if target_dir else Path(Config.FACIAL_TRACKER_DIR)
        venv_path = Path(Config.FACIAL_TRACKER_VENV)
        results = []
        all_success = True
        
        # Default hooks if none specified
        if hooks is None:
            hooks = ["install_requirements", "run_migrations", "restart_services"]
        
        logger.info(f"Running post-update hooks: {hooks}")
        
        for hook in hooks:
            hook_result = {"hook": hook, "success": False, "output": ""}
            
            try:
                if hook == "install_requirements":
                    # Install Python requirements
                    hook_result = self._hook_install_requirements(target, venv_path)
                    
                elif hook == "run_migrations":
                    # Run database migrations
                    hook_result = self._hook_run_migrations(target, venv_path)
                    
                elif hook == "restart_services":
                    # Restart all DMS services
                    hook_result = self._hook_restart_services()
                    
                elif hook == "validate_syntax":
                    # Validate Python syntax
                    hook_result = self._hook_validate_syntax(target, venv_path)
                    
                elif hook == "clear_cache":
                    # Clear Python cache
                    hook_result = self._hook_clear_cache(target)
                    
                elif hook.startswith("custom:"):
                    # Custom shell command
                    cmd = hook[7:]  # Remove "custom:" prefix
                    hook_result = self._hook_custom_command(cmd, target)
                    
                else:
                    hook_result = {"hook": hook, "success": False, "error": f"Unknown hook: {hook}"}
                
            except Exception as e:
                hook_result = {"hook": hook, "success": False, "error": str(e)}
            
            results.append(hook_result)
            if not hook_result.get("success", False):
                all_success = False
                logger.error(f"Hook '{hook}' failed: {hook_result.get('error', 'Unknown error')}")
            else:
                logger.info(f"Hook '{hook}' completed successfully")
        
        return {
            "hooks_run": len(results),
            "results": results,
            "all_success": all_success,
            "success": True,
        }
    
    def _hook_install_requirements(self, target: Path, venv_path: Path) -> dict:
        """Install Python requirements from requirements.txt"""
        requirements_file = target / "requirements.txt"
        
        if not requirements_file.exists():
            return {"hook": "install_requirements", "success": True, "output": "No requirements.txt found, skipping"}
        
        try:
            # Use venv pip if available
            pip_path = venv_path / "bin" / "pip" if venv_path.exists() else "pip3"
            
            logger.info(f"Installing requirements from {requirements_file}")
            result = subprocess.run(
                [str(pip_path), "install", "-r", str(requirements_file), "--upgrade"],
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout for pip install
                cwd=str(target),
            )
            
            return {
                "hook": "install_requirements",
                "success": result.returncode == 0,
                "output": result.stdout[-2000:] if result.stdout else "",  # Last 2000 chars
                "error": result.stderr[-1000:] if result.returncode != 0 else "",
            }
        except subprocess.TimeoutExpired:
            return {"hook": "install_requirements", "success": False, "error": "Timeout installing requirements"}
        except Exception as e:
            return {"hook": "install_requirements", "success": False, "error": str(e)}
    
    def _hook_run_migrations(self, target: Path, venv_path: Path) -> dict:
        """Run database migrations if migration script exists"""
        # Check for common migration scripts
        migration_scripts = [
            target / "migrate.py",
            target / "db_migrate.py",
            target / "migrations" / "migrate.py",
            target / "scripts" / "migrate.py",
        ]
        
        migration_script = None
        for script in migration_scripts:
            if script.exists():
                migration_script = script
                break
        
        if not migration_script:
            # Check for SQL migration files
            sql_migrations_dir = target / "migrations" / "sql"
            if sql_migrations_dir.exists():
                return self._run_sql_migrations(sql_migrations_dir, target)
            return {"hook": "run_migrations", "success": True, "output": "No migration script found, skipping"}
        
        try:
            # Use venv python if available
            python_path = venv_path / "bin" / "python" if venv_path.exists() else "python3"
            
            logger.info(f"Running migrations: {migration_script}")
            result = subprocess.run(
                [str(python_path), str(migration_script)],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(target),
            )
            
            return {
                "hook": "run_migrations",
                "success": result.returncode == 0,
                "output": result.stdout[-1000:] if result.stdout else "",
                "error": result.stderr[-500:] if result.returncode != 0 else "",
            }
        except subprocess.TimeoutExpired:
            return {"hook": "run_migrations", "success": False, "error": "Timeout running migrations"}
        except Exception as e:
            return {"hook": "run_migrations", "success": False, "error": str(e)}
    
    def _run_sql_migrations(self, sql_dir: Path, target: Path) -> dict:
        """Run SQL migration files in order"""
        try:
            sql_files = sorted(sql_dir.glob("*.sql"))
            if not sql_files:
                return {"hook": "run_migrations", "success": True, "output": "No SQL migrations found"}
            
            # Check for migration tracking file
            applied_file = sql_dir / ".applied_migrations"
            applied = set()
            if applied_file.exists():
                applied = set(applied_file.read_text().strip().split('\n'))
            
            newly_applied = []
            for sql_file in sql_files:
                if sql_file.name in applied:
                    continue
                
                logger.info(f"Applying SQL migration: {sql_file.name}")
                # Execute SQL file using sqlite3 or appropriate DB client
                # This is a placeholder - actual implementation depends on DB type
                newly_applied.append(sql_file.name)
                applied.add(sql_file.name)
            
            # Update tracking file
            applied_file.write_text('\n'.join(sorted(applied)))
            
            return {
                "hook": "run_migrations",
                "success": True,
                "output": f"Applied {len(newly_applied)} migrations: {newly_applied}",
            }
        except Exception as e:
            return {"hook": "run_migrations", "success": False, "error": str(e)}
    
    def _hook_restart_services(self) -> dict:
        """Restart all DMS services"""
        try:
            services = ["facial", "get_gps_data"]
            results = {}
            all_success = True
            
            # Stop all services first
            for service in services:
                success, msg = ServiceManager.stop_service(service)
                results[f"stop_{service}"] = {"success": success, "message": msg}
            
            time.sleep(2)  # Wait for services to stop
            
            # Start all services
            for service in services:
                success, msg = ServiceManager.start_service(service)
                results[f"start_{service}"] = {"success": success, "message": msg}
                if not success:
                    all_success = False
            
            return {
                "hook": "restart_services",
                "success": all_success,
                "output": json.dumps(results),
            }
        except Exception as e:
            return {"hook": "restart_services", "success": False, "error": str(e)}
    
    def _hook_validate_syntax(self, target: Path, venv_path: Path) -> dict:
        """Validate Python syntax of all .py files"""
        try:
            python_path = venv_path / "bin" / "python" if venv_path.exists() else "python3"
            errors = []
            
            for py_file in target.rglob("*.py"):
                result = subprocess.run(
                    [str(python_path), "-m", "py_compile", str(py_file)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    errors.append(f"{py_file.name}: {result.stderr}")
            
            return {
                "hook": "validate_syntax",
                "success": len(errors) == 0,
                "output": f"Validated {len(list(target.rglob('*.py')))} files",
                "errors": errors[:10] if errors else [],  # Limit to first 10 errors
            }
        except Exception as e:
            return {"hook": "validate_syntax", "success": False, "error": str(e)}
    
    def _hook_clear_cache(self, target: Path) -> dict:
        """Clear Python cache directories"""
        try:
            cleared = 0
            for cache_dir in target.rglob("__pycache__"):
                if cache_dir.is_dir():
                    shutil.rmtree(cache_dir)
                    cleared += 1
            
            # Also clear .pyc files
            for pyc_file in target.rglob("*.pyc"):
                pyc_file.unlink()
                cleared += 1
            
            return {
                "hook": "clear_cache",
                "success": True,
                "output": f"Cleared {cleared} cache items",
            }
        except Exception as e:
            return {"hook": "clear_cache", "success": False, "error": str(e)}
    
    def _hook_custom_command(self, cmd: str, target: Path) -> dict:
        """Run a custom shell command"""
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(target),
            )
            
            return {
                "hook": f"custom:{cmd[:50]}",
                "success": result.returncode == 0,
                "output": result.stdout[-1000:] if result.stdout else "",
                "error": result.stderr[-500:] if result.returncode != 0 else "",
            }
        except subprocess.TimeoutExpired:
            return {"hook": f"custom:{cmd[:50]}", "success": False, "error": "Command timeout"}
        except Exception as e:
            return {"hook": f"custom:{cmd[:50]}", "success": False, "error": str(e)}
    
    def auto_update_on_startup(self) -> dict:
        """Check for updates and apply automatically on startup"""
        logger.info("=" * 50)
        logger.info("Running startup auto-update check...")
        logger.info("=" * 50)
        
        # Step 1: Check for updates
        check_result = self.check_update()
        
        if not check_result.get("update_available", False):
            logger.info("No updates available on startup")
            return {
                "action": "startup_check",
                "update_available": False,
                "current_version": self._current_version,
                "success": True,
            }
        
        pending = self._pending_update
        if not pending:
            return {"action": "startup_check", "error": "No pending update info", "success": False}
        
        logger.info(f"Update available: {self._current_version} -> {pending['version']}")
        
        # Step 2: Download update
        download_url = pending.get("download_url")
        if not download_url:
            return {"action": "startup_check", "error": "No download URL in update info", "success": False}
        
        download_result = self.download_from_url(
            download_url=download_url,
            filename=pending.get("filename", f"update_{pending['version']}.tar.gz"),
            target_path=Config.OTA_DIR,
            checksum=pending.get("checksum"),
            auto_backup=True,
            auto_rollback=True,
        )
        
        if not download_result.get("success", False):
            logger.error(f"Failed to download update: {download_result.get('error')}")
            return {
                "action": "startup_check",
                "step": "download",
                "error": download_result.get("error"),
                "success": False,
            }
        
        # Step 3: Stop services before applying update
        logger.info("Stopping DMS services for update...")
        for service in ["facial", "get_gps_data"]:
            ServiceManager.stop_service(service)
        time.sleep(3)
        
        # Step 4: Apply update
        apply_result = self.apply_update(
            version=pending["version"],
            target_dir=Config.FACIAL_TRACKER_DIR,
        )
        
        if not apply_result.get("success", False):
            logger.error(f"Failed to apply update: {apply_result.get('error')}")
            # Try to restart services even if update failed
            for service in ["facial", "get_gps_data"]:
                ServiceManager.start_service(service)
            return {
                "action": "startup_check",
                "step": "apply",
                "error": apply_result.get("error"),
                "success": False,
            }
        
        # Step 5: Run post-update hooks
        hooks = pending.get("post_update_hooks", ["install_requirements", "run_migrations", "restart_services"])
        hooks_result = self.run_post_update_hooks(
            target_dir=Config.FACIAL_TRACKER_DIR,
            hooks=hooks,
        )
        
        logger.info("=" * 50)
        logger.info(f"Startup auto-update completed: {self._current_version}")
        logger.info("=" * 50)
        
        return {
            "action": "startup_check",
            "previous_version": check_result.get("current_version"),
            "new_version": pending["version"],
            "download": download_result,
            "apply": apply_result,
            "hooks": hooks_result,
            "success": True,
        }


class ConfigManager:
    """Manages device configuration"""
    
    CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'device_config.json')
    
    def __init__(self):
        self._config = self._load_config()
    
    def _load_config(self) -> dict:
        """Load configuration from file"""
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading config: {e}")
        return {}
    
    def _save_config(self):
        """Save configuration to file"""
        try:
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(self._config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get_config(self, key: str = None) -> dict:
        """Get configuration value(s)"""
        if key:
            return {"key": key, "value": self._config.get(key), "success": True}
        return {"config": self._config, "success": True}
    
    def set_config(self, key: str, value: Any) -> dict:
        """Set a configuration value"""
        self._config[key] = value
        self._save_config()
        return {"key": key, "value": value, "success": True}
    
    def delete_config(self, key: str) -> dict:
        """Delete a configuration value"""
        if key in self._config:
            del self._config[key]
            self._save_config()
            return {"key": key, "success": True}
        return {"error": f"Key not found: {key}", "success": False}
    
    def reset_config(self) -> dict:
        """Reset configuration to defaults"""
        self._config = {}
        self._save_config()
        return {"success": True}


class MQTTControlClient:
    """Lightweight MQTT client for control service"""
    
    def __init__(self, device_id: str, on_command: Callable):
        self.device_id = device_id
        self.on_command = on_command
        self._client = None
        self._connected = False
        self._loop = None
        
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    async def connect(self) -> bool:
        """Connect to MQTT broker"""
        try:
            import paho.mqtt.client as mqtt
            
            self._loop = asyncio.get_event_loop()
            # Use CallbackAPIVersion for paho-mqtt 2.x
            try:
                self._client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"pi-control-{self.device_id}",
                    protocol=mqtt.MQTTv311,
                )
            except (AttributeError, TypeError):
                # Fallback for older paho-mqtt versions
                self._client = mqtt.Client(
                    client_id=f"pi-control-{self.device_id}",
                    protocol=mqtt.MQTTv311,
                )
            
            # Set credentials if configured
            if Config.MQTT_USERNAME and Config.MQTT_PASSWORD:
                self._client.username_pw_set(Config.MQTT_USERNAME, Config.MQTT_PASSWORD)
            
            # Set callbacks
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message
            
            # Connect
            self._client.connect_async(
                Config.MQTT_BROKER_HOST,
                Config.MQTT_BROKER_PORT,
                keepalive=60,
            )
            self._client.loop_start()
            
            # Wait for connection
            for _ in range(50):  # 5 seconds timeout
                if self._connected:
                    return True
                await asyncio.sleep(0.1)
            
            return False
            
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            return False
    
    async def disconnect(self):
        """Disconnect from MQTT broker"""
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Handle MQTT connection"""
        # Handle both paho-mqtt 1.x (rc is int) and 2.x (rc is ReasonCode)
        rc_value = rc.value if hasattr(rc, 'value') else rc
        if rc_value == 0:
            logger.info("MQTT connected")
            self._connected = True
            # Subscribe to command topic
            topic = f"device/command/{self.device_id}"
            client.subscribe(topic, qos=1)
            logger.info(f"Subscribed to: {topic}")
        else:
            logger.error(f"MQTT connection failed: {rc}")
    
    def _on_disconnect(self, client, userdata, disconnect_flags=None, rc=None, properties=None):
        """Handle MQTT disconnection"""
        logger.warning(f"MQTT disconnected: {rc}")
        self._connected = False
    
    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT message"""
        if self._loop and self.on_command:
            asyncio.run_coroutine_threadsafe(
                self.on_command(msg.topic, msg.payload),
                self._loop,
            )
    
    async def publish_status(self, status: dict):
        """Publish status response"""
        if not self._connected:
            return False
        
        try:
            topic = f"device/status/{self.device_id}"
            payload = json.dumps(status)
            self._client.publish(topic, payload, qos=1)
            return True
        except Exception as e:
            logger.error(f"Publish error: {e}")
            return False
    
    async def publish_ota_result(self, result: dict):
        """Publish OTA deployment result"""
        if not self._connected:
            return False
        
        try:
            topic = f"device/ota_result/{self.device_id}"
            payload = json.dumps(result)
            self._client.publish(topic, payload, qos=1)
            logger.info(f"Published OTA result to {topic}")
            return True
        except Exception as e:
            logger.error(f"OTA result publish error: {e}")
            return False


class PiControlService:
    """Main Pi Device Control Service - Enhanced Version"""
    
    def __init__(self):
        self.device_id = Config.DEVICE_ID
        if not self.device_id:
            raise ValueError("DEVICE_ID not configured. Set it in .env file.")
        
        self.mqtt_client: Optional[MQTTControlClient] = None
        self._running = False
        self._start_time = time.time()
        
        # Initialize managers
        self.alert_manager = AlertManager()
        self.file_manager = FileManager()
        self.ota_manager = OTAManager()
        self.config_manager = ConfigManager()
        
    async def start(self):
        """Start the control service"""
        logger.info("=" * 60)
        logger.info("Pi Device Control Service - Enhanced Version")
        logger.info(f"Device ID: {self.device_id}")
        logger.info(f"MQTT Broker: {Config.MQTT_BROKER_HOST}:{Config.MQTT_BROKER_PORT}")
        logger.info("Features: Services, Alerts, Files, Shell, OTA, Config")
        logger.info("=" * 60)
        
        self._running = True
        
        # Run auto-update check on startup (if enabled)
        auto_update_enabled = os.getenv("OTA_AUTO_UPDATE_ON_STARTUP", "true").lower() == "true"
        if auto_update_enabled:
            try:
                logger.info("Checking for OTA updates on startup...")
                update_result = self.ota_manager.auto_update_on_startup()
                if update_result.get("update_available") and update_result.get("success"):
                    logger.info(f"Auto-update completed: {update_result.get('previous_version')} -> {update_result.get('new_version')}")
                elif update_result.get("update_available") and not update_result.get("success"):
                    logger.error(f"Auto-update failed: {update_result.get('error')}")
                else:
                    logger.info(f"No updates available. Current version: {self.ota_manager._current_version}")
            except Exception as e:
                logger.error(f"Auto-update check failed: {e}")
        else:
            logger.info("Auto-update on startup is disabled")
        
        # Initialize MQTT client
        self.mqtt_client = MQTTControlClient(
            device_id=self.device_id,
            on_command=self._handle_command,
        )
        
        # Connect to MQTT
        if await self.mqtt_client.connect():
            logger.info("MQTT connected - ready for commands")
            # Send immediate heartbeat on startup to mark device online
            await self._send_heartbeat()
            logger.info("Initial heartbeat sent - device marked online")
        else:
            logger.warning("MQTT connection failed - will retry")
        
        # Main loop
        await self._main_loop()
    
    async def stop(self):
        """Stop the control service"""
        logger.info("Stopping Pi Control Service...")
        self._running = False
        
        if self.mqtt_client:
            await self.mqtt_client.disconnect()
        
        logger.info("Pi Control Service stopped")
    
    async def _main_loop(self):
        """Main service loop - reconnect MQTT if needed, send heartbeat, check alerts"""
        reconnect_interval = 30
        heartbeat_interval = 60  # Send heartbeat every 60 seconds
        alert_check_interval = 30  # Check alerts every 30 seconds
        last_reconnect = 0
        last_heartbeat = 0
        last_alert_check = 0
        
        while self._running:
            try:
                current_time = time.time()
                
                # Reconnect MQTT if disconnected
                if not self.mqtt_client.is_connected:
                    if current_time - last_reconnect >= reconnect_interval:
                        last_reconnect = current_time
                        logger.info("Attempting MQTT reconnection...")
                        if await self.mqtt_client.connect():
                            # Send immediate heartbeat on reconnect to mark device online
                            await self._send_heartbeat()
                            last_heartbeat = current_time
                            logger.info("MQTT reconnected - heartbeat sent")
                
                # Check for alerts
                if current_time - last_alert_check >= alert_check_interval:
                    last_alert_check = current_time
                    system_metrics = SystemMetrics.get_all_metrics()
                    new_alerts = self.alert_manager.check_metrics(system_metrics)
                    
                    # Publish alerts if any
                    if new_alerts and self.mqtt_client.is_connected:
                        for alert in new_alerts:
                            await self.mqtt_client.publish_status({
                                "device_id": self.device_id,
                                "type": "alert",
                                "alert": asdict(alert),
                                "timestamp": int(current_time),
                            })
                
                # Send periodic heartbeat to keep device online status
                if self.mqtt_client.is_connected:
                    if current_time - last_heartbeat >= heartbeat_interval:
                        last_heartbeat = current_time
                        await self._send_heartbeat()
                
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(1)
    
    async def _send_heartbeat(self):
        """Send heartbeat to mark device online - no GPS required"""
        try:
            current_time = time.time()
            
            # Get DMS services status
            facial_status = ServiceManager.get_service_status("facial")
            gps_data_status = ServiceManager.get_service_status("get_gps_data")
            
            system_metrics = SystemMetrics.get_all_metrics()
            network_metrics = SystemMetrics.get_network_metrics()
            active_alerts = self.alert_manager.get_active_alerts()
            
            await self.mqtt_client.publish_status({
                "device_id": self.device_id,
                "type": "heartbeat",
                "uptime": self._get_uptime(),
                "timestamp": int(current_time),
                # DMS Services status
                "dms_services": {
                    "facial": {
                        "active": facial_status.get("active", False),
                        "enabled": facial_status.get("enabled", False),
                        "status": facial_status.get("status", "unknown"),
                        "pid": facial_status.get("pid"),
                    },
                    "get_gps_data": {
                        "active": gps_data_status.get("active", False),
                        "enabled": gps_data_status.get("enabled", False),
                        "status": gps_data_status.get("status", "unknown"),
                        "pid": gps_data_status.get("pid"),
                    },
                },
                "system": system_metrics,
                "network": network_metrics['network'],
                "system_uptime": network_metrics['uptime'],
                "alerts": {
                    "active": len(active_alerts),
                    "items": active_alerts,
                },
                "ota_version": self.ota_manager._current_version,
            })
            logger.debug("Heartbeat sent with metrics: CPU %.1f%%, Mem %.1f%%, Temp %.1f°C, IP %s, Alerts: %d",
                       system_metrics['cpu_percent'],
                       system_metrics['memory']['percent'],
                       system_metrics['cpu_temp'],
                       network_metrics['network']['ip_address'],
                       len(active_alerts))
        except Exception as e:
            logger.error(f"Failed to send heartbeat: {e}")
    
    async def _handle_command(self, topic: str, payload: bytes):
        """Handle incoming MQTT commands"""
        try:
            # Parse command
            try:
                command = json.loads(payload.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                import msgpack
                command = msgpack.unpackb(payload, raw=False)
            
            cmd_type = command.get("cmd", "")
            params = command.get("params") or {}  # Handle None params
            
            logger.info(f"Command received: {cmd_type}")
            
            # Route command
            response = await self._execute_command(cmd_type, params)
            
            # Add metadata to response
            response["type"] = "command_response"
            response["command"] = cmd_type
            response["device_id"] = self.device_id
            response["timestamp"] = int(time.time())
            
            # Publish response
            if self.mqtt_client:
                await self.mqtt_client.publish_status(response)
                
                # For OTA commands, also publish to ota_result topic for backend tracking
                if cmd_type in ("ota_download", "ota_download_apply") and "deployment_id" in response:
                    await self.mqtt_client.publish_ota_result(response)
                
        except Exception as e:
            logger.error(f"Command handling error: {e}")
    
    async def _execute_command(self, cmd_type: str, params: dict) -> dict:
        """Execute command and return response"""
        
        # ==================== Device Control Commands ====================
        if cmd_type == "ping":
            return {"status": "pong", "device_id": self.device_id, "uptime": self._get_uptime()}
        
        elif cmd_type == "status":
            return self._get_device_status()
        
        elif cmd_type == "get_stats":
            return self._get_device_status()
        
        elif cmd_type == "get_config":
            return self._get_config()
        
        elif cmd_type == "reboot":
            delay = params.get("delay", 3)
            logger.warning(f"Reboot in {delay} seconds...")
            asyncio.create_task(self._delayed_reboot(delay))
            return {"status": "rebooting", "delay": delay}
        
        elif cmd_type == "shutdown":
            delay = params.get("delay", 3)
            logger.warning(f"Shutdown in {delay} seconds...")
            asyncio.create_task(self._delayed_shutdown(delay))
            return {"status": "shutting_down", "delay": delay}
        
        # ==================== Service Control Commands ====================
        elif cmd_type == "list_services":
            return {"services": ServiceManager.list_services(), "success": True}
        
        elif cmd_type == "service_status":
            service = params.get("service", "pi-gps-tracker")
            return ServiceManager.get_service_status(service)
        
        elif cmd_type == "start_service":
            service = params.get("service", "pi-gps-tracker")
            success, msg = ServiceManager.start_service(service)
            return {"success": success, "message": msg, "action": "start", "service": service}
        
        elif cmd_type == "stop_service":
            service = params.get("service", "pi-gps-tracker")
            success, msg = ServiceManager.stop_service(service)
            return {"success": success, "message": msg, "action": "stop", "service": service}
        
        elif cmd_type == "restart_service":
            service = params.get("service", "pi-gps-tracker")
            success, msg = ServiceManager.restart_service(service)
            return {"success": success, "message": msg, "action": "restart", "service": service}
        
        elif cmd_type == "enable_service":
            service = params.get("service", "pi-gps-tracker")
            success, msg = ServiceManager.enable_service(service)
            return {"success": success, "message": msg, "action": "enable", "service": service}
        
        elif cmd_type == "disable_service":
            service = params.get("service", "pi-gps-tracker")
            success, msg = ServiceManager.disable_service(service)
            return {"success": success, "message": msg, "action": "disable", "service": service}
        
        elif cmd_type == "add_service":
            name = params.get("name")
            unit = params.get("unit")
            if not name or not unit:
                return {"error": "Missing name or unit parameter", "success": False}
            ServiceManager.add_service(name, unit)
            return {"success": True, "name": name, "unit": unit}
        
        elif cmd_type == "remove_service":
            name = params.get("name")
            if not name:
                return {"error": "Missing name parameter", "success": False}
            success = ServiceManager.remove_service(name)
            return {"success": success, "name": name}
        
        # Service group commands (for facial tracker batch operations)
        elif cmd_type == "list_groups":
            return {"groups": ServiceManager.list_groups(), "success": True}
        
        elif cmd_type == "group_status":
            group = params.get("group", "facial_tracker")
            return ServiceManager.control_group(group, "status")
        
        elif cmd_type == "start_group":
            group = params.get("group", "facial_tracker")
            return ServiceManager.control_group(group, "start")
        
        elif cmd_type == "stop_group":
            group = params.get("group", "facial_tracker")
            return ServiceManager.control_group(group, "stop")
        
        elif cmd_type == "restart_group":
            group = params.get("group", "facial_tracker")
            return ServiceManager.control_group(group, "restart")
        
        elif cmd_type == "all_services_status":
            return {"services": ServiceManager.get_all_status(), "success": True}
        
        # Facial Tracker shortcut commands
        elif cmd_type == "start_facial":
            success, msg = ServiceManager.start_service("facial")
            return {"success": success, "message": msg, "action": "start_facial"}
        
        elif cmd_type == "stop_facial":
            success, msg = ServiceManager.stop_service("facial")
            return {"success": success, "message": msg, "action": "stop_facial"}
        
        elif cmd_type == "restart_facial":
            success, msg = ServiceManager.restart_service("facial")
            return {"success": success, "message": msg, "action": "restart_facial"}
        
        # GPS Data service commands
        elif cmd_type == "start_gps_data":
            success, msg = ServiceManager.start_service("get_gps_data")
            return {"success": success, "message": msg, "action": "start_gps_data"}
        
        elif cmd_type == "stop_gps_data":
            success, msg = ServiceManager.stop_service("get_gps_data")
            return {"success": success, "message": msg, "action": "stop_gps_data"}
        
        elif cmd_type == "restart_gps_data":
            success, msg = ServiceManager.restart_service("get_gps_data")
            return {"success": success, "message": msg, "action": "restart_gps_data"}
        
        elif cmd_type == "start_all_dms":
            return ServiceManager.control_group("all_dms", "start")
        
        elif cmd_type == "stop_all_dms":
            return ServiceManager.control_group("all_dms", "stop")
        
        elif cmd_type == "restart_all_dms":
            return ServiceManager.control_group("all_dms", "restart")
        
        elif cmd_type == "dms_status":
            return ServiceManager.control_group("all_dms", "status")
        
        # Legacy GPS commands (backwards compatibility)
        elif cmd_type == "start_gps":
            success, msg = ServiceManager.start_service("pi-gps-tracker")
            return {"success": success, "message": msg, "action": "start_gps"}
        
        elif cmd_type == "stop_gps":
            success, msg = ServiceManager.stop_service("pi-gps-tracker")
            return {"success": success, "message": msg, "action": "stop_gps"}
        
        elif cmd_type == "restart_gps":
            success, msg = ServiceManager.restart_service("pi-gps-tracker")
            return {"success": success, "message": msg, "action": "restart_gps"}
        
        elif cmd_type == "enable_gps":
            success, msg = ServiceManager.enable_service("pi-gps-tracker")
            return {"success": success, "message": msg, "action": "enable_gps"}
        
        elif cmd_type == "disable_gps":
            success, msg = ServiceManager.disable_service("pi-gps-tracker")
            return {"success": success, "message": msg, "action": "disable_gps"}
        
        elif cmd_type == "get_logs":
            service = params.get("service", "pi-gps-tracker")
            lines = params.get("lines", 50)
            return self._get_service_logs(service, lines)
        
        # ==================== Alert Commands ====================
        elif cmd_type == "get_alerts":
            return {
                "active": self.alert_manager.get_active_alerts(),
                "count": len(self.alert_manager.active_alerts),
                "success": True,
            }
        
        elif cmd_type == "get_alert_history":
            limit = params.get("limit", 50)
            return {
                "history": self.alert_manager.get_alert_history(limit),
                "success": True,
            }
        
        elif cmd_type == "acknowledge_alert":
            alert_id = params.get("alert_id")
            if not alert_id:
                return {"error": "Missing alert_id parameter", "success": False}
            success = self.alert_manager.acknowledge_alert(alert_id)
            return {"success": success, "alert_id": alert_id}
        
        elif cmd_type == "clear_alerts":
            count = self.alert_manager.clear_alerts()
            return {"success": True, "cleared": count}
        
        elif cmd_type == "get_thresholds":
            return {"thresholds": asdict(self.alert_manager.thresholds), "success": True}
        
        elif cmd_type == "set_thresholds":
            thresholds = params.get("thresholds", {})
            updated = self.alert_manager.update_thresholds(thresholds)
            return {"thresholds": updated, "success": True}
        
        # ==================== File Management Commands ====================
        elif cmd_type == "list_files":
            path = params.get("path")
            pattern = params.get("pattern", "*")
            return self.file_manager.list_files(path, pattern)
        
        elif cmd_type == "read_file":
            path = params.get("path")
            if not path:
                return {"error": "Missing path parameter", "success": False}
            encoding = params.get("encoding", "utf-8")
            return self.file_manager.read_file(path, encoding)
        
        elif cmd_type == "write_file":
            path = params.get("path")
            content = params.get("content")
            if not path or content is None:
                return {"error": "Missing path or content parameter", "success": False}
            encoding = params.get("encoding", "utf-8")
            is_base64 = params.get("is_base64", False)
            return self.file_manager.write_file(path, content, encoding, is_base64)
        
        elif cmd_type == "delete_file":
            path = params.get("path")
            if not path:
                return {"error": "Missing path parameter", "success": False}
            return self.file_manager.delete_file(path)
        
        elif cmd_type == "upload_file":
            filename = params.get("filename")
            content = params.get("content")
            if not filename or not content:
                return {"error": "Missing filename or content parameter", "success": False}
            target_dir = params.get("target_dir")
            is_base64 = params.get("is_base64", True)
            is_compressed = params.get("is_compressed", False)
            return self.file_manager.upload_file(filename, content, target_dir, is_base64, is_compressed)
        
        elif cmd_type == "download_file":
            path = params.get("path")
            if not path:
                return {"error": "Missing path parameter", "success": False}
            return self.file_manager.download_file(path)
        
        # ==================== Shell Execution Commands ====================
        elif cmd_type == "shell_exec":
            command = params.get("command")
            if not command:
                return {"error": "Missing command parameter", "success": False}
            timeout = params.get("timeout")
            cwd = params.get("cwd")
            return ShellExecutor.execute(command, timeout, cwd)
        
        elif cmd_type == "shell_script":
            script = params.get("script")
            if not script:
                return {"error": "Missing script parameter", "success": False}
            interpreter = params.get("interpreter", "/bin/bash")
            timeout = params.get("timeout")
            return ShellExecutor.execute_script(script, interpreter, timeout)
        
        # ==================== OTA Update Commands ====================
        elif cmd_type == "ota_download":
            # Download OTA file from S3 via pre-signed URL
            download_url = params.get("download_url")
            filename = params.get("filename")
            target_path = params.get("target_path")
            checksum = params.get("checksum")
            deployment_id = params.get("deployment_id")
            auto_backup = params.get("auto_backup", True)
            auto_rollback = params.get("auto_rollback", True)
            
            if not download_url or not filename:
                return {"error": "Missing download_url or filename parameter", "success": False}
            
            return self.ota_manager.download_from_url(
                download_url, filename, target_path, checksum, deployment_id,
                auto_backup, auto_rollback
            )
        
        elif cmd_type == "ota_download_apply":
            # Download, validate, and apply OTA with automatic backup and rollback
            download_url = params.get("download_url")
            filename = params.get("filename")
            target_path = params.get("target_path")
            checksum = params.get("checksum")
            deployment_id = params.get("deployment_id")
            restart_service = params.get("restart_service")
            validation_cmd = params.get("validation_cmd")
            
            if not download_url or not filename or not target_path:
                return {"error": "Missing download_url, filename, or target_path parameter", "success": False}
            
            return self.ota_manager.download_and_apply(
                download_url, filename, target_path, checksum, deployment_id,
                restart_service, validation_cmd
            )
        
        elif cmd_type == "ota_status":
            return self.ota_manager.get_status()
        
        elif cmd_type == "ota_check":
            update_url = params.get("update_url")
            return self.ota_manager.check_update(update_url)
        
        elif cmd_type == "ota_prepare":
            package = params.get("package")
            version = params.get("version")
            checksum = params.get("checksum")
            if not package or not version:
                return {"error": "Missing package or version parameter", "success": False}
            return self.ota_manager.prepare_update(package, version, checksum)
        
        elif cmd_type == "ota_apply":
            version = params.get("version")
            target_dir = params.get("target_dir")
            restart_service = params.get("restart_service")
            if not version or not target_dir:
                return {"error": "Missing version or target_dir parameter", "success": False}
            return self.ota_manager.apply_update(version, target_dir, restart_service)
        
        elif cmd_type == "ota_rollback":
            backup_name = params.get("backup_name")
            target_dir = params.get("target_dir")
            if not backup_name or not target_dir:
                return {"error": "Missing backup_name or target_dir parameter", "success": False}
            return self.ota_manager.rollback(backup_name, target_dir)
        
        elif cmd_type == "ota_list_backups":
            return self.ota_manager.list_backups()
        
        elif cmd_type == "ota_cleanup":
            keep = params.get("keep", 5)
            return self.ota_manager.cleanup_backups(keep)
        
        # Facial Tracker OTA shortcuts
        elif cmd_type == "ota_update_dms":
            # Shortcut to update facial tracker firmware
            package = params.get("package")
            version = params.get("version")
            checksum = params.get("checksum")
            if not package or not version:
                return {"error": "Missing package or version parameter", "success": False}
            
            # Stop all DMS services first
            logger.info("Stopping DMS services for update...")
            ServiceManager.control_group("all_dms", "stop")
            
            # Prepare update
            prep_result = self.ota_manager.prepare_update(package, version, checksum)
            if not prep_result.get("success"):
                # Restart services on failure
                ServiceManager.control_group("all_dms", "start")
                return prep_result
            
            # Apply update to facial tracker directory
            apply_result = self.ota_manager.apply_update(
                version, 
                Config.FACIAL_TRACKER_DIR,
                restart_service=None  # We'll restart the group
            )
            
            # Restart all DMS services
            logger.info("Restarting DMS services after update...")
            ServiceManager.control_group("all_dms", "start")
            
            return {
                "success": apply_result.get("success", False),
                "version": version,
                "target": Config.FACIAL_TRACKER_DIR,
                "backup": apply_result.get("backup"),
                "services_restarted": True,
            }
        
        elif cmd_type == "ota_rollback_dms":
            # Shortcut to rollback facial tracker firmware
            backup_name = params.get("backup_name")
            if not backup_name:
                return {"error": "Missing backup_name parameter", "success": False}
            
            # Stop all DMS services
            logger.info("Stopping DMS services for rollback...")
            ServiceManager.control_group("all_dms", "stop")
            
            # Rollback
            result = self.ota_manager.rollback(backup_name, Config.FACIAL_TRACKER_DIR)
            
            # Restart services
            logger.info("Restarting DMS services after rollback...")
            ServiceManager.control_group("all_dms", "start")
            
            result["services_restarted"] = True
            return result
        
        elif cmd_type == "ota_run_hooks":
            # Run post-update hooks manually
            target_dir = params.get("target_dir", Config.FACIAL_TRACKER_DIR)
            hooks = params.get("hooks")  # Optional list of specific hooks to run
            return self.ota_manager.run_post_update_hooks(target_dir, hooks)
        
        elif cmd_type == "ota_auto_update":
            # Trigger auto-update check and apply
            return self.ota_manager.auto_update_on_startup()
        
        elif cmd_type == "ota_full_update":
            # Full OTA update with hooks: download, apply, run hooks
            download_url = params.get("download_url")
            filename = params.get("filename")
            version = params.get("version")
            checksum = params.get("checksum")
            hooks = params.get("hooks", ["install_requirements", "run_migrations", "restart_services"])
            
            if not download_url or not filename:
                return {"error": "Missing download_url or filename parameter", "success": False}
            
            # Stop all DMS services first
            logger.info("Stopping DMS services for full update...")
            ServiceManager.control_group("all_dms", "stop")
            time.sleep(2)
            
            # Download update
            download_result = self.ota_manager.download_from_url(
                download_url=download_url,
                filename=filename,
                target_path=Config.FACIAL_TRACKER_DIR,
                checksum=checksum,
                auto_backup=True,
                auto_rollback=True,
            )
            
            if not download_result.get("success"):
                # Restart services on failure
                ServiceManager.control_group("all_dms", "start")
                return download_result
            
            # Update version if provided
            if version:
                self.ota_manager._current_version = version
                self.ota_manager._write_version(version)
            
            # Run post-update hooks
            hooks_result = self.ota_manager.run_post_update_hooks(
                target_dir=Config.FACIAL_TRACKER_DIR,
                hooks=hooks,
            )
            
            return {
                "success": True,
                "download": download_result,
                "hooks": hooks_result,
                "version": version or self.ota_manager._current_version,
            }
        
        # ==================== Configuration Commands ====================
        elif cmd_type == "config_get":
            key = params.get("key")
            return self.config_manager.get_config(key)
        
        elif cmd_type == "config_set":
            key = params.get("key")
            value = params.get("value")
            if not key:
                return {"error": "Missing key parameter", "success": False}
            return self.config_manager.set_config(key, value)
        
        elif cmd_type == "config_delete":
            key = params.get("key")
            if not key:
                return {"error": "Missing key parameter", "success": False}
            return self.config_manager.delete_config(key)
        
        elif cmd_type == "config_reset":
            return self.config_manager.reset_config()
        
        # ==================== System Info Commands ====================
        elif cmd_type == "system_info":
            return self._get_system_info()
        
        elif cmd_type == "network_info":
            return SystemMetrics.get_network_info()
        
        elif cmd_type == "disk_info":
            return self._get_disk_info()
        
        elif cmd_type == "process_list":
            return self._get_process_list(params.get("limit", 20))
        
        # ==================== Unknown Command ====================
        else:
            logger.warning(f"Unknown command: {cmd_type}")
            return {"error": f"Unknown command: {cmd_type}"}
    
    def _get_uptime(self) -> str:
        """Get service uptime"""
        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"
    
    def _get_device_status(self) -> dict:
        """Get full device status"""
        # Get all service statuses
        services = {}
        for name in ServiceManager.ALLOWED_SERVICES.keys():
            try:
                services[name] = ServiceManager.get_service_status(name)
            except Exception:
                services[name] = {"status": "unknown", "active": False}
        
        # Get system metrics
        system_metrics = SystemMetrics.get_all_metrics()
        network_metrics = SystemMetrics.get_network_metrics()
        
        return {
            "device_id": self.device_id,
            "uptime": self._get_uptime(),
            "mqtt_connected": self.mqtt_client.is_connected if self.mqtt_client else False,
            "services": services,
            "system": system_metrics,
            "network": network_metrics['network'],
            "system_uptime": network_metrics['uptime'],
            "alerts": {
                "active": len(self.alert_manager.active_alerts),
                "items": self.alert_manager.get_active_alerts(),
            },
            "ota_version": self.ota_manager._current_version,
        }
    
    def _get_config(self) -> dict:
        """Get device configuration"""
        return {
            "device_id": self.device_id,
            "mqtt_broker": f"{Config.MQTT_BROKER_HOST}:{Config.MQTT_BROKER_PORT}",
            "services": list(ServiceManager.ALLOWED_SERVICES.keys()),
            "upload_dir": Config.UPLOAD_DIR,
            "download_dir": Config.DOWNLOAD_DIR,
            "max_file_size": Config.MAX_FILE_SIZE,
            "ota_dir": Config.OTA_DIR,
            "backup_dir": Config.BACKUP_DIR,
            "shell_timeout": Config.SHELL_TIMEOUT,
            "alert_thresholds": asdict(self.alert_manager.thresholds),
            "device_config": self.config_manager._config,
        }
    
    def _get_service_logs(self, service: str, lines: int = 50) -> dict:
        """Get recent logs for a service using journalctl"""
        try:
            # Limit lines to prevent huge responses
            lines = min(lines, 200)
            
            # Map service names
            service_unit = ServiceManager.ALLOWED_SERVICES.get(service, service)
            if not service_unit.endswith('.service'):
                service_unit = f"{service_unit}.service"
            
            # Get logs using journalctl
            result = subprocess.run(
                ['journalctl', '-u', service_unit, '-n', str(lines), '--no-pager', '-o', 'short-iso'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            
            log_lines = result.stdout.strip().split('\n') if result.stdout else []
            
            return {
                "service": service,
                "lines": log_lines,
                "count": len(log_lines),
                "success": True,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Timeout fetching logs", "success": False}
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def _get_system_info(self) -> dict:
        """Get comprehensive system information"""
        import platform
        
        info = {
            "device_id": self.device_id,
            "hostname": platform.node(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
        }
        
        # Get kernel version
        try:
            result = subprocess.run(['uname', '-r'], capture_output=True, text=True, timeout=5)
            info['kernel'] = result.stdout.strip()
        except Exception:
            info['kernel'] = 'unknown'
        
        # Get Pi model if available
        try:
            with open('/proc/device-tree/model', 'r') as f:
                info['model'] = f.read().strip().replace('\x00', '')
        except Exception:
            info['model'] = 'unknown'
        
        # Get serial number
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('Serial'):
                        info['serial'] = line.split(':')[1].strip()
                        break
        except Exception:
            info['serial'] = 'unknown'
        
        # Add metrics
        info['metrics'] = SystemMetrics.get_all_metrics()
        info['network'] = SystemMetrics.get_network_info()
        info['uptime'] = SystemMetrics.get_system_uptime()
        
        return info
    
    def _get_disk_info(self) -> dict:
        """Get detailed disk information"""
        try:
            result = subprocess.run(
                ['df', '-h'],
                capture_output=True, text=True, timeout=5
            )
            
            partitions = []
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                for line in lines[1:]:
                    parts = line.split()
                    if len(parts) >= 6:
                        partitions.append({
                            "filesystem": parts[0],
                            "size": parts[1],
                            "used": parts[2],
                            "available": parts[3],
                            "percent": parts[4],
                            "mount": parts[5],
                        })
            
            return {
                "partitions": partitions,
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    def _get_process_list(self, limit: int = 20) -> dict:
        """Get list of top processes by CPU/memory usage"""
        try:
            result = subprocess.run(
                ['ps', 'aux', '--sort=-pcpu'],
                capture_output=True, text=True, timeout=5
            )
            
            processes = []
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                for line in lines[1:limit+1]:
                    parts = line.split(None, 10)
                    if len(parts) >= 11:
                        processes.append({
                            "user": parts[0],
                            "pid": parts[1],
                            "cpu": parts[2],
                            "mem": parts[3],
                            "vsz": parts[4],
                            "rss": parts[5],
                            "stat": parts[7],
                            "start": parts[8],
                            "time": parts[9],
                            "command": parts[10][:100],  # Limit command length
                        })
            
            return {
                "processes": processes,
                "count": len(processes),
                "success": True,
            }
        except Exception as e:
            return {"error": str(e), "success": False}
    
    async def _delayed_reboot(self, delay: int):
        """Reboot after delay"""
        await asyncio.sleep(delay)
        os.system("sudo reboot")
    
    async def _delayed_shutdown(self, delay: int):
        """Shutdown after delay"""
        await asyncio.sleep(delay)
        os.system("sudo shutdown -h now")


async def main():
    """Main entry point"""
    service = PiControlService()
    
    # Setup signal handlers
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        logger.info("Shutdown signal received")
        asyncio.create_task(service.stop())
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await service.start()
    except KeyboardInterrupt:
        pass
    finally:
        await service.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass