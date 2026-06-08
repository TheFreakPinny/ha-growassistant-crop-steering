"""Constants for the GrowAssistant Crop Steering integration."""

from __future__ import annotations

DOMAIN = "growassistant_crop_steering"
NAME = "GrowAssistant – Crop Steering"
VERSION = "0.1.0"

DEFAULT_NAME = NAME
SERVICE_REFRESH = "refresh"

CONF_PUMP_SWITCH = "pump_switch"
CONF_LED_DAY_SENSOR = "led_day_sensor"
CONF_VWC_SENSOR = "vwc_sensor"
CONF_DRAIN_SENSOR = "drain_sensor"
CONF_DRAIN_TRAY_SENSOR = "drain_tray_sensor"

CONF_LED_SUNRISE = "led_sunrise"
CONF_LED_SUNSET = "led_sunset"

CONF_P1_MODE = "p1_mode"
CONF_P2_MODE = "p2_mode"

CONF_P0_TRANSPIRATION_MIN = "p0_transpiration_min"
CONF_P1_DURATION_MIN = "p1_duration_min"
CONF_P1_INTERVAL_MIN = "p1_interval_min"
CONF_P1_SHOT_DURATION_S = "p1_shot_duration_s"
CONF_P2_INTERVAL_MIN = "p2_interval_min"
CONF_P2_SHOT_DURATION_S = "p2_shot_duration_s"
CONF_P2_SHOTS = "p2_shots"

CONF_P1_SOAK_MIN = "p1_soak_min"
CONF_P2_SOAK_MIN = "p2_soak_min"
CONF_P2_END_OFFSET_MIN = "p2_end_offset_min"
CONF_FIELD_CAPACITY_VWC = "field_capacity_vwc"
CONF_P1_START_VWC = "p1_start_vwc"
CONF_P2_VWC_DROP = "p2_vwc_drop"
CONF_P2_REF_VWC = "p2_ref_vwc"
CONF_VWC_CAP = "vwc_cap"
CONF_VWC_HYST = "vwc_hyst"

CONF_P1_ACTIVE = "p1_active"
CONF_P1_DONE = "p1_done"
CONF_P1_WINDOW_OPENED_TODAY = "p1_window_opened_today"

CONF_P1_SHOTS_DONE = "p1_shots_done"
CONF_P2_SHOTS_DONE = "p2_shots_done"

CONF_LAST_SHOT = "last_shot"

CONFIG_ENTITY_KEYS = (
    CONF_PUMP_SWITCH,
    CONF_LED_DAY_SENSOR,
    CONF_VWC_SENSOR,
    CONF_DRAIN_SENSOR,
    CONF_DRAIN_TRAY_SENSOR,
    CONF_LED_SUNRISE,
    CONF_LED_SUNSET,
    CONF_P1_MODE,
    CONF_P2_MODE,
    CONF_P0_TRANSPIRATION_MIN,
    CONF_P1_DURATION_MIN,
    CONF_P1_INTERVAL_MIN,
    CONF_P1_SHOT_DURATION_S,
    CONF_P2_INTERVAL_MIN,
    CONF_P2_SHOT_DURATION_S,
    CONF_P2_SHOTS,
    CONF_P1_SOAK_MIN,
    CONF_P2_SOAK_MIN,
    CONF_P2_END_OFFSET_MIN,
    CONF_FIELD_CAPACITY_VWC,
    CONF_P1_START_VWC,
    CONF_P2_VWC_DROP,
    CONF_P2_REF_VWC,
    CONF_VWC_CAP,
    CONF_VWC_HYST,
    CONF_P1_ACTIVE,
    CONF_P1_DONE,
    CONF_P1_WINDOW_OPENED_TODAY,
    CONF_P1_SHOTS_DONE,
    CONF_P2_SHOTS_DONE,
    CONF_LAST_SHOT,
)