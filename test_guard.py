"""A trusted follower heading off the minotaur -- the positive counterpart to sabotage."""
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

def guard(uid=1, trust=55, alleg="loyal", origin=""):
    S.GUARD_STATE.update({"minotaur": False, "sokoban": False})
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0, origin, origin and "skeleton knight")
    st.update(trust=trust, allegiance=alleg)
    return st

def catches(st, tries=400):
    for _ in range(tries):
        S.GUARD_STATE.update({"minotaur": False, "sokoban": False})
        if S.favour_roll("minotaur", st): return True
    return False

# --- 1. trust is the gate, and it is the right one -------------------------------------
# Trust is otherwise only read by the disclosure tiers and the obedience formula; this is what
# it means -- not "do they like you" but "will they act for you when it matters".
ck("a trusted companion catches it", catches(guard(trust=S.GUARD_MIN_TRUST)))
ck("one you have not earned does not", not catches(guard(trust=S.GUARD_MIN_TRUST - 1)))
ck("nor does a stranger", not catches(guard(trust=0)))
# Friendship is deliberately NOT the gate: being liked is not the same as being relied on.
st = guard(trust=5); st["friendship"] = 100
ck("being adored is not enough without trust", not catches(st))

# --- 2. who cannot ---------------------------------------------------------------------
ck("not the spy who called it", not catches(guard(trust=90, alleg="spy")))
ck("not a conjuration", not catches(guard(trust=90, origin="summon")))

# --- 3. once per run -------------------------------------------------------------------
st = guard(trust=90)
S.GUARD_STATE.update({"minotaur": False, "sokoban": False})
first = any(S.favour_roll("minotaur", st) for _ in range(200))
ck("it fires", first)
ck("and never twice in a run", not any(S.favour_roll("minotaur", st) for _ in range(200)))
S.reset_run()
ck("a new run re-arms it", not S.GUARD_STATE["minotaur"])

# --- 4. it is not certain, so the minotaur stays a threat -------------------------------
S.reset_run()
hits = 0
for i in range(600):
    S.GUARD_STATE.update({"minotaur": False, "sokoban": False})
    S.follower_state.pop(500 + i, None)
    st = S.get_follower_state(500 + i, "human", 0); st.update(trust=90, allegiance="loyal")
    if S.favour_roll("minotaur", st): hits += 1
rate = hits / 600
ck("even the right companion sometimes misses", 0.6 < rate < 0.8, "%.2f" % rate)

# --- 5. the prompt does not hand the model the mechanism -------------------------------
p = S.build_guard_prompt("human", 11, "The Labyrinth", True)
# The word appears only inside the prohibition -- naming the forbidden route is the technique
# that works everywhere else in this project. What matters is whether the OUTPUT says it, and
# measured live it did not: 0 of 7 replies named a minotaur.
ck("it forbids naming the minotaur", "not name a minotaur" in p.lower())
ck("and never offers it as something to say",
   p.lower().count("minotaur") == 1, p.lower().count("minotaur"))
ck("it forbids explaining how", "Do NOT explain the mechanism" in p)
ck("it says where they are", "Labyrinth" in p)
pf = S.build_guard_prompt("human", 11, "The Labyrinth", False)
ck("the failure line admits it cannot be stopped", "cannot" in pf)
ck("and forbids naming it too", "not name a minotaur" in pf.lower())
ck("neither carries book text", len(p) < 3000 and len(pf) < 3000, (len(p), len(pf)))

# --- 6. the Sokoban favour: skipping a puzzle the player does not enjoy -----------------
def sok(uid=90, f=35, alleg="loyal", origin=""):
    S.GUARD_STATE.update({"minotaur": False, "sokoban": False})
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "gnome", 0, origin, origin and "skeleton knight")
    st.update(friendship=f, allegiance=alleg)
    return st

def solves(st, tries=400):
    for _ in range(tries):
        S.GUARD_STATE["sokoban"] = False
        if S.favour_roll("sokoban", st): return True
    return False

# Friendship, not trust: this is a chore done out of fondness, not a judgement call.
ck("a companion you have travelled with solves it", solves(sok(f=S.SOKOBAN_MIN_FRIENDSHIP)))
ck("someone you just met does not", not solves(sok(f=S.SOKOBAN_MIN_FRIENDSHIP - 1)))
ck("not a spy", not solves(sok(f=90, alleg="spy")))
ck("not a conjuration", not solves(sok(f=90, origin="summon")))
st = sok(f=90)
ck("it fires", any(S.favour_roll("sokoban", st) for _ in range(200)))
ck("and only once per run", not any(S.favour_roll("sokoban", st) for _ in range(200)))

# The two favours have independent budgets: solving the puzzle must not use up the one
# chance a companion has to head off a minotaur later.
S.reset_run()
st = S.get_follower_state(95, "gnome", 0); st.update(friendship=90, trust=90, allegiance="loyal")
any(S.favour_roll("sokoban", st) for _ in range(200))
ck("spending the sokoban favour leaves the minotaur one",
   any(S.favour_roll("minotaur", st) for _ in range(200)))
S.reset_run()
ck("a new run re-arms both",
   not S.GUARD_STATE["minotaur"] and not S.GUARD_STATE["sokoban"])
ck("an unknown favour is refused", not S.favour_roll("nonsense", sok(f=90)))

p = S.build_sokoban_prompt("gnome", 7, "Sokoban", True)
ck("the prompt owns the outcome, not the method",
   "shoved the lot of them into the pits" in p, p[-260:])
ck("it tells them to own the gold they cost you", "knocked a bit of the gold in" in p)
ck("and forbids boasting", "Do NOT boast" in p)
ck("the refusal branch is short and plain",
   "not going to attempt it" in S.build_sokoban_prompt("gnome", 7, "Sokoban", False))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
