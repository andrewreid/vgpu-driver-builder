"""Tests for cli.main dispatcher."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vgpu_driver_operator import cli


class TestCliMain:
    def test_help_exits_0(self):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--help"])
        assert exc_info.value.code == 0

    def test_poll_flatcar_help_exits_0(self):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["poll-flatcar", "--help"])
        assert exc_info.value.code == 0

    @patch("vgpu_driver_operator.cli._run_poll_flatcar")
    def test_poll_flatcar_subcommand_dispatches(self, mock_run):
        mock_run.return_value = 0
        rc = cli.main(["poll-flatcar"])
        assert rc == 0
        mock_run.assert_called_once_with(dry_run=False)

    @patch("vgpu_driver_operator.cli._run_poll_flatcar")
    def test_poll_flatcar_dry_run_flag(self, mock_run):
        mock_run.return_value = 0
        rc = cli.main(["poll-flatcar", "--dry-run"])
        assert rc == 0
        mock_run.assert_called_once_with(dry_run=True)

    @patch("vgpu_driver_operator.cli._run_controller")
    def test_controller_subcommand(self, mock_run):
        mock_run.return_value = 0
        rc = cli.main(["controller"])
        assert rc == 0
        mock_run.assert_called_once()

    @patch("vgpu_driver_operator.cli._run_controller")
    def test_no_subcommand_defaults_to_controller(self, mock_run):
        mock_run.return_value = 0
        rc = cli.main([])
        assert rc == 0
        mock_run.assert_called_once()

    @patch("vgpu_driver_operator.poller.run_once")
    @patch("vgpu_driver_operator.poller._configure_k8s")
    def test_run_poll_flatcar_dry_run(self, mock_cfg, mock_run_once):
        mock_run_once.return_value = 0
        rc = cli._run_poll_flatcar(dry_run=True)
        assert rc == 0
        # run_once called with a mock api that returns empty items
        mock_run_once.assert_called_once()
        call_kwargs = mock_run_once.call_args[1]
        api = call_kwargs["custom_api"]
        result = api.list_cluster_custom_object()
        assert result == {"items": []}

    @patch("vgpu_driver_operator.poller.run_once")
    @patch("vgpu_driver_operator.poller._configure_k8s")
    def test_run_poll_flatcar_no_dry_run(self, mock_cfg, mock_run_once):
        mock_run_once.return_value = 0
        rc = cli._run_poll_flatcar(dry_run=False)
        assert rc == 0
        mock_run_once.assert_called_once_with()

    def test_run_controller_calls_kopf_run(self):
        # Patch kopf.run to avoid actually starting the operator.
        with patch("kopf.run") as mock_kopf_run:
            mock_kopf_run.return_value = None
            rc = cli._run_controller()
        assert rc == 0
        mock_kopf_run.assert_called_once_with(standalone=True)
