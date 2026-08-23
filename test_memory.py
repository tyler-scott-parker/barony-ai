"""Follower conversation memory: both halves of an exchange, so a follower stops
contradicting itself across turns."""
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

S.reset_run()
st = S.get_follower_state(1, "rat", 0)

# --- 1. both halves are stored ---------------------------------------------------------
S.record_follower_interaction(1, "Do you like cheese?", 2, 0, "Squeak! Cheese is good.")
e = st["events"][-1]
ck("the exchange is a record, not a string", isinstance(e, dict), type(e).__name__)
ck("the player's half is kept", e["said"] == "Do you like cheese?", e)
# Storing only this half was the bug: the follower had no record of its own answer.
ck("the follower's half is kept", e["replied"] == "Squeak! Cheese is good.", e)

# --- 2. it reaches the prompt, worded the way that measured working --------------------
block = S._follower_memory_block(st)
ck("the block renders", block != "")
ck("it carries what the player said", "Do you like cheese?" in block, block)
ck("it carries what the FOLLOWER said", "Cheese is good" in block, block)
ck("it forbids contradiction by name", "do not contradict" in block.lower(), block[:70])
ck("it reaches the assembled prompt",
   "WHAT YOU ALREADY SAID" in S._follower_sections(1, "rat", 2, "hello"))

# --- 3. an exchange with no reply yet still renders ------------------------------------
S.reset_run(); st = S.get_follower_state(2, "rat", 0)
S.record_follower_interaction(2, "Hello?", 1, 0, "")
ck("a reply-less exchange renders the player's half only",
   'they said: "Hello?"' in S._follower_memory_block(st)
   and "you replied" not in S._follower_memory_block(st), S._follower_memory_block(st))

# --- 4. bounded, so a long conversation cannot inflate the prompt ----------------------
S.reset_run(); st = S.get_follower_state(3, "human", 0)
for i in range(40):
    S.record_follower_interaction(3, "question %d" % i, 1, 0, "answer %d" % i)
ck("history is capped", len(st["events"]) == S.FOLLOWER_EXCHANGE_MEMORY, len(st["events"]))
ck("the newest exchange survives", st["events"][-1]["said"] == "question 39")
shown = S._follower_memory_block(st).count("they said:")
ck("only the most recent few reach the prompt", shown == S.FOLLOWER_EXCHANGES_SHOWN, shown)
S.record_follower_interaction(3, "q", 1, 0, "y" * 500)
ck("a rambling reply is trimmed",
   len(st["events"][-1]["replied"]) == S.FOLLOWER_REPLY_RECALL, len(st["events"][-1]["replied"]))

# --- 5. a run already in flight when the format changed must not crash -----------------
S.reset_run(); st = S.get_follower_state(4, "rat", 0)
st["events"] = ['they said to you: "old format"', {"said": "new", "replied": "both"}]
out = S._follower_memory_block(st)
ck("old string entries are skipped, not crashed on", "new" in out and "old format" not in out, out)

# --- 6. talk still meters friendship exactly as before ---------------------------------
S.reset_run(); st = S.get_follower_state(5, "rat", 0)
before = st["friendship"]
for i in range(3):
    S.record_follower_interaction(5, "chatting %d" % i, 1, 0, "mm")
ck("three exchanges still buy one friendship", st["friendship"] == before + 1, st["friendship"])
for i in range(30):
    S.record_follower_interaction(5, "chatting more %d" % i, 1, 0, "mm")
ck("and the per-floor cap still holds",
   st["friendship"] <= before + S.CHAT_CAP_PER_FLOOR, st["friendship"])

# ⚠ "Stay consistent" alone makes repeating the sentence the most obedient answer available.
# Observed live: a follower echoed its own introduction back word for word on the next exchange.
# Same fix and same wording as the merchant who quoted its own previous line -- facts held,
# phrasing varied.
_st = S.get_follower_state(99, "human", 0)
S.record_follower_interaction(99, "where are you from?", 3, 0, "Hamlet, born and raised.")
_blk = S._follower_memory_block(_st)
ck("the memory block still demands consistency", "do not contradict" in _blk)
ck("but says consistency is about FACTS, not sentences",
   "does NOT mean repeating those sentences" in _blk)
ck("and asks for a different wording next time", "say it a different way" in _blk)
ck("the NPC block carries the same fix",
   "does NOT mean repeating those sentences" in S._npc_memory_block(
       {"exchanges": [{"who": "Ada", "said": "hello", "replied": "well met"}]}))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
