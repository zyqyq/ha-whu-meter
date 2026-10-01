"""Auto-manage the low-balance email alert automation.

The integration owns a single automation (id: whu_meter_balance_low_email).
On every integration setup / options update it rewrites that automation in
`automations.yaml` (visible and editable in the HA UI), pointing at the
entity ids registered by this config entry and the SMTP notify entity
chosen in the options flow. When the alert is disabled (or the entry is
removed) the automation is removed again.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml, save_yaml

from .const import (
    AUTOMATION_ID,
    CONF_EMAIL_NOTIFY_ENTITY,
    CONF_ROOM_NAME,
    DEFAULT_BALANCE_WARN,
    DEFAULT_EMAIL_NOTIFY_ENTITY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# entity-registry unique_id suffix -> template token
_SUFFIX_TOKENS = {
    "balance": "__SENS_BALANCE__",
    "balance_low": "__BS_LOW__",
    "yesterday": "__SENS_YESTERDAY__",
    "today": "__SENS_TODAY__",
    "cumulative": "__SENS_CUMUL__",
    "lastread": "__SENS_LASTREAD__",
}


def _unique_id(entry, suffix: str) -> str:
    return f"whu_meter_{entry.data['meter_id']}_{suffix}"


def _registry_entity_ids(hass: HomeAssistant, entry) -> dict[str, str]:
    """Resolve entity ids of this config entry from the entity registry."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    found: dict[str, str] = {}
    for reg in registry.entities.values():
        if reg.platform != DOMAIN or reg.config_entry_id != entry.entry_id:
            continue
        for suffix, token in _SUFFIX_TOKENS.items():
            if reg.unique_id == _unique_id(entry, suffix):
                found[token] = reg.entity_id
    return found


def _load_email_template() -> str:
    path = Path(__file__).parent / "email_template.html"
    return path.read_text(encoding="utf-8")


def _build_automation(entry, entity_ids: dict[str, str], options: dict[str, Any]) -> dict[str, Any]:
    room = entry.data.get(CONF_ROOM_NAME) or entry.data.get("room_no", "")
    notify_entity = options.get(CONF_EMAIL_NOTIFY_ENTITY, DEFAULT_EMAIL_NOTIFY_ENTITY)
    threshold = options.get("balance_warn_threshold", DEFAULT_BALANCE_WARN)

    html = _load_email_template()
    for token, entity_id in entity_ids.items():
        html = html.replace(token, entity_id)
    html = html.replace("__ROOM__", room).replace("__THRESHOLD__", str(threshold))

    title = (
        f"电费余额过低警报 · {room} · "
        f"仅剩 ¥{{{{ states('{entity_ids['__SENS_BALANCE__']}') }}}} 元"
    )
    message = (
        f"【武大宿舍电费警报】{room} 当前余额 {{{{ states('{entity_ids['__SENS_BALANCE__']}') }}}} 元，"
        f"已低于告警阈值 {{{{ state_attr('{entity_ids['__BS_LOW__']}','告警阈值') or {threshold} }}}} 元，"
        f"昨日用电 {{{{ states('{entity_ids['__SENS_YESTERDAY__']}') }}}} 度，"
        f"请尽快前往水电服务平台充值，避免断电。"
    )

    return {
        "id": AUTOMATION_ID,
        "alias": f"电费余额过低邮件警报（{room}）",
        "description": (
            "由 whu_meter 集成自动创建并维护：余额过低 binary_sensor 变为 on 时，"
            f"通过 smtp.send_message（目标实体 {notify_entity}）发送 HTML 警告邮件。"
            "充值后余额回升再跌破会再次触发。可在集成选项中关闭或更换通知实体。"
        ),
        "mode": "single",
        "trigger": [
            {"platform": "state", "entity_id": entity_ids["__BS_LOW__"], "to": "on"}
        ],
        "action": [
            {
                "action": "smtp.send_message",
                "target": {"entity_id": notify_entity},
                "data": {
                    "title": title,
                    "message": message,
                    "html": html,
                },
            }
        ],
    }


def _sync_file(hass: HomeAssistant, automation_cfg: dict[str, Any] | None) -> None:
    """Rewrite automations.yaml: insert/update/remove our automation by id."""
    path = hass.config.path("automations.yaml")
    try:
        config: list = load_yaml(path) or []
    except FileNotFoundError:
        config = []
    if not isinstance(config, list):
        config = []

    config = [item for item in config if not (isinstance(item, dict) and item.get("id") == AUTOMATION_ID)]
    if automation_cfg is not None:
        config.append(automation_cfg)

    save_yaml(path, config)


async def async_sync_alert_automation(hass: HomeAssistant, entry) -> None:
    """Create / update / remove the alert automation to match current options."""
    options = entry.options
    enabled = options.get("email_alert_enabled", True)
    notify_entity = options.get(CONF_EMAIL_NOTIFY_ENTITY, DEFAULT_EMAIL_NOTIFY_ENTITY)

    if not enabled:
        await hass.async_add_executor_job(_sync_file, hass, None)
        await _async_reload_automations(hass)
        _LOGGER.info("whu_meter: 邮件警报已关闭，自动化已从 automations.yaml 移除")
        return

    entity_ids = await hass.async_add_executor_job(_registry_entity_ids, hass, entry)
    missing = [tok for tok in _SUFFIX_TOKENS.values() if tok not in entity_ids]
    if missing:
        _LOGGER.warning("whu_meter: 无法创建邮件警报自动化，缺少实体: %s", missing)
        return
    if not notify_entity:
        _LOGGER.warning("whu_meter: 未配置 SMTP 通知实体，无法创建邮件警报自动化")
        return

    automation_cfg = await hass.async_add_executor_job(_build_automation, entry, entity_ids, options)
    await hass.async_add_executor_job(_sync_file, hass, automation_cfg)
    await _async_reload_automations(hass)
    _LOGGER.info("whu_meter: 邮件警报自动化已同步到 automations.yaml（通知实体 %s）", notify_entity)


async def _async_reload_automations(hass: HomeAssistant) -> None:
    """Reload the automation component so changes take effect."""
    if "automation" not in hass.config.components:
        _LOGGER.warning("whu_meter: automation 组件未加载，跳过重载")
        return
    await hass.services.async_call("automation", "reload", blocking=True)
