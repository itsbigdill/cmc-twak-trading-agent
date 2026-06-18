import os
import sys

import yaml
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def cfg():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)
