"""Shared test fixtures.

Tests run without a real database — we use simple dataclass stand-ins for
SQLAlchemy models so the math modules can be exercised in isolation. Anything
needing a DB belongs in an integration-test suite, not here.
"""

import os
import sys

# Ensure the project root is on the import path so `import app.*` works
# without needing pip install -e .
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
