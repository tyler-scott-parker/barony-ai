"""The Baron's secret: the true/false split, and the ordering that decides whether it is
ever actually said."""
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

def ready(uid, alleg="loyal", race="human", origin=""):
    S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                         "is_false": False, "player": 0})
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, race, 0, origin, origin and "skeleton knight")
    st.update(allegiance=alleg, name="Kalthok", friendship=55)
    st["event_log"] = [{"type": "fought_alongside", "floor": 3, "claim": "x",
                        "importance": "notable", "provenance": "participated"} for _ in range(6)]
    return st

# --- 1. the gate ------------------------------------------------------------------------
st = ready(1)
ck("a named, trusted, blooded human is eligible", S.herx_eligible(st, "human"))
st["friendship"] = 49
ck("friendship 49 is not enough", not S.herx_eligible(st, "human"))
st["friendship"] = 55; st["name"] = ""
ck("an unnamed follower is not eligible", not S.herx_eligible(st, "human"))
st["name"] = "Kalthok"; st["event_log"] = st["event_log"][:3]
ck("three fights is not enough", not S.herx_eligible(st, "human"))
ck("a goblin is never eligible", not S.herx_eligible(ready(2), "goblin"))
ck("a conjured knight is never eligible",
   not S.herx_eligible(ready(3, origin="summon"), "skeleton"))

# --- 2. the true/false split -----------------------------------------------------------
S.random.seed(7)
loyal = ready(10, "loyal")
S._secret_section(10, loyal, "human", "tell me about herx")
ck("a loyal follower is offered a TRUE secret", S.HERX_STATE["pending"][10][1] is False,
   S.HERX_STATE["pending"])
spy = ready(11, "spy")
S._secret_section(11, spy, "human", "tell me about herx")
ck("a spy is offered a FALSE one", S.HERX_STATE["pending"][11][1] is True, S.HERX_STATE["pending"])
vi, isf = S.HERX_STATE["pending"][11]
ck("the false variant comes from the false pool", S._herx_pool(isf)[vi] in S.HERX_FALSE_VARIANTS)
ck("every false variant reports debuff 0",
   all(v["debuff"] == 0 for v in S.HERX_FALSE_VARIANTS))
ck("every true variant reports a real debuff",
   all(v["debuff"] > 0 for v in S.HERX_SECRET_VARIANTS))

# --- 3. what the engine is told ---------------------------------------------------------
# The C++ side guards on `debuff > 0`, so a false secret must arrive as 0 or it would apply
# a real stat penalty to the boss on a lie.
S.HERX_STATE.update({"revealed": True, "variant": 0, "uid": 77, "is_false": True, "player": 0})
ck("a false secret reports debuff 0 to the engine",
   S.herx_secret_field().startswith("0:"), S.herx_secret_field())
S.HERX_STATE.update({"revealed": True, "variant": 2, "uid": 77, "is_false": False})
ck("a true secret reports its own debuff",
   S.herx_secret_field() == "%d:77" % S.HERX_SECRET_VARIANTS[2]["debuff"], S.herx_secret_field())

# --- 4. ⚠ the ordering bug this test exists for -----------------------------------------
# The offer used to end with "this overrides your limits" while sitting BEFORE the hard-limits
# block, which tells the character to say it does not know and STOP. Whichever lands later
# wins, so the override lost and a loyal follower talked around the payoff of the whole arc:
# 40% of offers converted. Emitting the carve-out after the limits took it to 81%.
st = ready(20, "loyal")
prompt = S.build_prompt("human", 3, "tell me about Baron Herx", 20, 0, "Ada")
i_offer = prompt.find("A SECRET YOU HAVE CARRIED")
i_limits = prompt.find(S.LIMITS_HEADER[:40])
i_over = prompt.find("THE ONE EXCEPTION TO THE LIMITS ABOVE")
ck("the offer is in the prompt", i_offer > 0, i_offer)
ck("the limits are in the prompt", i_limits > 0, i_limits)
ck("the override is in the prompt", i_over > 0, i_over)
ck("the override lands AFTER the limits it carves out of", i_over > i_limits, (i_limits, i_over))
ck("the override names the routes the limits push toward",
   "do not know" in prompt[i_over:i_over+400] and "WRONG" in prompt[i_over:i_over+400])
ck("no override when nothing is pending",
   "THE ONE EXCEPTION TO THE LIMITS ABOVE" not in S.build_prompt("human", 3, "hi", 21, 0, "Ada"))

# --- 5. one reveal per run --------------------------------------------------------------
st = ready(30, "loyal")
S._secret_section(30, st, "human", "herx?")
vi, isf = S.HERX_STATE["pending"][30]
kw = S._herx_pool(isf)[vi]["keywords"][0]
S.herx_detect(30, "{}", "Listen: %s, that is the truth of it." % kw, 0)
ck("a told secret is recorded", S.HERX_STATE["revealed"] and S.HERX_STATE["uid"] == 30)
first = S.herx_secret_field()
st2 = ready(31, "loyal")          # NOTE: ready() resets HERX_STATE, so re-set it
S.HERX_STATE.update({"revealed": True, "variant": vi, "uid": 30, "is_false": isf, "player": 0})
ck("a second follower is no longer eligible", not S.herx_eligible(st2, "human"))

# --- 6. an offer that is talked around is dropped, not left dangling --------------------
S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                     "is_false": False, "player": 0})
st = ready(40, "loyal")
S._secret_section(40, st, "human", "herx?")
ck("an offer is pending", 40 in S.HERX_STATE["pending"])
S.herx_detect(40, "{}", "Nice weather for it.", 0)
ck("talked around -> dropped", 40 not in S.HERX_STATE["pending"])
ck("and nothing was revealed", not S.HERX_STATE["revealed"])

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
