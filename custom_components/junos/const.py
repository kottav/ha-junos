"""Constants for the Juniper Junos integration."""

from __future__ import annotations

DOMAIN = "junos"
MANUFACTURER = "Juniper Networks"

CONF_PRIVATE_KEY = "private_key"
CONF_PASSPHRASE = "passphrase"
CONF_HOST_KEY = "host_key"
CONF_INTERFACES = "interfaces"

DEFAULT_PORT = 830
DEFAULT_USERNAME = "homeassistant"
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 3600

# Filesystems worth a "storage used" sensor (Junos has dozens of mounts; most are read-only package images).
STORAGE_MOUNTS = ("/", "/var", "/config", "/.mount", "/.mount/var", "/.mount/config", "/var/log", "/.mount/hostvar")
