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

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
