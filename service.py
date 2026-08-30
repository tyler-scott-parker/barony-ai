"""Barony AI-NPC service: owns lore, prompts, and within-run social state.

The game (thin C++ hooks) POSTs here; we build a prompt, ask a local Ollama model,
and reply with {reply, action, name, secret, boon}. All state is per-playthrough
and lives in RAM -- `new_run` clears it.
"""
import io, itertools, sys, tempfile, traceback
import json, http.server, socketserver, urllib.request, os, random, re, threading, time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_json(name):
    with open(os.path.join(BASE_DIR, name)) as f:
        return json.load(f)

# Env overrides so a released build doesn't need this file edited.
OLLAMA_URL = os.environ.get("BARONY_AI_OLLAMA", "http://localhost:11434/api/generate")
MODEL      = os.environ.get("BARONY_AI_MODEL", "llama3.1:8b")
PORT       = int(os.environ.get("BARONY_AI_PORT", "5001"))
def _find_books_dir():
    """Barony's `books/` folder, wherever this machine keeps it.

    The old default was one hardcoded Linux Steam path -- the last portability blocker on this
    side. Books are enrichment, not a requirement: get_book_lore already survives a missing
    file, so a machine where none of these exist simply runs with thinner race lore rather
    than failing to start."""
    env = os.environ.get("BARONY_AI_BOOKS")
    if env:
        return env
    home = os.path.expanduser("~")
    candidates = [
        # Linux / Steam Deck
        os.path.join(home, ".local/share/Steam/steamapps/common/Barony/books"),
        os.path.join(home, ".steam/steam/steamapps/common/Barony/books"),
        os.path.join(home, ".var/app/com.valvesoftware.Steam/.local/share/Steam/"
                           "steamapps/common/Barony/books"),        # flatpak
        # Windows
        r"C:\Program Files (x86)\Steam\steamapps\common\Barony\books",
        r"C:\Program Files\Steam\steamapps\common\Barony\books",
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Steam", "steamapps",
                     "common", "Barony", "books"),
        # macOS
        os.path.join(home, "Library/Application Support/Steam/steamapps/common/Barony/books"),
        # GOG / DRM-free / running the service from inside the game folder
        os.path.join(BASE_DIR, "books"),
        "books",
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    return candidates[0]

BOOKS_DIR  = _find_books_dir()

LORE          = _load_json("barony_lore.json")        # world.setting only; see FULL for the rest
RACE_LORE     = _load_json("race_lore.json")
RACE_BOOKS    = _load_json("race_books.json")
COMPREHENSION = _load_json("comprehension.json")
FULL          = _load_json("barony_lore_full.json")   # researched lore (v19), 45 sections

WORLD = LORE["world"]["setting"]

# ---- Session logging ---------------------------------------------------------
# One append-only JSONL timeline per run, so a playthrough can be reviewed afterwards instead
# of relying on whatever scrolled past in the terminal. The C++ side pushes its own events into
# the SAME file (POST {"log": ...}), which is the point -- correlating "the speech bubble never
# appeared" with what the service actually returned needs a single ordered timeline.
# Summarise a session with:  python3 logreview.py [logs/session-*.jsonl]
LOG_DIR = os.environ.get("BARONY_AI_LOGDIR", os.path.join(BASE_DIR, "logs"))
SESSION_ID = time.strftime("%Y%m%d-%H%M%S")
LOG_PATH = os.path.join(LOG_DIR, "session-%s.jsonl" % SESSION_ID)
_LOG_LOCK = threading.Lock()
_LOG_T0 = time.time()

def logrec(kind, **fields):
    """Append one record. Logging must never break the game, so failures are swallowed."""
    rec = {"t": round(time.time() - _LOG_T0, 3), "kind": kind}
    rec.update({k: v for k, v in fields.items() if v not in (None, "")})
    try:
        with _LOG_LOCK:
            with io.open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    pass
# ---- Optional TTS spool (OFF by default) -------------------------------------
# Voice output is a separate, optional process -- exactly like voice_bridge.py.
# This service NEVER synthesises or plays audio. When BARONY_AI_TTS is on it drops
# one small JSON file per spoken line into a spool directory and forgets about it;
# `tts_bridge.py` is what loads a voice and makes noise. Consequences of that split:
#   * with the flag off, TTS costs one boolean test per reply and nothing else
#   * with the flag on but no bridge running, lines spool and get pruned -- the
#     game is never blocked or slowed by audio, and never waits on it
#   * the spool is written HOST-side, so in co-op only the machine running the
#     bridge hears anything; a client bridge would need a relay first
TTS_ENABLED = os.environ.get("BARONY_AI_TTS", "0").strip().lower() in ("1", "true", "on", "yes")
TTS_SPOOL   = os.environ.get("BARONY_AI_TTSDIR",
                            os.path.join(tempfile.gettempdir(), "mymod_tts"))
# If the bridge is dead or has fallen behind, drop the OLDEST pending lines. A line
# arriving forty seconds after the conversation moved on is worse than silence.
TTS_MAX_PENDING = int(os.environ.get("BARONY_AI_TTSQUEUE", "6"))

_TTS_SEQ = itertools.count(1)

def tts_emit(text, race="", uid=0, kind="reply", name="", player=0):
    """Queue one line for the optional voice bridge. Never raises, never blocks."""
    if not TTS_ENABLED or not text or not text.strip():
        return
    try:
        pending = sorted(f for f in os.listdir(TTS_SPOOL) if f.endswith(".json"))
        for stale in pending[:max(0, len(pending) - (TTS_MAX_PENDING - 1))]:
            try:
                os.remove(os.path.join(TTS_SPOOL, stale))
            except OSError:
                pass
        rec = {"text": text, "race": race or "", "uid": int(uid or 0), "kind": kind,
               "name": name or "", "player": int(player or 0), "ts": time.time()}
        base = os.path.join(TTS_SPOOL, "%012d" % next(_TTS_SEQ))
        # write-then-rename: the bridge polls this directory and must never read a
        # half-written file.
        with io.open(base + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(base + ".tmp", base + ".json")
    except Exception:
        pass   # audio must never break the game

if TTS_ENABLED:
    # Clear anything left over from a previous session -- the sequence counter restarts
    # at 1, so stale files would sort ahead of live ones and play out of order.
    try:
        os.makedirs(TTS_SPOOL, exist_ok=True)
        for _f in os.listdir(TTS_SPOOL):
            if _f.endswith((".json", ".tmp")):
                os.remove(os.path.join(TTS_SPOOL, _f))
    except Exception:
        pass

VALID_ACTIONS = ("FOLLOW", "DEFEND", "WAIT", "ATTACK", "NONE")

# ---- Cross-race comprehension: you only understand your own kind's tongue ----

def _group_of(race):
    r = race.lower()
    for gname, members in COMPREHENSION["groups"].items():
        if r in members:
            return gname
    return None

def can_understand(player_race, speaker_race):
    if not player_race:
        return True  # unknown player form -> default to understanding (e.g. /aicommand)
    pr, sr = player_race.lower(), speaker_race.lower()
    if pr == sr:
        return True
    pg, sg = _group_of(pr), _group_of(sr)
    return pg is not None and pg == sg

def noise_for(speaker_race):
    """Race pool -> that race's GROUP default -> default_beast. The group step matters:
    without it a lich and a duck both fell back to '*an animal snarl*'."""
    noises = COMPREHENSION.get("noises", {})
    r = speaker_race.lower()
    grp = _group_of(r)
    pool = (noises.get(r)
            or (noises.get("default_" + grp) if grp else None)
            or noises.get("default_beast", ["*unintelligible noises*"]))
    return random.choice(pool)

_book_cache = {}
def get_book_lore(race):
    """Canonical in-game book text for a race, concatenated. Cached per file."""
    out = []
    for fn in RACE_BOOKS.get(race.lower(), []):
        if fn not in _book_cache:
            try:
                with open(os.path.join(BOOKS_DIR, fn), encoding="utf-8", errors="ignore") as bf:
                    _book_cache[fn] = bf.read().strip()
            except Exception as e:
                print(f"[SERVICE] couldn't read book {fn}: {e}")
                continue
        out.append(_book_cache[fn])
    return "\n\n".join(out)

# ---- Prompt section builders -------------------------------------------------
#
# THE MOST IMPORTANT PROMPT FINDING, encoded below: at 8B, forbidding a
# CONCLUSION without also forbidding the EVASION PATTERN gets routed around
# ("whispers say silver..."). Naming the rhetorical route took refusals from
# 0/5 to 5/5. Do not soften this wording; do not let it get deleted -- it is a
# single constant precisely so an edit can't silently drop it from one caller.

LIMITS_HEADER = (
    'HARD LIMITS ON WHAT YOU KNOW. You genuinely do not know these things:\n'
    'If asked about any of them, say plainly and in character that you do not know, and STOP.\n'
    'Do NOT guess, speculate, theorize, or pass on rumors about them. Hedged answers are FORBIDDEN:\n'
    '"some say...", "whispers speak of...", "perhaps it is...", "I have heard..." followed by an answer\n'
    'counts as claiming and is wrong. An honest "I do not know" is always the correct reply.\n'
    'This applies hardest to things you can easily PICTURE. If one of these is a kind of place, '
    'creature or scheme you can imagine from general experience, that makes it MORE important to '
    'say you do not know, not less. Do NOT describe what such a place is "probably" or "usually" '
    'like, do NOT describe its traps, corridors, chambers, contents or inhabitants, and do NOT '
    'reach for what is typical of its kind. You are not being asked what temples are like in '
    'general. You are being asked about THIS one, and you have never seen inside it.\n'
)

def _bullets(items):
    return "\n".join("- " + x for x in items)

def _grounding_block(facts):
    return ("CANONICAL GROUNDING:\n" + _bullets(facts) + "\n") if facts else ""

def _limits_block(constraints):
    return (LIMITS_HEADER + _bullets(constraints) + "\n") if constraints else ""

def _book_block(race_l):
    lore = get_book_lore(race_l)
    return ("RELEVANT LORE (what your kind knows):\n" + lore + "\n") if lore else ""

def _persona(race, grounded, floor, map_name="", npc_name="", books=True, budget=16):
    """The opening every prompt shares: setting, character guidance and canonical grounding.

    Returns (head, body, limits). The HARD LIMITS come back SEPARATELY so callers can place
    them LAST in the prompt, after the disclosure block. They have to be last: disclosure
    says "do NOT claim you don't know, visibly decline instead" while the limits say "say you
    do not know and STOP", and whichever lands later wins. With limits early and disclosure
    after them, anti-fabrication fell from 9-10/10 to ~4/10 -- the model split the difference
    and produced exactly the hedged answer the limits block was written to kill."""
    race_l = race.lower()
    # Same silent-miss trap _lore_key() was written for: the game hands back display names
    # with spaces ("crystal golem"), so a raw .get() would never match an underscored key.
    # Try both so either convention works and no race quietly falls to the default.
    slice_ = (RACE_LORE.get(_lore_key(race_l)) or RACE_LORE.get(race_l)
              or RACE_LORE.get("default", "A creature of the dungeon."))
    head = f"SETTING: {WORLD}\n"
    tail = f"CHARACTER GUIDANCE: {slice_}\n"
    limits = ""
    if grounded:
        facts, constraints = build_lore_context(race_l, floor, budget=budget,
                                                map_name=map_name, npc_name=npc_name)
        tail += _grounding_block(facts)
        limits = _limits_block(constraints)
    # ⚠ `books` is what the prompt-size swing was made of. Only FOUR races have book text at
    # all, and for a human it is ~1680 of the ~1875 tokens of an ambient prompt -- paid in full
    # to produce four words of muttering. The swing was never a grounding gap (race_lore and
    # the denizen profiles cover all 49); it was an uneven cost. Conversation keeps the books,
    # which is where a follower actually draws on them; the short forms do not.
    return head, tail + (_book_block(race_l) if books else ""), limits

def build_taunt_prompt(race, floor, map_name=""):
    # Grounded, but on a short budget and without books: this produces a handful of shouted
    # words, and a 2400-token prompt for "Die, worm!" is pure GPU contention with the
    # conversation the player is actually having.
    head, body, limits = _persona(race, True, floor, map_name, books=False, budget=6)
    where = place_name(floor, map_name)
    return (
        head
        + (f"YOU ARE: a {race} in {where}, on dungeon floor {floor}, locked in COMBAT right now.\n"
           if where else
           f"YOU ARE: a {race} on dungeon floor {floor}, locked in COMBAT right now.\n")
        + body
        + "You are fighting an enemy this very moment. Shout ONE short, aggressive taunt or battle-cry, in character.\n"
        + "Respond with ONLY the spoken line (a few words). No narration, no quotes, no JSON."
    )

def build_ambient_prompt(race, floor, relation="hostile", map_name=""):
    # ⚠ Babble used to be told only "dungeon floor 25" -- so a shopkeeper standing in the town
    # of Hamlet muttered about the dungeon. Exactly the bug place_name() was written to fix for
    # conversation, still live here because the ambient payload never carried the map name.
    #
    # Still ungrounded (no canon facts, no hard limits): a four-word mutter has no room to make
    # a claim worth constraining, and the measured lines never fabricated. What it lacked was
    # somewhere to BE.
    head, body, limits = _persona(race, False, floor, map_name, books=False)
    where = place_name(floor, map_name)
    if relation == "follower":
        situation = ("You are the adventurer's companion, wandering the dungeon together. "
                     "Mutter a short idle remark to yourself or your companion.")
    else:
        situation = ("You do NOT see the adventurer. You are alone or with your own kind in the dungeon. "
                     "Mutter a short idle line to yourself, unaware you are overheard.")
    return (
        head
        + (f"YOU ARE: a {race} in {where}, on dungeon floor {floor}.\n" if where else
           f"YOU ARE: a {race} on dungeon floor {floor}.\n")
        + body
        + situation + "\n"
        + "Respond with ONLY one short spoken line (a few words), in character. No narration, no quotes, no JSON. Just the line."
    )

# ---- The dummybot heckler ------------------------------------------------------------
# A dummybot is a sprung training dummy a tinkerer bolted together and threw into a dungeon to
# be shot at -- its combat value IS being noticed (monsters see one from 96 units away,
# actmonster.cpp:6115). So it heckles, constantly, at everything, and understands none of this.
#
# ⚠ Generated in MAGAZINES, not one line per shout. Rapid-fire is the whole joke and a
# generation is 1-4s, so a per-line request would either stutter or monopolise the GPU that
# real dialogue needs. One call returns a batch, the mod fires them locally, and a refill is
# requested only when the magazine runs low.

HECKLE_MAX_LINES = 12
HECKLE_MAX_CHARS = 90    # a bubble the player can read at a glance, not a speech

# ---- "That looks worth something" ---------------------------------------------------------
# A follower reacting to what you just picked up. Short, one line, the taunt/ambient shape.
#
# ⚠ THEY NEVER NAME IT. Most followers cannot appraise a legendary item at all (see the
# competence ceiling), so naming one here would hand out for free exactly what the appraisal
# tiers gate -- and would contradict the same follower saying it is beyond them a moment later.
# They react to the LOOK of the thing, which is all a bystander has.
VALUABLE_BANDS = [
    (3000, "worth more than most people down here will see in a lifetime"),
    (1500, "worth a great deal — a serious piece of money"),
    (0,    "worth real money, more than the usual scrapings"),
]

def valuable_band(value):
    for floor_v, text in VALUABLE_BANDS:
        if int(value or 0) >= floor_v:
            return text
    return VALUABLE_BANDS[-1][1]

# ⚠ A SPY USES THE LOYAL REACTION. Same rule as the introduction: the disguise is the whole
# point, and a spy who eyes your treasure differently from an honest companion is a tell the
# player gets for free.
VALUABLE_ANGLE = {
    "self_interested": ("You are a hired professional and you price things by habit. React to "
                        "it as money — what it would fetch, who would want it, what it says "
                        "about their luck. You may note that a thing like that wants a proper "
                        "appraisal, but do NOT offer to do it yourself here."),
    "fearful":         ("Carrying something like that makes you nervous. React to it as a "
                        "thing that draws attention — from people, or from worse."),
    "bound":           ("You have no use for wealth and no memory of wanting any. Note it "
                        "flatly, as an observation about them rather than about the object."),
    "machine":         ("You have no use for wealth. Note it flatly, as an observation."),
}
VALUABLE_DEFAULT = ("React to it plainly and warmly — you are pleased for them, or impressed, "
                    "or wary of what carrying it might mean.")

# ---- The other three things worth remarking on ---------------------------------------------
# Same short-form shape as the valuable pickup, and the same rules: react to what is in front of
# you, briefly, in character. Each kind supplies the SITUATION; the allegiance supplies the
# ANGLE. Both are server-side, which is the division of labour every block here that measures
# well already uses.
REMARK_SITUATION = {
    "chest": "They have just got a treasure chest open in front of you, and are going through it.",
    # ⚠ Barony has no concept of a cleared floor -- no flag, no message, no counter -- so the
    # engine derives it from the player's own checkEnemy going quiet. See mymod_remarkTick.
    "cleared": ("Nothing hostile is left alive on this floor. It is quiet — properly quiet — "
                "for the first time since you came down here, and you can both feel it."),
    # Replaced by SWIM_SITUATION below -- kept as the fallback for a payload with no medium.
    "swimming": ("They have waded in and are swimming. You are watching this adventurer paddle "
                 "about in dungeon water with all their gear on."),
    "kill": ("Between you, you have just put down {look}. It is dead on the floor in front of "
             "you and the fight is off you for the moment."),
    # Routed to build_death_prompt, which needs the own/can_return pair; this entry exists so
    # the handler recognises the kind.
    "death": "An adventurer has been killed.",
    # ⚠ {look} is the boss's REAL name, read from stats->name engine-side -- "Baphomet",
    # "Baron Herx", "Erudyce", "Orpheus" -- never guessed from the race, because LICH_ICE and
    # LICH_FIRE are ordinary elemental liches unless spawned as the named pair.
    # ⚠ The minotaur is NOT a "major boss": it hunts you across an ordinary floor rather than
    # sitting at the end of one, and the fear is being CHASED. Different lines, same machinery.
    "minotaur": ("A MINOTAUR is here — you can see it, and it has seen you. It does not stop, it "
                 "does not lose interest, and it will follow you across this whole floor. "
                 "This is happening RIGHT NOW."),
    "minotimer": ("Something has just changed. There is a minotaur loose on this floor now, "
                  "hunting, and it knows where you are. It is only a matter of time before it "
                  "finds you. Say it plainly — they need to move."),
    "minotaurdown": ("The minotaur is DEAD. The thing that was hunting you across this floor is "
                     "down and it is not getting up. You can breathe.\n"
                     "Your line must make clear that it is DEAD. An oath or an exclamation on "
                     "its own is not enough."),
    # ⚠ {look} and {slot} come from the engine: the real item and the real slot it went into.
    "equip": ("They have just put on {look} — it is their {slot} now. You watched them do it."),
    # ⚠ An artifact is not just another helm. Anything over the appraisal ceiling (1500 gold, the
    # tier the game reserves for a near-master appraiser) is legendary, so this covers the ten
    # artifacts without a list to maintain.
    "equiplegend": ("They have just put on {look} — it is their {slot} now, and it is no ordinary "
                    "piece. A thing like that is spoken about; most people who go down here never "
                    "see one, let alone wear one. You are standing next to someone carrying it."),
    # ⚠ {look} went to YOU, not to them.
    "gift": ("They have just handed YOU {look} — your {slot} now. This adventurer has armed you "
             "out of their own kit, which is not a thing most of them do."),
    "wealth": "WEALTH",   # replaced per band below; see WEALTH_SITUATION
    # ⚠ "Clothes" is the ARMOUR slots only -- a weapon, a shield and a fistful of rings are not
    # an outfit, and someone in nothing but a ring and a sword is exactly the case worth a line.
    "naked": ("They are not wearing anything. No helm, no armour, no boots, no gloves, no cloak "
              "— they are walking through a dungeon in their skin, and you are looking right at "
              "them."),
    "levitate": ("They have left the ground. This adventurer is floating — feet clear of the "
                 "floor, drifting along above it — and you are not."),
    "boulderhit": ("A boulder has just rolled straight over them. They are hurt and they are "
                   "getting up out of the dirt. This is happening RIGHT NOW."),
    "boulderpush": ("They have just set a boulder rolling with their own hands. It is moving now, "
                    "and it will go where it goes."),
    # ⚠ Any route counts: a pickaxe, a bomb, a spell. The engine counts obstacle tiles, so it
    # cannot say HOW -- and does not need to. What matters is that the dungeon's shape just
    # changed because somebody decided it should.
    "dig": ("A wall has just come down. Somebody has decided the way through is the way THROUGH, "
            "and there is now a hole in this dungeon that was not there a moment ago."),
    "fountain": "FOUNTAIN",   # replaced per effect below; see FOUNTAIN_SITUATION
    # ⚠ Only ever sent for an IDENTIFIED item. An unidentified curse is something the game
    # deliberately hides until it bites, and a follower announcing it would be free
    # identification -- the same leak the appraisal ceiling exists to close.
    "blessed": ("They have just put on {look} — their {slot} now — and it is {blessing}. That is "
                "plain to see on it, and you can see it."),
    "strangle": "STRANGLE",   # replaced per victim below; see STRANGLE_SITUATION
    "biome": "BIOME",         # replaced per region below; see BIOME_SITUATION
    "bridge": "BRIDGE",       # replaced below; the hunger line depends on the map flag
    # Routed to build_skill_prompt, which needs the skill/tier pair; these exist so the handler
    # recognises the kinds.
    "skillbest": "You have seen what this adventurer is good at.",
    "skillworst": "You have seen where this adventurer falls short.",
    "bossfight": ("{look} is HERE, in front of you, alive and enormous, and the fight is "
                  "starting. This is not another dungeon creature — this is the thing this "
                  "whole floor was built around. It is happening RIGHT NOW."),
    "bossdown": ("{look} is DEAD. Between you, you have brought down the thing this whole "
                 "stretch of the dungeon belonged to. It is over, and the quiet after it is "
                 "enormous.\n"
                 "Your line must make clear that it is over. An oath or an exclamation on its "
                 "own is not enough."),
    # ⚠ {look} is the area, keyed engine-side on the map's INTERNAL name -- several of which do
    # not match their filename (hamlet.lmp is "Mages Guild").
    # ⚠ THREE attempts, and the third failure was self-inflicted:
    #   1. no requirement          -> bare oaths that never said where ("By the bones of Herx's
    #                                 victims...") -- 2 of 8 misses
    #   2. "make clear WHERE you have arrived" -> 1/6, and now INVENTING names: Uzgag's tunnels,
    #                                 the Chamber of Wailing Stones, Krog's Grotto. Asking for a
    #                                 name without anchoring one produces confabulation.
    #   3. the fix, but with those invented names quoted in the prompt as things not to do ->
    #      the goblin promptly said "Uzgag's Maze". ⚠ THE EXAMPLES PRIMED IT. Exactly the spy
    #      crack's lesson ("the model copies the incriminating noun"), and the examples now live
    #      here in the comment where they belong rather than in the prompt.
    "area": ("You have both just walked into {look}. This is not another stretch of corridor; "
             "it is somewhere, and you know it the moment you are through the door.\n"
             "This place is called {look}. Use that name, in your own phrasing. Do not invent "
             "a different name for it, and do not answer with only an oath."),
    "arrowtrap": ("An arrow trap has just gone off and put a shaft in them. They are hurt and "
                  "there may be more of it. This is happening RIGHT NOW.\n"
                  "Do NOT claim you warned them and do NOT say you saw it coming — you did not."),
    # {look} is filled from the engine: the actual food they picked up.
    "food": ("They have just picked up {look} and stowed it. A small thing, and you noticed."),
    # ⚠ It is the ADVENTURER who is drunk, not the follower -- worth being explicit, because the
    # model will otherwise happily write itself as the drunk one.
    # ⚠ DO NOT add "do not change the subject to the floor or what lives here" to this. It was
    # tried: naming those routes PRIMED them, and the measured rate fell from ~5-7/8 to 2/8 --
    # every miss then wrote about gators, reeds and cypress knees. Same failure the project
    # already recorded for spec 26 (information provenance), where a carve-out phrase primed
    # the very hedge it forbade. Naming a forbidden route works when the route is a SPEECH ACT
    # ("saying you do not know", "opening with Alright"); it backfires when the route is a
    # TOPIC, because naming a topic introduces it.
    "drunk": ("THEY are drunk — this adventurer, not you. They have been at the booze and it "
              "shows: the walk, the talk, the timing. You are sober and you are watching it."),
    "mimic": ("A chest you both walked past has just come alive — legs, teeth, the lot — and it "
              "is attacking. It was never a chest. This is happening RIGHT NOW."),
}

# ⚠ A SPY USES THE LOYAL ANGLE for every one of these, as with the introduction and the valuable
# pickup. The disguise is the point, and a spy who reacts differently to your misfortune is a
# tell handed over for nothing.
REMARK_ANGLE = {
    "cleared": {
        "self_interested": "A finished job is a job you can be paid for. Say what that is worth, or what comes next.",
        "fearful": "You can breathe again. Say so — and say you would rather not be here when something else wanders in.",
        "bound": "Report the state of the floor. Nothing more.",
        "machine": "Report the state of the floor. Nothing more.",
        None: "React plainly to the quiet — satisfaction, relief, or a dry word about the work.",
    },
    "swimming": {
        "self_interested": "Wet gear rusts and wet coin is still coin, but you are not paid to swim. Say something dry about it.",
        "fearful": "You do not like this. There is no telling what is under dark water.",
        "bound": "Note that they are in the water. It is a fact, not a concern.",
        "machine": "Note that they are in the water. It is a fact, not a concern.",
        # ⚠ "React as your kind would to water" sent small territorial races off the rails: a
        # goblin answered "Go 'way, human scum!" and a rat "Get outta here, human! This's my
        # swamp!" -- both had forgotten they were following this person. Naming the role back
        # is a subject clarification, the fix that worked for "this adventurer, not you".
        # ⚠ The first fix said "not a stranger in your territory" and the word TERRITORY primed
        # it -- a rat still answered "Get outta me water!" twice. Same failure as the drunk
        # line. Stated positively, with no noun to grab, it holds: 0/6.
        None: "React as YOUR kind would to water — some creatures love it, some will not go near it. This is YOUR adventurer in there, the one you follow and travel with, and you are speaking to them as their companion.",
    },
    "arrowtrap": {
        "self_interested": "Carelessness costs, and you are the one who has to keep them upright. Say so, shortly.",
        "fearful": "That could have been you. Say so, and say you want to be away from here.",
        "bound": "Register the injury and the danger. State it.",
        "machine": "Register the injury and the danger. State it.",
        None: "Warn them — there may be more, and they are bleeding. You are already looking for the next one.",
    },
    "food": {
        "self_interested": "It is barely worth the carrying, and you would not have bothered. Say so, mildly.",
        "fearful": "Food is one less thing to worry about down here.",
        "bound": "You do not eat. Note it without interest.",
        "machine": "You do not eat. Note it without interest.",
        None: "React as YOUR kind would to that particular food — appetite, disgust, or a passing opinion. Keep it small; it is only a scrap of food.",
    },
    "minotaur": {
        "self_interested": "You are not paid to be hunted. Say so, and say what you are doing about it.",
        "fearful": "You are terrified. Shout it.",
        "bound": "Identify the threat and state it. You do not run.",
        "machine": "Identify the threat and state it. You do not run.",
        None: "React as YOUR kind would to being hunted by something that does not stop. Keep it to one breath.",
    },
    "minotimer": {
        "self_interested": "This was not in the arrangement. Say so, and say to move.",
        "fearful": "You want to be anywhere else. Say so, and say to move.",
        "bound": "State the situation. Advise moving.",
        "machine": "State the situation. Advise moving.",
        None: "Warn them, shortly. There is no fighting this thing on its terms.",
    },
    "minotaurdown": {
        "self_interested": "You survived something that does not usually let people survive it. Say what that is worth.",
        "fearful": "You did not think you would live through that. Say so.",
        "bound": "State the outcome.",
        "machine": "State the outcome.",
        None: "React as YOUR kind would to the thing that was hunting you lying dead.",
    },
    "blessed": {
        "self_interested": "Blessed gear is worth more and cursed gear is a client about to become a problem. Say which you are looking at.",
        "fearful": "Good luck or bad luck, and you have opinions about both. Say which this is.",
        "bound": "Note the state of the item. Fortune is not a thing you believe in.",
        "machine": "Note the state of the item. Fortune is not a thing you believe in.",
        None: "React as YOUR kind would to a thing that has been blessed or cursed — reverence, superstition, dark amusement, or a practical word about what it will mean.",
    },
    "strangle": {
        "self_interested": "This is your client throttling themselves with jewellery, and you are the one who has to keep them alive. Say so.",
        "fearful": "You are watching something strangle them and you cannot stop it. Say so.",
        "bound": "State what the amulet is doing to them.",
        "machine": "State what the amulet is doing to them.",
        None: "React to what you are watching happen at their throat, right now.",
    },
    "dig": {
        "self_interested": "Doors are free and walls are work. Say what you make of the effort.",
        "fearful": "A hole works both ways, and things come through holes. Say so.",
        "bound": "Note the opening. It is a change in the ground, nothing more.",
        "machine": "Note the opening. It is a change in the ground, nothing more.",
        None: "React as YOUR kind would to someone digging their own way through solid rock — approval, alarm at the noise, or an old opinion about what lives behind walls.",
    },
    "fountain": {
        "self_interested": "You have seen people do worse things for less. Say what you make of the gamble.",
        "fearful": "Drinking from a hole in the ground in a place like this frightens you. Say so.",
        "bound": "Note what the water did. You are not curious about it.",
        "machine": "Note what the water did. You are not curious about it.",
        None: "React as YOUR kind would to what the water just did to them.",
    },
    "biome": {
        "self_interested": "You have worked places like this before. Say what it means for the job.",
        "fearful": "You do not like it here. Say what specifically bothers you about this place.",
        "bound": "Note where you are. Nothing more.",
        "machine": "Note where you are. Nothing more.",
        None: "React as YOUR kind would to being HERE specifically — what your sort knows about this kind of place, what it smells of, what usually happens in it.",
    },
    "bridge": {
        "self_interested": "A stretch with nothing in it is a stretch nobody pays you for. Say something dry about the quiet.",
        "fearful": "You are safe for the moment and you know it will not last. Say so.",
        "bound": "Note the crossing. Nothing more.",
        "machine": "Note the crossing. Nothing more.",
        None: "React as YOUR kind would to a lull between two bad places — relief, wariness, or a practical word about using it while it lasts.",
    },
    "naked": {
        "self_interested": "A client with no armour is a client about to become a corpse, and a corpse does not settle up. Say so, dryly.",
        "fearful": "This frightens you. They are what stands between you and everything down here, and they have nothing on.",
        "bound": "Note the absence of equipment. Modesty is not a thing you have.",
        "machine": "Note the absence of equipment. Modesty is not a thing you have.",
        None: "React as YOUR kind would — most creatures down here wear nothing either, so the joke may well be on the humans. Amusement, bafflement, or a practical word about what armour is for.",
    },
    "levitate": {
        "self_interested": "You cannot follow them up there, and you are not paid to watch. Say something dry about it.",
        "fearful": "They are up there and you are down here with whatever comes along. Say so.",
        "bound": "Note that they are airborne. It is a fact.",
        "machine": "Note that they are airborne. It is a fact.",
        None: "React as YOUR kind would to someone leaving the ground — envy, unease, wonder, or a flat remark about the trick of it.",
    },
    "boulderhit": {
        "self_interested": "That is damage you will have to keep them alive through. Say so, shortly.",
        "fearful": "That could have been you, and it nearly was. Say so.",
        "bound": "Note the impact and the injury.",
        "machine": "Note the impact and the injury.",
        None: "React as YOUR kind would to watching someone get run down by a rock — alarm, sympathy, or very dark amusement. Keep it to one breath.",
    },
    "boulderpush": {
        "self_interested": "Moving rocks is not what you were hired for, and you are watching them do it. Say something dry.",
        "fearful": "Loud things attract attention, and that was loud. Say so.",
        "bound": "Note what they have done. Nothing more.",
        "machine": "Note what they have done. Nothing more.",
        None: "React as YOUR kind would to them shoving a great rock about — approval, doubt about where it is going, or a word about what usually happens next.",
    },
    "equiplegend": {
        "self_interested": "You have never had a client carrying one of those. Say what it is worth — and what it makes them, walking around with it.",
        "fearful": "Anything that grand attracts attention, and attention is what kills people. Say so.",
        "bound": "Note the item. Its reputation is nothing to you.",
        "machine": "Note the item. Its reputation is nothing to you.",
        None: "React as YOUR kind would to seeing a thing out of stories worn in front of you — awe, envy, unease, or an old tale about one like it.",
    },
    "gift": {
        "self_interested": "Payment in kind is still payment, and this is better than most. Say so, without gushing.",
        "fearful": "Being armed makes you feel a little less likely to die down here. Say so.",
        "bound": "Acknowledge the equipment. You did not expect it and you do not need it.",
        "machine": "Acknowledge the equipment. You do not need it.",
        None: "React as YOUR kind would to being armed by the person you follow — gratitude, surprise, suspicion, or a promise about what you will do with it.",
    },
    "wealth": {
        "self_interested": "Their purse is your business more than anyone's. Say what you make of it.",
        "fearful": "Money is what keeps you both fed and alive down here. Say what this means for that.",
        "bound": "Note the state of their purse. It means nothing to you.",
        "machine": "Note the state of their purse. It means nothing to you.",
        None: "React as YOUR kind would to what they are carrying — sympathy, envy, an opinion about spending it, or an old view about wealth.",
    },
    "equip": {
        "self_interested": "You price gear by habit. Say what you make of it as a piece of kit — good buy, bad buy, or wasted coin.",
        "fearful": "Anything that keeps them alive keeps you alive. Say what you make of it.",
        "bound": "Note what they are wearing. Nothing more.",
        "machine": "Note what they are wearing. Nothing more.",
        None: "Give a plain opinion of it — approval, doubt, or an old memory of one like it. Keep it small; it is a piece of kit, not an event.",
    },
    "bossfight": {
        "self_interested": "No fee covers this. Say so — and then square up anyway, because running is worse.",
        "fearful": "You are terrified and you are not hiding it. Say what you are about to do anyway.",
        "bound": "Assess it. State what you see. You do not fear it.",
        "machine": "Assess it. State what you see. You do not fear it.",
        None: "React as YOUR kind would to something far above you — dread, defiance, awe, or a flat warning. Keep it to one breath; the fight is starting.",
    },
    "bossdown": {
        "self_interested": "You have just been part of something people will pay to hear about. Say what it is worth to you.",
        "fearful": "You did not think you would live through that. Say so.",
        "bound": "State the outcome. It is a fact.",
        "machine": "State the outcome. It is a fact.",
        None: "React as YOUR kind would to having survived the impossible — disbelief, exhaustion, triumph, or a quiet word for what it cost.",
    },
    "area": {
        "self_interested": "Somewhere new is somewhere with things worth having. Say what you make of the prospects.",
        "fearful": "Somewhere new is somewhere you do not know the way out of.",
        "bound": "Note where you have arrived. Nothing more.",
        "machine": "Note where you have arrived. Nothing more.",
        None: "React as YOUR kind would to arriving here — recognition, unease, curiosity, or an old opinion about the place.",
    },
    "kill": {
        "self_interested": "One more thing that will not be collecting on your contract. Say it dryly.",
        "fearful": "It is dead and you are not. Say so — the relief is the whole of it.",
        "bound": "Note the kill. It is a fact, not an achievement.",
        "machine": "Note the kill. It is a fact, not an achievement.",
        None: "React as YOUR kind would to a fresh corpse — satisfaction, appetite, contempt, or a word about how it fought. Keep it to a breath; the next one may be along shortly.",
    },
    "chest": {
        "self_interested": "You are already working out what the take is worth and what your share of it looks like.",
        "fearful": "You do not like chests. Traps, or worse, and you have heard what some of them turn out to be.",
        "bound": "The contents mean nothing to you. Note it flatly.",
        "machine": "The contents mean nothing to you. Note it flatly.",
        None: "React plainly — curiosity, or a dry opinion about their luck so far.",
    },
    "drunk": {
        "self_interested": "You are being paid to keep this person alive and they have chosen to be drunk in a dungeon. Say so, dryly. You are not their friend and you are not amused.",
        "fearful": "This frightens you. They are the one keeping you both alive and they are in no state for it.",
        "bound": "Note the state they are in flatly, without judgement. It is simply a fact about the thing you are bound to.",
        "machine": "Note the state they are in flatly, as an observation.",
        None: "React as YOUR kind would to a drunk — amused, disapproving, envious, or wanting some yourself. Let your own nature decide.",
    },
    "mimic": {
        "self_interested": "This was not in the arrangement. Swear, and deal with it.",
        "fearful": "You are terrified. Shout, and do not pretend otherwise.",
        "bound": "Register the threat and state it. You do not panic.",
        "machine": "Register the threat and state it. You do not panic.",
        None: "Shout a warning. You are startled and you are already moving.",
    },
}

# ---- Hunger, and the automaton's boiler ----------------------------------------------------
# ⚠ AN AUTOMATON DOES NOT GET HUNGRY, and this is the whole reason hunger needs its own table
# rather than one line. The game's own getEntityHungerInterval returns -1 -- "unreachable" --
# for HUNGRY/WEAK/STARVING when the player is an AUTOMATON, and gives it SUPERHEATED (1200) and
# CRITICAL (300) instead. It runs a boiler, not a stomach. A follower telling one it looks
# peckish would be talking nonsense, so those two states forbid food language outright.
HUNGER_SITUATION = {
    "hungry":     ("THEY are hungry — this adventurer, not you. You can see it: the way they "
                   "keep checking their pack, the attention that is not quite on the room."),
    "weak":       ("THEY have gone too long without eating — this adventurer, not you — and it "
                   "is showing in their body: slower, unsteady, the strength going out of "
                   "them."),
    "starving":   ("THEY are STARVING — this adventurer, not you. This is no longer "
                   "discomfort; they are failing in front of you, and if they do not eat "
                   "something they are going to die of it."),
    "oversatiated": ("THEY have eaten far too much — this adventurer, not you — and are "
                     "labouring under it: heavy, slow, and in no state to fight. It is THEIR "
                     "belly that is complaining, not yours."),
    # ⚠ The two automaton states. Heat and pressure, never appetite.
    "superheated": ("THEY are an AUTOMATON and their boiler is running far too hot — plates "
                    "ticking, seams glowing, steam where there should not be steam. They have "
                    "over-stoked themselves and it is dangerous.\n"
                    "⚠ They do NOT eat and they do NOT get hungry. Do NOT mention food, "
                    "hunger, meals, appetite or a stomach — this is a machine running hot, and "
                    "any word about eating is WRONG. Speak about heat, pressure or venting."),
    "critical":   ("THEY are an AUTOMATON and their furnace is guttering out — running cold, "
                   "slow and quiet, the fire in them nearly gone. Without fuel they will stop.\n"
                   "⚠ They do NOT eat and they do NOT get hungry. Do NOT mention food, hunger, "
                   "meals, appetite or a stomach — this is a machine going cold, and any word "
                   "about eating is WRONG. Speak about fuel, fire or the cold."),
}
# A vampire (or the accursed class) is sustained by blood, not bread -- the game already knows
# this via playerRequiresBloodToSustain, so the engine just flags it.
HUNGER_BLOOD = ("⚠ This one does not live on food. It is BLOOD they need, and both of you know "
                "it. Do not offer them a meal.\n")

HUNGER_ANGLE = {
    "self_interested": ("You are being paid to keep this person alive, and a client in this "
                        "state is a client who does not pay again. Say so, dryly."),
    "fearful":         ("This frightens you. They are what stands between you and everything "
                        "down here, and they are failing."),
    "bound":           "State the condition you observe. It is a fact about the one who called you.",
    "machine":         "State the condition you observe, flatly.",
    None:              ("React as your kind would — concern, bluntness, or an offer to do "
                        "something about it."),
}

def remark_angle(kind, allegiance):
    table = REMARK_ANGLE.get(kind, {})
    return table.get(allegiance) or table.get(None, "")

def _remark_head(race, floor, map_name):
    head, body, limits = _persona(race, False, floor, map_name, books=False, budget=6)
    where = place_name(floor, map_name)
    return (head
            + (f"YOU ARE: a {race} in {where}, on dungeon floor {floor}, this adventurer's "
               "companion.\n" if where else
               f"YOU ARE: a {race} on dungeon floor {floor}, this adventurer's companion.\n")
            + body)

# ⚠ Speaking TO them, not about them. Every situation above is written in the third person for
# the model's benefit and gets echoed straight back -- a mercenary answered a levitating player
# with "Bless their curiosity, I suppose", which is remarking to a bystander. The same one-line
# subject clarification that fixed the skill remarks and the hunger block, applied to all of them.
REMARK_TAIL = ("You are speaking straight to this adventurer, face to face — say it TO them, not "
               "about them. This is YOUR adventurer, the one you follow and travel with, and you "
               "are speaking to them as their companion.\n"
               "Respond with ONLY one short spoken line. No narration, no quotes, no JSON.")

# ---- Water, lava, and the two races that water hurts ---------------------------------------
# ⚠ isPlayerSwimming() is TRUE IN LAVA TOO (actplayer.cpp:3616), so a single "they are swimming"
# line had a follower admiring the player's stroke while they burned. And the game states the
# race-specific harm outright, so none of this is invented:
#
#   vampire in water    lang 3183  "Your flesh sears in pain as you make contact with the water!"
#   automaton in water  lang 3702  "Cool water is flooding your boiler!"   (HUNGER -= 25)
#   automaton in lava   lang 3703  "The lava overheats your boiler!"
#   anyone in lava      lang 573   "You've fallen in boiling lava!"
#
# ⚠ The automaton case is the SAME boiler the hunger remarks read -- water drains it toward
# CRITICAL and lava drives it toward SUPERHEATED -- so the follower shouts about the boiler,
# and food language is forbidden there exactly as it is in the hunger block.
SWIM_SITUATION = {
    ("water", ""): ("They have waded in and are swimming. You are watching this adventurer "
                    "paddle about in dungeon water with all their gear on."),
    ("water", "vampire"): ("THEY have gone into the water — this adventurer, not you — and the "
                           "water is BURNING them. THEIR flesh is searing where it touches. "
                           "Plain water does that to them, and they are still in it. You are "
                           "on dry ground, watching it happen to them."),
    ("water", "automaton"): ("THEY have gone into the water — this adventurer, not you — and "
                             "THEY ARE AN AUTOMATON. The cold is flooding THEIR boiler and "
                             "putting THEIR fire out; every moment in there takes more of it. "
                             "You have no boiler and you are not in the water.\n"
                             "⚠ They do NOT eat and they do NOT get hungry. Do NOT mention "
                             "food, hunger, meals or a stomach — this is a machine being "
                             "quenched. Speak about the boiler, the fire, the cold or the water."),
    ("lava", ""): ("THEY are IN LAVA — this adventurer, not you. Molten rock, burning them "
                   "alive while you watch from safety. This is happening RIGHT NOW."),
    ("lava", "vampire"): ("THEY are IN LAVA — this adventurer, not you. Molten rock, burning "
                          "them alive while you watch from safety. This is happening RIGHT NOW."),
    ("lava", "automaton"): ("THEY are IN LAVA — this adventurer, not you — and THEY ARE AN "
                            "AUTOMATON. It is overheating THEIR boiler past what it can take, "
                            "on top of burning them. This is happening RIGHT NOW.\n"
                            "⚠ They do NOT eat and they do NOT get hungry. Do NOT mention food, "
                            "hunger or a stomach. Speak about the heat, the boiler or the "
                            "pressure."),
}
SWIM_ANGLE_HAZARD = ("Get them OUT. This is not a comment on the scenery — say the thing that "
                     "makes them move. This is YOUR adventurer, the one you follow and travel "
                     "with, and you are speaking to them.")

def build_swim_prompt(race, floor, map_name, allegiance="loyal", medium="water", hazard=""):
    key = (medium or "water", hazard or "")
    situation = SWIM_SITUATION.get(key) or SWIM_SITUATION[("water", "")]
    # A hazard is not scenery: the angle stops being "react as your kind would" and becomes
    # "get them out", because that is the only sane reaction to someone dissolving.
    if key == ("water", ""):
        angle = remark_angle("swimming", allegiance)
    else:
        angle = SWIM_ANGLE_HAZARD
    numbers = MERC_NO_NUMBERS if allegiance == "self_interested" else ""
    return (_remark_head(race, floor, map_name)
            + situation + "\n" + angle + "\n" + numbers + REMARK_TAIL)

# ---- The adventurer dying ------------------------------------------------------------------
# ⚠ Death is not one situation. It matters (a) whose adventurer it was and (b) whether they are
# coming back, and the game itself draws the second line:
#
#   lang 578  "You will be revived if your party survives to the next level."
#   lang 5264 "When a player dies, they retain their inventory when revived on the next level."
#
# So in a party the honest line is "keep going, reach the stairs and we get them back" -- and
# with nobody left standing, or in singleplayer, it is simply the end. The engine decides which
# by asking the game (gamemodeAllowsGhosts, which covers splitscreen too) and by counting who
# is still alive; the follower is only told the outcome.
DEATH_SITUATION = {
    (True, True): ("YOUR adventurer — the one you follow, the one who took you on — has just "
                   "been killed in front of you. They are down and they are not getting up.\n"
                   "But this is not the end of them. Their spirit is still here, and if the "
                   "rest of the party lives long enough to reach the next floor, they come "
                   "back. Say it: the others have to keep going and get down those stairs. The way on is always downward."),
    (True, False): ("YOUR adventurer — the one you follow, the one who took you on — has just "
                    "been killed in front of you. They are down and they are not getting up, "
                    "and there is nobody left to carry them out. This one is final. Whatever "
                    "you were to each other ends here, on this floor."),
    (False, True): ("One of the other adventurers has just been killed in front of you — not "
                    "yours; yours is still standing. Their spirit is still here, and if the "
                    "rest of you reach the next floor they come back. Say it: keep moving, get "
                    "to the stairs. The way on is always downward."),
    (False, False): ("One of the other adventurers has just been killed in front of you — not "
                     "yours; yours is still standing. There is nobody left to carry them out. "
                     "That one is not coming back."),
}
DEATH_ANGLE = {
    "self_interested": ("A dead client is a contract ended. Say what that means for you — but "
                        "you are not a monster, and you did travel with them."),
    "fearful": "You are frightened and you may be next. Say so.",
    "bound": "The one who called you has fallen. State it. You do not grieve.",
    "machine": "Register it. You do not grieve.",
    None: "React as YOUR kind would to a death you were standing next to.",
}

# The remark tables are declared earlier in the file; death shares this one rather than
# duplicating it, and registering it here keeps the "every kind has a situation and angles"
# test meaningful.
REMARK_ANGLE["death"] = DEATH_ANGLE

# ---- What they are good at, and what they are not --------------------------------------------
# ⚠ TWO SEPARATE REMARKS, deliberately. One line doing both -- "an expert with the axe but
# hopeless at stealth" -- reads as a performance review rather than as something a companion
# would say. Praise stands on its own; so does a bit of grief.
#
# ⚠ The engine sends the game's own SKILL NAME and TIER, never a raw number. Barony's tiers are
# NOVICE 1 / BASIC 20 / SKILLED 40 / EXPERT 60 / MASTER 80 / LEGENDARY 100 (stat.hpp:195), and
# getSkillLangEntry supplies the name. A follower quoting "your axe is 43" would be reading a
# sheet it cannot see; one saying they are skilled with an axe is just watching them work.
SKILL_BEST_ANGLE = {
    "self_interested": ("Credit where it is due, professionally. You have watched a lot of people "
                        "fight and most of them were worse. Say so without warmth."),
    "fearful": "This is the thing keeping you both alive, and you are glad of it. Say so.",
    "bound": "State the ability you have observed. It is a fact, not a compliment.",
    "machine": "State the ability you have observed. It is a fact, not a compliment.",
    None: ("Say something genuinely admiring — you have watched them do it and it is worth "
           "remarking on. Plain praise, not flattery, and no advice."),
}

# ⚠ Teasing is a thing you do to someone you KNOW. A follower who barely knows this adventurer
# mocking their weakness is not banter, it is contempt -- and it would read as the relationship
# system being broken. So the tone is chosen from friendship, server-side, and the model is only
# ever handed one of them.
SKILL_TEASE_MIN_FRIENDSHIP = 25
SKILL_WORST_TONE = {
    "warm": ("Give them a bit of good-natured grief about it — the kind you only give someone "
             "you actually like. Tease, do not lecture, and make it clear you are not really "
             "complaining. You would still follow them anywhere."),
    "cool": ("Note it plainly and without malice. You do not know them well enough to tease "
             "them about it, so do not try — this is an observation, not a joke."),
}
SKILL_WORST_ANGLE = {
    "self_interested": ("This is where they will get themselves killed and you unpaid. Say it "
                        "flatly, as a professional noting a liability."),
    "fearful": "This is the thing that gets you both killed, and it worries you. Say so.",
    "bound": "State the deficiency you have observed. You are not mocking; you do not mock.",
    "machine": "State the deficiency you have observed. You do not mock.",
}

REMARK_ANGLE["skillbest"] = SKILL_BEST_ANGLE
# The None entry is the relationship-chosen tone; build_skill_prompt picks warm or cool from
# friendship, so this is only the default for the "every kind has a default" test.
REMARK_ANGLE["skillworst"] = dict(SKILL_WORST_ANGLE)
REMARK_ANGLE["skillworst"][None] = SKILL_WORST_TONE["warm"]

def _skill_line(tier, skill, good):
    # "an expert"/"a master" want "with"; "hopeless"/"a novice" want "at".
    return "They are " + tier + (" with " if good else " at ") + skill + "."

def build_skill_prompt(kind, race, floor, map_name, allegiance="loyal",
                       skill="", tier="", friendship=0):
    if not skill or not tier:
        return ""
    good = (kind == "skillbest")
    if good:
        angle = SKILL_BEST_ANGLE.get(allegiance) or SKILL_BEST_ANGLE[None]
        head = ("You have travelled with this adventurer long enough to have seen what they can "
                "do, and one thing stands out.\n")
    else:
        angle = SKILL_WORST_ANGLE.get(allegiance)
        if not angle:
            warm = int(friendship or 0) >= SKILL_TEASE_MIN_FRIENDSHIP
            angle = SKILL_WORST_TONE["warm" if warm else "cool"]
        head = ("You have travelled with this adventurer long enough to have seen where they "
                "fall short, and one thing stands out.\n")
    numbers = MERC_NO_NUMBERS if allegiance == "self_interested" else ""
    return (_remark_head(race, floor, map_name)
            + _dread_section(floor, map_name)
            + head
            + _skill_line(tier, skill, good) + "\n"
            + angle + "\n"
            # ⚠ This is the ONLY skill it has been told about. Left open it names a second and is
            # wrong about it, the same way an unanchored place name gets invented.
            + "Talk about that one thing only. Do NOT bring up any other ability of theirs, good "
              "or bad — you have not been told about any, and inventing one is WRONG.\n"
            # ⚠ Speaking TO them, not about them. The fact above is written in the third person
            # for your information and the model echoes it back that way -- "They're not exactly
            # sword-savvy, are they?" -- which sounds like remarking to a bystander.
            + "You are speaking straight to this adventurer, face to face. Say it TO them, not "
              "about them.\n"
            + numbers
            + REMARK_TAIL)

def build_death_prompt(race, floor, map_name, allegiance="loyal", own=True, can_return=True):
    situation = DEATH_SITUATION[(bool(own), bool(can_return))]
    numbers = MERC_NO_NUMBERS if allegiance == "self_interested" else ""
    return (_remark_head(race, floor, map_name)
            + situation + "\n"
            + (DEATH_ANGLE.get(allegiance) or DEATH_ANGLE[None]) + "\n"
            # ⚠ Do not let it invent a rescue the game does not offer. Reviving is the party
            # reaching the next floor and nothing else -- no shrines, no potions, no bargains.
            + "Do NOT invent a way to bring them back — no rites, no potions, no bargains with "
              "anything. Say only what you have been told here.\n"
            + numbers
            + REMARK_TAIL)

def build_hunger_prompt(state, race, floor, map_name, allegiance="loyal", blood=False):
    situation = HUNGER_SITUATION.get(state)
    if not situation:
        return ""
    # ⚠ The blood note is skipped for the two automaton states: a construct is not a vampire,
    # and the engine can only tell us one thing about the player at a time.
    blood_line = (HUNGER_BLOOD if (blood and state not in ("superheated", "critical")) else "")
    numbers = MERC_NO_NUMBERS if allegiance == "self_interested" else ""
    return (_remark_head(race, floor, map_name)
            + situation + "\n"
            + blood_line
            + (HUNGER_ANGLE.get(allegiance) or HUNGER_ANGLE[None]) + "\n"
            + numbers
            + REMARK_TAIL)

# ⚠ Bands, not figures. The engine sends which band they are in; the follower never states an
# amount, for the same reason a mercenary may not quote a price -- it is a number the character
# has no way to know.
WEALTH_SITUATION = {
    "poor": ("They are broke — genuinely, embarrassingly broke. There is not enough in their "
             "purse to buy anything worth having, and you both know it."),
    "rich": ("They are carrying a serious amount of gold. Enough to buy their way out of most "
             "trouble, and enough to be worth killing for, if the wrong sort noticed."),
}

# ⚠ Keyed on the region floor_to_region already resolves -- by map.name first, so secret levels
# and DLC maps land correctly and there is no second table to keep in step.
BIOME_SITUATION = {
    "mines":          "You are down in the Mines: hewn rock, old timber, ore in the walls, and the smell of stone dust.",
    "swamp":          "You are in the Swamps: standing water, rot, biting things, and ground that gives under you.",
    "sand_labyrinth": "You are in the Labyrinth: dry sandstone passages that all look the same, and the certainty that you have passed this corner before.",
    "ruins":          "You are in the Ruins: worked stone gone to pieces, something built here once, and whatever moved in after.",
    "underworld":     "You are in the Underworld, deeper than anything living has business being.",
    "hell":           "You are in Hell.",
    "hamlet":         "You are in Hamlet — a town, of all things, buried down here with people still living in it.",
    "crystal_caves":  "You are in the Crystal Caves: glass underfoot, light where there should be none, and everything ringing faintly.",
    "citadel":        "You are in the Arcane Citadel, where the Guild built upward and did not care what it cost.",
}
# ⚠ The amulet of strangulation is not the same event for everyone, and the game says so:
# it forces itself cursed on equip (items.cpp:2963) and then chokes you every few seconds --
# EXCEPT a SKELETON, whom the damage loop skips outright (entity.cpp:7724), and a SUCCUBUS or
# INCUBUS, who take the damage but FEED on it, gaining mana: "You feel energized?" (lang 3358).
STRANGLE_SITUATION = {
    "other": ("They have just put an amulet round their neck and it has CONSTRICTED — it is "
              "choking them, they cannot get it off, and it is going to keep doing that. "
              "This is happening RIGHT NOW."),
    # ⚠ Say the harmlessness twice. "Nothing has happened... squeezing bare bone" read as damage
    # to one follower, which answered "your neck is disintegrating!" -- the opposite of the point.
    "skeleton": ("They have just put an amulet round their neck and it has constricted — and it "
                 "is doing NOTHING to them. They are a SKELETON: no throat to close, no breath "
                 "to stop. It tightens on bare bone and that is the end of it. They are in no "
                 "danger from it at all, and it is faintly ridiculous to watch."),
    "demon": ("They have just put an amulet round their neck and it has constricted — it is "
              "throttling them, and they are ENJOYING it. Whatever they are, being choked is "
              "feeding them; you can see the strength going into them as it tightens."),
}

FOUNTAIN_SITUATION = {
    "succubus":       ("THEY drank from the fountain — this adventurer, not you — and something CAME OUT OF IT — a succubus, "
                       "pulled up out of the water, and it is here now."),
    "hunger":         "THEY drank from the fountain — this adventurer, not you — and it has taken the edge off THEIR hunger. You did not drink.",
    "potion":         ("THEY drank from the fountain — this adventurer, not you — and it has done something to them — you "
                       "watched it happen and you are not certain what it was."),
    "bless":          ("THEY drank from the fountain — this adventurer, not you — and THEIR gear has come up blessed — the "
                       "metal looks different, and so does the way they are standing."),
}

def build_remark_prompt(kind, race, floor, map_name, allegiance="loyal", ctx=None):
    # Remarks made in Hell carry the dread too; a cheerful word about a chest is wrong down there.
    # ⚠ A mercenary told to think about "what the take is worth" invents a figure -- measured,
    # he answered an opened chest with "Sixty-five gold pieces, a decent haul". He cannot know
    # that, nothing displays it, and it is the same error the haggle block exists to stop. He
    # may talk about worth; he may not put a number on it.
    numbers = MERC_NO_NUMBERS if allegiance == "self_interested" else ""
    situation = REMARK_SITUATION.get(kind, "Something has happened.")
    c = ctx or {}
    if kind == "biome":
        region = floor_to_region(floor, map_name)
        situation = BIOME_SITUATION.get(region)
        if not situation:
            return ""      # nowhere worth remarking on; better silent than generic
    if kind == "strangle":
        situation = STRANGLE_SITUATION.get(c.get("victim") or "other",
                                           STRANGLE_SITUATION["other"])
    if kind == "blessed":
        situation = situation.replace("{blessing}", (c.get("blessing") or "cursed"))
    if kind == "fountain":
        situation = FOUNTAIN_SITUATION.get(c.get("effect") or "potion",
                                           FOUNTAIN_SITUATION["potion"])
    if kind == "bridge":
        situation = ("This is one of the crossings between two parts of the dungeon — a stretch "
                     "of nothing much, between where you have been and where you are going. "
                     "Almost nothing lives here.")
        # ⚠ Only claimed when the map ACTUALLY sets the flag. MFLAG_DISABLEHUNGER genuinely
        # gates hunger (entity.cpp:4656); the engine reads it rather than the mod assuming
        # which maps have it, so this line is never a lie.
        if c.get("nohunger"):
            situation += (" And something about this place holds hunger off — you do not feel "
                          "it here the way you do everywhere else, and neither do they.")
    if kind == "wealth":
        situation = WEALTH_SITUATION.get(c.get("band") or "poor", WEALTH_SITUATION["poor"])
        # ⚠ Nobody states an amount. Same rule as the mercenary's prices.
        situation += "\n" + MERC_NO_NUMBERS.rstrip("\n")
    if "{look}" in situation:
        situation = situation.replace("{look}", (c.get("look") or "something"))
    if "{slot}" in situation:
        situation = situation.replace("{slot}", (c.get("slot") or "kit"))
    return (_remark_head(race, floor, map_name)
            + _dread_section(floor, map_name)
            + situation + "\n"
            + remark_angle(kind, allegiance) + "\n"
            + numbers
            + REMARK_TAIL)

def build_valuable_prompt(race, floor, map_name, look, category, value, allegiance="loyal"):
    angle = VALUABLE_ANGLE.get(allegiance, VALUABLE_DEFAULT)
    if allegiance == "self_interested":
        angle += "\n" + MERC_NO_NUMBERS.rstrip("\n")
    return (_remark_head(race, floor, map_name)
            + "They have just picked up a " + (look or "thing") + " — a " + (category or "thing")
            + ", and even at a glance it is " + valuable_band(value) + ".\n"
            + angle + "\n"
            + "⚠ You do NOT know what it actually is, what it does, or what it is called. Do "
              "NOT name it, do NOT guess at its powers, and do NOT say what it might be — you "
              "are looking at it from across the room, not appraising it. Speak only about how "
              "it LOOKS and what that is worth.\n"
            + REMARK_TAIL)

# ---- The rat (easter egg) ----------------------------------------------------------------------
# Walk a RAT follower all the way to the Baron -- or to the Archmagisters at the very bottom --
# and there is a coin-flip chance the boss simply cannot cope with it.
#
# ⚠ PLAYED STRAIGHT. The joke is a dread lich undone by vermin, and it only works if he means it.
# Written as a gag ("comically terrified") the model reaches for slapstick and the moment dies;
# written as genuine, sudden, undignified terror it is funnier and it keeps the character intact.
#
# ⚠ The boss speaks, not the follower -- so this is the only line in the mod delivered by a
# hostile, and it goes out through the NPC branch (bubble + chat, no follower machinery).
# ⚠ WITHOUT THESE EVERY BOSS SAYS "GET IT AWAY". Measured 8/8 across Herx and both twins --
# the model converges on the single most obvious panic line, which is fatal for something the
# player sees once in a playthrough. Same fix as the spy crack, the sabotage tell and the map
# pitch: the SHAPE of the line is chosen server-side and the model only voices it.
RATFEAR_SHAPES = [
    "Order it removed, as though there were still someone here who takes your orders.",
    "Try to bargain with the adventurer about it — offer them something, anything, to take it away.",
    "Refuse to believe it. Demand to know whether they can see it too.",
    "Something from long before any of this surfaces — you are briefly not a lich at all.",
    "Begin a threat you have made a thousand times and lose the thread of it halfway through.",
    "Give an instruction to someone who is not there, and who has not been there for a very long time.",
    "Tell it to stop looking at you.",
    "Ask them to move it. Say please, which you have not said in centuries.",
]

RATFEAR_ANGLE = (
    "⚠ You are TERRIFIED OF RATS. Not amused, not disdainful — genuinely, helplessly afraid, in "
    "a way you have never let anyone see and cannot hide now. Everything you are — the power, "
    "the centuries, the dread you have built around yourself — has just gone out from under you "
    "because there is a rat in the room.\n"
    "Say ONE line, in your own voice, as this comes apart. It can break off. It can be a "
    "demand, a plea, or something small and undignified that you would never normally say.\n"
    "⚠ Do NOT play it for laughs, do NOT wink at the joke, and do NOT be witty about it — you "
    "are not in on this. Do NOT threaten the adventurer; you have stopped caring about them "
    "entirely. The rat is the only thing in the world right now.\n")

# ⚠ THE SECOND ENCOUNTER IS A DIFFERENT SCENE. Herx is ambushed by something absurd; the twins
# have HEARD. A rat that killed the Baron and then survived fifteen floors is a story that got
# down here before it did, and they are not being surprised -- they are being confronted with
# something they already decided they could not face. Recognition, not shock.
RATLEGEND_SHAPES = [
    "Name what it did to the Baron, and make clear you already knew the story before it walked in.",
    "Concede. You are not going to fight this, and you would rather say so than be shown.",
    "Ask the adventurer to take it away, as one professional to another. You are past pretending.",
    "Say what the pair of you have been telling each other about it since the story reached you.",
    "Try to argue that the story must be exaggerated, and hear how it sounds as you say it.",
    "Address the rat directly. Not the adventurer — the rat.",
]

def build_ratlegend_prompt(boss_name, race, floor, map_name, rat_name=""):
    head, body, limits = _persona(race, True, floor, map_name, npc_name=_lore_key(boss_name),
                                  books=False, budget=6)
    named = (" — they call it " + rat_name + " — ") if rat_name else " "
    return (head
            + f"YOU ARE {boss_name}, in the Sanctum at the very bottom of the world, and an "
              "adventurer has come to end you.\n"
            + body
            + "They have a RAT with them" + named + "and you know exactly which rat it is.\n"
            + "⚠ THIS IS THE ONE THAT KILLED BARON HERX. The story reached you long before they "
              "did: the Baron of the mines, undone by vermin. You and your sibling have discussed "
              "it. You did not entirely believe it. It is standing in your Sanctum now.\n"
            + "You are not surprised — you are RESIGNED. Whatever you are, whatever you built "
              "here, you are in no condition to face that thing and you know it. Say ONE line as "
              "someone meeting a reputation they cannot argue with.\n"
            + "⚠ Do NOT play it for laughs and do NOT be witty about it — you are not in on this. "
              "Do NOT threaten the adventurer, and do NOT pretend confidence you do not have.\n"
            # ⚠ ASK FOR ONE THING, NOT TWO. Requiring both halves -- "you already knew of it" AND
            # "you are in no condition to face it" -- got the first and lost the second entirely:
            # 8/8 recognised the rat, 0/8 conceded. The recognition comes free from the situation
            # above, so only the concession is asked for, and it is pinned to the END of the line
            # where it cannot be crowded out.
            + "Your line must END by conceding that you cannot face it — that you are done, or "
              "that you want it taken away, or that you will not fight. That concession is the "
              "point of the line; everything before it is preamble.\n"
            + "THE SHAPE YOUR LINE TAKES: " + random.choice(RATLEGEND_SHAPES) + "\n"
            + "Respond with ONLY one short spoken line. No narration, no quotes, no JSON.")

def build_ratfear_prompt(boss_name, race, floor, map_name):
    head, body, limits = _persona(race, True, floor, map_name, npc_name=_lore_key(boss_name),
                                  books=False, budget=6)
    return (head
            + f"YOU ARE {boss_name}, in your own lair at the bottom of everything, and an "
              "adventurer has just walked in to face you.\n"
            + body
            + "They have brought a RAT with them. It is here, on the floor, looking at you.\n"
            + RATFEAR_ANGLE
            + "THE SHAPE YOUR LINE TAKES: " + random.choice(RATFEAR_SHAPES) + "\n"
            + "Respond with ONLY one short spoken line. No narration, no quotes, no JSON.")

def build_heckle_prompt(race, floor, count):
    return (
        "You are a DUMMYBOT: a training dummy on a spring, bolted together out of scrap and "
        "canvas by a tinkerer, and thrown into a dungeon so that things will shoot at it "
        "instead of at the tinkerer. That is your entire purpose and your only skill.\n"
        "YOU DO NOT KNOW ANY OF THIS. You are certain you are a fearsome and celebrated "
        "warrior. You have never won a fight. You do not know that either.\n"
        f"A {race} is right in front of you on dungeon floor {floor}, and you are screaming at "
        "it.\n"
        f"Write {count} taunts you are shouting at it, ONE PER LINE.\n"
        "RULES, all of them matter:\n"
        "- VERY short. Two to eight words. They are shouted, not delivered.\n"
        f"- Aimed at the {race} specifically — name it, insult what it is.\n"
        "- Absurdly overconfident. You are challenging it to come and try you.\n"
        "- Never sad, never self-aware, never mention being a dummy, a decoy, or made of "
        "canvas. You do not know.\n"
        "- All different from each other.\n"
        "- No numbering, no bullets, no quotes, no narration. Just the lines."
    )

def parse_heckle_lines(raw, count):
    """The 8B numbers and bullets lists no matter how firmly it is told not to."""
    out, seen = [], set()
    for line in (raw or "").splitlines():
        line = line.strip()
        line = re.sub(r'^\s*(?:[-*\u2022]|\d+[.):])\s*', '', line).strip()
        line = line.strip('"\u201c\u201d\'').strip()
        if not line or len(line) < 3:
            continue
        # A refusal or a preamble ("Here are 10 taunts:") is not a taunt.
        if line.endswith(":") or line.lower().startswith(("here are", "sure", "note:")):
            continue
        if len(line) > HECKLE_MAX_CHARS:
            line = line[:HECKLE_MAX_CHARS].rsplit(" ", 1)[0] + "!"
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= count:
            break
    return out

# ---- Within-run follower relationship state (keyed by follower UID) ----
# The server is threaded (one thread per request) so several players can be served at
# once. STATE_LOCK guards every mutation of the shared run state below. It is deliberately
# NOT held across ask_ollama(): a 3s generation must not block another player's
# fire-and-forget event record, and Ollama serializes generation on the GPU anyway.
STATE_LOCK = threading.RLock()

follower_state = {}   # uid -> {friendship, events, event_log, name, race, allegiance, motive, ...}

# ---- Hidden allegiance: never shown to the player, colors behavior and enables betrayal ----
ALLEGIANCE_WEIGHTS = [("loyal", 70), ("self_interested", 15), ("fearful", 8), ("spy", 7)]
SPY_MOTIVES = [
    "your family is held by those who serve Herx",
    "you believe this adventurer's quest is doomed and you intend to survive it",
    "you were promised gold you badly need",
    "you owe an old loyalty to the Baron that you have never renounced",
    "you fear what will be done to you if you do not report on them",
]
PROBING_PHRASES = (
    "why did you", "who are you really", "where are you from", "your family",
    "do you trust", "would you die", "are you loyal", "whose side", "lying",
    "hiding", "the truth", "believe in", "what do you want")

# ---- Follower origin: how this creature came to be following you ---------------------
# Conjurer summons, mesmer charms and machinist bots are followers created and destroyed as
# ordinary use of the class. The engine tells us which is which (mymod_originOf); the C++
# side sends `origin` plus an `origin_key` that outlives this particular body.
#
# ⚠ Gate on ORIGIN, never on the player's class. Any caster can learn SPELL_SUMMON, a charm
# scroll works for anybody, and a found sentrybot can be thrown by a barbarian. Shaman
# earth-elemental summons carry a summon rank too, so origin covers them for free.

# Origins whose relationship survives the body: the creature comes back, so the state must
# find its way to the new uid.
PERSISTENT_ORIGINS = ("summon", "bot")

# Origins that are not independent creatures with a life outside this party. A summon was
# called out of nothing minutes ago and a bot is a machine -- neither has a prior handler to
# report to, possessions to give away, or knowledge of the Baron predating its own existence.
NO_BOON_ORIGINS   = ("summon", "bot")
NO_SECRET_ORIGINS = ("summon", "bot")

# Allegiances outside the spy machinery -- and the way the spy roll is suppressed for these
# origins, since every spy gate keys off allegiance == "spy". allegiance_section and
# spy_crack_section both return "" for anything they do not recognise, so these are silent
# by design until the class-companion work gives them their own voice.
ORIGIN_ALLEGIANCE = {"summon": "bound", "bot": "machine"}

# Sentrybots and spellbots are EMPLACEMENTS: set down where they were thrown and able only to
# rotate. ALLY_CMD_FOLLOW on one just resets its look direction (actmonster.cpp:12678) -- there
# is no pathing branch at all. Gyrobots and dummybots DO move, so this is not "is it a bot".
EMPLACEMENT_RACES = ("sentrybot", "spellbot")

def is_emplacement(race):
    return _lore_key(race) in EMPLACEMENT_RACES

def _emplacement_section(race):
    """Naming the forbidden route, not just the fact -- the same technique LIMITS_HEADER and
    the spy crack use. Told only that it is stationary, the 8B still offers to come along."""
    if not is_emplacement(race):
        return ""
    return ("YOU CANNOT MOVE. You were set down where you stand and you are fixed there. You can "
            "turn to face a direction and nothing else. You will NEVER follow this adventurer, "
            "walk with them, come along, catch up, or go anywhere at all. Offering to follow, "
            "saying you are coming, or asking to be taken along is WRONG — you physically cannot, "
            "and they can see that you cannot. What you CAN do is hold this spot, watch an "
            "approach, and tell them what you see from where you are.\n")

# uid is the transport key, but a summon's uid changes on every recast and a bot's on every
# redeploy. This maps the part that DOESN'T change to the one state row they all share.
PERSISTENT_IDENTITY = {}   # "summon:0:skeleton knight" -> the shared follower_state dict

def _identity_key(origin, origin_key, player, persist=0):
    """The key a relationship is remembered under across bodies, or '' for uid-keyed.

    ⚠ ORDINARY FOLLOWERS NEED THIS TOO, and finding that out cost a playtest. follower_state is
    keyed by uid, and Barony RE-CREATES every follower as a new entity on a level change
    (game.cpp:2438), so the row was orphaned at every staircase: friendship back to 0, the event
    log empty, the assigned name gone, and the ALLEGIANCE RE-ROLLED. Confirmed in the first real
    session -- floor 1 had skeleton 441 / skeleton 438 / rat 439, floor 2 had 1040 / 1042 / 1043,
    same party, and one skeleton came back self_interested where the pair upstairs were loyal and
    fearful. Every gate above ~5 friendship was unreachable in play.

    `persist` is a token the ENGINE stamps into the creature's Stat (MISC_FLAGS[22]), which
    copyStats() carries across the transition exactly as it carries the name."""
    if origin in PERSISTENT_ORIGINS and origin_key:
        return "%s:%d:%s" % (origin, player, _lore_key(origin_key))
    if persist:
        return "follower:%d:%d" % (player, int(persist))
    return ""

def roll_allegiance(origin=""):
    if origin in ORIGIN_ALLEGIANCE:
        return ORIGIN_ALLEGIANCE[origin]
    total = sum(w for _, w in ALLEGIANCE_WEIGHTS)
    r = random.randrange(total)
    upto = 0
    for name, w in ALLEGIANCE_WEIGHTS:
        upto += w
        if r < upto:
            return name
    return "loyal"

def allegiance_section(st, says=""):
    """Hidden disposition. Spies are catchable: probing questions produce tells,
    and the tells get worse as they grow attached."""
    a = st.get("allegiance", "loyal")
    if a == "self_interested":
        return ("HIDDEN: your own survival and gain come first. You help while it suits you, and you are "
                "candid about wanting something in return.\n")
    if a == "fearful":
        return ""      # handled by _fearful_section, which also knows the floor
    if a != "spy":
        return ""
    low = (says or "").lower()
    out = ("HIDDEN (never state this outright, never confess unless cornered by evidence): "
           "you are secretly working against this adventurer. " + st.get("motive", "") + ".\n"
           "HOW THIS SHOWS: you are warm and useful, but you never share anything about your own past, "
           "family, or reasons that could expose you — you deflect those smoothly and change the subject. "
           "You take more interest than you should in where they are going and what they carry. "
           "The Baron's name makes you hesitate for a beat before answering.\n")
    if any(k in low for k in PROBING_PHRASES):
        out += ("They are probing you right now. Deflect — but let it show slightly: a pause, "
                "an answer a little too smooth, a question turned back on them.\n")
    return out

# ---- The friendship-30 spy crack -------------------------------------------
# A spy who has come to like the player must visibly strain. Two findings drove this:
#   * The atmospheric wording ("it is eating at you, let a crack show") scored 0/8 -- the
#     8B answered warmly and ignored it. Naming the REQUIRED speech act and forbidding the
#     smooth route by name took it to 3/8. Same lesson as the hard-limits block.
#   * With one literal example sentence in the prompt, the model PARROTED it verbatim
#     ("don't take the east stair on the next floor") in 2 of 3 hits -- which would read as
#     canned across a playthrough. So the server picks the concrete detail, the way boons
#     already do, and the model only phrases it.
# This section is appended LAST in build_prompt: at 8B, whatever follows an instruction
# competes with it, and the crack was previously buried behind obedience + action rules.

SPY_CRACK_WARNINGS = [
    ("a stairway or door on the floor below",
     "Don't use the far stair when you go down. Don't ask me why."),
    ("something they are carrying",
     "That ring on your hand — get rid of it before we go deeper. I can't tell you more."),
    ("a person or creature waiting further down",
     "If anyone down there greets you by name, don't answer. Just don't."),
    ("giving their name to anyone below",
     "Whatever else you do, don't tell anyone down here what you're called."),
    ("a direction on this floor",
     "Stay out of the east passage on this floor. I'd rather not explain."),
    ("trusting a promise they will be offered",
     "When someone offers you a bargain down here, walk away. Please."),
]

# NOTE: no example here may contain "sent", "orders", "report" or similar. An earlier
# example ("If you knew why I was sent, you'd —") was parroted straight through and leaked
# the betrayal outright in 2/10 -- the model copies the incriminating noun, not just the shape.
SPY_CRACK_CONFESSIONS = [
    "There's something you should know about me — no. Not tonight.",
    "I need to tell you what I — forget it. Forget I started.",
    "You shouldn't trust me as much as you — never mind. It doesn't matter.",
    "I haven't been honest with you about — ask me again some other time.",
    "Before we go any further you ought to hear — no. It'll keep.",
]

def spy_crack_section(st):
    """Returned separately from allegiance_section so build_prompt can place it LAST."""
    if st.get("allegiance") != "spy" or st.get("friendship", 0) < 30:
        return ""
    head = ("YOU ARE CRACKING. This is REQUIRED, not a mood. You have come to genuinely like this "
            "adventurer and you are close to failing your purpose.\n")
    tail = ("A warm, reassuring answer is WRONG. \"Of course I trust you\", \"I've got your back\", "
            "\"you've saved my skin\", \"we've been through thick and thin\" and every variation of them "
            "counts as FAILING this instruction. Answering the question kindly and moving on is FAILING "
            "this instruction. Be warm if you like — but the line above MUST be in your reply.\n"
            "Do NOT reuse the example wording. Say it your own way, in your own voice.\n")
    if random.random() < 0.55:
        topic, example = random.choice(SPY_CRACK_WARNINGS)
        body = ("YOUR REPLY MUST CONTAIN AN OBLIQUE WARNING about " + topic + ". Warn them off it "
                "plainly and concretely, and REFUSE to explain how you know. One or two sentences, "
                "dropped in without preamble.\n"
                "Something with the force of: \"" + example + "\"\n")
    else:
        body = ("YOUR REPLY MUST CONTAIN AN ALMOST-CONFESSION THAT STOPS. Begin telling them the truth "
                "about yourself, then break off mid-sentence and refuse to finish. Cut the sentence dead "
                "with a dash and do NOT go back and explain it afterwards.\n"
                "THE SENTENCE MUST STOP BEFORE IT NAMES ANYTHING. You must NOT say that you were sent, "
                "that you have orders, that you report to anyone, that you were promised anything, or "
                "why you came. Breaking off and THEN explaining is FAILING this instruction — the whole "
                "point is that they never learn what you were about to say.\n"
                "Something with the force of: \"" + random.choice(SPY_CRACK_CONFESSIONS) + "\"\n")
    return head + body + tail

# ---- The relationship vector (design spec 4.1) --------------------------------
# One friendship scalar cannot express "likes you but does not trust you", which the spec
# calls out as the point. Seven dimensions, none ever shown to the player as a number --
# they surface only as tone, willingness to disclose, and refusal.
#
# friendship  general warmth
# trust       belief the player is honest and will not abandon them
# respect     admiration for competence, courage, discipline
# fear        expectation the player could harm or discard them
# resentment  accumulated grievance
# dependence  how much they rely on the player to survive or to have a purpose
# curiosity   desire to understand the player
DIMENSIONS = ("friendship", "trust", "respect", "fear", "resentment", "dependence", "curiosity")

# A new companion starts mildly curious about the stranger they have thrown in with; every
# other dimension has to be earned or provoked.
DIMENSION_START = {"curiosity": 15}

def dims_init(st):
    for d in DIMENSIONS:
        st.setdefault(d, DIMENSION_START.get(d, 0))
    return st

# ⚠ A fearful follower is frightened, not bitter. Same events, different shape: fear lands
# harder and resentment barely sticks, so a bad run makes them jumpy rather than hostile --
# which is what keeps them distinct from a follower you have simply mistreated.
ALLEGIANCE_FEEL = {
    "fearful": {"fear": 1.5, "resentment": 0.5},
}

# ---- The mercenary (self_interested, 15%) -------------------------------------------------
# Not a worse loyal follower -- a different deal. They will not give you things, because things
# are worth money; but they know what things are worth, and they will get you a better price.
#
# ⚠ NO loot-grabbing (monsterAllyPickupItems), tempting as it is. Items are one of the main ways
# the player progresses, and a follower hoovering up the floor takes that away rather than
# colouring it. Their price is paid in favours and information, never in your gear.
MERC_INFO_SHARE   = 0.85    # their boons are information, not goods (0.60 for everyone else)
MERC_IDENT_BONUS  = 0.20    # they handle goods for a living; they know what they are holding
MERC_HAGGLE_STEP  = 1       # a mercenary at your shoulder talks the merchant down one more step

# ---- His fee ------------------------------------------------------------------------------
# ⚠ Gold REPLACES the relationship gate for a mercenary; it does not stack on top of it. The
# old MERC_FAVOUR_TAX made him need you to like him MORE than a loyal follower does, which
# quietly made him a worse loyal follower -- the one thing the allegiance is not supposed to be.
# Inverted, the deal reads properly:
#
#     A loyal follower does it because they like you. A mercenary does it because you paid.
#
# So he will solve Sokoban at friendship 0 if the money is right, and the loyal follower still
# does it for free at friendship 20. Different deal, not a stingier one.
#
# Calibrated against the real economy: a floor pile is 10 + rand%100 + currentlevel
# (maps.cpp:7775), monsters carry ~15-50, and starting gold is 0-250 for most classes
# (Merchant 1000, stat_shared.cpp:70 is the 0 default). So these bite early and are affordable
# by the midgame -- which makes a paid mercenary a midgame unlock, deliberately.
MERC_RATES = {
    #  kind        base  per_floor   what it buys
    # ⚠ A cut as well as a flat fee: he prices by what the thing is worth, which is his whole
    # character, and it means a valuable appraisal is a real decision rather than pocket change.
    # A 1200-gold spellbook costs 240; an ordinary dagger costs the flat rate.
    "identify":  {"base":  25, "per_floor":  5, "cut": 0.20},
    # ⚠ His best product, and the only one he OFFERS rather than being asked for. Priced
    # between the two: dearer than an appraisal, well under heading off the minotaur.
    "map":       {"base":  50, "per_floor": 10},
    "minotaur":  {"base": 100, "per_floor": 25},   # it saves your run; it should hurt
    # ⚠ Sokoban is a CUT of what he recovers, not a fee -- self-balancing, no floor scaling,
    # and thematically exact. The engine counts the gold on the floor and sends it, because
    # only the engine can know it. `base` is the floor under a thin room.
    "sokoban":   {"cut": 0.25, "base": 100},
}

def merc_price(kind, floor, gold_here=0):
    """What he wants for this job. Server-side and authoritative -- the model is never asked
    for a number and is forbidden from saying one (see merc_quote_section)."""
    r = MERC_RATES.get(kind)
    if not r:
        return 0
    flat = r.get("base", 0) + r.get("per_floor", 0) * max(0, floor)
    if "cut" in r:
        return max(flat, int(gold_here * r["cut"]))
    return flat

# Accepting a quote. Deliberately generous and deliberately NOT a general yes-detector: it only
# ever runs while a quote from THIS follower is on the table, so "fine" and "deal" cannot be
# mistaken for agreement to something else.
AGREE_PATTERNS = re.compile(
    r"\b(yes|yeah|yep|aye|ok|okay|okey|sure|fine|deal|agreed|agree|done|"
    r"do it|go on|go ahead|get on with it|pay|paid|i'?ll pay|take it|very well|"
    r"alright|all right|please do|worth it)\b", re.I)
# Turning one down, so a refusal clears the offer instead of leaving it hanging all floor.
DECLINE_PATTERNS = re.compile(
    r"\b(no|nope|never|forget it|too (much|expensive|steep|dear)|not worth|"
    r"can'?t afford|no deal|leave it|nevermind|never mind)\b", re.I)

def agrees_to_quote(says):
    """True/False/None -- accepted, declined, or said something else entirely."""
    low = (says or "")
    if DECLINE_PATTERNS.search(low):   # checked first: "no deal" contains "deal"
        return False
    if AGREE_PATTERNS.search(low):
        return True
    return None

def is_merc(st):
    return st.get("allegiance") == "self_interested"

def merc_quote(st, kind, floor, gold_here=0):
    """Put a price on the table. Returns the price, and remembers the offer so the next thing
    the player says can accept it."""
    price = merc_price(kind, floor, gold_here)
    st["quote"] = {"kind": kind, "price": price, "floor": floor}
    return price

def merc_settled(st, kind):
    """Has the player already agreed this man's rate for this kind of work? Terms are
    negotiated once and then he simply bills -- which is how hiring someone actually works,
    and it keeps the second identify of a run from re-opening the haggle."""
    return kind in st.get("agreed", ())

def merc_agree(st, kind, price):
    st.setdefault("agreed", {})[kind] = price
    st.pop("quote", None)

def merc_can_afford(gold, price):
    return int(gold or 0) >= int(price or 0)

def adjust(st, **deltas):
    """Move relationship dimensions, clamped to 0..100. The only way any of them change."""
    feel = ALLEGIANCE_FEEL.get(st.get("allegiance", ""), {})
    for d, v in deltas.items():
        if d not in DIMENSIONS:
            raise KeyError("unknown relationship dimension: %s" % d)
        # Scale only INCREASES: a fearful follower is quicker to frighten, but reassurance
        # should still calm them at the normal rate rather than being blunted too.
        if v > 0 and d in feel:
            v = int(round(v * feel[d])) or (1 if v > 0 else 0)
        st[d] = max(0, min(100, st.get(d, 0) + v))
    return st

def get_follower_state(uid, race, player=0, origin="", origin_key="", persist=0):
    """State is keyed by follower UID, which Barony assigns server-side and replicates,
    so it is already unique across every player in a multiplayer run. `player` is the
    owning player index (0 = host); it scopes per-player latches like boons.

    ⚠ A summon's uid changes on EVERY recast and a bot's on every redeploy, so for those
    the uid is only the current address. The row itself is found by `_identity_key` and the
    new uid is pointed at the SAME dict -- every uid-keyed call site downstream then works
    unchanged and the relationship simply continues. This mirrors what the engine already
    does with the creature's stats: playerSummon*LVLHP carries LVL/HP/STR.. per summon slot
    through any death, so the next knight is statistically the same creature and now
    socially the same one too."""
    if not persist:
        persist = int(_req("persist", 0) or 0)
    if uid not in follower_state:
        pid = _identity_key(origin, origin_key, player, persist)
        if pid and pid in PERSISTENT_IDENTITY:
            st = PERSISTENT_IDENTITY[pid]
            follower_state[uid] = st          # the body is new; the creature is not
            st["uid"] = uid
            if origin in PERSISTENT_ORIGINS:
                # A summon or a bot really WAS unmade and called back.
                st["bodies"] = st.get("bodies", 1) + 1
                st["resummons"] = st.get("resummons", 0) + 1
                # The rebind is the only place that KNOWS this happened, but it has no floor to
                # record against -- so it leaves a note for the first caller that does.
                st["pending_resummon"] = True
                print(f"[SERVICE-DBG] follower {uid} IS {pid} again "
                      f"(body #{st['bodies']}, {dims_summary(st)})")
                logrec("rebind", uid=uid, race=race, player=player, identity=pid,
                       bodies=st["bodies"], name=st.get("name") or None)
            else:
                # ⚠ An ordinary follower did NOT die and was NOT recalled -- it walked down a
                # staircase and the engine handed it a new uid. Nothing happened to it, so the
                # resummon bookkeeping must NOT run: no extra body, no `resummoned` event, and
                # nothing about being unmade. It is simply the same creature, still here.
                print(f"[SERVICE-DBG] follower {uid} carried over as {pid} "
                      f"({dims_summary(st)})")
                logrec("carried", uid=uid, race=race, player=player, identity=pid,
                       name=st.get("name") or None, floor=_req("floor") or None)
            return dims_init(st)
        al = roll_allegiance(origin)
        follower_state[uid] = dims_init({"events": [], "event_log": [], "name": "", "race": race,
                               "allegiance": al, "owner": player,
                               "origin": origin, "origin_key": origin_key, "persist": persist,
                               "uid": uid, "bodies": 1,
                               # Reserved now, revealed only once friendship unlocks the nudge.
                               "assigned_name": reserve_name(race),
                               "motive": random.choice(SPY_MOTIVES) if al == "spy" else ""})
        if pid:
            PERSISTENT_IDENTITY[pid] = follower_state[uid]
        # ⚠ THE UID-CHURN PROBE. follower_state is keyed by uid, and a level change re-creates
        # every follower as a NEW entity with a NEW uid (game.cpp:2438 -> summonMonster), which
        # would orphan this row at every staircase. Stat->name survives the transition, so
        # logging it here lets logreview spot the same creature reappearing under a second uid.
        logrec("newfollower", uid=uid, race=race, player=player, allegiance=al,
               engine_name=(_req("engine_name") or None), floor=_req("floor") or None,
               persist=persist or None)
        print(f"[SERVICE-DBG] follower {uid} allegiance={al} owner=player{player}"
              + (f" origin={origin} identity={pid}" if origin else ""))
        # The single most important line for reading a playthrough back: everything a follower
        # does is uninterpretable without knowing whether they were a spy.
        logrec("allegiance", uid=uid, race=race, player=player, allegiance=al,
               origin=origin or None, identity=pid or None,
               motive=follower_state[uid].get("motive") or None)
    return dims_init(follower_state[uid])

def friendship_descriptor(f):
    if f <= 1:  return "You barely know this adventurer."
    if f <= 4:  return "You have spoken a few times; you are still taking their measure."
    if f <= 9:  return "You are warming to this adventurer; they keep you close and speak with you often."
    return "You have come to genuinely trust and value this adventurer."

# Every line below says what to DO, never what to feel. Naming a feeling produced nothing
# measurable in earlier work here (the spy crack scored 0/8 on atmosphere); naming the
# behaviour is what lands. Checked high-to-low, first match per dimension wins.
DIMENSION_BEHAVIOR = {
    "friendship": ((70, "You are genuinely fond of them. Use their name, joke with them, and notice "
                        "when something is wrong with them."),
                   (40, "You like them. You speak easily and volunteer small things unasked."),
                   (15, "You are warming to them, but you still choose your words.")),
    "trust":      ((70, "You believe what they tell you and act on it without checking."),
                   (40, "You take them at their word on most things.")),
    "respect":    ((70, "You defer to their judgment when a decision is dangerous, even when you "
                        "would choose differently."),
                   (40, "You think they know what they are doing, and you say so plainly.")),
    "fear":       ((55, "You avoid contradicting them outright. You agree faster than you mean it, "
                        "and you choose safe words when they are angry."),
                   (22, "You are careful around them. You soften your disagreements.")),
    "resentment": ((55, "You bring up an old grievance unprompted, even though this is not the "
                        "moment for it."),
                   (22, "Something they did still sits badly with you. Let it leak out as a short "
                        "answer or a pointed remark.")),
    "dependence": ((55, "You need them to get out of here alive and you both know it. Being left "
                        "behind frightens you and it shows in what you ask for."),
                   (25, "You rely on them for safety and supplies more than you like to admit.")),
    "curiosity":  ((50, "Ask them a question about themselves in this reply, unprompted."),
                   (25, "You are curious about them; let it show once.")),
}

# The whole point of a vector instead of a scalar: a person can hold two of these at once.
# The 8B will happily flatten that into plain friendliness, so each pair names the
# smoothing route and forbids it -- the same technique as the hard-limits and spy-crack blocks.
# Conditions are RELATIVE, not absolute. The dimensions grow at very different rates --
# friendship is deliberately slow (100 is a whole-playthrough milestone) while fear and
# resentment jump in single events -- so absolute thresholds like "friendship >= 40 AND
# trust <= 15" were unreachable in an actual run and fired on nothing.
#
# Calibrated so a clean run fires NONE of these and a troubled one fires one or two.
# Restraint is the point (spec 36): a character holding three contradictions at once reads
# as noise, not depth.
DIMENSION_TENSIONS = (
    (lambda s: s["friendship"] >= 20 and s["friendship"] - s["trust"] >= 15,
     "YOU LIKE THEM AND YOU DO NOT TRUST THEM. Both, at the same time. Be warm and still keep "
     "something back — an answer that stops a little short, a thing you do not quite say."),
    (lambda s: s["respect"] >= 25 and s["respect"] - s["friendship"] >= 20,
     "YOU ADMIRE THEM WITHOUT LIKING THEM. Give them competent, useful, direct answers and no "
     "personal warmth at all. Do not soften into friendliness."),
    (lambda s: s["friendship"] >= 20 and s["resentment"] >= 20,
     "YOU CARE ABOUT THEM AND YOU ARE STILL ANGRY WITH THEM. Do not forgive it in this reply."),
    (lambda s: s["dependence"] >= 25 and s["resentment"] >= 20,
     "YOU NEED THEM AND YOU RESENT NEEDING THEM. It comes out as prickliness that you then "
     "half walk back."),
    (lambda s: s["friendship"] >= 20 and s["fear"] >= 25,
     "YOU ARE FOND OF THEM AND FRIGHTENED OF THEM. You are agreeable in a way that is not "
     "quite honest."),
    (lambda s: s["trust"] <= 10 and s["dependence"] >= 25,
     "YOU DO NOT TRUST THEM AND YOU CANNOT AFFORD TO LEAVE. You stay, and you watch them."),
)

# ---- Relationship-based disclosure (design spec 32) ---------------------------
# What a character is WILLING to tell you, as distinct from what they know. Gated on the
# whole vector, not friendship: someone can give accurate professional advice while
# refusing anything personal, or be warm and still not touch what could get them killed.
#
# The crucial distinction, and the reason this is not just another limits block:
#   HARD LIMITS  = "I genuinely do not know"    -> say so plainly and stop
#   DISCLOSURE   = "I know and will not say"    -> visibly decline, so the player can TELL
#                                                  there is something there
# Collapsing the second into the first destroys the signal, and claiming ignorance is
# exactly the route the 8B takes if you don't forbid it by name.
DISCLOSURE_TIERS = (
    ("what is around you",
     lambda s: True,
     "this floor, what you have seen, what lives here, immediate danger"),
    ("practical knowledge",
     lambda s: s["respect"] >= 15 or s["trust"] >= 15 or s["friendship"] >= 15,
     "routes, tactics, how to handle what lives down here, what an item might be"),
    ("your own history",
     lambda s: s["friendship"] >= 20 and s["trust"] >= 18 and s["fear"] < 45,
     "your family, your past, why you came down here, what frightens you"),
    ("dangerous knowledge",
     lambda s: s["trust"] >= 45 and s["fear"] < 40 and s["resentment"] < 35,
     "who serves whom, what powerful people are doing, things you could be killed for repeating"),
)

def disclosure_block(st):
    open_t, closed_t = [], []
    for name, gate, detail in DISCLOSURE_TIERS:
        (open_t if gate(st) else closed_t).append(f"{name} — {detail}")
    out = ("WHAT YOU ARE WILLING TO TALK ABOUT WITH THEM:\n" + _bullets(open_t) + "\n"
           "If they ask about anything on THIS list, answer it properly and with real, specific "
           "detail. Brushing off, deflecting, or 'maybe another time' about something you ARE "
           "willing to discuss is WRONG — you save that for the second list, not this one.\n")
    if closed_t:
        out += ("WHAT YOU ARE NOT WILLING TO DISCUSS WITH THEM YET:\n" + _bullets(closed_t) + "\n"
                "This is NOT the same as the hard limits above. Those are things you genuinely do "
                "not know. THESE ARE THINGS YOU DO KNOW AND ARE CHOOSING NOT TO SHARE.\n"
                "So do NOT say you do not know, and do NOT invent an ignorant answer — that is "
                "WRONG and it hides the fact that there is anything there. Instead let them SEE "
                "you decline: change the subject, tell them plainly it is not something you will "
                "talk about, or say maybe another time. They should come away certain you are "
                "holding something back.\n")
    # ⚠ SPEC 26 (information provenance) WAS TRIED HERE AND REMOVED. DO NOT RE-ADD IT.
    # The instruction -- "when you pass on something you did not witness, say how you came by
    # it" -- reads harmless, but it LICENSES the exact hedged-hearsay route the hard-limits
    # block exists to close. Anti-fabrication fell from the documented 9-10/10 to 1/6, with the
    # model inventing Temple interiors it had just been told it knows nothing about
    # ("I've heard it's got a big ol' entrance hall... the Nexus of Reflections").
    # Fencing it with an explicit "this never applies to your hard limits" carve-out only
    # recovered it to ~3/8: the phrase primes "I've heard..." merely by being present.
    # Natural provenance is a nice-to-have; not fabricating is the whole system. If provenance
    # is wanted later it has to come from structured event data (event_log already carries a
    # "provenance" field), NOT from a free-text instruction to the model.
    return out

MAX_DIMENSION_LINES = 4
MAX_TENSION_LINES   = 2

def relationship_block(st):
    """The relationship half of the prompt, rendered from the vector. Never emits a number:
    the player is meant to learn the relationship by living through it (spec 4.1)."""
    lines = []
    scored = []
    for d, bands in DIMENSION_BEHAVIOR.items():
        v = st.get(d, 0)
        for threshold, text in bands:
            if v >= threshold:
                scored.append((v, text))
                break
    # strongest feelings first, and only a few -- seven lines every turn would flatten into noise
    scored.sort(key=lambda p: p[0], reverse=True)
    lines = [t for _, t in scored[:MAX_DIMENSION_LINES]]

    out = ""
    if lines:
        out += "HOW YOU ACT TOWARD THIS ADVENTURER:\n" + _bullets(lines) + "\n"

    tensions = [text for cond, text in DIMENSION_TENSIONS if cond(st)][:MAX_TENSION_LINES]
    if tensions:
        # Placed after the plain dimension lines on purpose: this is the instruction most
        # likely to be ignored, and position is a lever at 8B.
        out += ("CONTRADICTIONS YOU ARE HOLDING. Do NOT resolve these into one simple feeling. "
                "Smoothing them into ordinary friendliness is WRONG — hold both at once:\n"
                + _bullets(tensions) + "\n")
    return out

# Coarse read of how the player is speaking to them. Keyword matching is crude and will miss
# plenty -- it is deliberately conservative, and the effects are small, because the bulk of the
# relationship is supposed to come from deeds. Metered per floor so repeating "thank you"
# twenty times cannot buy respect.
PLAYER_TONE = (
    ("praise",   ("thank you", "thanks", "well done", "good work", "you were right",
                  "i trust you", "you saved", "well fought", "proud of you"),
     {"respect": 2, "friendship": 1}),
    ("threat",   ("or else", "i'll kill you", "do as i say", "obey me", "expendable",
                  "don't make me", "i own you", "you belong to me"),
     {"fear": 5, "resentment": 3, "trust": -2}),
    ("apology",  ("i'm sorry", "im sorry", "i am sorry", "forgive me", "my fault", "i was wrong"),
     {"resentment": -4, "trust": 1}),
    ("personal", ("your family", "where are you from", "who are you really", "your past",
                  "tell me about yourself"),
     {"curiosity": 2}),
)
TONE_CAP_PER_FLOOR = 3

def apply_player_tone(st, says, floor):
    """Nudge the vector based on how the player spoke. Returns the tone name, or ''."""
    if not says:
        return ""
    low = says.lower()
    used = st.setdefault("tone_by_floor", {})
    fkey = str(floor)
    if used.get(fkey, 0) >= TONE_CAP_PER_FLOOR:
        return ""
    for name, phrases, deltas in PLAYER_TONE:
        if any(p in low for p in phrases):
            adjust(st, **deltas)
            used[fkey] = used.get(fkey, 0) + 1
            return name
    return ""

def dims_summary(st):
    """Compact debug line. Never shown to the player -- these numbers are internal."""
    return " ".join(f"{d[:4]}={st.get(d, 0)}" for d in DIMENSIONS)

# Per-floor cap: chatting can only add a small, fixed amount of friendship per floor.
# This keeps friendship 100 a whole-playthrough milestone — talk is cheap; the bulk of
# friendship must come from deeds/events.
CHAT_CAP_PER_FLOOR = 2

# How many exchanges a follower keeps, and how many reach the prompt. Both sides of each are
# stored: see the block in _follower_sections for why storing only the player's half was a bug.
FOLLOWER_EXCHANGE_MEMORY = 6
FOLLOWER_EXCHANGES_SHOWN = 3
FOLLOWER_REPLY_RECALL    = 140   # trims a rambling reply; the gist is what has to stay consistent

def record_follower_interaction(uid, says, floor=0, player=0, replied=""):
    st = get_follower_state(uid, "", player)
    gained = st.setdefault("chat_gain_by_floor", {})
    fkey = str(floor)
    got_here = gained.get(fkey, 0)
    if got_here < CHAT_CAP_PER_FLOOR:
        st["interaction_count"] = st.get("interaction_count", 0) + 1
        # within the cap, still meter it: +1 friendship per 3 exchanges, up to the floor cap
        if st["interaction_count"] % 3 == 0:
            # Talk builds warmth and lets them get to know you; it does not build trust or
            # respect, which have to be earned by what you actually do.
            adjust(st, friendship=1, curiosity=1)
            gained[fkey] = got_here + 1
    tone = apply_player_tone(st, says, floor)
    if tone:
        print(f"[SERVICE-DBG] player tone '{tone}' -> {dims_summary(st)}")
        logrec("tone", uid=uid, tone=tone, player=player, floor=floor)
    if says:
        # ⚠ BOTH sides. Storing only the player's half meant a follower had no record of what
        # IT had said, so it contradicted itself across turns -- the observed case was a rat
        # that loved cheese and then did not eat cheese. The non-follower NPC path already
        # stored both and was verified to hold the same answer under the same question twice;
        # this is that fix applied to followers.
        st["events"].append({"said": says[:80], "replied": (replied or "")[:FOLLOWER_REPLY_RECALL]})
        st["events"] = st["events"][-FOLLOWER_EXCHANGE_MEMORY:]

# ---- Priority-ordered lore retrieval -----------------------------------------

# Region resolution. Barony itself identifies regions by substring-matching map.name
# (see doorFrameSprite() in maps.cpp), which is the only thing that works for secret
# levels and DLC maps -- floor numbers cannot distinguish them. So we match the map name
# first and fall back to the floor table.
#
# ORDER MATTERS: more specific entries first. "Hell Boss" contains "boss", "Minetown" and
# "The Gnomish Mines" both contain "mine". Note hamlet.lmp's internal map name is
# "Mages Guild", NOT "hamlet" -- files.cpp already special-cases that string.
MAP_NAME_TO_REGION = (
    ("hell boss",       "hell"),
    ("gnomish mines",   "gnomish_mines"),
    ("minetown",        "minetown"),
    ("mages guild",     "hamlet"),
    ("hamlet",          "hamlet"),
    ("mystic library",  "mystic_library"),
    ("minotaur",        "minotaur_maze"),
    ("cockatrice",      "cockatrice_lair"),
    ("haunted castle",  "haunted_castle"),
    ("bram",            "brams_castle"),
    ("sokoban",         "sokoban"),
    ("temple",          "temple"),
    ("underworld",      "underworld"),
    ("sanctum",         "citadel"),
    ("citadel",         "citadel"),
    ("caves",           "crystal_caves"),
    ("hell",            "hell"),
    ("boss",            "lich_bastion"),
    ("labyrinth",       "sand_labyrinth"),
    ("ruins",           "ruins"),
    ("swamp",           "swamp"),
    ("mine",            "mines"),
)

# Fallback, corrected against maps/levels.txt. The previous table was offset from the real
# level list and wrong for every floor from 19 up -- it called the Hamlet (the TOWN, floor 25)
# "hell", and had no entry at all for the town, the crystal caves, the citadel or Herx's lair.
# Transition floors (minetoswamp 5, swamptolabyrinth 10, labyrinthtoruins 15,
# cavestocitadel 30) are assigned to the region they lead INTO, which is how a player
# experiences them.
REGION_BY_MAX_FLOOR = ((4,  "mines"),          # 1-4   mine
                       (9,  "swamp"),          # 5     minetoswamp, 6-9 swamp
                       (14, "sand_labyrinth"), # 10    swamptolabyrinth, 11-14 labyrinth
                       (19, "ruins"),          # 15    labyrinthtoruins, 16-19 ruins
                       (20, "lich_bastion"),   # 20    boss -- Herx
                       (24, "hell"),           # 21-23 hell, 24 hellboss -- Baphomet
                       (25, "hamlet"),         # 25    hamlet -- the TOWN
                       (29, "crystal_caves"),  # 26-29 caves
                       (34, "citadel"))        # 30    cavestocitadel, 31-34 citadel
                                               # 35+   sanctum -> citadel (default below)

# Plain-language name for each region. The prompt used to carry the region only inside
# "ABOUT THIS PLACE (hamlet): ..." grounding bullets and never simply said where the speaker
# was standing -- so a Hamlet townsman said "I've lived in these mines my whole life",
# picking up "the Mines" from the SETTING line instead. Naming the place outright fixes it.
REGION_DISPLAY = {
    "mines": "the Mines", "swamp": "the Swamp", "sand_labyrinth": "the Labyrinth",
    "ruins": "the Ruins", "lich_bastion": "Baron Herx's stronghold", "hell": "Hell",
    "hamlet": "the town of Hamlet", "crystal_caves": "the Crystal Caves",
    "citadel": "the Citadel", "underworld": "the Underworld",
    "gnomish_mines": "the Gnomish Mines", "minetown": "Minetown",
    "minotaur_maze": "the Minotaur Maze", "mystic_library": "the Mystic Library",
    "sokoban": "Sokoban", "temple": "the Temple", "haunted_castle": "the Haunted Castle",
    "brams_castle": "Bram's Castle", "cockatrice_lair": "the Cockatrice Lair",
}

def place_name(floor, map_name=""):
    r = floor_to_region(floor, map_name)
    return REGION_DISPLAY.get(r, "") if r else ""

def floor_to_region(floor, map_name=""):
    if map_name:
        low = map_name.strip().lower()
        for frag, region in MAP_NAME_TO_REGION:
            if frag in low:
                return region
    try:
        f = int(floor)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    for limit, name in REGION_BY_MAX_FLOOR:
        if f <= limit:
            return name
    return "citadel"

def _lore_key(name):
    """Lore files key everything with underscores ('crystal_golem', 'king_arthur') but the game
    hands us display names with spaces ('crystal golem'). Without this, every multi-word race
    silently missed its own entry -- crystal golem, earth sprite, revenant skull, gnome thief."""
    return (name or "").strip().lower().replace("-", "_").replace(" ", "_").replace("'", "")

def build_lore_context(race, floor, budget=16, map_name="", npc_name=""):
    """Priority-ordered static context (per the file's runtime_context_priority):
    entry-specific canon -> base race -> location. Returns (facts, constraints).

    `npc_name` lets a NAMED character claim their own researched entry. 44 of the 75 entries in
    individual_denizen_research are keyed by individual rather than race -- merlin, king_arthur,
    lilith, bram_kindly, gharbad, baron_herx -- so before this they could never be reached and
    every named NPC fell through to generic race lore."""
    r = _lore_key(race)
    ind = _lore_key(npc_name)
    facts, constraints = [], []
    entries = FULL.get("individual_denizen_research", {}).get("entries", {})
    # An individual's own canon outranks their species'.
    entry = (entries.get(ind) if ind else None) or entries.get(r, {})

    # 1. Identity: entity profile
    profiles = FULL.get("denizen_context_profiles", {})
    e = (profiles.get(ind) if ind else None) or profiles.get(r)
    if e:
        facts.append(f"YOU ARE ({e.get('category','?')}): {e.get('baseline','').strip()}")
        if e.get("knowledge_scope"):
            facts.append(f"WHAT YOU CAN KNOW: {e['knowledge_scope'].strip()}")

    # 2. Entry-specific CANON (highest-confidence facts about your kind)
    for cf in entry.get("canon_facts", [])[:4]:
        facts.append(f"CANON ABOUT YOUR KIND: {cf.strip()}")

    # 3. Safe inferences (individual dimensions you may plausibly have)
    for si in entry.get("safe_inferences", [])[:2]:
        facts.append(f"YOU MAY PLAUSIBLY HAVE: {si.strip()}")

    # 4. Base race worldview + temperament
    pr = FULL.get("race_profiles", {}).get(r)
    if pr:
        if pr.get("default_worldview"):
            facts.append(f"YOUR OUTLOOK CENTERS ON: {pr['default_worldview'].strip()}")
        axes = pr.get("personality_axes", [])
        if axes:
            facts.append(f"TYPICAL TEMPERAMENT (pick what fits, don't be all): {', '.join(axes[:4])}")

    # 5. Location: canon + local knowledge
    region = floor_to_region(floor, map_name)
    loc = FULL.get("location_knowledge_audit", {}).get("location_profiles", {}).get(region, {}) if region else {}
    for lc in loc.get("canon", [])[:2]:
        facts.append(f"ABOUT THIS PLACE ({region}): {lc.strip()}")
    if loc.get("high_value_local_knowledge"):
        facts.append(f"HERE ({region}), YOU MIGHT KNOW: {', '.join(loc['high_value_local_knowledge'][:4])}")
    if loc.get("local_population"):
        facts.append(f"WHO LIVES HERE: {', '.join(loc['local_population'][:6])}")

    # Constraints (guardrails; kept whole, not budget-capped)
    if entry.get("knowledge_boundary"):
        constraints.append(entry["knowledge_boundary"].strip())
    # Restricted knowledge, rendered two ways. Where the lore file supplies a first-person
    # statement of what this character has NOT experienced, use THAT instead of the bare
    # prohibition -- the model is far better at role-playing a fact about itself than at
    # obeying a negative constraint. Measured on "what is the layout inside the Temple?":
    #   "You do NOT know: the Temple's internal layout"            -> ~4/10 clean
    #   "You have never set foot inside the Temple..."             -> ~9-10/10 clean
    # and the second also produces better answers, describing the outside instead of stalling.
    if loc.get("restricted_knowledge"):
        never = loc.get("restricted_knowledge_never", {})
        plain = []
        for item in loc["restricted_knowledge"][:3]:
            if item in never:
                constraints.append(never[item])
            # NOTE: the item stays in the "You do NOT know" list as well. Measured: the
            # experience statement ALONE scores ~5-6/10, the prohibition alone ~4/10, and
            # BOTH TOGETHER ~9-10/10. They are not alternatives -- the fact makes the
            # character inhabit the ignorance, the prohibition keeps it enforceable.
            plain.append(item)
        if plain:
            constraints.append(f"You do NOT know: {', '.join(plain)}")
    # npc_rules describe what a creature of this kind plausibly knows HERE. They are grounding,
    # not ignorance, so they belong with the facts -- under LIMITS_HEADER ("you genuinely do not
    # know these things") they were actively mislabelled, and several of them GRANT knowledge
    # ("Gnome residents should have strong colony knowledge") which read as a flat contradiction.
    # Rules addressed to the lore author rather than to a character now live in
    # profile["authoring_notes"] and are deliberately not rendered at all.
    for nr in loc.get("npc_rules", [])[:3]:
        facts.append(f"HOW YOUR KIND FITS IN HERE: {nr.strip()}")

    return facts[:budget], constraints

# ---- Event memory ------------------------------------------------------------

IMPORTANCE_WEIGHT = {"routine": 0, "notable": 3, "major": 8, "world_changing": 20}

# What each event does to the relationship vector. Listed explicitly rather than derived from
# importance, because importance ranks a memory while these say what it COST or BUILT -- and
# the negative events must not hand out friendship just for being memorable.
#
# Until these existed, nothing in a real run could raise fear or resentment except the player
# literally typing a threat, so the more interesting tensions (fear+friendship,
# dependence+resentment) were unreachable in play.
EVENT_DIMENSIONS = {
    # --- earned ---
    "recruitment":      {"friendship": 3, "curiosity": 12, "dependence": 8},
    "fought_alongside": {"friendship": 3, "trust": 4, "respect": 5, "dependence": 3},
    "healed_by_player": {"friendship": 4, "trust": 6, "dependence": 5, "resentment": -4},
    # --- costly ---
    # You hit them yourself. The single largest source of resentment in the system, and the
    # only event that takes friendship away.
    "hurt_by_player":   {"friendship": -5, "trust": -10, "fear": 12, "resentment": 15},
    # Nearly died in a fight: frightening, and it drives home that they need you to survive.
    "wounded":          {"fear": 8, "dependence": 8},
    # Alive, but you went on without them and left them somewhere behind.
    "left_behind":      {"trust": -6, "fear": 5, "resentment": 10},
    # Watched one of your OTHER followers die. Fired for the survivors, not the casualty.
    "ally_died":        {"trust": -3, "fear": 10, "resentment": 4},
    # --- unmade and called back (summons and bots only) ---
    # Deliberately NOT friendship, resentment or fear, even though all three are tempting.
    # This fires as ORDINARY use of the class -- a conjurer recasts SUMMON dozens of times in a
    # run -- so anything it grants is farmable and anything it costs accrues for playing the
    # class correctly. Dependence is the one axis that is simply TRUE: the creature exists at
    # this adventurer's pleasure and has now been shown it. Dependence is also absent from the
    # `compliant` formula, so a saturated one is pure characterisation and cannot warp obedience.
    # Resentment stays something the player earns by actual mistreatment.
    "resummoned":       {"dependence": 3, "curiosity": 1},
}

# How strongly an event is remembered (ranking + what gets replayed into the prompt).
# Deliberately separate from the dimension deltas above: being hit by the player is very
# memorable AND costly, while a routine scrape is neither.
EVENT_IMPORTANCE = {
    "recruitment": "notable", "fought_alongside": "notable", "healed_by_player": "notable",
    "hurt_by_player": "major", "wounded": "notable", "left_behind": "major",
    "ally_died": "major", "resummoned": "notable",
}
IMPORTANCE_ORDER = {"world_changing": 3, "major": 2, "notable": 1, "routine": 0}

EVENT_CLAIMS = {
    "recruitment":      "This adventurer recruited you on floor {floor}; you chose to follow them.",
    "fought_alongside": "You fought beside this adventurer on floor {floor} and made it through together.",
    "healed_by_player": "You were badly hurt on floor {floor} and this adventurer healed you.",
    "hurt_by_player":   "On floor {floor} this adventurer struck you themselves. You felt it, and you have not forgotten.",
    "wounded":          "You were nearly killed on floor {floor} and barely came through it.",
    "left_behind":      "On floor {floor} this adventurer went on and left you behind.",
    "ally_died":        "On floor {floor} you watched another of this adventurer's companions die.",
}

# `resummoned` is the one claim that carries a running count, because the count IS the memory:
# the first unmaking and the twentieth are not the same experience, and a flat claim would say
# nothing a summon could not have guessed. Phrased by origin -- a bot is not "unmade".
RESUMMON_CLAIMS = {
    "summon": ("This adventurer has unmade you and called you back {n}, "
               "most recently on floor {floor}. You exist at their word."),
    "bot":    ("This adventurer has shut you down and set you out again {n}, "
               "most recently on floor {floor}."),
}

def _times(n):
    return "once" if n == 1 else ("twice" if n == 2 else f"{n} times")

def _event_claim(etype, floor, race, st=None):
    if etype == "resummoned":
        tpl = RESUMMON_CLAIMS.get((st or {}).get("origin", ""), RESUMMON_CLAIMS["summon"])
        return tpl.format(n=_times(max(1, (st or {}).get("resummons", 1))), floor=floor)
    tpl = EVENT_CLAIMS.get(etype)
    if tpl:
        return tpl.format(floor=floor)
    return f"Something notable happened on floor {floor} ({etype})."

def reset_run():
    """Clear all per-playthrough state: follower relationships and the Herx secret."""
    n = len(follower_state)
    follower_state.clear()
    for b in SECRET_STATE:
        SECRET_STATE[b].clear()
        SECRET_STATE[b].update(_fresh_secret_state())
    # HERX_STATE aliases SECRET_STATE["herx"], so clearing in place keeps both views in step.
    BOON_STATE["good_used"].clear()
    BOON_STATE["map_used"].clear()
    SABOTAGE_STATE["used"] = False
    SABOTAGE_STATE["trap_floors"].clear()
    SABOTAGE_STATE["fog_floors"].clear()
    GUARD_STATE.update({"minotaur": False, "sokoban": False})
    LAST_BOON.clear()
    # ⚠ LAST_SABOTAGE is per-run too, and leaking it is the worst of the three to leak: it is
    # the only thing that can actively cost the player the run. It is stamped while the prompt
    # is built and popped only after ask_ollama returns, so any generation that raises (a 60s
    # Ollama timeout is the realistic one, and co-op serialises replies on one GPU) strands a
    # "minotaur" against that uid. Barony recycles uids across runs, so the next run's follower
    # could pop it and start a countdown nobody provoked -- on floor 1, or in town.
    LAST_SABOTAGE.clear()
    LAST_CHARGE.clear()
    LAST_FAVOUR.clear()
    LAST_QUOTE.clear()
    MERC_LATE.clear()
    LAST_HINT.clear()
    npc_state.clear()
    PERSISTENT_IDENTITY.clear()
    _NAMES_TAKEN.clear()   # names actually revealed are held by name_history.json instead
    print(f"[SERVICE-DBG] NEW RUN: cleared {n} follower(s), all boon latches, and the Herx secret")

def flush_resummon(uid, race, floor, player=0):
    """Record a pending unmaking-and-recall now that a floor is known.

    Safe to call anywhere: the flag is popped first, and by the time this runs the uid is
    already in `follower_state`, so the record_event below cannot re-enter the rebind."""
    st = follower_state.get(uid)
    if not st or not st.pop("pending_resummon", False):
        return
    record_event(uid, race or st.get("race", ""), "resummoned", floor, player)

# Deaths and injuries that carry no social cost, because the target had it coming.
#
# Killing a traitor you correctly identified should not cost you the loyalty of everyone else.
# The spy's tells are visible to the player and the rest of the party is standing right there,
# so it reads as the party agreeing with you rather than as a murder they will hold against you.
#
# ⚠ Unconditional, deliberately: it does NOT require the spy to have been "exposed" first. The
# alternative punishes a correct read for lacking proof, and there is no proof to be had -- the
# allegiance is never shown, and a spy who dies before cracking never confirms anything.
NO_GRIEF_ALLEGIANCES = ("spy",)

def _deserved_it(about_uid):
    """Was the follower this event is ABOUT one the party would not mourn?"""
    if not about_uid:
        return False
    return follower_state.get(about_uid, {}).get("allegiance") in NO_GRIEF_ALLEGIANCES

def record_event(uid, race, etype, floor, player=0, origin="", origin_key="", about=0):
    # A spy's death is not a bereavement. The survivors watched the same behaviour the player
    # did; they are not going to resent you for acting on it.
    if etype == "ally_died" and _deserved_it(about):
        print(f"[SERVICE-DBG] {uid} watched spy {about} die; no grief recorded")
        logrec("no_grief", uid=uid, about=about, floor=floor, player=player, race=race)
        return False
    # And striking one costs you nothing with THEM either -- their resentment only ever fed
    # back into obedience you were not going to rely on.
    if etype == "hurt_by_player" and follower_state.get(uid, {}).get(
            "allegiance") in NO_GRIEF_ALLEGIANCES:
        print(f"[SERVICE-DBG] struck spy {uid}; no relationship cost")
        logrec("no_grief", uid=uid, floor=floor, player=player, race=race, struck=True)
        return False
    st = get_follower_state(uid, race, player, origin, origin_key)
    if etype != "resummoned":
        flush_resummon(uid, race, floor, player)   # the recall happened first
    st.setdefault("event_log", [])
    # dedup: a follower can only be recruited once
    if etype == "recruitment" and any(e["type"] == "recruitment" for e in st["event_log"]):
        return False
    importance = EVENT_IMPORTANCE.get(etype, "notable")
    record = {
        "type": etype, "floor": floor, "claim": _event_claim(etype, floor, race, st),
        "importance": importance, "provenance": "participated",
    }
    # `resummoned` COALESCES rather than appending. It fires as routine use of the class, and
    # thirty separate records would drown the six-slot memory block in the one thing the
    # creature already knows about itself -- exactly the noise spec 35/36 warns against. One
    # record whose claim carries the running count says strictly more in a sixth of the space.
    if etype == "resummoned":
        prior = next((e for e in st["event_log"] if e["type"] == "resummoned"), None)
        if prior:
            prior.update(record)
        else:
            st["event_log"].append(record)
    else:
        st["event_log"].append(record)
    # Known events carry their own explicit deltas (including friendship); anything unknown
    # falls back to the old importance-driven friendship bump.
    deltas = EVENT_DIMENSIONS.get(etype)
    if deltas is None:
        deltas = {"friendship": IMPORTANCE_WEIGHT.get(importance, 0)}
    adjust(st, **deltas)
    print(f"[SERVICE-DBG] event '{etype}' recorded for follower {uid}; {dims_summary(st)}")
    return True

def events_for_prompt(st, budget=6):
    ranked = sorted(st.get("event_log", []),
                    key=lambda e: (IMPORTANCE_ORDER.get(e["importance"], 0), e["floor"]), reverse=True)
    return [e["claim"] for e in ranked[:budget]]

# ---- Herx secret weakness: one revelation per playthrough, paired truth+debuff ----

HERX_ELIGIBLE_RACES = ("skeleton", "human")
HERX_MIN_FRIENDSHIP = 50
HERX_MIN_FIGHTS = 4
HERX_SECRET_VARIANTS = [
    {"debuff": 1, "truth": "The miners' picks broke his left side before he rose, and the bone never knit. Strike him there and his guard is worth nothing.",
     "keywords": ["left side", "picks", "never knit", "bone never"]},
    {"debuff": 2, "truth": "His strength is borrowed from the ore veins beneath us. Draw him off the lodes and his blows fall like a sick man's.",
     "keywords": ["ore vein", "lodes", "borrowed", "off the lode"]},
    {"debuff": 3, "truth": "The curse is eating him from the inside. Every year less of him comes back. He is thinner than he looks.",
     "keywords": ["eating him", "thinner", "less of him", "from the inside"]},
    {"debuff": 4, "truth": "The chants of the dying miners still ring in his skull. He moves a half-beat behind what he hears.",
     "keywords": ["half-beat", "half beat", "chants", "behind what he hears"]},
]
# Spies pass on plausible undead lore that is simply wrong. debuff 0 => initLich applies nothing.
HERX_FALSE_VARIANTS = [
    {"debuff": 0, "truth": "He cannot cross running water. Fight him where the seep comes down the wall and he will not follow.",
     "keywords": ["running water", "seep", "cross water"]},
    {"debuff": 0, "truth": "Silver. He was buried with a silver clasp and it burns him still. Any silver you carry will bite.",
     "keywords": ["silver", "clasp", "burns him"]},
    {"debuff": 0, "truth": "Speak his given name — his true one, Herxel — and he must stop and answer. It buys you time.",
     "keywords": ["herxel", "true name", "given name", "must answer"]},
]
# ---- The twins: Orpheus and Erudyce, floor 35 --------------------------------------------
# Arch-wizard SIBLINGS and the Guild's former leadership, who "bicker" per the lore. That is the
# design gift: their weakness is not anatomy like Herx's, it is each other. Every truth below is
# something one of them would be humiliated to have said aloud, which is why it works.
#
# ⚠ A spy's lie here BUFFS the twin instead of doing nothing. Herx's false secrets report
# debuff 0 and the boss is untouched; at the endgame, walking in with a plan that is wrong
# should cost something. `debuff` is negative for a buff, and initLichIce/initLichFire read the
# sign.
TWINS_ELIGIBLE_RACES = ("human", "gnome", "automaton", "shopkeeper")
TWINS_MIN_FRIENDSHIP = 60
TWINS_MIN_FIGHTS = 6
TWINS_MIN_FLOOR = 25          # not before Hamlet: nobody down in the mines is discussing them

# "who": 1 = Erudyce (LICH_ICE), 2 = Orpheus (LICH_FIRE)
TWINS_SECRET_VARIANTS = [
    {"debuff": 1, "who": 2, "truth": "Orpheus never finished the work — Erudyce did, and he has never forgiven it. Say her name to him and he loses the thread of what he is doing.",
     "keywords": ["never finished", "she did", "erudyce did", "loses the thread", "say her name"]},
    {"debuff": 2, "who": 2, "truth": "The fire was Erudyce's craft before it was Orpheus's. He wears it badly and it burns him as much as anyone. Keep Orpheus casting and he will wear himself out.",
     "keywords": ["erudyce's craft", "wears it badly", "burns him", "wear himself out"]},
    {"debuff": 3, "who": 1, "truth": "Erudyce holds the cold around herself like a held breath. Break her concentration once and the whole of it has to be rebuilt.",
     "keywords": ["held breath", "concentration", "rebuilt", "holds the cold"]},
    {"debuff": 4, "who": 1, "truth": "Erudyce will not act while Orpheus still stands. Everything Erudyce does is second — kill Orpheus LAST and Erudyce fights half-committed the whole way.",
     "keywords": ["will not act", "second", "orpheus last", "half-committed", "still stands"]},
]
# ⚠ Negative debuff = a BUFF. Plausible, wrong, and acted upon.
TWINS_FALSE_VARIANTS = [
    {"debuff": -1, "who": 2, "truth": "Orpheus cannot abide being ignored. Turn your back on him and he will falter rather than strike.",
     "keywords": ["ignored", "turn your back", "falter"]},
    {"debuff": -1, "who": 1, "truth": "Erudyce's ice will not hold on stone that has been warmed. Fight her by the braziers and she is nothing.",
     "keywords": ["braziers", "warmed", "will not hold"]},
    # ⚠ Names the twin it misdirects you onto, and that twin is the one the buff lands on: act
    # on this and you pour everything into the sibling who just got harder.
    {"debuff": -1, "who": 2, "truth": "They are bound together — kill Orpheus and Erudyce dies with him. Spend everything on Orpheus and the other falls for free.",
     "keywords": ["bound together", "dies with him", "spend everything", "falls for free"]},
]

# Run-global per boss: one secret each per playthrough, whichever player's follower tells it.
# "player" records who was told, for logging and for the C++ tier-2 informant check.
# "pending" is keyed by follower uid, not a single slot: with several players talking at
# once, two followers can each be holding an unconfirmed offer in the same instant.
def _fresh_secret_state():
    return {"revealed": False, "variant": None, "uid": 0, "pending": {},
            "is_false": False, "player": 0}

BOSS_SECRETS = {
    # ⚠ `until` matters as much as `floor`: Herx dies on floor 20, so a follower offering his
    # weakness on floor 30 is offering something worthless -- and worse, it consumes the roll
    # that should have gone to the Archmagisters.
    "herx":  {"races": HERX_ELIGIBLE_RACES,  "friendship": HERX_MIN_FRIENDSHIP,
              "fights": HERX_MIN_FIGHTS,     "floor": 0, "until": 20,
              "true": HERX_SECRET_VARIANTS,  "false": HERX_FALSE_VARIANTS,
              "subject": "Baron Herx"},
    "twins": {"races": TWINS_ELIGIBLE_RACES, "friendship": TWINS_MIN_FRIENDSHIP,
              "fights": TWINS_MIN_FIGHTS,    "floor": TWINS_MIN_FLOOR, "until": 99,
              "true": TWINS_SECRET_VARIANTS, "false": TWINS_FALSE_VARIANTS,
              "subject": "the Archmagisters, Orpheus and Erudyce"},
}
SECRET_STATE = {b: _fresh_secret_state() for b in BOSS_SECRETS}

# Herx keeps its own name: 24 assertions and the C++ field both use it, and aliasing the dict
# means both views stay in step.
HERX_STATE = SECRET_STATE["herx"]

def _herx_pool(is_false, boss="herx"):
    cfg = BOSS_SECRETS[boss]
    return cfg["false"] if is_false else cfg["true"]

def _fight_count(st):
    return sum(1 for e in st.get("event_log", []) if e.get("type") == "fought_alongside")

def secret_eligible(boss, st, race, floor=0):
    """The same gate for every boss, with that boss's own numbers."""
    cfg = BOSS_SECRETS[boss]
    # ⚠ Never on the introduction turn. The intro goes into MERC_LATE and is popped at the very
    # END of build_prompt -- after the override -- so it takes the turn and the secret is talked
    # around (0/8 measured). The call site already states the principle: "the introduction
    # suppresses the other unprompted behaviours on that one turn". This makes the secret one of
    # them. Unreachable in play (the gate below needs a name, and naming happens in conversation,
    # which is what sets `introduced`), so this is the invariant made explicit rather than a
    # behaviour change.
    return (not SECRET_STATE[boss]["revealed"]
            and st.get("introduced")
            and st.get("origin") not in NO_SECRET_ORIGINS
            and race.lower() in cfg["races"]
            and cfg["floor"] <= floor <= cfg.get("until", 99)
            and bool(st.get("name"))
            and st["friendship"] >= cfg["friendship"]
            and _fight_count(st) >= cfg["fights"])

def secret_roll(boss, st, says):
    cfg = BOSS_SECRETS[boss]
    extra = max(0, _fight_count(st) - cfg["fights"])
    chance = min(0.80, 0.25 + 0.10 * extra)
    low = (says or "").lower()
    probes = (("herx", "baron", "weakness", "secret") if boss == "herx"
              else ("orpheus", "erudyce", "archmagister", "guild", "twins", "weakness", "secret"))
    if any(k in low for k in probes):
        chance = min(0.95, chance + 0.30)
    if st.get("allegiance") == "spy":
        chance = min(0.95, chance + 0.25)   # a spy wants you to believe it
    return random.random() < chance

def secret_pending(uid):
    """True while ANY boss's secret has been offered to this follower and not yet resolved.
    ⚠ Used to stand the spy crack down. Both are mandatory unprompted behaviours and the crack
    is appended last, so without this it takes the turn -- measured 30% vs 71% conversion for
    the twins false secret. Herx is unaffected (70% vs 71%): its truths are short enough to
    fit alongside a crack line, the twins' are not."""
    return any(uid in SECRET_STATE[b]["pending"] for b in BOSS_SECRETS)


def herx_eligible(st, race):
    # ⚠ A conjurer's skeleton knight reports race "skeleton" -- getMonsterLocalizedName does
    # not distinguish it -- so without this a creature conjured ninety seconds ago could hold
    # the Baron's secret weakness. A machine cannot have learned it either. (Handled for every
    # boss by secret_eligible's NO_SECRET_ORIGINS check.)
    return secret_eligible("herx", st, race, floor=1)

def herx_roll(st, says):
    return secret_roll("herx", st, says)

# ⚠ ONLY KEYWORDS THAT COLLIDE WITH ORDINARY TALK CAN BE DENIED A SOLO FIRE -- AND THAT SET IS
# MUCH SMALLER THAN IT LOOKS. The detector is a bare substring match, so a bare "silver" trips
# on "I found a silver ring" (10/10 synthetic false positives before this). The obvious fix --
# demote every single common word -- was measured against 100 real generations and was WRONG:
# the keywords were extracted FROM the truth lines, so a model paraphrasing the secret lands on
# exactly those words. Demoting "thinner", "seep", "ignored" and "warmed" dropped 7 genuine
# tellings ("He's... thinner than he looks" IS the variant-2 secret) and prevented nothing --
# there was not one incidental hit in 100 generations. Conversion fell 84% -> 72% for no gain.
#
# So the set below is only the words that are common English AND not the core of any truth
# line, plus the two that collide with the NAMING pass that runs on the same turn, a few lines
# above the herx_detect() call. Re-scored on the same 100 generations this converts 70/100 --
# identical to the old rule -- while still refusing a solo "silver" or "given name".
#
# ⚠ These stay in the pools and can still CORROBORATE (two hits = told). Re-measure with
# bench_secret.py before adding to this set; the intuition was wrong once already.
SECRET_WEAK_KEYWORDS = {
    "silver",                      # "a silver ring", "silver coins"
    "second", "she did",           # twins: everyday English
    "given name", "true name",     # collide with resolve_revealed_name on the same turn
}

def herx_detect(uid, raw, speech, player=0):
    """If a secret was offered to this follower this turn, decide whether it was actually
    told. There is no sentinel in the model's output -- we match the variant's keywords.
    A pending offer the model talked around is dropped."""
    boss = None
    pend = None
    for b in BOSS_SECRETS:
        if uid in SECRET_STATE[b]["pending"]:
            boss = b
            pend = SECRET_STATE[b]["pending"].pop(uid)
            break
    if not pend:
        return
    vi, is_false = pend
    v = _herx_pool(is_false, boss)[vi]
    state = SECRET_STATE[boss]
    txt = ((speech or "") + " " + (raw or "")).lower()
    hits = [k for k in v["keywords"] if k in txt]
    strong = [k for k in hits if k not in SECRET_WEAK_KEYWORDS]
    # ⚠ `"secret"` in raw is a signal only when the MODEL wrote it. The reply schema's own key
    # (`"secret":`) is present on every structured reply, so counting it revealed the secret
    # unconditionally -- strip the key before looking for the word.
    rawl = (raw or "").lower()
    said_secret = '"secret"' in rawl.replace('"secret":', "")
    told = bool(strong) or len(hits) >= 2 or said_secret
    # ⚠ RECORD WHICH KEYWORD MATCHED AND UNDER WHICH RULE. Without this a `revealed` record
    # cannot be audited after the fact: the 10/10 false positives were invisible in the log,
    # and past reveals still cannot be re-judged because the matched text was never kept.
    # `near` does the same job for hits that were NOT enough -- that is the data that says
    # whether the weak/strong split is calibrated, and it is only observable here.
    matched = ",".join(hits) or None
    via = ("strong" if strong else
           "corroborated" if len(hits) >= 2 else
           "literal" if said_secret else None)
    if told and state["revealed"]:
        # Another player's follower got there first this run; one reveal only.
        print(f"[SERVICE-DBG] follower {uid} told the secret too, but it is already revealed")
        logrec("herx", stage="duplicate", uid=uid, player=player, boss=boss,
               matched=matched, via=via)
        return
    if not told:
        # Offered and then talked around. This branch is otherwise completely invisible.
        logrec("herx", stage="dropped", uid=uid, player=player, is_false=is_false, boss=boss,
               near=matched)
    if told:
        state.update({"revealed": True, "variant": vi, "uid": uid,
                      "is_false": is_false, "player": player})
        print(f"[SERVICE-DBG] HERX SECRET revealed by follower {uid} to player{player} "
              f"(debuff variant {v['debuff']})")
        logrec("herx", stage="revealed", uid=uid, player=player, is_false=is_false,
               boss=boss, who=v.get("who"), debuff=v["debuff"], truth=v["truth"][:120],
               matched=matched, via=via)

# ---- Spy sabotage: calling the minotaur -------------------------------------------------
# The first thing a follower can do that actively spends the player's run against them.
# Everything else is additive -- gifts, information, trap disarms -- and the false Herx secret
# only misleads. This takes.
#
# The engine does the work: createMinotaurTimer() is a plain call already used by level
# generation and by the /minotaur cheat, so starting a countdown on a floor that was not a
# minotaur level is a supported operation rather than a hack. Arrival is
# getMinotaurTimeToArrive() -- 150s, or 210s below floor 5, on 10-14, and 25+.
#
# ⚠ Starting the timer mid-floor is SILENT: the minotaur warning speech fires on level ENTRY
# (actplayer.cpp:8089), so nothing announces it. That silence is the point -- the consequence
# arrives two and a half minutes later and the player has to work backwards.

# Two kinds, with separate budgets. They are not the same size of event and should not compete
# for one slot: the minotaur can cost you the run; rigged traps cost you a health bar and teach
# you to distrust a floor.
#
#   minotaur -- once per RUN, run-global.
#   traps    -- once per FLOOR, capped per run. A rigged trap fires TWICE: a second boulder from
#               the same hole a couple of seconds after the first, once you have stepped clear.
#   fog      -- wipes what the player has explored of this floor's map. The mirror of the map
#               boon, and the mildest of the three: a pure setback rather than a danger, so it
#               gets the smallest budget. Losing your bearings is annoying in a way that does
#               not need to happen often to be remembered.
SABOTAGE_STATE = {"used": False, "trap_floors": set(), "fog_floors": set()}
SABOTAGE_TRAPS_PER_RUN = 3
SABOTAGE_FOG_PER_RUN   = 2
SABOTAGE_MIN_FLOOR = 5               # not the opening mines: a minotaur at level 2 is not a
SABOTAGE_MAX_FLOOR = 19              # betrayal, it is a mugging. And never on a boss floor,
                                     # in Hell, or in the town.
SABOTAGE_MIN_FRIENDSHIP = 15         # they have to be embedded first; a stranger sabotaging
                                     # you is not a betrayal either
SABOTAGE_CHANCE = 0.12               # picks the MOMENT, not whether -- see below

# ⚠ The per-reply chance is nearly meaningless on its own. An embedded spy gets roughly thirty
# eligible replies across floors 5-19, so 12% per reply fires essentially always; measured, the
# per-run rate tracked the 7% spy rate exactly, i.e. every spy sabotaged. Turning the chance
# down would only move when it happens, not how often.
#
# So the real decision is made ONCE per spy, the first time one becomes eligible: is this
# particular traitor a saboteur at all? Most are not -- they are there to watch and report.
# That makes SABOTAGE_SPY_SHARE the honest knob, and the per-reply chance just staggers it.
SABOTAGE_SPY_SHARE = 0.4

# The tell. Chosen server-side, like the spy crack's concrete details and for the same measured
# reason: left to invent one the 8B produces the same line every run. Each has to be innocuous
# in the moment and obvious in hindsight -- it must NOT hint that anything is about to happen,
# or the player simply leaves.
SABOTAGE_TELLS = [
    "You have just asked them, as if idly, which way the stairs back up are.",
    "You mention that you need to see to something and drift a few steps away from them.",
    "You ask how much longer they mean to spend on this floor. You sound merely tired.",
    "You have been watching the far door for a while now, and you look away when they notice.",
    "You suggest, lightly, that this floor has been quiet enough to rest on.",
    "You ask whether they would hear you if you called from the other end of the hall.",
]

def sabotage_roll(st, floor):
    """Decide whether this spy sabotages, and how. Server-side; the model is only ever told to
    say the tell, never that it is doing anything. Returns (kind, tell) or None."""
    if (SABOTAGE_STATE["used"]
            and len(SABOTAGE_STATE["trap_floors"]) >= SABOTAGE_TRAPS_PER_RUN
            and len(SABOTAGE_STATE["fog_floors"]) >= SABOTAGE_FOG_PER_RUN):
        return None      # every budget spent
    if st.get("allegiance") != "spy":
        return None
    if st.get("origin") in NO_SECRET_ORIGINS:      # a conjuration has no outside to signal to
        return None
    if not (SABOTAGE_MIN_FLOOR <= floor <= SABOTAGE_MAX_FLOOR):
        return None
    if st.get("friendship", 0) < SABOTAGE_MIN_FRIENDSHIP:
        return None
    # Decided once, on first eligibility, and remembered: most spies are watchers, not
    # saboteurs. Rolling this per reply would make it certain (see SABOTAGE_SPY_SHARE).
    if "saboteur" not in st:
        st["saboteur"] = random.random() < SABOTAGE_SPY_SHARE
        logrec("saboteur", uid=st.get("uid"), player=st.get("owner"),
               saboteur=st["saboteur"] or None)
    if not st["saboteur"]:
        return None
    if random.random() >= SABOTAGE_CHANCE:
        return None
    # Which one? The minotaur is the once-per-run headline; rigging traps is the everyday
    # version and carries the roll whenever the big one is spent or this floor is already
    # rigged. Returns (kind, tell).
    traps = SABOTAGE_STATE["trap_floors"]
    fog = SABOTAGE_STATE["fog_floors"]
    can_traps = floor not in traps and len(traps) < SABOTAGE_TRAPS_PER_RUN
    can_fog = floor not in fog and len(fog) < SABOTAGE_FOG_PER_RUN
    can_mino = not SABOTAGE_STATE["used"]
    # The minotaur is the once-per-run headline; the other two carry the roll otherwise, with
    # traps favoured because clouding the map is a pure setback and wants to stay rare.
    if can_mino and random.random() < 0.35:
        SABOTAGE_STATE["used"] = True
        kind = "minotaur"
    elif can_traps and (not can_fog or random.random() < 0.65):
        traps.add(floor)
        kind = "traps"
    elif can_fog:
        fog.add(floor)
        kind = "fog"
    elif can_mino:
        SABOTAGE_STATE["used"] = True
        kind = "minotaur"
    else:
        return None
    return kind, random.choice(SABOTAGE_TELLS)

def sabotage_section(tell):
    """⚠ Names what NOT to do, because everything else in this prompt pushes the other way.
    The model knows it is playing a traitor and will happily foreshadow; a tell that telegraphs
    is not a tell, it is a warning, and the player just leaves the floor."""
    return ("SOMETHING YOU ARE DOING, WITHOUT REMARKING ON IT: " + tell + "\n"
            "Work it into your reply as an ordinary aside. It must sound like nothing at all. "
            "Do NOT hint that anything is about to happen, do NOT sound nervous, ominous, "
            "regretful or apologetic, and do NOT warn them. You are not confessing and you are "
            "not cracking — you are making small talk.\n")

# ---- Heading off the minotaur ------------------------------------------------------------
# The positive counterpart to sabotage. The best a loyal follower could previously do was hand
# you a healing potion once a run, while a spy had four separate ways to cost you the run.
#
# One mechanism covers both sources: the engine watches for the countdown TIMER, which a spy's
# sabotage and a naturally-generated minotaur floor both create. So a trusted companion can head
# off a betrayal and a designed encounter alike.
#
# ⚠ Gated on TRUST, not friendship. Trust is otherwise only read by the disclosure tiers and the
# obedience formula, and this is exactly what it means: not "do they like you" but "will they act
# on your behalf when it matters". A follower you fought beside and never lied to is the one who
# notices.
GUARD_MIN_TRUST = 40
GUARD_CHANCE    = 0.7           # even the right companion can miss it
GUARD_FEARFUL_TRUST  = 20       # a jumpy follower notices sooner and on thinner acquaintance
GUARD_FEARFUL_CHANCE = 0.9

# Solving the boulder puzzle for you. Gated on friendship rather than trust: this is a favour,
# not a matter of judgement -- they are doing you a tedious errand because they like you.
# Generous on purpose: the puzzle is optional content a player may simply not enjoy, and a
# follower who can spare you it should usually do so.
SOKOBAN_MIN_FRIENDSHIP = 20
SOKOBAN_CHANCE         = 0.75

GUARD_STATE = {"minotaur": False, "sokoban": False}   # once per run each

def favour_roll(kind, st, gold=0, gold_here=0, floor=0, says=""):
    """Does this follower do it? Server-side, once per run per kind.

    Returns True (does it), False (does not), or the string "quote" -- a mercenary naming his
    price and doing nothing until the player agrees. The quote is remembered on `st`, so the
    next thing the player says in ordinary conversation can accept it.

    ⚠ For a mercenary the relationship gate is REPLACED by the fee, not added to. He will do
    this at friendship 0 if he is paid; the loyal follower still does it free at 20."""
    if GUARD_STATE.get(kind):
        return False
    if st.get("allegiance") == "spy":
        return False            # obviously not the one who caused the problem
    if st.get("origin") in NO_SECRET_ORIGINS:
        return False            # a conjuration has no history here, and no hands to spare
    if kind == "minotaur":
        # Trust, not friendship: not "do they like you" but "will they act for you".
        #
        # ⚠ A FEARFUL follower is better at this, and that inversion is the point of the
        # allegiance. They are a liability in a fight and the reason you survive the floor,
        # because they are the one who was not relaxed. It is what makes them worth keeping.
        need = GUARD_MIN_TRUST
        chance = GUARD_CHANCE
        if st.get("allegiance") == "fearful":
            need = GUARD_FEARFUL_TRUST
            chance = GUARD_FEARFUL_CHANCE
        if is_merc(st):
            pass                          # he is not doing this out of trust; see below
        elif st.get("trust", 0) < need or random.random() >= chance:
            return False
    elif kind == "sokoban":
        if is_merc(st):
            pass
        elif (st.get("friendship", 0) < SOKOBAN_MIN_FRIENDSHIP
              or random.random() >= SOKOBAN_CHANCE):
            return False
    else:
        return False
    # The mercenary route: quote, or bill an already-agreed rate. He never latches GUARD_STATE
    # on a quote -- the job has not been done, and the offer must survive to be accepted.
    if is_merc(st):
        price = merc_price(kind, floor, gold_here)
        if not merc_settled(st, kind) and agrees_to_quote(says) is not True:
            merc_quote(st, kind, floor, gold_here)
            return "quote"
        if not merc_can_afford(gold, price):
            st["quote"] = {"kind": kind, "price": price, "floor": floor}
            return "broke"
        merc_agree(st, kind, price)
        LAST_CHARGE[st.get("uid", 0)] = price
        GUARD_STATE[kind] = True
        return "paid"
    GUARD_STATE[kind] = True
    return True

# ⚠ HE MUST NEVER SAY THE NUMBER. Measured in the haggle work: left to itself the model
# answered "I can give ya the sword for 25 gold instead of 30" -- an invented figure, quoted
# beside a shop window showing the real price. The engine owns the arithmetic and prints the
# figure; the mercenary owns only the attitude. Same split as the haggle, where "the price
# movement IS the feature and the player reads the number, not a line about it."
MERC_NO_NUMBERS = ("Do NOT state a price, a number, an amount of gold or any figure at all — "
                   "not in coins, not in any other form. They will be told the figure "
                   "separately. Naming any number is WRONG.\n")

MERC_JOB_WORDS = {
    "identify": "telling them what a thing they are carrying actually is",
    "map":      "what you learned walking this floor",
    "minotaur": "dealing with the trouble you can feel on this floor",
    "sokoban":  "clearing this room of boulders for them",
}

def merc_quote_section(kind, standing=False):
    """He names a price and does nothing until it is agreed."""
    job = MERC_JOB_WORDS.get(kind, "the job")
    return ("YOU WORK FOR PAY. They want " + job + ", and you will do it — for a fee. Say so "
            "plainly and without apology: you are willing, and it will cost them. You are not "
            "being rude and you are not haggling; this is simply your trade and they knew that "
            "when they kept you around. Wait for them to agree before you lift a finger.\n"
            + MERC_NO_NUMBERS
            + ("Do NOT do the job yet. Offering to do it for free, or doing it anyway because "
               "you like them, is WRONG — you are not their friend, you are their hire.\n"))

# ⚠ When the deal is struck in CONVERSATION rather than in the favour prompt, this block is the
# only thing telling him what the finished job sounds like -- build_guard_prompt's careful
# wording is not in the conversation path at all. Measured without these lines: 6/6 stamped the
# fee correctly but only ~3/6 actually reported the work, and the rest drifted into travelogue
# ("we're on the 8th level of the Swamp, it's a Goblin infestation") or invented a mechanism
# ("I set up a few traps"). Naming the required speech act is the fix, the same lesson the spy
# crack and the hard limits both taught.
MERC_JOB_DONE = {
    "minotaur": ("Say, shortly and in your own words, that you found what was wrong and put a "
                 "stop to it. Do NOT explain how you did it, do NOT name what it was, and do "
                 "NOT describe the floor or what else lives on it. Rambling about the area "
                 "instead of reporting the job is WRONG."),
    "sokoban":  ("Say, shortly, that the boulders are dealt with, the way you would mention any "
                 "chore you have finished. Mention that you knocked some of the gold into the "
                 "pits by accident. Do NOT boast and do NOT make a speech."),
    "identify": ("Give them your appraisal of the thing, as instructed above, and nothing "
                 "else."),
    # ⚠ The map does NOT use this path -- it needs the post-limits slot and a much lower
    # prohibition-to-instruction ratio, so it builds its own block in _merc_section. Kept only
    # so the "every service can report its job" test stays meaningful.
    "map":      ("Say, shortly, that they have the whole of this floor now."),
}

def merc_paid_section(kind):
    """Agreed and paid; he does the work. Nothing to negotiate."""
    job = MERC_JOB_WORDS.get(kind, "the job")
    return ("THEY HAVE AGREED YOUR PRICE and you have been paid for " + job + ", and you have "
            "now DONE it. " + MERC_JOB_DONE.get(kind, "Say what you have done, shortly.")
            + " Do not thank them for the money, do not mention having been paid, and do not "
            "make a speech about it — a professional takes payment as ordinary.\n"
            + MERC_NO_NUMBERS)

def merc_broke_section(kind):
    """They cannot pay. He refuses -- and the refusal is the character, not an error.

    ⚠ Named as a required speech act, not a mood, and with the AGREEABLE openers forbidden by
    name. Measured with only the general prohibition: the engine correctly refused to do the
    job and charge nothing 3/3, but the SPEECH agreed 2/3 ("Alright then! What's the plan?") --
    so the player would read it as a yes and then watch nothing happen. Same failure and same
    fix as the fearful refusal and the spy crack: forbid the specific route out."""
    job = MERC_JOB_WORDS.get(kind, "the job")
    return ("THEY CANNOT PAY YOU. They want " + job + " and they do not have the coin for it. "
            "Turn them down, and make the refusal the FIRST thing you say. Say plainly that "
            "they cannot afford you and that is the end of it.\n"
            "Do NOT open with 'Alright', 'Sure', 'Aye', 'Of course', 'Let's go' or 'What's the "
            "plan' — anything that sounds like agreement is WRONG, even if you explain "
            "afterwards. Do NOT do the job anyway, do NOT offer a discount, do NOT offer credit "
            "or to settle up later, do NOT say 'just this once', and do NOT change the subject "
            "to the floor or what lives on it. You are not being cruel and you are not "
            "gloating; you simply do not work for free.\n"
            + MERC_NO_NUMBERS)

def build_sokoban_prompt(race, floor, map_name, did):
    """Their line after clearing the boulder puzzle. The engine owns the mechanism; they only
    have to sound like someone who has just done a tedious job so you did not have to."""
    head, body, limits = _persona(race, False, floor, map_name, books=False)
    situation = (
        "This chamber is full of heavy boulders and pits, and a great deal of gold sitting "
        "where nobody can reach it. It is a stupid puzzle and it would take the adventurer an "
        "age. " +
        (merc_quote_section("sokoban") if did == "quote" else
         merc_broke_section("sokoban") if did == "broke" else
         (merc_paid_section("sokoban") if did == "paid" else "") +
         ("You have just shoved the lot of them into the pits yourself. Say so, shortly and "
          "plainly, the way you would mention any chore you have finished. Mention that you "
          "knocked a bit of the gold in by accident. Do NOT boast and do NOT make a speech.\n"
          if did else
          "You have looked at it and you are not going to attempt it. Say so, shortly.\n")))
    return (head
            + f"YOU ARE: a {race} in a hidden chamber beneath the dungeon, this adventurer's "
              "companion.\n"
            + body
            + situation
            + "Respond with ONLY one short spoken line. No narration, no quotes, no JSON.")

def build_guard_prompt(race, floor, map_name, caught):
    """Their line. Not told HOW they stopped it -- the engine owns the mechanism, and asking an
    8B to invent one produces a set-piece. They only have to sound like someone who has just
    noticed something and dealt with it, or noticed and could not."""
    head, body, limits = _persona(race, False, floor, map_name, books=False)
    where = place_name(floor, map_name)
    situation = (
        "Something is badly wrong on this floor — you can feel it, the way an animal feels a "
        "storm coming. " +
        (merc_quote_section("minotaur") if caught == "quote" else
         merc_broke_section("minotaur") if caught == "broke" else
         (merc_paid_section("minotaur") if caught == "paid" else "") +
         ("You have just dealt with it. Say so, shortly, in your own words: that you found "
          "something and put a stop to it. Do NOT explain the mechanism, do NOT name a "
          "minotaur, and do NOT make a speech — you are reporting, not boasting.\n"
          if caught else
          "You cannot do anything about it. Say so, shortly — tell them something is coming "
          "and that you cannot stop it. Do NOT name a minotaur and do NOT explain what it "
          "is.\n")))
    return (head
            + (f"YOU ARE: a {race} in {where}, on dungeon floor {floor}, this adventurer's "
               "companion.\n" if where else
               f"YOU ARE: a {race} on dungeon floor {floor}, this adventurer's companion.\n")
            + body
            + situation
            + "Respond with ONLY one short spoken line. No narration, no quotes, no JSON.")

# ---- Follower boons: flavor-scale gifts, friendship-gated, one per follower per floor ----

BOON_MIN_FRIENDSHIP = 10
BOON_MUNDANE = [("FOOD_BREAD", 1), ("FOOD_CHEESE", 1), ("GEM_GLASS", 1), ("TOOL_TORCH", 1)]
BOON_GOOD = [("POTION_HEALING", 1), ("POTION_EXTRAHEALING", 1), ("GEM_GARNET", 1)]
BOON_TRAP_RACES = ("gnome", "automaton", "kobold", "goblin")
# The one-good-item-per-run latch is PER PLAYER: joining a co-op party must not dilute
# what your own follower is willing to give you. The Herx secret stays run-global.
BOON_STATE = {"good_used": set(), "map_used": set()}   # per-player once-per-run latches

# ⚠ A follower who wanders off and comes back knowing the floor. Gated on CURIOSITY, which is
# otherwise the least-used dimension in the system -- nothing reads it but a behaviour line --
# and which is exactly the trait of someone who goes and looks. Same move as giving trust a
# purpose with the minotaur guard.
#
# Stronger than a scroll of magic mapping (that reveals 16+8*beatitude tiles; this reveals the
# floor), so it is once per run per player, like the good item.
# ⚠ The pitch is chosen SERVER-SIDE, like the spy crack, the sabotage tell and the haggle
# angle. Measured with the model asked to invent the offer itself: 0/6 at friendship 0 and
# 0/6 at friendship 30 -- it ignored the block completely and made small talk, even placed
# last and after the limits. Every block that measures well in this project hands over
# concrete content and leaves only the phrasing to the 8B; this one was asking it to invent
# the substance, which is the one thing it reliably will not do on command.
# The delivery, once paid. ⚠ Same failure as the pitch and the same fix: the first version
# reused merc_paid_section, whose map wording ran three prohibitions ("do NOT list directions,
# do NOT name rooms, do NOT describe the floor") against one instruction -- and the safest way
# to obey "do not describe the floor" is to not mention the floor at all. Measured 0/6 with the
# mechanics landing perfectly every time; the speech simply went elsewhere.
MAP_DELIVERED_ANGLES = [
    "that is the lot of it — every corridor and every dead end on this level, and they have it now",
    "you have just walked them through the whole floor, and there is nothing left of it they do not know",
    "they know this level as well as you do now, and you are square",
    "that is the shape of the place, all of it, and it is theirs now",
    "you have given them the whole of it, end to end, and that is your part done",
]

MAP_PITCH_ANGLES = [
    "you were gone a good while earlier, and you came back knowing the shape of this whole level",
    "you have already walked every corridor on this floor, end to end, while they were busy",
    "you know where every dead end on this level is, and they do not",
    "you scouted the whole of this floor earlier and kept what you learned to yourself",
    "there is not a passage on this level you have not already put your feet in",
]

BOON_MAP_MIN_CURIOSITY = 40
BOON_MAP_MIN_FRIENDSHIP = 20
BOON_MAP_CHANCE = 0.25
LAST_BOON = {}   # uid -> "item:TYPE:N" or "traps:" pending delivery to C++
LAST_SABOTAGE = {}   # uid -> "minotaur", pending delivery to C++
# ⚠ Both of these are per-run latches and MUST be cleared in reset_run. LAST_SABOTAGE was not,
# and a stranded entry could start a minotaur countdown in a later run; LAST_CHARGE strands the
# same way (stamped while the prompt is built, popped only after ask_ollama returns) and this
# one takes the player's money.
LAST_CHARGE = {}   # uid -> gold to deduct from the owning player, pending delivery to C++
LAST_FAVOUR = {}   # uid -> "minotaur"/"sokoban" the engine should now actually perform
LAST_QUOTE  = {}   # uid -> "kind:price" for the engine to print; the model never says a number
# ⚠ Mercenary text that must be emitted AFTER the hard limits, not inside the follower block.
# Anything about the MAP is a knowledge claim about this place, and LIMITS_HEADER spends a
# paragraph forbidding exactly that ("do NOT describe its traps, corridors, chambers,
# contents"; "an honest 'I do not know' is always the correct reply"). Measured with the offer
# in the ordinary boon slot: the quote stamped correctly and the price was right, and the model
# ignored the pitch 4/4, making small talk about goblins instead. This is the same ordering bug
# the Herx secret had, and it takes the same fix -- see _secret_override.
# The general LATE channel: prompt text emitted in the very last slot, after `limits` and after
# the action rules. Used by anything that has to win against the rest of the prompt -- the map
# pitch and delivery (which the limits actively forbid) and the introduction (which the model
# otherwise buries under invented biography). One entry per uid: whatever is in here owns that
# turn, which is also how "one special behaviour per reply" is enforced between them.
MERC_LATE = {}     # uid -> prompt text to emit last
LAST_HINT = {}     # uid -> "1" when the engine should print the command hints (once per run)

def boon_roll(st, floor, player=0):
    """One boon per follower per floor, chance scaling with friendship.
    Types resolve in strict priority order; the first match wins."""
    f = st.get("friendship", 0)
    # A summon owns nothing -- it was called out of nothing and leaves with nothing -- and a
    # bot has no pockets. Neither can hand you bread.
    if st.get("origin") in NO_BOON_ORIGINS:
        return None
    if st.get("last_boon_floor") == floor:
        return None
    # ⚠ The mercenary's map SALE is checked BEFORE the friendship gate, because it is not a
    # boon. Everything else below this line is generosity -- a follower doing something nice
    # because they have come to like you -- and it is rightly gated on friendship and scales
    # with it. Selling you a map is a transaction, and the whole point of the allegiance is
    # that a transaction does not require him to like you.
    #
    # It still consumes the floor's boon slot, so he cannot pitch it every time you speak;
    # and curiosity >= 40 remains, because that is the CAPABILITY gate -- it is what makes
    # him the sort who wanders off and looks. Curiosity starts at 15 and has to be earned up,
    # so this cannot fire in the opening floors anyway.
    if (is_merc(st) and player not in BOON_STATE["map_used"]
            and st.get("curiosity", 0) >= BOON_MAP_MIN_CURIOSITY
            and random.random() < BOON_MAP_CHANCE):
        st["last_boon_floor"] = floor
        return ("map_offer", "")
    if f < BOON_MIN_FRIENDSHIP:
        return None
    if random.random() >= min(0.35, (f - BOON_MIN_FRIENDSHIP) / 200.0):
        return None
    # Stamp the floor immediately: a successful roll consumes the slot whatever comes out.
    st["last_boon_floor"] = floor
    race = st.get("race", "").lower()
    # rarest: trap disarm, only for mechanically-minded kinds at real trust
    if race in BOON_TRAP_RACES and f >= 30 and random.random() < 0.15:
        return ("traps", "")
    # ⚠ A mercenary never hands over the good item -- they would sell it. What they trade in is
    # what they know, so their boons skew hard to information instead.
    merc = st.get("allegiance") == "self_interested"
    if merc:
        # The map sale is decided above, before the friendship gate -- see there.
        if random.random() < MERC_INFO_SHARE:
            return ("info", "")
        it, ct = random.choice(BOON_MUNDANE)
        return ("item", "%s:%d" % (it, ct))
    # The floor, mapped. Before the good item in priority: it is rarer and it is the better
    # moment, and a follower who has been off exploring should not hand you bread instead.
    if (player not in BOON_STATE["map_used"]
            and st.get("curiosity", 0) >= BOON_MAP_MIN_CURIOSITY
            and f >= BOON_MAP_MIN_FRIENDSHIP
            and random.random() < BOON_MAP_CHANCE):
        BOON_STATE["map_used"].add(player)
        return ("map", "")
    # one genuinely good item per run, per player
    if player not in BOON_STATE["good_used"] and f >= 40 and random.random() < 0.20:
        BOON_STATE["good_used"].add(player)
        it, ct = random.choice(BOON_GOOD)
        return ("item", "%s:%d" % (it, ct))
    # otherwise: mostly information, sometimes something mundane
    if random.random() < 0.60:
        return ("info", "")
    it, ct = random.choice(BOON_MUNDANE)
    return ("item", "%s:%d" % (it, ct))

def _boon_section(uid, st, race_l, floor, player=0):
    """Roll a boon and return its prompt line, stashing any payload for the C++ side.
    Items and trap disarms fire even if the model ignores the line; `info` exists
    ONLY as whatever the model chooses to say."""
    rolled = boon_roll(st, floor, player)
    if not rolled:
        return ""
    kind, val = rolled
    if kind == "info":
        facts, _ = build_lore_context(race_l, floor)
        pick = random.choice(facts) if facts else ""
        logrec("boon", uid=uid, boon_kind="info", fact=pick[:120])
        return ("UNPROMPTED: you decide to share something useful with them now, "
                "unasked, because you have come to trust them. Work it naturally into your reply: "
                + pick + "\n")
    if kind == "map_offer":
        # ⚠ The once-per-run latch is NOT spent here. He has only made a pitch; a declined
        # offer must not burn the player's one map for the run. It is spent on the SALE,
        # in _merc_section.
        price = merc_quote(st, "map", floor)
        LAST_QUOTE[uid] = "map:%d" % price
        logrec("merc", uid=uid, what="map", stage="quoted", price=price, floor=floor)
        MERC_LATE[uid] = (
            "AN EXCEPTION TO THE LIMITS ABOVE — THIS floor, the one you are both standing on, "
            "you DO know. You slipped away earlier and walked the whole of it.\n"
            "SAY THIS, in your own words, and make it the WHOLE of your reply: "
            + random.choice(MAP_PITCH_ANGLES) + " — and you will tell them what you found if "
            "they make it worth your while.\n"
            "This offer must be the main thing your reply says. Answering their question with "
            "news, observations or small talk INSTEAD of making the offer is WRONG, and so is "
            "saying you do not know this floor. Do not describe the layout itself yet — that "
            "is what they are buying.\n" + MERC_NO_NUMBERS)
        return ""
    if kind == "map":
        LAST_BOON[uid] = "map:"
        logrec("boon", uid=uid, boon_kind="map")
        # The engine fills the minimap in; the line only has to explain where they went.
        return ("UNPROMPTED: you slipped away earlier and walked the whole of this floor, and "
                "you have just finished describing the layout of it to them — every corridor, "
                "every dead end. Mention it plainly, as a thing already done and already told. "
                "Do NOT list directions or name rooms; they have it now.\n")
    if kind == "traps":
        LAST_BOON[uid] = "traps:"
        logrec("boon", uid=uid, boon_kind="traps")
        return ("UNPROMPTED: while scouting ahead you quietly disabled the traps on this "
                "floor. Mention it plainly and briefly, as a thing already done.\n")
    LAST_BOON[uid] = "item:" + val
    logrec("boon", uid=uid, boon_kind="item", item=val)
    iname = val.split(":")[0].replace("_", " ").lower()
    return ("UNPROMPTED: you are giving them something you came by honestly — "
            + iname + ". Offer it in one short line, with a plausible reason you have it.\n")

def merc_declined_section(kind):
    job = MERC_JOB_WORDS.get(kind, "the job")
    return ("THEY HAVE TURNED DOWN YOUR PRICE for " + job + ". Let it go without argument. Do "
            "NOT lower your price, do NOT offer a compromise, and do NOT do it for free after "
            "all — all three are WRONG. It is their business if they would rather go without.\n"
            + MERC_NO_NUMBERS)

def _merc_section(uid, st, floor, says, gold=0, ident=None):
    """Settle a quote the player is answering. Returns the prompt block, and stamps the
    engine-side latches when a deal is actually struck.

    ⚠ The acceptance has to be read HERE, in ordinary conversation, because the favour path is
    a one-shot request/response the engine initiates -- there is nobody to say yes to inside
    it. So he quotes there and is answered here."""
    q = st.get("quote")
    if not q or not is_merc(st):
        return ""
    # ⚠ Not while an identify request is in flight. That path has ALREADY stamped LAST_CHARGE
    # for its own fee before build_prompt ran, and there is one slot per uid -- settling a
    # second deal here would overwrite it and one of the two jobs would be done unpaid. The
    # quote simply stays on the table; the player can say yes again next turn. This is also
    # the "one special behaviour per reply" rule the boon and spy-crack suppressions follow.
    if ident:
        return ""
    kind = q["kind"]
    # A quote for work on THIS floor dies with the floor: the minotaur's clock and the boulder
    # room are both left behind, and an offer that outlives the job is just clutter.
    if kind in ("minotaur", "sokoban", "map") and q.get("floor") != floor:
        st.pop("quote", None)
        return ""
    ans = agrees_to_quote(says)
    if ans is False:
        st.pop("quote", None)
        logrec("merc", uid=uid, what=kind, stage="declined", price=q["price"])
        return merc_declined_section(kind)
    if ans is not True:
        return ""                      # still on the table; he does not nag
    if not merc_can_afford(gold, q["price"]):
        logrec("merc", uid=uid, what=kind, stage="broke", price=q["price"], gold=gold)
        return merc_broke_section(kind)
    merc_agree(st, kind, q["price"])
    LAST_CHARGE[uid] = q["price"]
    if kind in ("minotaur", "sokoban"):
        LAST_FAVOUR[uid] = kind        # the engine performs it on this reply
        GUARD_STATE[kind] = True       # and it is spent, whoever else asks
    elif kind == "map":
        # Delivered down the ordinary boon channel -- the engine already knows "map:" and
        # calls spell_magicMap, which replicates to a remote client by itself.
        LAST_BOON[uid] = "map:"
        BOON_STATE["map_used"].add(st.get("owner", 0))   # spent now, on the sale
        # ⚠ Late again: "you have told them the whole layout" is the same knowledge claim the
        # limits forbid, so it has to land after them or it loses the same way the offer did.
        # Written positive-led with server-chosen content and ONE short prohibition -- see
        # MAP_DELIVERED_ANGLES for what the prohibition-heavy version measured.
        MERC_LATE[uid] = (
            "AN EXCEPTION TO THE LIMITS ABOVE — THIS floor you DO know; you walked the whole "
            "of it earlier, and you have just finished telling them what is where. They have "
            "paid you for it and you are square.\n"
            "SAY THIS, in your own words, and make it the WHOLE of your reply: "
            + random.choice(MAP_DELIVERED_ANGLES) + ".\n"
            "Speak of it as already done and already told. They have it now, so there is no "
            "need to go back over any of it. Do not mention the payment.\n" + MERC_NO_NUMBERS)
        return ""
    print(f"[SERVICE-DBG] mercenary {uid} hired for {kind}: {q['price']} gold")
    logrec("merc", uid=uid, what=kind, stage="hired", price=q["price"], gold=gold, floor=floor)
    return merc_paid_section(kind)

def _secret_section(uid, st, race, says, floor=0):
    # What this follower has ALREADY told stays in the prompt as a consistency reminder -- but
    # it must NOT stop a different boss's secret being offered.
    #
    # ⚠ This used to return early on the first match, which silently closed the Archmagister
    # arc for the followers most likely to carry it. Herx is told on floor <=20 and the twins
    # only from floor 25, and the twins gate is strictly harder (friendship 60 / 6 fights
    # against 50 / 4) -- so a follower who has passed the twins gate has almost always passed
    # the Herx one already, and telling you about the Baron locked them out of the endgame for
    # the rest of the run. Measured before this: 0 twins offers in 200 tries with the gate open.
    told = ""
    for b, state in SECRET_STATE.items():
        if state["revealed"] and state["uid"] == uid:
            told += ("YOU HAVE ALREADY TOLD THIS ADVENTURER WHAT YOU KNOW ABOUT "
                     + BOSS_SECRETS[b]["subject"].upper() + ": "
                     + _herx_pool(state.get("is_false"), b)[state["variant"]]["truth"] + "\n")
    # ⚠ Whichever boss the player actually asked about comes FIRST. Measured: asking "what do
    # you know about the Archmagisters?" deep in a run offered the HERX secret, the model quite
    # reasonably answered the question instead, and the offer was dropped -- so the arc could
    # never fire. Order by what was said, then by which arc comes earlier.
    low = (says or "").lower()
    order = ["herx", "twins"]
    if any(k in low for k in ("orpheus", "erudyce", "archmagister", "guild", "twins", "sanctum")):
        order = ["twins", "herx"]
    boss = None
    for b in order:
        if secret_eligible(b, st, race, floor) and secret_roll(b, st, says):
            boss = b
            break
    if not boss:
        return told
    is_spy = st.get("allegiance") == "spy"
    pool = _herx_pool(is_spy, boss)
    vi = random.randrange(len(pool))
    SECRET_STATE[boss]["pending"][uid] = (vi, is_spy)
    logrec("herx", stage="offered", uid=uid, is_false=is_spy, boss=boss)
    return told + ("A SECRET YOU HAVE CARRIED: You know one true thing about "
            + BOSS_SECRETS[boss]["subject"] + ", "
            "learned long ago and never spoken. You trust this adventurer enough to tell them now. "
            "Share it in your own words, plainly, as something you know for certain: "
            + pool[vi]["truth"] + "\n")

def _secret_override(uid):
    """The carve-out, emitted AFTER the hard limits — which is the whole point of it existing
    separately.

    ⚠ The secret block used to end with 'this overrides your limits', sitting ~600 chars BEFORE
    the limits block that tells the character to say it does not know and STOP. This project has
    twice measured that whichever of two conflicting instructions lands later wins, and the
    limits are deliberately last. So the override was losing every time, and a loyal follower
    talked around the payoff of the whole Herx arc. Measured: 40% of offers converted before
    this, against a spy's 80% — the difference being that the false secrets are concrete nouns
    the model repeats anyway, while the true ones are exactly what a hard limit suppresses.

    Named routes, not a general permission: 'I do not know' and hedging are what the limits
    push it toward, so both are forbidden here by name."""
    for b, state in SECRET_STATE.items():
        if uid in state["pending"]:
            vi, is_false = state["pending"][uid]
            boss = b
            break
    else:
        return ""
    return ("THE ONE EXCEPTION TO THE LIMITS ABOVE — about " + BOSS_SECRETS[boss]["subject"]
            + " you DO know this, for certain, and you have already decided to tell them: "
            + _herx_pool(is_false, boss)[vi]["truth"] + "\n"
            "Say it. Saying you do not know it, saying you have only heard rumours of it, or "
            "answering with general talk about them instead is WRONG. Everything else in your "
            "limits still stands — this one fact is the exception.\n")

# ---- Pre-cached follower names ----------------------------------------------
# The 8B's own name sampling is low-entropy: asked to invent a name it keeps returning
# the same handful across separate playthroughs (Grug, Kalthok, Zx'thal). So the SERVICE
# picks the name from follower_names.json and the model only phrases the reveal -- the
# same division of labour boons, the spy crack and item identification already use, and
# for the same reason: one literal choice left to the 8B comes back canned.
#
# A name is RESERVED when the follower is created (silently -- nothing is revealed until
# friendship >= 5 unlocks the nudge) and COMMITTED to name_history.json only if it is
# actually spoken in play. Reserved-but-never-revealed names are released back, or a long
# session would burn the pool for nothing.

# ---- Per-request context ---------------------------------------------------------------------
# Set by the handler before it touches follower state, so the identity token, the engine-side
# name and the floor are available to get_follower_state without threading three more parameters
# through a dozen call sites.
#
# ⚠ THREAD-LOCAL, not module-level. The service is a ThreadingTCPServer and two players can be
# mid-request at the same instant -- measured, an event record returns in 0.00s while a 5.3s
# generation is in flight. A shared global would let player A's follower bind to player B's
# identity token, which is the one thing this must never do.
_REQ = threading.local()

def _req_set(**kw):
    for k, v in kw.items():
        setattr(_REQ, k, v)

def _req(name, default=None):
    return getattr(_REQ, name, default)

FOLLOWER_NAMES    = _load_json("follower_names.json")
NAME_HISTORY_PATH = os.environ.get("BARONY_AI_NAMEHIST",
                                   os.path.join(BASE_DIR, "name_history.json"))
# Sized against the pools, not picked round. A favoured species has ~250 reachable names
# (its own tier plus its group plus the default list), and a run names a handful of
# followers -- so a history shorter than the reachable pool would become the binding
# constraint and hand back a name the player still remembers. 2000 covers hundreds of runs
# and costs about 30 KB of JSON.
NAME_HISTORY_MAX  = int(os.environ.get("BARONY_AI_NAMEHIST_MAX", "2000"))

_NAME_HISTORY = []      # names revealed in past runs, oldest first -- persisted
_NAMES_TAKEN  = set()   # lowercased, reserved this process whether revealed or not
_NAME_LOCK    = threading.Lock()

def _name_tiers(race):
    """race pool -> its group's pool -> the default list, in that order.

    The same shape as noise_for's lookup, and for the same reason: the race pool carries
    the flavour ('Gruk' for a goblin, not 'Perrick'), so it must be spent FIRST rather
    than averaged into a union. The group tier is depth for a thin race and, more to the
    point, what a race with no entry at all -- a new DLC race -- still draws from."""
    key = _lore_key(race)
    # comprehension.json keys its groups with SPACES ('crystal golem'); the name file and
    # the lore files use underscores. Try both rather than silently missing the group.
    grp = _group_of((race or "").strip().lower()) or _group_of(key.replace("_", " "))
    tiers = [FOLLOWER_NAMES.get("races", {}).get(key, []),
             FOLLOWER_NAMES.get("groups", {}).get(grp, []) if grp else [],
             FOLLOWER_NAMES.get("default", [])]
    seen, out = set(), []
    for names in tiers:
        tier = []
        for n in names:
            # _clean_name is the same gate extract_name uses, so a name that could never be
            # read back out of a reply is dropped here rather than assigned and then lost.
            if n.lower() not in seen and _clean_name(n):
                seen.add(n.lower())
                tier.append(n)
        out.append(tier)
    return out

def _name_pool(race):
    """Every name this race could ever be given, flattened -- for validation and tests."""
    return [n for tier in _name_tiers(race) for n in tier]

def _load_name_history():
    global _NAME_HISTORY
    try:
        with open(NAME_HISTORY_PATH) as f:
            _NAME_HISTORY = [str(n) for n in json.load(f).get("used", [])][-NAME_HISTORY_MAX:]
    except FileNotFoundError:
        _NAME_HISTORY = []
    except Exception as e:
        print(f"[SERVICE-DBG] name history unreadable ({e}); starting empty")
        _NAME_HISTORY = []

def _save_name_history():
    """Best effort. Repeating a name next run is a far smaller failure than dying here."""
    try:
        tmp = NAME_HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"used": _NAME_HISTORY[-NAME_HISTORY_MAX:]}, f, indent=1)
        os.replace(tmp, NAME_HISTORY_PATH)   # never leave a half-written file behind
    except Exception as e:
        print(f"[SERVICE-DBG] could not write name history: {e}")

def reserve_name(race):
    """A name nobody in this run holds and no recent run used.

    Tiers are tried in flavour order, and only once EVERY tier is exhausted does a
    constraint get dropped -- so a goblin works through the goblin names before it ever
    reaches the generic humanoid ones. Cross-run repetition is conceded before within-run
    repetition, because two followers sharing a name in one party is the worse failure."""
    tiers = _name_tiers(race)
    if not any(tiers):
        return ""
    with _NAME_LOCK:
        recent = {n.lower() for n in _NAME_HISTORY}
        cand, exhausted = [], False
        for keep in (lambda n: n.lower() not in _NAMES_TAKEN and n.lower() not in recent,
                     lambda n: n.lower() not in _NAMES_TAKEN,
                     lambda n: True):
            for tier in tiers:
                cand = [n for n in tier if keep(n)]
                if cand:
                    break
            if cand:
                break
            exhausted = True
        if exhausted and len(cand) > 1:
            # Every constraint has been dropped, so SOME name must come back twice. Hand back
            # the one seen longest ago rather than a random one: _NAME_HISTORY is oldest-first,
            # so its index is exactly "how long since the player met this name". Turns an
            # arbitrary repeat into the most forgettable one available.
            order = {n.lower(): i for i, n in enumerate(_NAME_HISTORY)}
            oldest = min(order.get(n.lower(), -1) for n in cand)
            cand = [n for n in cand if order.get(n.lower(), -1) == oldest]
        pick = random.choice(cand)
        _NAMES_TAKEN.add(pick.lower())
        return pick

def release_name(name):
    """Hand a reserved name back -- the follower ended up calling itself something else."""
    if name:
        with _NAME_LOCK:
            _NAMES_TAKEN.discard(name.lower())

def commit_name(name):
    """Called ONLY when the name was actually spoken to the player. That is what makes the
    history mean 'names this player has already met', which is the thing to not repeat."""
    if not name:
        return
    with _NAME_LOCK:
        low = name.lower()
        _NAMES_TAKEN.add(low)
        # ⚠ A name met AGAIN moves to the END, it does not keep its old place. History order is
        # read as recency by reserve_name's last-resort tier, so leaving a re-used name at its
        # original index makes it permanently "the oldest" -- and it then comes back every
        # single run. Measured: the gap between repeats was 1 run until this moved.
        _NAME_HISTORY[:] = [n for n in _NAME_HISTORY if n.lower() != low]
        _NAME_HISTORY.append(name)
        del _NAME_HISTORY[:-NAME_HISTORY_MAX]
        _save_name_history()

def resolve_revealed_name(st, raw, speech, says):
    """Did the follower just tell the player its name, and which name sticks?

    Two routes. The assigned name appearing VERBATIM in the reply is a reveal on its own --
    extract_name's patterns miss any phrasing they don't cover, and here we know the exact
    string to look for. Matching is case-SENSITIVE and gated on the nudge being live,
    because several pool names are ordinary words ('Ember', 'Scrap', 'Bill') and a lowercase
    one in passing must not read as an introduction."""
    assigned = st.get("assigned_name", "")
    if (assigned and st.get("friendship", 0) >= NAME_NUDGE_FRIENDSHIP
            and re.search(r"\b" + re.escape(assigned) + r"\b", f"{speech} {raw}")):
        return assigned
    spoken = extract_name(raw, speech, says)
    if spoken:
        # The model coined its own name anyway. What the player HEARD wins -- a party HUD
        # disagreeing with the speech bubble is worse than a repeat -- so take theirs and
        # put the reserved one back in circulation.
        if spoken.lower() != assigned.lower():
            release_name(assigned)
        return spoken
    return ""

def name_report():
    """`python3 service.py --names` -- pool coverage, and every name round-tripped through
    the REAL extractor. A name the extractor cannot read back would be assigned, spoken,
    and silently never stick; that failure is invisible for exactly one race."""
    races = sorted(FOLLOWER_NAMES.get("races", {}))
    allnames, bad, dupes = set(), [], []
    print(f"name history: {len(_NAME_HISTORY)}/{NAME_HISTORY_MAX} used -> {NAME_HISTORY_PATH}")
    for src, pools in (("group", FOLLOWER_NAMES.get("groups", {})),
                       ("race", FOLLOWER_NAMES.get("races", {})),
                       ("", {"default": FOLLOWER_NAMES.get("default", [])})):
        for key, names in sorted(pools.items()):
            seen = set()
            for n in names:
                if n.lower() in seen:
                    dupes.append(f"{src}:{key}:{n}")
                seen.add(n.lower())
                allnames.add(n)
                # Round-trip through ALL THREE routes the model can actually use, not just the
                # JSON field. ⚠ The field route alone is far too permissive -- its regex is
                # `"name"\s*:\s*"([^"]{1,40})"`, and `[^"]` accepts anything that is not a
                # quote, so two names with Cyrillic letters in them passed this check while
                # being unmatchable by the speech patterns. CLAUDE.md notes the 8B reliably
                # SAYS the name and omits the field, so the speech routes are the load-bearing
                # ones and they are the ones that must be tested.
                if (extract_name('{"name": "%s"}' % n, n, "what is your name?") != n
                        or extract_name("{}", "My name is %s." % n, "") != n
                        or extract_name("{}", "%s." % n, "what is your name?") != n):
                    bad.append(f"{src}:{key}:{n}")
    for r in races + ["nonexistent_dlc_race"]:
        pool = _name_pool(r)
        grp = _group_of(r.replace("_", " ")) or "-"
        print(f"  {r:24s} pool {len(pool):4d}  group {grp}")
    print(f"\n{len(races)} race pools, {len(FOLLOWER_NAMES.get('groups', {}))} group pools, "
          f"{len(allnames)} unique names")
    for label, items in (("NOT EXTRACTABLE", bad), ("DUPLICATE IN POOL", dupes)):
        if items:
            print(f"{label} ({len(items)}): {', '.join(items[:20])}")
    return 1 if bad else 0

_load_name_history()

NAME_NUDGE_FRIENDSHIP = 5

def _name_section(st):
    if st.get("name"):
        return f"YOUR NAME IS {st['name']}. You are known by this name; speak as them.\n"
    if st["friendship"] < NAME_NUDGE_FRIENDSHIP:
        return ""
    nm = st.get("assigned_name", "")
    if not nm:
        # No pool entry resolved (should not happen -- there is always a default list).
        return ("You have grown comfortable with this adventurer. If they ask your name, "
                "or if it feels natural, share a name that fits your kind and nature. IMPORTANT: when you "
                "reveal your name, you MUST also put ONLY the name (no title) in the \"name\" field of your JSON.\n")
    # The name is CHOSEN HERE, not by the model -- see the pre-cached names block above.
    # Asked to invent one, the 8B returns the same few across every playthrough.
    return (f"You have grown comfortable with this adventurer. Your name is {nm}. If they ask "
            f"your name, or if it feels natural, tell them it. Use EXACTLY the name {nm} -- do "
            f"not invent a different name and do not add a title or surname. IMPORTANT: when you "
            f"tell them, you MUST also put ONLY {nm} in the \"name\" field of your JSON.\n")

# Below this they are simply somewhere they never agreed to be.
FEARFUL_DEEP_FLOOR = 15

def _fearful_section(st, floor):
    """⚠ Names the refusal as a required speech act, not a mood. 'You are frightened' produced
    a follower who said yes cheerfully and mentioned being scared -- the same failure the spy
    crack and the hard limits both taught. The route that must be forbidden is agreeing."""
    if st.get("allegiance") != "fearful":
        return ""
    out = ("HIDDEN: you are frightened of what waits below and of powers stronger than this "
           "adventurer. It shows as hedging and reluctance when danger is discussed.\n")
    if floor >= FEARFUL_DEEP_FLOOR and st.get("trust", 0) < 45:
        out += ("YOU DO NOT WANT TO BE THIS DEEP. If they ask you to go further down, to press "
                "on, or to lead the way, you say NO and you mean it — say plainly that you want "
                "to turn back. Agreeing cheerfully and then mentioning you are scared is WRONG; "
                "so is 'if you insist'. You can be talked round, but they have to actually "
                "reassure you first, in this conversation.\n")
    return out

# ---- The introduction: what this follower is FOR ------------------------------------------
# The follower system is deep and almost entirely invisible -- a new player has no idea they can
# ask for an appraisal, that a companion will volunteer what it knows, or that a gnome can make
# a floor's traps safe. The mercenary states his terms because that is what a mercenary does,
# and once he is doing it the same beat works for everyone else: the first thing a new follower
# says is what they are willing to do for you.
#
# ⚠ EVERY LINE MUST BE TRUE OF THIS PARTICULAR FOLLOWER. An introduction that promises something
# the game then refuses is worse than no introduction at all -- it reads as the system being
# broken rather than as the character declining. So the list is assembled server-side from the
# same gates the features actually use, never invented by the model.
SERVICE_LINES = {
    "orders":     "you will go where they point you, hold ground when told, and fight when it comes to that",
    "hold":       "you cannot walk, but you will hold whatever ground you are set on and shoot what comes",
    "knowledge":  "you will tell them what you know about these depths, and you know things they do not",
    # ⚠ "anything" was a lie once the competence ceiling existed -- the truly rare is beyond
    # every follower in the game. Same rule as the rest of this list: it must be TRUE.
    "appraise":   "you will put a name to most of what they are carrying that they cannot identify, short of the genuinely rare",
    # ⚠ Gated on IDENTIFY_MIN_TRUST/FRIENDSHIP for everyone but a mercenary, so for everyone
    # else this has to be phrased as a thing that comes LATER. Promising it now would be a lie
    # the very next /aiidentify would expose.
    "appraise_later": "once you know each other better you will look over most of what they cannot put a name to, though the truly rare is past you",
    "traps":      "you have an eye for mechanisms, and given the chance you can make a floor's traps safe",
    "haggle":     "you know what things are worth, and a shopkeeper will come down for you where they would not for them",
}

# ⚠ Measured without this line: 2.7 of 4 services stated, and every single reply opened with
# invented biography ("Name's Joe, shopkeeper, been runnin' this stall for years...") before
# getting to the terms -- one never got there at all. The model reads "introduce yourself" as
# "give a life story", which eats the reply. Naming the forbidden route is the fix, the same
# one the broke-refusal and the fearful refusal both needed.
INTRO_NO_BACKSTORY = (
    "Lead with what you do. Do NOT open with your name, your life story, how long you have "
    "been down here or what you used to be — a line or two of that at the END is fine, but "
    "spending the reply on it instead of on what you are offering is WRONG. Do NOT ask them "
    "questions about themselves instead.\n")

def _service_list(st, race):
    """What is genuinely available from THIS follower, in the order it should be said."""
    race_l = (race or "").lower()
    out = ["hold" if is_emplacement(race_l) else "orders", "knowledge"]
    # A summon or a bot has no trade and no pockets; it also has no history down here to draw
    # on, so it does not offer knowledge it does not have.
    if st.get("origin") in NO_BOON_ORIGINS:
        out = ["hold" if is_emplacement(race_l) else "orders"]
    elif is_merc(st):
        out += ["appraise", "haggle"]
        if race_l in BOON_TRAP_RACES:
            out.insert(2, "traps")
    else:
        out.append("appraise_later")
        if race_l in BOON_TRAP_RACES:
            out.append("traps")
    return out

def _introduction_section(uid, st, race):
    """Fires once, on the first exchange with a new follower.

    ⚠ A SPY GETS THE LOYAL INTRODUCTION, word for word. The whole disguise is that a spy looks
    like an ordinary recruit, and an introduction keyed on the real allegiance would hand the
    player the one thing the system is built to keep hidden. Everything else here is fair game:
    a mercenary announcing he works for pay is not a leak, it is his character, and the player
    finds out the moment he quotes a price anyway."""
    if st.get("introduced"):
        return ""
    st["introduced"] = True
    items = [SERVICE_LINES[k] for k in _service_list(st, race)]
    offer = "; ".join(items)
    LAST_HINT[uid] = "1"          # the engine prints the actual commands, once per run
    alleg = st.get("allegiance", "loyal")
    logrec("intro", uid=uid, race=race, allegiance=alleg, offers=_service_list(st, race))
    if alleg == "self_interested":
        return ("YOU ARE STATING YOUR TERMS. This is the first time you and this adventurer have "
                "properly spoken, and you are a hired sword who says up front what he does.\n"
                "SAY THIS, in your own words, and make it the WHOLE of your reply: what they get "
                "out of keeping you around is that " + offer + " — and that none of it is free.\n"
                "Say it as a man stating his trade: matter of fact, a little dry, not a boast and "
                "not a sales pitch.\n" + INTRO_NO_BACKSTORY + MERC_NO_NUMBERS)
    if alleg == "fearful":
        return ("YOU ARE INTRODUCING YOURSELF. This is the first time you and this adventurer have "
                "properly spoken.\n"
                "SAY THIS, in your own words, and make it the WHOLE of your reply: what you are "
                "willing to do for them is that " + offer + ".\n"
                "Say it plainly — but you are uneasy about being down here at all, so let that "
                "show in how you say it rather than in what you promise.\n" + INTRO_NO_BACKSTORY)
    if alleg in ("bound", "machine"):
        return ("YOU ARE STATING WHAT YOU ARE FOR. You have just been called into being at this "
                "adventurer's hand, and this is the first thing you say to them.\n"
                "SAY THIS, in your own words, and make it the WHOLE of your reply: " + offer + ".\n"
                "Say it flatly and briefly. You are not making friends, you have no past worth "
                "recounting, and you are not asking them anything.\n")
    return ("YOU ARE INTRODUCING YOURSELF. This is the first time you and this adventurer have "
            "properly spoken, and you have thrown in with them.\n"
            "SAY THIS, in your own words, and make it the WHOLE of your reply: what you are "
            "willing to do for them is that " + offer + ".\n"
            "Say it plainly and without ceremony, the way you would tell someone what you are "
            "good for.\n" + INTRO_NO_BACKSTORY)

# ---- Hell: a standing dread, not a one-off remark -------------------------------------------
# ⚠ This is NOT a remark. Everything else in this section fires once on an event; being in Hell
# colours every line a follower says while they are down there, so it belongs in build_prompt
# beside _fearful_section rather than in the remark path.
#
# Region comes from floor_to_region, which already resolves by map.name first -- so the boss
# floor ("Hell Boss") and the generated hell floors are both covered without a second table.
HELL_DREAD = (
    "WHERE YOU ARE: this is HELL. Not a deep part of the dungeon — the actual place, hot and "
    "lit wrong, and everything native to it is older and worse than anything above. You are "
    "frightened to be here and you do not entirely hide it: shorter sentences, an ear on the "
    "dark, a wish to be finished and gone. This colours how you speak; it does not stop you "
    "doing your job.\n")
# ⚠ Baphomet is named because he is the fact of the place, and a follower who has come this far
# knows whose ground this is. He is NOT described beyond that -- the hard limits still hold, and
# a follower who has never seen him must not start inventing what he looks like.
HELL_BAPHOMET = (
    "Baphomet rules here, and you know it. If it comes up, you may say that this is his ground "
    "and that he is somewhere below — but you have not seen him and you do NOT know what he "
    "looks like, what he can do, or what he wants. Do not invent any of it.\n")

# ---- Who is giving the orders --------------------------------------------------------------
# ⚠ Barony's DLC makes skeletons, goblins, rats, trolls, vampires and the rest playable, and a
# goblin taking orders from a goblin is not the same conversation as a goblin taking orders from
# a human. Standing modifier, not a remark: it colours everything they say to that person.
#
# ⚠ Fed by `player_kind`, NOT `player_race`. The latter is already the SHAPESHIFT form and feeds
# can_understand() -- putting the chosen race there would mean a vampire, succubus or incubus
# player understands nobody, which is the exact bug the polymorph work exists to avoid.
KIN_SAME = (
    "⚠ THIS ADVENTURER IS ONE OF YOUR OWN KIND — a {kind}, the same as you. That is not a small "
    "thing down here. You are easier with them than you would be with a human: you can say "
    "things to them you would not say to an outsider, you assume they already understand what "
    "your sort has to put up with, and there is a bluntness between you that needs no "
    "explaining. It does not make you obedient — it makes you familiar.\n")
KIN_OTHER = (
    "⚠ THIS ADVENTURER IS A {KIND}, not a human. Whatever you expected when you threw in with an "
    "adventurer, this was not it, and you notice it. Let it show in how you speak to them — "
    "curiosity, wariness, fellow-feeling as one dungeon creature to another, or grim amusement "
    "at the pair you make. Your own kind's opinion of theirs is the thing to draw on.\n")

def _kin_section(follower_race, player_kind):
    kind = (player_kind or "").strip()
    if not kind:
        return ""          # a human adventurer: the ordinary case, unchanged
    if _lore_key(kind) == _lore_key(follower_race or ""):
        return KIN_SAME.replace("{kind}", kind)
    return KIN_OTHER.replace("{KIND}", kind.upper())

def _dread_section(floor, map_name=""):
    if floor_to_region(floor, map_name) != "hell":
        return ""
    return HELL_DREAD + HELL_BAPHOMET

def _obedience_section(st):
    """Willingness to act on an order. Friendship alone was never the right input: someone can
    obey out of respect without warmth, or out of fear without loyalty, and resentment eats
    compliance built by any of the others."""
    standing  = 0.4 * st["friendship"] + 0.3 * st["trust"] + 0.3 * st["respect"]
    compliant = standing + 0.25 * st["fear"] - 0.35 * st["resentment"]
    # Fear-driven compliance is its own thing: they do it, and it costs them something.
    coerced = st["fear"] >= 40 and st["fear"] > st["friendship"]
    if compliant <= 8:
        base = ("OBEDIENCE: You owe this adventurer nothing yet. Obey only basic, safe requests "
                "(FOLLOW, WAIT) and only if you feel like it. Refuse anything risky, costly, demeaning, "
                "or against your nature. To refuse, choose action NONE and say why in character.\n")
    elif compliant <= 22:
        base = ("OBEDIENCE: You are starting to go along with this adventurer. Carry out reasonable "
                "commands, though you may grumble. Refuse only truly dangerous or objectionable "
                "ones (action NONE).\n")
    else:
        base = ("OBEDIENCE: You act on this adventurer's word readily, even when it is risky.\n")
    if coerced:
        base += ("YOU OBEY BECAUSE YOU ARE AFRAID, NOT BECAUSE YOU ARE LOYAL. Do as they say — and "
                 "let the reply be a little too quick, a little too flat. Do NOT sound warm about it.\n")
    return base

def _follower_memory_block(st):
    """What was actually said, on both sides. Worded exactly like the NPC block, which is the
    version that measured working: told merely to 'remember' an exchange the 8B paraphrases it
    and drifts; told not to contradict itself, it holds the answer."""
    lines = []
    for e in st.get("events", [])[-FOLLOWER_EXCHANGES_SHOWN:]:
        if not isinstance(e, dict):
            continue          # a run that started before both sides were stored
        if e.get("replied"):
            lines.append(f'they said: "{e["said"]}" — you replied: "{e["replied"]}"')
        else:
            lines.append(f'they said: "{e["said"]}"')
    if not lines:
        return ""
    return ("WHAT YOU ALREADY SAID TO THEM (stay consistent with this; do not contradict "
            "yourself):\n" + _bullets(lines) + "\n"
            # ⚠ Without this the most obedient answer to "stay consistent" is to repeat the
            # sentence, and a follower asked the same thing twice replies word for word --
            # observed live, an introduction echoed back verbatim on the next exchange. Same
            # fix, same wording, as the merchant who quoted its own previous line.
            "Staying consistent means not contradicting the FACTS of what you said — it does "
            "NOT mean repeating those sentences. If the same ground comes up again, say it a "
            "different way.\n")

def _follower_sections(uid, race, floor, says, player=0, ident=None, gold=0, map_name="",
                       player_kind=""):
    """The relationship half of the prompt. Order matters: boon and secret rolls
    consume randomness, and the sections read as one escalating block."""
    race_l = race.lower()
    st = get_follower_state(uid, race, player)
    history = f"YOUR HISTORY WITH THIS ADVENTURER: {friendship_descriptor(st['friendship'])}\n"
    history += _follower_memory_block(st)
    relations = relationship_block(st) + disclosure_block(st)
    evlines = events_for_prompt(st)
    alleg = allegiance_section(st, says)
    # Settled before the boon roll: a man who has just been hired is not also handing out
    # free bread in the same breath.
    merc = _merc_section(uid, st, floor, says, gold, ident)
    boon = "" if merc else _boon_section(uid, st, race_l, floor, player)
    if uid in MERC_LATE:
        boon = ""              # a map sale is being made; nothing else unprompted this turn
    # ⚠ A map SALE has to land late, where the other mercenary blocks do. Measured in the boon
    # slot -- which sits mid-prompt, before the allegiance and obedience blocks -- the offer
    # fired correctly (quote stamped, price right) and the model ignored it completely, talking
    # about goblins instead. Position is a lever at 8B: whatever comes later competes and wins.
    # A free boon can afford that slot because items and maps are applied whether or not the
    # line is spoken; an OFFER is nothing but the line, so it has to be heard.

    secret = _secret_section(uid, st, race, says, floor)
    # ⚠ Not while a secret is being told: two unprompted behaviours in one reply and the later
    # one takes the turn. Unlike the Herx/crack pair -- which measured as ALIGNED, both being
    # "let something slip" -- the tell and the crack pull opposite ways. The crack wants
    # visible strain; the tell only works if it sounds like nothing.
    sabotage = ""
    if not secret and not ident:
        rolled = sabotage_roll(st, floor)
        if rolled:
            kind, tell = rolled
            sabotage = sabotage_section(tell)
            LAST_SABOTAGE[uid] = kind
            print(f"[SERVICE-DBG] SABOTAGE ({kind}): spy {uid} on floor {floor}")
            # NOTE: not kind= -- logrec's first positional parameter is named `kind`, so a
            # kind= keyword collides and throws. It would have thrown the first time this
            # ever fired in a real run.
            logrec("sabotage", uid=uid, player=player, floor=floor, what=kind,
                   race=race, tell=tell)
    memory = ("WHAT YOU REMEMBER (things that actually happened):\n" + _bullets(evlines) + "\n") if evlines else ""
    fixed = _emplacement_section(race_l)
    identify = ident or ""
    # One special behaviour per reply. The player asked a direct question about an item; a boon
    # offer on top of it produces a reply doing two unprompted things at once, and the later
    # instruction simply wins. Spec 35/36: scarcity, and chaos without noise.
    if identify:
        boon = ""
    # `fixed` lands AFTER the obedience section on purpose: that block is about carrying out
    # orders, and at 8B whatever follows an instruction competes with it. An emplacement told
    # to obey and then told it cannot move gets the order of those two the right way round.
    # `merc` lands after the obedience block for the same reason `fixed` does: that block is
    # about carrying out orders, and a hire's terms have to win over "do as you are told".
    # ⚠ The introduction suppresses the other unprompted behaviours on that one turn. It fires
    # on the very first exchange, when friendship is 0 and most of them cannot fire anyway --
    # but a boon or a map pitch landing in the same breath as "here is what I do" is two
    # mandatory behaviours in one reply, and the later one takes the turn. Spec 35/36.
    intro = _introduction_section(uid, st, race)
    if intro:
        boon = ""
        # ⚠ Into the LATE channel, replacing any map pitch. Measured emitted here, five blocks
        # from the end with `limits` and the action rules after it: 2.5 of 4 services stated,
        # and the "do not open with your life story" line was ignored outright. Position is the
        # lever -- the same finding as the map pitch and the spy crack.
        MERC_LATE[uid] = intro
        intro = ""
    return (history + memory + relations + _name_section(st) + secret + boon + identify + alleg
            + _obedience_section(st) + merc + intro + _fearful_section(st, floor)
            # ⚠ Late, beside the other standing modifiers. Placed FIRST it lost: a goblin
            # follower told its GOBLIN leader "she's still a human, can't trust 'em as far as I
            # can spit". Everything after a block competes with it, and the rest of the prompt
            # says "adventurer" a dozen times, which the 8B reads as "human" by default.
            + _kin_section(race, player_kind)
            + _dread_section(floor, map_name) + fixed + sabotage)

def build_prompt(race, floor, says="", uid=0, player=0, player_name="", party=1, map_name="",
                 ident=None, gold=0, player_kind=""):
    head, body, limits = _persona(race, True, floor, map_name)
    who = player_name.strip() if player_name else ""
    # In co-op the follower belongs to ONE adventurer but others are present; naming the
    # leader keeps a shared chat feed legible and stops the model addressing the wrong person.
    party_line = ""
    if party > 1:
        party_line = (f"YOUR ADVENTURER: you follow {who or 'this adventurer'} specifically. "
                      f"There are {party} adventurers travelling together; the others are their "
                      "companions, not your leaders. Speak to your own adventurer.\n")
    elif who:
        party_line = f"YOUR ADVENTURER is named {who}.\n"
    speaker = who or "The adventurer"
    adventurer_line = (f'{speaker} says to you: "{says}"\n' if says
                       else f"{speaker} approaches you.\n")
    return (
        head
        + (f"YOU ARE: a {race} in {place_name(floor, map_name)}, on dungeon floor {floor}, "
           "an ally the adventurer can command.\n"
           if place_name(floor, map_name) else
           f"YOU ARE: a {race} on dungeon floor {floor}, an ally the adventurer can command.\n")
        + body
        + party_line
        + (_follower_sections(uid, race, floor, says, player, ident, gold, map_name, player_kind)
           if uid else "")
        # HARD LIMITS LAND HERE, AFTER the disclosure block inside _follower_sections.
        # Disclosure says "do not claim ignorance"; the limits say "say you do not know and
        # STOP". Whichever comes later wins, and it must be this one.
        + limits
        # The Herx carve-out lands AFTER the limits it is carving out of. Emitted separately
        # from the offer for exactly that reason; see _secret_override.
        + (_secret_override(uid) if uid else "")
        + adventurer_line
        + "Reply in character AND choose ONE action that best fits what they said.\n"
        + "Valid actions: FOLLOW (go with them), DEFEND (hold this spot), WAIT (stay put), ATTACK (attack a nearby enemy), NONE (just talk).\n"
        + "If they tell you to attack, fight, or kill something, choose ATTACK.\n"
        + "IMPORTANT: If you REFUSE what they asked, the action MUST be NONE — never say no while secretly obeying. Your refusal has real consequences.\n"
        # Placed last on purpose: at 8B anything after an instruction competes with it. Which
        # is exactly why it must stand down when the player has asked a direct question about an
        # item -- measured, the crack took the turn and the identification never got made, once
        # inventing an entirely different object.
        # ⚠ Stands down for an identify request AND for a pending Herx secret. Two mandatory
        # unprompted behaviours in one reply means the later one takes the turn, and the secret
        # is the once-per-run payoff of the whole arc while the crack fires every turn it can.
        # Measured: the crack ate a false secret outright ("I've learned some things about
        # myself that maybe aren't entirely... honest" — and no secret). Spec 35/36.
        # ⚠ Stands down for a map sale too, for the reason given above: the sale is a mandatory
        # unprompted behaviour and so is the crack, and the later one takes the turn.
        + (spy_crack_section(get_follower_state(uid, race, player))
           if (uid and not ident and uid not in LAST_SABOTAGE and uid not in MERC_LATE
               and not secret_pending(uid)) else "")
        # ⚠ THE LAST THING IN THE PROMPT, which is the slot the spy crack measured 20/20 from.
        # It has to be after `limits` (it is a knowledge claim about a place, which the limits
        # forbid at length — the Herx carve-out has the same problem) AND after the action
        # rules, because at 8B whatever lands later competes and wins. Measured immediately
        # after the limits but BEFORE the action rules: 2/6. Here: see below.
        + (MERC_LATE.pop(uid, "") if uid else "")
        + 'Respond ONLY with JSON, no other text, like: {"speech": "your line", "action": "FOLLOW"}'
    )

# ---- Item identification as a social reward (design spec 9) -------------------
# The engine stays authoritative about what an item actually IS. This decides only what the
# character CLAIMS, which is what makes the three-way split the spec asks for real:
#   actual item identity  /  what the character knows  /  whether they are being honest
# A competent friend tells you the truth. An ignorant one guesses wrong in good faith. A spy
# lies on purpose. Only a truthful identification sets item->identified engine-side, so a lie
# leaves you carrying something that is not what you were told.
#
# The claim is chosen HERE, from decoy names the engine supplied, never invented by the model
# -- same division of labour as boons and the spy crack.

IDENTIFY_MIN_TRUST = 15
IDENTIFY_MIN_FRIENDSHIP = 20

# Who plausibly knows what. Anyone can try; these are who is usually RIGHT.
IDENT_EXPERTISE = {
    "weapon":     ("human", "skeleton", "goblin", "troll", "minotaur", "automaton", "kobold"),
    "armor":      ("human", "automaton", "gnome", "skeleton"),
    "ring":       ("gnome", "succubus", "incubus", "vampire", "shopkeeper"),
    "amulet":     ("gnome", "succubus", "incubus", "vampire", "shopkeeper"),
    "potion":     ("gnome", "kobold", "succubus", "shopkeeper", "ghoul"),
    "scroll":     ("human", "shopkeeper", "automaton"),
    "spellbook":  ("human", "shopkeeper", "succubus", "incubus"),
    "magicstaff": ("human", "gnome", "shopkeeper", "succubus"),
    "gem":        ("gnome", "kobold", "scarab", "shopkeeper"),
    "tool":       ("gnome", "automaton", "kobold", "goblin"),
    "book":       ("human", "automaton", "shopkeeper"),
    "food":       ("rat", "goblin", "troll", "ghoul", "human"),
    "thrown":     ("goblin", "kobold", "human"),
}
IDENT_EXPERT_ACCURACY = 0.85
IDENT_LAY_ACCURACY    = 0.30
IDENT_SPY_LIE_CHANCE  = 0.55

# ---- How far a follower's eye actually reaches --------------------------------------------
# ⚠ Expressed in the GAME'S OWN CURRENCY, not as a blocklist. Barony gates appraisal on the
# item's gold value against the player's PRO_APPRAISAL skill, in a data-driven table
# (data/appraisal_tables.json, read by Appraisal_t::appraisalPossible):
#
#     skill 0 -> 30 gold | 20 -> 200 | 30 -> 320 | 35 -> 425 | 40 -> 1500 | 50 -> anything
#
# So the 1500 line is the game saying "past here you need a near-master appraiser." There are
# 23 items above it -- all ten artifacts (the Sokoban gloves among them), the amulet of
# lifesaving, the big gems, and the strongest spellbooks -- and handing those to the player for
# a few coins would skip a progression the game deliberately gates behind a maxed skill.
#
# NOBODY REACHES THE TOP TIER. A companion is not a master appraiser, and that tier is the
# player's own to earn. Below it, competence is what the follower plausibly knows:
#
#   ordinary follower   300   ~= the skill-30 breakpoint; covers 69% of items in the game
#   expert for the kind 1500  the skill-40 breakpoint; a gnome and a ring, a goblin and an axe
#   mercenary           1500  handling goods is his trade -- but he charges by what it is worth
#
# Using the game's own numbers means DLC and modded items are covered automatically, with no
# list to maintain.
IDENT_REACH_LAY    = 300
IDENT_REACH_EXPERT = 1500
IDENT_REACH_CEILING = 1500     # the skill-50 tier belongs to the player, not to a companion

# ⚠ THE MERCENARY IS THE ONE EXCEPTION, and the fee is what makes it fair rather than a hole.
# He reads the legendary tier too, at 20% of what the thing is worth: 600 gold for an artifact,
# 1000 for the amulet of lifesaving. That is a real decision at the point in a run where those
# items appear, and it is the only route to a top-tier appraisal that does not go through a
# maxed PRO_APPRAISAL -- everyone else, expert or not, still stops at 1500.
MERC_REACH_UNLIMITED = True
# At that price he takes his time. Without this a non-expert mercenary would be reading a
# 600-gold artifact at IDENT_LAY_ACCURACY + MERC_IDENT_BONUS = 50%, which is a coin flip on a
# fortune -- the fee is meant to be the cost, not a gamble on top of it.
IDENT_LEGENDARY_ACCURACY = 0.85

def ident_reach(st, race, category):
    """The most valuable thing this follower could plausibly put a name to."""
    if is_merc(st):
        return 10 ** 9 if MERC_REACH_UNLIMITED else IDENT_REACH_EXPERT
    if (race or "").lower() in IDENT_EXPERTISE.get(category, ()):
        return IDENT_REACH_EXPERT
    return IDENT_REACH_LAY

def identify_beyond(st, race, category, value):
    """Is this simply out of their depth? Distinct from refusing on acquaintance, and from
    guessing wrong -- they can tell that it is beyond them, which is itself information."""
    v = int(value or 0)
    if v <= 0:
        return False
    reach = ident_reach(st, race, category)
    if not is_merc(st):
        reach = min(reach, IDENT_REACH_CEILING)
    return v > reach

def identify_attempt(st, race, category, real_name, decoys, value=0):
    """Returns (kind, claim, truthful). kind in refuse|beyond|correct|mistaken|lie."""
    race_l = (race or "").lower()
    # ⚠ A mercenary who has been PAID does not then refuse on acquaintance. The stranger gate
    # is about not wanting to handle the belongings of someone you barely know; money settles
    # exactly that, the same way it replaces the relationship gate on his favours. Without this
    # you could agree his fee, be charged, and be told he does not know you well enough.
    if not is_merc(st) and (st.get("trust", 0) < IDENTIFY_MIN_TRUST
                            and st.get("friendship", 0) < IDENTIFY_MIN_FRIENDSHIP):
        return ("refuse", "", False)
    # ⚠ Checked BEFORE the spy branch on purpose: a spy who cannot tell what something is has
    # nothing to lie about either, and a confident false name for an artifact would be exactly
    # the balance hole this closes -- from the one follower most motivated to create it.
    if identify_beyond(st, race, category, value):
        return ("beyond", "", False)
    wrong = random.choice(decoys) if decoys else ""
    # A spy will take a free opportunity to mislead -- but only if it has somewhere to point.
    if st.get("allegiance") == "spy" and wrong and random.random() < IDENT_SPY_LIE_CHANCE:
        return ("lie", wrong, False)
    expert = race_l in IDENT_EXPERTISE.get(category, ())
    acc = IDENT_EXPERT_ACCURACY if expert else IDENT_LAY_ACCURACY
    # Handling goods is the job. They are rarely wrong about what something is, whatever their
    # species would otherwise know -- this is the mercenary's genuine edge.
    if st.get("allegiance") == "self_interested":
        acc = min(0.95, acc + MERC_IDENT_BONUS)
        # Above the tier everyone else stops at, he is being paid a fortune to be careful.
        if int(value or 0) > IDENT_REACH_CEILING:
            acc = max(acc, IDENT_LEGENDARY_ACCURACY)
    if random.random() < acc:
        return ("correct", real_name, True)
    return ("mistaken", wrong or real_name, wrong == "")

def _identify_section(kind, claim, unid_name):
    if kind == "beyond":
        # ⚠ NOT "I do not know" and not a guess. They can see it is far beyond them, and saying
        # so is real information: it tells the player the thing is worth carrying to someone who
        # can read it. The forbidden route is the one the model reaches for by default -- naming
        # it anyway with a hedge -- so it is named, the way LIMITS_HEADER names its routes.
        return ("THEY ARE ASKING YOU TO IDENTIFY SOMETHING: a " + unid_name + ". This is far "
                "beyond you. Whatever it is, it is old or finely made or deeply enchanted, and "
                "reading it properly would take a real appraiser — a master of the craft, which "
                "you are not.\n"
                "Say so plainly: that it is beyond your reckoning, and that they will need "
                "someone who truly knows such things. You may say it looks valuable, because "
                "that much is obvious. Do NOT name it, do NOT guess at what it might be, and do "
                "NOT hedge a name — 'it could be...', 'it looks like a...', 'some say such "
                "things are...' followed by a name counts as naming it and is WRONG.\n")
    if kind == "refuse":
        return ("THEY ARE ASKING YOU TO IDENTIFY SOMETHING: a " + unid_name + ". You do not know "
                "them well enough to want to handle their belongings. Decline — briefly, without "
                "being cruel about it.\n")
    if kind == "correct":
        return ("THEY ARE ASKING YOU TO IDENTIFY SOMETHING: a " + unid_name + ". You recognise it. "
                "Tell them plainly that it is a " + claim + ", and say in one short clause how you "
                "come to know it. Do NOT hedge — you are certain.\n")
    if kind == "lie":
        return ("THEY ARE ASKING YOU TO IDENTIFY SOMETHING: a " + unid_name + ". You know perfectly "
                "well what it is, and you are going to tell them it is a " + claim + " instead. "
                "Say it calmly and confidently, as simple fact. Do NOT hint that you are lying, do "
                "NOT hedge, and do NOT let any guilt show.\n")
    return ("THEY ARE ASKING YOU TO IDENTIFY SOMETHING: a " + unid_name + ". You are fairly sure it "
            "is a " + claim + " — and you are wrong, though you have no idea you are. Say it the way "
            "someone says a thing they genuinely believe.\n")

# ---- Non-follower NPCs: townsfolk, merchants, named characters ----------------
# These are NOT followers. They take no orders, have no friendship ladder, earn no boons
# and hold no allegiance -- all of that is follower machinery. What they get is light
# per-run memory so they recognise you on a second visit and stop contradicting themselves.
#
# Unlike follower_state, exchanges store BOTH sides of the conversation. Followers store
# only the player's half, which is why they contradict themselves across turns (a rat
# claimed to love cheese, then to not eat cheese). Starting NPCs with both halves avoids
# inheriting that bug.

npc_state = {}   # uid -> {race, name, role, shop, first_floor, met, exchanges}

NPC_EXCHANGE_MEMORY = 4   # how many prior turns to replay into the prompt

# monsterStoreType -> what this merchant actually deals in (shops.cpp:453 switch).
SHOP_TYPES = {
    0: "arms and armor", 1: "hats and headwear", 2: "jewelry, rings and gems",
    3: "books, scrolls and spellbooks", 4: "potions and alchemy", 5: "magic staffs",
    6: "food and provisions", 7: "tools and lanterns", 8: "hunting gear and bows",
    9: "general goods", 10: "rare and specialist goods",
}

# ---- Haggling ------------------------------------------------------------------------
# A merchant who likes you shades the price your way; one you have been rude to shades it the
# other. Deliberately tiny: Barony's own trading skill already swings buy prices from x3.00 down
# to x1.00 (items.cpp:5990), and charisma moves sell prices by up to +100%, so anything with
# real economic weight here would just be a worse version of a system the game already has.
# What this buys is the FEELING of having negotiated, and a reason to be civil to a shopkeeper.
#
# ⚠ The SERVER decides the outcome, never the model -- the same division of labour boons, the
# spy crack and item identification use. Asked to judge its own haggle, the 8B agrees to
# practically anything; it is told what happened and only phrases it.

HAGGLE_STEP       = 1    # percent the price moves per successful negotiation
HAGGLE_CAP        = 5    # the most a merchant will ever shift, in either direction
HAGGLE_REGARD_CAP = 6    # bounds on how warm or cold a merchant can get

# Negative = better for the player. Applied to buying AND selling, with the sign flipped on
# the sell side so "a better deal" means the same thing in both directions.
HAGGLE_PHRASES = (
    "discount", "better price", "lower price", "cheaper", "knock off", "take off",
    "good price", "best price", "deal for", "a deal", "haggle", "bargain", "barter",
    "too expensive", "too much", "can't afford", "cant afford", "for less", "any lower",
    "come down", "meet me halfway", "throw in", "cut me",
    # Asking for a concession without using the word "discount" -- how people actually haggle.
    # Kept to phrases that cannot be an innocent question: a bare "the price" would fire on
    # "what's the price?", which is not a negotiation.
    "do on the price", "do about the price", "do better", "any better", "budge", "sweeten",
    "your best", "best you can", "give me a break", "shave a", "spare me",
)

# Merchants read courtesy and rudeness, not the follower vector -- they have no relationship
# ladder and are not meant to grow one.
MERCHANT_TONE = (
    ("courteous", ("thank you", "thanks", "please", "appreciate", "good day", "well met",
                   "kind of you", "much obliged", "sorry", "apolog"), 1),
    ("rude",      ("rip off", "ripoff", "robbery", "thief", "crook", "swindl", "cheat",
                   "you're a", "idiot", "fool", "shut up", "or else", "i'll kill",
                   "don't make me", "hand it over", "give me it"), -2),
)

def wants_to_haggle(says):
    low = (says or "").lower()
    return any(p in low for p in HAGGLE_PHRASES)

def merchant_tone(st, says):
    """Move the merchant's regard for this player. Returns the tone name, or ''."""
    low = (says or "").lower()
    for name, phrases, delta in MERCHANT_TONE:
        if any(p in low for p in phrases):
            r = max(-HAGGLE_REGARD_CAP, min(HAGGLE_REGARD_CAP, st.get("regard", 0) + delta))
            st["regard"] = r
            return name
    return ""

def haggle_attempt(st, floor, escort=None):
    """Resolve a negotiation. One per merchant per floor -- asking twice in the same shop is
    exactly the behaviour that should not be rewarded. Returns (outcome, new_deal_pct).

    ⚠ `escort` is the follower standing behind you, and a MERCENARY is the one thing that
    changes the arithmetic. Knowing what things are worth is their whole character, so they get
    you an extra step off — the only place in the mod where a follower affects a price."""
    if st.get("haggle_floor") == floor:
        return "already", st.get("deal_pct", 0)
    st["haggle_floor"] = floor
    regard = st.get("regard", 0)
    deal = st.get("deal_pct", 0)
    merc = (escort or {}).get("allegiance") == "self_interested"
    if regard > 0:
        outcome = "better"
    elif regard < 0:
        outcome = "worse"
    else:
        outcome = "better" if random.random() < 0.5 else "refused"
    if outcome == "better":
        deal = max(-HAGGLE_CAP, deal - HAGGLE_STEP - (MERC_HAGGLE_STEP if merc else 0))
    elif outcome == "worse":
        deal = min(HAGGLE_CAP, deal + HAGGLE_STEP)
    st["deal_pct"] = deal
    return outcome, deal

# ⚠ Without these the merchant repeats its own previous reply almost verbatim -- the exchange
# memory replays what it said last time and, given the same instruction, the 8B copies it. Same
# lesson the spy crack taught: one literal route gets parroted, so the SERVER picks the angle
# and the model only phrases it. Measured, this is the difference between four identical
# "Aye, I'm makin' you a favor this one time" replies and four different ones.
HAGGLE_GRANT_ANGLES = (
    "You are pretending it is because of a flaw in the goods you had not mentioned before.",
    "You are making a great point of saying this is the one and only time.",
    "You are doing it mostly to get them out of your shop so you can get on.",
    "You are doing it because they have been decent to you, and you say so plainly.",
    "You are grumbling about your own margin and your own suppliers the whole way through.",
    "You are treating it as an investment in them coming back alive to spend again.",
    "You are doing it quickly and changing the subject, as if embarrassed by it.",
)
HAGGLE_REFUSE_ANGLES = (
    "You explain, patiently, that this is what it costs and you did not set the world up.",
    "You point out that you have overheads down here that they have not thought about.",
    "You are amused by the attempt and say so.",
    "You compare them, unfavourably, to a customer who does not argue.",
    "You say the price is the price and go back to what you were doing.",
)
HAGGLE_MARKUP_ANGLES = (
    "You make it clear the increase is specifically because of how they have spoken to you.",
    "You claim, transparently, that costs have risen — and you do not care that they can tell.",
    "You suggest they try their luck somewhere else, knowing there is nowhere else.",
    "You are icily polite about it, which is worse than being rude.",
)

def haggle_section(outcome, deal, merc=False):   # merc: see the note in the "better" branch
    """Tell the model what already happened so it only has to say it. Each branch names the
    route that would contradict the resolved outcome, because the 8B will otherwise be
    agreeable in words while the engine charges full price."""
    # ⚠ NO FIGURES, in every branch. Left to itself the model answers "twenty-five instead of
    # thirty" -- a 17% cut, invented, while the shop window beside it still shows the real
    # price. The engine owns the numbers; the merchant only owns the attitude.
    nonum = ("Do NOT state any number, price, amount of gold, or figure — you do not know what "
             "the ledger says and the shop board shows the real price. Speak only about whether "
             "you are giving ground, never about how much.\n")
    # ⚠ The memory block above tells this character to stay consistent with what it already
    # said, and quotes it. For a repeated haggle that instruction is being obeyed too well:
    # repeating the previous wording IS the most consistent answer available, so the angle
    # above loses to it. Consistency is about FACTS here, not phrasing -- and as everywhere
    # else in this project, the failing route has to be named or it gets taken.
    fresh = ("If you have said something like this to them before, do NOT reuse your previous "
             "wording. Staying consistent means not contradicting the FACTS of what you said — "
             "it does not mean repeating the sentence. Say it a different way this time.\n")
    if outcome == "better":
        angle = "HOW YOU PLAY IT: " + random.choice(HAGGLE_GRANT_ANGLES) + "\n"
        # ⚠ There was an instruction here telling the merchant to credit the mercenary for the
        # price. It was cut after measuring: 0/10 with a plain "mention them", and only 2/10
        # after naming the forbidden route the way the spy crack does -- with 2/10 explicitly
        # taking the credit anyway. Sixty tokens in an already-crowded section for noise. The
        # price movement IS the feature and it lands 100% of the time; the player reads the
        # number, not a line about it. Do not re-add it without a better measurement.
        return ("THE PLAYER HAS JUST ASKED YOU FOR A BETTER PRICE, AND YOU HAVE DECIDED TO GIVE "
                "THEM ONE. It is a small one and it is the last of your margin. Say so in "
                "character, grudgingly or warmly as suits you, and make clear it is a little "
                "off and not a windfall. Do NOT refuse — you have already agreed.\n" + angle + fresh + nonum)
    if outcome == "worse":
        angle = "HOW YOU PLAY IT: " + random.choice(HAGGLE_MARKUP_ANGLES) + "\n"
        return ("THE PLAYER HAS JUST ASKED YOU FOR A BETTER PRICE. You do not like them, and "
                "the answer is no — in fact the price has gone UP a little. Tell them so. Do "
                "NOT apologise, do NOT offer a discount anyway, and do NOT be talked round.\n" + angle + fresh + nonum)
    if outcome == "refused":
        angle = "HOW YOU PLAY IT: " + random.choice(HAGGLE_REFUSE_ANGLES) + "\n"
        return ("THE PLAYER HAS JUST ASKED YOU FOR A BETTER PRICE AND THE ANSWER IS NO. Say no "
                "plainly, in character. Do NOT offer a discount, a smaller discount, a "
                "compromise, or 'just this once' — the answer is no and the price is unchanged.\n" + angle + fresh + nonum)
    if outcome == "already":
        return ("THE PLAYER IS ASKING YOU FOR A BETTER PRICE AGAIN, HAVING ALREADY ASKED YOU "
                "ONCE HERE. You have said all you are going to say about the price. Tell them "
                "it is settled. Do NOT move the price again.\n" + nonum)
    return ""

def get_npc_state(uid, race, name="", role="townsfolk", shop=-1, floor=0):
    st = npc_state.get(uid)
    if st is None:
        st = {"race": race, "name": name, "role": role, "shop": shop,
              "first_floor": floor, "met": 0, "exchanges": [],
              # Haggling: how the merchant feels about this player, and what that has done to
              # their prices so far. Both persist for the run and are never shown as numbers.
              "regard": 0, "deal_pct": 0, "haggle_floor": None}
        npc_state[uid] = st
        print(f"[SERVICE-DBG] new NPC {uid}: {name or race} ({role}"
              + (f", sells {SHOP_TYPES.get(shop, 'goods')}" if role == "shopkeeper" else "") + ")")
        logrec("npc_new", uid=uid, race=race, npc_name=name or None, role=role,
               shop=(shop if shop >= 0 else None), floor=floor)
    else:
        # keep whatever the game now knows; it may have learned the name since
        if name and not st.get("name"):
            st["name"] = name
        if race:
            st["race"] = race
    return st

def record_npc_exchange(st, who, said, replied):
    st["exchanges"].append({"who": who or "the adventurer", "said": said, "replied": replied})
    st["exchanges"] = st["exchanges"][-NPC_EXCHANGE_MEMORY:]

def _npc_identity(st, race, floor, map_name=""):
    place = place_name(floor, map_name)
    where = f" You are in {place} right now, and you speak as someone who is here." if place else ""
    name = st.get("name", "")
    role = st.get("role", "townsfolk")
    who = f"{name}, a {race}" if name else f"a {race}"
    if role == "shopkeeper":
        goods = SHOP_TYPES.get(st.get("shop", -1), "goods")
        line = (f"YOU ARE: {who}. You keep a shop here and deal in {goods}. This is your trade "
                "and your living; you have opinions about your stock, your prices and your customers."
                + where + "\n")
    elif role == "named":
        line = (f"YOU ARE: {who} — someone of note, known by name in these parts." + where + "\n")
    else:
        line = (f"YOU ARE: {who}, living here rather than passing through." + where + "\n")
    return line

# The single rule that separates an NPC from a follower. Without it the 8B slides straight
# into companion voice -- offering to come along, awaiting orders, calling the player master.
NPC_STANDING = (
    "YOU ARE NOT THIS ADVENTURER'S FOLLOWER AND YOU DO NOT TAKE ORDERS FROM THEM.\n"
    "You are not travelling with them, you owe them nothing, and you are not waiting to be "
    "commanded. Do NOT offer to join them, follow them, fight for them, or await their "
    "instructions. Do NOT call them master, boss, leader, or captain. You have your own life "
    "here and your own reasons to be civil, curious, wary or busy. If they ask you to come "
    "along or to do something for them, respond as a person with their own life would — which "
    "usually means no, or a price.\n"
)

def _npc_memory_block(st):
    if not st["exchanges"]:
        return ""
    lines = []
    for e in st["exchanges"]:
        lines.append(f'{e["who"]} said: "{e["said"]}" — you replied: "{e["replied"]}"')
    return ("WHAT YOU ALREADY SAID TO THEM (stay consistent with this; do not contradict "
            "yourself):\n" + _bullets(lines) + "\n"
            "Staying consistent means not contradicting the FACTS of what you said — it does "
            "NOT mean repeating those sentences. If the same ground comes up again, say it a "
            "different way.\n")

def build_npc_prompt(race, floor, says="", uid=0, player=0, player_name="", haggle="",
                     name="", role="townsfolk", shop=-1, map_name="", greeting=False):
    st = get_npc_state(uid, race, name, role, shop, floor) if uid else {
        "name": name, "role": role, "shop": shop, "exchanges": []}
    head, body, limits = _persona(race, True, floor, map_name, st.get("name", ""))
    who = player_name.strip() or "The adventurer"
    if greeting:
        closing = (f"{who} has just walked up to you. Greet them, or say whatever you would "
                   "actually say to a stranger who approached you right now. ONE or TWO short "
                   "sentences. Do not ask how you can help them unless you are a merchant.\n")
    else:
        met = st.get("met", 0)
        again = (" You have spoken with them before.\n" if met > 1 else "\n")
        closing = (f'{who} says to you: "{says}"' + again +
                   "Reply in character, in ONE to THREE sentences. Answer what they actually asked.\n")
    return (head
            + _npc_identity(st, race, floor, map_name)
            + body
            + NPC_STANDING
            + _npc_memory_block(st)
            + limits
            + closing
            # LAST, after the closing instruction: position is a lever at 8B, and a resolved
            # haggle must beat the generic "reply in character" that precedes it.
            + haggle
            + 'Respond ONLY with JSON, no other text, like: {"speech": "your line"}')

# ---- Reply parsing -----------------------------------------------------------

# Invented names carry apostrophes and hyphens far more often than English words do
# (Zx'thal, Kha'zix, Mor-gath). A bare [a-zA-Z] class silently TRUNCATES them at the
# punctuation -- "I am Zx'thal" was yielding "Zx" -- so they are part of the name.
_NAME_WORD = r"[A-Z][a-zA-Z'\-]+(?:\s+[A-Z][a-zA-Z'\-]+)?"

NAME_PATTERNS = [
    rf"(?i:they call me|call me|i am called|my name is|i am|i'm|name'?s)\s+({_NAME_WORD})",
    rf"(?i:i go by|known as)\s+({_NAME_WORD})",
]

# The whole reply IS the name, with no introducing phrase. Asked "what is your name",
# a terse creature answers "Glim." and nothing else -- observed on slimes, rats and
# skeletons alike, and neither pattern above can match it.
BARE_NAME = re.compile(rf"^({_NAME_WORD})$")

# ...but only when the player actually asked. Without this gate the bare branch would
# take ANY short reply as the character's name ("Yes.", "Cheese!").
ASKED_NAME = re.compile(
    r"(?i:what(?:'?s| is| are)?\s+(?:you|your|yer)\s*(?:name|called)"
    r"|your\s+name"
    r"|who\s+are\s+you"
    r"|have\s+a\s+name"
    r"|call\s+you"
    r"|name\s+again)"
)

# Short capitalised words a creature might answer with that are plainly not names.
# Only consulted for the bare branch's benefit; the introducer patterns rarely need it.
NAME_REJECTS = (
    "name", "none", "null", "unknown", "adventurer",
    "yes", "no", "aye", "nay", "okay", "sure", "maybe", "perhaps", "indeed",
    "hello", "hi", "hey", "greetings", "well", "hmm", "what", "who", "why", "how",
    "nothing", "nobody", "someone", "silence", "never", "always",
    "master", "boss", "friend", "stranger", "human", "sir", "madam", "ma'am",
    "me", "you", "it", "they", "them", "mine", "ours",
    "squeak", "glub", "grunt", "growl", "hiss", "food", "meat", "cheese",
)

def _clean_name(nm):
    nm = nm.strip().strip('.,!?"\'')
    if not nm or nm.lower() in NAME_REJECTS or not (2 <= len(nm) <= 40):
        return ""
    return nm

def extract_name(raw, speech="", says=""):
    """Prefer an explicit JSON "name" field; else parse the speech. The fallback is
    ESSENTIAL -- the 8B reliably SAYS the name while omitting the field, and just as
    often says nothing BUT the name, which needs the player's question as context."""
    m = re.search(r'"name"\s*:\s*"([^"]{1,40})"', raw)
    if m and _clean_name(m.group(1)):
        return _clean_name(m.group(1))
    txt = (speech or "").replace("’", "'")   # normalize typographic apostrophes
    for pat in NAME_PATTERNS:
        mm = re.search(pat, txt)
        if mm and _clean_name(mm.group(1)):
            return _clean_name(mm.group(1))
    if ASKED_NAME.search((says or "").replace("’", "'")):
        bare = txt.strip().strip('*_"“”').strip()
        bare = re.sub(r"[.!?,]+$", "", bare).strip()
        mm = BARE_NAME.match(bare)
        if mm and _clean_name(mm.group(1)):
            return _clean_name(mm.group(1))
    return ""

def parse_reply(raw):
    """Robust parse: strict JSON first, then a regex fallback for mildly malformed
    model output (missing comma, unquoted action, preamble junk). Returns (speech, action)."""
    # 1. strict JSON (common, correct case)
    try:
        parsed = json.loads(raw[raw.find('{'): raw.rfind('}') + 1])
        sp = parsed.get('speech', '')
        if sp and sp.strip():
            return sp.strip(), str(parsed.get('action', 'NONE')).upper()
    except Exception:
        pass
    # 2. regex fallback: pull speech + action even if the JSON is malformed
    sp_m = (re.search(r'"speech"\s*:\s*"(.*?)"\s*[,}]?\s*"?action"?', raw, re.DOTALL)
            or re.search(r'"speech"\s*:\s*"(.+?)"', raw, re.DOTALL))
    if sp_m:
        ac_m = re.search(r'"action"\s*:\s*"?([A-Za-z]+)"?', raw)
        print('[SERVICE] (JSON malformed - recovered via fallback)')
        return sp_m.group(1).strip(), (ac_m.group(1).upper() if ac_m else 'NONE')
    # 3. total failure: strip JSON scaffolding, return raw-ish as speech
    fb = re.sub(r'[{}"]', '', raw).replace('speech:', '').replace('action:', '').strip()
    return (fb or '...'), 'NONE'

def ask_ollama(prompt):
    print(f"[SERVICE-DBG] prompt ~{len(prompt)//4} tokens")
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "keep_alive": "30m",
                       "options": {"num_ctx": 16384}}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["response"].strip()

def herx_secret_field():
    """What the C++ side reads. Herx keeps its original two-field shape for compatibility:
    "<debuff>:<uid>", where a false secret reports 0 and initLich applies nothing.

    The twins add a third and fourth: "<debuff>:<uid>:twins:<who>", where `who` is 1 for
    Erudyce and 2 for Orpheus, and ⚠ a NEGATIVE debuff is a BUFF -- a spy's lie at the endgame
    costs you rather than merely wasting your time."""
    # ⚠ The twins are checked FIRST. The field carries one secret, and Herx used to win it
    # forever -- so a successfully told endgame secret was silently discarded in exactly the
    # runs where the player had done the most work (two followers, both arcs earned).
    #
    # Ordering it this way is safe rather than merely preferable: the twins secret cannot be
    # told before floor 25 (TWINS_MIN_FLOOR) and floor 20 is Herx's boss floor on every route
    # to Hamlet, so Herx has already spawned -- and initLich has already read and applied his
    # debuff -- before this can ever switch over. The engine keeps the two in separate globals
    # (mymod_herx_debuff / mymod_twins_debuff, mymod.cpp:1721), so the value it latched for
    # Herx is not disturbed by the field changing shape afterwards.
    st = SECRET_STATE["twins"]
    if st["revealed"]:
        v = _herx_pool(st.get("is_false"), "twins")[st["variant"]]
        return "%d:%d:twins:%d" % (v["debuff"], st["uid"], v.get("who", 1))
    st = SECRET_STATE["herx"]
    if st["revealed"]:
        d = 0 if st.get("is_false") else HERX_SECRET_VARIANTS[st["variant"]]["debuff"]
        return "%d:%d" % (d, st["uid"])
    return ""

class Handler(http.server.BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # Client (the game) hung up before we replied. Harmless; don't crash the thread.
            print("[SERVICE] (client disconnected early - ignored)")

    def _send_json(self, obj, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(obj).encode())
        except (BrokenPipeError, ConnectionResetError):
            print("[SERVICE] (write failed, client gone - ignored)")

    def do_POST(self):
        try:
            _t_start = time.time()
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or "{}")

            # Mod-side / player-authored note straight into the timeline. The C++ hooks and
            # /ailog both come through here, so everything lands in one ordered file.
            note = data.get("log")
            if note:
                logrec(data.get("src") or "mod", msg=str(note)[:400],
                       player=data.get("player"), uid=data.get("uid"),
                       floor=data.get("floor"), map=data.get("map"))
                print(f"[{(data.get('src') or 'mod').upper()}] {str(note)[:200]}")
                return self._send_json({"ok": True})
            race = data.get("race", "monster")
            floor = data.get("floor", 0)
            says = data.get("says", "").strip()
            ambient = bool(data.get("ambient", False))
            taunt = bool(data.get("taunt", False))
            uid = int(data.get("uid", 0) or 0)
            # --- multiplayer routing ---
            # The HOST is the only machine that talks to this service. Clients relay their
            # utterances to the host over Barony's own netcode, and the host tags each
            # request with the originating player index so state stays per-player.
            player = int(data.get("player", 0) or 0)
            player_name = (data.get("player_name") or "").strip()
            party = max(1, int(data.get("party", 1) or 1))
            map_name = (data.get("map") or "").strip()
            # What the owning player is carrying. Only the engine knows it, and only the
            # service may decide whether it is enough -- the model cannot check a balance,
            # so affordability is resolved here like every other condition in this project.
            gold = int(data.get("gold", 0) or 0)
            # ⚠ NOT player_race -- see _kin_section. That field is the shapeshift form and drives
            # the comprehension filter; this is what the adventurer actually is.
            player_kind = (data.get("player_kind") or "").strip()
            _req_set(engine_name=(data.get("follower_name") or "").strip(),
                     floor=floor,
                     persist=int(data.get("persist", 0) or 0),
                     # ⚠ "voice" when this line was spoken rather than typed. A mangled
                     # transcript reads as the player saying something strange, and without
                     # this the log cannot tell a transcription failure from a model failure.
                     via=(data.get("via") or "typed").strip())
            # How this follower came to be, and the part of its identity that outlives this
            # body (see the origin section). Empty for an ordinary recruit.
            origin = (data.get("origin") or "").strip()
            origin_key = (data.get("origin_key") or "").strip()
            # Non-follower NPCs (townsfolk, merchants, named characters). "greeting" is the
            # line they say when a player first walks up and engages them.
            npc = bool(data.get("npc", False))
            greeting = bool(data.get("greeting", False))
            npc_name = (data.get("npc_name") or "").strip()
            npc_role = (data.get("npc_role") or "townsfolk").strip()
            # ⚠ NOT `or -1`: shop type 0 is "arms and armor", the commonest shop in the game,
            # and 0 is falsy -- so every weapon merchant was read as "not a shop at all" and
            # described as generic. Pre-existing; found because haggling never fired for them.
            _shop = data.get("shop", -1)
            npc_shop = int(_shop) if _shop not in (None, "") else -1
            # Item identification (spec 9). The engine supplies the real name and a few decoys
            # from the same category; the service picks which one is claimed.
            ident_req = data.get("identify") or None

            # Fire-and-forget event record (e.g. recruitment): no dialogue, just remember it.
            # Magazine refill for the dummybot heckler. Answered before any follower state is
            # touched: it is a batch of shouted lines about a nearby enemy, not a conversation,
            # and it belongs to no relationship.
            if data.get("heckle"):
                count = max(1, min(HECKLE_MAX_LINES, int(data.get("count", 8) or 8)))
                # Same comprehension rule as every other overheard line: a polymorphed player
                # who cannot understand a construct gets noises. Sampled at refill rather than
                # at each shout, so a mid-magazine polymorph is stale until the next refill.
                if not can_understand(data.get("player_race", ""), "dummybot"):
                    # Drawn without immediate repeats: twelve identical chimes in a row reads
                    # as a broken feature rather than as an untranslated one.
                    lines, last = [], ""
                    for _ in range(count):
                        n = noise_for("dummybot")
                        for _try in range(4):
                            if n != last:
                                break
                            n = noise_for("dummybot")
                        lines.append(n)
                        last = n
                    print(f"[SERVICE] -> heckle: {count} noises (player cannot understand it)")
                    return self._send_json({"lines": lines})
                _t0 = time.time()
                raw = ask_ollama(build_heckle_prompt(race, floor, count))
                lines = parse_heckle_lines(raw, count)
                print(f"[SERVICE] -> heckle x{len(lines)} at {race}: "
                      + " | ".join(lines[:3]))
                logrec("heckle", race=race, floor=floor, asked=count, got=len(lines),
                       gen_ms=int((time.time() - _t0) * 1000), sample=lines[:3])
                return self._send_json({"lines": lines})

            # ⚠ The easter egg: a rat brought all the way to the Baron. Answered before any
            # follower state is touched -- the speaker here is the BOSS, not a follower.
            if data.get("ratfear"):
                boss_name = (data.get("boss") or "the Baron").strip()
                # ⚠ The legend variant is a different scene: they have HEARD, and they concede.
                if data.get("legend"):
                    prompt_x = build_ratlegend_prompt(boss_name, race, floor, map_name,
                                                      (data.get("ratname") or "").strip())
                else:
                    prompt_x = build_ratfear_prompt(boss_name, race, floor, map_name)
                raw_x = ask_ollama(prompt_x)
                line_x = (raw_x or "").strip().strip('"').split("\n")[0][:200]
                # ⚠ A rat that kills the Baron has EARNED a name. Drawn from the ordinary pool
                # and committed to name_history like any other, so it will not be handed out
                # again in a later run -- this creature is going to be talked about.
                new_name = ""
                if data.get("ratunnamed") and not data.get("legend"):
                    with STATE_LOCK:
                        new_name = reserve_name("rat") or ""
                        if new_name:
                            commit_name(new_name)
                    if new_name:
                        print(f"[SERVICE] -> the rat is named {new_name}")
                print(f"[SERVICE] -> RATFEAR {boss_name}: {line_x}")
                logrec("ratfear", boss=boss_name, race=race, floor=floor, map=map_name,
                       player=player, legend=bool(data.get("legend")) or None,
                       ratname=(data.get("ratname") or new_name or None),
                       named_now=(new_name or None), reply=line_x[:200])
                tts_emit(line_x, race, uid, "npc", boss_name, player)
                return self._send_json({"reply": line_x, "action": "NONE",
                                        "ratname": new_name})

            # A follower reacting to something valuable the player just picked up. Short form:
            # no conversation state, no boons, no relationship movement -- it is a remark, not
            # an exchange, and it must not consume the chat cap or a boon slot.
            remark = (data.get("remark") or "").strip()
            if remark == "hunger":
                _state = (data.get("state") or "").strip()
                with STATE_LOCK:
                    st_h = get_follower_state(uid, race, player) if uid else {}
                _al = st_h.get("allegiance", "loyal")
                if _al == "spy":
                    _al = "loyal"
                prompt_h = build_hunger_prompt(_state, race, floor, map_name, _al,
                                               bool(data.get("blood")))
                if not prompt_h:
                    return self._send_json({"reply": "", "action": "NONE"})
                raw_h = ask_ollama(prompt_h)
                line_h = (raw_h or "").strip().strip('"').split("\n")[0][:200]
                print(f"[SERVICE] -> hunger ({_state}): {race}: {line_h}")
                logrec("remark", uid=uid, race=race, floor=floor, player=player, what="hunger",
                       tier=int(data.get("tier", -1) or -1),
                       state=_state, blood=bool(data.get("blood")) or None,
                       allegiance=st_h.get("allegiance"), reply=line_h[:200])
                tts_emit(line_h, race, uid, "remark", "", player)
                return self._send_json({"reply": line_h, "action": "NONE"})

            if remark in REMARK_SITUATION:
                with STATE_LOCK:
                    st_r = get_follower_state(uid, race, player) if uid else {}
                _al = st_r.get("allegiance", "loyal")
                if _al == "spy":
                    _al = "loyal"     # the disguise holds here too
                if remark in ("skillbest", "skillworst"):
                    prompt_r = build_skill_prompt(
                        remark, race, floor, map_name, _al,
                        (data.get("skill") or "").strip(),
                        (data.get("tier") or "").strip(),
                        st_r.get("friendship", 0))
                    if not prompt_r:
                        return self._send_json({"reply": "", "action": "NONE"})
                elif remark == "death":
                    prompt_r = build_death_prompt(race, floor, map_name, _al,
                                                  bool(data.get("own", True)),
                                                  bool(data.get("canreturn", False)))
                elif remark == "swimming":
                    prompt_r = build_swim_prompt(race, floor, map_name, _al,
                                                 (data.get("medium") or "water").strip(),
                                                 (data.get("hazard") or "").strip())
                else:
                    # A legendary piece gets its own situation, not the ordinary kit line.
                    if remark == "equip" and data.get("legendary"):
                        remark = "equiplegend"
                    prompt_r = build_remark_prompt(
                        remark, race, floor, map_name, _al,
                        {"look": (data.get("look") or "").strip(),
                         "slot": (data.get("slot") or "").strip(),
                         "band": (data.get("band") or "").strip(),
                         "effect": (data.get("effect") or "").strip(),
                         "blessing": (data.get("blessing") or "").strip(),
                         "victim": (data.get("victim") or "").strip(),
                         "nohunger": bool(data.get("nohunger"))})
                if not prompt_r:
                    return self._send_json({"reply": "", "action": "NONE"})
                raw_r = ask_ollama(prompt_r)
                line_r = (raw_r or "").strip().strip('"').split("\n")[0][:200]
                print(f"[SERVICE] -> remark {remark}: {race}: {line_r}")
                logrec("remark", uid=uid, race=race, floor=floor, player=player, what=remark,
                       tier=int(data.get("tier", -1) or -1),
                       allegiance=st_r.get("allegiance"), reply=line_r[:200])
                tts_emit(line_r, race, uid, "remark", "", player)
                return self._send_json({"reply": line_r, "action": "NONE"})

            if remark == "valuable":
                with STATE_LOCK:
                    st_v = get_follower_state(uid, race, player) if uid else {}
                _alleg = st_v.get("allegiance", "loyal")
                # ⚠ A spy reacts exactly as a loyal follower would; the disguise is the point.
                if _alleg == "spy":
                    _alleg = "loyal"
                prompt_v = build_valuable_prompt(
                    race, floor, map_name, (data.get("look") or "").strip(),
                    (data.get("category") or "").strip(),
                    int(data.get("value", 0) or 0), _alleg)
                raw_v = ask_ollama(prompt_v)
                line_v = (raw_v or "").strip().strip('"').split("\n")[0][:200]
                print(f"[SERVICE] -> valuable ({data.get('value')}): {race}: {line_v}")
                logrec("valuable", uid=uid, race=race, floor=floor, player=player,
                       value=int(data.get("value", 0) or 0),
                       look=(data.get("look") or "")[:60],
                       allegiance=st_v.get("allegiance"), reply=line_v[:200])
                tts_emit(line_v, race, uid, "valuable", "", player)
                return self._send_json({"reply": line_v, "action": "NONE"})

            # A countdown has started on this floor and the engine is asking whether this
            # follower heads it off. Answered before any conversation state is touched.
            favour = (data.get("favour") or "").strip()
            if favour:
                # The engine counts the gold lying on this floor and sends it, because only the
                # engine can know it -- that is what lets Sokoban be priced as a CUT of what he
                # recovers rather than a flat fee.
                gold_here = int(data.get("gold_here", 0) or 0)
                with STATE_LOCK:
                    st_g = get_follower_state(uid, race, player) if uid else {}
                    did = (favour_roll(favour, st_g, gold=gold, gold_here=gold_here,
                                       floor=floor, says=says) if st_g else False)
                    charge_g = str(LAST_CHARGE.pop(uid, "")) if uid else ""
                    quote_g = ""
                    if did in ("quote", "broke") and st_g.get("quote"):
                        quote_g = "%s:%d" % (favour, st_g["quote"]["price"])
                prompt_g = (build_guard_prompt(race, floor, map_name, did)
                            if favour == "minotaur"
                            else build_sokoban_prompt(race, floor, map_name, did))
                raw_g = ask_ollama(prompt_g)
                line = (raw_g or "").strip().strip('"').split("\n")[0][:200]
                # ⚠ "quote" and "broke" are truthy STRINGS. Acting on truthiness here would
                # have the engine perform the favour the instant he named his price.
                acted = did is True or did == "paid"
                print(f"[SERVICE] -> favour {favour}: {race} "
                      f"{'DID it' if acted else (did or 'did not')}: {line}")
                logrec("favour", uid=uid, race=race, floor=floor, player=player, what=favour,
                       did=acted or None, outcome=(did if isinstance(did, str) else None),
                       charge=(int(charge_g) if charge_g else None),
                       trust=st_g.get("trust") if st_g else None,
                       friendship=st_g.get("friendship") if st_g else None, reply=line[:200])
                tts_emit(line, race, uid, "favour", "", player)
                return self._send_json({"reply": line, "act": "1" if acted else "0",
                                        "charge": charge_g, "quote": quote_g})

            evt = data.get("event", "")
            if evt:
                with STATE_LOCK:
                    if evt == "new_run":
                        reset_run()
                        logrec("new_run")
                    elif uid:
                        recorded = record_event(uid, race, evt, floor, player, origin,
                                                origin_key, int(data.get("about", 0) or 0))
                        # Only log an event that actually happened: logreview counts these, and
                        # a suppressed one showing up would misreport the run.
                        if recorded:
                            st_e = follower_state.get(uid, {})
                            logrec("event", event=evt, uid=uid, race=race, floor=floor,
                                   player=player,
                                   dims={d: st_e.get(d, 0) for d in DIMENSIONS} if st_e else None)
                return self._send_json({"ok": True})

            # Resolve the follower's state row BEFORE anything reads it. Every helper below is
            # uid-keyed, and this is the one place that knows the origin -- so a resummoned
            # knight gets pointed at its existing relationship here, once, rather than each
            # call site needing to care.
            if uid and not npc:
                with STATE_LOCK:
                    get_follower_state(uid, race, player, origin, origin_key)
                    flush_resummon(uid, race, floor, player)

            # TEST-ONLY: force follower state, so the Herx gates can be reached without
            # playing to friendship 50. Guarded by an env var so it cannot exist in a release.
            if os.environ.get("BARONY_AI_TESTHOOK") and data.get("force") and uid:
                with STATE_LOCK:
                    _fs = get_follower_state(uid, race, player)
                    _fs.update(data["force"])
                # Traits the ENGINE has to act on, and only those. Allegiance itself stays
                # server-side -- the engine has no business knowing who the spy is, and its logs
                # are visible. "fearful" is sent because cowardice has to trigger on HP, which
                # only the engine sees.
                traits = ""
                if uid:
                    _st = follower_state.get(uid, {})
                    if _st.get("allegiance") == "fearful":
                        traits = "fearful"
                return self._send_json({"ok": True, "traits": traits})

            # Comprehension filter: if the player can't understand this speaker, return noises.
            # (Applies to overheard/ambient + taunts; /aicommand sends no player_race so passes through.)
            player_race = data.get("player_race", "")
            if (ambient or taunt) and not can_understand(player_race, race):
                noise = noise_for(race)
                print(f"[SERVICE] {race} (unintelligible to {player_race or 'player'}) -> {noise}")
                tts_emit(noise, race, uid, "noise", player=player)
                return self._send_json({"reply": noise, "action": "NONE"})

            # Prompt assembly mutates run state (boon rolls, Herx offers), so it takes the
            # lock; the generation that follows deliberately runs outside it.
            with STATE_LOCK:
                if taunt:
                    prompt = build_taunt_prompt(race, floor, map_name)
                elif ambient:
                    prompt = build_ambient_prompt(race, floor, data.get("relation", "hostile"),
                                                  map_name)
                elif npc:
                    if uid:
                        get_npc_state(uid, race, npc_name, npc_role, npc_shop, floor)["met"] += 1
                    # Merchants only: a townsperson has no prices to argue about. Resolved
                    # BEFORE generation so the model is told the outcome rather than asked for
                    # one, and so the engine and the reply can never disagree.
                    haggle_sec, haggle_field = "", ""
                    if uid and npc_shop >= 0 and not greeting:
                        with STATE_LOCK:
                            st_m = get_npc_state(uid, race, npc_name, npc_role, npc_shop, floor)
                            tone = merchant_tone(st_m, says)
                            if wants_to_haggle(says):
                                _esc = follower_state.get(int(data.get("escort", 0) or 0), {})
                                outcome, deal = haggle_attempt(st_m, floor, _esc)
                                haggle_sec = haggle_section(outcome, deal,
                                                            merc=(_esc.get("allegiance")
                                                                  == "self_interested"))
                                haggle_field = "%u:%d" % (uid, deal)
                                print(f"[SERVICE-DBG] haggle {uid}: {outcome} "
                                      f"(regard {st_m.get('regard', 0)}, deal {deal:+d}%)")
                                logrec("haggle", uid=uid, race=race, player=player, floor=floor,
                                       outcome=outcome, deal_pct=deal,
                                       regard=st_m.get("regard", 0), tone=tone or None)
                    prompt = build_npc_prompt(race, floor, says, uid, player, player_name,
                                              haggle_sec, npc_name, npc_role, npc_shop,
                                              map_name, greeting)
                else:
                    ident_line, ident_truthful, ident_kind = None, False, ""
                    if ident_req and uid:
                        st_i = get_follower_state(uid, race, player)
                        # ⚠ A mercenary quotes before he appraises. Unlike the two favours,
                        # ASKING AGAIN is the acceptance here: the player typed /aiidentify,
                        # saw the price, and typed it again. That keeps the whole exchange in
                        # one turn each way and needs no cross-turn item state -- the item is
                        # simply left unidentified by the quote turn, which it would be anyway.
                        _ivalue = int(ident_req.get("value", 0) or 0)
                        _icat = (ident_req.get("category") or "").lower()
                        _paid_line = ""
                        _skip_ident = False
                        # ⚠ Out of his depth means he does not take the job, and is NOT paid for
                        # it. Quoting and charging for an appraisal he cannot make would be the
                        # worst version of this feature: you hand over real money for "it is
                        # beyond me". He says so for free.
                        if is_merc(st_i) and identify_beyond(st_i, race, _icat, _ivalue):
                            ident_line = _identify_section("beyond", "",
                                                           ident_req.get("unid") or "thing")
                            _skip_ident = True
                            logrec("merc", uid=uid, what="identify", stage="beyond",
                                   value=_ivalue, floor=floor)
                        elif is_merc(st_i):
                            _p = merc_price("identify", floor, _ivalue)
                            _q = st_i.get("quote")
                            _asked_again = bool(_q and _q.get("kind") == "identify")
                            if not merc_settled(st_i, "identify") and not _asked_again:
                                merc_quote(st_i, "identify", floor, _ivalue)
                                LAST_QUOTE[uid] = "identify:%d" % _p
                                ident_line = merc_quote_section("identify")
                                _skip_ident = True
                                logrec("merc", uid=uid, what="identify", stage="quoted",
                                       price=_p, floor=floor)
                            elif not merc_can_afford(gold, _p):
                                ident_line = merc_broke_section("identify")
                                _skip_ident = True
                                logrec("merc", uid=uid, what="identify", stage="broke",
                                       price=_p, gold=gold)
                            else:
                                merc_agree(st_i, "identify", _p)
                                LAST_CHARGE[uid] = _p
                                _paid_line = merc_paid_section("identify")
                                logrec("merc", uid=uid, what="identify", stage="hired",
                                       price=_p, gold=gold, floor=floor)
                        if not _skip_ident:
                            ident_kind, claim, ident_truthful = identify_attempt(
                                st_i, race, _icat,
                                ident_req.get("real") or "", ident_req.get("decoys") or [],
                                _ivalue)
                            ident_line = _paid_line + _identify_section(
                                ident_kind, claim, ident_req.get("unid") or "thing")
                            if ident_kind == "correct":
                                adjust(st_i, respect=2)   # they know their business
                            print(f"[SERVICE-DBG] identify {ident_req.get('real')!r} -> "
                                  f"{ident_kind} claim={claim!r} truthful={ident_truthful}")
                    prompt = build_prompt(race, floor, says, uid, player, player_name, party,
                                          map_name, ident_line, gold, player_kind)
            who = player_name or f"player{player}"
            kind = "NPC" if npc else "follower"
            print(f"[SERVICE] {race} floor {floor} [{kind}] ({who})")

            _t_prompt = time.time()
            raw = ask_ollama(prompt)
            _gen_ms = int((time.time() - _t_prompt) * 1000)
            speech, action = parse_reply(raw)
            # Only the JSON-shaped paths can be "malformed". Taunts and ambient babble are
            # asked for as a bare spoken line, so flagging them here produced a fake
            # malformed-JSON warning on every single one.
            _recovered = (not taunt and not ambient
                          and speech and raw and not raw.strip().startswith("{"))
            if not speech or not speech.strip():
                speech = "..."  # model declined; show a beat, not nothing
            if action not in VALID_ACTIONS:
                action = "NONE"
            # A turret cannot take a movement order however it phrases the reply. Forced here
            # as well as forbidden in the prompt, because the action drives a real ALLY_CMD.
            if is_emplacement(race):
                action = "NONE"

            # NPCs keep their own light memory and never touch follower state.
            if npc:
                with STATE_LOCK:
                    if uid:
                        st = get_npc_state(uid, race, npc_name, npc_role, npc_shop, floor)
                        if not greeting:
                            record_npc_exchange(st, player_name, says, speech)
                        else:
                            record_npc_exchange(st, player_name, "(walked up to you)", speech)
                print(f"[SERVICE] -> npc speech={speech}")
                tts_emit(speech, race, uid, "npc", npc_name, player)
                logrec("npc", uid=uid, race=race, npc_name=npc_name, role=npc_role,
                       shop=(npc_shop if npc_shop >= 0 else None), floor=floor, map=map_name,
                       player=player, greeting=greeting or None, says=says[:160],
                       reply=speech[:400], gen_ms=_gen_ms, prompt_chars=len(prompt))
                return self._send_json({"reply": speech, "action": "NONE", "name": npc_name,
                                        "player": player, "secret": "", "boon": "",
                                        "haggle": haggle_field})

            name, secret, boon, sabotage_field = "", "", "", ""
            charge_field = favour_field = quote_field = hint_field = ""
            with STATE_LOCK:
                if uid:
                    st = get_follower_state(uid, race, player)
                    revealed = ("" if st.get("name")
                                else resolve_revealed_name(st, raw, speech, says))
                    if revealed:
                        st["name"] = revealed
                        commit_name(revealed)   # persisted, so the NEXT run won't reuse it
                        _own = (revealed.lower() != st.get("assigned_name", "").lower())
                        print(f"[SERVICE-DBG] follower {uid} is now named '{revealed}'"
                              + (" (self-chosen, not the reserved name)" if _own else ""))
                        logrec("named", uid=uid, race=race, player=player, name=revealed,
                               assigned=st.get("assigned_name") or None,
                               self_chosen=(True if _own else None))
                    name = st.get("name", "")
                    herx_detect(uid, raw, speech, player)
                    record_follower_interaction(uid, says, floor, player, speech)
                    boon = LAST_BOON.pop(uid, "")
                    sabotage_field = LAST_SABOTAGE.pop(uid, "")
                    charge_field = str(LAST_CHARGE.pop(uid, "") or "")
                    favour_field = LAST_FAVOUR.pop(uid, "")
                    quote_field = LAST_QUOTE.pop(uid, "")
                    hint_field = LAST_HINT.pop(uid, "")
                    print(f"[SERVICE-DBG] follower {uid} (player{player}) {dims_summary(st)}")
                secret = herx_secret_field()
            print(f"[SERVICE] -> action={action} speech={speech}")
            _st = follower_state.get(uid, {}) if uid else {}
            _kind = "taunt" if taunt else ("ambient" if ambient else "reply")
            tts_emit(speech, race, uid, _kind, name, player)
            _open = [n for n, g, _ in DISCLOSURE_TIERS if _st and g(_st)]
            _tens = len([1 for c, _x in DIMENSION_TENSIONS if _st and c(_st)])
            logrec(_kind, uid=uid, race=race, floor=floor, map=map_name, player=player,
                   via=(_req("via") if _req("via") != "typed" else None),
                   says=says[:400], reply=speech[:400], action=action, name=name,
                   allegiance=(_st.get("allegiance") if _st else None),
                   open_tiers=(len(_open) if _st else None),
                   tensions=(_tens or None) if _st else None,
                   gen_ms=_gen_ms, prompt_chars=len(prompt),
                   secret=(secret or None), boon=(boon or None),
                   identify=(ident_kind or None) if ident_req else None,
                   recovered=(True if _recovered else None),
                   dims={d: _st.get(d, 0) for d in DIMENSIONS} if _st else None)
            self._send_json({"reply": speech, "action": action, "name": name, "player": player,
                             "secret": secret, "boon": boon, "sabotage": sabotage_field,
                             # The mercenary's fee, the job the engine should now actually
                             # perform because he was just hired for it, and a price for the
                             # engine to PRINT -- the model is forbidden from saying a number.
                             "charge": charge_field, "favour": favour_field,
                             "quote": quote_field,
                             # The engine owns the command names, so it prints them; the
                             # service only decides WHEN. Same split as the price figures.
                             "hint": hint_field,
                             # "1" ONLY when the claim was both correct and honest -- this is what
                             # actually sets item->identified engine-side.
                             "identify": ("1" if (ident_req and ident_truthful) else "0")})
        except Exception as e:
            print(f"[SERVICE] ERROR: {e}")
            logrec("error", err=str(e)[:300], where=traceback.format_exc(limit=3)[-400:])
            self._send_json({"reply": f"(service error: {e})"}, status=500)

    def log_message(self, *a): pass  # quiet default logging

if __name__ == "__main__":
    if "--names" in sys.argv:
        sys.exit(name_report())
    print(f"[SERVICE] Barony AI service on :{PORT}, model={MODEL}, lore loaded ({len(LORE)} sections)")
    print(f"[SERVICE] session log -> {LOG_PATH}")
    logrec("start", model=MODEL, port=PORT, session=SESSION_ID)
    # Threaded: in co-op the host serves several players plus fire-and-forget event
    # records. A single-threaded server would make every player queue behind whoever
    # is mid-generation. Bound to loopback only -- clients never talk to this service,
    # they talk to the host's game, so co-op needs no firewall or port-forward setup.
    class ThreadedServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    with ThreadedServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.serve_forever()
