"""Spy sabotage: the first thing a follower can do that spends the player's run against them,
so the gating matters more than the mechanism."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

def spy(uid=1, alleg="spy", f=25, origin=""):
    S.SABOTAGE_STATE["used"] = False
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0, origin, origin and "skeleton knight")
    st.update(allegiance=alleg, friendship=f)
    return st

def fires(st, floor, tries=4000):
    """Does the GATE let this through at all? Clears the once-per-spy saboteur decision each
    try as well as the run latch -- otherwise a spy that rolled 'watcher' once can never fire,
    and this would be testing that decision rather than the gate. The saboteur share is
    checked separately below."""
    for _ in range(tries):
        S.SABOTAGE_STATE["used"] = False
        st.pop("saboteur", None)
        if S.sabotage_roll(st, floor): return True
    return False

# --- 1. only a spy, and only once ------------------------------------------------------
ck("a spy can", fires(spy(), 10))
for a in ("loyal", "self_interested", "fearful", "bound", "machine"):
    ck("a %s never does" % a, not fires(spy(2, a), 10))
st = spy(3)
S.SABOTAGE_STATE["used"] = False
first = any(S.sabotage_roll(st, 10) for _ in range(4000))
ck("it fires at all", first)
ck("and never twice in a run", not any(S.sabotage_roll(st, 10) for _ in range(4000)))
S.reset_run()
ck("a new run re-arms it", not S.SABOTAGE_STATE["used"])

# --- 2. floors where it would be a mugging rather than a betrayal ----------------------
for f in (1, 2, 3, 4):
    ck("never on floor %d (opening mines)" % f, not fires(spy(), f))
for f in (20, 22, 24, 25, 30):
    ck("never on floor %d (boss/hell/town/late)" % f, not fires(spy(), f))
ck("fires in the middle of a run (floor 12)", fires(spy(), 12))

# --- 3. a stranger sabotaging you is not a betrayal either -----------------------------
ck("not from a follower you barely know", not fires(spy(4, "spy", 5), 10))
ck("once they are embedded, yes", fires(spy(5, "spy", S.SABOTAGE_MIN_FRIENDSHIP), 10))
# A conjuration has no outside to signal to.
ck("never from a summon", not fires(spy(6, "spy", 25, "summon"), 10))

# --- 4. the tell has to sound like nothing ---------------------------------------------
sec = S.sabotage_section(S.SABOTAGE_TELLS[0])
ck("the tell is in the block", S.SABOTAGE_TELLS[0] in sec)
# ⚠ Everything else in the prompt tells this character it is a traitor, so it will happily
# foreshadow. A tell that telegraphs is a warning, and the player just leaves the floor.
for word in ("nervous", "ominous", "warn"):
    ck("it forbids sounding %s" % word, word in sec)
ck("it says outright this is not a confession", "not confessing" in sec)
ck("and not the crack", "not cracking" in sec)
ck("there are several tells, so it is not the same line each run", len(S.SABOTAGE_TELLS) >= 5)
ck("no tell hints at the minotaur",
   not any("minotaur" in t.lower() or "danger" in t.lower() for t in S.SABOTAGE_TELLS))

# --- 4b. once per PLAYTHROUGH, not once per follower -----------------------------------
# A party can hold several spies. The latch is run-global so three of them still produce one
# sabotage, not three.
S.reset_run()
party = []
for i in range(3):
    st = S.get_follower_state(2000 + i, "human", 0)
    st.update(allegiance="spy", friendship=30, saboteur=True)
    party.append(st)
fired = 0
for floor in range(5, 20):
    for st in party:
        for _ in range(200):
            if S.sabotage_roll(st, floor): fired += 1
ck("three spies over fifteen floors still fire it once", fired == 1, fired)
S.reset_run()
ck("and a new playthrough re-arms it", not S.SABOTAGE_STATE["used"])

# --- 4c. most spies are watchers, not saboteurs ----------------------------------------
# ⚠ The per-reply chance is nearly meaningless: an embedded spy gets ~30 eligible replies, so
# 12% per reply fires essentially always. Measured, the per-run rate tracked the 7% spy rate
# exactly -- every spy sabotaged. The decision is made once per spy instead.
S.reset_run()
saboteurs = 0
for i in range(600):
    S.SABOTAGE_STATE["used"] = False
    S.follower_state.pop(3000 + i, None)
    st = S.get_follower_state(3000 + i, "human", 0)
    st.update(allegiance="spy", friendship=30)
    for _ in range(80):
        if S.sabotage_roll(st, 10): break
    if st.get("saboteur"): saboteurs += 1
share = saboteurs / 600
ck("only a minority of spies are saboteurs", 0.30 < share < 0.50, "%.2f" % share)
st = S.get_follower_state(3000, "human", 0)
ck("and the decision is remembered, not re-rolled", "saboteur" in st)

# --- 5. it does not stack with the other unprompted behaviours -------------------------
S.reset_run()
st = spy(10, "spy", 60)
st["name"] = "Kalthok"
st["event_log"] = [{"type": "fought_alongside", "floor": 10, "claim": "x",
                    "importance": "notable", "provenance": "participated"} for _ in range(6)]
S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                     "is_false": False, "player": 0})
S.random.seed(3)
saw_both = 0
for _ in range(400):
    S.SABOTAGE_STATE["used"] = False
    S.HERX_STATE.update({"revealed": False, "pending": {}})
    S.LAST_SABOTAGE.clear()
    body = S._follower_sections(10, "human", 10, "what now?")
    if "A SECRET YOU HAVE CARRIED" in body and "SOMETHING YOU ARE DOING" in body:
        saw_both += 1
ck("a secret and a sabotage never share a reply", saw_both == 0, saw_both)

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
