"""probe_network — smoke/repair probe network must fail closed.

A node that declares egress routes its probe to the shared egress network.
If that network is missing, the probe must get NO network (``none``), never
the open default ``bridge`` — falling back to bridge would silently defeat
the egress declaration (C21, declared-egress-and-sealed-nodes).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

from _common import probe_network  # noqa: E402


def test_undeclared_egress_is_none_without_touching_docker():
    with patch("_common.subprocess.run") as run:
        assert probe_network({}) == "none"
        assert probe_network({"allowed_egress": []}) == "none"
        assert probe_network({"allowed_egress": None}) == "none"
    run.assert_not_called()


def test_declared_egress_with_network_present_uses_egress_net(monkeypatch):
    monkeypatch.delenv("MOD_WIZARD_EGRESS_NETWORK", raising=False)
    with patch("_common.subprocess.run", return_value=MagicMock(returncode=0)) as run:
        assert probe_network({"allowed_egress": ["github.com"]}) == "mod-user-egress"
    assert run.call_args[0][0] == ["docker", "network", "inspect", "mod-user-egress"]


def test_declared_egress_honours_network_override(monkeypatch):
    monkeypatch.setenv("MOD_WIZARD_EGRESS_NETWORK", "custom-egress")
    with patch("_common.subprocess.run", return_value=MagicMock(returncode=0)):
        assert probe_network({"allowed_egress": ["api.example.com"]}) == "custom-egress"


def test_declared_egress_with_network_missing_fails_closed(monkeypatch, capsys):
    monkeypatch.delenv("MOD_WIZARD_EGRESS_NETWORK", raising=False)
    with patch("_common.subprocess.run", return_value=MagicMock(returncode=1)):
        result = probe_network({"allowed_egress": ["github.com"]})
    assert result == "none"
    assert result != "bridge"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "mod-user-egress" in err
    assert "failing closed" in err


def test_declared_egress_inspect_error_fails_closed(capsys):
    with patch("_common.subprocess.run", side_effect=FileNotFoundError("docker")):
        result = probe_network({"allowed_egress": ["github.com"]})
    assert result == "none"
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "failing closed" in err
