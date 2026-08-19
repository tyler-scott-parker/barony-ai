import json, http.server, socketserver, urllib.request, os

LORE_PATH = os.path.expanduser("~/barony-ai/barony_lore.json")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.1:8b"
PORT = 5001

with open(LORE_PATH) as f:
    LORE = json.load(f)

RACE_LORE_PATH = os.path.expanduser("~/barony-ai/race_lore.json")
with open(RACE_LORE_PATH) as f:
    RACE_LORE = json.load(f)

BOOKS_DIR = "/home/tyler/.local/share/Steam/steamapps/common/Barony/books"
RACE_BOOKS_PATH = os.path.expanduser("~/barony-ai/race_books.json")
with open(RACE_BOOKS_PATH) as f:
    RACE_BOOKS = json.load(f)

import random as _random
COMPREHENSION_PATH = os.path.expanduser("~/barony-ai/comprehension.json")
with open(COMPREHENSION_PATH) as f:
    COMPREHENSION = json.load(f)

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
    r = speaker_race.lower()
    noises = COMPREHENSION.get("noises", {})
    pool = noises.get(r) or noises.get("default_beast", ["*unintelligible noises*"])
    return _random.choice(pool)

_book_cache = {}
def get_book_lore(race):
    files = RACE_BOOKS.get(race.lower(), [])
    if not files:
        return ""
    out = []
    for fn in files:
        if fn in _book_cache:
            out.append(_book_cache[fn]); continue
        try:
            with open(os.path.join(BOOKS_DIR, fn), encoding="utf-8", errors="ignore") as bf:
                txt = bf.read().strip()
            _book_cache[fn] = txt
            out.append(txt)
        except Exception as e:
            print(f"[SERVICE] couldn't read book {fn}: {e}")
    return "\n\n".join(out)

def build_taunt_prompt(race, floor):
    world = LORE["world"]["setting"]
    race_l = race.lower()
    slice_ = RACE_LORE.get(race_l, RACE_LORE.get("default", "A creature of the dungeon."))
    book_lore = get_book_lore(race_l)
    book_section = ('RELEVANT LORE (what your kind knows):' + chr(10) + book_lore + chr(10)) if book_lore else ''
    _facts, _constraints = build_lore_context(race, floor)
    grounding_section = ('CANONICAL GROUNDING:' + chr(10) + chr(10).join('- ' + x for x in _facts) + chr(10)) if _facts else ''
    limits_section = ('HARD LIMITS ON WHAT YOU KNOW. You genuinely do not know these things:' + chr(10) + 'If asked about any of them, say plainly and in character that you do not know, and STOP.' + chr(10) + 'Do NOT guess, speculate, theorize, or pass on rumors about them. Hedged answers are FORBIDDEN:' + chr(10) + '"some say...", "whispers speak of...", "perhaps it is...", "I have heard..." followed by an answer' + chr(10) + 'counts as claiming and is wrong. An honest "I do not know" is always the correct reply.' + chr(10) + chr(10).join('- ' + x for x in _constraints) + chr(10)) if _constraints else ''
    return (
        f"SETTING: {world}\n"
        f"YOU ARE: a {race} on dungeon floor {floor}, locked in COMBAT right now.\n"
        f"CHARACTER GUIDANCE: {slice_}\n"
        f"{grounding_section}"
        f"{limits_section}"
        f"{book_section}"
        f"You are fighting an enemy this very moment. Shout ONE short, aggressive taunt or battle-cry, in character.\n"
        f"Respond with ONLY the spoken line (a few words). No narration, no quotes, no JSON."
    )

def build_ambient_prompt(race, floor, relation="hostile"):
    world = LORE["world"]["setting"]
    race_l = race.lower()
    slice_ = RACE_LORE.get(race_l, RACE_LORE.get("default", "A creature of the dungeon."))
    book_lore = get_book_lore(race_l)
    book_section = ('RELEVANT LORE (what your kind knows):' + chr(10) + book_lore + chr(10)) if book_lore else ''
    if relation == "follower":
        situation = "You are the adventurer's companion, wandering the dungeon together. Mutter a short idle remark to yourself or your companion."
    else:
        situation = "You do NOT see the adventurer. You are alone or with your own kind in the dungeon. Mutter a short idle line to yourself, unaware you are overheard."
    return (
        f"SETTING: {world}\n"
        f"YOU ARE: a {race} on dungeon floor {floor}.\n"
        f"CHARACTER GUIDANCE: {slice_}\n"
        f"{book_section}"
        f"{situation}\n"
        f"Respond with ONLY one short spoken line (a few words), in character. No narration, no quotes, no JSON. Just the line."
    )

# ---- Within-run follower relationship state (keyed by follower UID) ----
follower_state = {}   # uid -> {"friendship": int, "events": [str], "race": str}

# ---- Hidden allegiance: never shown to the player, colors behavior and enables betrayal ----
ALLEGIANCE_WEIGHTS = [("loyal", 70), ("self_interested", 15), ("fearful", 8), ("spy", 7)]
SPY_MOTIVES = [
    "your family is held by those who serve Herx",
    "you believe this adventurer's quest is doomed and you intend to survive it",
    "you were promised gold you badly need",
    "you owe an old loyalty to the Baron that you have never renounced",
    "you fear what will be done to you if you do not report on them",
]

def roll_allegiance():
    total = sum(w for _, w in ALLEGIANCE_WEIGHTS)
    r = _random.randrange(total)
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
    f = st.get("friendship", 0)
    low = (says or "").lower()
    probing = any(k in low for k in (
        "why did you", "who are you really", "where are you from", "your family",
        "do you trust", "would you die", "are you loyal", "whose side", "lying",
        "hiding", "the truth", "believe in", "what do you want"))
    if a == "spy":
        out = ("HIDDEN (never state this outright, never confess unless cornered by evidence): "
               "you are secretly working against this adventurer. " + st.get("motive", "") + "." + chr(10))
        out += ("HOW THIS SHOWS: you are warm and useful, but you never share anything about your own past, "
                "family, or reasons that could expose you — you deflect those smoothly and change the subject. "
                "You take more interest than you should in where they are going and what they carry. "
                "The Baron's name makes you hesitate for a beat before answering." + chr(10))
        if probing:
            out += ("They are probing you right now. Deflect — but let it show slightly: a pause, "
                    "an answer a little too smooth, a question turned back on them." + chr(10))
        if f >= 30:
            out += ("You have come to genuinely like this adventurer, and it is eating at you. "
                    "Let a crack show: an unfinished sentence, an odd warning, a moment where you almost say something."
                    + chr(10))
        return out
    if a == "self_interested":
        return ("HIDDEN: your own survival and gain come first. You help while it suits you, and you are "
                "candid about wanting something in return." + chr(10))
    if a == "fearful":
        return ("HIDDEN: you are frightened of what waits below and of powers stronger than this adventurer. "
                "It shows as hedging and reluctance when danger is discussed." + chr(10))
    return ""

def get_follower_state(uid, race):
    if uid not in follower_state:
        _al = roll_allegiance()
        follower_state[uid] = {"friendship": 0, "events": [], "event_log": [], "name": "", "race": race,
                               "allegiance": _al,
                               "motive": _random.choice(SPY_MOTIVES) if _al == "spy" else ""}
        print(f"[SERVICE-DBG] follower {uid} allegiance={_al}")
    return follower_state[uid]

def friendship_descriptor(f):
    if f <= 1:  return "You barely know this adventurer."
    if f <= 4:  return "You have spoken a few times; you are still taking their measure."
    if f <= 9:  return "You are warming to this adventurer; they keep you close and speak with you often."
    return "You have come to genuinely trust and value this adventurer."

def record_follower_interaction(uid, says, floor=0):
    st = get_follower_state(uid, "")
    # Per-floor cap: chatting can only add a small, fixed amount of friendship per floor.
    # This keeps friendship 100 a whole-playthrough milestone — talk is cheap; the bulk of
    # friendship must come from deeds/events (added later). Tunable: CHAT_CAP_PER_FLOOR.
    CHAT_CAP_PER_FLOOR = 2
    fkey = str(floor)
    gained = st.setdefault("chat_gain_by_floor", {})
    got_here = gained.get(fkey, 0)
    if got_here < CHAT_CAP_PER_FLOOR:
        st["interaction_count"] = st.get("interaction_count", 0) + 1
        # within the cap, still meter it: +1 friendship per 3 exchanges, up to the floor cap
        if st["interaction_count"] % 3 == 0:
            st["friendship"] = min(100, st["friendship"] + 1)
            gained[fkey] = got_here + 1
    if says:
        st["events"].append('they said to you: "' + says[:60] + '"')
        st["events"] = st["events"][-6:]

# ---- Full researched lore (v19) + priority-ordered context builder ----
FULL_LORE_PATH = os.path.expanduser("~/barony-ai/barony_lore_full.json")
with open(FULL_LORE_PATH) as _f:
    FULL = json.load(_f)

def floor_to_region(floor):
    try: f = int(floor)
    except: return None
    if f <= 0: return None
    if f <= 4:  return "mines"
    if f <= 8:  return "swamp"
    if f <= 13: return "sand_labyrinth"
    if f <= 18: return "ruins"
    if f <= 24: return "underworld"
    return "hell"

def build_lore_context(race, floor, budget=16):
    """Priority-ordered static context (per the file's runtime_context_priority):
    entry-specific canon -> base race -> location. Returns (facts, constraints)."""
    r = race.lower()
    facts, constraints = [], []
    dcp = FULL.get("denizen_context_profiles", {})
    rp  = FULL.get("race_profiles", {})
    idr = FULL.get("individual_denizen_research", {}).get("entries", {})

    # 1. Identity: entity profile
    if r in dcp:
        e = dcp[r]
        facts.append(f"YOU ARE ({e.get('category','?')}): {e.get('baseline','').strip()}")
        if e.get("knowledge_scope"):
            facts.append(f"WHAT YOU CAN KNOW: {e['knowledge_scope'].strip()}")

    # 2. Entry-specific CANON (highest-confidence facts about your kind)
    entry = idr.get(r, {})
    for cf in entry.get("canon_facts", [])[:4]:
        facts.append(f"CANON ABOUT YOUR KIND: {cf.strip()}")

    # 3. Safe inferences (individual dimensions you may plausibly have)
    for si in entry.get("safe_inferences", [])[:2]:
        facts.append(f"YOU MAY PLAUSIBLY HAVE: {si.strip()}")

    # 4. Base race worldview + temperament
    if r in rp:
        pr = rp[r]
        if pr.get("default_worldview"):
            facts.append(f"YOUR OUTLOOK CENTERS ON: {pr['default_worldview'].strip()}")
        axes = pr.get("personality_axes", [])
        if axes:
            facts.append(f"TYPICAL TEMPERAMENT (pick what fits, don't be all): {', '.join(axes[:4])}")

    # 5. Location: canon + local knowledge
    region = floor_to_region(floor)
    lp = FULL.get("location_knowledge_audit", {}).get("location_profiles", {})
    loc = lp.get(region, {}) if region else {}
    for lc in loc.get("canon", [])[:2]:
        facts.append(f"ABOUT THIS PLACE ({region}): {lc.strip()}")
    hv = loc.get("high_value_local_knowledge", [])
    if hv:
        facts.append(f"HERE ({region}), YOU MIGHT KNOW: {', '.join(hv[:4])}")
    pop = loc.get("local_population", [])
    if pop:
        facts.append(f"WHO LIVES HERE: {', '.join(pop[:6])}")

    # Constraints (guardrails; kept whole, not budget-capped)
    if entry.get("knowledge_boundary"):
        constraints.append(entry["knowledge_boundary"].strip())
    rk = loc.get("restricted_knowledge", [])
    if rk:
        constraints.append(f"You do NOT know: {', '.join(rk[:3])}")
    for nr in loc.get("npc_rules", [])[:2]:
        constraints.append(nr.strip())

    return facts[:budget], constraints

IMPORTANCE_WEIGHT = {"routine": 0, "notable": 3, "major": 8, "world_changing": 20}

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
    HERX_STATE["revealed"] = False
    HERX_STATE["variant"] = None
    HERX_STATE["uid"] = 0
    HERX_STATE["pending"] = None
    print(f"[SERVICE-DBG] NEW RUN: cleared {n} follower(s) and the Herx secret")

def record_event(uid, race, etype, floor):
    st = get_follower_state(uid, race)
    st.setdefault("event_log", [])
    # dedup: a follower can only be recruited once
    if etype == "recruitment" and any(e["type"] == "recruitment" for e in st["event_log"]):
        return
    importance = {"recruitment": "notable"}.get(etype, "notable")
    st["event_log"].append({
        "type": etype, "floor": floor, "claim": _event_claim(etype, floor, race),
        "importance": importance, "provenance": "participated",
    })
    st["friendship"] = min(100, st["friendship"] + IMPORTANCE_WEIGHT.get(importance, 0))
    print(f"[SERVICE-DBG] event '{etype}' recorded for follower {uid}; friendship now {st['friendship']}")

def events_for_prompt(st, budget=6):
    order = {"world_changing": 3, "major": 2, "notable": 1, "routine": 0}
    ranked = sorted(st.get("event_log", []), key=lambda e: (order.get(e["importance"], 0), e["floor"]), reverse=True)
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
HERX_FALSE_VARIANTS = [
    {"debuff": 0, "truth": "He cannot cross running water. Fight him where the seep comes down the wall and he will not follow.",
     "keywords": ["running water", "seep", "cross water"]},
    {"debuff": 0, "truth": "Silver. He was buried with a silver clasp and it burns him still. Any silver you carry will bite.",
     "keywords": ["silver", "clasp", "burns him"]},
    {"debuff": 0, "truth": "Speak his given name — his true one, Herxel — and he must stop and answer. It buys you time.",
     "keywords": ["herxel", "true name", "given name", "must answer"]},
]
HERX_STATE = {"revealed": False, "variant": None, "uid": 0, "pending": None, "is_false": False}

def _fight_count(st):
    return sum(1 for e in st.get("event_log", []) if e.get("type") == "fought_alongside")

def herx_eligible(st, race):
    if HERX_STATE["revealed"]:
        return False
    if race.lower() not in HERX_ELIGIBLE_RACES:
        return False
    if not st.get("name"):
        return False
    if st["friendship"] < HERX_MIN_FRIENDSHIP:
        return False
    return _fight_count(st) >= HERX_MIN_FIGHTS

def herx_roll(st, says):
    extra = max(0, _fight_count(st) - HERX_MIN_FIGHTS)
    chance = min(0.80, 0.25 + 0.10 * extra)
    low = (says or "").lower()
    if any(k in low for k in ("herx", "baron", "weakness", "secret")):
        chance = min(0.95, chance + 0.30)
    if st.get("allegiance") == "spy":
        chance = min(0.95, chance + 0.25)   # a spy wants you to believe it
    return _random.random() < chance

def herx_detect(uid, raw, speech):
    """If a secret was offered to this follower this turn, decide whether it was actually told."""
    pend = HERX_STATE.get("pending")
    if not pend or pend[0] != uid:
        return
    _false = len(pend) > 2 and pend[2]
    v = (HERX_FALSE_VARIANTS if _false else HERX_SECRET_VARIANTS)[pend[1]]
    txt = ((speech or "") + " " + (raw or "")).lower()
    told = '"secret"' in (raw or "").lower() or any(k in txt for k in v["keywords"])
    HERX_STATE["pending"] = None
    if told:
        HERX_STATE["revealed"] = True
        HERX_STATE["variant"] = pend[1]
        HERX_STATE["uid"] = uid
        HERX_STATE["is_false"] = _false
        print(f"[SERVICE-DBG] HERX SECRET revealed by follower {uid} (debuff variant {v['debuff']})")

# ---- Follower boons: flavor-scale gifts, friendship-gated, one per follower per floor ----
BOON_MIN_FRIENDSHIP = 10
BOON_MUNDANE = [("FOOD_BREAD", 1), ("FOOD_CHEESE", 1), ("GEM_GLASS", 1), ("TOOL_TORCH", 1)]
BOON_GOOD = [("POTION_HEALING", 1), ("POTION_EXTRAHEALING", 1), ("GEM_GARNET", 1)]
BOON_TRAP_RACES = ("gnome", "automaton", "kobold", "goblin")
BOON_STATE = {"good_used": False}
LAST_BOON = {}   # uid -> "item:TYPE:N" or "traps:" pending delivery to C++

def boon_roll(st, floor):
    """One boon per follower per floor, chance scaling with friendship."""
    f = st.get("friendship", 0)
    if f < BOON_MIN_FRIENDSHIP:
        return None
    if st.get("last_boon_floor") == floor:
        return None
    chance = min(0.35, (f - BOON_MIN_FRIENDSHIP) / 200.0)
    if _random.random() >= chance:
        return None
    st["last_boon_floor"] = floor
    race = st.get("race", "").lower()
    # rarest: trap disarm, only for mechanically-minded kinds at real trust
    if race in BOON_TRAP_RACES and f >= 30 and _random.random() < 0.15:
        return ("traps", "")
    # one genuinely good item per run
    if not BOON_STATE["good_used"] and f >= 40 and _random.random() < 0.20:
        BOON_STATE["good_used"] = True
        it, ct = _random.choice(BOON_GOOD)
        return ("item", "%s:%d" % (it, ct))
    # otherwise: mostly information, sometimes something mundane
    if _random.random() < 0.60:
        return ("info", "")
    it, ct = _random.choice(BOON_MUNDANE)
    return ("item", "%s:%d" % (it, ct))

def build_prompt(race, floor, says="", uid=0):
    world = LORE["world"]["setting"]
    race_l = race.lower()
    slice_ = RACE_LORE.get(race_l, RACE_LORE.get("default", "A creature of the dungeon."))
    book_lore = get_book_lore(race_l)
    book_section = ('RELEVANT LORE (what your kind knows):' + chr(10) + book_lore + chr(10)) if book_lore else ''
    _facts, _constraints = build_lore_context(race_l, floor)
    grounding_section = ('CANONICAL GROUNDING:' + chr(10) + chr(10).join('- ' + x for x in _facts) + chr(10)) if _facts else ''
    limits_section = ('HARD LIMITS ON WHAT YOU KNOW. You genuinely do not know these things:' + chr(10) + 'If asked about any of them, say plainly and in character that you do not know, and STOP.' + chr(10) + 'Do NOT guess, speculate, theorize, or pass on rumors about them. Hedged answers are FORBIDDEN:' + chr(10) + '"some say...", "whispers speak of...", "perhaps it is...", "I have heard..." followed by an answer' + chr(10) + 'counts as claiming and is wrong. An honest "I do not know" is always the correct reply.' + chr(10) + chr(10).join('- ' + x for x in _constraints) + chr(10)) if _constraints else ''
    history_section = ""
    memory_section = ""
    obedience_section = ""
    name_section = ""
    secret_section = ""
    boon_section = ""
    alleg_section = ""
    _boon_payload = ""
    if uid:
        st = get_follower_state(uid, race)
        f = st["friendship"]
        hist = friendship_descriptor(f)
        mem = (" You remember: " + "; ".join(st["events"][-3:])) if st["events"] else ""
        history_section = f"YOUR HISTORY WITH THIS ADVENTURER: {hist}{mem}" + chr(10)
        _evlines = events_for_prompt(st)
        alleg_section = allegiance_section(st, says)
        _bn = boon_roll(st, floor)
        if _bn:
            _bkind, _bval = _bn
            if _bkind == "info":
                _bfacts, _ = build_lore_context(race_l, floor)
                _pick = _random.choice(_bfacts) if _bfacts else ""
                boon_section = ("UNPROMPTED: you decide to share something useful with them now, "
                    "unasked, because you have come to trust them. Work it naturally into your reply: "
                    + _pick + chr(10))
            elif _bkind == "traps":
                _boon_payload = "traps:"
                LAST_BOON[uid] = _boon_payload
                boon_section = ("UNPROMPTED: while scouting ahead you quietly disabled the traps on this "
                    "floor. Mention it plainly and briefly, as a thing already done." + chr(10))
            else:
                _boon_payload = "item:" + _bval
                LAST_BOON[uid] = _boon_payload
                _iname = _bval.split(":")[0].replace("_", " ").lower()
                boon_section = ("UNPROMPTED: you are giving them something you came by honestly — "
                    + _iname + ". Offer it in one short line, with a plausible reason you have it." + chr(10))
        if HERX_STATE["revealed"] and HERX_STATE["uid"] == uid:
            secret_section = ("YOU HAVE ALREADY TOLD THIS ADVENTURER HERX'S WEAKNESS: "
                + (HERX_FALSE_VARIANTS if HERX_STATE.get("is_false") else HERX_SECRET_VARIANTS)[HERX_STATE["variant"]]["truth"] + chr(10))
        elif herx_eligible(st, race) and herx_roll(st, says):
            _isspy = st.get("allegiance") == "spy"
            _pool = HERX_FALSE_VARIANTS if _isspy else HERX_SECRET_VARIANTS
            _vi = _random.randrange(len(_pool))
            HERX_STATE["pending"] = (uid, _vi, _isspy)
            secret_section = ("A SECRET YOU HAVE CARRIED: You know one true thing about Baron Herx, "
                "learned long ago and never spoken. You trust this adventurer enough to tell them now. "
                "Share it in your own words, plainly, as something you know for certain: "
                + _pool[_vi]["truth"] + chr(10)
                + "This overrides your limits: about THIS one fact you may speak with certainty." + chr(10))
        if st.get("name"):
            name_section = f"YOUR NAME IS {st['name']}. You are known by this name; speak as them.\n"
        elif st["friendship"] >= 5:
            name_section = ("You have grown comfortable with this adventurer. If they ask your name, "
                "or if it feels natural, share a name that fits your kind and nature. IMPORTANT: when you "
                "reveal your name, you MUST also put ONLY the name (no title) in the \"name\" field of your JSON.\n")
        else:
            name_section = ""
        memory_section = ("WHAT YOU REMEMBER (things that actually happened):" + chr(10) + chr(10).join("- " + c for c in _evlines) + chr(10)) if _evlines else ""
        if f <= 4:
            obedience_section = ("OBEDIENCE: You owe this adventurer nothing yet. Obey only basic, safe requests "
                "(FOLLOW, WAIT) and only if you feel like it. Refuse anything risky, costly, demeaning, or against "
                "your nature. To refuse, choose action NONE and say why in character." + chr(10))
        elif f <= 9:
            obedience_section = ("OBEDIENCE: You are starting to trust this adventurer. Carry out reasonable commands, "
                "though you may grumble. Refuse only truly dangerous or objectionable ones (action NONE)." + chr(10))
        else:
            obedience_section = ("OBEDIENCE: You trust this adventurer deeply. Carry out their commands readily, even "
                "risky ones — loyalty means acting on their word." + chr(10))
    if says:
        adventurer_line = f'The adventurer says to you: "{says}"' + chr(10)
    else:
        adventurer_line = 'The adventurer approaches you.' + chr(10)
    return (
        f"SETTING: {world}\n"
        f"YOU ARE: a {race} on dungeon floor {floor}, an ally the adventurer can command.\n"
        f"CHARACTER GUIDANCE: {slice_}\n"
        f"{grounding_section}"
        f"{limits_section}"
        f"{book_section}"
        f"{history_section}"
        f"{memory_section}"
        f"{name_section}"
        f"{secret_section}"
        f"{boon_section}"
        f"{alleg_section}"
        f"{obedience_section}"
        f"{adventurer_line}"
        f"Reply in character AND choose ONE action that best fits what they said.\n"
        f"Valid actions: FOLLOW (go with them), DEFEND (hold this spot), WAIT (stay put), ATTACK (attack a nearby enemy), NONE (just talk).\n"
        f"If they tell you to attack, fight, or kill something, choose ATTACK.\n"
        f"IMPORTANT: If you REFUSE what they asked, the action MUST be NONE — never say no while secretly obeying. Your refusal has real consequences.\n"
        f"Respond ONLY with JSON, no other text, like: {{\"speech\": \"your line\", \"action\": \"FOLLOW\"}}"
    )

import re as _re
def extract_name(raw, speech=""):
    # Prefer an explicit JSON "name" field; else fall back to parsing the speech,
    # since the model reliably SAYS the name even when it omits the field.
    def _clean(nm):
        nm = nm.strip().strip('.,!?"\'')
        if not nm or nm.lower() in ("name", "none", "null", "unknown", "adventurer"):
            return ""
        if len(nm) > 40 or len(nm) < 2:
            return ""
        return nm
    m = _re.search(r'"name"\s*:\s*"([^"]{1,40})"', raw)
    if m:
        got = _clean(m.group(1))
        if got:
            return got
    # Fallback: common self-naming phrasings in the speech.
    txt = speech or ""
    txt = txt.replace("\u2019", "'")   # normalize typographic apostrophes
    patterns = [
        r"(?i:they call me|call me|i am called|my name is|i am|i'm|name'?s)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
        r"(?i:i go by|known as)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)",
    ]
    for pat in patterns:
        mm = _re.search(pat, txt)
        if mm:
            got = _clean(mm.group(1))
            if got:
                return got
    return ""

def parse_reply(raw):
    """Robust parse: strict JSON first, then a regex fallback for mildly malformed
    model output (missing comma, unquoted action, preamble junk). Returns (speech, action)."""
    # 1. strict JSON (common, correct case)
    try:
        s = raw[raw.find('{'): raw.rfind('}')+1]
        parsed = json.loads(s)
        sp = parsed.get('speech', '')
        ac = str(parsed.get('action', 'NONE')).upper()
        if sp and sp.strip():
            return sp.strip(), ac
    except Exception:
        pass
    # 2. regex fallback: pull speech + action even if the JSON is malformed
    sp_m = _re.search(r'"speech"\s*:\s*"(.*?)"\s*[,}]?\s*"?action"?', raw, _re.DOTALL)
    if not sp_m:
        sp_m = _re.search(r'"speech"\s*:\s*"(.+?)"', raw, _re.DOTALL)
    ac_m = _re.search(r'"action"\s*:\s*"?([A-Za-z]+)"?', raw)
    if sp_m:
        print('[SERVICE] (JSON malformed - recovered via fallback)')
        return sp_m.group(1).strip(), (ac_m.group(1).upper() if ac_m else 'NONE')
    # 3. total failure: strip JSON scaffolding, return raw-ish as speech
    fb = _re.sub(r'[{}"]', '', raw).replace('speech:', '').replace('action:', '').strip()
    return (fb or '...'), 'NONE'

def ask_ollama(prompt):
    print(f"[SERVICE-DBG] prompt ~{len(prompt)//4} tokens")
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "keep_alive": "30m",
                       "options": {"num_ctx": 16384}}).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["response"].strip()

class Handler(http.server.BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # Client (the game) hung up before we replied. Harmless; don't crash the thread.
            print("[SERVICE] (client disconnected early - ignored)")
    def _safe_write(self, data):
        try:
            self.wfile.write(data)
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
            relation = data.get("relation", "hostile")
            player_race = data.get("player_race", "")
            # Fire-and-forget event record (e.g. recruitment): no dialogue, just remember it.
            _evt = data.get("event", "")
            if _evt == "new_run":
                reset_run()
                out = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._safe_write(out)
                return
            if _evt:
                _euid = int(data.get("uid", 0) or 0)
                if _euid:
                    record_event(_euid, race, _evt, floor)
                out = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._safe_write(out)
                return
            # Comprehension filter: if the player can't understand this speaker, return noises
            # (applies to overheard/ambient + taunts; direct /aicommand sends no player_race so passes through).
            if (ambient or taunt) and not can_understand(player_race, race):
                noise = noise_for(race)
                out = json.dumps({"reply": noise, "action": "NONE"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self._safe_write(out)
                print(f"[SERVICE] {race} (unintelligible to {player_race or 'player'}) -> {noise}")
                return
            if taunt:
                prompt = build_taunt_prompt(race, floor)
            elif ambient:
                prompt = build_ambient_prompt(race, floor, relation)
            else:
                prompt = build_prompt(race, floor, says, int(data.get("uid", 0) or 0))
            print(f"[SERVICE] {race} floor {floor}")
            raw = ask_ollama(prompt)
            # Model is asked for JSON; try to parse it, fall back to plain speech.
            speech, action = raw, "NONE"
            speech, action = parse_reply(raw)
            _revealed_name = extract_name(raw, speech)
            _reply_name = ""
            _nuid = int(data.get("uid", 0) or 0)
            if _nuid:
                _nst = get_follower_state(_nuid, race)
                if _revealed_name and not _nst.get("name"):
                    _nst["name"] = _revealed_name
                    print(f"[SERVICE-DBG] follower {_nuid} is now named '{_revealed_name}'")
                _reply_name = _nst.get("name", "")
                herx_detect(_nuid, raw, speech)
            if not speech or not speech.strip():
                speech = "..."  # model declined; show a beat, not nothing
            if action not in ("FOLLOW", "DEFEND", "WAIT", "ATTACK", "NONE"):
                action = "NONE"
            _uid = int(data.get("uid", 0) or 0)
            if _uid:
                record_follower_interaction(_uid, says, floor)
                _st = follower_state.get(_uid, {})
                print(f"[SERVICE-DBG] follower {_uid} friendship={_st.get('friendship')} ({friendship_descriptor(_st.get('friendship',0))})")
            print(f"[SERVICE] -> action={action} speech={speech}")
            _secret = ("%d:%d" % (0 if HERX_STATE.get("is_false") else HERX_SECRET_VARIANTS[HERX_STATE["variant"]]["debuff"], HERX_STATE["uid"])) if HERX_STATE["revealed"] else ""
            _boon = LAST_BOON.pop(_nuid, "") if _nuid else ""
            out = json.dumps({"reply": speech, "action": action, "name": _reply_name, "secret": _secret, "boon": _boon}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self._safe_write(out)
        except Exception as e:
            print(f"[SERVICE] ERROR: {e}")
            self.send_response(500); self.end_headers()
            self._safe_write(json.dumps({"reply": f"(service error: {e})"}).encode())
    def log_message(self, *a): pass  # quiet default logging

print(f"[SERVICE] Barony AI service on :{PORT}, model={MODEL}, lore loaded ({len(LORE)} sections)")
with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
    httpd.serve_forever()
