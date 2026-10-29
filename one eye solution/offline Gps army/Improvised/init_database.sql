-- ============================================
-- Sapience DMS - Database Initialization Script
-- Run this on a new Raspberry Pi to create all required tables
-- ============================================
-- Usage: mysql -u root -praspberry@123 < init_database.sql
-- ============================================

-- Create database
CREATE DATABASE IF NOT EXISTS car;
USE car;

-- ============================================
-- 1. Device Table (for provisioning)
-- Stores device_id and auth_key from backend
-- ============================================
CREATE TABLE IF NOT EXISTS device (
    id INT AUTO_INCREMENT PRIMARY KEY,
    device_id VARCHAR(100) UNIQUE NOT NULL,
    auth_key VARCHAR(64),
    device_type VARCHAR(50) DEFAULT 'DM',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 2. User Info Table (legacy - for backward compatibility)
-- Some scripts may still reference this table
-- ============================================
CREATE TABLE IF NOT EXISTS user_info (
    id INT AUTO_INCREMENT PRIMARY KEY,
    phone_number VARCHAR(20),
    access_token VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 3. Car Data Table (driver status tracking)
-- Stores driver monitoring status events
-- ============================================
CREATE TABLE IF NOT EXISTS car_data (
    id INT AUTO_INCREMENT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    driver_status VARCHAR(255)
);

-- ============================================
-- 4. GPS Data Table (location tracking)
-- Stores GPS coordinates and speed data
-- ============================================
CREATE TABLE IF NOT EXISTS gps_data (
    id INT AUTO_INCREMENT PRIMARY KEY,
    latitude DECIMAL(10, 8),
    longitude DECIMAL(11, 8),
    speed DECIMAL(10, 2),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    driver_status VARCHAR(255),
    acceleration DECIMAL(10, 4)
);

-- Add index for faster queries on timestamp
CREATE INDEX IF NOT EXISTS idx_gps_timestamp ON gps_data(timestamp);

-- ============================================
-- 5. Configure Table (device configuration)
-- Stores key-value configuration settings
-- ============================================
CREATE TABLE IF NOT EXISTS configure (
    id INT AUTO_INCREMENT PRIMARY KEY,
    config_key VARCHAR(100) UNIQUE NOT NULL,
    config_value VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Insert default configuration values
INSERT IGNORE INTO configure (config_key, config_value) VALUES
    ('speed', '0'),
    ('alert_enabled', '1'),
    ('upload_interval', '30');

-- ============================================
-- 6. Count Table (daily event counting)
-- Tracks daily event counts
-- ============================================
CREATE TABLE IF NOT EXISTS count_table (
    id INT AUTO_INCREMENT PRIMARY KEY,
    date DATE UNIQUE NOT NULL,
    count INT DEFAULT 0
);

-- ============================================
-- Grant permissions (if needed)
-- ============================================
-- GRANT ALL PRIVILEGES ON car.* TO 'root'@'localhost';
-- FLUSH PRIVILEGES;

-- ============================================
-- Verification: Show created tables
-- ============================================
SHOW TABLES;

SELECT 'Database initialization complete!' AS status;
