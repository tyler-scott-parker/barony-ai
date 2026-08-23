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
for k in ("chest", "drunk", "mimic", "cleared", "swimming", "arrowtrap", "food", "kill",
          "death", "bossfight", "bossdown", "area", "minotaur", "minotimer", "minotaurdown",
          "equip"):
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
for k in ("chest", "drunk", "mimic", "cleared", "swimming", "arrowtrap", "food", "kill",
          "death", "bossfight", "bossdown", "area", "minotaur", "minotimer", "minotaurdown",
          "equip", "skills"):
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

# --- cleared / swimming / arrow trap / food -------------------------------------------------
# ⚠ Barony has NO concept of a cleared floor -- no flag, no message, no counter -- so the engine
# derives it from the player's own checkEnemy going quiet, which is what stops Hamlet's
# townsfolk and your own followers from counting as "hostile".
ck("the cleared remark says the floor is quiet", "quiet" in S.REMARK_SITUATION["cleared"])
# ⚠ The follower did not see the trap coming and must not claim it did.
ck("the arrow-trap remark forbids claiming a warning",
   "Do NOT claim you warned them" in S.REMARK_SITUATION["arrowtrap"])
ck("and says it is happening now", "RIGHT NOW" in S.REMARK_SITUATION["arrowtrap"])

# The food line carries the actual item, filled from the engine.
_f = S.build_remark_prompt("food", "rat", 7, "swamp", "loyal", {"look": "loaf of bread"})
ck("the food remark names what was picked up", "loaf of bread" in _f)
ck("the food line falls back cleanly with no context",
   "{look}" not in S.build_remark_prompt("food", "rat", 7, "swamp", "loyal"))
ck("it keeps the moment small", "only a scrap of food" in S.REMARK_ANGLE["food"][None])
ck("a construct does not eat", "do not eat" in S.REMARK_ANGLE["food"]["machine"])

# ⚠ SWIMMING made small races forget they were followers: a goblin answered "Go 'way, human
# scum!" and a rat "Get outta here, human! This's my swamp!". Naming the role back fixed the
# goblins. ⚠ The FIRST attempt said "not a stranger in your territory" and the word TERRITORY
# primed it -- same failure as the drunk line. Stated positively it holds for every race but
# rat, which still gets possessive about the water now and then.
ck("the swimming angle names the follower's role",
   "the one you follow and travel with" in S.REMARK_ANGLE["swimming"][None])
ck("and names no territory to grab",
   "territory" not in S.REMARK_ANGLE["swimming"][None])

# --- water, lava, and the two races water hurts ---------------------------------------------
# ⚠ isPlayerSwimming() is TRUE IN LAVA TOO (actplayer.cpp:3616 tests swimmingtiles ||
# lavatiles), so one "they are swimming" line had a follower admiring the player's stroke while
# they burned. The race-specific harm is the game's own (lang 3183 / 3702 / 3703 / 573).
ck("every medium/hazard combination has its own situation",
   set(S.SWIM_SITUATION) == {("water", ""), ("water", "vampire"), ("water", "automaton"),
                             ("lava", ""), ("lava", "vampire"), ("lava", "automaton")})
for key, sit in S.SWIM_SITUATION.items():
    if key == ("water", ""):
        continue
    # ⚠ A skeleton answered the automaton-water case with "Fire's dying, get ME out of the
    # water" -- writing ITSELF as the one drowning. 0/10 after saying whose body it is.
    ck("the %s situation says whose body it is" % (key,), "not you" in sit)
for key in (("lava", ""), ("lava", "vampire"), ("lava", "automaton")):
    ck("%s is urgent" % (key,), "RIGHT NOW" in S.SWIM_SITUATION[key])

# ⚠ The automaton cases are the SAME boiler the hunger remarks read: water is HUNGER -= 25
# (actplayer.cpp:9542), draining toward CRITICAL, and lava drives it the other way.
for key in (("water", "automaton"), ("lava", "automaton")):
    sit = S.SWIM_SITUATION[key]
    ck("%s forbids food language" % (key,), "Do NOT mention" in sit and "hunger" in sit)
    ck("%s names the boiler instead" % (key,), "boiler" in sit)
ck("water quenches the boiler", "flooding" in S.SWIM_SITUATION[("water", "automaton")])
ck("lava overheats it", "overheating" in S.SWIM_SITUATION[("lava", "automaton")])
ck("water burns a vampire", "BURNING" in S.SWIM_SITUATION[("water", "vampire")])

# A hazard is not scenery: the angle becomes "get them out".
_h = S.build_swim_prompt("goblin", 7, "hell", "loyal", "lava", "")
ck("a hazard tells them to get out", "Get them OUT" in _h)
ck("and names the follower's role", "the one you follow" in _h)
_plain = S.build_swim_prompt("goblin", 7, "swamp", "loyal", "water", "")
ck("plain water keeps the ordinary angle", "Get them OUT" not in _plain)
ck("an unknown medium falls back rather than breaking",
   S.build_swim_prompt("goblin", 7, "swamp", "loyal", "acid", "wizard"))
ck("a mercenary is still forbidden figures in water",
   "Naming any number is WRONG" in S.build_swim_prompt("gnome", 7, "swamp", "self_interested",
                                                        "water", "automaton"))

# --- the party killing something ------------------------------------------------------------
# ⚠ kills[] is the game's own per-run tally, credited to a player and cleared on a new game, so
# an edge on it means "your side just killed something" and the index says WHAT.
_k = S.build_remark_prompt("kill", "rat", 7, "swamp", "loyal", {"look": "a spider"})
ck("the kill remark names what died", "a spider" in _k)
ck("and falls back cleanly", "{look}" not in S.build_remark_prompt("kill", "rat", 7, "swamp",
                                                                   "loyal"))
ck("it keeps the moment short", "Keep it to a breath" in S.REMARK_ANGLE["kill"][None])
ck("a construct does not gloat", "not an achievement" in S.REMARK_ANGLE["kill"]["machine"])

# --- the adventurer dying -------------------------------------------------------------------
# ⚠ Death is not one situation. It matters whose adventurer it was AND whether they are coming
# back, and the game itself draws the second line: lang 578 "You will be revived if your party
# survives to the next level", lang 5264 "they retain their inventory when revived".
ck("all four death situations exist",
   set(S.DEATH_SITUATION) == {(True, True), (True, False), (False, True), (False, False)})
_own_ret = S.build_death_prompt("human", 7, "swamp", "loyal", own=True, can_return=True)
_own_end = S.build_death_prompt("human", 7, "swamp", "loyal", own=True, can_return=False)
_oth_ret = S.build_death_prompt("human", 7, "swamp", "loyal", own=False, can_return=True)
ck("a revivable death points at the next floor", "next floor" in _own_ret)
ck("a final one says there is nobody to carry them out", "nobody left" in _own_end)
ck("and does not promise a return", "come back" not in _own_end)
ck("someone else's death says yours is still standing", "yours is still standing" in _oth_ret)
ck("your own death says it was the one who took you on", "took you on" in _own_ret)

# ⚠ The follower must not invent a rescue the game does not offer. Reviving is the party
# reaching the next floor and nothing else. Measured 0/5 inventing one.
for _p in (_own_ret, _own_end, _oth_ret):
    ck("no invented resurrection is permitted", "Do NOT invent a way to bring them back" in _p)
ck("a construct does not grieve", "not grieve" in S.DEATH_ANGLE["machine"])
ck("a mercenary is still forbidden figures over a corpse",
   "Naming any number is WRONG" in S.build_death_prompt("gnome", 7, "swamp", "self_interested"))
ck("death is a short form", tok(_own_ret) < 500, tok(_own_ret))

# --- major bosses ---------------------------------------------------------------------------
# ⚠ The boss's REAL name is read from stats->name engine-side, never derived from the race:
# LICH_ICE/LICH_FIRE are Erudyce and Orpheus only when spawned as the named pair
# (stat_shared.cpp:918/946) and are ordinary elemental liches otherwise.
_bf = S.build_remark_prompt("bossfight", "human", 24, "Hell Boss", "loyal", {"look": "Baphomet"})
_bd = S.build_remark_prompt("bossdown", "human", 24, "Hell Boss", "loyal", {"look": "Baphomet"})
ck("the boss is named in the fight line", "Baphomet is HERE" in _bf)
ck("and in the victory line", "Baphomet is DEAD" in _bd)
ck("the fight line is urgent", "RIGHT NOW" in _bf)
ck("a construct does not fear a boss", "not fear it" in S.REMARK_ANGLE["bossfight"]["machine"])

# --- special and optional areas ---------------------------------------------------------------
# ⚠ FOUR attempts. Bare oaths that never said where; then "make clear WHERE you have arrived",
# which made followers INVENT names (1/6, 2 inventions); then the fix with those invented names
# quoted in the prompt as things to avoid -- which primed them straight back in (4/8, 2
# inventions), the spy crack's "the model copies the incriminating noun" all over again. With
# the name supplied and the examples moved to a comment: 6/8 named the real place, 0/8 invented.
_ar = S.build_remark_prompt("area", "gnome", 4, "The Gnomish Mines", "loyal",
                            {"look": "the Gnomish Mines"})
ck("the area line supplies the real name", "called the Gnomish Mines" in _ar)
ck("and forbids inventing another", "Do not invent a different name" in _ar)
ck("and forbids answering with only an oath", "only an oath" in _ar)
ck("no invented example is quoted back at the model",
   not any(w in S.REMARK_SITUATION["area"] for w in ("Uzgag", "Wailing", "Krog")))

# --- Hell: a standing dread, not a remark -----------------------------------------------------
# ⚠ Everything else here fires once on an event; being in Hell colours every line spoken down
# there, so it lives in build_prompt beside _fearful_section.
ck("hell floors carry the dread", bool(S._dread_section(22, "hell")))
ck("so does Baphomet's floor", bool(S._dread_section(24, "Hell Boss")))
ck("the swamp does not", not S._dread_section(7, "swamp"))
ck("and neither does the town", not S._dread_section(25, "Mages Guild"))
ck("the dread names Hell outright", "this is HELL" in S.HELL_DREAD)
# ⚠ Baphomet is named because a follower this deep knows whose ground it is -- but NOT described,
# because the hard limits still hold and an unseen boss must not be invented.
ck("Baphomet is named as the ruler here", "Baphomet rules here" in S.HELL_BAPHOMET)
ck("but the follower admits it has not seen him", "you have not seen him" in S.HELL_BAPHOMET)
ck("and is forbidden from inventing him", "Do not invent any of it" in S.HELL_BAPHOMET)
# Remarks made in Hell carry it too -- a cheerful word about a chest is wrong down there.
ck("remarks in Hell carry the dread",
   "this is HELL" in S.build_remark_prompt("chest", "human", 22, "hell", "loyal"))
ck("remarks elsewhere do not",
   "this is HELL" not in S.build_remark_prompt("chest", "human", 7, "swamp", "loyal"))

# --- the minotaur ---------------------------------------------------------------------------
# ⚠ NOT a "major boss": it hunts you across an ordinary floor rather than sitting at the end of
# one, and the fear is being CHASED. Same entity machinery, different lines.
ck("the minotaur situation is about being hunted",
   "follow you across this whole floor" in S.REMARK_SITUATION["minotaur"])
ck("and it is urgent", "RIGHT NOW" in S.REMARK_SITUATION["minotaur"])
ck("the countdown line says to move", "they need to move" in S.REMARK_SITUATION["minotimer"])
ck("killing one is relief, not triumph", "You can breathe" in S.REMARK_SITUATION["minotaurdown"])
# ⚠ Same oath-only failure the area line had: "By Herx's bones, what in the depths...?" over a
# corpse never says the thing is dead.
for k in ("minotaurdown", "bossdown"):
    ck("%s refuses a bare oath" % k, "own is not enough" in S.REMARK_SITUATION[k])

# --- what they are wearing --------------------------------------------------------------------
_eq = S.build_remark_prompt("equip", "rat", 7, "swamp", "loyal",
                            {"look": "leather boots", "slot": "boots"})
ck("the equip line names the item", "leather boots" in _eq)
ck("and the slot it went into", "their boots now" in _eq)
ck("it keeps the moment small", "not an event" in S.REMARK_ANGLE["equip"][None])
ck("the equip line falls back cleanly with no context",
   "{look}" not in S.build_remark_prompt("equip", "rat", 7, "swamp", "loyal")
   and "{slot}" not in S.build_remark_prompt("equip", "rat", 7, "swamp", "loyal"))

# --- best and worst skills --------------------------------------------------------------------
# ⚠ The engine sends the game's own skill NAME and TIER, never a raw number -- Barony's tiers are
# NOVICE 1 / BASIC 20 / SKILLED 40 / EXPERT 60 / MASTER 80 / LEGENDARY 100 (stat.hpp:195). A
# follower quoting "your axe is 43" would be reading a sheet it cannot see.
_sk = S.build_skills_prompt("human", 7, "swamp", "loyal",
                            "the axe", "an expert", "stealth", "hopeless")
ck("the skills line names the best", "an expert with the axe" in _sk)
ck("and the worst", "hopeless at stealth" in _sk)
# ⚠ Those two are the only skills it knows. Left open it invents a third, the same way an
# unanchored place name gets invented.
ck("and forbids inventing a third", "Do NOT bring up any other ability" in _sk)
ck("no skills prompt without both ends",
   S.build_skills_prompt("human", 7, "swamp", "loyal", "the axe", "an expert", "", "") == "")
ck("a mercenary is still forbidden figures there",
   "Naming any number is WRONG" in S.build_skills_prompt(
       "gnome", 7, "swamp", "self_interested", "trading", "a master", "the sword", "a novice"))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
