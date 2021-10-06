"""Define a connection to the SimpliSafe websocket."""
from __future__ import annotations

import asyncio
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, cast

from aiohttp import ClientWebSocketResponse, WSMsgType
from aiohttp.client_exceptions import (
    ClientError,
    ServerDisconnectedError,
    WSServerHandshakeError,
)

from simplipy.const import DEFAULT_USER_AGENT, LOGGER
from simplipy.device import DeviceTypes
from simplipy.errors import (
    CannotConnectError,
    ConnectionClosedError,
    ConnectionFailedError,
    InvalidMessageError,
    NotConnectedError,
)
from simplipy.util.dt import utc_from_timestamp

if TYPE_CHECKING:
    from simplipy import API

WEBSOCKET_SERVER_URL = "wss://socketlink.prd.aser.simplisafe.com"

DEFAULT_WATCHDOG_TIMEOUT = timedelta(minutes=1)

EVENT_ALARM_CANCELED = "alarm_canceled"
EVENT_ALARM_TRIGGERED = "alarm_triggered"
EVENT_ARMED_AWAY = "armed_away"
EVENT_ARMED_AWAY_BY_KEYPAD = "armed_away_by_keypad"
EVENT_ARMED_AWAY_BY_REMOTE = "armed_away_by_remote"
EVENT_ARMED_HOME = "armed_home"
EVENT_AUTOMATIC_TEST = "automatic_test"
EVENT_AWAY_EXIT_DELAY_BY_KEYPAD = "away_exit_delay_by_keypad"
EVENT_AWAY_EXIT_DELAY_BY_REMOTE = "away_exit_delay_by_remote"
EVENT_CAMERA_MOTION_DETECTED = "camera_motion_detected"
EVENT_CONNECTION_LOST = "connection_lost"
EVENT_CONNECTION_RESTORED = "connection_restored"
EVENT_DISARMED_BY_MASTER_PIN = "disarmed_by_master_pin"
EVENT_DISARMED_BY_REMOTE = "disarmed_by_remote"
EVENT_DOORBELL_DETECTED = "doorbell_detected"
EVENT_DEVICE_TEST = "device_test"
EVENT_ENTRY_DELAY = "entry_delay"
EVENT_HOME_EXIT_DELAY = "home_exit_delay"
EVENT_LOCK_ERROR = "lock_error"
EVENT_LOCK_LOCKED = "lock_locked"
EVENT_LOCK_UNLOCKED = "lock_unlocked"
EVENT_POWER_OUTAGE = "power_outage"
EVENT_POWER_RESTORED = "power_restored"
EVENT_SECRET_ALERT_TRIGGERED = "secret_alert_triggered"
EVENT_SENSOR_NOT_RESPONDING = "sensor_not_responding"
EVENT_SENSOR_PAIRED_AND_NAMED = "sensor_paired_and_named"
EVENT_SENSOR_RESTORED = "sensor_restored"
EVENT_USER_INITIATED_TEST = "user_initiated_test"

EVENT_MAPPING = {
    1110: EVENT_ALARM_TRIGGERED,
    1120: EVENT_ALARM_TRIGGERED,
    1132: EVENT_ALARM_TRIGGERED,
    1134: EVENT_ALARM_TRIGGERED,
    1154: EVENT_ALARM_TRIGGERED,
    1159: EVENT_ALARM_TRIGGERED,
    1162: EVENT_ALARM_TRIGGERED,
    1170: EVENT_CAMERA_MOTION_DETECTED,
    1301: EVENT_POWER_OUTAGE,
    1350: EVENT_CONNECTION_LOST,
    1381: EVENT_SENSOR_NOT_RESPONDING,
    1400: EVENT_DISARMED_BY_MASTER_PIN,
    1406: EVENT_ALARM_CANCELED,
    1407: EVENT_DISARMED_BY_REMOTE,
    1409: EVENT_SECRET_ALERT_TRIGGERED,
    1429: EVENT_ENTRY_DELAY,
    1458: EVENT_DOORBELL_DETECTED,
    1531: EVENT_SENSOR_PAIRED_AND_NAMED,
    1601: EVENT_USER_INITIATED_TEST,
    1602: EVENT_AUTOMATIC_TEST,
    1604: EVENT_DEVICE_TEST,
    3301: EVENT_POWER_RESTORED,
    3350: EVENT_CONNECTION_RESTORED,
    3381: EVENT_SENSOR_RESTORED,
    3401: EVENT_ARMED_AWAY_BY_KEYPAD,
    3407: EVENT_ARMED_AWAY_BY_REMOTE,
    3441: EVENT_ARMED_HOME,
    3481: EVENT_ARMED_AWAY,
    3487: EVENT_ARMED_AWAY,
    3491: EVENT_ARMED_HOME,
    9401: EVENT_AWAY_EXIT_DELAY_BY_KEYPAD,
    9407: EVENT_AWAY_EXIT_DELAY_BY_REMOTE,
    9441: EVENT_HOME_EXIT_DELAY,
    9700: EVENT_LOCK_UNLOCKED,
    9701: EVENT_LOCK_LOCKED,
    9703: EVENT_LOCK_ERROR,
}


class Watchdog:
    """Define a watchdog to kick the websocket connection at intervals."""

    def __init__(
        self, action: Callable[..., Any], timeout: timedelta = DEFAULT_WATCHDOG_TIMEOUT
    ):
        """Initialize."""
        self._action = action
        self._action_task: asyncio.Task | None = None
        self._loop = asyncio.get_running_loop()
        self._timeout_seconds = timeout.total_seconds()
        self._timer_task: asyncio.TimerHandle | None = None

    def _on_expire(self) -> None:
        """Log and act when the watchdog expires."""
        LOGGER.info("Websocket watchdog expired")

        if asyncio.iscoroutinefunction(self._action):
            asyncio.create_task(self._action())
        else:
            self._loop.call_soon(self._action())

    def cancel(self) -> None:
        """Cancel the watchdog."""
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None

    def trigger(self) -> None:
        """Trigger the watchdog."""
        LOGGER.info(
            "Websocket watchdog triggered – sleeping for %s seconds",
            self._timeout_seconds,
        )

        if self._timer_task:
            self._timer_task.cancel()

        self._timer_task = self._loop.call_later(self._timeout_seconds, self._on_expire)


@dataclass(frozen=True)
class WebsocketEvent:  # pylint: disable=too-many-instance-attributes
    """Define a representation of a message."""

    event_cid: InitVar[int]
    info: str
    system_id: int
    timestamp: float

    event_type: str | None = field(init=False)

    changed_by: str | None = None
    sensor_name: str | None = None
    sensor_serial: str | None = None
    sensor_type: DeviceTypes | None = None

    def __post_init__(self, event_cid: int) -> None:
        """Run post-init initialization."""
        if event_cid in EVENT_MAPPING:
            object.__setattr__(self, "event_type", EVENT_MAPPING[event_cid])
        else:
            LOGGER.warning(
                'Encountered unknown websocket event type: %s ("%s"). Please report it '
                "at https://github.com/bachya/simplisafe-python/issues.",
                event_cid,
                self.info,
            )
            object.__setattr__(self, "event_type", None)

        object.__setattr__(self, "timestamp", utc_from_timestamp(self.timestamp))

        if self.sensor_type is not None:
            try:
                object.__setattr__(self, "sensor_type", DeviceTypes(self.sensor_type))
            except ValueError:
                LOGGER.warning(
                    'Encountered unknown device type: %s ("%s"). Please report it at'
                    "https://github.com/home-assistant/home-assistant/issues.",
                    self.sensor_type,
                    self.info,
                )
                object.__setattr__(self, "sensor_type", None)


def websocket_event_from_payload(payload: dict[str, Any]) -> WebsocketEvent:
    """Create a Message object from a websocket event payload."""
    return WebsocketEvent(
        payload["data"]["eventCid"],
        payload["data"]["info"],
        payload["data"]["sid"],
        payload["data"]["eventTimestamp"],
        changed_by=payload["data"]["pinName"],
        sensor_name=payload["data"]["sensorName"],
        sensor_serial=payload["data"]["sensorSerial"],
        sensor_type=payload["data"]["sensorType"],
    )


class WebsocketClient:
    """A websocket connection to the SimpliSafe cloud.

    Note that this class shouldn't be instantiated directly; it will be instantiated as
    appropriate via :meth:`simplipy.API.async_from_auth` or
    :meth:`simplipy.API.async_from_refresh_token`.

    :param api: A :meth:`simplipy.API` object
    :type api: :meth:`simplipy.API`
    """

    def __init__(self, api: API) -> None:
        """Initialize."""
        self._api = api
        self._connect_listeners: list[Callable[..., None]] = []
        self._disconnect_listeners: list[Callable[..., None]] = []
        self._event_listeners: list[Callable[..., None]] = []
        self._loop = asyncio.get_running_loop()
        self._watchdog = Watchdog(self.async_reconnect)

        # These will get filled in after initial authentication:
        self._client: ClientWebSocketResponse | None = None

    @property
    def connected(self) -> bool:
        """Return if currently connected to the websocket."""
        return self._client is not None and not self._client.closed

    @staticmethod
    def _add_listener(
        listener_list: list, callback: Callable[..., None]
    ) -> Callable[..., None]:
        """Add a listener callback to a particular list."""
        listener_list.append(callback)

        def remove() -> None:
            """Remove the callback."""
            listener_list.remove(callback)

        return remove

    async def _async_receive_json(self) -> dict[str, Any]:
        """Receive a JSON response from the websocket server."""
        assert self._client
        msg = await self._client.receive()

        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            raise ConnectionClosedError("Connection was closed.")

        if msg.type == WSMsgType.ERROR:
            raise ConnectionFailedError

        if msg.type != WSMsgType.TEXT:
            raise InvalidMessageError(f"Received non-text message: {msg.type}")

        try:
            data = msg.json()
        except ValueError as err:
            raise InvalidMessageError("Received invalid JSON") from err

        LOGGER.debug("Received data from websocket server: %s", data)

        return cast(Dict[str, Any], data)

    async def _async_send_json(self, payload: dict[str, Any]) -> None:
        """Send a JSON message to the websocket server.

        Raises NotConnectedError if client is not connected.
        """
        if not self.connected:
            raise NotConnectedError

        assert self._client

        LOGGER.debug("Sending data to websocket server: %s", payload)

        await self._client.send_json(payload)

    def _parse_message(self, message: dict[str, Any]) -> None:
        """Parse an incoming message."""
        if message["type"] == "com.simplisafe.event.standard":
            event = websocket_event_from_payload(message)
            for listener in self._event_listeners:
                self._schedule_listener_call(listener, event)

    def _schedule_listener_call(self, listener: Callable[..., Any], *args: Any) -> None:
        """Schedule a listener to be called."""
        if asyncio.iscoroutinefunction(listener):
            asyncio.create_task(listener(*args))
        else:
            self._loop.call_soon(listener, *args)

    def add_connect_listener(
        self, callback: Callable[..., None]
    ) -> Callable[..., None]:
        """Add a listener callback to be called after connecting.

        :param callback: The method to call after connecting
        :type callback: ``Callable[..., None]``
        """
        return self._add_listener(self._connect_listeners, callback)

    def add_disconnect_listener(
        self, callback: Callable[..., None]
    ) -> Callable[..., None]:
        """Add a listener callback to be called after disconnecting.

        :param callback: The method to call after disconnecting
        :type callback: ``Callable[..., None]``
        """
        return self._add_listener(self._disconnect_listeners, callback)

    def add_event_listener(self, callback: Callable[..., None]) -> Callable[..., None]:
        """Add a listener callback to be called upon receiving an event.

        Note that callbacks should expect to receive a WebsocketEvent object as a
        parameter.

        :param callback: The method to call after receiving an event.
        :type callback: ``Callable[..., None]``
        """
        return self._add_listener(self._event_listeners, callback)

    async def async_connect(self) -> None:
        """Connect to the websocket server."""
        try:
            self._client = await self._api.session.ws_connect(
                WEBSOCKET_SERVER_URL, heartbeat=55
            )
        except (ClientError, ServerDisconnectedError, WSServerHandshakeError) as err:
            raise CannotConnectError(err) from err

        LOGGER.info("Connected to websocket server")

        for listener in self._connect_listeners:
            self._schedule_listener_call(listener)

        self._watchdog.trigger()

    async def async_disconnect(self) -> None:
        """Disconnect from the websocket server."""
        assert self._client

        await self._client.close()

        LOGGER.info("Disconnected from websocket server")

        for listener in self._disconnect_listeners:
            self._schedule_listener_call(listener)

    async def async_listen(self) -> None:
        """Start listening to the websocket server."""
        assert self._client

        now = datetime.utcnow()
        now_ts = round(now.timestamp() * 1000)
        now_utc_iso = f"{now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z"

        try:
            await self._async_send_json(
                {
                    "datacontenttype": "application/json",
                    "type": "com.simplisafe.connection.identify",
                    "time": now_utc_iso,
                    "id": f"ts:{now_ts}",
                    "specversion": "1.0",
                    "source": DEFAULT_USER_AGENT,
                    "data": {
                        "auth": {
                            "schema": "bearer",
                            "token": self._api.access_token,
                        },
                        "join": [f"uid:{self._api.user_id}"],
                    },
                }
            )

            while not self._client.closed:
                message = await self._async_receive_json()
                self._parse_message(message)
        except ConnectionClosedError:
            pass
        finally:
            LOGGER.debug("Listen completed; cleaning up")

            for listener in self._disconnect_listeners:
                self._schedule_listener_call(listener)

    async def async_reconnect(self) -> None:
        """Reconnect (and re-listen, if appropriate) to the websocket."""
        await self.async_disconnect()
        await asyncio.sleep(1)
        await self.async_connect()
