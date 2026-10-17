"""Exercise the installed NVIDIA startup policy without requiring a GPU."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


PROJECT = Path(__file__).resolve().parent.parent


def startup_script():
    tasks = yaml.safe_load((PROJECT / 'config/nvidia.yml').read_text())
    task = next(task for task in tasks if task['name'] == 'Install the model-specific NVIDIA power policy')
    return task['ansible.builtin.copy']['content']


class NvidiaPowerPolicy(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = self.root / 'calls'
        self.policy = self.root / 'wdc-nvidia-power-limit'
        self.policy.write_text(startup_script())
        self.policy.chmod(0o755)
        smi = self.root / 'nvidia-smi'
        smi.write_text('''#!/bin/sh
if [ "$1" = "--query-gpu=uuid,name" ]; then
    [ "$FAIL_QUERY" != 1 ] || exit 1
    printf '%s\\n' "$GPU_ROWS"
    exit 0
fi
printf '%s\\n' "$*" >> "$GPU_CALLS"
''')
        smi.chmod(0o755)
        logger = self.root / 'logger'
        logger.write_text('#!/bin/sh\nexit 0\n')
        logger.chmod(0o755)

    def run_policy(self, rows='', fail_query=False):
        environment = os.environ.copy()
        environment.update(PATH=str(self.root) + os.pathsep + environment['PATH'],
                           GPU_ROWS=rows, GPU_CALLS=str(self.calls),
                           FAIL_QUERY='1' if fail_query else '0')
        subprocess.run([str(self.policy)], env=environment, check=True, timeout=5)
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_only_high_power_sm120_models_receive_the_limit(self):
        rows = '\n'.join([
            'GPU-5090, NVIDIA GeForce RTX 5090',
            'GPU-W, NVIDIA RTX PRO 6000 Blackwell Workstation Edition',
            'GPU-S, NVIDIA RTX PRO 6000 Blackwell Server Edition',
            'GPU-Q, NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition',
            'GPU-OTHER, NVIDIA GeForce RTX 5080',
        ])
        self.assertEqual(self.run_policy(rows), [
            '-i GPU-5090 -pm 1', '-i GPU-5090 -pl 550',
            '-i GPU-W -pm 1', '-i GPU-W -pl 550',
            '-i GPU-S -pm 1', '-i GPU-S -pl 550',
        ])

    def test_no_gpu_or_unavailable_driver_leaves_power_untouched(self):
        self.assertEqual(self.run_policy(), [])
        self.assertEqual(self.run_policy(fail_query=True), [])
