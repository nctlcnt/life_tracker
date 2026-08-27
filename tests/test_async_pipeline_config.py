import pytest

import config


@pytest.mark.parametrize(
    ("outbound", "worker", "apply"),
    [
        (False, False, False),
        (True, False, False),
        (True, True, False),
        (True, True, True),
    ],
)
def test_valid_async_pipeline_stages(outbound, worker, apply):
    config._validate_async_pipeline_flags(
        outbound=outbound, worker=worker, apply=apply)


@pytest.mark.parametrize(
    ("outbound", "worker", "apply"),
    [
        (False, True, False),
        (False, False, True),
        (False, True, True),
        (True, False, True),
    ],
)
def test_invalid_async_pipeline_stages_fail_fast(outbound, worker, apply):
    with pytest.raises(ValueError):
        config._validate_async_pipeline_flags(
            outbound=outbound, worker=worker, apply=apply)
