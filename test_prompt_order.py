"""Prompt-block ordering, asserted rather than described.

At 8B, whichever of two conflicting instructions lands LATER wins. This project has measured
that six times, and three of those were bugs found only by watching a subsystem quietly fail:

  * the secret offer said "this overrides your limits" ~600 chars BEFORE the limits  (40% -> 81%)
  * the spy crack landed after the secret override and ate the twins secret         (30% -> 71%)
  * the introduction lands last of all and ate it too                               (0/8)

Every one of those was documented in a comment at the time. None was enforced. These assertions
turn the ordering rules into something that fails a test instead of quietly costing conversion.

Two kinds of check:
  ORDER      block A must appear before block B
  STAND-DOWN when one mandatory unprompted behaviour is on this turn, no OTHER one may land
             after it -- the general form of both bugs above
"""
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

# The blocks whose position is load-bearing. Keep these strings in sync with service.py; a
# marker that stops matching makes the whole file vacuously pass, so `present` is asserted too.
CANON     = "CANON ABOUT YOUR KIND"
OFFER     = "A SECRET YOU HAVE CARRIED"
LIMITS    = "HARD LIMITS ON WHAT YOU KNOW"
OVERRIDE  = "THE ONE EXCEPTION TO THE LIMITS ABOVE"
OBEDIENCE = "OBEDIENCE:"
ACTIONS   = "Valid actions:"
CRACK     = "YOU ARE CRACKING"
INTRO     = "YOU ARE INTRODUCING YOURSELF"

# Mandatory unprompted behaviours: blocks that TELL THE MODEL IT MUST SAY SOMETHING this turn.
# Two of these in one reply means the later one takes it. This list is the point of the file --
# add to it whenever such a block is introduced.
MANDATORY = [("the spy crack", CRACK), ("the introduction", INTRO)]


def follower(uid, boss="herx", alleg="loyal", introduced=True, race=None):
    cfg = S.BOSS_SECRETS[boss]
    S.reset_run()
    for b in S.BOSS_SECRETS:
        S.SECRET_STATE[b]["pending"].clear()
        S.SECRET_STATE[b]["revealed"] = False
    S.LAST_SABOTAGE.pop(uid, None)
    S.MERC_LATE.pop(uid, None)
    race = race or cfg["races"][0]
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, race, 0)
    st.update(allegiance=alleg, name="Kalthok", introduced=introduced,
              friendship=cfg["friendship"] + 10)
    st["event_log"] = [{"type": "fought_alongside", "floor": 3, "claim": "x",
                        "importance": "notable", "provenance": "participated"}
                       for _ in range(cfg["fights"] + 2)]
    return st, race, max(cfg["floor"], 1)


def build(uid, boss="herx", alleg="loyal", offer=True, says=None, introduced=True, **kw):
    st, race, floor = follower(uid, boss, alleg, introduced)
    _roll = S.secret_roll
    S.secret_roll = (lambda *a, **k: True) if offer else (lambda *a, **k: False)
    try:
        return S.build_prompt(race, floor,
                              says if says is not None else "what do you know about the Baron?",
                              uid, 0, "Ada", 1, "", **kw)
    finally:
        S.secret_roll = _roll


def before(p, a, b):
    """a must appear, b must appear, and a must come first."""
    return a in p and b in p and p.index(a) < p.index(b)


# --- 1. the documented chain --------------------------------------------------------------
p = build(1)
ck("the offer is present at all", OFFER in p)
ck("the limits are present at all", LIMITS in p)
ck("the override is present at all", OVERRIDE in p)
ck("canon lands before the limits", before(p, CANON, LIMITS))
ck("the offer lands before the limits", before(p, OFFER, LIMITS))
# ⚠ The one that cost 40% -> 81%. The carve-out is worthless before the rule it carves out of.
ck("the override lands AFTER the limits it carves out of", before(p, LIMITS, OVERRIDE))
ck("the obedience block lands before the limits", before(p, OBEDIENCE, LIMITS))

# --- 2. nothing mandatory may land after the override --------------------------------------
# The general form of the crack bug AND the intro bug. Checked for a loyal follower and for a
# spy, because the spy is the one that carries a second mandatory block.
for alleg in ("loyal", "spy"):
    q = build(2, alleg=alleg)
    ck("secret pending, %s: override present" % alleg, OVERRIDE in q)
    for name, marker in MANDATORY:
        ck("secret pending, %s: %s does not land after the override" % (alleg, name),
           marker not in q or q.index(marker) < q.index(OVERRIDE),
           "%s at %s, override at %s" % (name, q.find(marker), q.find(OVERRIDE)))

# --- 3. the crack still fires when it should ----------------------------------------------
# The stand-downs must not be so broad that the block never appears -- that would make section
# 2 pass for the wrong reason.
q = build(3, alleg="spy", offer=False, says="how are you holding up?")
ck("a cracking spy with no offer still cracks", CRACK in q)
ck("and the crack is the LAST mandatory block, after the action rules",
   before(q, ACTIONS, CRACK))

# --- 4. the crack stands down, one reason at a time ---------------------------------------
ck("stands down for a pending secret", CRACK not in build(4, alleg="spy"))

q = build(5, alleg="spy", offer=False, says="what is this?", ident="a dull ring")
ck("stands down for an identify request", CRACK not in q)

st, race, floor = follower(6, alleg="spy")
S.LAST_SABOTAGE[6] = "minotaur"
_roll = S.secret_roll; S.secret_roll = lambda *a, **k: False
q = S.build_prompt(race, floor, "hello", 6, 0, "Ada", 1, "")
S.secret_roll = _roll; S.LAST_SABOTAGE.pop(6, None)
ck("stands down for a pending sabotage", CRACK not in q)

# --- 5. the introduction and the secret never share a turn ---------------------------------
# The intro goes into MERC_LATE and is popped at the very END of build_prompt, after the
# override, so it takes the turn. secret_eligible now requires `introduced`.
q = build(7, introduced=False)
ck("an un-introduced follower is not offered a secret", OFFER not in q)
ck("and the introduction is what fires instead", INTRO in q)
ck("secret_eligible requires an introduction",
   not S.herx_eligible(follower(8, introduced=False)[0], "human"))
ck("and allows one that has happened", S.herx_eligible(follower(9)[0], "human"))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
