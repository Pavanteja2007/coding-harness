"""P5/W1 T5 — the loopback-only pin for the OpenAI-compatible endpoint.

An endpoint that accepts ``/v1/chat/completions`` and runs tools is an agent
with a network identity. The pin: it is loopback-only by default, and the
refusal of a non-loopback bind is real. Fast and deterministic — this belongs
to the blocking smoke lane.
"""

from __future__ import annotations

import urllib.request

import pytest

from agent_sdk.server import AgentServer
from agent_sdk.errors import InvalidRequestError


class TestTheEndpointIsLoopbackOnly:
    def test_the_default_host_is_loopback(self, tmp_path):
        server = AgentServer(str(tmp_path))
        assert server.host == "127.0.0.1"
        server.stop()

    def test_a_non_loopback_bind_is_refused_without_the_flag(self, tmp_path):
        with pytest.raises(InvalidRequestError, match="loopback"):
            AgentServer(str(tmp_path), host="0.0.0.0")

    def test_a_non_loopback_bind_still_needs_the_flag_and_a_token(self, tmp_path):
        # The SDK refuses without the flag; the CLI plan refuses without a token.
        with pytest.raises(InvalidRequestError):
            AgentServer(str(tmp_path), host="0.0.0.0")
        from cli import serve

        with pytest.raises(PermissionError, match="unauthenticated"):
            serve.build_serve_plan(
                repo=tmp_path, host="0.0.0.0", port=0, allow_non_loopback=True, token=""
            )

    def test_a_real_bind_never_leaves_loopback(self, tmp_path):
        server = AgentServer(str(tmp_path), port=0).start()
        try:
            host, _port = server.address
            assert host == "127.0.0.1"
            with urllib.request.urlopen(server.url + "/health", timeout=5) as response:
                assert response.status == 200
        finally:
            server.stop()

    def test_version_negotiation_stays_fail_closed(self):
        """Already claimed; verified here as a pin of the public contract."""
        from acp.models import negotiate_protocol_version

        with pytest.raises(Exception):
            negotiate_protocol_version(2, {1})
        assert negotiate_protocol_version(1, {1}) == 1
