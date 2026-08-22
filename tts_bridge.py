#!/usr/bin/env python3
"""Barony AI-NPC voice bridge -- OPTIONAL. Nothing else depends on it.

Speaks the lines the service generates, with a different voice per creature.
Runs as a separate process, exactly like voice_bridge.py, for the same reason:
the game must never wait on audio.

    Host, with voice:      BARONY_AI_TTS=1 python3 service.py
                           python3 tts_bridge.py          # second terminal
    Host, without voice:   python3 service.py             # nothing else to do

First-time setup for the good voices (one command, ~80 MB, all local):

    python3 tts_bridge.py --setup

That builds .venv-tts/, installs piper-tts into it, and downloads
en_GB-vctk-medium -- ONE model file containing 109 distinct real human voices,
which is what makes casting a dungeon full of NPCs cheap. Without it the bridge
still runs on espeak-ng, which sounds like a 1987 answering machine.

Try voices without launching the game:
    python3 tts_bridge.py --say "Stay behind me." --race troll
    python3 tts_bridge.py --demo                  # one line in every race's voice
    python3 tts_bridge.py --list                  # the casting table
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, time

HERE       = os.path.dirname(os.path.abspath(__file__))
SPOOL      = os.environ.get("BARONY_AI_TTSDIR", "/tmp/mymod_tts")
BACKEND    = os.environ.get("BARONY_AI_TTS_BACKEND", "auto").lower()
VOICE_DIR  = os.environ.get("BARONY_AI_TTS_VOICES", os.path.join(HERE, "voices"))
VENV       = os.environ.get("BARONY_AI_TTS_VENV", os.path.join(HERE, ".venv-tts"))
MAX_CHARS  = int(os.environ.get("BARONY_AI_TTS_MAXCHARS", "320"))
MAX_AGE    = float(os.environ.get("BARONY_AI_TTS_MAXAGE", "20"))   # drop lines the
                              # conversation has already moved past
VOLUME     = float(os.environ.get("BARONY_AI_TTS_VOLUME", "1.0"))
POLL       = 0.05

PIPER_MODEL = "en_GB-vctk-medium"
# Extra single-speaker models, downloaded only by --setup --full. Purely optional:
# every race is castable from vctk alone.
PIPER_EXTRA = ["en_GB-alan-medium", "en_GB-northern_english_male-medium", "en_GB-alba-medium"]

# ---- Casting -----------------------------------------------------------------
#
# VCTK SPEAKER BANDS, measured rather than guessed. There is no usable public
# metadata mapping piper's speaker ids to voice character, so `--profile`
# synthesises the same sentence with all 109 speakers and estimates each one's
# median F0 by autocorrelation. The ids below are that measurement, bucketed:
#
#     deep    80- 98 Hz   low   100-130 Hz   mid   143-168 Hz
#     high   172-204 Hz   shrill 206-251 Hz
#
# Regenerate with `python3 tts_bridge.py --profile` if the model ever changes.
BANDS = {
    "deep":   [76, 71, 102, 97, 104, 32, 29, 20, 13, 75, 47, 38, 24, 10, 23, 106, 93],
    "low":    [7, 50, 12, 28, 96, 26, 61, 55, 60, 64, 95, 4, 67, 70, 98, 81, 57, 16,
               62, 79, 82, 105, 69, 25, 92, 5, 9],
    "mid":    [84, 19, 83, 80, 39, 68, 59],
    "high":   [107, 46, 63, 65, 35, 103, 85, 15, 45, 52, 94, 22, 101, 34, 91, 41, 0, 58,
               78, 77, 43, 54, 33, 56, 100, 74, 72, 90, 3, 2, 48, 14, 37, 89, 53, 73,
               66, 21, 42, 51],
    "shrill": [18, 108, 30, 8, 31, 17, 44, 99, 40, 11, 88, 86, 1, 87, 49, 6, 27, 36],
}

# Per race:
#   bands   which VCTK voices this creature can be cast from (piper)
#   length  piper length-scale; >1 slower, <1 faster
#   fx      sox chain applied to PIPER output -- deliberately gentler than the espeak
#           chain below, because over-processing an already-natural voice is what makes
#           cheap TTS sound cheap
#   variants/speed/pitch/efx   the espeak-ng fallback tier, unchanged and still tuned
#           to carry the character on its own when piper isn't installed
DEFAULT_VOICE = {
    "bands": ["low", "mid"], "length": 1.0, "fx": [],
    "variants": ["m3", "m1", "m5", "f2"], "speed": 165, "pitch": 45, "efx": [],
}

VOICES = {
    # --- humanoids you can hold a conversation with ---
    # Humans draw from the whole natural range: townsfolk should sound like a town.
    "human":        {"bands": ["low", "mid", "high"], "length": 1.0, "fx": [],
                     "variants": ["m3", "m1", "f2", "f5", "Andy", "Alicia", "steph2"],
                     "speed": 168, "pitch": 48, "efx": []},
    "shopkeeper":   {"bands": ["low", "mid", "high"], "length": 0.97, "fx": [],
                     "variants": ["m2", "f4", "Mike", "grandpa"],
                     "speed": 175, "pitch": 52, "efx": []},
    "goblin":       {"bands": ["low"], "length": 0.94, "fx": ["pitch", "-90", "overdrive", "3"],
                     "variants": ["m5", "m6", "croak", "m7"],
                     "speed": 180, "pitch": 28, "efx": ["overdrive", "6", "pitch", "-120"]},
    "goatman":      {"bands": ["low", "deep"], "length": 1.0, "fx": ["pitch", "-70", "overdrive", "2"],
                     "variants": ["m6", "croak", "m4"],
                     "speed": 172, "pitch": 32, "efx": ["overdrive", "4", "pitch", "-90"]},
    "gnome":        {"bands": ["shrill"], "length": 0.90, "fx": ["pitch", "120"],
                     "variants": ["f1", "f3", "Tweaky", "Gene2"],
                     "speed": 198, "pitch": 78, "efx": ["pitch", "180"]},
    "kobold":       {"bands": ["shrill"], "length": 0.92, "fx": ["pitch", "100", "overdrive", "2"],
                     "variants": ["f1", "Tweaky", "m7"],
                     "speed": 195, "pitch": 72, "efx": ["pitch", "150", "overdrive", "3"]},
    "dwarf":        {"bands": ["deep"], "length": 1.06, "fx": ["pitch", "-60"],
                     "variants": ["m4", "m8", "grandpa"],
                     "speed": 150, "pitch": 22, "efx": ["pitch", "-160"]},
    "insectoid":    {"bands": ["high"], "length": 0.95,
                     "fx": ["overdrive", "3", "echo", "0.8", "0.6", "8", "0.2"],
                     "variants": ["m7", "robosoft2"], "speed": 185, "pitch": 60,
                     "efx": ["overdrive", "5", "echo", "0.8", "0.6", "8", "0.2"]},

    # --- the big and the slow ---
    "troll":        {"bands": ["deep"], "length": 1.32, "fx": ["pitch", "-260", "overdrive", "5"],
                     "variants": ["m4", "m8", "croak"], "speed": 112, "pitch": 5,
                     "efx": ["pitch", "-320", "overdrive", "9"]},
    "minotaur":     {"bands": ["deep"], "length": 1.26, "fx": ["pitch", "-230", "overdrive", "7"],
                     "variants": ["m8", "m4"], "speed": 120, "pitch": 8,
                     "efx": ["pitch", "-280", "overdrive", "12"]},
    "cyclops":      {"bands": ["deep"], "length": 1.30, "fx": ["pitch", "-240", "overdrive", "6"],
                     "variants": ["m8", "m4"], "speed": 115, "pitch": 6,
                     "efx": ["pitch", "-300", "overdrive", "10"]},

    # --- the dead ---
    "skeleton":     {"bands": ["deep"], "length": 1.14,
                     "fx": ["pitch", "-110", "reverb", "55", "50", "70"],
                     "variants": ["whisper", "m8", "Demonic"], "speed": 132, "pitch": 18,
                     "efx": ["pitch", "-140", "reverb", "70", "50", "80"]},
    "ghoul":        {"bands": ["deep"], "length": 1.20, "fx": ["pitch", "-170", "reverb", "40"],
                     "variants": ["croak", "m8"], "speed": 122, "pitch": 12,
                     "efx": ["pitch", "-220", "reverb", "50"]},
    "lich":         {"bands": ["deep"], "length": 1.18,
                     "fx": ["pitch", "-200", "reverb", "75", "60", "95",
                            "echo", "0.85", "0.7", "55", "0.3"],
                     "variants": ["Demonic", "m8", "whisper"], "speed": 128, "pitch": 10,
                     "efx": ["pitch", "-260", "reverb", "85", "60", "100",
                             "echo", "0.85", "0.7", "60", "0.35"]},
    "vampire":      {"bands": ["low"], "length": 1.08, "fx": ["reverb", "30"],
                     "variants": ["m2", "Storm"], "speed": 140, "pitch": 30,
                     "efx": ["reverb", "40"]},
    "shadow":       {"bands": ["high"], "length": 1.10, "fx": ["reverb", "80", "70", "95"],
                     "variants": ["whisper", "whisperf"], "speed": 130, "pitch": 25,
                     "efx": ["reverb", "90", "70", "100"]},
    "revenant skull": {"bands": ["deep"], "length": 1.16,
                     "fx": ["pitch", "-160", "reverb", "70"],
                     "variants": ["whisper", "Demonic"], "speed": 126, "pitch": 15,
                     "efx": ["pitch", "-200", "reverb", "80"]},

    # --- the infernal ---
    "demon":        {"bands": ["deep"], "length": 1.20,
                     "fx": ["pitch", "-280", "overdrive", "8", "reverb", "40"],
                     "variants": ["Demonic", "m8"], "speed": 128, "pitch": 8,
                     "efx": ["pitch", "-340", "overdrive", "14", "reverb", "45"]},
    "devil":        {"bands": ["deep"], "length": 1.22,
                     "fx": ["pitch", "-310", "overdrive", "10", "reverb", "50"],
                     "variants": ["Demonic", "m8"], "speed": 124, "pitch": 5,
                     "efx": ["pitch", "-380", "overdrive", "16", "reverb", "60"]},
    "imp":          {"bands": ["shrill"], "length": 0.86, "fx": ["pitch", "200", "overdrive", "2"],
                     "variants": ["Tweaky", "f1", "croak"], "speed": 205, "pitch": 85,
                     "efx": ["pitch", "260", "overdrive", "4"]},
    "succubus":     {"bands": ["high"], "length": 1.05, "fx": ["reverb", "28"],
                     "variants": ["f3", "whisperf", "steph3"], "speed": 152, "pitch": 62,
                     "efx": ["reverb", "35"]},
    "incubus":      {"bands": ["low"], "length": 1.05, "fx": ["reverb", "28"],
                     "variants": ["m2", "Storm"], "speed": 148, "pitch": 32,
                     "efx": ["reverb", "35"]},

    # --- constructs: the one place a robotic voice is CORRECT ---
    "automaton":    {"bands": ["low", "mid"], "length": 1.0,
                     "fx": ["overdrive", "4", "echo", "0.85", "0.7", "5", "0.35",
                            "highpass", "180", "lowpass", "3400"],
                     "variants": ["robosoft", "UniRobot", "robosoft3"], "speed": 162, "pitch": 45,
                     "efx": ["echo", "0.8", "0.7", "6", "0.3", "overdrive", "3"]},
    "sentrybot":    {"bands": ["mid"], "length": 0.98,
                     "fx": ["overdrive", "4", "echo", "0.85", "0.7", "4", "0.35",
                            "highpass", "200", "lowpass", "3200"],
                     "variants": ["robosoft4", "UniRobot"], "speed": 170, "pitch": 40,
                     "efx": ["echo", "0.8", "0.7", "5", "0.3"]},
    "spellbot":     {"bands": ["high"], "length": 0.98,
                     "fx": ["overdrive", "3", "echo", "0.85", "0.7", "4", "0.35",
                            "highpass", "200", "lowpass", "3600"],
                     "variants": ["robosoft6", "UniRobot"], "speed": 170, "pitch": 55,
                     "efx": ["echo", "0.8", "0.7", "5", "0.3"]},
    "crystal golem": {"bands": ["deep"], "length": 1.12,
                     "fx": ["pitch", "-140", "reverb", "70", "60", "85"],
                     "variants": ["robosoft8", "m8"], "speed": 130, "pitch": 30,
                     "efx": ["pitch", "-180", "reverb", "75", "60", "90"]},
    "earth sprite": {"bands": ["shrill"], "length": 0.94, "fx": ["pitch", "80", "reverb", "25"],
                     "variants": ["m7", "Tweaky"], "speed": 175, "pitch": 65,
                     "efx": ["pitch", "120", "reverb", "30"]},

    # --- beasts: these speak in *noises*, so they get pushed well past natural ---
    "rat":          {"bands": ["shrill"], "length": 0.80, "fx": ["pitch", "380", "overdrive", "2"],
                     "variants": ["Tweaky", "f1"], "speed": 215, "pitch": 95,
                     "efx": ["pitch", "420", "overdrive", "2"]},
    "spider":       {"bands": ["shrill"], "length": 0.84, "fx": ["pitch", "300", "overdrive", "4"],
                     "variants": ["croak", "Tweaky"], "speed": 200, "pitch": 88,
                     "efx": ["pitch", "330", "overdrive", "6"]},
    "scorpion":     {"bands": ["shrill"], "length": 0.88, "fx": ["pitch", "210", "overdrive", "5"],
                     "variants": ["croak", "m7"], "speed": 190, "pitch": 70,
                     "efx": ["pitch", "240", "overdrive", "7"]},
    "scarab":       {"bands": ["shrill"], "length": 0.84, "fx": ["pitch", "270", "overdrive", "4"],
                     "variants": ["Tweaky", "croak"], "speed": 205, "pitch": 82,
                     "efx": ["pitch", "300", "overdrive", "5"]},
    "cave crawler": {"bands": ["shrill"], "length": 0.88, "fx": ["pitch", "230", "overdrive", "4"],
                     "variants": ["croak", "m7"], "speed": 195, "pitch": 75,
                     "efx": ["pitch", "260", "overdrive", "6"]},
    "slime":        {"bands": ["low"], "length": 1.15, "fx": ["pitch", "-60", "reverb", "45"],
                     "variants": ["croak", "m6"], "speed": 150, "pitch": 40,
                     "efx": ["pitch", "-80", "reverb", "50"]},
    "cockatrice":   {"bands": ["shrill"], "length": 0.86, "fx": ["pitch", "250", "overdrive", "6"],
                     "variants": ["croak", "Tweaky"], "speed": 200, "pitch": 80,
                     "efx": ["pitch", "280", "overdrive", "8"]},
    # --- lesser beasts and ambient life ---
    "bat":          {"bands": ["shrill"], "length": 0.84, "fx": ["pitch", "300", "overdrive", "3"],
                     "variants": ["Tweaky", "f5"], "speed": 210, "pitch": 85,
                     "efx": ["pitch", "340", "overdrive", "4"]},
    "moth":         {"bands": ["shrill"], "length": 0.92, "fx": ["pitch", "220", "reverb", "25"],
                     "variants": ["whisper", "f5"], "speed": 190, "pitch": 76,
                     "efx": ["pitch", "260", "reverb", "30"]},
    "crab":         {"bands": ["low"], "length": 1.08, "fx": ["pitch", "-80", "overdrive", "4"],
                     "variants": ["croak", "m6"], "speed": 145, "pitch": 30,
                     "efx": ["pitch", "-110", "overdrive", "6"]},
    "duck":         {"bands": ["shrill"], "length": 0.95, "fx": ["pitch", "180", "overdrive", "5"],
                     "variants": ["Tweaky", "croak"], "speed": 185, "pitch": 70,
                     "efx": ["pitch", "220", "overdrive", "7"]},
    "bugbear":      {"bands": ["deep", "low"], "length": 1.10, "fx": ["pitch", "-140", "overdrive", "4"],
                     "variants": ["m8", "croak"], "speed": 138, "pitch": 20,
                     "efx": ["pitch", "-180", "overdrive", "6"]},
    "minimimic":    {"bands": ["shrill"], "length": 0.90, "fx": ["pitch", "160", "overdrive", "4"],
                     "variants": ["Tweaky", "m6"], "speed": 192, "pitch": 68,
                     "efx": ["pitch", "200", "overdrive", "6"]},
    "mimic":        {"bands": ["low"], "length": 1.04, "fx": ["pitch", "-70", "overdrive", "3"],
                     "variants": ["m6", "croak"], "speed": 158, "pitch": 38,
                     "efx": ["pitch", "-100", "overdrive", "5"]},

    # --- fey and growing things: slow, breathy, unhurried ---
    "dryad":        {"bands": ["high"], "length": 1.22, "fx": ["reverb", "45", "40", "60"],
                     "variants": ["whisper", "f2"], "speed": 128, "pitch": 55,
                     "efx": ["reverb", "55", "40", "70"]},
    "myconid":      {"bands": ["low"], "length": 1.26,
                     "fx": ["pitch", "-90", "reverb", "60", "55", "80"],
                     "variants": ["whisper", "m6"], "speed": 124, "pitch": 34,
                     "efx": ["pitch", "-120", "reverb", "70", "55", "85"]},
    "gremlin":      {"bands": ["shrill"], "length": 0.88, "fx": ["pitch", "210", "overdrive", "5"],
                     "variants": ["Tweaky", "f5"], "speed": 205, "pitch": 82,
                     "efx": ["pitch", "260", "overdrive", "7"]},

    # --- elementals: the voice is the element ---
    "salamander":   {"bands": ["low"], "length": 1.00, "fx": ["pitch", "-50", "overdrive", "7"],
                     "variants": ["croak", "m7"], "speed": 162, "pitch": 36,
                     "efx": ["pitch", "-70", "overdrive", "10"]},
    "flame elemental": {"bands": ["deep"], "length": 1.12,
                     "fx": ["pitch", "-120", "overdrive", "9", "reverb", "35"],
                     "variants": ["croak", "Demonic"], "speed": 140, "pitch": 24,
                     "efx": ["pitch", "-150", "overdrive", "12", "reverb", "40"]},

    # --- undead variants ---
    "frosty lich":  {"bands": ["deep"], "length": 1.24,
                     "fx": ["pitch", "-190", "reverb", "80", "65", "100",
                            "highpass", "180"],
                     "variants": ["whisper", "Demonic"], "speed": 120, "pitch": 8,
                     "efx": ["pitch", "-240", "reverb", "85", "65", "100"]},
    "fiery lich":   {"bands": ["deep"], "length": 1.08,
                     "fx": ["pitch", "-180", "overdrive", "6", "reverb", "60", "55", "80"],
                     "variants": ["Demonic", "croak"], "speed": 140, "pitch": 12,
                     "efx": ["pitch", "-220", "overdrive", "9", "reverb", "65"]},

    # --- more constructs: bandpass is what sells "not a throat" ---
    "gyrobot":      {"bands": ["shrill"], "length": 0.94,
                     "fx": ["pitch", "140", "overdrive", "3", "highpass", "300", "lowpass", "3800"],
                     "variants": ["robosoft4", "Tweaky"], "speed": 200, "pitch": 74,
                     "efx": ["pitch", "180", "overdrive", "5"]},
    "dummybot":     {"bands": ["mid"], "length": 1.02,
                     "fx": ["overdrive", "3", "highpass", "220", "lowpass", "3000"],
                     "variants": ["robosoft", "m3"], "speed": 158, "pitch": 42,
                     "efx": ["overdrive", "5"]},
    "hologram":     {"bands": ["mid"], "length": 1.00,
                     "fx": ["overdrive", "2", "echo", "0.9", "0.75", "3", "0.4",
                            "highpass", "260", "lowpass", "3400"],
                     "variants": ["robosoft2", "m3"], "speed": 170, "pitch": 50,
                     "efx": ["overdrive", "4", "echo", "0.9", "0.75", "3", "0.4"]},
    "adorcised possession": {"bands": ["deep"], "length": 1.16,
                     "fx": ["pitch", "-160", "overdrive", "5", "reverb", "70", "60", "90",
                            "echo", "0.8", "0.7", "40", "0.35"],
                     "variants": ["Demonic", "whisper"], "speed": 132, "pitch": 14,
                     "efx": ["pitch", "-200", "overdrive", "8", "reverb", "75"]},
}

# Named characters the lore file already treats as individuals get a FIXED speaker id,
# overriding their race -- Merlin should sound like Merlin every time, not like whichever
# human the hash landed on. Keyed on the name the service sends.
#
# NOTE this is the `npc_name` path only. A follower who names ITSELF "Merlin" does not
# inherit the voice, the same scoping rule the lore lookup uses.
NAMED = {
    "king arthur":  {"sid": 19,  "length": 1.06, "fx": ["reverb", "30"],
                     "variants": ["m2"], "speed": 148, "pitch": 38, "efx": ["reverb", "45"]},
    "merlin":       {"sid": 93,  "length": 1.16, "fx": ["reverb", "40"],
                     "variants": ["grandpa"], "speed": 138, "pitch": 34, "efx": ["reverb", "55"]},
    "lilith":       {"sid": 41,  "length": 1.08, "fx": ["reverb", "65", "50", "85"],
                     "variants": ["whisperf", "f3"], "speed": 138, "pitch": 55,
                     "efx": ["reverb", "70", "50", "90"]},
    "bram kindly":  {"sid": 84,  "length": 1.0,  "fx": [],
                     "variants": ["m2"], "speed": 158, "pitch": 42, "efx": []},
    "baron herx":   {"sid": 76,  "length": 1.22,
                     "fx": ["pitch", "-260", "reverb", "80", "60", "100",
                            "echo", "0.85", "0.7", "65", "0.38"],
                     "variants": ["Demonic"], "speed": 124, "pitch": 6,
                     "efx": ["pitch", "-360", "reverb", "85", "60", "100",
                             "echo", "0.85", "0.7", "70", "0.4"]},
    "gharbad":      {"sid": 12,  "length": 0.94, "fx": ["pitch", "-90", "overdrive", "3"],
                     "variants": ["m5", "croak"], "speed": 178, "pitch": 30,
                     "efx": ["overdrive", "6", "pitch", "-120"]},
    "algernon":     {"sid": 36,  "length": 0.92, "fx": ["pitch", "60"],
                     "variants": ["m7"], "speed": 185, "pitch": 58, "efx": []},
    "shelob":       {"sid": 51,  "length": 1.06, "fx": ["reverb", "50"],
                     "variants": ["whisperf"], "speed": 150, "pitch": 60, "efx": ["reverb", "60"]},
}


def _norm(s):
    """Match the service's own key normalisation -- the game hands back display names
    with spaces ('crystal golem'), and getting this wrong is a silent fallthrough."""
    return re.sub(r"[\s\-]+", " ", (s or "").strip().lower()).replace("'", "")


def _hash(race, uid):
    """md5, NOT Python's hash() -- that is salted per process, so the same creature
    would be recast with a new voice every time the bridge restarts."""
    return int(hashlib.md5(("%s|%s" % (_norm(race), uid)).encode()).hexdigest()[:8], 16)


def voice_for(race, uid=0, name=""):
    """Resolve a creature to concrete synth settings. Deterministic in uid, so a given
    creature keeps one voice for the whole run."""
    named = NAMED.get(_norm(name))
    prof = named or VOICES.get(_norm(race)) or DEFAULT_VOICE
    h = _hash(race, uid)

    if named and "sid" in named:
        sid = named["sid"]
    else:
        pool = [s for b in prof.get("bands", DEFAULT_VOICE["bands"]) for s in BANDS.get(b, ())]
        sid = pool[h % len(pool)] if pool else 0

    variants = prof.get("variants") or DEFAULT_VOICE["variants"]
    # Small jitter so two creatures cast from the same voice still differ a little.
    length = prof.get("length", 1.0) * (1.0 + (((h >> 8) % 11) - 5) / 100.0)
    speed  = prof.get("speed", 165) + ((h >> 8) % 17) - 8
    pitch  = prof.get("pitch", 45) + ((h >> 16) % 13) - 6
    return {"sid": sid, "length": round(max(0.5, min(2.0, length)), 3),
            "fx": list(prof.get("fx") or []),
            "variant": variants[h % len(variants)],
            "speed": max(80, min(300, speed)), "pitch": max(0, min(99, pitch)),
            "efx": list(prof.get("efx") or [])}


# ---- Text cleanup ------------------------------------------------------------

_STRIP = re.compile(r"[\*_`~#<>\[\]{}\\|]+")

def clean(text):
    """Model output is written to be READ. Strip what a synth would mangle."""
    t = _STRIP.sub(" ", text or "")
    t = t.replace("’", "'").replace("‘", "'")
    t = t.replace("“", "").replace("”", "").replace('"', "")
    t = t.replace("—", ", ").replace("–", ", ")
    t = re.sub(r"\s+", " ", t).strip()
    if t in ("", "...", ".", "-"):
        return ""
    if len(t) > MAX_CHARS:
        cut = t[:MAX_CHARS]
        stop = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        t = cut[:stop + 1] if stop > MAX_CHARS // 2 else cut.rsplit(" ", 1)[0] + "..."
    return t


# ---- Playback ----------------------------------------------------------------

HAVE_SOX = bool(shutil.which("play") and shutil.which("sox"))

def _player_cmd(fx, rate=None, raw=False):
    """A command that consumes audio on stdin and plays it. sox is strongly preferred
    because it is also what applies the effect chain; without it the fx are skipped and
    voices go flat but still work."""
    if HAVE_SOX:
        cmd = ["play", "-q"]
        if raw:
            cmd += ["-t", "raw", "-r", str(rate or 22050), "-e", "signed", "-b", "16", "-c", "1"]
        else:
            cmd += ["-t", "wav"]
        return cmd + ["-", "vol", str(VOLUME)] + list(fx)
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-"]
    return []


def _play_bytes(pcm, rate, fx):
    cmd = _player_cmd(fx, rate=rate, raw=True)
    if not cmd:
        print("[TTS] no audio player found (install sox, or ffmpeg for ffplay)")
        return
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        p.communicate(pcm, timeout=120)
    except subprocess.TimeoutExpired:
        p.kill()


# ---- Piper (quality tier) ----------------------------------------------------

_PIPER = {"voice": None, "rate": 22050}

def piper_model_path():
    p = os.path.join(VOICE_DIR, PIPER_MODEL + ".onnx")
    return p if os.path.exists(p) else ""


def piper_load():
    """Load once and keep it. ~0.9s and ~80 MB RSS; synthesis then runs at RTF ~0.03 on
    CPU, so a 4-second line takes ~0.13s. Deliberately CPU: the GPU is already holding
    the 8B (about 6 of 8 GB), and TTS must not compete with it."""
    if _PIPER["voice"] is not None:
        return _PIPER["voice"]
    from piper import PiperVoice
    path = piper_model_path()
    t0 = time.time()
    _PIPER["voice"] = PiperVoice.load(path)
    cfg = json.load(open(path + ".json"))
    _PIPER["rate"] = cfg["audio"]["sample_rate"]
    print("[TTS] piper %s loaded (%d speakers, %.1fs)"
          % (PIPER_MODEL, cfg.get("num_speakers", 1), time.time() - t0))
    return _PIPER["voice"]


def speak_piper(text, v):
    from piper import SynthesisConfig
    voice = piper_load()
    cfg = SynthesisConfig(speaker_id=v["sid"], length_scale=v["length"],
                          noise_scale=0.667, noise_w_scale=0.8)
    pcm = b"".join(c.audio_int16_bytes for c in voice.synthesize(text, cfg))
    _play_bytes(pcm, _PIPER["rate"], v["fx"])


# ---- espeak-ng (fallback tier) -----------------------------------------------

def speak_espeak(text, v):
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    args = ["-v", "en-us+%s" % v["variant"], "-s", str(v["speed"]), "-p", str(v["pitch"])]
    play = _player_cmd(v["efx"])
    if not play:
        subprocess.run([exe] + args + [text], timeout=60)
        return
    p1 = subprocess.Popen([exe] + args + ["-a", "170", "--stdout", text],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p2 = subprocess.Popen(play, stdin=p1.stdout, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    p1.stdout.close()
    try:
        p2.wait(timeout=90)
    except subprocess.TimeoutExpired:
        p2.kill()
    p1.wait(timeout=5)


def speak_flite(text, v):
    subprocess.run(["flite", "-t", text], timeout=60)


# ---- Backend selection -------------------------------------------------------

def have_piper_module():
    try:
        import piper  # noqa: F401
        return True
    except Exception:
        return False


def pick_backend():
    if BACKEND != "auto":
        return BACKEND
    if piper_model_path() and have_piper_module():
        return "piper"
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak"
    if shutil.which("flite"):
        return "flite"
    return ""


def speak(text, race="", uid=0, name="", backend=None):
    t = clean(text)
    if not t:
        return
    v = voice_for(race, uid, name)
    be = backend or _BACKEND
    who = name or race or "?"
    tag = ("spk%d" % v["sid"]) if be == "piper" else v["variant"]
    print("[TTS] %-18s %-9s %s" % (who[:18], tag, t[:60]))
    try:
        if be == "piper":
            speak_piper(t, v)
        elif be == "flite":
            speak_flite(t, v)
        else:
            speak_espeak(t, v)
    except Exception as e:
        print("[TTS] playback failed (%s): %s" % (be, e))


# ---- Spool loop --------------------------------------------------------------

def drain():
    try:
        files = sorted(f for f in os.listdir(SPOOL) if f.endswith(".json"))
    except FileNotFoundError:
        return
    for fn in files:
        path = os.path.join(SPOOL, fn)
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            rec = None
        try:
            os.remove(path)     # claim it BEFORE speaking; a crash must not loop a line
        except OSError:
            pass
        if not rec:
            continue
        if time.time() - float(rec.get("ts", 0)) > MAX_AGE:
            print("[TTS] (dropped stale line from %s)" % (rec.get("race") or "?"))
            continue
        speak(rec.get("text", ""), rec.get("race", ""), rec.get("uid", 0), rec.get("name", ""))


# ---- Setup / maintenance -----------------------------------------------------

def venv_python():
    p = os.path.join(VENV, "bin", "python")
    if os.path.exists(p):
        return p
    p = os.path.join(VENV, "Scripts", "python.exe")   # Windows
    return p if os.path.exists(p) else ""


def do_setup(full=False):
    """Build the optional venv, install piper, fetch voices. Isolated on purpose: the
    service must never acquire a hard dependency on any of this."""
    py = venv_python()
    if not py:
        print("[TTS] creating %s" % VENV)
        subprocess.check_call([sys.executable, "-m", "venv", VENV])
        py = venv_python()
    print("[TTS] installing piper-tts ...")
    subprocess.check_call([py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    subprocess.check_call([py, "-m", "pip", "install", "-q", "piper-tts"])
    os.makedirs(VOICE_DIR, exist_ok=True)
    wanted = [PIPER_MODEL] + (PIPER_EXTRA if full else [])
    for name in wanted:
        if os.path.exists(os.path.join(VOICE_DIR, name + ".onnx")):
            print("[TTS] %s already present" % name)
            continue
        print("[TTS] downloading %s ..." % name)
        subprocess.check_call([py, "-m", "piper.download_voices",
                               "--download-dir", VOICE_DIR, name])
    print("\n[TTS] setup done. Now run:")
    print("      BARONY_AI_TTS=1 python3 service.py")
    print("      python3 tts_bridge.py")


def do_profile():
    """Re-measure the VCTK speaker bands. See the BANDS comment for why this exists:
    piper's speaker ids carry no usable metadata, so voice character is measured, not
    looked up. Prints a BANDS block ready to paste back into this file."""
    import numpy as np
    from piper import SynthesisConfig
    voice = piper_load()
    cfg = json.load(open(piper_model_path() + ".json"))
    names = {i: n for n, i in cfg["speaker_id_map"].items()}
    sr = cfg["audio"]["sample_rate"]
    text = "The old road runs deep beneath the hill, and few who walk it ever come back again."

    def f0(a):
        a = a.astype(np.float64); a -= a.mean()
        W, H = int(sr * 0.04), int(sr * 0.02)
        lo, hi = int(sr / 350), int(sr / 60)
        rms, out = np.sqrt((a ** 2).mean()), []
        for s in range(0, len(a) - W, H):
            f = a[s:s + W]
            if np.sqrt((f ** 2).mean()) < 0.06 * rms:
                continue
            c = np.correlate(f, f, "full")[W - 1:]
            if c[0] <= 0:
                continue
            seg = (c / c[0])[lo:hi]
            if not len(seg):
                continue
            k = int(np.argmax(seg))
            if seg[k] < 0.3:
                continue
            out.append(sr / (lo + k))
        return float(np.median(out)) if out else 0.0

    rows = []
    for sid in range(cfg.get("num_speakers", 1)):
        ch = list(voice.synthesize(text, SynthesisConfig(speaker_id=sid)))
        a = np.frombuffer(b"".join(c.audio_int16_bytes for c in ch), dtype=np.int16)
        rows.append((sid, names.get(sid, "?"), f0(a)))
    rows.sort(key=lambda r: r[2])
    for sid, nm, hz in rows:
        print("%3d %-6s %6.1f Hz" % (sid, nm, hz))
    edges = (("deep", 0, 100), ("low", 100, 132), ("mid", 132, 170),
             ("high", 170, 205), ("shrill", 205, 9999))
    print("\nBANDS = {")
    for nm, lo, hi in edges:
        print('    "%s": %r,' % (nm, [r[0] for r in rows if lo <= r[2] < hi]))
    print("}")


def maybe_reexec_into_venv():
    """Convenience: the docs say `python3 tts_bridge.py`, but piper lives in the venv.
    Hand off once, guarded against a loop."""
    if have_piper_module() or os.environ.get("BARONY_AI_TTS_NOREEXEC"):
        return
    # NOTE: compare sys.prefix, NOT realpath(sys.executable). A venv's bin/python is a
    # SYMLINK to the system interpreter, so a realpath comparison matches and silently
    # skips the hand-off -- leaving the user on robotic espeak with no clue why.
    py = venv_python()
    if not py or os.path.realpath(sys.prefix) == os.path.realpath(VENV):
        return
    os.environ["BARONY_AI_TTS_NOREEXEC"] = "1"
    # -u so the hand-off keeps unbuffered output; block buffering after execv makes a
    # redirected bridge log look empty.
    os.execv(py, [py, "-u", os.path.abspath(__file__)] + sys.argv[1:])


def main():
    ap = argparse.ArgumentParser(description="Barony AI-NPC voice bridge (optional)")
    ap.add_argument("--say", help="speak one line and exit")
    ap.add_argument("--race", default="human")
    ap.add_argument("--name", default="", help="named character override, e.g. 'Merlin'")
    ap.add_argument("--uid", type=int, default=0)
    ap.add_argument("--demo", action="store_true", help="one line in every race's voice")
    ap.add_argument("--list", action="store_true", help="print the casting table and exit")
    ap.add_argument("--setup", action="store_true", help="install piper + download voices")
    ap.add_argument("--full", action="store_true", help="with --setup, also fetch extra models")
    ap.add_argument("--profile", action="store_true", help="re-measure VCTK speaker bands")
    ap.add_argument("--backend", default="", choices=["", "piper", "espeak", "flite"])
    args = ap.parse_args()

    if args.setup:
        return do_setup(args.full)
    if args.list:
        print("%-16s %-22s %-6s %s" % ("RACE", "PIPER BANDS", "LENGTH", "SOX FX"))
        for r, p in sorted(VOICES.items()):
            print("%-16s %-22s %-6s %s" % (r, ",".join(p["bands"]), p["length"],
                                           " ".join(p.get("fx") or [])))
        print("\nnamed characters: " + ", ".join("%s(spk%d)" % (k, v["sid"])
                                                 for k, v in sorted(NAMED.items())))
        print("\nvoice bands (measured F0): " +
              ", ".join("%s=%d" % (k, len(v)) for k, v in BANDS.items()))
        return

    maybe_reexec_into_venv()

    global _BACKEND
    _BACKEND = args.backend or pick_backend()
    if not _BACKEND:
        sys.exit("[TTS] no backend found. Run:  python3 tts_bridge.py --setup")
    if _BACKEND == "piper" and not piper_model_path():
        sys.exit("[TTS] piper model missing. Run:  python3 tts_bridge.py --setup")

    if args.profile:
        return do_profile()
    if args.say:
        return speak(args.say, args.race, args.uid, args.name)
    if args.demo:
        line = "Stay close. This place is not what it looks like."
        for r in sorted(VOICES):
            speak(line, r, uid=7)
        return

    os.makedirs(SPOOL, exist_ok=True)
    print("[TTS] backend=%s sox=%s spool=%s" % (_BACKEND, "yes" if HAVE_SOX else "no", SPOOL))
    if _BACKEND != "piper":
        print("[TTS] running on %s -- this is the ROBOTIC fallback. For real voices: "
              "python3 tts_bridge.py --setup" % _BACKEND)
    if not HAVE_SOX:
        print("[TTS] sox not found -- per-race effects disabled (voices will be flat). "
              "dnf install sox")
    if _BACKEND == "piper":
        piper_load()   # pay the 0.9s now, not on the first line of dialogue
    print("[TTS] ready. Run the service with BARONY_AI_TTS=1.")
    while True:
        drain()
        time.sleep(POLL)


_BACKEND = ""
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[TTS] stopped.")
