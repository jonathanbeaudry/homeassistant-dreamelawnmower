"""UI configuration adapter for the existing guarded mowing engine."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.core import CoreState, Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.trigger import (
    async_initialize_triggers,
    async_validate_trigger_config,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .device_settings_control import device_settings_section
from .dreame_lawn_mower_client.position_tracking import snapshot_is_docked
from .managed_mowing_state import TASK_SCHEMA
from .mowing_automation_config import (
    CONF_MOWING_AUTOMATION,
    ROTATION_SETTINGS,
    automation_config,
    zone_rest_days_by_id,
)
from .mowing_conditions import (
    MowingConditions,
    do_not_disturb_end,
    next_do_not_disturb_start,
)
from .option_updates import EntryUpdateSnapshot
from .sensor_zone_history import current_zone_overview, selected_map_revision
from .zone_rotation import due_zone_ids, rotation_zones

_LOGGER = logging.getLogger(__name__)
# Starts after recharge within one Schedule block, at most this many times.
MAX_BLOCK_CONTINUATIONS = 8
# Skips meaning the mower is busy with its own task (away from the dock,
# mowing, or holding an unfinished task it resumes after charging).
_MOWER_BUSY_REASONS = frozenset(
    {"not_docked", "active_or_unknown_task", "resumable_or_unknown_task"}
)
# A continuation ends on any outcome other than these.
_CONTINUATION_KEEPS = frozenset({"submitted", "started", "deferred", "paused"})

# Settings controls may bind too; only these three gate the schedule trigger.
REQUIRED_BINDINGS = frozenset({"permission", "enabled", "mower"})


def automation_runtime(coordinator: Any) -> MowingAutomation | None:
    """Share one lifecycle adapter between its mower and local helper entities."""
    runtime: MowingAutomation | None = getattr(coordinator, "mowing_automation", None)
    if runtime is None:
        config = automation_config(
            coordinator.entry.options.get(CONF_MOWING_AUTOMATION)
        )
        if config:
            runtime = MowingAutomation(coordinator, config)
            coordinator.mowing_automation = runtime
    return runtime


class MowingAutomation:
    """Adapt UI inputs into one owned request per real Schedule block start.

    No startup catch-up, browser timer, separate retry engine or mower settings
    writes are introduced here. ManagedMowing remains the dispatch authority.
    """

    def __init__(self, coordinator: Any, config: dict[str, Any]) -> None:
        self.coordinator = coordinator
        self.hass = coordinator.hass
        self.config = config
        self.enabled: bool = config["enabled"]
        self.conditions = MowingConditions(config)
        self.permitted = False
        self.reason = "Waiting for garden observations"
        self.entities: dict[str, Any] = {}
        self._closed = False
        self._started = False
        self._unsub: list[Callable[[], None]] = []
        self._setup_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._trigger_error: str | None = None
        self.last_outcome: dict[str, Any] | None = None
        # Block end (the Schedule's next_event while on) of the continuation
        # armed by a finished run, and how many continuations that block had.
        self._continuation_block: str | None = None
        self._continuation_armed = False
        self._continuations = 0
        # Deadline of a start waiting for zone history after an integration
        # reload, and whether that start was a continuation.
        self._history_wait: tuple[datetime, bool] | None = None
        # Last time budget computed for a start or the plan, for dashboards.
        self._budget: dict[str, Any] = {}

    @callback
    def bind(self, kind: str, entity: Any) -> None:
        """Wait for actual registered entity IDs before attaching triggers."""
        self.entities[kind] = entity
        if (
            REQUIRED_BINDINGS <= self.entities.keys()
            and not self._started
            and not self._closed
        ):
            self._started = True
            self._setup_task = self.hass.async_create_task(self._async_start())

    async def _async_start(self) -> None:
        self._unsub.extend(
            [
                self.hass.bus.async_listen("state_changed", self._state_changed),
                self.hass.bus.async_listen(f"{DOMAIN}_scheduled_run", self._outcome),
                async_track_time_interval(
                    self.hass, self._evaluate, timedelta(seconds=15)
                ),
            ]
        )
        self._evaluate()
        if self.hass.state is CoreState.running:
            await self._attach()
        else:
            self._unsub.append(async_at_started(self.hass, self._attach))

    async def _attach(self, _: Any = None) -> None:
        if self._closed or not self.enabled or not self.config["schedule_entity"]:
            return
        try:
            config = await async_validate_trigger_config(
                self.hass,
                [
                    {
                        "platform": "schedule.block_started",
                        "target": {"entity_id": self.config["schedule_entity"]},
                    }
                ],
            )
            remove = await async_initialize_triggers(
                self.hass,
                config,
                self._run,
                DOMAIN,
                "Mowing automation",
                _LOGGER.log,
            )
            if remove is None:
                raise HomeAssistantError("Schedule trigger could not be attached")
            if self._closed:
                remove()
            else:
                self._unsub.append(remove)
        except (HomeAssistantError, ValueError, vol.Invalid) as err:
            self._trigger_error = f"Schedule unavailable: {err}"
            self._evaluate()
            _LOGGER.error("Cannot attach mowing automation schedule: %s", err)

    @callback
    def _state_changed(self, event: Event) -> None:
        if event.data.get("entity_id") in {
            self.config["soil_entity"],
            self.config["irrigation_entity"],
            self.config["permission_entity"],
        }:
            self._evaluate()

    @callback
    def _evaluate(self, _: Any = None) -> None:
        if self._closed:
            return
        previous = (self.permitted, self.reason)
        self.permitted, self.reason = self.conditions.evaluate(
            self.hass,
            dt_util.utcnow(),
            device_settings_section(getattr(self.coordinator, "device_settings", None)),
        )
        if self._trigger_error:
            self.permitted, self.reason = False, self._trigger_error
        permission = self.entities.get("permission")
        if permission is not None:
            # Heartbeat report; source freshness is checked by MowingConditions.
            permission.async_write_ha_state()
        if previous != (self.permitted, self.reason):
            self.coordinator.async_update_listeners()
        self._maybe_retry_history()
        self._maybe_continue()

    async def _run(self, variables: dict[str, Any], context: Any = None) -> None:
        if self._closed or not self.enabled or self._run_task is not None:
            return
        continuation = bool(variables.get("continuation"))
        if not continuation:
            # A real block start begins a new block: no continuation yet.
            self._continuation_block = self._current_block()
            self._continuation_armed = False
            self._continuations = 0
        manager = getattr(self.coordinator, "managed_mowing", None)
        if manager is None:
            return
        self._run_task = asyncio.current_task()
        try:
            self._evaluate()
            occurrence = dt_util.utcnow().strftime("%Y-%m-%dT%H:%M")
            request: dict[str, Any] = {
                "task_type": "all",
                "minimum_battery": self.config["minimum_battery"],
                "permission_entity": self.entities["permission"].entity_id,
                "permission_max_age": 60,
                "defer_minutes": self.config["defer_minutes"],
                "interruption": self.config["interruption"],
                "resume_when_allowed": self.config["resume_when_allowed"],
                "recovery_minutes": self.config["recovery_minutes"],
                "automation_entity": self.entities["enabled"].entity_id,
                "run_id": (
                    f"ui:{self.coordinator.entry.entry_id}:{occurrence}"
                    + (f":c{self._continuations}" if continuation else "")
                ),
            }
            if self.config["mowing_target"] == "rotation":
                selection = self._rotation_target()
                if selection == "zone_history_unavailable" and self._wait_for_history(
                    continuation
                ):
                    return
                self._history_wait = None
                if isinstance(selection, str):
                    manager.skip(
                        dict(request, task_type="zone", zone_ids=[]), selection
                    )
                    return
                request.update(selection)
            await manager.async_request(TASK_SCHEMA(request))
        finally:
            self._run_task = None

    def _current_block(self) -> str | None:
        """The active Schedule block, named by its end, or None outside one."""
        state = self.hass.states.get(self.config["schedule_entity"] or "")
        if state is None or state.state != "on":
            return None
        end = state.attributes.get("next_event")
        return str(end) if end else "open"

    def _wait_for_history(self, continuation: bool) -> bool:
        """Hold a start while zone history reloads, within the wait window.

        An integration reload or restart leaves zone history unread for a few
        minutes; a start falling in that gap waits for it like any deferred
        start, and is skipped once the wait window has passed.
        """
        wait = self.config["defer_minutes"]
        if wait <= 0:
            return False
        now = dt_util.utcnow()
        if self._history_wait is None:
            self._history_wait = (now + timedelta(minutes=wait), continuation)
            self.coordinator.async_update_listeners()
            return True
        if now < self._history_wait[0]:
            return True
        self._history_wait = None
        return False

    @callback
    def _maybe_retry_history(self) -> None:
        """Resume a start held for zone history once it is readable or late."""
        if (
            self._history_wait is None
            or self._closed
            or not self.enabled
            or self._run_task is not None
        ):
            return
        deadline, continuation = self._history_wait
        if (
            dt_util.utcnow() < deadline
            and self._rotation_target() == "zone_history_unavailable"
        ):
            return
        self.hass.async_create_task(
            self._run({"continuation": continuation}), "mowing automation history wait"
        )

    @callback
    def _maybe_continue(self) -> None:
        """Start again after recharge while the same block is still open.

        Only a finished run of this automation arms it; the guarded request
        still checks battery, permission, rain and native schedules itself.
        """
        if (
            not self._continuation_armed
            or not self.config.get("continue_in_block")
            or self._closed
            or not self.enabled
            or self._run_task is not None
        ):
            return
        if (
            self._current_block() != self._continuation_block
            or self._continuations >= MAX_BLOCK_CONTINUATIONS
        ):
            self._continuation_armed = False
            return
        manager = getattr(self.coordinator, "managed_mowing", None)
        snapshot = getattr(self.coordinator, "data", None)
        battery = getattr(snapshot, "battery_level", None)
        if (
            manager is None
            or getattr(manager, "summary", None) is not None
            or not snapshot_is_docked(snapshot)
            or not isinstance(battery, int | float)
            or battery < self.config["minimum_battery"]
        ):
            return
        # Disarm before scheduling: Home Assistant starts tasks eagerly, and
        # _run owns _run_task, so nothing here may mark the run afterwards.
        self._continuation_armed = False
        self._continuations += 1
        self.hass.async_create_task(
            self._run({"continuation": True}), "mowing automation continuation"
        )

    def _rotation_map_index(self) -> int | None:
        """The selected map only while the inventory verifies its revision."""
        coordinator = self.coordinator
        map_index = getattr(coordinator, "selected_map_index", None)
        if type(map_index) is int and selected_map_revision(
            coordinator.app_maps, map_index,
        ) is not None:
            return map_index
        return None

    def _rotation_overview(self) -> tuple[list[dict[str, Any]], int] | str:
        """Zone history of the verified selected map, or why it is unusable."""
        coordinator = self.coordinator
        history = getattr(coordinator, "zone_mowing_sessions", None)
        map_index = self._rotation_map_index()
        if (
            history is None
            or not history.complete
            or not history.attributable
            or map_index is None
        ):
            return "zone_history_unavailable"
        overview = current_zone_overview(coordinator)
        if not overview:
            return "zone_history_unavailable"
        return overview, map_index

    def _window_end(self, at: datetime) -> datetime | None:
        """When mowing must be over: the block end or the next Do Not Disturb start.

        The block end is known only while the block is on (its next_event).
        """
        ends: list[datetime] = []
        state = self.hass.states.get(self.config["schedule_entity"] or "")
        if state is not None and state.state == "on":
            end = dt_util.parse_datetime(str(state.attributes.get("next_event") or ""))
            if end is not None:
                ends.append(end)
        if self.config.get("outside_dnd"):
            dnd = next_do_not_disturb_start(
                device_settings_section(
                    getattr(self.coordinator, "device_settings", None)
                ),
                at,
            )
            if dnd is not None:
                ends.append(dnd)
        return min(ends) if ends else None

    def _area_budget(self, at: datetime) -> float | str | None:
        """Area mowable before the window ends at the measured rate, or why not."""
        self._budget = {"window_end": None, "mowing_rate_m2_h": None, "area_m2": None}
        if not self.config.get("finish_before_window_end"):
            return None
        history = getattr(self.coordinator, "zone_mowing_sessions", None)
        rate: float | None = history.mowing_rate() if history is not None else None
        if rate is None:
            return "mowing_rate_unknown"
        end = self._window_end(at)
        self._budget["mowing_rate_m2_h"] = round(rate, 1)
        if end is None:
            return None
        # Inside the mower's Do Not Disturb window the start waits for its
        # end: the time left counts from then, not from now.
        begin = at
        if self.config.get("outside_dnd"):
            begin = (
                do_not_disturb_end(
                    device_settings_section(
                        getattr(self.coordinator, "device_settings", None)
                    ),
                    at,
                )
                or at
            )
        budget = max(0.0, (end - begin).total_seconds() / 3600 * rate)
        self._budget["window_end"] = end.isoformat()
        self._budget["area_m2"] = round(budget, 1)
        return budget

    def _rotation_target(
        self, today: date | None = None, at: datetime | None = None
    ) -> dict[str, Any] | str:
        """Due whole zones of the selected map, or why none can be chosen.

        The scheduled start and the dashboard preview both call this, so the
        preview cannot announce zones the start would not choose.
        """
        usable = self._rotation_overview()
        if isinstance(usable, str):
            return usable
        overview, map_index = usable
        budget = self._area_budget(at or dt_util.utcnow())
        if isinstance(budget, str):
            return budget
        selection = {
            "rest_days": self.config["rotation_rest_days"],
            "max_area": self.config["rotation_max_area"],
            "today": today or dt_util.now().date(),
            "zone_rest_days_by_id": zone_rest_days_by_id(self.config, map_index),
        }
        zone_ids = due_zone_ids(overview, area_budget=budget, **selection)
        if not zone_ids:
            # Due zones that do not fit the time left are not "nothing due".
            if budget is not None and due_zone_ids(overview, **selection):
                return "no_time_left"
            return "no_zone_due"
        return {"task_type": "zone", "zone_ids": zone_ids, "map_index": map_index}

    @callback
    def _outcome(self, event: Event) -> None:
        owner = self.entities.get("enabled")
        if owner is None or event.data.get("automation_entity") != owner.entity_id:
            return
        self.last_outcome = {
            key: event.data.get(key)
            for key in ("status", "reason", "detail", "observed_at")
        }
        status = event.data.get("status")
        native_pending = (
            status == "skipped" and event.data.get("reason") in _MOWER_BUSY_REASONS
        )
        if status == "finished" or native_pending:
            # A finished run, or a mower busy with its own task (it finishes
            # it first, e.g. native resume after charging), lets this block
            # start again once it is back on the dock with the minimum battery.
            block = self._current_block()
            self._continuation_armed = bool(
                self.config.get("continue_in_block")
                and block is not None
                and block == self._continuation_block
            )
        elif status not in _CONTINUATION_KEEPS:
            self._continuation_armed = False
        self.coordinator.async_update_listeners()
        if self.config["notify_outcome"]:
            persistent_notification.async_create(
                self.hass,
                f"{event.data.get('status', '')}: "
                f"{event.data.get('reason', '').replace('_', ' ')}. "
                f"{event.data.get('detail') or ''}",
                title="Mowing automation",
                notification_id=f"{DOMAIN}_{self.coordinator.entry.entry_id}_automation",
            )

    async def async_cancel_owned(self) -> None:
        """Revoke only this wizard's intent before replacing its configuration."""
        self.enabled = False
        self._history_wait = None
        owner = self.entities.get("enabled")
        if owner is not None:
            owner.async_write_ha_state()
        manager = getattr(self.coordinator, "managed_mowing", None)
        if owner is not None and manager is not None:
            await manager.async_cancel_owner(owner.entity_id)

    async def async_set_enabled(self, enabled: bool) -> None:
        """Persist an opt-in toggle; disabling revokes intent without movement."""
        if enabled and (
            not self.config["schedule_entity"]
            or not self.config["native_plans_disabled"]
        ):
            raise HomeAssistantError(
                "Confirm disabled native plans in Mowing automation setup first"
            )
        if not enabled:
            await self.async_cancel_owned()
        options = dict(self.coordinator.entry.options)
        config = dict(options[CONF_MOWING_AUTOMATION])
        config["enabled"] = enabled
        options[CONF_MOWING_AUTOMATION] = config
        self.hass.config_entries.async_update_entry(
            self.coordinator.entry, options=options
        )
        self.enabled = enabled
        self.entities["enabled"].async_write_ha_state()

    async def async_set_rotation(self, **changes: Any) -> None:
        """Persist rotation choices in place; the next start reads them.

        Unlike the wizard, these settings never need a reload: the runtime
        applies them at once and records the entry as already applied.
        """
        if set(changes) - ROTATION_SETTINGS:
            raise HomeAssistantError("Only rotation settings change in place")
        entry = self.coordinator.entry
        config = automation_config(
            {**entry.options[CONF_MOWING_AUTOMATION], **changes}
        )
        if not config:
            raise HomeAssistantError("Invalid mowing automation setting")
        options = dict(entry.options)
        options[CONF_MOWING_AUTOMATION] = config
        applied_changes = {key: config[key] for key in changes}
        self.config = {**self.config, **applied_changes, "enabled": self.enabled}
        applied = getattr(self.coordinator, "applied_entry_update", None)
        if isinstance(applied, EntryUpdateSnapshot):
            # Acknowledge only the settings applied here. Pending switch,
            # connection or polling changes still belong to the entry listener.
            applied_options = deepcopy(applied.options)
            applied_options[CONF_MOWING_AUTOMATION] = {
                **applied_options.get(CONF_MOWING_AUTOMATION, {}),
                **{key: self.config[key] for key in ROTATION_SETTINGS},
            }
            self.coordinator.applied_entry_update = EntryUpdateSnapshot(
                deepcopy(applied.data), applied_options
            )
        self.hass.config_entries.async_update_entry(entry, options=options)
        self.coordinator.async_update_listeners()

    async def async_set_zone_rest_days(
        self, zone_id: int, rest_days: int | None, *, map_index: int | None = None,
    ) -> None:
        """Give one zone its own rest days, or None to use the default again."""
        selected = self._rotation_map_index()
        if selected is None or (map_index is not None and map_index != selected):
            raise HomeAssistantError("The selected map changed or is not verified")
        if rest_days is not None and not any(
            zone["zone_id"] == zone_id
            for zone in current_zone_overview(self.coordinator)
        ):
            raise HomeAssistantError("The zone does not belong to the selected map")
        maps = deepcopy(self.config.get("rotation_zone_rest_days", {}))
        overrides = maps.setdefault(str(selected), {})
        if rest_days is None:
            overrides.pop(str(zone_id), None)
        else:
            overrides[str(zone_id)] = rest_days
        if not overrides:
            maps.pop(str(selected), None)
        await self.async_set_rotation(rotation_zone_rest_days=maps)

    def zone_schedule(self) -> list[dict[str, Any]]:
        """Each zone's rest days and next due date, from the start's own rules."""
        usable = self._rotation_overview()
        if isinstance(usable, str):
            return []
        return rotation_zones(
            usable[0],
            rest_days=self.config["rotation_rest_days"],
            overrides=zone_rest_days_by_id(self.config, usable[1]),
        )

    def plan(self) -> dict[str, Any]:
        """What the next Schedule block start would request, if it began now.

        Native mower checks, permission and battery still apply at the start.
        """
        state = self.hass.states.get(self.config["schedule_entity"] or "")
        next_start = None
        if state is not None and state.state == "off":
            next_start = dt_util.parse_datetime(
                str(state.attributes.get("next_event") or "")
            )
        result: dict[str, Any] = {
            "next_start": next_start.isoformat() if next_start else None,
            "task_type": "all",
            "zones": [],
            "area_m2": None,
            "reason": None,
        }
        if self.config["mowing_target"] != "rotation":
            return result
        day = dt_util.as_local(next_start).date() if next_start else None
        target = self._rotation_target(day, next_start)
        result["task_type"] = "zone"
        if isinstance(target, str):
            result["reason"] = target
            return result
        overview = {
            item["zone_id"]: item for item in current_zone_overview(self.coordinator)
        }
        zones = [
            {
                "zone_id": zone_id,
                "label": overview.get(zone_id, {}).get("label"),
                "area_m2": overview.get(zone_id, {}).get("area_m2"),
            }
            for zone_id in target["zone_ids"]
        ]
        areas = [zone["area_m2"] for zone in zones]
        result["zones"] = zones
        if all(isinstance(area, int | float) for area in areas):
            result["area_m2"] = round(sum(areas), 1)
        return result

    @property
    def summary(self) -> dict[str, Any]:
        """Advertise UI capabilities and actual entity IDs to thin dashboards."""
        return {
            "version": 1,
            "configured": True,
            "enabled": self.enabled,
            "permitted": self.permitted,
            "reason": self.reason,
            "schedule_entity": self.config["schedule_entity"],
            "permission_entity": getattr(
                self.entities.get("permission"), "entity_id", None
            ),
            "enable_entity": getattr(self.entities.get("enabled"), "entity_id", None),
            "entry_id": self.coordinator.entry.entry_id,
            "last_outcome": self.last_outcome,
            "mowing_target": self.config["mowing_target"],
            "rotation_rest_days": self.config["rotation_rest_days"],
            "rotation_max_area": self.config["rotation_max_area"],
            "rotation_map_index": self._rotation_map_index(),
            "rotation_zone_rest_days": {
                str(zone_id): days for zone_id, days in zone_rest_days_by_id(
                    self.config, self._rotation_map_index(),
                ).items()
            },
            "rotation_zones": self.zone_schedule(),
            "target_entity": getattr(self.entities.get("target"), "entity_id", None),
            "rest_days_entity": getattr(
                self.entities.get("rest_days"), "entity_id", None
            ),
            "max_area_entity": getattr(
                self.entities.get("max_area"), "entity_id", None
            ),
            "plan": self.plan(),
            "continue_in_block": bool(self.config.get("continue_in_block")),
            "continuation": {
                "armed": self._continuation_armed,
                "count": self._continuations,
            },
            "finish_before_window_end": bool(
                self.config.get("finish_before_window_end")
            ),
            "time_budget": dict(self._budget),
            "waiting_for_history_until": (
                self._history_wait[0].isoformat() if self._history_wait else None
            ),
        }

    async def async_close(self) -> None:
        """Stop owned callbacks/tasks before the managed engine is drained."""
        if self._closed:
            return
        self._closed = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        for task in (self._setup_task, self._run_task):
            if (
                task is not None
                and task is not asyncio.current_task()
                and not task.done()
            ):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
