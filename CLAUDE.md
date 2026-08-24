# Barony AI-NPC Mod — Project Context

Drop this in as `CLAUDE.md` in either repo (or paste at the start of a fresh chat).

---

## What this is

An AI-NPC mod for **Barony** (open-source C++ roguelike, v5.0.2), built from source on **Nobara Linux**. Thin C++ engine hooks ↔ a local Python HTTP service that owns lore, prompts, and social state, calling a local `llama3.1:8b` via Ollama. Fully local, no cloud.

**Hardware:** i7-9700K, RTX 2070 Super (8GB), 32GB RAM.

**Release intent:** Nexus Mods. Co-op friends are non-technical and on Windows; dev env is Linux. Design converges on: thin mod + configurable backend endpoint + setup guide, host-authoritative.

## Where the documentation lives

| File | Loaded | Contents |
|---|---|---|
| **`CLAUDE.md`** (this file) | every session | paths, hooks, build, the region map, diagnostics, and the *Hard-won prompt rules* digest |
| **`FINDINGS.md`** | **on demand — read it** | the lab notebook: all 39 subsystem sections and the measurements behind them |

⚠ **Before editing any subsystem's prompts or mechanics, read that subsystem's section in
`FINDINGS.md`.** Most of it is negative results — things tried, measured and reverted — so working
from this file alone will re-introduce them.

## Repos

| Repo | Path | Remote |
|---|---|---|
| Python service | `~/barony-ai/` | `origin` → `tyler-scott-parker/barony-ai` |
| Barony fork | `~/Barony/` (branch `mymod`) | `origin` → TurningWheel upstream; `mine` → `tyler-scott-parker/barony-mymod` |

Fetch upstream with `git fetch origin`; push your work with `git push mine mymod`.

## Key paths

- All mod C++ now lives in **`src/mymod/mymod.cpp`** + `mymod.hpp` (extracted so upstream merges stay clean)
- Listed in `src/CMakeLists.txt` under `GAME_SOURCES` — **not** in `EDITOR_SOURCES`
- Service: `~/barony-ai/service.py` (port 5001), plus `barony_lore_full.json` (449KB, 45 sections), `barony_lore.json` (**only** source of `world.setting` — its wording differs from the full file's, so they are not interchangeable), `race_lore.json`, `race_books.json`, `comprehension.json`, `follower_names.json`, `voice_bridge.py`, `tts_bridge.py` (optional voice output, off by default; its `.venv-tts/` and `voices/` are gitignored)
- Data files are resolved relative to `service.py`, so the repo can live anywhere
- Dev binary: `~/.local/share/Steam/steamapps/common/Barony/barony-modded`
- IPC is a **direct TCP POST via SDL_net** (`src/mymod/mymod_net.hpp`) — no curl, no python3,
  no temp files, no shell. Scratch files that remain (push-to-talk, `/aiserver` config) go
  through `mymod_tmpPath()`, which reads `TMPDIR`/`TEMP`/`TMP`

## Windows portability

⚠ **The shell-out transport was the single root cause of most of the Windows blockers.** Every
request used to be `popen("curl ... > /tmp/x.json; python3 -c 'import json...'")`, which fails on
Windows for five separate reasons at once: `/tmp` does not exist, `cmd.exe` does not honour
`'...'` quoting, `python3` is `python` or `py`, `popen` is `_popen` under MSVC, and each call
flashes up a console window. One replacement fixed all five.

**`src/mymod/mymod_net.hpp`** — a direct TCP POST over **SDL_net**, which the game already links
(`main.hpp` includes it), so this costs no new dependency and no winsock `#ifdef`s. Plus a small
reply reader for exactly what the service emits: `mymod_jsonField`, `mymod_jsonStringArray`, and
the escapes `json.dumps` produces (`\"`, `\\`, `\n`, `\uXXXX` → UTF-8).

⚠ **It also removed a whole failure mode.** The old extractor printed the reply and split it on
`::ACTION::`/`::NAME::`/`::SECRET::` markers — so a follower who happened to *say* `::ACTION::`
would corrupt everything after it. Field lookup cannot do that; there is a regression test for it.

| Was | Now |
|---|---|
| 5 `popen`/`system` shell-outs | 0 |
| 9 hardcoded `/tmp/mymod_*` paths | 0 — `mymod_tmpPath()` reads `TMPDIR`/`TEMP`/`TMP` |
| `curl` + `python3` required by the *game* | neither |
| `BOOKS_DIR` = one hardcoded Linux Steam path | `_find_books_dir()` searches Linux/flatpak/Windows/macOS/GOG |
| bridges hardcoded `/tmp` | `tempfile.gettempdir()` — reads the same variables the C++ does |

**Test it with `g++ -o httptest httptest.cpp $(pkg-config --cflags --libs SDL2_net) && ./httptest`**
— 26 assertions covering URL parsing, field extraction, the real escapes, the marker regression,
the heckle array, a live POST, and a dead port failing cleanly instead of hanging. Not in the
build (`CMakeLists.txt` lists sources explicitly), same arrangement as `packtest.cpp`.

**Still open, and none of it verifiable from Linux:**
- **Nobody has compiled the mod with MSVC.** GCC accepts things MSVC rejects; expect a round of
  small fixes. `getenv` may need `_CRT_SECURE_NO_WARNINGS`.
- **How the mod is distributed on Windows** is undecided — a patched binary is a much bigger ask
  of a non-technical friend than Barony's own mod format would be.
- The **service** still needs Python 3 on the host; on Windows the launch command is `python`,
  not `python3`. Documentation, not code.
- **Voice and TTS bridges** need sox / espeak-ng / piper. Both optional and off by default.
- `mymod_tmpPath` joins with `/`. Windows accepts forward slashes in `fopen`, so this is fine —
  noted because it looks wrong at a glance.

## Upstream hooks (verify these after any upstream merge)

| File | Hook | Notes |
|---|---|---|
| `game.cpp` | `mymod_pollAI()`, `mymod_ambientTick()` | per-frame, in the game loop |
| `actmonster.cpp` | `mymod_recordEvent("recruitment", ...)` ×2 | `forceFollower` (~11186, guard `leader.behavior == &actPlayer`) and interact-recruit (~2170). **The interact path is the one `/friendly` and normal recruitment actually use.** |
| `files.cpp` | `new_run` event in `physfsLoadMapFile` at `levelToLoad <= 1`; plus a `__attribute__((weak))` `mymod_recordEvent` stub | The weak stub exists because the **editor** target compiles `files.cpp` without the mod and would otherwise fail to link |
| `monster_lich.cpp` | Herx debuff block after `my->setHardcoreStats(*myStats)` | inside the `!MONSTER_INIT` guard, so it can't double-apply |
| `consolecommand.cpp` | `/aicommand`, `/aiserver`, `/aitest` + `#include "../mymod/mymod.hpp"` | commands legitimately belong here |
| `net.cpp` | `{'MYAI'}`, `{'MYID'}` in `serverPacketHandlers`; `{'MYNM'}`, `{'MYSH'}`, `{'MYIV'}`, `{'MYFR'}` in `clientPacketHandlers` + `#include "mymod/mymod.hpp"` | the six mod packets. The tables are file-`static`, so registration cannot happen from `mymod.cpp` |
| `actmonster.cpp` | `mymod_npcEngage()` at the top of `handleMonsterChatter` (~12216) | clicking a talking NPC. **Falls through to the vanilla canned line when it returns false**, so an NPC is never mute if the service is down |
| `shops.cpp` | `mymod_npcEngage()` at the end of `startTradingServer` | merchant greets on shop open. At the END of the function on purpose — the local-player and remote-client branches both flow through it |
| `player.cpp` | `everybodyfriendly || intro` early-out at the top of `monsterIsFriendlyForTooltip` (~3367) | **the other half of `MYFR`** — without it a client receives the flag and still ignores it, because the `checkEnemy` shortcut that reads it is wrapped in `if (multiplayer != CLIENT)`. Mirrors `checkFriend`/`checkEnemy` exactly |

## Build & run

```bash
cd ~/Barony/build && make -j$(nproc) 2>&1 | tail -10
cp ~/Barony/build/barony ~/.local/share/Steam/steamapps/common/Barony/barony-modded
cd ~/.local/share/Steam/steamapps/common/Barony && ./barony-modded 2>&1 | grep -E 'MYMOD|AI'
```

Configure (only needed if CMakeLists changes):
```bash
cmake .. -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DOPENAL_ENABLED=OFF -DFMOD_ENABLED=OFF -DSTEAMWORKS_ENABLED=OFF -DEOS_ENABLED=OFF
```

Service + voice in separate terminals:
```bash
python3 ~/barony-ai/service.py
python3 ~/barony-ai/voice_bridge.py          # optional: push-to-talk INPUT
```

Voice OUTPUT (TTS) is off unless both halves are turned on — this machine deliberately runs
without it:
```bash
python3 ~/barony-ai/tts_bridge.py --setup    # one time: venv + piper + 77MB voice model
BARONY_AI_TTS=1 python3 ~/barony-ai/service.py
python3 ~/barony-ai/tts_bridge.py            # optional: spoken OUTPUT
```
`.venv-tts/` and `voices/` are gitignored — the ONNX model must never be committed.

Service config is env-overridable, so a released build needs no file edits:
`BARONY_AI_OLLAMA`, `BARONY_AI_MODEL`, `BARONY_AI_PORT`, `BARONY_AI_BOOKS` (the books dir still
defaults to the hardcoded Steam path — that default is the remaining portability blocker),
`BARONY_AI_TTS` (**default `0`**), `BARONY_AI_TTSDIR`, `BARONY_AI_TTSQUEUE`.

In-game test harness: `/enablecheats` → `/summonall` → `/friendly`, then interact-recruit.
⚠ **`/friendly` did not work for co-op clients until the `MYFR` packet** — see *Multiplayer* in `FINDINGS.md`.
For most of this project the harness was therefore single-player-only without anyone noticing.

### The DLC build (Steamworks) — separate from the mod-testing build

Barony's DLC is **entitlement-only**: the content ships in the base depot and three bools gate it
(`menu.cpp:277-279`). Evidence — the Steam app manifest installs one depot (371973, 1.35 GB) and
nothing else, and the GOG unlock mechanism is a 32-byte key file, so there is nothing to download.

Exactly two mechanisms can set those bools, and they are mutually exclusive at compile time:

| Build | Mechanism | Where |
|---|---|---|
| `STEAMWORKS` or `USE_EOS` defined | `SteamApps()->BIsDlcInstalled(1010820/21/22)` | `MainMenu.cpp:26400-26408` |
| **neither** | GOG `.key` files (`mythsandoutcasts.key`, …) | `init_game.cpp:218-290` |

A `STEAMWORKS_ENABLED=OFF` build compiles the second branch, looks for GOG key files a Steam
customer does not have, and locks all DLC. That was the state of this project until Aug 21 2026.

⚠ **`-DSTEAMWORKS_ENABLED=ON` does NOT define the `STEAMWORKS` preprocessor macro.** Nothing in
`CMakeLists.txt` ever does — it only calls `find_package` and `target_link_libraries`, while every
Steam code path is behind `#ifdef STEAMWORKS`. The build **succeeds**, links `libsteam_api.so`,
references **zero** Steam symbols, and changes nothing. It looks exactly like success. Upstream's
CI must pass the define in flags; we pass it explicitly.

⚠ **SDK version must be 1.53a, not the current one.** Barony 5.0.2 targets it exactly — the
shipped `libsteam_api.so` and 1.53a agree on `SteamClient020`, `SteamUser021`, `SteamFriends017`,
`SteamUtils010`, and both export `SteamAPI_Init`. SDK 1.65 drifts on all four, drops
`SteamAPI_Init`, and removed `ISteamUtils::IsSteamRunningOnSteamDeck` (6 call sites) in favour of
`IsRunningOnSteamHardware()`. Building against 1.65 would need source patches **and** overwriting
the game folder's `libsteam_api.so`, which vanilla loads too (`-rpath='$ORIGIN'`). With 1.53a:
zero source patches, nothing in the Steam install touched.

⚠ **Steamworks makes `nativefiledialog` a REQUIRED dependency** (`CMakeLists.txt:366`) for one
call — `NFD_PickFolder` for the Workshop mod-upload picker (`MainMenu.cpp:31110`). The real
library needs GTK3 headers. `~/nfd-stub` is an API-compatible stub (`libnfd.a`, enum order matches
upstream so the real lib drops in later); every entry point reports cancelled, so only that one
Workshop dialog is inert.

```bash
# one time: SDK 1.53a from partner.steamgames.com -> ~/steamworks-153 (must contain sdk/public)
cd ~/Barony/build-steam && STEAMWORKS_ROOT=~/steamworks-153 NFD_DIR=~/nfd-stub cmake .. \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DSTEAMWORKS_ENABLED=ON \
  -DOPENAL_ENABLED=OFF -DFMOD_ENABLED=OFF -DEOS_ENABLED=OFF \
  -DCMAKE_C_FLAGS="-DSTEAMWORKS" -DCMAKE_CXX_FLAGS="-DSTEAMWORKS"
make -j$(nproc) && cp barony ~/.local/share/Steam/steamapps/common/Barony/barony-modded-steam
```

Needs the Steam client running and logged in; `steam_appid.txt` (371970) is already in the game
folder, which is what lets a self-built binary init Steam at all.

**Two binaries on purpose**, two build trees, neither disturbing the other:

| Binary | Build tree | Use |
|---|---|---|
| `barony-modded` | `build/` | mod testing — direct-IP, two local instances |
| `barony-modded-steam` | `build-steam/` | playing — DLC + Steam lobbies |

**Verifying which path compiled** (both silent at runtime — the Steam path never `printlog`s DLC
status; only the GOG branch prints `[LICENSE]`):
```bash
strings barony | grep -c "Myths and Outcasts DLC license key found"   # 1 = GOG path, 0 = Steam path
strings barony | grep -c "store.steampowered.com/dlc/371970"          # 1 = Steam path
nm -D -u barony | grep -ic steam                                      # 0 = STEAMWORKS macro missing!
```

⚠ **The Steam build changes multiplayer shape.** `directConnect` becomes `false` (`main.cpp:319`),
so hosting creates a Steam lobby with friend invites instead of direct IP — much better for
non-technical friends, but it likely breaks the two-local-instances test setup. That is the reason
`barony-modded` is kept as-is.

## Ollama configuration

Systemd drop-in at `/etc/systemd/system/ollama.service.d/override.conf`:
```
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

`ask_ollama` sends `options: {"num_ctx": 16384}`, `keep_alive: "30m"`, and logs `[SERVICE-DBG] prompt ~N tokens`.

Verified: 5.9 GB, **100% GPU**, CONTEXT 16384, warm replies ~2-4s. Prompts run **~650 (rat) to ~3100 (human spy with events)** tokens — nowhere near the ceiling. **Context size is not the limiting factor; the 8B's attention is.**

---

## Reference

### Robustness
`parse_reply(raw)` — strict `json.loads` first, then a regex fallback for malformed 8B output. Tested against 9 patterns. Logs `(JSON malformed - recovered via fallback)`; observed firing in real play.

Display: status lines (thinking/listening/transcribing/executed) are `printlog` (terminal only). **Double-print fixed** — every line used to print twice (a `MESSAGE_MISC` *and* the `MESSAGE_CHAT` broadcast); now one chat line + one bubble. Each conversation slot carries its own `speaker_uid` (whose head the bubble goes over) and `prefix` (the `[taunt]`/`[overheard]` label, or the co-op `"<Player>'s <Follower>: "` attribution).

---

### Where things actually are (`maps/levels.txt`)

Worth having written down, because two assumptions here were wrong:

| Floor | Map | Floor | Map |
|---|---|---|---|
| 1–4 | mine | 20 | **boss — Baron Herx** |
| 5 | minetoswamp | 21–23 | hell |
| 6–9 | swamp | 24 | **hellboss — Baphomet** |
| 10 | swamptolabyrinth | 25 | **hamlet — the TOWN** |
| 11–14 | labyrinth | 26–29 | caves |
| 15 | labyrinthtoruins | 30 | cavestocitadel |
| 16–19 | ruins | 31–34 | citadel, 35 sanctum |

**Hamlet is level 25, but there is more than one route to it, and Hell is OPTIONAL.** Beating the
Baron can take you there directly; going through Hell (21–23) to Baphomet (24) is the other way.
Do not describe the town as "gated behind Baphomet" — that was wrong.

Supporting evidence in the source: `actWinningPortal` only appears on `"Boss"` / `"Hell Boss"` when
`SV_FLAG_CLASSIC` is set (`actladder.cpp:445`) — in the default expanded campaign it hides itself,
so killing Herx does **not** end the run. It is a genuine midpoint, which is also why friendship 50
fits the Herx secret.

**The routing does not affect region resolution**, which is the thing that matters here: custom
portals jump by map NAME, setting `skipLevelsOnLoad = levelToJumpTo - currentlevel` where
`levelToJumpTo` is that map's index in `levels.txt` (`actladder.cpp:1538`). So arriving at Hamlet
means `currentlevel == 25` by whichever road.

**The game has its OWN floor→region mapping — use it as the reference** (`mod_tools.cpp:17273`):
`1-4 mines · 6-9 swamps · 11-14 labyrinth · 16-19 ruins · 20 herx lair · 21-23 hell ·
24 molten throne · 25 hamlet · 26-29 crystal caves · 31-34 arcane citadel · 35 citadel sanctum`,
with 5/10/15/30 explicitly called "transition floor". Our table matches it; we assign the four
transition floors to the region they lead into, which is the only deliberate difference.

**`floor_to_region` was misaligned with this and wrong for 21 of 35 floors** — every floor from 19 up,
including telling floor-25 townsfolk they were in *hell*. Six of the lore file's 19 location profiles
were reachable; `hamlet`, `crystal_caves`, `citadel`, `lich_bastion`, `gnomish_mines` and `minetown`
never resolved at all. Now fixed, and **resolution is by `map.name` first**, mirroring Barony's own
`doorFrameSprite()` (`maps.cpp:239`) — the only thing that works for secret levels and DLC maps,
since floor numbers cannot distinguish them. The floor table is now just the fallback.

⚠ **`hamlet.lmp`'s internal `map.name` is `"Mages Guild"`, not "hamlet"** — `files.cpp` already
special-cases that string. Guessing the name would have failed silently.

**Prompts now name the place, not just the floor.** Grounding bullets said `ABOUT THIS PLACE (hamlet)`
but nothing ever stated where the speaker was standing, so a Hamlet townsman said *"I've lived in
these mines my whole life"* — picking up "the Mines" from the SETTING line. `place_name()` fixes it
for NPCs and followers alike. Verified: 4/4 said Hamlet after, and a follower on floor 20 named
Herx's stronghold unprompted.

### Session logging (for the playthrough)

One append-only JSONL timeline per run at `logs/session-<stamp>.jsonl`. **The C++ side pushes into
the same file** (`POST {"log":...}` → `mymod_log()`), which is the point — correlating *"the bubble
never appeared"* with what the service actually returned needs one ordered timeline. Engine-only
facts are logged there because they are invisible service-side: boons landing, an item actually
being identified, `MYAI` arriving, friendly fire, deaths, NPC engagement.

- **`/ailog <what went wrong>`** — a player note straight into the timeline. The single most useful
  line in a playtest log is the human saying where to look; `logreview.py` prints these first.
- **`/aistatus`** — live state (partner, busy flags, follower watch, Herx debuff).
- **`python3 logreview.py`** — summarises the newest session: errors, malformed-JSON recoveries,
  player notes, latency percentiles, prompt size, events with volume warnings, per-follower
  relationship trajectories, boons/secrets/identifications, NPC conversations.

⚠ **`logreview` reports peaks, not just first→last.** First-vs-last hides a round trip: trust built
by fighting together and then destroyed by friendly fire ends where it started and reads as "no
change", when it is the most interesting thing that happened. Shown as `trus 0->0 peak 16`.

⚠ **A follower asked the same thing twice used to answer word for word.** "Stay consistent; do
not contradict yourself" makes repeating the sentence the most obedient reply available —
observed live, a follower echoed its own introduction back verbatim on the next exchange. The
merchant had the same bug and the same fix; the wording just never made it into
`_follower_memory_block` / `_npc_memory_block`. Both now add *"staying consistent means not
contradicting the FACTS — it does NOT mean repeating those sentences"*. Verified: the facts held
across three askings ("from Hamlet, born and raised", "Herx's regime") while the phrasing moved.

⚠ **Prompt size is now ~3.5k tokens** and `logreview` warns above that. Context was never the limit
here — attention is — so this is worth watching as more sections are added.

## Hard-won prompt rules (digest)

Full evidence for every line here is in **`FINDINGS.md`** — read the relevant section there before
editing a subsystem's prompts. These are the rules that generalize across the whole project.

1. **Name the forbidden SPEECH ACT, never the forbidden TOPIC.** *"Saying you do not know"*,
   *"opening with 'Alright'"*, *"'some say…' followed by an answer"* all work. Naming a topic
   *supplies* it — mentioning gators is how gators get into the reply. This has backfired **every**
   time it was tried (drunk remark 7/8 → 2/8; spec §26; the swamp "territory" priming).
2. **Whichever of two conflicting instructions lands LATER wins.** Measured three times. So hard
   limits are emitted **last**, and any carve-out from them (`_secret_override`, `MERC_LATE`) must
   land *after* them. The Herx offer sat before the limits and converted 40%; moved after, 81%.
3. **Position is a lever.** The true last slot — after the action rules — is the strongest;
   the spy crack measures 20/20 there and scored 3/8 five blocks earlier.
4. **The server chooses the content; the model only phrases it.** Boons, the spy crack, sabotage
   tells, the map pitch, names, haggle outcomes, identification claims, rat-panic shapes. Any single
   literal choice left to the 8B comes back canned — and placement alone will not fix it.
5. **State a limit as a fact about the character AND as a prohibition.** `restricted_knowledge_never`
   (*"You have never once set foot inside the Temple… you have no idea what is in there"*) beside the
   `You do NOT know:` line. Either alone is markedly worse: ~5/10 vs ~8/10.
6. **`unless X` is an escape hatch** — the model takes the exception. Limits are absolute; conditions
   are resolved **server-side** (`restricted_knowledge_lifts`), never by the model.
7. **Say who the person is.** *"This is YOUR adventurer"*, *"it is the adventurer who is drunk, not
   you"*, *"say it TO them, not about them"*. This subject/role clarification has paid **four**
   separate times and has **never once backfired** — the exact opposite of naming a topic.
8. **Read the outputs, never trust the regex score.** Six probes in one session reported a failure
   the replies did not show (*"have not the strength"*, *"it's finally still"*, *"you're one of us"*).
   A bad probe nearly cost a working prompt.
9. **Ask for ONE thing, not two.** Requiring recognition *and* concession got recognition 8/8 and
   concession 0/8. Pin the one that matters to the END of the line.
10. **Never quote bad examples in the prompt** — they prime. Move them to a code comment beside it
    (the invented place names went 2-still-invented → 0/8 that way).
11. **Scarcity.** Two mandatory unprompted behaviours must not stack in one reply — the later one
    steals the turn (the spy crack ate identification; the introduction suppresses boons).
12. **Measure before rewriting a class of prompt.** Three limit probes scored 6/6 *before* a planned
    rewrite; the work became correctness tidy-up rather than a bug fix, and stayed narrow.

---

## Decisions on record

- **TTS: optional, off by default, and text stays authoritative.** The original "no" was about not
  letting speech constrain replies. That still holds and is *why* the build is additive: nothing
  about prompt, reply length, or the shared feed changed, the synth reads whatever the model already
  wrote, and with the flag off the cost is one boolean test per reply. Runs **piper on CPU at RTF
  0.034**, so the "no extra VRAM atop 8B + Whisper" objection is answered rather than accepted —
  the GPU keeps all 8 GB for the 8B. Mispronounced generated names remain a real, unfixed cost —
  the reason it is opt-in rather than default.
- **Obedience is earned, not gated in C++** — prompt-driven, so it stays probabilistic and characterful. A hard C++ friendship gate is a possible later layer.
- **ATTACK stays diegetic** — target-based attack needs cursor-aim, parked.
- **The event log is structured records, not flat strings** — so new event types slot in with just a type + importance.

---

## Open / next

**The playtest is the highest-value next step.** Every number below is a guess that has never met a real run:
- Is friendship 50 reachable by Herx? (~2 hours of play to reach him)
- Do boons read as companionship or noise?
- Are spy tells catchable *live*, not side-by-side?
- Do items at a follower's feet get noticed?
- Do the debuff numbers matter against a 1250 HP boss? (`initLich` logs the applied stats)

**Multiplayer is built but has never met a second machine.** It compiles, the service side is
verified by curl (two players concurrently, independent state, correct routing), and the host path
is unchanged from the single-player path that already works. Untested in an actual session:
- Does a client's `MYAI` actually arrive and come back as chat + bubble?
- Do shared bubbles render for a player who is nowhere near the speaker? (`createDialogueTooltip`
  is keyed by uid; if the entity isn't loaded on that client the bubble presumably just doesn't draw.)
- Does `MYNM` land before the player next opens the follower HUD?
- Four players talking at once: Ollama queues on one GPU, so replies serialize. How bad does that
  feel? A queue-depth cap or a "your follower is thinking" nudge may be needed.
- Recruitment attribution for a *client's* follower — `mymod_ownerOf` is verified by reading, not by play.

**Resolved (kept for the facts, not the history):**
- **The false secret works.** A spy tells a false variant and reports `0:<uid>`;
  `monster_lich.cpp:99` guards on `debuff > 0`, so nothing is applied. It had simply never been
  rolled (7% allegiance), not been broken.
- **Chat history stores both halves.** `record_follower_interaction` keeps both sides and
  `_follower_memory_block` renders them under the NPC path's wording. Bounded at 6 kept / 3 shown
  / 140 chars so a long conversation cannot inflate the prompt, and old string-format entries from
  a run already in flight are skipped rather than crashed on. 16 assertions in `test_memory.py`.

**Designed, not built — the peaceful Herx route.** `actWinningPortal` already contains the answer: the portal **exists on the boss floor, invisible**, and reveals itself when its per-tick scan finds no `LICH` or `DEVIL` alive. So a spared-Herx ending needs only (a) a flag excluding a pacified Herx from that scan and (b) clearing his hostility. No new entity, no replaced code path. The chain that *earns* it — merchants, testimony, leverage — is the real project and is undesigned. Design the failure mode first: a half-fired peaceful path could strand the player with a non-hostile boss and no exit.

**DLC:** *Deserters & Disciples* shipped Jan 29 2026 with *Instruments of Destruction Part 1* (which removed Magic/Casting/Swimming and revamped magic into Sorcery/Thaumaturgy/Mysticism). **Part 2 is the upcoming one.** New races will need entries in `race_lore.json`, `comprehension.json`, and the lore file's denizen profiles or they fall through to generic defaults. (`follower_names.json` is the exception — an unknown race still draws from its comprehension group, so naming degrades to a fitting pool rather than a broken one.) **Answered Aug 21 2026: a `STEAMWORKS_ENABLED=OFF` build can NOT** — it compiles the GOG `.key` branch and locks everything. `barony-modded-steam` (SDK 1.53a, `-DSTEAMWORKS`) unlocks all three packs against real Steam entitlement; all DLC races confirmed present in character creation. See **The DLC build** under Build & run. The lore-file gap above is now live, not hypothetical.

---

## Working style

Prove each piece in isolation before wiring it — curl-test the service before touching the game. Build service-side first (testable in seconds), then the C++ hook. Read the game's own source rather than guessing at APIs; several features turned out to be near-free because Barony already had the mechanism (`actTrapSabotaged`, `Stat->name` in the HUD, the invisible winning portal).

Commit after each working session, not in big batches.

No unsolicited break suggestions or timeline estimates.

### Editing discipline (from painful experience)

These cost many rounds before Claude Code:
- **Verify edits by the most specific unique token** — a function *call* with its name, never a bare word. `fought_alongside` also appeared in an unrelated comment, so every "present: True" check passed while the actual code was never inserted (~8 rounds lost).
- **Never bound a replacement by "up to the next `def`/function"** — that silently deleted `build_prompt`'s grounding block, leaving conversation with no canonical grounding *and no hard limits* for several tests. The tell was the prompt token count not moving.
- When a change doesn't move the token count, **`grep -n "^def "` to map the file** is the first diagnostic, not the fourth.
- Assert anchor uniqueness before writing (there are four copies of `Entity* pl = players[clientnum]->entity;` in the old `consolecommand.cpp`).
- **Pass primitives across a cross-file boundary and resolve names where the function links.** `mymod_recordEvent` takes the race as a raw int enum because `getMonsterLocalizedName` links in `mymod.cpp` but not in `actmonster.cpp` — passing the enum dodges the linker error.
- Watch tab-vs-space indentation; reconstructed multi-line anchors usually fail.
