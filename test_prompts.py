"""Prompt shape for the short forms: ambient babble and combat taunts. Both produce a handful
of words and used to pay full conversation cost, and neither knew where it was standing."""
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

def tok(s): return len(s) // 4

# --- 1. they know where they are -------------------------------------------------------
# A shopkeeper on floor 25 is standing in a TOWN. Told only "dungeon floor 25", it muttered
# about the dungeon -- the same bug place_name() was written to fix for conversation.
amb = S.build_ambient_prompt("shopkeeper", 25, "hostile", "Mages Guild")
ck("ambient names the place", S.place_name(25, "Mages Guild") in amb, S.place_name(25, "Mages Guild"))
ck("and it is Hamlet, not the dungeon", "Hamlet" in amb)
tau = S.build_taunt_prompt("demon", 22, "Hell")
ck("taunts name the place too", S.place_name(22, "Hell") in tau, S.place_name(22, "Hell"))
ck("no map name still works", "dungeon floor 3" in S.build_ambient_prompt("rat", 3, "hostile", ""))

# --- 2. ⚠ the book block was the entire prompt-size swing -------------------------------
# Only four races have book text at all; for a human it was ~1680 of ~1875 ambient tokens,
# paid in full to produce four words of muttering.
ck("a human's book block is the big one", tok(S._book_block("human")) > 1000,
   tok(S._book_block("human")))
ck("ambient carries no book text", tok(S.build_ambient_prompt("human", 3, "hostile", "")) < 400,
   tok(S.build_ambient_prompt("human", 3, "hostile", "")))
ck("nor do taunts", tok(S.build_taunt_prompt("human", 3, "")) < 600,
   tok(S.build_taunt_prompt("human", 3, "")))
# ...but conversation keeps them, which is where a follower actually draws on them.
ck("conversation still carries them",
   tok(S.build_prompt("human", 3, "hello", 0, 0, "Ada")) > 2000,
   tok(S.build_prompt("human", 3, "hello", 0, 0, "Ada")))

# --- 3. the spread across races is what "uneven cost" meant -----------------------------
sizes = {r: tok(S.build_ambient_prompt(r, 3, "hostile", "")) for r in list(S.RACE_LORE)[1:]}
lo, hi = min(sizes.values()), max(sizes.values())
ck("ambient is now flat across every race", hi - lo < 150, "%d..%d" % (lo, hi))
ck("no race pays more than ~400 tokens to mutter", hi < 400, hi)
tsizes = {r: tok(S.build_taunt_prompt(r, 3, "")) for r in list(S.RACE_LORE)[1:]}
ck("taunts are bounded too", max(tsizes.values()) < 700, max(tsizes.values()))

# --- 4. taunts stay grounded, ambient stays deliberately not ----------------------------
# A taunt makes claims worth constraining; a four-word mutter has no room to.
ck("taunts still carry canon", "ABOUT THIS PLACE" in tau or "CANON" in tau or "FACTS" in tau.upper(),
   tau[:0])
ck("ambient carries no hard limits", S.LIMITS_HEADER[:30] not in amb)
ck("conversation still carries hard limits",
   S.LIMITS_HEADER[:30] in S.build_prompt("human", 3, "hello", 0, 0, "Ada"))

# --- 5. the setting and character guidance survive the trim -----------------------------
ck("ambient still sets the world", S.WORLD[:40] in amb)
ck("ambient still carries race guidance", "CHARACTER GUIDANCE" in amb)

# --- the valuable-pickup remark ---------------------------------------------------------
# A follower reacting to something you just picked up. Another short form, and the same rules:
# it must know where it is, it must be cheap, and it must not say more than it knows.
val = S.build_valuable_prompt("gnome", 25, "Mages Guild", "clear gemstone", "gem", 3000, "loyal")
ck("the remark knows it is in a town, not a dungeon", "Hamlet" in val)
ck("and it is a SHORT form", tok(val) < 500, tok(val))
ck("no book block, like the other short forms", "FROM THE BOOKS" not in val)

# ⚠ THE BALANCE-CRITICAL LINE. Most followers cannot appraise a legendary item at all, so
# naming one here would hand out free exactly what the appraisal tiers gate -- and would
# contradict the same follower calling it beyond them a moment later.
ck("it forbids naming the item", "Do NOT name it" in val)
ck("and forbids guessing what it does", "do NOT guess at its powers" in val)
ck("it says what they may talk about instead", "how it LOOKS" in val)

# Bands, so a 1200-gold gem and an artifact do not read the same.
ck("an artifact sits in the top band", "lifetime" in S.valuable_band(3000))
ck("a 2000-gold ring in the middle", "serious piece of money" in S.valuable_band(2000))
ck("and an aquamarine in the lowest", "usual scrapings" in S.valuable_band(1200))
ck("bands are ordered high to low", [b[0] for b in S.VALUABLE_BANDS]
   == sorted([b[0] for b in S.VALUABLE_BANDS], reverse=True))

# ⚠ A SPY HAS NO REACTION OF ITS OWN -- the handler maps it to the loyal one before building
# the prompt. A spy who eyes your treasure differently is a tell the player gets for free.
ck("no spy-specific reaction exists to leak", "spy" not in S.VALUABLE_ANGLE)
ck("a mercenary does have one", "self_interested" in S.VALUABLE_ANGLE)
ck("and a fearful follower worries about attention",
   "attention" in S.VALUABLE_ANGLE["fearful"])
ck("the mercenary does not tout for the appraisal here",
   "do NOT offer to do it yourself" in S.VALUABLE_ANGLE["self_interested"])

# --- chest, drunk and mimic -----------------------------------------------------------------
for k in ("chest", "drunk", "mimic"):
    pr = S.build_remark_prompt(k, "goblin", 7, "swamp", "loyal")
    ck("the %s remark is a short form" % k, tok(pr) < 500, tok(pr))
    ck("the %s remark knows where it is" % k, "Swamp" in pr or "swamp" in pr)
    ck("the %s remark asks for one spoken line" % k, "ONLY one short spoken line" in pr)

# ⚠ It is the ADVENTURER who is drunk. Left implicit the model writes ITSELF as the drunk one.
ck("the drunk remark says whose state it is",
   "not you" in S.REMARK_SITUATION["drunk"])
# ⚠ A mimic is not a chest that transforms -- map generation replaces the chest with a MIMIC
# monster (maps.cpp:10893) -- but from the follower's side it is "that was never a chest".
ck("the mimic remark says it is happening now",
   "RIGHT NOW" in S.REMARK_SITUATION["mimic"])

# ⚠ A mercenary invents a gold figure if allowed to: measured, he answered an opened chest with
# "Sixty-five gold pieces, a decent haul". He cannot know it and nothing displays it -- the same
# error the haggle block exists to stop. 0/6 named a figure after this.
ck("a mercenary is forbidden numbers in a remark",
   "Naming any number is WRONG" in S.build_remark_prompt("chest", "gnome", 7, "swamp",
                                                         "self_interested"))
ck("and in a valuable-pickup remark too",
   "Naming any number is WRONG" in S.build_valuable_prompt("gnome", 7, "swamp", "gem stone",
                                                           "gem", 3000, "self_interested"))
ck("but nobody else is burdened with that line",
   "Naming any number is WRONG" not in S.build_remark_prompt("chest", "gnome", 7, "swamp",
                                                             "loyal"))

# ⚠ DO NOT re-add a "do not change the subject to the floor" line to the drunk situation.
# Naming those TOPICS primed them: the measured rate fell to 2/8 and the misses all wrote about
# gators, reeds and cypress knees. Naming a forbidden SPEECH ACT works; naming a forbidden
# TOPIC introduces it. Same failure the project recorded for spec 26.
ck("the drunk situation names no topic to avoid",
   "cypress" not in S.REMARK_SITUATION["drunk"]
   and "what lives here" not in S.REMARK_SITUATION["drunk"])

# A spy has no reaction of its own for any kind; the handler maps it to loyal.
for k in ("chest", "drunk", "mimic"):
    ck("no spy-specific %s angle to leak" % k, "spy" not in S.REMARK_ANGLE[k])
    ck("every %s angle has a default" % k, S.REMARK_ANGLE[k].get(None))
ck("a fearful follower dreads chests", "Traps" in S.REMARK_ANGLE["chest"]["fearful"])
ck("and shouts at a mimic", "terrified" in S.REMARK_ANGLE["mimic"]["fearful"])
ck("a machine does not panic", "not panic" in S.REMARK_ANGLE["mimic"]["machine"])
ck("every remark kind has a situation and angles",
   set(S.REMARK_SITUATION) == set(S.REMARK_ANGLE))

# --- hunger, and the automaton's boiler -----------------------------------------------------
# ⚠ AN AUTOMATON DOES NOT GET HUNGRY. The game's own getEntityHungerInterval returns -1 --
# "unreachable" -- for HUNGRY/WEAK/STARVING when the player is an AUTOMATON, and gives it
# SUPERHEATED (1200) and CRITICAL (300) instead. It runs a boiler, not a stomach.
for st_ in ("hungry", "weak", "starving", "oversatiated"):
    pr = S.build_hunger_prompt(st_, "human", 7, "swamp", "loyal")
    ck("the %s prompt exists and is short" % st_, pr and tok(pr) < 500, tok(pr))
    # ⚠ Whose state it is has to be explicit: a troll answered "oversatiated" with "Me belly
    # hurt, bad eating" -- writing ITSELF as the one who overate. 0/9 after saying "not you".
    ck("the %s prompt says whose state it is" % st_, "not you" in pr)

for st_ in ("superheated", "critical"):
    pr = S.build_hunger_prompt(st_, "human", 7, "swamp", "loyal")
    ck("the %s prompt exists" % st_, bool(pr))
    ck("%s forbids food language outright" % st_,
       "any word about eating is WRONG" in pr)
    ck("%s says they do not eat" % st_, "do NOT get hungry" in pr)
    ck("%s names the machine axis instead" % st_,
       ("heat" in pr or "fuel" in pr) and "AUTOMATON" in pr)
ck("superheated is about pressure", "pressure" in S.HUNGER_SITUATION["superheated"])
ck("critical is about the fire going out", "cold" in S.HUNGER_SITUATION["critical"])

# ⚠ A vampire is sustained by blood, not bread -- the engine flags it from the game's own
# playerRequiresBloodToSustain, so nothing about vampires is duplicated here.
ck("the blood note appears when flagged",
   "BLOOD they need" in S.build_hunger_prompt("hungry", "human", 7, "swamp", "loyal", blood=True))
ck("and not otherwise",
   "BLOOD they need" not in S.build_hunger_prompt("hungry", "human", 7, "swamp", "loyal"))
# ⚠ A construct is not a vampire. The engine can only flag one thing, so the blood note must
# never land on top of the boiler states.
ck("a construct is never told it needs blood",
   "BLOOD they need" not in S.build_hunger_prompt("critical", "human", 7, "swamp",
                                                  "loyal", blood=True))

ck("an unknown state produces nothing rather than a wrong line",
   S.build_hunger_prompt("peckish", "human", 7, "swamp", "loyal") == "")
ck("a mercenary is still forbidden figures here",
   "Naming any number is WRONG" in S.build_hunger_prompt("starving", "gnome", 7, "swamp",
                                                         "self_interested"))
ck("no spy-specific hunger angle to leak", "spy" not in S.HUNGER_ANGLE)
ck("every hunger state has a situation",
   set(S.HUNGER_SITUATION) == {"hungry", "weak", "starving", "oversatiated",
                               "superheated", "critical"})

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
