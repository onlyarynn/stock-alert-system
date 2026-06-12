"""
conftest.py
-----------
Pytest configuration file.
Adds the project root to Python's path so that
'from src.analyzer import ...' works correctly in all tests.
"""

import sys
import os

# Add project root to path so pytest can find the src package
sys.path.insert(0, os.path.dirname(__file__))