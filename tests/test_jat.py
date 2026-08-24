import os
import sys

import pytest

from josh_room.jat import JATError, _run


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-specific")
def test_jat_timeout_returns_bounded_metadata_and_terminates_process_group():
    with pytest.raises(JATError) as error:
        _run([sys.executable, "-c", "import time; time.sleep(60)"], 0.01)
    assert error.value.result["timed_out"] is True
    assert error.value.result["exit_status"] is None
    assert len(error.value.result["argv"]) == 3
