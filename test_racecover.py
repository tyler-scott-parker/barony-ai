"""Coverage tests for the per-race data files: `python3 test_racecover.py`.

⚠ WHY THIS EXISTS. A race the game can report but the data files do not know about fails
SILENTLY and plausibly: build_lore_context finds no entry and falls through to `default`,
_name_tiers returns only the generic pool, _group_of returns None -- which drops the creature
out of comprehension entirely and gives it `default_beast` noises. Nothing logs, and the
follower still talks, so the only symptom is that it has no character. That is exactly how
`fire sprite` went unnoticed: SPELL_FIRE_SPRITE calls forceFollower (magic/magic.cpp:3366),
so it is a real player follower, but getMonsterLocalizedName returns its special-NPC name
rather than "moth" (actmonster.cpp:230) and no file had that key.

The authority is Barony's own Monster enum, so this test reads it from a checkout when it can
find one (BARONY_SRC, or the usual sibling paths) and falls back to a frozen list otherwise --
a clone with no game source still gets the internal-consistency half.
"""
import os, sys, re, json, tempfile
_tmp = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "nh.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "  " + str(extra)))
    if not cond: fails.append(label)

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 1. every race name the ENGINE can hand us resolves in every file --------------------
# Frozen at Barony 5.0.2 + Deserters & Disciples / Instruments of Destruction Pt 1. These are
# getMonsterLocalizedName's outputs, not enum spellings: 'crystal golem' not CRYSTALGOLEM.
FROZEN = [
    "human", "rat", "goblin", "slime", "troll", "bat", "spider", "ghoul", "skeleton",
    "scorpion", "imp", "crab", "gnome", "demon", "succubus", "mimic", "lich", "minotaur",
    "devil", "shopkeeper", "kobold", "scarab", "crystal golem", "incubus", "vampire",
    "shadow", "cockatrice", "insectoid", "goatman", "automaton", "frosty lich", "fiery lich",
    "sentrybot", "spellbot", "gyrobot", "dummybot", "bugbear", "dryad", "myconid",
    "salamander", "gremlin", "revenant skull", "minimimic", "adorcised possession",
    "flame elemental", "hologram", "moth", "earth sprite", "duck",
    # Not an enum member: a MOTH_SMALL renamed by SPELL_FIRE_SPRITE, which getMonsterLocalizedName
    # reports by its special-NPC name. A player summon, so it is a follower like any other.
    "fire sprite",
]

SPECIAL_FROZEN = ["fire sprite"]

def _special_npc_names(root):
    """Names getMonsterLocalizedName can return that are NOT enum spellings.

    ⚠ This is the `fire sprite` class, and it used to be a hardcoded append here with a comment
    saying the next one would have to be hardcoded too. It does not: the function has exactly
    one shape for these -- a `nameMatchesSpecialNPCName(*optionalStats, "<key>")` branch that
    returns `optionalStats->name` -- so the keys are derivable from the function body itself.
    Only THIS function counts. `nameMatchesSpecialNPCName` is called all over the engine (for
    "skeleton knight" and "skeleton sentinel" among others), but those are SKELETONs and the
    name function reports them as "skeleton"; a key is only a race we must know about if the
    name function will hand it back to us.
    Returns None when the body cannot be found, which is a failure, not an empty result."""
    cpp = os.path.join(root, "src", "actmonster.cpp")
    if not os.path.isfile(cpp):
        return None
    src = open(cpp, encoding="utf-8", errors="ignore").read()
    i = src.find("std::string getMonsterLocalizedName(")
    if i < 0:
        return None
    j = src.find("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                break
    else:
        return None
    body = src[j:k + 1]
    return re.findall(r'nameMatchesSpecialNPCName\s*\([^,]+,\s*"([^"]+)"\s*\)', body)


def _engine_races():
    """Parse the live Monster enum if a Barony checkout is reachable; else use FROZEN."""
    roots = [os.environ.get("BARONY_SRC", "")]
    roots += [os.path.join(HERE, "..", "Barony"), os.path.expanduser("~/Barony"),
              os.path.expanduser("~/Projects/Barony")]
    for r in roots:
        if not r: continue
        hpp = os.path.join(r, "src", "monster.hpp")
        lang = os.path.join(r, "lang", "en.txt")
        if not (os.path.isfile(hpp) and os.path.isfile(lang)):
            continue
        src = open(hpp, encoding="utf-8", errors="ignore").read()
        m = re.search(r"enum Monster : int\s*\{(.*?)\}", src, re.S)
        if not m: continue
        names = [x.strip() for x in re.sub(r"//.*", "", m.group(1)).split(",")]
        names = [x for x in names if re.fullmatch(r"[A-Z_0-9]+", x or "")]
        L = {}
        for line in open(lang, encoding="utf-8", errors="ignore"):
            mm = re.match(r"^(\d+)\s+(.*)#\s*$", line.rstrip("\r\n"))
            if mm: L[int(mm.group(1))] = mm.group(2)
        i = {n: k for k, n in enumerate(names)}
        if "KOBOLD" not in i or "DRYAD" not in i: continue
        def nm(c):
            if names[c] == "BUGBEAR": return L.get(6256)
            if i["DRYAD"] <= c <= i["GREMLIN"]: return L.get(6358 + (c - i["DRYAD"]))
            if i["REVENANT_SKULL"] <= c <= i["MINIMIMIC"]: return L.get(6568 + (c - i["REVENANT_SKULL"]))
            if i["MONSTER_ADORCISED_WEAPON"] <= c <= i["MONSTER_UNUSED_8"]: return L.get(6580 + (c - i["MONSTER_ADORCISED_WEAPON"]))
            if c < i["KOBOLD"]: return L.get(90 + c)
            return L.get(2000 + c - i["KOBOLD"])
        out = []
        for c, n in enumerate(names):
            if n == "NOTHING" or n.startswith("MONSTER_UNUSED") or n == "MAX_MONSTER": continue
            v = nm(c)
            if v: out.append(v)
        # Names reachable only through a spell are not in the enum -- derived from the name
        # function itself rather than hardcoded, so a SECOND fire sprite is caught for free.
        special = _special_npc_names(r)
        SPECIAL_FOUND.append(special)
        for x in (special if special is not None else SPECIAL_FROZEN):
            if x not in out:
                out.append(x)
        if len(out) > 40:
            print("      (read %d races from the live enum at %s)" % (len(out), r))
            return out
    print("      (no Barony checkout found -- using the frozen 5.0.2 list)")
    return FROZEN

SPECIAL_FOUND = []
RACES = _engine_races()

for r in RACES:
    k = S._lore_key(r)
    ck("lore: %-22s" % r, k in S.RACE_LORE and S.RACE_LORE[k] is not S.RACE_LORE.get("default"))
    ck("group: %-21s" % r, S._group_of(r) is not None)
    ck("names: %-21s" % r, len(S._name_tiers(r)[0]) > 0 or len(S._name_tiers(r)[1]) > 0)

# ⚠ A race with no group is understood by NOBODY once the player is polymorphed, and gets
# default_beast noises. Assert the whole set, so a new race cannot quietly land outside.
ungrouped = [r for r in RACES if S._group_of(r) is None]
ck("every engine race has a comprehension group", not ungrouped, ungrouped)

# ---- 2. the data files agree with each other --------------------------------------------
lore_keys = {k for k in S.RACE_LORE if k not in ("_comment", "default")}
name_keys = set(S.FOLLOWER_NAMES.get("races", {}))
group_keys = {S._lore_key(x) for g in S.COMPREHENSION["groups"].values() for x in g}

ck("no lore entry without a comprehension group", not (lore_keys - group_keys), sorted(lore_keys - group_keys))
ck("no name pool without a lore entry", not (name_keys - lore_keys), sorted(name_keys - lore_keys))
# The reverse of the first: a group member nothing else knows about is a typo or a dead entry
# ('cave crawler' sat in `beast` for months and is not a monster in 5.0.2).
# ⚠ Every per-race file must be keyed the way _lore_key normalises, or the lookup misses in
# silence. race_books' four entries are single words, so its key shape was never exercised.
_bad_books = sorted(k for k in S.RACE_BOOKS if k != "_comment" and k != S._lore_key(k))
ck("race_books keys are all in _lore_key form", not _bad_books, _bad_books)
ck("no book entry for a race with no lore",
   not [k for k in S.RACE_BOOKS if k != "_comment" and S._lore_key(k) not in S.RACE_LORE])

ck("no comprehension member without a lore entry", not (group_keys - lore_keys), sorted(group_keys - lore_keys))

# ---- 3. a race is in exactly one group ---------------------------------------------------
seen, dupes = {}, []
for g, members in S.COMPREHENSION["groups"].items():
    for x in members:
        k = S._lore_key(x)
        if k in seen: dupes.append((x, seen[k], g))
        seen[k] = g
ck("no race in two comprehension groups", not dupes, dupes)

# ---- 4. the specific regression: fire sprite ----------------------------------------------
# ⚠ ANTI-VACUITY. If upstream renames the call or reshapes that branch, the parse silently
# returns nothing and this file quietly stops covering the whole spell-reachable class -- the
# same way it silently stopped covering the voice table. A parse that finds nothing is a
# FAILURE, not an empty result.
if SPECIAL_FOUND:
    _sp = SPECIAL_FOUND[0]
    ck("the special-NPC branch is still parseable from actmonster.cpp", _sp is not None)
    ck("and it still yields at least one name", bool(_sp), _sp)
    ck("fire sprite is DERIVED from the source, not hardcoded",
       bool(_sp) and "fire sprite" in _sp, _sp)

ck("fire sprite has its own lore", S._lore_key("fire sprite") in S.RACE_LORE)
ck("fire sprite has its own name pool", len(S._name_tiers("fire sprite")[0]) > 0)
ck("fire sprite is not given a beast growl", S.noise_for("fire sprite") in S.COMPREHENSION["noises"]["fire sprite"])
ck("a polymorphed salamander understands it", S.can_understand("salamander", "fire sprite"))

# ---- 5. the VOICE table is a fourth per-race file, and it had the same hole ---------------
# ⚠ `tts_bridge.py` was NOT covered by section 1, and that is how `fire sprite` survived there
# after being fixed in all three JSON files: a race with no cast falls through to the default
# voice silently, which is the identical failure mode this file exists to prevent, one layer
# further out. The table is parsed as TEXT on purpose -- importing the bridge re-execs it into
# .venv-tts, and a test must not depend on piper being installed.
_VP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_bridge.py")
if os.path.exists(_VP):
    with open(_VP, encoding="utf-8") as _fh:
        _src = _fh.read()
    _m = re.search(r"^VOICES\s*=\s*\{", _src, re.M)
    ck("tts_bridge.py still has a VOICES table to check", bool(_m))
    if _m:
        _cast = {S._lore_key(k) for k in
                 re.findall(r'^\s{4}"([^"]+)":\s*\{', _src[_m.start():], re.M)}
        _missing = sorted(r for r in RACES if S._lore_key(r) not in _cast)
        ck("every engine race has a voice cast", not _missing, _missing)
        ck("fire sprite has its own voice cast", S._lore_key("fire sprite") in _cast)
        # Known-inert casts are tolerated (they cost nothing but a dict entry); a race the game
        # CAN return with no cast is the one that is silent and wrong.

print()
print("FAILED: %d" % len(fails) if fails else "ALL PASS (%d assertions)" % (3 * len(RACES) + 8))
sys.exit(1 if fails else 0)
