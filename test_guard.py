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
    S.GUARD_STATE["used"] = False
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0, origin, origin and "skeleton knight")
    st.update(trust=trust, allegiance=alleg)
    return st

def catches(st, tries=400):
    for _ in range(tries):
        S.GUARD_STATE["used"] = False
        if S.guard_roll(st): return True
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
S.GUARD_STATE["used"] = False
first = any(S.guard_roll(st) for _ in range(200))
ck("it fires", first)
ck("and never twice in a run", not any(S.guard_roll(st) for _ in range(200)))
S.reset_run()
ck("a new run re-arms it", not S.GUARD_STATE["used"])

# --- 4. it is not certain, so the minotaur stays a threat -------------------------------
S.reset_run()
hits = 0
for i in range(600):
    S.GUARD_STATE["used"] = False
    S.follower_state.pop(500 + i, None)
    st = S.get_follower_state(500 + i, "human", 0); st.update(trust=90, allegiance="loyal")
    if S.guard_roll(st): hits += 1
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

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
