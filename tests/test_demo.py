"""Run the Docker-free demonstration as part of ordinary pytest."""
from hookrelay.reliability_demo import run_demo


def test_reliability_demo_runs_successfully():
    messages = []
    run_demo(output=messages.append)
    assert len([message for message in messages if message.startswith("PASS ")]) == 5
