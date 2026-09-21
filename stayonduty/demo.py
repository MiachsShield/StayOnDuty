"""Iteration 1 demo: crash mid-task, restart, watch it resume.

Run: python3 -m stayonduty.demo
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stayonduty.store import Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(HERE, "demo.db")
TASK = None


def run_phase(crash_after):
    env = dict(os.environ, CRASH_AFTER=str(crash_after))
    p = subprocess.run([sys.executable, "-m", "stayonduty.runner", DB, TASK],
                       env=env, capture_output=True, text=True, cwd=ROOT)
    print(p.stdout, end="")
    if p.returncode not in (0, 1):
        print(p.stderr, end="")
    elif not p.stdout.strip():
        # exit 1 with no stdout = the crash path prints via os._exit; anything
        # else silent is suspicious, so surface stderr
        print(p.stderr, end="")
    return p.returncode


for ext in ("", "-wal", "-shm"):
    if os.path.exists(DB + ext):
        os.remove(DB + ext)

s = Store(DB)
TASK = s.create_task("deliver 5 packages", {"items": [1, 2, 3, 4, 5]})
s.close()

print("== phase 1: agent works, then DIES mid-task ==")
run_phase(crash_after=2)

print("== phase 2: agent restarts (lease expires -> task recovered -> resume) ==")
time.sleep(4)  # let the 3s lease expire; a real orchestrator would do this
run_phase(crash_after=0)

s = Store(DB)
t = s.get_task(TASK)
print(f"\n== result: task {t['id']} status={t['status']} attempts={t['attempts']} ==")
print("-- event timeline --")
for e in s.events(TASK):
    print(f"  {e['type']:10s} {e['detail']}")
print("-- memory kept across the crash --")
for k, v in s.memories(scope=f"task:{TASK}").items():
    print(f"  {k} = {v}")
s.close()
