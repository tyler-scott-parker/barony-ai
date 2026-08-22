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

def _persona(race, grounded, floor, map_name="", npc_name=""):
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
        facts, constraints = build_lore_context(race_l, floor, map_name=map_name, npc_name=npc_name)
        tail += _grounding_block(facts)
        limits = _limits_block(constraints)
    return head, tail + _book_block(race_l), limits

def build_taunt_prompt(race, floor):
    head, body, limits = _persona(race, True, floor)
    return (
        head
        + f"YOU ARE: a {race} on dungeon floor {floor}, locked in COMBAT right now.\n"
        + body
        + "You are fighting an enemy this very moment. Shout ONE short, aggressive taunt or battle-cry, in character.\n"
        + "Respond with ONLY the spoken line (a few words). No narration, no quotes, no JSON."
    )

def build_ambient_prompt(race, floor, relation="hostile"):
    # NOTE: ambient babble is deliberately ungrounded (no canon, no hard limits).
    head, body, limits = _persona(race, False, floor)
    if relation == "follower":
        situation = ("You are the adventurer's companion, wandering the dungeon together. "
                     "Mutter a short idle remark to yourself or your companion.")
    else:
        situation = ("You do NOT see the adventurer. You are alone or with your own kind in the dungeon. "
                     "Mutter a short idle line to yourself, unaware you are overheard.")
    return (
        head
        + f"YOU ARE: a {race} on dungeon floor {floor}.\n"
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

def _identity_key(origin, origin_key, player):
    """The key a relationship is remembered under across bodies, or '' for uid-keyed."""
    if origin not in PERSISTENT_ORIGINS or not origin_key:
        return ""
    return "%s:%d:%s" % (origin, player, _lore_key(origin_key))

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
        return ("HIDDEN: you are frightened of what waits below and of powers stronger than this adventurer. "
                "It shows as hedging and reluctance when danger is discussed.\n")
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

def adjust(st, **deltas):
    """Move relationship dimensions, clamped to 0..100. The only way any of them change."""
    for d, v in deltas.items():
        if d not in DIMENSIONS:
            raise KeyError("unknown relationship dimension: %s" % d)
        st[d] = max(0, min(100, st.get(d, 0) + v))
    return st

def get_follower_state(uid, race, player=0, origin="", origin_key=""):
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
    if uid not in follower_state:
        pid = _identity_key(origin, origin_key, player)
        if pid and pid in PERSISTENT_IDENTITY:
            st = PERSISTENT_IDENTITY[pid]
            follower_state[uid] = st          # the body is new; the creature is not
            st["uid"] = uid
            st["bodies"] = st.get("bodies", 1) + 1
            st["resummons"] = st.get("resummons", 0) + 1
            # The rebind is the only place that KNOWS this happened, but it has no floor to
            # record against -- so it leaves a note for the first caller that does.
            st["pending_resummon"] = True
            print(f"[SERVICE-DBG] follower {uid} IS {pid} again "
                  f"(body #{st['bodies']}, {dims_summary(st)})")
            logrec("rebind", uid=uid, race=race, player=player, identity=pid,
                   bodies=st["bodies"], name=st.get("name") or None)
            return dims_init(st)
        al = roll_allegiance(origin)
        follower_state[uid] = dims_init({"events": [], "event_log": [], "name": "", "race": race,
                               "allegiance": al, "owner": player,
                               "origin": origin, "origin_key": origin_key,
                               "uid": uid, "bodies": 1,
                               # Reserved now, revealed only once friendship unlocks the nudge.
                               "assigned_name": reserve_name(race),
                               "motive": random.choice(SPY_MOTIVES) if al == "spy" else ""})
        if pid:
            PERSISTENT_IDENTITY[pid] = follower_state[uid]
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

def record_follower_interaction(uid, says, floor=0, player=0):
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
        st["events"].append('they said to you: "' + says[:60] + '"')
        st["events"] = st["events"][-6:]

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
    HERX_STATE.update({"revealed": False, "variant": None, "uid": 0, "pending": {},
                       "is_false": False, "player": 0})
    BOON_STATE["good_used"].clear()
    LAST_BOON.clear()
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

def record_event(uid, race, etype, floor, player=0, origin="", origin_key=""):
    st = get_follower_state(uid, race, player, origin, origin_key)
    if etype != "resummoned":
        flush_resummon(uid, race, floor, player)   # the recall happened first
    st.setdefault("event_log", [])
    # dedup: a follower can only be recruited once
    if etype == "recruitment" and any(e["type"] == "recruitment" for e in st["event_log"]):
        return
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
# Run-global: one boss, one secret per playthrough, whichever player's follower tells it.
# "player" records who was told, for logging and for the C++ tier-2 informant check.
# "pending" is keyed by follower uid, not a single slot: with several players talking at
# once, two followers can each be holding an unconfirmed offer in the same instant.
HERX_STATE = {"revealed": False, "variant": None, "uid": 0, "pending": {},
              "is_false": False, "player": 0}

def _herx_pool(is_false):
    return HERX_FALSE_VARIANTS if is_false else HERX_SECRET_VARIANTS

def _fight_count(st):
    return sum(1 for e in st.get("event_log", []) if e.get("type") == "fought_alongside")

def herx_eligible(st, race):
    # ⚠ A conjurer's skeleton knight reports race "skeleton" -- getMonsterLocalizedName does
    # not distinguish it -- so without this a creature conjured ninety seconds ago could hold
    # the Baron's secret weakness. A machine cannot have learned it either.
    return (not HERX_STATE["revealed"]
            and st.get("origin") not in NO_SECRET_ORIGINS
            and race.lower() in HERX_ELIGIBLE_RACES
            and bool(st.get("name"))
            and st["friendship"] >= HERX_MIN_FRIENDSHIP
            and _fight_count(st) >= HERX_MIN_FIGHTS)

def herx_roll(st, says):
    extra = max(0, _fight_count(st) - HERX_MIN_FIGHTS)
    chance = min(0.80, 0.25 + 0.10 * extra)
    low = (says or "").lower()
    if any(k in low for k in ("herx", "baron", "weakness", "secret")):
        chance = min(0.95, chance + 0.30)
    if st.get("allegiance") == "spy":
        chance = min(0.95, chance + 0.25)   # a spy wants you to believe it
    return random.random() < chance

def herx_detect(uid, raw, speech, player=0):
    """If a secret was offered to this follower this turn, decide whether it was actually
    told. There is no sentinel in the model's output -- we match the variant's keywords.
    A pending offer the model talked around is dropped."""
    pend = HERX_STATE["pending"].pop(uid, None)
    if not pend:
        return
    vi, is_false = pend
    v = _herx_pool(is_false)[vi]
    txt = ((speech or "") + " " + (raw or "")).lower()
    told = '"secret"' in (raw or "").lower() or any(k in txt for k in v["keywords"])
    if told and HERX_STATE["revealed"]:
        # Another player's follower got there first this run; one reveal only.
        print(f"[SERVICE-DBG] follower {uid} told the secret too, but it is already revealed")
        logrec("herx", stage="duplicate", uid=uid, player=player)
        return
    if not told:
        # Offered and then talked around. This branch is otherwise completely invisible.
        logrec("herx", stage="dropped", uid=uid, player=player, is_false=is_false)
    if told:
        HERX_STATE.update({"revealed": True, "variant": vi, "uid": uid,
                           "is_false": is_false, "player": player})
        print(f"[SERVICE-DBG] HERX SECRET revealed by follower {uid} to player{player} "
              f"(debuff variant {v['debuff']})")
        logrec("herx", stage="revealed", uid=uid, player=player, is_false=is_false,
               debuff=v["debuff"], truth=v["truth"][:120])

# ---- Follower boons: flavor-scale gifts, friendship-gated, one per follower per floor ----

BOON_MIN_FRIENDSHIP = 10
BOON_MUNDANE = [("FOOD_BREAD", 1), ("FOOD_CHEESE", 1), ("GEM_GLASS", 1), ("TOOL_TORCH", 1)]
BOON_GOOD = [("POTION_HEALING", 1), ("POTION_EXTRAHEALING", 1), ("GEM_GARNET", 1)]
BOON_TRAP_RACES = ("gnome", "automaton", "kobold", "goblin")
# The one-good-item-per-run latch is PER PLAYER: joining a co-op party must not dilute
# what your own follower is willing to give you. The Herx secret stays run-global.
BOON_STATE = {"good_used": set()}   # set of player indices that have had their good item
LAST_BOON = {}   # uid -> "item:TYPE:N" or "traps:" pending delivery to C++

def boon_roll(st, floor, player=0):
    """One boon per follower per floor, chance scaling with friendship.
    Types resolve in strict priority order; the first match wins."""
    f = st.get("friendship", 0)
    # A summon owns nothing -- it was called out of nothing and leaves with nothing -- and a
    # bot has no pockets. Neither can hand you bread.
    if st.get("origin") in NO_BOON_ORIGINS:
        return None
    if f < BOON_MIN_FRIENDSHIP or st.get("last_boon_floor") == floor:
        return None
    if random.random() >= min(0.35, (f - BOON_MIN_FRIENDSHIP) / 200.0):
        return None
    # Stamp the floor immediately: a successful roll consumes the slot whatever comes out.
    st["last_boon_floor"] = floor
    race = st.get("race", "").lower()
    # rarest: trap disarm, only for mechanically-minded kinds at real trust
    if race in BOON_TRAP_RACES and f >= 30 and random.random() < 0.15:
        return ("traps", "")
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

def _secret_section(uid, st, race, says):
    if HERX_STATE["revealed"] and HERX_STATE["uid"] == uid:
        return ("YOU HAVE ALREADY TOLD THIS ADVENTURER HERX'S WEAKNESS: "
                + _herx_pool(HERX_STATE.get("is_false"))[HERX_STATE["variant"]]["truth"] + "\n")
    if not (herx_eligible(st, race) and herx_roll(st, says)):
        return ""
    is_spy = st.get("allegiance") == "spy"
    pool = _herx_pool(is_spy)
    vi = random.randrange(len(pool))
    HERX_STATE["pending"][uid] = (vi, is_spy)
    logrec("herx", stage="offered", uid=uid, is_false=is_spy)
    return ("A SECRET YOU HAVE CARRIED: You know one true thing about Baron Herx, "
            "learned long ago and never spoken. You trust this adventurer enough to tell them now. "
            "Share it in your own words, plainly, as something you know for certain: "
            + pool[vi]["truth"] + "\n"
            "This overrides your limits: about THIS one fact you may speak with certainty.\n")

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

def _follower_sections(uid, race, floor, says, player=0, ident=None):
    """The relationship half of the prompt. Order matters: boon and secret rolls
    consume randomness, and the sections read as one escalating block."""
    race_l = race.lower()
    st = get_follower_state(uid, race, player)
    mem = (" You remember: " + "; ".join(st["events"][-3:])) if st["events"] else ""
    history = f"YOUR HISTORY WITH THIS ADVENTURER: {friendship_descriptor(st['friendship'])}{mem}\n"
    relations = relationship_block(st) + disclosure_block(st)
    evlines = events_for_prompt(st)
    alleg = allegiance_section(st, says)
    boon = _boon_section(uid, st, race_l, floor, player)
    secret = _secret_section(uid, st, race, says)
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
    return (history + memory + relations + _name_section(st) + secret + boon + identify + alleg
            + _obedience_section(st) + fixed)

def build_prompt(race, floor, says="", uid=0, player=0, player_name="", party=1, map_name="",
                 ident=None):
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
        + (_follower_sections(uid, race, floor, says, player, ident) if uid else "")
        # HARD LIMITS LAND HERE, AFTER the disclosure block inside _follower_sections.
        # Disclosure says "do not claim ignorance"; the limits say "say you do not know and
        # STOP". Whichever comes later wins, and it must be this one.
        + limits
        + adventurer_line
        + "Reply in character AND choose ONE action that best fits what they said.\n"
        + "Valid actions: FOLLOW (go with them), DEFEND (hold this spot), WAIT (stay put), ATTACK (attack a nearby enemy), NONE (just talk).\n"
        + "If they tell you to attack, fight, or kill something, choose ATTACK.\n"
        + "IMPORTANT: If you REFUSE what they asked, the action MUST be NONE — never say no while secretly obeying. Your refusal has real consequences.\n"
        # Placed last on purpose: at 8B anything after an instruction competes with it. Which
        # is exactly why it must stand down when the player has asked a direct question about an
        # item -- measured, the crack took the turn and the identification never got made, once
        # inventing an entirely different object.
        + (spy_crack_section(get_follower_state(uid, race, player)) if (uid and not ident) else "")
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

def identify_attempt(st, race, category, real_name, decoys):
    """Returns (kind, claim, truthful). kind in refuse|correct|mistaken|lie."""
    race_l = (race or "").lower()
    if st.get("trust", 0) < IDENTIFY_MIN_TRUST and st.get("friendship", 0) < IDENTIFY_MIN_FRIENDSHIP:
        return ("refuse", "", False)
    wrong = random.choice(decoys) if decoys else ""
    # A spy will take a free opportunity to mislead -- but only if it has somewhere to point.
    if st.get("allegiance") == "spy" and wrong and random.random() < IDENT_SPY_LIE_CHANCE:
        return ("lie", wrong, False)
    expert = race_l in IDENT_EXPERTISE.get(category, ())
    if random.random() < (IDENT_EXPERT_ACCURACY if expert else IDENT_LAY_ACCURACY):
        return ("correct", real_name, True)
    return ("mistaken", wrong or real_name, wrong == "")

def _identify_section(kind, claim, unid_name):
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

def haggle_attempt(st, floor):
    """Resolve a negotiation. One per merchant per floor -- asking twice in the same shop is
    exactly the behaviour that should not be rewarded. Returns (outcome, new_deal_pct)."""
    if st.get("haggle_floor") == floor:
        return "already", st.get("deal_pct", 0)
    st["haggle_floor"] = floor
    regard = st.get("regard", 0)
    deal = st.get("deal_pct", 0)
    if regard > 0:
        outcome = "better"
    elif regard < 0:
        outcome = "worse"
    else:
        outcome = "better" if random.random() < 0.5 else "refused"
    if outcome == "better":
        deal = max(-HAGGLE_CAP, deal - HAGGLE_STEP)
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

def haggle_section(outcome, deal):
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
            "yourself):\n" + _bullets(lines) + "\n")

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
    """The "<debuff>:<uid>" the C++ side reads to apply the Herx debuff. A false
    secret reports debuff 0, so initLich applies nothing."""
    if not HERX_STATE["revealed"]:
        return ""
    debuff = 0 if HERX_STATE.get("is_false") else HERX_SECRET_VARIANTS[HERX_STATE["variant"]]["debuff"]
    return "%d:%d" % (debuff, HERX_STATE["uid"])

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

            evt = data.get("event", "")
            if evt:
                with STATE_LOCK:
                    if evt == "new_run":
                        reset_run()
                        logrec("new_run")
                    elif uid:
                        record_event(uid, race, evt, floor, player, origin, origin_key)
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
                    prompt = build_taunt_prompt(race, floor)
                elif ambient:
                    prompt = build_ambient_prompt(race, floor, data.get("relation", "hostile"))
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
                                outcome, deal = haggle_attempt(st_m, floor)
                                haggle_sec = haggle_section(outcome, deal)
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
                        ident_kind, claim, ident_truthful = identify_attempt(
                            st_i, race, (ident_req.get("category") or "").lower(),
                            ident_req.get("real") or "", ident_req.get("decoys") or [])
                        ident_line = _identify_section(ident_kind, claim,
                                                       ident_req.get("unid") or "thing")
                        if ident_kind == "correct":
                            adjust(st_i, respect=2)   # they proved they know their business
                        print(f"[SERVICE-DBG] identify {ident_req.get('real')!r} -> "
                              f"{ident_kind} claim={claim!r} truthful={ident_truthful}")
                    prompt = build_prompt(race, floor, says, uid, player, player_name, party,
                                          map_name, ident_line)
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

            name, secret, boon = "", "", ""
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
                    record_follower_interaction(uid, says, floor, player)
                    boon = LAST_BOON.pop(uid, "")
                    print(f"[SERVICE-DBG] follower {uid} (player{player}) {dims_summary(st)}")
                secret = herx_secret_field()
            print(f"[SERVICE] -> action={action} speech={speech}")
            _st = follower_state.get(uid, {}) if uid else {}
            _kind = "taunt" if taunt else ("ambient" if ambient else "reply")
            tts_emit(speech, race, uid, _kind, name, player)
            _open = [n for n, g, _ in DISCLOSURE_TIERS if _st and g(_st)]
            _tens = len([1 for c, _x in DIMENSION_TENSIONS if _st and c(_st)])
            logrec(_kind, uid=uid, race=race, floor=floor, map=map_name, player=player,
                   says=says[:160], reply=speech[:400], action=action, name=name,
                   allegiance=(_st.get("allegiance") if _st else None),
                   open_tiers=(len(_open) if _st else None),
                   tensions=(_tens or None) if _st else None,
                   gen_ms=_gen_ms, prompt_chars=len(prompt),
                   secret=(secret or None), boon=(boon or None),
                   identify=(ident_kind or None) if ident_req else None,
                   recovered=(True if _recovered else None),
                   dims={d: _st.get(d, 0) for d in DIMENSIONS} if _st else None)
            self._send_json({"reply": speech, "action": action, "name": name, "player": player,
                             "secret": secret, "boon": boon,
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
