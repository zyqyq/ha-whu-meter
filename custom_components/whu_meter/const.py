"""Constants for the WHU Electricity Meter integration."""
from __future__ import annotations

DOMAIN = "whu_meter"

MANUFACTURER = "WHU 水电服务中心"
MODEL = "ICBS_V2 远传电表"

CONF_BASE_URL = "base_url"
CONF_AREA_ID = "area_id"
CONF_AREA_NAME = "area_name"
CONF_ARCHITECTURE_ID = "architecture_id"
CONF_ARCHITECTURE_NAME = "architecture_name"
CONF_FLOOR = "floor"
CONF_ROOM_NO = "room_no"
CONF_ROOM_NAME = "room_name"
CONF_METER_ID = "meter_id"
CONF_METER_ADDRESS = "meter_address"

CONF_SCAN_INTERVAL = "scan_interval_minutes"
CONF_BALANCE_WARN = "balance_warn_threshold"

# Low-balance email alert automation (owned & auto-managed by the integration)
AUTOMATION_ID = "whu_meter_balance_low_email"
CONF_EMAIL_ALERT_ENABLED = "email_alert_enabled"
CONF_EMAIL_NOTIFY_ENTITY = "email_notify_entity"
DEFAULT_EMAIL_NOTIFY_ENTITY = "notify.home_assistant_manager"

DEFAULT_BASE_URL = "http://zwhqbsd.whu.edu.cn"
DEFAULT_SCAN_INTERVAL = 15  # minutes
DEFAULT_BALANCE_WARN = 20.0  # CNY

# Server paths (relative to the ICBS_V2_Server root)
PATH_SIGNIN = "/v3/XINTFLg/SpecialSignIn"
PATH_AREA = "/v3/XINTF/GetAreaInfo"
PATH_ARCH = "/v3/XINTF/GetArchitectureInfo"
PATH_ROOM = "/v3/XINTF/GetRoomInfo"
PATH_ROOM_METER = "/v3/XINTF/GetRoomMeterInfo"
PATH_METER_INFO = "/v3/XINTF/GetMeterInfo"
PATH_RESERVE = "/v3/XINTF/GetReserve"
PATH_DAY_VALUE = "/v3/XINTF/GetMeterDayValue"

SIGNIN_ACCOUNT = "ph"
SIGNIN_PASSWORD = "phAPI"
SIGNIN_SYS_ID = 1
