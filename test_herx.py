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

def ready_keep(uid, alleg="loyal", race="human"):
    """ready() clears HERX_STATE; this one leaves the run's reveal latch alone."""
    saved = dict(S.HERX_STATE)
    st = ready(uid, alleg, race)
    S.HERX_STATE.update(saved)
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

# --- 7. the SPY path end to end -----------------------------------------------------------
# The endpoints were covered (a spy is offered from the false pool; a false secret reports
# debuff 0) but nothing drove herx_detect with is_false=True, so the middle of the spy arc had
# never executed. This is the branch the README calls invisible.
spy = ready(50, "spy")
S._secret_section(50, spy, "human", "tell me about herx")
ck("a spy has a false offer pending", S.HERX_STATE["pending"].get(50, (None, None))[1] is True)
vi, isf = S.HERX_STATE["pending"][50]
S.herx_detect(50, "{}", "Listen: %s, that is the truth of it."
              % S._herx_pool(isf)[vi]["keywords"][0], 0)
ck("a told FALSE secret is recorded", S.HERX_STATE["revealed"] and S.HERX_STATE["uid"] == 50)
ck("and it is marked false", S.HERX_STATE["is_false"] is True)
ck("the recorded variant came from the false pool",
   S._herx_pool(True)[S.HERX_STATE["variant"]] in S.HERX_FALSE_VARIANTS)
ck("the engine is told debuff 0", S.herx_secret_field().startswith("0:"), S.herx_secret_field())
ck("a spy's lie still burns the one reveal per run",
   not S.herx_eligible(ready_keep(51), "human"))

# --- 8. a keyword that is also ordinary talk cannot fire alone ----------------------------
# Each line below tells nothing. Before the weak-keyword rule every one revealed the secret:
# the true-pool ones handed the engine a real debuff, and "given name"/"true name" collide
# with the naming pass that runs on the same turn, a few lines above the herx_detect() call.
def talked(uid, pool_is_false, kw, line):
    """Put a specific variant on offer, say `line`, report whether it counted as told."""
    S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                         "is_false": False, "player": 0})
    pool = S._herx_pool(pool_is_false)
    vi = next(i for i, v in enumerate(pool) if kw in v["keywords"])
    S.HERX_STATE["pending"][uid] = (vi, pool_is_false)
    S.herx_detect(uid, "{}", line, 0)
    return S.HERX_STATE["revealed"]

for kw, line in [("silver",     "I found a silver ring in that chest, take it."),
                 ("given name", "My given name is Kalthok, if you must know."),
                 ("true name",  "That is not my true name, but it will do.")]:
    ck("false pool: %r alone is not a telling" % kw, not talked(60, True, kw, line), line)

# ⚠ The mirror of the above, and the more important half. These were demoted too at first,
# which cost 7 genuine tellings in 100 real generations and prevented nothing -- the keywords
# come FROM the truth lines, so a real paraphrase lands on them. Measured, then reverted.
for kw, line in [("thinner", "He's... thinner than he looks."),
                 ("seep",    "Fight him near the seep on the wall and he will not follow."),
                 ("picks",   "The picks broke him, that is what I heard.")]:
    ck("%r alone IS a telling (measured genuine, never incidental)" % kw,
       talked(61, kw == "seep", kw, line), line)

ck("and a dropped offer is still cleared from pending", 61 not in S.HERX_STATE["pending"])

# --- 9. weak keywords still corroborate ---------------------------------------------------
# Demoting them must not cost recall: a real paraphrase lands several, and two is enough.
ck("two weak hits together ARE a telling",
   talked(62, True, "true name", "They say his true name is not the given name we use."))
ck("one strong hit alone is still a telling",
   talked(63, False, "picks", "Strike his left side, that is where he is soft."))

# --- 10. the reply schema's own key is not the model saying "secret" ----------------------
# `"secret":` appears in every structured reply, so counting it revealed the secret on any
# turn an offer was pending, regardless of what was actually said.
S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                     "is_false": False, "player": 0})
S.HERX_STATE["pending"][70] = (0, True)
S.herx_detect(70, '{"reply":"nothing to say","secret":""}', "Nothing to say.", 0)
ck('the schema own "secret" key does not count', not S.HERX_STATE["revealed"])

S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                     "is_false": False, "player": 0})
S.HERX_STATE["pending"][71] = (0, True)
S.herx_detect(71, 'I will tell you the "secret" I carry.', "I will tell you.", 0)
ck('but the model writing "secret" still counts', S.HERX_STATE["revealed"])


# --- 11. the log record says WHICH keyword decided it ------------------------------------
# A `revealed` record with no matched text cannot be audited afterwards, which is why the
# false positives above were invisible in real sessions. `near` records the hits that were
# not enough, so the weak/strong split can be judged against real play instead of guesswork.
import json as _json

def _last_herx():
    with open(S.LOG_PATH, encoding="utf-8") as fh:
        recs = [_json.loads(l) for l in fh if l.strip()]
    return [r for r in recs if r.get("kind") == "herx"][-1]

talked(80, False, "picks", "Strike his left side, that is where he is soft.")
r = _last_herx()
ck("a reveal records the matched keyword", "left side" in (r.get("matched") or ""), r)
ck("and that it fired on a strong keyword", r.get("via") == "strong", r)

talked(81, True, "true name", "They say his true name is not the given name we use.")
r = _last_herx()
ck("a two-weak-hit reveal is marked corroborated", r.get("via") == "corroborated", r)
ck("and it names both weak keywords", r.get("matched") == "true name,given name", r)

talked(82, True, "silver", "I found a silver ring in that chest.")
r = _last_herx()
ck("a drop records the near miss", r.get("stage") == "dropped" and r.get("near") == "silver", r)

talked(83, False, "picks", "Nice weather for it.")
r = _last_herx()
ck("a drop with no hits at all records no near", "near" not in r, r)


# --- 12. the spy crack stands down for a pending secret -----------------------------------
# Both are mandatory unprompted behaviours and the crack is appended LAST, so without this it
# takes the turn. The comment at the call site always said it stood down for a pending secret;
# the condition did not implement it. Measured 30% vs 71% conversion for the twins false
# secret (herx 70% vs 71%, i.e. unaffected -- its truths are short enough to fit alongside).
def crack_prompt(uid, offer):
    S.HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                         "is_false": False, "player": 0})
    for b in S.BOSS_SECRETS:
        S.SECRET_STATE[b]["pending"].clear()
        S.SECRET_STATE[b]["revealed"] = False
    cfg = S.BOSS_SECRETS["twins"]
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, cfg["races"][0], 0)
    st.update(allegiance="spy", name="Kalthok", friendship=cfg["friendship"] + 10,
              introduced=True)
    st["event_log"] = [{"type": "fought_alongside", "floor": 3, "claim": "x",
                        "importance": "notable", "provenance": "participated"}
                       for _ in range(cfg["fights"] + 2)]
    _roll = S.secret_roll
    S.secret_roll = (lambda *a, **k: True) if offer else (lambda *a, **k: False)
    try:
        return S.build_prompt(cfg["races"][0], 25,
                              "what do you know about the Archmagisters?", uid, 0, "Ada", 1, "")
    finally:
        S.secret_roll = _roll

p_offer = crack_prompt(90, True)
ck("an offer suppresses the spy crack", "YOU ARE CRACKING" not in p_offer)
ck("and the secret offer is still there", "A SECRET YOU HAVE CARRIED" in p_offer)
ck("a cracking spy with no offer still cracks", "YOU ARE CRACKING" in crack_prompt(91, False))
# secret_pending is what the stand-down keys on, so assert it directly rather than inferring
# it from the prompt text.
crack_prompt(92, True)
ck("secret_pending is True while an offer is outstanding", S.secret_pending(92) is True)
ck("and False for a follower with no offer", S.secret_pending(9999) is False)
S.herx_detect(92, "{}", "Nice weather for it.", 0)
ck("and False again once the offer is resolved", S.secret_pending(92) is False)


print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
