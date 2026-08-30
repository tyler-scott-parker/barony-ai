"""The Archmagisters, Orpheus and Erudyce: the endgame secret arc, and a spy's lie that BUFFS."""
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

def ready(uid, alleg="loyal", race="human", fights=8):
    for b in S.SECRET_STATE:
        S.SECRET_STATE[b].clear(); S.SECRET_STATE[b].update(S._fresh_secret_state())
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, race, 0)
    st.update(allegiance=alleg, name="Kalthok", friendship=70, introduced=True)
    st["event_log"] = [{"type": "fought_alongside", "floor": 30, "claim": "x",
                        "importance": "notable", "provenance": "participated"}
                       for _ in range(fights)]
    return st

# --- 1. the gate is its own, and harder than Herx's -------------------------------------
ck("harder friendship than Herx", S.TWINS_MIN_FRIENDSHIP > S.HERX_MIN_FRIENDSHIP)
ck("and more fights", S.TWINS_MIN_FIGHTS > S.HERX_MIN_FIGHTS)
st = ready(1)
ck("eligible deep in the run", S.secret_eligible("twins", st, "human", 30))
# ⚠ Nobody down in the mines is discussing the Archmagisters.
ck("but never before Hamlet", not S.secret_eligible("twins", st, "human", 10))
ck("a skeleton cannot know them", not S.secret_eligible("twins", ready(2, race="skeleton"),
                                                        "skeleton", 30))
ck("a gnome can", S.secret_eligible("twins", ready(3, race="gnome"), "gnome", 30))
# A conjuration has no history with the Guild, same rule as the Herx arc.
for b in S.SECRET_STATE:
    S.SECRET_STATE[b].clear(); S.SECRET_STATE[b].update(S._fresh_secret_state())
S.follower_state.pop(4, None)
_sum = S.get_follower_state(4, "human", 0, "summon", "skeleton knight")
_sum.update(name="Kalthok", friendship=70, allegiance="loyal", introduced=True)
_sum["event_log"] = [{"type": "fought_alongside", "floor": 30, "claim": "x",
                      "importance": "notable", "provenance": "participated"} for _ in range(8)]
ck("a summon never can", not S.secret_eligible("twins", _sum, "human", 30))

# --- 2. every variant names which twin, and the false ones BUFF -------------------------
ck("every true variant targets one twin",
   all(v.get("who") in (1, 2) for v in S.TWINS_SECRET_VARIANTS))
ck("both twins are covered",
   {v["who"] for v in S.TWINS_SECRET_VARIANTS} == {1, 2})
ck("true variants debuff", all(v["debuff"] > 0 for v in S.TWINS_SECRET_VARIANTS))
# This is the difference from Herx, where a lie merely reports 0 and does nothing.
ck("a spy's lie BUFFS instead of doing nothing",
   all(v["debuff"] < 0 for v in S.TWINS_FALSE_VARIANTS))
ck("Herx's lies still merely do nothing",
   all(v["debuff"] == 0 for v in S.HERX_FALSE_VARIANTS))

# ⚠ Every variant must NAME the sibling it means. Left with a pronoun the model swapped them --
# "Orpheus won't act while HER brother's alive", mechanically targeting Erudyce while telling
# the player to leave Orpheus for last. A true secret that misdirects is worse than none, and a
# lie should misdirect onto the twin the buff actually lands on.
_unnamed = [v["truth"][:50] for v in S.TWINS_SECRET_VARIANTS + S.TWINS_FALSE_VARIANTS
            if ("Erudyce" if v["who"] == 1 else "Orpheus") not in v["truth"]]
ck("every variant names its own target", not _unnamed, _unnamed)
ck("and every one names both siblings or is unambiguous",
   all("Orpheus" in v["truth"] or "Erudyce" in v["truth"]
       for v in S.TWINS_SECRET_VARIANTS + S.TWINS_FALSE_VARIANTS))

# --- 3. the field the engine reads ------------------------------------------------------
S.SECRET_STATE["twins"].update({"revealed": True, "variant": 0, "uid": 55,
                                "is_false": False, "player": 0})
f = S.herx_secret_field()
ck("twins field carries the boss and the target", f.endswith(":twins:%d" % S.TWINS_SECRET_VARIANTS[0]["who"]), f)
ck("and the debuff and informant", f.startswith("%d:55:" % S.TWINS_SECRET_VARIANTS[0]["debuff"]), f)
S.SECRET_STATE["twins"].update({"revealed": True, "variant": 0, "is_false": True})
ck("a lie reports a negative number", S.herx_secret_field().startswith("-"), S.herx_secret_field())
# Herx keeps its original two-field shape, which the C++ still parses the old way.
for b in S.SECRET_STATE:
    S.SECRET_STATE[b].clear(); S.SECRET_STATE[b].update(S._fresh_secret_state())
S.SECRET_STATE["herx"].update({"revealed": True, "variant": 2, "uid": 9, "is_false": False})
ck("Herx's field is unchanged", S.herx_secret_field() == "%d:9" % S.HERX_SECRET_VARIANTS[2]["debuff"],
   S.herx_secret_field())
ck("and carries no twins marker", ":twins:" not in S.herx_secret_field())

# --- 4. the two arcs are independent ----------------------------------------------------
st = ready(10)
S.SECRET_STATE["herx"].update({"revealed": True, "uid": 999, "variant": 0})
ck("Herx being told does not close the twins", S.secret_eligible("twins", st, "human", 30))
ck("but Herx itself is closed", not S.secret_eligible("herx", st, "human", 30))
S.reset_run()
ck("a new run reopens both",
   not S.SECRET_STATE["herx"]["revealed"] and not S.SECRET_STATE["twins"]["revealed"])
ck("and HERX_STATE still aliases the same dict", S.HERX_STATE is S.SECRET_STATE["herx"])

# --- 5. telling one boss's secret must not close the other's ----------------------------
# ⚠ Found by audit, not by play. _secret_section returned early on the first ALREADY-TOLD
# match, so a follower who told you about Herx could never be offered the twins -- and since
# the twins gate is strictly harder than Herx's, that is precisely the follower who qualifies.
# The gate said open (checked above); the live path offered 0 in 200.
st = ready(50)
S.SECRET_STATE["herx"].update({"revealed": True, "variant": 0, "uid": 50, "is_false": False})
_offers = _reminders = 0
for _ in range(200):
    _out = S._secret_section(50, st, "human", "what do you know about the Archmagisters?", 30)
    if "A SECRET YOU HAVE CARRIED" in _out: _offers += 1
    if "ALREADY TOLD" in _out: _reminders += 1
    S.SECRET_STATE["twins"]["pending"].clear()
ck("having told Herx does not close the twins in the LIVE path", _offers > 0, _offers)
ck("and the consistency reminder is still carried every time", _reminders == 200, _reminders)

# --- 6. both secrets told: the twins must still reach the engine ------------------------
# ⚠ The field carries one secret and Herx used to win it forever, so a successfully told
# endgame secret was silently discarded. Safe to order twins first: they cannot be told before
# floor 25 and Herx spawns on floor 20, so initLich has already applied his.
for b in S.SECRET_STATE:
    S.SECRET_STATE[b].clear(); S.SECRET_STATE[b].update(S._fresh_secret_state())
S.SECRET_STATE["herx"].update({"revealed": True, "variant": 2, "uid": 9, "is_false": False})
ck("Herx alone still reports the two-field form",
   S.herx_secret_field() == "%d:9" % S.HERX_SECRET_VARIANTS[2]["debuff"], S.herx_secret_field())
S.SECRET_STATE["twins"].update({"revealed": True, "variant": 3, "uid": 222, "is_false": False})
_f = S.herx_secret_field()
ck("with both told, the twins field is the one sent", ":twins:" in _f, _f)
ck("and it names the right twin",
   _f == "%d:222:twins:%d" % (S.TWINS_SECRET_VARIANTS[3]["debuff"],
                              S.TWINS_SECRET_VARIANTS[3]["who"]), _f)

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
