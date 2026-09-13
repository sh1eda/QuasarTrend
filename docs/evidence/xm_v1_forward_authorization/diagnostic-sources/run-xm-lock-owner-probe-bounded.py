"""Run only the explicit synthetic lock-owner probe with a 60-second limit."""
import subprocess
import sys
raise SystemExit(subprocess.run([sys.executable, sys.argv[1], '--repo', sys.argv[2], '--output', sys.argv[3]], timeout=60).returncode)
