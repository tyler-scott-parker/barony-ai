"""The map boon: a curious follower wanders off and comes back knowing the floor."""
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

def who(uid=1, cur=55, f=45, player=0):
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", player)
    st.update(curiosity=cur, friendship=f, allegiance="loyal")
    return st

def maps(st, player=0, tries=3000):
    for _ in range(tries):
        st["last_boon_floor"] = None
        r = S.boon_roll(st, 4, player)
        if r and r[0] == "map": return True
    return False

# --- 1. curiosity is the gate, and it is the point ---------------------------------------
# Nothing else in the system reads curiosity except one behaviour line; this gives it a job,
# and it is exactly the trait of someone who goes and looks.
S.reset_run()
ck("a curious companion maps the floor", maps(who(1, cur=S.BOON_MAP_MIN_CURIOSITY)))
S.reset_run()
ck("an incurious one never does", not maps(who(2, cur=S.BOON_MAP_MIN_CURIOSITY - 1)))
S.reset_run()
ck("nor one who will not share it yet",
   not maps(who(3, cur=90, f=S.BOON_MAP_MIN_FRIENDSHIP - 1)))

# --- 2. once per run, per player ---------------------------------------------------------
S.reset_run()
st = who(10)
ck("it fires", maps(st))
ck("and never twice in a run", not maps(who(11)))
# Co-op: one player's map must not consume another's.
S.reset_run()
ck("player 0 gets theirs", maps(who(12, player=0), player=0))
ck("and player 1 still gets theirs", maps(who(13, player=1), player=1))
S.reset_run()
ck("a new run re-arms it", not S.BOON_STATE["map_used"])

# --- 3. it outranks the good item ---------------------------------------------------------
# A follower who has been off exploring should not hand you bread instead.
S.reset_run()
st = who(20, cur=90, f=90)
firsts = []
for _ in range(400):
    S.BOON_STATE["map_used"].clear()
    S.BOON_STATE["good_used"].clear()
    st["last_boon_floor"] = None
    r = S.boon_roll(st, 4, 0)
    if r: firsts.append(r[0])
ck("map appears in the mix", "map" in firsts, set(firsts))
ck("and so do the others", len(set(firsts)) > 1, set(firsts))

# --- 4. the payload the engine reads ------------------------------------------------------
S.reset_run()
st = who(30, cur=90, f=90)
got = None
for _ in range(3000):
    S.LAST_BOON.clear()
    S.BOON_STATE["map_used"].clear()
    st["last_boon_floor"] = None
    line = S._boon_section(30, st, "human", 4, 0)
    if S.LAST_BOON.get(30) == "map:":
        got = line
        break
ck("the engine payload is 'map:'", got is not None)
ck("the line says it is already done", got and "already done" in got, (got or "")[:80])
# The engine fills the minimap in; a list of directions would be noise on top of it.
ck("and forbids reciting directions", got and "Do NOT list directions" in got)

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
