"""Killing a spy you correctly identified should not cost you the party."""
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

def party(alleg_dead):
    """A spy and two loyal companions, all following the same player."""
    S.reset_run()
    dead = S.get_follower_state(1, "human", 0); dead["allegiance"] = alleg_dead
    a = S.get_follower_state(2, "goblin", 0);   a["allegiance"] = "loyal"
    b = S.get_follower_state(3, "skeleton", 0); b["allegiance"] = "loyal"
    for st in (a, b): st.update(trust=40, fear=0, resentment=0)
    return dead, a, b

def dims(st): return (st["trust"], st["fear"], st["resentment"])

# --- 1. killing a spy costs nothing with the survivors ---------------------------------
dead, a, b = party("spy")
before = (dims(a), dims(b))
S.record_event(2, "goblin",   "ally_died", 8, 0, about=1)
S.record_event(3, "skeleton", "ally_died", 8, 0, about=1)
ck("the survivors' trust is untouched", dims(a)[0] == before[0][0] == 40, dims(a))
ck("no fear added", dims(a)[1] == 0 and dims(b)[1] == 0, (dims(a), dims(b)))
ck("no resentment added", dims(a)[2] == 0 and dims(b)[2] == 0, (dims(a), dims(b)))
ck("and nothing is written into their memory",
   not any(e["type"] == "ally_died" for e in a["event_log"]), a["event_log"])

# --- 2. an ordinary follower dying still lands, or the system means nothing -------------
dead, a, b = party("loyal")
S.record_event(2, "goblin", "ally_died", 8, 0, about=1)
ck("a loyal death still costs trust", a["trust"] < 40, a["trust"])
ck("still frightens them", a["fear"] > 0, a["fear"])
ck("still breeds resentment", a["resentment"] > 0, a["resentment"])
ck("and is remembered", any(e["type"] == "ally_died" for e in a["event_log"]))

# --- 3. the other allegiances are not a free pass ---------------------------------------
for alleg in ("self_interested", "fearful", "bound", "machine"):
    dead, a, b = party(alleg)
    S.record_event(2, "goblin", "ally_died", 8, 0, about=1)
    ck("a %s death is still mourned" % alleg, a["trust"] < 40, (alleg, a["trust"]))

# --- 4. striking the spy costs nothing with the spy either -----------------------------
dead, a, b = party("spy")
dead.update(friendship=20, trust=20, fear=0, resentment=0)
S.record_event(1, "human", "hurt_by_player", 8, 0)
ck("hitting a spy costs no friendship", dead["friendship"] == 20, dead["friendship"])
ck("and no trust", dead["trust"] == 20, dead["trust"])
ck("and breeds no resentment", dead["resentment"] == 0, dead["resentment"])
# ...but hitting anyone else still does, which is the whole point of the event.
dead, a, b = party("loyal")
a.update(friendship=20, trust=20)
S.record_event(2, "goblin", "hurt_by_player", 8, 0)
ck("hitting a loyal follower still hurts", a["friendship"] < 20 and a["trust"] < 20,
   (a["friendship"], a["trust"]))

# --- 5. a death with no `about` (an older event, or an unknown body) still mourns -------
dead, a, b = party("spy")
S.record_event(2, "goblin", "ally_died", 8, 0)          # about defaults to 0
ck("an unattributed death is still mourned", a["trust"] < 40, a["trust"])
ck("an unknown uid is not treated as a spy", not S._deserved_it(999999))
ck("nor is uid 0", not S._deserved_it(0))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
