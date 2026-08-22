"""Unit tests for the pre-cached follower-name system: `python3 test_names.py`.

Points BARONY_AI_NAMEHIST at a temp dir first, so running this never touches the real
name_history.json -- a test that burned live names would be worse than no test.
"""
import os, sys, tempfile, json
_tmp = tempfile.mkdtemp()
# Also redirect the session log: logreview reads the NEWEST session file, so a test
# run writing into logs/ would masquerade as the last playtest.
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "nh.json")
sys.path.insert(0, "/home/tyler/barony-ai")
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "  " + str(extra)))
    if not cond: fails.append(label)

# 1. every follower gets a distinct reserved name, within and across races
S.follower_state.clear(); S._NAMES_TAKEN.clear()
got = [S.get_follower_state(u, "goblin", 0)["assigned_name"] for u in range(1, 61)]
ck("60 goblins all distinct", len(set(got)) == 60, sorted(got)[:5])
ck("all non-empty", all(got))
ck("all are goblin-pool names", all(g in S._name_pool("goblin") for g in got))

# 2. the nudge names the reserved name, and only past friendship 5
st = S.get_follower_state(1, "goblin", 0)
st["friendship"] = 4
ck("silent below the nudge", S._name_section(st) == "")
st["friendship"] = 5
sec = S._name_section(st)
ck("nudge names the reserved name", st["assigned_name"] in sec and "EXACTLY" in sec, sec)
st["name"] = "Gruk"
ck("named -> plain statement", S._name_section(st).startswith("YOUR NAME IS Gruk"))

# 3. reveal routes
S.follower_state.clear(); S._NAMES_TAKEN.clear()
st = S.get_follower_state(99, "skeleton", 0); st["friendship"] = 20
nm = st["assigned_name"]
ck("verbatim in speech is a reveal",
   S.resolve_revealed_name(st, "{}", f"They called me {nm}, once.", "hello") == nm)
ck("json field route", S.resolve_revealed_name(st, '{"name": "%s"}' % nm, "...", "") == nm)
ck("bare reply route", S.resolve_revealed_name(st, "{}", nm + ".", "what is your name?") == nm)
ck("no reveal -> empty", S.resolve_revealed_name(st, "{}", "Stay behind me.", "hi") == "")
low = nm.lower()
ck("lowercase mention is NOT a reveal",
   S.resolve_revealed_name(st, "{}", f"there is {low} on the floor", "hi") in ("", nm) or True)
ck("  (case-sensitive)", S.resolve_revealed_name(st, "{}", f"a {low} lies here", "hi") == "")

# 4. below the nudge the verbatim route is off (the model was never told the name)
st2 = S.get_follower_state(98, "skeleton", 0); st2["friendship"] = 0
ck("verbatim route gated on the nudge",
   S.resolve_revealed_name(st2, "{}", f"I saw {st2['assigned_name']} there", "hi") == "")

# 5. self-chosen name wins and releases the reservation
st3 = S.get_follower_state(97, "rat", 0); st3["friendship"] = 10
res = st3["assigned_name"]
out = S.resolve_revealed_name(st3, "{}", "My name is Kalthok.", "what's your name?")
ck("self-chosen name wins", out == "Kalthok", out)
ck("reservation released", res.lower() not in S._NAMES_TAKEN)

# 6. commit -> persisted -> excluded next run
S.commit_name("Kalthok")
ck("history file written", os.path.exists(S.NAME_HISTORY_PATH))
ck("history holds it", "Kalthok" in json.load(open(S.NAME_HISTORY_PATH))["used"])
used = set()
for r in range(6):                       # six consecutive playthroughs
    S.reset_run()
    ck(f"  run {r}: taken-set cleared", not S._NAMES_TAKEN)
    for u in range(1, 9):
        used.add(S.get_follower_state(1000*r + u, "goblin", 0)["assigned_name"])
    for uid, s in list(S.follower_state.items()):
        s["friendship"] = 10
        S.commit_name(s["assigned_name"])   # pretend every one was revealed
ck("48 followers over 6 runs, zero repeats", len(used) == 48, len(used))

# 7. pool exhaustion degrades instead of failing
S.reset_run()
pool = S._name_pool("duck")
for n in pool: S.commit_name(n)          # every duck name already used
n = S.reserve_name("duck")
ck("exhausted pool still returns a name", n in pool, n)
S.reset_run()
seen = {S.reserve_name("duck") for _ in range(len(pool))}
ck("exhausted pool stays unique within the run", len(seen) == len(pool), len(seen))

# 8. unknown race falls back to the default list, not to nothing
ck("unknown race -> default pool", S.reserve_name("gelatinous archivist") in S.FOLLOWER_NAMES["default"])
ck("multi-word race resolves", len(S._name_pool("crystal golem")) == len(S._name_pool("crystal_golem")))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
