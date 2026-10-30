import sys
import uuid

import pytest

from cod_region_filter import app


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex")
def test_second_instance_is_refused():
    # The real mutex is held whenever the app is running, so use a private name.
    name = f"Local\\CODPingKing.Test.{uuid.uuid4()}"
    assert app.acquire_single_instance(name) is True
    assert app.acquire_single_instance(name) is False
