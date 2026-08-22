"""Origin-gated companion logic: persistence across bodies, and the systems that must not
apply to a conjuration or a machine."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
# Also redirect the session log: logreview reads the NEWEST session file, so a test
# run writing into logs/ would masquerade as the last playtest.
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
sys.path.insert(0, "/home/tyler/barony-ai")
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

def summon(uid, player=0, slot="skeleton knight"):
    return S.get_follower_state(uid, "skeleton", player, "summon", slot)
def bot(uid, player=0, kind="sentrybot"):
    return S.get_follower_state(uid, kind, player, "bot", kind)

S.reset_run()

# --- 1. a resummoned knight IS the same creature -------------------------------------
st = summon(100)
st["friendship"], st["trust"] = 44, 30
st["name"] = "Vessik"
S.record_event(100, "skeleton", "fought_alongside", 3, 0, "summon", "skeleton knight")
before = dict(st)
st2 = summon(777)                      # recast: brand-new uid, same slot
ck("resummon reuses the SAME state object", st2 is st)
ck("friendship survives the recast", st2["friendship"] == before["friendship"], st2["friendship"])
ck("the name survives", st2["name"] == "Vessik")
ck("event log survives", len(st2["event_log"]) == len(before["event_log"]) >= 1)
ck("body count increments", st2["bodies"] == 2, st2.get("bodies"))
ck("old uid still resolves to it", S.follower_state[100] is st2)
ck("state knows its current body", st2["uid"] == 777)

# --- 2. the two summon slots are separate creatures -----------------------------------
sent = summon(778, 0, "skeleton sentinel")
ck("knight and sentinel are different creatures", sent is not st)
ck("sentinel starts fresh", sent["friendship"] == 0 and not sent["name"])

# --- 3. and separate per player (co-op) ----------------------------------------------
p1 = summon(779, 1, "skeleton knight")
ck("another player's knight is their own", p1 is not st)

# --- 4. bots persist per type, per player --------------------------------------------
b1 = bot(200); b1["friendship"] = 12
b2 = bot(888)                          # picked up and re-thrown
ck("redeployed bot keeps its state", b2 is b1 and b2["friendship"] == 12)
ck("a different bot type is different", bot(889, 0, "gyrobot") is not b1)

# --- 5. an ordinary recruit is untouched ---------------------------------------------
r1 = S.get_follower_state(300, "goblin", 0)
r2 = S.get_follower_state(301, "goblin", 0)
ck("recruits are still uid-keyed", r1 is not r2)
ck("recruits carry no origin", r1.get("origin", "") == "")
ck("recruit allegiance still rolls normally",
   all(S.roll_allegiance() in ("loyal", "self_interested", "fearful", "spy") for _ in range(50)))

# --- 6. no spies among conjurations or machines --------------------------------------
S.reset_run()
alleg = set()
for i in range(200):
    alleg.add(summon(1000 + i, 0, "knight%d" % i)["allegiance"])
    alleg.add(bot(2000 + i, 0, "bot%d" % i)["allegiance"])
ck("summons and bots are never spies", alleg == {"bound", "machine"}, alleg)
ck("no spy motive assigned",
   all(not S.follower_state[1000 + i].get("motive") for i in range(200)))
ck("the spy crack stays silent for them",
   S.spy_crack_section(dict(S.follower_state[1000], friendship=60)) == "")
ck("allegiance_section stays silent for them",
   S.allegiance_section(S.follower_state[1000]) == "")

# --- 7. no boons -----------------------------------------------------------------------
S.reset_run()
sm = summon(400); sm["friendship"] = 90
bt = bot(401);    bt["friendship"] = 90
ck("a summon never offers a boon", all(S.boon_roll(sm, f) is None for f in range(1, 60)))
ck("a bot never offers a boon", all(S.boon_roll(bt, f) is None for f in range(1, 60)))
rc = S.get_follower_state(402, "goblin", 0); rc["friendship"] = 90
ck("a recruit still can (control)",
   any(S.boon_roll(rc, f) is not None for f in range(1, 200)))

# --- 8. no Herx secret from something conjured minutes ago -----------------------------
S.reset_run()
def herx_ready(st):
    st.update(name="Vessik", friendship=60)
    st["event_log"] = [{"type": "fought_alongside"} for _ in range(6)]
    return st
ck("a skeleton knight cannot hold the Baron's secret",
   not S.herx_eligible(herx_ready(summon(500)), "skeleton"))
ck("a recruited skeleton still can (control)",
   S.herx_eligible(herx_ready(S.get_follower_state(501, "skeleton", 0)), "skeleton"))

# --- 9. a new run forgets every identity ----------------------------------------------
S.reset_run()
ck("identity table cleared", not S.PERSISTENT_IDENTITY)
fresh = summon(600)
ck("next run's knight is a stranger", fresh["friendship"] == 0 and fresh["bodies"] == 1)

# --- 10. emplacements: sentrybots and spellbots cannot move ---------------------------
ck("sentrybot is an emplacement", S.is_emplacement("sentrybot"))
ck("spellbot is an emplacement", S.is_emplacement("spellbot"))
ck("gyrobot is NOT", not S.is_emplacement("gyrobot"))
ck("dummybot is NOT", not S.is_emplacement("dummybot"))
ck("nor is any recruit", not any(S.is_emplacement(r) for r in ("goblin", "human", "automaton")))
sec = S._emplacement_section("sentrybot")
ck("the block forbids the route by name, not just the fact",
   "NEVER follow" in sec and "WRONG" in sec, sec[:80])
ck("mobile bots get no block", S._emplacement_section("gyrobot") == "")
S.reset_run()
turret = S.get_follower_state(900, "sentrybot", 0, "bot", "sentrybot")
turret["friendship"] = 40
ck("the block lands AFTER the obedience section",
   S._follower_sections(900, "sentrybot", 3, "come here").rstrip().endswith(
       S._emplacement_section("sentrybot").rstrip()))

# --- 11. key hygiene -------------------------------------------------------------------
ck("charmed followers are uid-keyed", S._identity_key("charmed", "goblin", 0) == "")
ck("recruits are uid-keyed", S._identity_key("", "", 0) == "")
ck("multi-word slots normalise",
   S._identity_key("summon", "Skeleton Knight", 0) == S._identity_key("summon", "skeleton_knight", 0))
ck("a summon with no key falls back to uid", S._identity_key("summon", "", 0) == "")

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
