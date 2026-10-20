"""Constants for the samsungtv_smart integration."""

from enum import Enum


class AppLoadMethod(Enum):
    """Valid application load methods."""

    All = 1
    Default = 2
    NotLoad = 3


class AppLaunchMethod(Enum):
    """Valid application launch methods."""

    Standard = 1
    Remote = 2
    Rest = 3


class PowerOnMethod(Enum):
    """Valid power on methods."""

    WOL = 1
    SmartThings = 2
    IPControl = 3


DOMAIN = "samsungtv_smart"

MIN_HA_MAJ_VER = 2025
MIN_HA_MIN_VER = 6
__min_ha_version__ = f"{MIN_HA_MAJ_VER}.{MIN_HA_MIN_VER}.0"

DATA_CFG = "cfg"
DATA_CFG_YAML = "cfg_yaml"
DATA_OPTIONS = "options"
DATA_ART_API = "art_api"  # Shared Frame Art API instance
CONF_IS_FRAME_TV = "is_frame_tv"  # Persisted flag: TV confirmed as Frame TV
# V7: persisted capability flags for the dedicated brightness / colour-temp
# WebSocket requests. On TVs that don't respond (e.g. Frame 2024) we learn
# this at runtime and remember it across restarts so we don't pay the probe
# cost on every start.
CONF_SUPPORTS_GET_BRIGHTNESS = "supports_get_brightness"
CONF_SUPPORTS_GET_COLOR_TEMPERATURE = "supports_get_color_temperature"
# V7: content list polling interval (seconds). The Frame Art sensor refreshes
# at SCAN_INTERVAL (15 s) for cheap state, but the full get_content_list call
# is now throttled to this longer cycle. Default 5 min, configurable via
# advanced options.
CONF_CONTENT_LIST_INTERVAL = "content_list_interval"
DEFAULT_CONTENT_LIST_INTERVAL = 300
MIN_CONTENT_LIST_INTERVAL = 30
MAX_CONTENT_LIST_INTERVAL = 3600
CONF_SLIDESHOW_API = "slideshow_api"  # Persisted: "slideshow" or "auto_rotation"
LOCAL_LOGO_PATH = "local_logo_path"
WS_PREFIX = "[Home Assistant]"

ATTR_DEVICE_MAC = "device_mac"
ATTR_DEVICE_MODEL = "device_model"
ATTR_DEVICE_NAME = "device_name"
ATTR_DEVICE_OS = "device_os"

CONF_APP_LAUNCH_METHOD = "app_launch_method"
CONF_APP_LIST = "app_list"
CONF_APP_LOAD_METHOD = "app_load_method"
CONF_CHANNEL_LIST = "channel_list"
CONF_DEVICE_MODEL = "device_model"
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_OS = "device_os"
CONF_DUMP_APPS = "dump_apps"
CONF_EXT_POWER_ENTITY = "ext_power_entity"
CONF_LOAD_ALL_APPS = "load_all_apps"
CONF_LOGO_OPTION = "logo_option"
CONF_PING_PORT = "ping_port"
CONF_POWER_ON_METHOD = "power_on_method"
CONF_SHOW_CHANNEL_NR = "show_channel_number"
CONF_SOURCE_LIST = "source_list"
CONF_SYNC_TURN_OFF = "sync_turn_off"
CONF_SYNC_TURN_ON = "sync_turn_on"
CONF_TOGGLE_ART_MODE = "toggle_art_mode"
CONF_USE_LOCAL_LOGO = "use_local_logo"
CONF_USE_MUTE_CHECK = "use_mute_check"
CONF_USE_ST_CHANNEL_INFO = "use_st_channel_info"
CONF_USE_ST_STATUS_INFO = "use_st_status_info"
CONF_WOL_REPEAT = "wol_repeat"
CONF_WS_NAME = "ws_name"

# for SmartThings integration api key usage
CONF_ST_ENTRY_UNIQUE_ID = "st_entry_unique_id"
CONF_USE_ST_INT_API_KEY = "use_st_int_api_key"  # obsolete used for migration
CONF_API_KEY = "api_key"
CONF_DEVICE_ID = "device_id"

# OAuth2 authentication
CONF_AUTH_METHOD = "auth_method"
CONF_OAUTH_TOKEN = "oauth_token"

# IP Control (JSON-RPC) — persisted access token, per TV (entry.data).
# Presence of a token means the feature is paired/enabled for that TV.
CONF_IP_CONTROL_TOKEN = "ip_control_token"
CONF_ENABLE_IP_CONTROL = "enable_ip_control"
# The TCP port the TV's IP Control JSON-RPC service listens on. Samsung uses
# different ports across model generations: 2020+ Frames listen on 1516, while
# the 2019 QLEDs (Q-series) listen on 1515. The port is auto-probed at pair
# time (see ipcontrol.IP_CONTROL_PORT_CANDIDATES) and persisted here per TV so
# the installer never has to know which model uses which port. Absent on
# entries paired before this field existed → falls back to the 1516 default.
CONF_IP_CONTROL_PORT = "ip_control_port"
# Whether IP Control's artModeControl/getTVStates are used for Art Mode
# detection and switching. Disabled by default: some firmwares (e.g. after a
# factory reset) leave artModeControl wedged "on" regardless of the actual
# panel state, causing false art_mode_status readings. Power on/off via IP
# Control (CONF_ENABLE_IP_CONTROL / CONF_POWER_ON_METHOD) is unaffected by
# this setting. Re-enable once the TV's firmware reports artModeControl
# correctly again.
CONF_IP_CONTROL_ART_MODE = "ip_control_art_mode"
# PROOS: the discrete token this panel's firmware uses for its TUNER input
# (read it from the ip_input_source attribute while the panel is on the
# real tuner). When set, selecting source "TV" sends this token instead.
CONF_IP_TUNER_TOKEN = "ip_tuner_token"

# Authentication methods
AUTH_METHOD_OAUTH = "oauth"
AUTH_METHOD_PAT = "pat"
AUTH_METHOD_ST_ENTRY = "st_entry"

# obsolete
CONF_UPDATE_METHOD = "update_method"
CONF_UPDATE_CUSTOM_PING_URL = "update_custom_ping_url"
CONF_SCAN_APP_HTTP = "scan_app_http"

DEFAULT_APP = "TV/HDMI"
DEFAULT_PORT = 8001
DEFAULT_SOURCE_LIST = {"TV": "KEY_TV", "HDMI": "KEY_HDMI"}
# ProOS standard discrete-input source map. Applied automatically as the source
# list for any TV that is IP-Control paired, so the installer gets working
# discrete HDMI 1-4 + broadcast tuner with no configuration. Each label maps to
# the IP_ source key the media_player routes through the IP Control
# inputSourceControl path (the dead KEY_HDMIn remote codes are never used).
# An explicit source list set in options still takes precedence over this.
STANDARD_IP_SOURCE_MAP = {
    "HDMI 1": "IP_HDMI1",
    "HDMI 2": "IP_HDMI2",
    "HDMI 3": "IP_HDMI3",
    "HDMI 4": "IP_HDMI4",
    "TV": "IP_TV",
}
DEFAULT_TIMEOUT = 6

MAX_WOL_REPEAT = 5

RESULT_NOT_SUCCESSFUL = "not_successful"
RESULT_NOT_SUPPORTED = "not_supported"
RESULT_ST_DEVICE_USED = "st_device_used"
RESULT_ST_DEVICE_NOT_FOUND = "st_device_not_found"
RESULT_ST_MULTI_DEVICES = "st_multiple_device"
RESULT_SUCCESS = "success"
RESULT_WRONG_APIKEY = "wrong_api_key"

SERVICE_SELECT_PICTURE_MODE = "select_picture_mode"

# Frame Art Extended Services
SERVICE_ART_GET_ARTMODE = "art_get_artmode"
SERVICE_ART_SET_ARTMODE = "art_set_artmode"
SERVICE_ART_AVAILABLE = "art_available"
SERVICE_ART_GET_CURRENT = "art_get_current"
SERVICE_ART_SELECT_IMAGE = "art_select_image"
SERVICE_ART_UPLOAD = "art_upload"
SERVICE_ART_DELETE = "art_delete"
SERVICE_ART_GET_THUMBNAIL = "art_get_thumbnail"
SERVICE_ART_GET_THUMBNAILS_BATCH = "art_get_thumbnails_batch"
SERVICE_ART_SET_BRIGHTNESS = "art_set_brightness"
SERVICE_ART_GET_BRIGHTNESS = "art_get_brightness"
SERVICE_ART_SET_COLOR_TEMPERATURE = "art_set_color_temperature"
SERVICE_ART_GET_COLOR_TEMPERATURE = "art_get_color_temperature"
SERVICE_ART_CHANGE_MATTE = "art_change_matte"
SERVICE_ART_SET_PHOTO_FILTER = "art_set_photo_filter"
SERVICE_ART_GET_PHOTO_FILTER_LIST = "art_get_photo_filter_list"
SERVICE_ART_GET_MATTE_LIST = "art_get_matte_list"
SERVICE_ART_SET_FAVOURITE = "art_set_favourite"
SERVICE_ART_SET_SLIDESHOW = "art_set_slideshow"
SERVICE_ART_SET_AUTO_ROTATION = "art_set_auto_rotation"

# Frame Art Service Attributes
ATTR_CONTENT_ID = "content_id"
ATTR_CATEGORY_ID = "category_id"
ATTR_FILE_PATH = "file_path"
ATTR_FILE_TYPE = "file_type"
ATTR_MATTE_ID = "matte_id"
ATTR_FILTER_ID = "filter_id"
ATTR_BRIGHTNESS = "brightness"
ATTR_COLOR_TEMPERATURE = "color_temperature"
ATTR_SHOW = "show"
ATTR_DURATION = "duration"
ATTR_SHUFFLE = "shuffle"
ATTR_ENABLED = "enabled"
ATTR_STATUS = "status"

SIGNAL_CONFIG_ENTITY = f"{DOMAIN}_config"

STD_APP_LIST = {
    "org.tizen.browser": {
        "st_app_id": "",
        "logo": "tizenbrowser.png",
        "name": "Internet",
    },
    "11101200001": {
        "st_app_id": "RN1MCdNq8t.Netflix",
        "logo": "netflix.png",
        "name": "Netflix",
    },
    "3201907018807": {
        "st_app_id": "org.tizen.netflix-app",
        "logo": "netflix.png",
        "name": "Netflix",
    },
    "111299001912": {
        "st_app_id": "9Ur5IzDKqV.TizenYouTube",
        "logo": "youtube.png",
        "name": "YouTube",
    },
    "3201512006785": {
        "st_app_id": "org.tizen.ignition",
        "logo": "primevideo.png",
        "name": "Prime Video",
    },
    "3201910019365": {
        "st_app_id": "org.tizen.primevideo",
        "logo": "primevideo.png",
        "name": "Prime Video",
    },
    "3201901017640": {
        "st_app_id": "MCmYXNxgcu.DisneyPlus",
        "logo": "disneyplus.png",
        "name": "Disney+",
    },
    "3202110025305": {
        "st_app_id": "rJyOSqC6Up.PPlusIntl",
        "logo": "paramountplus.png",
        "name": "Paramount+",
    },
    "11091000000": {
        "st_app_id": "4ovn894vo9.Facebook",
        "logo": "facebook.png",
        "name": "Facebook",
    },
    "3201806016390": {
        "st_app_id": "yu1NM3vHsU.DAZN",
        "logo": "dazn.png",
        "name": "DAZN",
    },
    "3201601007250": {
        "st_app_id": "QizQxC7CUf.PlayMovies",
        "logo": "",
        "name": "Google Play",
    },
    "3201606009684": {
        "st_app_id": "rJeHak5zRg.Spotify",
        "logo": "spotify.png",
        "name": "Spotify",
    },
    "3201512006963": {
        "st_app_id": "kIciSQlYEM.plex",
        "logo": "",
        "name": "Plex",
    },
    "com.samsung.tv.csfs": {
        "st_app_id": "",
        "logo": "",
        "name": "Smart Hub",
    },
}
