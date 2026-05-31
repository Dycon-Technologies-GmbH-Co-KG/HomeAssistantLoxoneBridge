"""Loxone Miniserver WebSocket API client."""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_lib
import json
import logging
import ssl
import struct
import time
import uuid as uuid_module
from typing import Any, Callable

import aiohttp
import certifi

from .const import (
    LOXONE_CMD_ENABLE_STATUS_UPDATE,
    LOXONE_CMD_GET_KEY,
    LOXONE_CMD_GET_STRUCTURE,
    LOXONE_MSG_BINARY,
    LOXONE_MSG_KEEPALIVE,
    LOXONE_MSG_TEXT,
    LOXONE_MSG_TEXT_STATES,
    LOXONE_MSG_VALUE_STATES,
    MAX_RECONNECT_ATTEMPTS,
    RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class LoxoneApiError(Exception):
    """Exception for Loxone API errors."""


class LoxoneApi:
    """WebSocket client for Loxone Miniserver communication."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool = True,
        verify_ssl: bool = False,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the Loxone API client."""
        self._host = host
        self._port = port
        self._auth = aiohttp.BasicAuth(username, password)
        self._use_tls = use_tls
        self._verify_ssl = verify_ssl
        self._ssl_context = self._create_ssl_context()
        self._session = session
        self._own_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._structure: dict[str, Any] = {}
        self._controls: dict[str, Any] = {}
        self._rooms: dict[str, Any] = {}
        self._categories: dict[str, Any] = {}
        self._state_callbacks: list[Callable] = []
        self._text_state_callbacks: list[Callable] = []
        self._connection_callbacks: list[Callable] = []
        self._connected = False
        self._token: str | None = None
        self._listen_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False
        self._reconnecting = False
        self._reconnect_lock = asyncio.Lock()
        self._pending_binary_type: int | None = None

    def _create_ssl_context(self) -> ssl.SSLContext | bool | None:
        """Create an SSL context for secure communication.

        Loxone Miniservers typically use self-signed certificates,
        so verification is disabled by default but TLS encryption is active.
        """
        if not self._use_tls:
            return None
        if self._verify_ssl:
            ctx = ssl.create_default_context(cafile=certifi.where())
            return ctx
        # TLS encryption active, but skip certificate verification
        # (required for self-signed Loxone Miniserver certificates)
        return False

    @property
    def host(self) -> str:
        """Return the Miniserver host."""
        return self._host

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected

    @property
    def structure(self) -> dict[str, Any]:
        """Return the full structure file."""
        return self._structure

    @property
    def controls(self) -> dict[str, Any]:
        """Return all controls."""
        return self._controls

    @property
    def rooms(self) -> dict[str, Any]:
        """Return all rooms."""
        return self._rooms

    @property
    def categories(self) -> dict[str, Any]:
        """Return all categories."""
        return self._categories

    def register_state_callback(self, callback: Callable) -> Callable:
        """Register a callback for value state updates. Returns unregister function."""
        self._state_callbacks.append(callback)

        def _unregister() -> None:
            try:
                self._state_callbacks.remove(callback)
            except ValueError:
                pass

        return _unregister

    def register_text_state_callback(self, callback: Callable) -> Callable:
        """Register a callback for text state updates. Returns unregister function."""
        self._text_state_callbacks.append(callback)

        def _unregister() -> None:
            try:
                self._text_state_callbacks.remove(callback)
            except ValueError:
                pass

        return _unregister

    def register_connection_callback(self, callback: Callable[[bool], None]) -> Callable:
        """Register a callback for connection state changes. Returns unregister function."""
        self._connection_callbacks.append(callback)

        def _unregister() -> None:
            try:
                self._connection_callbacks.remove(callback)
            except ValueError:
                pass

        return _unregister

    def _notify_connection(self, connected: bool) -> None:
        """Notify all connection callbacks of a state change."""
        for callback in list(self._connection_callbacks):
            try:
                callback(connected)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error in connection callback: %s", err)

    def _compute_ws_auth_hash(self, key_hex: str, salt: str, hash_alg: str, user: str = "") -> str:
        """Compute the Loxone WebSocket authentication hash.

        Algorithm for gettoken (Gen2 firmware 10+):
          pwHash   = SHA<N>(password + ':' + salt)  – hex, uppercase
          authHash = HMAC_SHA<N>(key=bytes.fromhex(key_hex),
                                  msg=(user + ':' + pwHash).encode())

        The username prefix in the HMAC message is required by the gettoken
        command; omitting it produces a hash the Miniserver rejects with 401.
        The username must preserve its original case (e.g. ``Loxberry``, not
        ``loxberry``) because the Miniserver is case-sensitive for the HMAC.
        Pass ``user=""`` only for the legacy authenticatewithhash command which
        does not include the username in the HMAC input.
        """
        digest = hashlib.sha256 if hash_alg == "SHA256" else hashlib.sha1
        pw_hash = digest(
            f"{self._auth.password}:{salt}".encode()
        ).hexdigest().upper()
        hmac_input = f"{user}:{pw_hash}" if user else pw_hash
        return hmac_lib.new(
            bytes.fromhex(key_hex),
            hmac_input.encode(),
            digest,
        ).hexdigest()

    @property
    def _base_url(self) -> str:
        """Return the HTTPS base URL."""
        scheme = "https" if self._use_tls else "http"
        return f"{scheme}://{self._host}:{self._port}"

    @property
    def _ws_url(self) -> str:
        """Return the WebSocket (WSS/WS) URL."""
        scheme = "wss" if self._use_tls else "ws"
        return f"{scheme}://{self._host}:{self._port}/ws/rfc6455"

    @property
    def _ssl_param(self) -> ssl.SSLContext | bool | None:
        """Return the SSL parameter for aiohttp requests."""
        return self._ssl_context

    async def _ensure_session(self) -> None:
        """Ensure a single aiohttp session exists and is open."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True

    async def _close_ws(self) -> None:
        """Close the current WebSocket connection if open."""
        self._pending_binary_type = None
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None

    async def _ws_receive_until(
        self, keyword: str, timeout: float = 10.0
    ) -> dict | None:
        """Receive WS text messages until one whose control field contains *keyword*.

        Binary messages and non-matching text messages are skipped.
        Returns the ``LL`` dict of the matching message, or ``None`` on timeout
        or connection close.  Safe to call before the listen loop is started.
        """
        if not self._ws or self._ws.closed:
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _LOGGER.debug("Timeout waiting for WS response matching '%s'", keyword)
                return None
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                _LOGGER.debug("Timeout waiting for WS response matching '%s'", keyword)
                return None
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error receiving WS message: %s", err)
                return None

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    ll = json.loads(msg.data).get("LL", {})
                    if keyword.lower() in ll.get("control", "").lower():
                        return ll
                    _LOGGER.debug(
                        "Skipping WS message '%s' while waiting for '%s'",
                        ll.get("control", "")[:60],
                        keyword,
                    )
                except (json.JSONDecodeError, AttributeError):
                    pass
            elif msg.type == aiohttp.WSMsgType.BINARY:
                _LOGGER.debug("Skipping binary WS message while waiting for '%s'", keyword)
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                _LOGGER.debug("WS closed/error while waiting for '%s'", keyword)
                return None

    async def async_connect(self) -> bool:
        """Connect to the Loxone Miniserver v2 via WSS.

        Loxone Gen2 protocol requires an unauthenticated WS upgrade.
        Credentials are exchanged exclusively via the getkey2 /
        authenticatewithhash WS commands inside async_authenticate().
        """
        await self._ensure_session()
        await self._close_ws()

        try:
            self._ws = await self._session.ws_connect(
                self._ws_url,
                ssl=self._ssl_param,
                heartbeat=30,
                protocols=["remotecontrol"],
            )
            self._connected = True
            self._notify_connection(True)
            _LOGGER.info("Connected to Loxone Miniserver at %s", self._host)
            return True
        except Exception as err:
            _LOGGER.error("Failed to connect to Loxone Miniserver: %s", err)
            self._connected = False
            return False

    async def async_authenticate(self) -> bool:
        """Authenticate the WebSocket session using token-based authentication.

        Uses ``jdev/sys/gettoken`` which is the recommended method for Loxone
        Miniserver Gen2 (firmware 10+) and the method used by every maintained
        Python Loxone library (e.g. pyloxone).

        ``authenticatewithhash`` is a legacy command that was consistently
        rejected (code 400) regardless of salt or session type.  ``gettoken``
        uses the *static* per-user ``salt`` (not ``oneTimeSalt``) and both
        authenticates the current WS session *and* issues a reusable token.

        Flow (all over the WS channel, before the listen loop starts):
          1. Send  jdev/sys/getkey2/<user>
             → receive { key, salt, hashAlg, oneTimeSalt }
          2. Compute  HMAC_SHA<N>(SHA<N>(password:salt).upper(), key_bytes)
             (uses the *static* salt, not the one-time salt)
          3. Send  jdev/sys/gettoken/<hash>/<user>/4/HA_Loxone_Bridge
          4. Receive token response; return True on code 200.
        """
        if not self._ws or self._ws.closed:
            return False

        try:
            # Step 1 – request a one-time key over the WS channel
            getkey_cmd = f"jdev/sys/getkey2/{self._auth.login}"
            await self._ws.send_str(getkey_cmd)
            _LOGGER.debug("Sent getkey2 over WS for user %s", self._auth.login)

            ll = await self._ws_receive_until("getkey2", timeout=10.0)
            if ll is None:
                _LOGGER.warning(
                    "No getkey2 response received over WS; authentication aborted"
                )
                return False

            value = ll.get("value", {})
            if not isinstance(value, dict):
                _LOGGER.warning("Unexpected getkey2 WS response: %s", ll)
                return False

            key_hex = value.get("key", "")
            hash_alg = value.get("hashAlg", "SHA256").upper()
            # gettoken uses the *static* per-user salt, not the one-time salt.
            # oneTimeSalt is only meaningful for the deprecated authenticatewithhash.
            static_salt = value.get("salt", "")
            one_time_salt = value.get("oneTimeSalt", "")

            _LOGGER.debug(
                "getkey2 WS response: hashAlg=%s key_len=%d "
                "static_salt_present=%s onetimesalt_present=%s",
                hash_alg,
                len(key_hex),
                bool(static_salt),
                bool(one_time_salt),
            )

            if not key_hex or not static_salt:
                _LOGGER.warning("Missing key or static salt in getkey2 response")
                return False

            # Step 2 – compute hash using the static salt
            # gettoken requires HMAC input: user + ":" + pwHash (original case)
            auth_hash = self._compute_ws_auth_hash(key_hex, static_salt, hash_alg, user=self._auth.login)

            # Step 3 – request token (authenticates this WS session and issues token)
            # Format: gettoken/<hash>/<user>/<permission>/<clientUuid>/<info>
            client_uuid = "0" + uuid_module.uuid4().hex
            token_cmd = (
                f"jdev/sys/gettoken/{auth_hash}/{self._auth.login}"
                f"/4/{client_uuid}/HA_Loxone_Bridge"
            )
            _LOGGER.debug(
                "WS gettoken: user=%s alg=%s key_prefix=%s hash_prefix=%s",
                self._auth.login,
                hash_alg,
                key_hex[:8],
                auth_hash[:8],
            )
            await self._ws.send_str(token_cmd)

            # Step 4 – wait for the token response
            token_ll = await self._ws_receive_until("gettoken", timeout=10.0)
            if token_ll is None:
                _LOGGER.warning(
                    "No gettoken response received; treating as failure"
                )
                return False

            code = str(token_ll.get("Code", token_ll.get("code", 0)))
            if code == "200":
                # Persist the token for reconnect via usetoken
                token_value = token_ll.get("value", {})
                if isinstance(token_value, dict) and token_value.get("token"):
                    self._token = token_value["token"]
                    _LOGGER.debug("Stored auth token for future reconnects")
                _LOGGER.info(
                    "WS authentication successful for user %s", self._auth.login
                )
                return True

            _LOGGER.error(
                "Loxone WS authentication failed (code=%s). "
                "Check username/password in the integration options.",
                code,
            )
            return False

        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "WS authentication error (%s): %s", type(err).__name__, err
            )
            return False

    async def _async_authenticate_with_token(self) -> bool:
        """Re-authenticate using a previously stored token via usetoken.

        This avoids re-sending the password hash on every WS reconnect.
        Falls back to full gettoken auth if the token is expired or invalid.
        """
        if not self._token or not self._ws or self._ws.closed:
            return False

        try:
            cmd = f"jdev/sys/usetoken/{self._token}"
            await self._ws.send_str(cmd)
            ll = await self._ws_receive_until("usetoken", timeout=10.0)
            if ll is None:
                _LOGGER.debug("No usetoken response; token may be expired")
                self._token = None
                return False
            code = str(ll.get("Code", ll.get("code", 0)))
            if code == "200":
                _LOGGER.info("WS re-authentication via token successful")
                return True
            _LOGGER.debug("usetoken rejected (code=%s); falling back to gettoken", code)
            self._token = None
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("usetoken error: %s", err)
            self._token = None
            return False

    async def async_get_structure(self) -> dict[str, Any]:
        """Fetch the LoxAPP3.json structure file via HTTPS."""
        url = f"{self._base_url}/{LOXONE_CMD_GET_STRUCTURE}"
        try:
            async with self._session.get(
                url,
                auth=self._auth,
                ssl=self._ssl_param,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 200:
                    self._structure = await resp.json(content_type=None)
                    self._controls = self._structure.get("controls", {})
                    self._rooms = self._structure.get("rooms", {})
                    self._categories = self._structure.get("cats", {})
                    _LOGGER.info(
                        "Loaded Loxone structure: %d controls, %d rooms",
                        len(self._controls), len(self._rooms),
                    )
                    return self._structure
                raise LoxoneApiError(f"Failed to get structure: HTTP {resp.status}")
        except aiohttp.ClientError as err:
            raise LoxoneApiError(f"Error fetching structure: {err}") from err

    async def async_enable_status_updates(self) -> None:
        """Enable binary status updates via WebSocket."""
        if not self._ws or self._ws.closed:
            return
        await self._ws.send_str(LOXONE_CMD_ENABLE_STATUS_UPDATE)
        _LOGGER.info("Sent enablebinstatusupdate command")

    async def async_start_listening(self) -> None:
        """Start listening for WebSocket messages.

        Cancels any previous listen task to avoid concurrent receive() calls.
        """
        # Stop old listener to prevent concurrent receive()
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        self._closing = False
        self._listen_task = asyncio.ensure_future(self._listen_loop())

    async def _listen_loop(self) -> None:
        """Main WebSocket listen loop."""
        _LOGGER.info("Listen loop started, waiting for WS messages")
        msg_count = 0
        while not self._closing and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=60)
                msg_count += 1
                if msg_count <= 10:
                    data_len = len(msg.data) if msg.data else 0
                    _LOGGER.info(
                        "WS msg #%d: type=%s len=%d",
                        msg_count, msg.type, data_len,
                    )

                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_text_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._handle_binary_message(msg.data)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    _LOGGER.warning("WebSocket connection closed/error")
                    break
            except asyncio.TimeoutError:
                # Send keepalive
                if self._ws and not self._ws.closed:
                    await self._ws.send_str("keepalive")
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.error("Error in listen loop: %s", err)
                break

        self._connected = False
        self._notify_connection(False)
        if not self._closing:
            _LOGGER.info("Connection lost, scheduling reconnect")
            self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        """Reconnect to the Miniserver with backoff and attempt limit."""
        if self._reconnecting:
            return  # Another reconnect is already in progress

        async with self._reconnect_lock:
            self._reconnecting = True
            try:
                for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
                    if self._closing:
                        return
                    delay = min(RECONNECT_DELAY * attempt, 60)
                    _LOGGER.info(
                        "Reconnect attempt %d/%d in %ds",
                        attempt, MAX_RECONNECT_ATTEMPTS, delay,
                    )
                    await asyncio.sleep(delay)
                    if self._closing:
                        return
                    if await self.async_connect():
                        # Try token-based re-auth first (avoids re-sending password)
                        if not await self._async_authenticate_with_token():
                            if not await self.async_authenticate():
                                _LOGGER.warning(
                                    "Reconnect attempt %d: authentication failed, retrying",
                                    attempt,
                                )
                                continue
                        await self.async_enable_status_updates()
                        await self.async_start_listening()
                        return
                _LOGGER.error(
                    "Failed to reconnect after %d attempts", MAX_RECONNECT_ATTEMPTS
                )
            finally:
                self._reconnecting = False

    async def _handle_text_message(self, data: str) -> None:
        """Handle a text WebSocket message."""
        try:
            parsed = json.loads(data)
            ll = parsed.get("LL", {})
            control = ll.get("control", "")
            code = str(ll.get("Code", ll.get("code", 0)))
            value = ll.get("value", ll.get("Value"))

            _LOGGER.debug("Text message: control=%s code=%s", control, code)

            if value and isinstance(value, dict):
                for uuid, state in value.items():
                    for callback in self._text_state_callbacks:
                        try:
                            callback(uuid, state)
                        except Exception as err:
                            _LOGGER.error("Error in text state callback: %s", err)
        except (json.JSONDecodeError, KeyError):
            _LOGGER.debug("Non-JSON text message: %s", data[:200])

    async def _handle_binary_message(self, data: bytes) -> None:
        """Handle binary WebSocket messages using Loxone's two-frame protocol.

        The Loxone Miniserver sends binary state updates as two consecutive
        WebSocket frames:
          Frame 1 (header):  8 bytes – 0x03 | type | flags | reserved | length (LE u32)
          Frame 2 (payload): the actual state data (length bytes)

        Keepalive (type 6) is header-only; no payload frame follows.
        Text responses (type 0) are followed by a TEXT WS frame handled
        by ``_handle_text_message``.
        """
        # --- Phase 2: payload for a previously received header ---
        if self._pending_binary_type is not None:
            msg_type = self._pending_binary_type
            self._pending_binary_type = None
            _LOGGER.info(
                "Binary payload: type=%d len=%d", msg_type, len(data)
            )
            if msg_type == LOXONE_MSG_VALUE_STATES:
                await self._handle_value_states(data)
            elif msg_type == LOXONE_MSG_TEXT_STATES:
                await self._handle_text_states(data)
            else:
                _LOGGER.debug("Ignoring binary payload for type %d (len=%d)", msg_type, len(data))
            return

        # --- Phase 1: this should be an 8-byte binary header ---
        if len(data) < 8:
            _LOGGER.debug("Short binary frame ignored (len=%d)", len(data))
            return

        # Byte 0: fixed 0x03, Byte 1: message type
        msg_type = data[1]
        _LOGGER.info(
            "Binary header: type=%d len=%d hex=%s",
            msg_type, len(data), data[:8].hex(),
        )

        if msg_type == LOXONE_MSG_KEEPALIVE:
            # Keepalive is header-only – no payload frame follows
            _LOGGER.debug("Keepalive received")
        elif msg_type in (LOXONE_MSG_TEXT, LOXONE_MSG_BINARY):
            # Type 0 (text): next frame is a TEXT WS frame, handled elsewhere
            # Type 1 (binary file): not used by this integration
            pass
        else:
            # Types 2-7 (value states, text states, daytimer, weather, …):
            # the next binary frame is the payload
            self._pending_binary_type = msg_type

    async def _handle_value_states(self, data: bytes) -> None:
        """Parse value state update table.

        Each entry: 16 bytes UUID + 8 bytes double value = 24 bytes.
        """
        count = 0
        first_uuid = ""
        offset = 0
        while offset + 24 <= len(data):
            uuid_bytes = data[offset : offset + 16]
            uuid = self._bytes_to_uuid(uuid_bytes)
            value = struct.unpack("<d", data[offset + 16 : offset + 24])[0]
            offset += 24
            count += 1
            if count == 1:
                first_uuid = uuid

            for callback in self._state_callbacks:
                try:
                    callback(uuid, value)
                except Exception as err:
                    _LOGGER.error("Error in state callback: %s", err)

        if count > 0:
            _LOGGER.info(
                "Parsed %d value states (first: %s), %d callbacks registered",
                count, first_uuid, len(self._state_callbacks),
            )

    async def _handle_text_states(self, data: bytes) -> None:
        """Parse text state update table.

        Each entry:
          16 bytes  state UUID
          16 bytes  icon UUID
           4 bytes  text length (unsigned LE)
           N bytes  text (padded to 4-byte boundary)
        """
        count = 0
        offset = 0
        while offset + 36 <= len(data):
            uuid_bytes = data[offset : offset + 16]
            uuid = self._bytes_to_uuid(uuid_bytes)
            # Skip state UUID (16) + icon UUID (16) = 32 bytes
            offset += 32
            text_len = struct.unpack("<I", data[offset : offset + 4])[0]
            offset += 4
            text = data[offset : offset + text_len].decode("utf-8", errors="replace")
            # Align to 4 bytes
            padded_len = text_len + (4 - text_len % 4) % 4
            offset += padded_len
            count += 1

            for callback in self._text_state_callbacks:
                try:
                    callback(uuid, text)
                except Exception as err:
                    _LOGGER.error("Error in text state callback: %s", err)

        if count > 0:
            _LOGGER.info("Parsed %d text states", count)

    @staticmethod
    def _bytes_to_uuid(uuid_bytes: bytes) -> str:
        """Convert 16 bytes (Loxone mixed-endian) to a UUID string.

        Loxone binary protocol encodes the first three UUID components in
        little-endian byte order, while the last two are big-endian, matching
        the hex strings stored in the structure file (LoxAPP3.json).

        Loxone uses a non-standard 3-dash format: xxxxxxxx-xxxx-xxxx-xxxxxxxxxxxxxxxx
        (the last two components are concatenated without a dash, unlike the
        standard RFC 4122 format which uses 4 dashes).

        Layout of the 16 bytes on the wire:
          bytes  0-3  → uint32 LE  (component 1, 8 hex chars)
          bytes  4-5  → uint16 LE  (component 2, 4 hex chars)
          bytes  6-7  → uint16 LE  (component 3, 4 hex chars)
          bytes  8-15 → big-endian (component 4+5, 16 hex chars, no dash)
        """
        if len(uuid_bytes) != 16:
            return ""
        p1 = struct.unpack_from("<I", uuid_bytes, 0)[0]
        p2 = struct.unpack_from("<H", uuid_bytes, 4)[0]
        p3 = struct.unpack_from("<H", uuid_bytes, 6)[0]
        p4 = uuid_bytes[8:10].hex()
        p5 = uuid_bytes[10:16].hex()
        return f"{p1:08x}-{p2:04x}-{p3:04x}-{p4}{p5}"

    async def async_send_command(self, uuid: str, command: str) -> bool:
        """Send a command to a Loxone control."""
        cmd = f"jdev/sps/io/{uuid}/{command}"
        return await self._async_send_ws_command(cmd)

    async def async_send_raw_command(self, command: str) -> bool:
        """Send a raw command string to Loxone."""
        return await self._async_send_ws_command(command)

    async def _async_send_ws_command(self, command: str) -> bool:
        """Send a command via WebSocket."""
        if not self._ws or self._ws.closed:
            _LOGGER.error("Cannot send command: not connected")
            return False
        try:
            await self._ws.send_str(command)
            _LOGGER.debug("Sent command: %s", command)
            return True
        except Exception as err:
            _LOGGER.error("Failed to send command: %s", err)
            return False

    async def async_send_http_command(self, uuid: str, command: str) -> dict | None:
        """Send a command via HTTPS (fallback).

        Returns the response dict on success, None on failure.
        404/403 are expected for non-existent Virtual Inputs and logged at debug level.
        """
        url = f"{self._base_url}/jdev/sps/io/{uuid}/{command}"
        try:
            async with self._session.get(
                url,
                auth=self._auth,
                ssl=self._ssl_param,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                # 404/403 are expected when Virtual Input doesn't exist
                if resp.status in (404, 403):
                    _LOGGER.debug("HTTP command %s/%s: %s", uuid, command, resp.status)
                else:
                    _LOGGER.warning("HTTP command %s/%s failed: %s", uuid, command, resp.status)
                return None
        except Exception as err:
            _LOGGER.debug("HTTP command error for %s: %s", uuid, type(err).__name__)
            return None

    async def async_get_state(self, uuid: str) -> Any:
        """Get the current state of a control via HTTPS."""
        url = f"{self._base_url}/jdev/sps/io/{uuid}"
        try:
            async with self._session.get(
                url,
                auth=self._auth,
                ssl=self._ssl_param,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    return data.get("LL", {}).get("value")
                return None
        except Exception as err:
            _LOGGER.error("Error getting state for %s: %s", uuid, err)
            return None

    async def async_test_connection(self) -> bool:
        """Test the connection to the Miniserver via HTTPS."""
        try:
            async with self._session.get(
                f"{self._base_url}/jdev/cfg/api",
                auth=self._auth,
                ssl=self._ssl_param,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def async_disconnect(self) -> None:
        """Disconnect from the Miniserver and clean up all resources."""
        self._closing = True
        self._connected = False

        # Cancel reconnect first so it doesn't restart while we clean up
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        # Cancel listen task
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        # Close WebSocket
        await self._close_ws()

        # Close session only if we own it
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

        _LOGGER.info("Disconnected from Loxone Miniserver")

    def get_control(self, uuid: str) -> dict[str, Any] | None:
        """Get a specific control by UUID."""
        return self._controls.get(uuid)

    def get_room_name(self, room_uuid: str) -> str:
        """Get a room name by UUID."""
        room = self._rooms.get(room_uuid, {})
        return room.get("name", "Unknown Room")

    def get_category_name(self, cat_uuid: str) -> str:
        """Get a category name by UUID."""
        cat = self._categories.get(cat_uuid, {})
        return cat.get("name", "Unknown Category")

    def get_controls_by_type(self, control_type: str) -> dict[str, Any]:
        """Get all controls of a specific type."""
        return {
            uuid: ctrl
            for uuid, ctrl in self._controls.items()
            if ctrl.get("type") == control_type
        }
