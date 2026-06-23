import pytest
from api import app

def test_app_imports():
    assert app is not None

# Add more endpoint tests as needed
