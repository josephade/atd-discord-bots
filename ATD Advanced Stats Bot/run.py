#!/usr/bin/env python3
"""
ATD Advanced Stats Bot - Entry point.
Runs the Node.js bot (bot.js) via subprocess.
"""

import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
result = subprocess.run(["node", "bot.js"], check=False)
sys.exit(result.returncode)
