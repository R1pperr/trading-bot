"""
Imported first by every test file: points the files the bot writes during a run at a
temporary folder, so tests never touch the real control switches, AI health count, activity
feed or watchdog state, and never call the real engine over the internet.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity      # noqa: E402
import ai_analyst    # noqa: E402
import fixes         # noqa: E402
import watchdog      # noqa: E402

FOLDER = tempfile.mkdtemp(prefix="tach-tests-")
fixes.CONTROL_FILE = os.path.join(FOLDER, "control.json")
ai_analyst.HEALTH_FILE = os.path.join(FOLDER, "ai_health.json")
activity.FEED_FILE = os.path.join(FOLDER, "activity.json")
watchdog.STATE_FILE = os.path.join(FOLDER, "watchdog.json")
watchdog.SITE_FILE = os.path.join(FOLDER, "site.json")
