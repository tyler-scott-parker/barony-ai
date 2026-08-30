# Adorcism

**Everything in Barony's dungeon talks, remembers what you did, and has opinions about you.**
It runs entirely on your own machine — no account, no API key, no network traffic leaving your
computer.

> *adorcism, n.* — the deliberate invitation of a spirit into a body, so that it wakes and acts
> alongside you. The inverse of exorcism. Barony's own word: the **Adorcise** menu binds a spirit
> into a weapon so it fights at your side.

Every line below came out of the model during development, unedited. None of it is scripted, and
none of it exists in the base game — vanilla Barony gives spoken dialogue to humans only.

| | |
|---|---|
| A dummybot, at a crystal golem | *"Bring it on, Sparkle Face!"* |
| A conjured skeleton knight, asked how often it has died | *"Three times, I think. You unmade me and called me back each time. You are a peculiar one, Ada."* |
| A goblin, after a run where you hurt it and left it behind | *"Fine, you want me to go first? I'm not getting killed again because of your recklessness."* |
| A shopkeeper, asked to come and fight | *"I'm a shopkeeper, not a soldier."* |

## Status

**Pre-release. Source only — there is no Windows build yet.** Everything described here is
implemented and verified in isolation on Linux; the honest gaps are at the bottom.

## The two repositories

Adorcism is a thin engine mod plus a local service. They are separate repos on purpose: the mod
stays small and merges cleanly with upstream Barony, and all the lore, prompts and social state
live in Python where they can be changed without recompiling anything.

| | What it is |
|---|---|
| **[barony-ai](https://github.com/tyler-scott-parker/barony-ai)** (here) | The service. Lore, prompts, relationships, memory. Python, no dependencies beyond the standard library. |
| **[barony-mymod](https://github.com/tyler-scott-parker/barony-mymod)** | A fork of [TurningWheel/Barony](https://github.com/TurningWheel/Barony) v5.0.2. All mod code is in `src/mymod/`; the diff against upstream is ~2,700 lines across 18 files. |

The game POSTs to the service over a local socket and gets back `{reply, action, name, secret,
boon, identify, haggle}`. The service talks to [Ollama](https://ollama.com), which does the
generation.

## Running it

You need [Ollama](https://ollama.com) and a model:

```bash
ollama pull llama3.1:8b
python3 service.py
```

Then launch the modded Barony. In game:

```
/aicommand stay behind me and watch the left corridor
/aiidentify 1          ask a companion what an unidentified item is
/aistatus              what the mod currently believes
/ailog <what broke>    drop a note into the session timeline
```

Everything is env-configurable, so a released build needs no file edits:
`BARONY_AI_OLLAMA`, `BARONY_AI_MODEL`, `BARONY_AI_PORT`, `BARONY_AI_BOOKS`, `BARONY_AI_TTS`,
`BARONY_AI_NAMEHIST`. `/aiserver <url>` repoints the game at a different backend at runtime.

## What it does

- **Everyone speaks.** All 49 creatures the game can produce, plus merchants, townsfolk, and the
  ~30 named characters, each with written lore about what it is and what it could plausibly know.
- **They remember.** Fighting beside you, being healed, being wounded, being struck *by you*,
  being left behind, watching another companion die — recorded as structured events and recalled
  specifically.
- **Seven kinds of regard**, tracked separately, never shown as a number: friendship, trust,
  respect, fear, resentment, dependence, curiosity. A companion can like you without trusting you.
- **Obedience is earned.** A refusal is a real refusal, not flavour over compliance.
- **Not everyone is loyal.** Some recruits work against you, deflect about their past, and take
  too much interest in your route. One who comes to like you anyway eventually strains and slips.
- **The Baron's weakness.** A companion you have earned may tell you something true that
  measurably weakens Herx. One deceiving you may tell you something plausible and wrong.
- **Item identification.** Whether they know is one question; whether they tell you the truth is
  another.
- **They name themselves**, from 4,153 names written per species, and a name you have met will
  not come back for many runs.
- **Comprehension.** Polymorph into a rat and most of the dungeon stops making sense.
- **Class companions differ.** A Conjurer's summons keep one identity across every recast and
  remember being unmade; a Machinist's turrets know they are bolted down.
- **Haggling.** Merchants shade prices by how you have spoken to them — ±1% a time, ±5% at most.
- **Voice, optional both ways.** Push-to-talk in, 109 cast voices out. Both off by default.

## Multiplayer

Host-authoritative: **only the host runs the service and Ollama.** Clients install the mod and
join — no Python, no model, no port forwarding, no configuration.

A client can also join with **no mod at all**: the version handshake passes, unknown packets are
ignored rather than fatal, and AI dialogue reaches them as ordinary chat and speech bubbles. They
talk by prefixing a chat line with `@`. They give up the party-HUD rename, merchant lines inside
the shop window, `/aiidentify`, and haggled prices — all gracefully.

## Tests

Service-side suites run with no arguments and no service running:

```bash
python3 test_names.py test_origin.py test_resummon.py test_heckle.py test_haggle.py test_memory.py
python3 service.py --names        # validates all 4,153 names through the real extractor
python3 logreview.py              # summarise the newest session log
```

The boss-secret arc has a measurement harness rather than only assertions, because whether a
secret is *actually told* is a property of the model, not of the code:

```bash
BENCH_N=25 python3 bench_secret.py   # ~100 generations against the local model
```

It drives the real path (`build_prompt` → `ask_ollama` → `herx_detect`), scores every
generation under both the current and the previous detection rule, and prints the
disagreements. Conversion currently sits at 84% (herx) and 80% (twins) for a loyal follower.

The engine side has standalone tests that are deliberately *not* in the build (`src/CMakeLists.txt`
lists sources explicitly, so a second `main()` is inert):

```bash
cd src/mymod
g++ -o httptest httptest.cpp $(pkg-config --cflags --libs SDL2_net) && ./httptest
g++ -o wavtest  wavtest.cpp  $(sdl2-config --cflags --libs)        && ./wavtest
g++ -o packtest packtest.cpp                                       && ./packtest
```

## Known gaps

- **No Windows build.** The code is portable — no shell-outs, no hardcoded paths — but nobody has
  compiled it with MSVC. Expect a round of small fixes.
- **Never played end to end.** Every system is verified in isolation; no one has taken a full run
  from floor 1 to the boss. The tuning numbers are educated guesses until then.
- **Multiplayer has never met a second machine.** The netcode is verified by reading and by
  driving both sides locally.
- The false Herx secret has never been observed firing — every test roll landed non-spy, and it is
  the one branch where a bug would be invisible.
- Ambient babble has no lore grounding, unlike conversation and taunts.
- Prompt size swings from ~650 tokens (rat) to ~3,100 (human) because book-lore injection is
  uneven. Thin races are where fabrication would reappear first.

## Credits and licence

Barony is by [Turning Wheel LLC](https://github.com/TurningWheel/Barony), BSD 2-Clause — which is
the only reason a mod like this can exist. [Ollama](https://ollama.com) does the heavy lifting;
every word is generated locally on your own hardware. Push-to-talk uses
[faster-whisper](https://github.com/SYSTRAN/faster-whisper); spoken replies use
[Piper](https://github.com/rhasspy/piper).

Inspired by the AI-companion mods that came before it in other games — CHIM, Mantella and Herika
for Skyrim among them — which proved the idea was worth building.
