"""CLI entry-point dispatcher for vgpu-driver-operator.

Subcommands
-----------
controller
    Run the kopf operator (default when no subcommand is given).
poll-flatcar
    Run one poller pass and exit.  The CronJob marks failed when this exits
    non-zero, which happens on any HTTP/network error.
"""

from __future__ import annotations

import argparse
import http.server
import sys
import threading
import urllib.parse


def main(argv: list[str] | None = None) -> int:
    """Parse *argv* and dispatch to the appropriate subcommand.

    Returns an integer exit code (0 = success).
    """
    parser = argparse.ArgumentParser(
        prog="vgpu-driver-operator",
        description="vGPU driver image operator for Flatcar Linux clusters.",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # controller subcommand
    controller_p = sub.add_parser(
        "controller",
        help="Run the kopf operator controller (default).",
    )
    controller_p.add_argument(
        "--liveness",
        metavar="URL",
        default=None,
        help=(
            "Start an HTTP health server at this URL "
            "(e.g. http://0.0.0.0:8080/healthz). "
            "Responds 200 on the configured path, 404 otherwise."
        ),
    )

    # poll-flatcar subcommand
    poll_p = sub.add_parser(
        "poll-flatcar",
        help="Poll Flatcar release feeds and patch VGPUDriverImage status.",
    )
    poll_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Skip real Kubernetes API calls (list nothing, patch nothing). "
            "Useful for smoke-testing without a live cluster."
        ),
    )

    args = parser.parse_args(argv)

    # Default to 'controller' when no subcommand is given.
    if args.subcommand is None or args.subcommand == "controller":
        return _run_controller(liveness_url=getattr(args, "liveness", None))

    if args.subcommand == "poll-flatcar":
        return _run_poll_flatcar(dry_run=getattr(args, "dry_run", False))

    parser.print_help()
    return 1


def _start_health_server(url: str) -> None:
    """Parse *url* and start a daemon HTTP health server thread."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "0.0.0.0"
    port = parsed.port or 8080
    health_path = parsed.path or "/healthz"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == health_path:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_: object) -> None:
            pass  # silence access log noise

    server = http.server.ThreadingHTTPServer((host, port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


def _run_controller(liveness_url: str | None = None) -> int:
    """Start the kopf operator.  Blocks until the process is terminated."""
    if liveness_url:
        _start_health_server(liveness_url)

    # Import main here so kopf decorators register before kopf.run() is called.
    import vgpu_driver_operator.main  # noqa: F401  (side-effect: registers handlers)
    import kopf  # type: ignore[import-untyped]

    kopf.run(standalone=True)
    return 0


def _run_poll_flatcar(*, dry_run: bool = False) -> int:
    """Run one poller pass."""
    if dry_run:
        # Dry-run: mock the Kubernetes API to return nothing.
        from unittest.mock import MagicMock

        mock_api = MagicMock()
        mock_api.list_cluster_custom_object.return_value = {"items": []}

        from vgpu_driver_operator import poller as _poller

        return _poller.run_once(custom_api=mock_api)

    from vgpu_driver_operator import poller as _poller

    return _poller.run_once()
