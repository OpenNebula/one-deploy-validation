"""Receiver identity checks; full transaction/rollback is tested by Ansible fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

RECEIVER = Path(__file__).resolve().parents[2] / 'roles/observability_validation/files/alert_receiver.py'


class ReceiverTests(unittest.TestCase):
    def test_only_current_firing_alerts_produce_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            process = subprocess.Popen([sys.executable, str(RECEIVER), directory, 'unique', '--lifetime', '10'])
            try:
                deadline = time.monotonic() + 5
                while not (work / 'receiver.json').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                state = lambda: json.loads((work / 'receiver.json').read_text())
                port = state()['port']
                def post(run='unique', source='fe-0', status='firing', receiver='ODVValidation_unique', path='unique'):
                    body = {'receiver': receiver, 'alerts': [{'status': status, 'labels': {
                        'alertname': 'ODVValidation_' + run, 'odv_run_id': run, 'odv_source': source}}]}
                    request = urllib.request.Request('http://127.0.0.1:%d/%s' % (port, path), data=json.dumps(body).encode())
                    with urllib.request.urlopen(request, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                post(run='another')
                post(status='resolved')
                self.assertEqual(state()['sources'], [])
                with self.assertRaises(urllib.error.HTTPError):
                    post(receiver='ordinary')
                with self.assertRaises(urllib.error.HTTPError):
                    post(path='wrong')
                post()
                post(source='fe-1')
                post()
                self.assertEqual(state()['sources'], ['fe-0', 'fe-1'])
            finally:
                (work / 'stop').touch()
                process.wait(timeout=7)
            self.assertEqual(process.returncode, 0)

    def test_receiver_exits_when_lifetime_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(RECEIVER), directory, 'unique', '--lifetime', '1'], timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads((Path(directory) / 'receiver.json').read_text())['sources'], [])


if __name__ == '__main__':
    unittest.main()
