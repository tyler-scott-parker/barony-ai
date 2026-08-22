"""The fearful allegiance: a liability in a fight who is the reason you survive the floor."""
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

def f(uid=1, alleg="fearful", trust=20, floor=17):
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0)
    st.update(allegiance=alleg, trust=trust, friendship=30)
    return st

# --- 1. frightened, not bitter ---------------------------------------------------------
a, b = f(1), f(2, alleg="loyal")
S.adjust(a, fear=10, resentment=10)
S.adjust(b, fear=10, resentment=10)
ck("fear lands harder on them", a["fear"] > b["fear"], (a["fear"], b["fear"]))
ck("resentment barely sticks", a["resentment"] < b["resentment"], (a["resentment"], b["resentment"]))
# Reassurance must still work at full strength, or they can never be calmed down.
before = a["fear"]
S.adjust(a, fear=-5)
ck("but calming them works at the normal rate", a["fear"] == before - 5, a["fear"])
c = f(3, alleg="fearful")
_t0 = c["trust"]
S.adjust(c, trust=10)
ck("dimensions other than fear and resentment are unscaled", c["trust"] == _t0 + 10, c["trust"])
ck("and other allegiances are unscaled", "loyal" not in S.ALLEGIANCE_FEEL)

# --- 2. the inversion: the jumpy one is watching the door ------------------------------
S.reset_run()
def guards(st, tries=400):
    for _ in range(tries):
        S.GUARD_STATE["minotaur"] = False
        if S.favour_roll("minotaur", st): return True
    return False
ck("a fearful follower catches it on thinner acquaintance",
   guards(f(10, trust=S.GUARD_FEARFUL_TRUST)))
ck("where a loyal one at the same trust would not",
   not guards(f(11, alleg="loyal", trust=S.GUARD_FEARFUL_TRUST)))
ck("the fearful gate is genuinely lower", S.GUARD_FEARFUL_TRUST < S.GUARD_MIN_TRUST)
ck("and they notice more reliably", S.GUARD_FEARFUL_CHANCE > S.GUARD_CHANCE)
# Still not the spy who called it, whatever their nerves.
ck("a fearful spy is impossible but a spy is still refused",
   not guards(f(12, alleg="spy", trust=90)))

# --- 3. they will not go deeper unasked ------------------------------------------------
deep = S._fearful_section(f(20, trust=20), S.FEARFUL_DEEP_FLOOR)
ck("deep and unearned -> refusal block", "YOU DO NOT WANT TO BE THIS DEEP" in deep)
# ⚠ Naming the required speech act, not the mood: "you are frightened" produced a follower
# who agreed cheerfully and mentioned being scared -- the failure the spy crack taught.
ck("it forbids agreeing cheerfully", "Agreeing cheerfully" in deep)
ck("and forbids 'if you insist'", "if you insist" in deep)
ck("but says they can be talked round", "talked round" in deep)
shallow = S._fearful_section(f(21, trust=20), S.FEARFUL_DEEP_FLOOR - 1)
ck("shallow floors carry no refusal", "YOU DO NOT WANT TO BE THIS DEEP" not in shallow)
ck("but still carry the disposition", "frightened" in shallow)
earned = S._fearful_section(f(22, trust=60), S.FEARFUL_DEEP_FLOOR + 3)
ck("earned trust removes the refusal", "YOU DO NOT WANT TO BE THIS DEEP" not in earned)
ck("nobody else gets any of this", S._fearful_section(f(23, alleg="loyal"), 19) == "")

# --- 4. it reaches the assembled prompt ------------------------------------------------
st = f(30, trust=20)
p = S.build_prompt("human", 17, "we're going down", 30, 0, "Ada")
ck("the refusal is in the prompt", "YOU DO NOT WANT TO BE THIS DEEP" in p)
# It has to land AFTER the obedience block -- that block is telling them to carry out orders,
# and at 8B whatever lands later wins. Anchored on the obedience text itself: "REFUSE" also
# appears in the action rules much further down, which made the first version of this check
# pass for the wrong reason.
_ob = S._obedience_section(st)
ck("and after the obedience section",
   p.index("YOU DO NOT WANT TO BE THIS DEEP") > p.index(_ob[:40]), "ordering")
ck("but before the action rules it must not override",
   p.index("YOU DO NOT WANT TO BE THIS DEEP") < p.index("IMPORTANT: If you REFUSE"))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
