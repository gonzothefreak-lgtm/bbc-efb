"""EXPERIMENTAL standalone-Core local interface (second release). Not wired into the EFB.

The first whole-EFB build runs Core in-process and never imports this package. It is a
prototype of the versioned local interface the owner scheduled for after the first-build
review: a wire protocol (:mod:`.protocol`), a server that gives each local client its own
Core consumer (:mod:`.server`) and a client whose :class:`RemoteConsumerAPI` mirrors
``ConsumerAPI`` (:mod:`.client`). Hosting, supervision and access control are open owner
decisions; see ``docs/STANDALONE_CORE_IPC.md``.

Importing this package opens nothing. Only ``CoreServer.start()`` creates a listener: a
Windows named pipe or a private socket file, never a TCP/IP port. Windows pipes are created
with ``PIPE_REJECT_REMOTE_CLIENTS`` so other computers are refused (not yet checked on a
native Windows install); the shared-key handshake guards every connection either way.
"""

from __future__ import annotations

from .client import CoreUnavailableError, HandshakeRejected, RemoteConsumerAPI, ServerNotTrusted, connect
from .protocol import (
    CONSUMER_METHODS, DELIVERY_PULL, DELIVERY_PUSH, EXPERIMENTAL, PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS,
    RemoteCoreError, WireError, api_versions_compatible, decode, default_address, default_authkey_path,
    default_family, encode, load_or_create_authkey, schema_fingerprint,
)
from .server import (
    AccessDenied, AccessPolicy, ClientHello, ClientInfo, CorePumpThread, CoreServer, Grant, PrincipalPolicy,
    ReadOnlyPolicy, as_registrar,
)

__all__ = [
    "AccessDenied", "AccessPolicy", "CONSUMER_METHODS", "ClientHello", "ClientInfo", "CorePumpThread",
    "CoreServer", "CoreUnavailableError", "DELIVERY_PULL", "DELIVERY_PUSH", "EXPERIMENTAL", "Grant",
    "HandshakeRejected", "PROTOCOL_VERSION", "PrincipalPolicy", "ReadOnlyPolicy", "RemoteConsumerAPI",
    "RemoteCoreError", "SUPPORTED_PROTOCOL_VERSIONS", "ServerNotTrusted", "WireError", "api_versions_compatible",
    "connect", "decode", "default_address", "default_authkey_path", "default_family", "encode",
    "as_registrar", "load_or_create_authkey", "schema_fingerprint",
]
