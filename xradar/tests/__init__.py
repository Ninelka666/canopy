"""Test package. Silences xradar's own error logging: several tests exercise
failure paths on purpose, and their log lines would otherwise look like
test failures."""

import logging

logging.getLogger("xradar").addHandler(logging.NullHandler())
logging.getLogger("xradar").propagate = False
