"""Minimal async NETCONF-over-SSH client for Junos, built on asyncssh.

No Home Assistant imports here so the module can be tested and used on its own.

Only NETCONF base:1.0 is advertised, so the switch uses the simple ``]]>]]>`` end-of-message framing.
Replies are parsed with the stdlib ElementTree and all XML namespaces are stripped, so callers can use
plain tag names such as ``route-engine-information``.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import asyncssh

_LOGGER = logging.getLogger(__name__)

EOM = "]]>]]>"
HELLO = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
    "<capabilities><capability>urn:ietf:params:netconf:base:1.0</capability></capabilities>"
    "</hello>" + EOM
)
_XML_DECL = re.compile(r"<\?xml[^>]*\?>")
DEFAULT_TIMEOUT = 30.0


class JunosError(Exception):
    """Base error."""


class JunosConnectionError(JunosError):
    """Could not connect, or the connection dropped."""


class JunosAuthError(JunosError):
    """The switch rejected the credentials."""


class JunosHostKeyError(JunosError):
    """The switch's SSH host key does not match the one stored at setup."""


class JunosRpcError(JunosError):
    """The switch answered an RPC with an <rpc-error> of severity error."""

    def __init__(self, rpc: str, message: str) -> None:
        super().__init__(f"{rpc}: {message}")
        self.rpc = rpc
        self.message = message

    @property
    def unsupported(self) -> bool:
        """True when the error means this platform does not have the RPC (e.g. PoE on a non-PoE box)."""
        m = self.message.lower()
        return any(s in m for s in ("syntax error", "not supported", "unknown command", "invalid command",
                                    "permission denied", "is not enabled", "not running", "not valid",
                                    "not available"))


def strip_ns(root: ET.Element) -> ET.Element:
    """Remove namespaces from every tag and attribute name, in place."""
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
        if el.attrib:
            for k in list(el.attrib):
                if "}" in k:
                    el.attrib[k.split("}", 1)[1]] = el.attrib.pop(k)
    return root


@dataclass
class Credentials:
    """How to log in."""

    username: str
    password: str | None = None
    private_key: str | None = None  # OpenSSH/PEM text
    passphrase: str | None = None


class JunosNetconf:
    """One NETCONF session to a Junos device. Not safe for concurrent RPCs; calls are serialised."""

    def __init__(self, host: str, port: int, creds: Credentials, host_key: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self._creds = creds
        self._host_key = host_key  # OpenSSH public key string; None = trust on first use
        self._timeout = timeout
        self._conn: asyncssh.SSHClientConnection | None = None
        self._writer: Any = None
        self._reader: Any = None
        self._buf = ""
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self.server_host_key: str | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _connect_options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "port": self.port,
            "username": self._creds.username,
            "config": (),            # never read ~/.ssh/config (blocking I/O, surprising behaviour)
            "agent_path": None,      # never use an SSH agent
            "connect_timeout": self._timeout,
            "keepalive_interval": 30,
        }
        if self._host_key:
            opts["known_hosts"] = ([asyncssh.import_public_key(self._host_key)], [], [])
        else:
            opts["known_hosts"] = None
        if self._creds.private_key:
            opts["client_keys"] = [asyncssh.import_private_key(self._creds.private_key, self._creds.passphrase or None)]
            opts["password"] = self._creds.password or None
        else:
            opts["client_keys"] = None
            opts["password"] = self._creds.password
        return opts

    async def connect(self) -> None:
        """Open SSH, start the netconf subsystem and exchange hellos."""
        try:
            opts = self._connect_options()
        except (asyncssh.KeyImportError, ValueError) as err:
            raise JunosAuthError(f"Could not read the private key: {err}") from err
        try:
            self._conn = await asyncssh.connect(self.host, **opts)
            key = self._conn.get_server_host_key()
            self.server_host_key = key.export_public_key().decode().strip() if key else None
            self._writer, self._reader, _ = await self._conn.open_session(subsystem="netconf", encoding="utf-8")
            self._buf = ""
            await asyncio.wait_for(self._read_message(), self._timeout)  # server hello
            self._writer.write(HELLO)
        except asyncssh.PermissionDenied as err:
            await self.close()
            raise JunosAuthError(str(err)) from err
        except asyncssh.HostKeyNotVerifiable as err:
            await self.close()
            raise JunosHostKeyError(str(err)) from err
        except asyncssh.ChannelOpenError as err:
            await self.close()
            raise JunosConnectionError(
                f"NETCONF subsystem refused ({err}). Is 'set system services netconf ssh' configured?") from err
        except (asyncio.TimeoutError, TimeoutError) as err:
            await self.close()
            raise JunosConnectionError(
                f"No answer from {self.host}:{self.port} within {self._timeout:.0f}s. Check that Home Assistant "
                "can reach the switch's management address on this port (routing / firewall)") from err
        except (OSError, asyncssh.Error) as err:
            await self.close()
            raise JunosConnectionError(f"{self.host}:{self.port}: {type(err).__name__}: {err}") from err

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if self._writer is not None:
                self._writer.write('<rpc message-id="close"><close-session/></rpc>' + EOM)
        except Exception:  # noqa: BLE001 - best effort
            pass
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), 5)
        except Exception:  # noqa: BLE001
            pass
        self._writer = self._reader = None

    async def _read_message(self) -> str:
        while EOM not in self._buf:
            chunk = await self._reader.read(65536)
            if not chunk:
                raise JunosConnectionError("Connection closed by the switch")
            self._buf += chunk
        msg, self._buf = self._buf.split(EOM, 1)
        return msg

    async def rpc(self, body: str, name: str | None = None) -> ET.Element:
        """Send one RPC (inner XML, e.g. ``<get-alarm-information/>``) and return the rpc-reply element.

        Raises JunosRpcError for <rpc-error> with severity error; warnings are logged and ignored.
        """
        name = name or body.strip("</ >").split()[0].split(">")[0]
        async with self._lock:
            if self._conn is None:
                await self.connect()
            msg_id = next(self._ids)
            try:
                self._writer.write(f'<rpc message-id="{msg_id}">{body}</rpc>{EOM}')
                raw = await asyncio.wait_for(self._read_message(), self._timeout)
            except (OSError, asyncssh.Error, asyncio.TimeoutError, JunosConnectionError) as err:
                await self.close()
                raise JunosConnectionError(f"{name}: {type(err).__name__}: {err}") from err
        try:
            root = strip_ns(ET.fromstring(_XML_DECL.sub("", raw).strip()))
        except ET.ParseError as err:
            raise JunosRpcError(name, f"unparseable reply: {err}") from err
        errors = []
        for err_el in root.iter("rpc-error"):
            sev = (err_el.findtext("error-severity") or "error").strip()
            text = " ".join(t.strip() for t in (err_el.findtext("error-message") or "",
                                                  err_el.findtext("error-info/bad-element") or "") if t.strip())
            if sev == "warning":
                _LOGGER.debug("%s warning: %s", name, text)
            else:
                errors.append(text or "unknown error")
        # Some daemons answer with a bare <error><message>..</message></error> instead of <rpc-error>,
        # e.g. PoE on a non-PoE switch: "command is not valid on the current system (chassis-control)".
        for err_el in root.findall("error") + root.findall("*/error"):
            errors.append((err_el.findtext("message") or "unknown error").strip())
        if errors:
            raise JunosRpcError(name, "; ".join(errors))
        return root
