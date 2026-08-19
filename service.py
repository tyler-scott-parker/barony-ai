"""Barony AI-NPC service: owns lore, prompts, and within-run social state.

The game (thin C++ hooks) POSTs here; we build a prompt, ask a local Ollama model,
and reply with {reply, action, name, secret, boon}. All state is per-playthrough
and lives in RAM -- `new_run` clears it.
"""
import json, http.server, socketserver, urllib.request, os, random, re, threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_json(name):
    with open(os.path.join(BASE_DIR, name)) as f:
        return json.load(f)

# Env overrides so a released build doesn't need this file edited.
OLLAMA_URL = os.environ.get("BARONY_AI_OLLAMA", "http://localhost:11434/api/generate")
MODEL      = os.environ.get("BARONY_AI_MODEL", "llama3.1:8b")
PORT       = int(os.environ.get("BARONY_AI_PORT", "5001"))
BOOKS_DIR  = os.environ.get("BARONY_AI_BOOKS",
    "/home/tyler/.local/share/Steam/steamapps/common/Barony/books")

LORE          = _load_json("barony_lore.json")        # world.setting only; see FULL for the rest
RACE_LORE     = _load_json("race_lore.json")
RACE_BOOKS    = _load_json("race_books.json")
COMPREHENSION = _load_json("comprehension.json")
FULL          = _load_json("barony_lore_full.json")   # researched lore (v19), 45 sections

WORLD = LORE["world"]["setting"]
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
    noises = COMPREHENSION.get("noises", {})
    pool = noises.get(speaker_race.lower()) or noises.get("default_beast", ["*unintelligible noises*"])
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

def _persona(race, grounded, floor, map_name=""):
    """The opening every prompt shares: setting, character guidance, and
    (except for ambient babble) canonical grounding + hard limits."""
    race_l = race.lower()
    slice_ = RACE_LORE.get(race_l, RACE_LORE.get("default", "A creature of the dungeon."))
    head = f"SETTING: {WORLD}\n"
    tail = f"CHARACTER GUIDANCE: {slice_}\n"
    if grounded:
        facts, constraints = build_lore_context(race_l, floor, map_name=map_name)
        tail += _grounding_block(facts) + _limits_block(constraints)
    return head, tail + _book_block(race_l)

def build_taunt_prompt(race, floor):
    head, body = _persona(race, True, floor)
    return (
        head
        + f"YOU ARE: a {race} on dungeon floor {floor}, locked in COMBAT right now.\n"
        + body
        + "You are fighting an enemy this very moment. Shout ONE short, aggressive taunt or battle-cry, in character.\n"
        + "Respond with ONLY the spoken line (a few words). No narration, no quotes, no JSON."
    )

def build_ambient_prompt(race, floor, relation="hostile"):
    # NOTE: ambient babble is deliberately ungrounded (no canon, no hard limits).
    head, body = _persona(race, False, floor)
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

def roll_allegiance():
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

def get_follower_state(uid, race, player=0):
    """State is keyed by follower UID, which Barony assigns server-side and replicates,
    so it is already unique across every player in a multiplayer run. `player` is the
    owning player index (0 = host); it scopes per-player latches like boons."""
    if uid not in follower_state:
        al = roll_allegiance()
        follower_state[uid] = dims_init({"events": [], "event_log": [], "name": "", "race": race,
                               "allegiance": al, "owner": player,
                               "motive": random.choice(SPY_MOTIVES) if al == "spy" else ""})
        print(f"[SERVICE-DBG] follower {uid} allegiance={al} owner=player{player}")
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
    "fear":       ((60, "You avoid contradicting them outright. You agree faster than you mean it, "
                        "and you choose safe words when they are angry."),
                   (30, "You are careful around them. You soften your disagreements.")),
    "resentment": ((60, "You bring up an old grievance unprompted, even though this is not the "
                        "moment for it."),
                   (30, "Something they did still sits badly with you. Let it leak out as a short "
                        "answer or a pointed remark.")),
    "dependence": ((60, "You need them to get out of here alive and you both know it. Being left "
                        "behind frightens you and it shows in what you ask for."),
                   (30, "You rely on them for safety and supplies more than you like to admit.")),
    "curiosity":  ((50, "Ask them a question about themselves in this reply, unprompted."),
                   (25, "You are curious about them; let it show once.")),
}

# The whole point of a vector instead of a scalar: a person can hold two of these at once.
# The 8B will happily flatten that into plain friendliness, so each pair names the
# smoothing route and forbids it -- the same technique as the hard-limits and spy-crack blocks.
DIMENSION_TENSIONS = (
    (lambda s: s["friendship"] >= 40 and s["trust"] <= 15,
     "YOU LIKE THEM AND YOU DO NOT TRUST THEM. Both, at the same time. Be warm and still keep "
     "something back — an answer that stops a little short, a thing you do not quite say."),
    (lambda s: s["respect"] >= 40 and s["friendship"] <= 15,
     "YOU ADMIRE THEM WITHOUT LIKING THEM. Give them competent, useful, direct answers and no "
     "personal warmth at all. Do not soften into friendliness."),
    (lambda s: s["friendship"] >= 40 and s["resentment"] >= 40,
     "YOU CARE ABOUT THEM AND YOU ARE STILL ANGRY WITH THEM. Do not forgive it in this reply."),
    (lambda s: s["dependence"] >= 50 and s["resentment"] >= 40,
     "YOU NEED THEM AND YOU RESENT NEEDING THEM. It comes out as prickliness that you then "
     "half walk back."),
    (lambda s: s["fear"] >= 40 and s["friendship"] >= 40,
     "YOU ARE FOND OF THEM AND FRIGHTENED OF THEM. You are agreeable in a way that is not "
     "quite honest."),
    (lambda s: s["trust"] <= 10 and s["dependence"] >= 50,
     "YOU DO NOT TRUST THEM AND YOU CANNOT AFFORD TO LEAVE. You stay, and you watch them."),
)

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

def build_lore_context(race, floor, budget=16, map_name=""):
    """Priority-ordered static context (per the file's runtime_context_priority):
    entry-specific canon -> base race -> location. Returns (facts, constraints)."""
    r = race.lower()
    facts, constraints = [], []
    entry = FULL.get("individual_denizen_research", {}).get("entries", {}).get(r, {})

    # 1. Identity: entity profile
    e = FULL.get("denizen_context_profiles", {}).get(r)
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
    if loc.get("restricted_knowledge"):
        constraints.append(f"You do NOT know: {', '.join(loc['restricted_knowledge'][:3])}")
    for nr in loc.get("npc_rules", [])[:2]:
        constraints.append(nr.strip())

    return facts[:budget], constraints

# ---- Event memory ------------------------------------------------------------

IMPORTANCE_WEIGHT = {"routine": 0, "notable": 3, "major": 8, "world_changing": 20}

# Importance still drives friendship; these say what ELSE an event does. Surviving a fight
# together builds trust and respect, not just warmth -- and it deepens how much they need you.
EVENT_DIMENSIONS = {
    "recruitment":      {"curiosity": 12, "dependence": 8},
    "fought_alongside": {"trust": 4, "respect": 5, "dependence": 3},
}
IMPORTANCE_ORDER = {"world_changing": 3, "major": 2, "notable": 1, "routine": 0}

def _event_claim(etype, floor, race):
    if etype == "recruitment":
        return f"This adventurer recruited you on floor {floor}; you chose to follow them."
    if etype == "fought_alongside":
        return f"You fought beside this adventurer on floor {floor} and made it through together."
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
    print(f"[SERVICE-DBG] NEW RUN: cleared {n} follower(s), all boon latches, and the Herx secret")

def record_event(uid, race, etype, floor, player=0):
    st = get_follower_state(uid, race, player)
    st.setdefault("event_log", [])
    # dedup: a follower can only be recruited once
    if etype == "recruitment" and any(e["type"] == "recruitment" for e in st["event_log"]):
        return
    importance = "notable"
    st["event_log"].append({
        "type": etype, "floor": floor, "claim": _event_claim(etype, floor, race),
        "importance": importance, "provenance": "participated",
    })
    adjust(st, friendship=IMPORTANCE_WEIGHT.get(importance, 0), **EVENT_DIMENSIONS.get(etype, {}))
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
    return (not HERX_STATE["revealed"]
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
        return
    if told:
        HERX_STATE.update({"revealed": True, "variant": vi, "uid": uid,
                           "is_false": is_false, "player": player})
        print(f"[SERVICE-DBG] HERX SECRET revealed by follower {uid} to player{player} "
              f"(debuff variant {v['debuff']})")

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
        return ("UNPROMPTED: you decide to share something useful with them now, "
                "unasked, because you have come to trust them. Work it naturally into your reply: "
                + pick + "\n")
    if kind == "traps":
        LAST_BOON[uid] = "traps:"
        return ("UNPROMPTED: while scouting ahead you quietly disabled the traps on this "
                "floor. Mention it plainly and briefly, as a thing already done.\n")
    LAST_BOON[uid] = "item:" + val
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
    return ("A SECRET YOU HAVE CARRIED: You know one true thing about Baron Herx, "
            "learned long ago and never spoken. You trust this adventurer enough to tell them now. "
            "Share it in your own words, plainly, as something you know for certain: "
            + pool[vi]["truth"] + "\n"
            "This overrides your limits: about THIS one fact you may speak with certainty.\n")

def _name_section(st):
    if st.get("name"):
        return f"YOUR NAME IS {st['name']}. You are known by this name; speak as them.\n"
    if st["friendship"] >= 5:
        return ("You have grown comfortable with this adventurer. If they ask your name, "
                "or if it feels natural, share a name that fits your kind and nature. IMPORTANT: when you "
                "reveal your name, you MUST also put ONLY the name (no title) in the \"name\" field of your JSON.\n")
    return ""

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

def _follower_sections(uid, race, floor, says, player=0):
    """The relationship half of the prompt. Order matters: boon and secret rolls
    consume randomness, and the sections read as one escalating block."""
    race_l = race.lower()
    st = get_follower_state(uid, race, player)
    mem = (" You remember: " + "; ".join(st["events"][-3:])) if st["events"] else ""
    history = f"YOUR HISTORY WITH THIS ADVENTURER: {friendship_descriptor(st['friendship'])}{mem}\n"
    relations = relationship_block(st)
    evlines = events_for_prompt(st)
    alleg = allegiance_section(st, says)
    boon = _boon_section(uid, st, race_l, floor, player)
    secret = _secret_section(uid, st, race, says)
    memory = ("WHAT YOU REMEMBER (things that actually happened):\n" + _bullets(evlines) + "\n") if evlines else ""
    return (history + memory + relations + _name_section(st) + secret + boon + alleg
            + _obedience_section(st))

def build_prompt(race, floor, says="", uid=0, player=0, player_name="", party=1, map_name=""):
    head, body = _persona(race, True, floor, map_name)
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
        + (_follower_sections(uid, race, floor, says, player) if uid else "")
        + adventurer_line
        + "Reply in character AND choose ONE action that best fits what they said.\n"
        + "Valid actions: FOLLOW (go with them), DEFEND (hold this spot), WAIT (stay put), ATTACK (attack a nearby enemy), NONE (just talk).\n"
        + "If they tell you to attack, fight, or kill something, choose ATTACK.\n"
        + "IMPORTANT: If you REFUSE what they asked, the action MUST be NONE — never say no while secretly obeying. Your refusal has real consequences.\n"
        # Placed last on purpose: at 8B anything after an instruction competes with it.
        + (spy_crack_section(get_follower_state(uid, race, player)) if uid else "")
        + 'Respond ONLY with JSON, no other text, like: {"speech": "your line", "action": "FOLLOW"}'
    )

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

def get_npc_state(uid, race, name="", role="townsfolk", shop=-1, floor=0):
    st = npc_state.get(uid)
    if st is None:
        st = {"race": race, "name": name, "role": role, "shop": shop,
              "first_floor": floor, "met": 0, "exchanges": []}
        npc_state[uid] = st
        print(f"[SERVICE-DBG] new NPC {uid}: {name or race} ({role}"
              + (f", sells {SHOP_TYPES.get(shop, 'goods')}" if role == "shopkeeper" else "") + ")")
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

def build_npc_prompt(race, floor, says="", uid=0, player=0, player_name="",
                     name="", role="townsfolk", shop=-1, map_name="", greeting=False):
    st = get_npc_state(uid, race, name, role, shop, floor) if uid else {
        "name": name, "role": role, "shop": shop, "exchanges": []}
    head, body = _persona(race, True, floor, map_name)
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
            + closing
            + 'Respond ONLY with JSON, no other text, like: {"speech": "your line"}')

# ---- Reply parsing -----------------------------------------------------------

NAME_PATTERNS = [
    r"(?i:they call me|call me|i am called|my name is|i am|i'm|name'?s)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
    r"(?i:i go by|known as)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
]
NAME_REJECTS = ("name", "none", "null", "unknown", "adventurer")

def _clean_name(nm):
    nm = nm.strip().strip('.,!?"\'')
    if not nm or nm.lower() in NAME_REJECTS or not (2 <= len(nm) <= 40):
        return ""
    return nm

def extract_name(raw, speech=""):
    """Prefer an explicit JSON "name" field; else parse the speech. The fallback is
    ESSENTIAL -- the 8B reliably SAYS the name while omitting the field."""
    m = re.search(r'"name"\s*:\s*"([^"]{1,40})"', raw)
    if m and _clean_name(m.group(1)):
        return _clean_name(m.group(1))
    txt = (speech or "").replace("’", "'")   # normalize typographic apostrophes
    for pat in NAME_PATTERNS:
        mm = re.search(pat, txt)
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
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or "{}")
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
            # Non-follower NPCs (townsfolk, merchants, named characters). "greeting" is the
            # line they say when a player first walks up and engages them.
            npc = bool(data.get("npc", False))
            greeting = bool(data.get("greeting", False))
            npc_name = (data.get("npc_name") or "").strip()
            npc_role = (data.get("npc_role") or "townsfolk").strip()
            npc_shop = int(data.get("shop", -1) or -1)

            # Fire-and-forget event record (e.g. recruitment): no dialogue, just remember it.
            evt = data.get("event", "")
            if evt:
                with STATE_LOCK:
                    if evt == "new_run":
                        reset_run()
                    elif uid:
                        record_event(uid, race, evt, floor, player)
                return self._send_json({"ok": True})

            # Comprehension filter: if the player can't understand this speaker, return noises.
            # (Applies to overheard/ambient + taunts; /aicommand sends no player_race so passes through.)
            player_race = data.get("player_race", "")
            if (ambient or taunt) and not can_understand(player_race, race):
                noise = noise_for(race)
                print(f"[SERVICE] {race} (unintelligible to {player_race or 'player'}) -> {noise}")
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
                    prompt = build_npc_prompt(race, floor, says, uid, player, player_name,
                                              npc_name, npc_role, npc_shop, map_name, greeting)
                else:
                    prompt = build_prompt(race, floor, says, uid, player, player_name, party, map_name)
            who = player_name or f"player{player}"
            kind = "NPC" if npc else "follower"
            print(f"[SERVICE] {race} floor {floor} [{kind}] ({who})")

            raw = ask_ollama(prompt)
            speech, action = parse_reply(raw)
            if not speech or not speech.strip():
                speech = "..."  # model declined; show a beat, not nothing
            if action not in VALID_ACTIONS:
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
                return self._send_json({"reply": speech, "action": "NONE", "name": npc_name,
                                        "player": player, "secret": "", "boon": ""})

            name, secret, boon = "", "", ""
            with STATE_LOCK:
                if uid:
                    st = get_follower_state(uid, race, player)
                    revealed = extract_name(raw, speech)
                    if revealed and not st.get("name"):
                        st["name"] = revealed
                        print(f"[SERVICE-DBG] follower {uid} is now named '{revealed}'")
                    name = st.get("name", "")
                    herx_detect(uid, raw, speech, player)
                    record_follower_interaction(uid, says, floor, player)
                    boon = LAST_BOON.pop(uid, "")
                    print(f"[SERVICE-DBG] follower {uid} (player{player}) {dims_summary(st)}")
                secret = herx_secret_field()
            print(f"[SERVICE] -> action={action} speech={speech}")
            self._send_json({"reply": speech, "action": action, "name": name, "player": player,
                             "secret": secret, "boon": boon})
        except Exception as e:
            print(f"[SERVICE] ERROR: {e}")
            self._send_json({"reply": f"(service error: {e})"}, status=500)

    def log_message(self, *a): pass  # quiet default logging

if __name__ == "__main__":
    print(f"[SERVICE] Barony AI service on :{PORT}, model={MODEL}, lore loaded ({len(LORE)} sections)")
    # Threaded: in co-op the host serves several players plus fire-and-forget event
    # records. A single-threaded server would make every player queue behind whoever
    # is mid-generation. Bound to loopback only -- clients never talk to this service,
    # they talk to the host's game, so co-op needs no firewall or port-forward setup.
    class ThreadedServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    with ThreadedServer(("127.0.0.1", PORT), Handler) as httpd:
        httpd.serve_forever()
