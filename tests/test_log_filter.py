import logging
import sys

import pytest


def test_dropped_window_connections_are_not_logged_as_errors():
    sys.argv = ["app.py"]
    app = pytest.importorskip("app")
    f = app._ClosedConnectionNoise()

    def record(msg, exc):
        try:
            raise exc
        except Exception:
            return logging.LogRecord("asyncio", logging.ERROR, "", 0, msg, None, sys.exc_info())

    noisy = record("Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)", ConnectionResetError())
    real = record("Exception in callback something_else()", ValueError("boom"))
    assert f.filter(noisy) is False and f.filter(real) is True
