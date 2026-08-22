"""The dummybot heckler: magazine parsing and the comprehension path."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
# Import the service from beside this file, not from a path baked in at authoring
# time -- these tests should run from a clone anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

# --- the 8B numbers and bullets lists no matter how firmly it is told not to -----------
raw = '''Here are 8 taunts:
1. Goblin scum, I await you!
2) You can't touch me!
- Come on then, greenskin!
* Your ugly face needs a punch!
"Bring it on, goblin!"
  Goblin trash, step forward and die!
GOBLIN SCUM, I AWAIT YOU!
x
Note: these are shouted.
'''
lines = S.parse_heckle_lines(raw, 12)
ck("strips numbering", "Goblin scum, I await you!" in lines, lines)
ck("strips both numbering styles", "You can't touch me!" in lines)
ck("strips bullets", "Come on then, greenskin!" in lines and "Your ugly face needs a punch!" in lines)
ck("strips quotes", "Bring it on, goblin!" in lines)
ck("drops the preamble", not any("Here are" in l for l in lines), lines)
ck("drops the trailing note", not any(l.startswith("Note") for l in lines), lines)
ck("drops the stub", "x" not in lines)
ck("dedupes case-insensitively", len([l for l in lines if l.lower().startswith("goblin scum")]) == 1, lines)
ck("count is right", len(lines) == 6, lines)

# --- a bubble the player can read at a glance -----------------------------------------
long = "You are the most enormous and profoundly disappointing creature that I have ever had the distinct misfortune of laying my magnificent eyes upon in all my many years"
out = S.parse_heckle_lines(long, 1)
ck("over-long lines are cut", len(out[0]) <= S.HECKLE_MAX_CHARS, len(out[0]))
ck("cut on a word boundary", not out[0].rstrip("!").endswith(" "), repr(out[0]))
ck("and still ends like a shout", out[0].endswith("!"), out[0])

# --- the count cap holds -------------------------------------------------------------
many = "\n".join("Line number %d here" % i for i in range(50))
ck("never returns more than asked", len(S.parse_heckle_lines(many, 8)) == 8)
ck("empty input is empty output, not a crash", S.parse_heckle_lines("", 8) == [])
ck("junk input is empty output", S.parse_heckle_lines("\n\n- \n2.\n", 8) == [])

# --- comprehension: a dummybot is a construct ----------------------------------------
ck("a rat cannot understand a dummybot", not S.can_understand("rat", "dummybot"))
ck("a gyrobot can", S.can_understand("gyrobot", "dummybot"))
ck("an unpolymorphed player always can", S.can_understand("", "dummybot"))
ck("there is a noise pool for it", S.noise_for("dummybot").strip() != "")
# One entry meant a whole magazine of identical chimes, which reads as a broken feature.
ck("and it has enough entries to not repeat",
   len({S.noise_for("dummybot") for _ in range(60)}) >= 4)

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
