"""Robustness tests for the session-log reviewer: `python3 test_logreview.py`.

⚠ WHY THIS EXISTS. `logreview.py` is built top-down and indexes `r["kind"]` in seventeen
places, so ONE record without that key raised KeyError on the first summary line and killed
the entire review -- including the `>> PLAYER NOTES` section, which is the single thing
PLAYTEST.md tells the tester to bias toward writing. Losing the human's own notes because a
machine record was malformed is exactly backwards, and it went unnoticed because none of the
107 real session logs has ever contained a player note: the path was never exercised.

Everything here runs the real script as a subprocess against synthetic logs. No game, no
service, no model.
"""
import os, sys, json, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "logreview.py")

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "  " + str(extra)))
    if not cond:
        fails.append(label)

def review(lines):
    """Write a synthetic session log and return the reviewer's combined output."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "session-19700101-000000.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for l in lines:
            fh.write((l if isinstance(l, str) else json.dumps(l)) + "\n")
    r = subprocess.run([sys.executable, SCRIPT, p], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr

NOTE = {"t": 2.0, "kind": "cpp", "msg": "NOTE (p0): the speech bubble never appeared",
        "floor": 3, "map": "The Mines"}

# --- 1. the player-note path, which no real log has ever exercised -------------------------
# The chain is /ailog -> mymod_playerNote -> mymod_log("NOTE (p%d): ...") -> POST {"log":...,
# "src":"cpp"} -> logrec("cpp", msg=...), so what lands here is kind "cpp" with "NOTE" in msg.
rc, out = review([{"t": 1.0, "kind": "start"}, NOTE])
ck("a player note is surfaced", "PLAYER NOTES" in out, out[-400:])
ck("and the note text is shown", "speech bubble never appeared" in out)
ck("and the run exits clean", rc == 0, rc)

# --- 2. one malformed record must not cost the whole review --------------------------------
rc, out = review([{"t": 1.0, "kind": "start"}, {"t": 2.0, "msg": "no kind at all"}, NOTE])
ck("a record with no 'kind' does not kill the run", rc == 0, out[-500:])
ck("it is reported rather than hidden", "no 'kind'" in out)
ck("and the player notes still survive it", "PLAYER NOTES" in out)

# --- 3. the other shapes a half-written log can take ---------------------------------------
rc, out = review([{"t": 1.0, "kind": "start"}, "{not json at all", NOTE])
ck("an unparseable line is skipped, not fatal", rc == 0 and "PLAYER NOTES" in out, out[-300:])
ck("and it is reported", "unparseable" in out)

rc, out = review([{"t": 1.0, "kind": "start"}, "[1,2,3]", NOTE])
ck("a non-object line is skipped, not fatal", rc == 0 and "PLAYER NOTES" in out, out[-300:])

rc, out = review([{"t": 1.0, "kind": "start"}, {"t": 2.0, "kind": "event"}, NOTE])
ck("an 'event' record with no event name is not fatal", rc == 0 and "PLAYER NOTES" in out,
   out[-300:])

# --- 4. an empty log is a no-op, not a traceback -------------------------------------------
rc, out = review([])
ck("an empty log does not traceback", "Traceback" not in out, out[-300:])

# --- 5. the action gate is auditable --------------------------------------------------------
# The service drops an action the player never ordered and keeps what the model wanted in
# `model_action`. A playtest has to be able to see those, or a real order the patterns missed
# would vanish without trace.
rc, out = review([{"t": 1.0, "kind": "start"},
                  {"t": 2.0, "kind": "reply", "uid": 4, "says": "What is your name?",
                   "action": "NONE", "model_action": "WAIT"}])
ck("a dropped action is surfaced", "ACTIONS NOT TAKEN" in out and "What is your name?" in out,
   out[-400:])
ck("and the run exits clean", rc == 0, rc)

print()
print("FAILED: %d" % len(fails) if fails else "ALL PASS (13 assertions)")
sys.exit(1 if fails else 0)
