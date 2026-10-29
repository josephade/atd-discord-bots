import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from instance_lock import single_instance


class InstanceLockTests(unittest.TestCase):
    def test_second_process_is_blocked_then_can_start_after_release(self):
        code = '''
import sys
from instance_lock import single_instance
try:
    with single_instance(sys.argv[1]):
        print('acquired')
except RuntimeError as exc:
    print(str(exc))
    sys.exit(42)
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bot.lock'
            with single_instance(path):
                duplicate = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, timeout=10)
                self.assertEqual(duplicate.returncode, 42)
                self.assertIn('already running', duplicate.stdout)
            restarted = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(restarted.returncode, 0)
            self.assertIn('acquired', restarted.stdout)
