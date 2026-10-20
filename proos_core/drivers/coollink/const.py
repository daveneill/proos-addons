"""Constants for the CoolLink (CoolAutomation) integration."""

DOMAIN = "coollink"

DEFAULT_PORT = 10102
DEFAULT_HOST = "192.168.1.12"  # Dave's CoolLinkBridge (editable in the config flow)

CONF_SUPPORTED_MODES = "supported_modes"
CONF_SEND_WAKEUP_PROMPT = "send_wakeup_prompt"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_HALF_STEPS = "half_steps"

DEFAULT_MIN_TEMP = 16
DEFAULT_MAX_TEMP = 30

# How often to poll the hub. Kept short so changes made at the physical wall
# controller / IR remote show up on the dashboard promptly (the hub reflects them
# in its next `ls2`). Each poll is one lightweight TCP round-trip for all units.
DEFAULT_SCAN_INTERVAL = 10  # seconds

MAX_RETRIES = 3
BACKOFF_BASE_DELAY = 2
