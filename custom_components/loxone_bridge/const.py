"""Constants for the Loxone Bridge integration."""

DOMAIN = "loxone_bridge"

# Configuration keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_USE_TLS = "use_tls"
CONF_VERIFY_SSL = "verify_ssl"
CONF_SYNC_HA_TO_LOXONE = "sync_ha_to_loxone"
CONF_SYNC_LOXONE_TO_HA = "sync_loxone_to_ha"

# Defaults
DEFAULT_PORT = 443
DEFAULT_USE_TLS = True
DEFAULT_VERIFY_SSL = False  # Loxone Miniservers typically use self-signed certificates
DEFAULT_NAME = "Loxone Miniserver"

# Loxone WebSocket commands
LOXONE_CMD_GET_STRUCTURE = "data/LoxAPP3.json"
LOXONE_CMD_ENABLE_STATUS_UPDATE = "jdev/sps/enablebinstatusupdate"
LOXONE_CMD_KEY_EXCHANGE = "jdev/sys/keyexchange/"
LOXONE_CMD_GET_KEY = "jdev/sys/getkey2/"
LOXONE_CMD_AUTHENTICATE = "authenticate/"
LOXONE_CMD_GET_TOKEN = "jdev/sys/gettoken/"

# Loxone control types → HA platforms
LOXONE_CONTROL_MAP = {
    "Switch": "switch",
    "Pushbutton": "switch",
    "TimedSwitch": "switch",
    "Light": "light",
    "LightController": "light",
    "LightControllerV2": "light",
    "Dimmer": "light",
    "ColorPickerV2": "light",
    "Jalousie": "cover",
    "Gate": "cover",
    "Window": "cover",
    "InfoOnlyAnalog": "sensor",
    "InfoOnlyDigital": "binary_sensor",
    "TextState": "sensor",
    "Meter": "sensor",
    "EnergyFlowMonitor": "sensor",
    "IRoomController": "climate",
    "IRoomControllerV2": "climate",
    "Ventilation": "fan",
    "Alarm": "binary_sensor",
    "SmokeAlarm": "binary_sensor",
    "PresenceDetector": "binary_sensor",
}

# Platforms supported by this integration
PLATFORMS = ["light", "switch", "cover", "sensor", "binary_sensor", "climate", "fan"]

# WebSocket message types (binary header)
LOXONE_MSG_TEXT = 0
LOXONE_MSG_BINARY = 1
LOXONE_MSG_VALUE_STATES = 2
LOXONE_MSG_TEXT_STATES = 3
LOXONE_MSG_DAYTIMER_STATES = 4
LOXONE_MSG_OUT_OF_SERVICE = 5
LOXONE_MSG_KEEPALIVE = 6
LOXONE_MSG_WEATHER_STATES = 7

# Webhook
WEBHOOK_ID = "loxone_bridge_webhook"

# State update interval (fallback polling, seconds)
UPDATE_INTERVAL = 60

# Connection
RECONNECT_DELAY = 5
MAX_RECONNECT_ATTEMPTS = 10

# Attributes
ATTR_UUID = "loxone_uuid"
ATTR_ROOM = "loxone_room"
ATTR_CATEGORY = "loxone_category"
ATTR_CONTROL_TYPE = "loxone_control_type"

# Events
EVENT_LOXONE_COMMAND = "loxone_bridge_command"
EVENT_LOXONE_STATE = "loxone_bridge_state_update"
