"""The `resummoned` event: fires on rebind, coalesces, and never becomes farmable."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
# Also redirect the session log: logreview reads the NEWEST session file, so a test
# run writing into logs/ would masquerade as the last playtest.
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
sys.path.insert(0, "/home/tyler/barony-ai")
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

def recast(uid, floor, slot="skeleton knight", race="skeleton", origin="summon", player=0):
    S.get_follower_state(uid, race, player, origin, slot)
    S.flush_resummon(uid, race, floor, player)
    return S.follower_state[uid]

def logof(st, t="resummoned"):
    return [e for e in st.get("event_log", []) if e["type"] == t]

S.reset_run()

# --- 1. the first body is not a resummon ----------------------------------------------
st = recast(10, 1)
ck("first summon records nothing", logof(st) == [], logof(st))
ck("no resummon count yet", st.get("resummons", 0) == 0)

# --- 2. a recast does ------------------------------------------------------------------
st = recast(11, 2)
ck("recast records the event", len(logof(st)) == 1)
ck("claim says 'once'", "once" in logof(st)[0]["claim"], logof(st)[0]["claim"])
ck("claim carries the floor", "floor 2" in logof(st)[0]["claim"])
ck("dependence moved", st["dependence"] > 0, st["dependence"])
ck("friendship did NOT", st["friendship"] == 0, st["friendship"])
ck("resentment did NOT", st["resentment"] == 0)
ck("fear did NOT", st["fear"] == 0)

# --- 3. it coalesces instead of flooding ----------------------------------------------
for i, floor in enumerate(range(3, 33)):
    st = recast(20 + i, floor)
ck("still exactly ONE record after 31 recasts", len(logof(st)) == 1, len(logof(st)))
ck("count is accurate", "31 times" in logof(st)[0]["claim"], logof(st)[0]["claim"])
ck("floor is the latest", "floor 32" in logof(st)[0]["claim"], logof(st)[0]["claim"])
ck("bodies matches", st["bodies"] == 32, st["bodies"])
ck("friendship is STILL zero after 31 recasts", st["friendship"] == 0, st["friendship"])
ck("resentment is still zero", st["resentment"] == 0, st["resentment"])
ck("dependence saturates rather than overflowing", 0 < st["dependence"] <= 100, st["dependence"])

# --- 4. it reaches the prompt, and does not crowd it out ------------------------------
for e in range(4):
    S.record_event(st["uid"], "skeleton", "fought_alongside", 32, 0)
lines = S.events_for_prompt(st)
ck("the memory block mentions it", any("called you back" in l for l in lines), lines)
ck("and only once", sum(1 for l in lines if "called you back" in l) == 1)
ck("other memories still fit", any("fought beside" in l for l in lines), lines)

# --- 5. bots get their own wording ----------------------------------------------------
S.reset_run()
b = recast(50, 4, "sentrybot", "sentrybot", "bot")
b = recast(51, 5, "sentrybot", "sentrybot", "bot")
ck("bot claim is not 'unmade'", "unmade" not in logof(b)[0]["claim"], logof(b)[0]["claim"])
ck("bot claim reads right", "set you out again" in logof(b)[0]["claim"], logof(b)[0]["claim"])

# --- 6. ordinary recruits can never get it --------------------------------------------
S.reset_run()
r = S.get_follower_state(60, "goblin", 0)
S.flush_resummon(60, "goblin", 3, 0)
r2 = S.get_follower_state(61, "goblin", 0)          # a second, unrelated goblin
S.flush_resummon(61, "goblin", 3, 0)
ck("recruits never record a resummon", logof(r) == [] and logof(r2) == [])

# --- 7. ordering: the recall is recorded before whatever prompted the lookup ----------
S.reset_run()
recast(70, 1)
S.get_follower_state(71, "skeleton", 0, "summon", "skeleton knight")
S.record_event(71, "skeleton", "fought_alongside", 6, 0, "summon", "skeleton knight")
types = [e["type"] for e in S.follower_state[71]["event_log"]]
ck("resummon lands before the event that flushed it",
   types.index("resummoned") < types.index("fought_alongside"), types)

# --- 8. flush is idempotent and safe on anything --------------------------------------
before = len(S.follower_state[71]["event_log"])
for _ in range(5):
    S.flush_resummon(71, "skeleton", 6, 0)
ck("flushing again is a no-op", len(S.follower_state[71]["event_log"]) == before)
S.flush_resummon(999999, "goblin", 1, 0)
ck("flushing an unknown uid is harmless", True)

# --- 9. a new run forgets the count ----------------------------------------------------
S.reset_run()
fresh = recast(80, 1)
ck("count resets with the run", fresh.get("resummons", 0) == 0 and logof(fresh) == [])

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
