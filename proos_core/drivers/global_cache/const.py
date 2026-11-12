"""Global Caché — constants (register 1323)."""
DOMAIN = "global_cache"
CONF_MODEL = "model"
CONF_MAC = "mac"
MANUFACTURER = "Global Caché"
# plumbing: how often relay and sensor states are read on a unit that cannot push them (an iTach; a GC-100 whose
# sensors are not set to auto-notify). A Global Connect pushes its sensors on the open connection ("notify").
POLL_SECONDS = 5
# the platform's own NECCommand default carrier; used only when a command carries none (infrared_protocols)
DEFAULT_CARRIER = 38000
PLATFORMS = ["infrared", "switch", "binary_sensor", "select"]
IR_SEND_MODES = ("IR", "IR_BLASTER", "IR_NOCARRIER", "BL2_BLASTER", "IRTRIPORT", "IRTRIPORT_BLASTER")
IR_SENSOR_MODES = ("SENSOR", "SENSOR_NOTIFY")
