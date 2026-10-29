import os
import logging
from logging.handlers import RotatingFileHandler

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def setup_logger(log_dir):
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    root = logging.getLogger('')
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return

    # Size-capped so the SD card can't fill up on a long-running device.
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "device.log"),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    root.setLevel(logging.INFO)

def log_info(message):
    logging.info(message)

def log_error(message):
    logging.error(message)

# Set the directory where you want to store the log files
log_directory = "./logs"
setup_logger(log_directory)
