# Barony AI-NPC Mod — Project Context

Drop this in as `CLAUDE.md` in either repo (or paste at the start of a fresh chat).

---

## What this is

An AI-NPC mod for **Barony** (open-source C++ roguelike, v5.0.2), built from source on **Nobara Linux**. Thin C++ engine hooks ↔ a local Python HTTP service that owns lore, prompts, and social state, calling a local `llama3.1:8b` via Ollama. Fully local, no cloud.

**Hardware:** i7-9700K, RTX 2070 Super (8GB), 32GB RAM.

**Release intent:** Nexus Mods. Co-op friends are non-technical and on Windows; dev env is Linux. Design converges on: thin mod + configurable backend endpoint + setup guide, host-authoritative.

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
⚠ **`/friendly` did not work for co-op clients until the `MYFR` packet** — see Multiplayer below.
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

## Systems built (all verified in-game unless noted)

### Foundation
Async via detached `std::thread` + shared globals + `mymod_pollAI()` per frame (no freeze). Two-way command loop: `/aicommand` finds the player's follower via `Stat->leader_uid`, service returns `{speech, action}` ∈ FOLLOW/DEFEND/WAIT/ATTACK/NONE. **ATTACK is diegetic only** — Barony's combat AI handles fighting. Push-to-talk voice (hold V → faster-whisper small.en, cuda/float16). Speech bubbles via `createDialogueTooltip(uid, DIALOGUE_NPC, "%s", reply)` — **the `"%s"` guard is required**. Polymorph-as-comprehension (see below — it was dead code until Aug 21 2026). 34 canonical books injected per race. `/aiserver <url>` for BYO-model.



### Race coverage — what every creature knows about being itself

Audited against all 49 races `getMonsterLocalizedName` can return (resolved from `lang/en.txt`,
not guessed — `LICH_ICE` is `"frosty lich"`, `BAT_SMALL` is `"bat"`, `EARTH_ELEMENTAL` is
`"earth sprite"`).

| Source | Before | After | What it feeds |
|---|---|---|---|
| `race_lore.json` | **6** | **49** | `CHARACTER GUIDANCE:` — every prompt, grounded or not |
| `race_profiles` | **11** | **49** | `YOUR OUTLOOK CENTERS ON:` + `TYPICAL TEMPERAMENT:` |
| comprehension groups | 12 | 49 | see above |
| `individual_denizen_research` | 34 | **49** | `CANON:` / `YOU MAY PLAUSIBLY HAVE:` |
| `denizen_context_profiles` | 33 | **49** | `YOU ARE (category): baseline` |
| TTS casting | 30 | **49** | voice band + sox chain |

⚠ **`race_lore.json` was the highest-leverage file in the project and the thinnest.** It is a flat
`race -> one short string` map injected as `CHARACTER GUIDANCE` on **every** conversation, taunt and
babble. 43 of 49 races were falling through to `default` — *"A creature of the dungeon depths
beneath Hamlet. Crude and wary of intruders."* — which is precisely the shapeless prompt that
invites fabrication. Style to match when adding more: **what they are, how they talk, what they
care about**, in 1–3 sentences.

⚠ **`RACE_LORE.get(race.lower())` had the multi-word bug `_lore_key()` exists to prevent.** It never
bit only because all six original keys were single words; `crystal golem` would have silently
missed an underscored entry. The lookup now tries `_lore_key()` first and the raw form second, so
either convention resolves.

Measured after, same question to each, races that previously had nothing at all:
*dryad* — "Roots are patient, Ada. I care for growth in seasons to come."; *myconid* — "dampness
and darkness are important to us" (the plural voice is authored); *frosty lich* — "Stillness
matters to me. I seek to remove the heat from this place."; *gremlin* — "Can't fix what's broke,
but I can show you how it came apart." Prompts land at **~850–1000 tokens**, so the additions are
nearly free against the 3.5k warning line.

⚠ **The generic-vs-named split is what `lich` and `devil` needed.** They are the archetypes, not
Baron Herx and Baphomet — those have their own entries and are chosen by `npc_name`, never by race.
Both new entries say so in `knowledge_boundary` by name, because the model will otherwise happily
promote itself to the boss. Verified 3/3: *"I am not the Baron, but his... associate."*

Authored to the file's own `mechanics_rule` — mechanics inform behaviour but cannot establish
culture, history or morality — so `canon_facts` stay observable and personality lives in
`safe_inferences`. Where real canon is thin (dryad, myconid, hologram, adorcised_possession) the
entries say little and forbid inventing more, rather than padding: `adorcised_possession` is told
to leave the bound spirit's origin unknown *because that unknown is the point*. Sources are honestly
marked `Gameplay behavior` / `Compendium` rather than citing books that may not exist.

Two boundary probes after: a duck asked for stairs and guards gave no real information; a hologram
asked what projects it did not name a machine.

`gyrobot` turned out to have a denizen entry but **no** `denizen_context_profiles` entry — a
pre-existing hole nothing had noticed, now filled.

⚠ **`"sentry bot"` was keyed with a space and the game says `sentrybot`** — so that cast was
authored, listed, and **never once reachable**; it fell through to `DEFAULT_VOICE` silently. Exactly
the `_norm()` trap the file already warns about, sitting in the file that warns about it. There is
now a check that every VOICES key is normalisation-stable (`_norm(key) == key`), which would have
caught it.

18 casts added — lesser beasts, fey, elementals, the two elemental liches, and the remaining
constructs (bandpass on the constructs, since a robotic result is *correct* there). **All 57 casts
— 49 races plus 8 named characters — were validated by actually rendering**, on both tiers: piper
synthesis at the cast speaker id and length-scale, then the `fx` chain pushed through sox; then
espeak-ng at the cast variant/speed/pitch with the `efx` chain. Sox writes to `-n` (null sink), so
validation renders without playing anything. All 33 referenced espeak variants confirmed present in
`/usr/share/espeak-ng-data/voices/!v`.

The validator lives in the scratchpad rather than the repo; it is worth re-running after any cast
edit, because a bad sox chain fails for exactly one race and nothing else notices.

**Still open:** `cave crawler`, `cyclops` and `dwarf` are VOICES keys that match no race the game
can return — harmless, but they are cast for creatures that never arrive.

### Comprehension — polymorph only (was dead code)

⚠ **The filter never fired once, in any session.** `can_understand` opens with
`if not player_race: return True`, and **no payload the mod built ever contained
`player_race`** — the only two callers are the taunt and ambient payloads
(`mymod.cpp:541`/`560` as they were), and `mymod_payloadHead` sends `player_name`, not race.
Documented as a built feature, voiced in the TTS section, and inert the whole time. Found by
auditing race coverage, not by anything failing.

**`player_race` is the SHAPESHIFT form, never the chosen race** (`mymod_polymorphRace`, reading
`effectShapeshift` = `skill[53]`, empty when `NOTHING`). This is the load-bearing decision. Sending
the chosen race would mean a **vampire, succubus or incubus player understands nobody** — none of
them are in a group, `_group_of` returns `None`, and `pg is not None` fails — and DLC pack 1/2 make
exactly those races playable. Omitting the field when not polymorphed means normal play is
bit-for-bit unchanged, whatever race you picked.

**All 49 races the game can report are now in exactly one group** — `humanoid, beast, undead,
demonic, construct, fey, amorphous`. Coverage matters structurally, not cosmetically: a race in
*no* group understands nobody but its own kind, so a missing entry is a silent muting. Existing
memberships were preserved exactly; the change is purely additive.

`noise_for` now falls back **race pool → `default_<group>` → `default_beast`**. Without the group
step a lich and a duck both came back `*an animal snarl*`. 30 noise pools now, up from 4.

Verified live by curl: not polymorphed → real dialogue from a lich; rat hearing a human →
*"\*speech in a tongue you do not share\*"*; rat hearing a spider → real dialogue; duck hearing a
lich → *"\*a dry rattle from somewhere behind the teeth\*"*; lich hearing a vampire → real dialogue.

⚠ **Untested in game** — the C++ half compiles but polymorph has not been exercised in a real run.

### Relationship-based disclosure (design spec §32)

What a character is **willing** to tell you, as distinct from what they know. Four tiers, each
gated on the whole vector rather than friendship:

| Tier | Opens when |
|---|---|
| what is around you | always |
| practical knowledge | `respect ≥15 or trust ≥15 or friendship ≥15` |
| your own history | `friendship ≥20 and trust ≥18 and fear <45` |
| dangerous knowledge | `trust ≥45 and fear <40 and resentment <35` |

Reproduces §32's worked examples: *respected but not liked* → practical open, personal closed;
*friendly but afraid* → low-risk only; *deeply trusted* → everything.

**The distinction that makes this more than another limits block:**
- HARD LIMITS = *"I genuinely do not know"* → say so plainly and stop
- DISCLOSURE = *"I know and will not say"* → **visibly decline**, so the player can tell there is
  something there

Collapsing the second into the first destroys the signal, and claiming ignorance is exactly the
route the 8B takes unless it is forbidden by name. Verified: closed tier **5/5 declined visibly,
0 claimed ignorance** (*"Family's not something I'm comfortable talkin' about"*).

⚠ **The OPEN list has to be directive too.** Listed as merely permissive ("what you are willing to
discuss"), the closed block's strong language bled into general reticence and the control only
opened up **2/5**. Adding *"if they ask about anything on THIS list, answer properly with real
specific detail — brushing off something you ARE willing to discuss is WRONG"* took it to **6/6**.
Both halves need the failure route named, not just the prohibition.

**Hard limits are now emitted LAST** (`_persona` returns them separately so callers place them
after the disclosure block). The two instructions genuinely conflict about whether "I don't know"
is correct, and whichever lands later wins.

⚠ **HOW TO WRITE A HARD LIMIT THAT ACTUALLY HOLDS.** This took a full session to get right and
the first two diagnoses were wrong, so the order matters:

| Rendering | Temple probe |
|---|---|
| `"You do NOT know: Temple internal layout **unless visited**"` | ~3/10 |
| `"You do NOT know: the Temple's internal layout"` (conditional removed) | ~4/10 |
| first-person experience statement **alone** | ~5–6/10 |
| **experience statement AND prohibition together** | **~8/10** |
| absolute/abstract topic (`later-region politics`) — for reference | 10/10 |

Three separate lessons:

1. **`unless X` is an escape hatch** — it parses as "you don't know this, EXCEPT you might", and the
   model takes the exception. All 12 such entries in `restricted_knowledge` are now absolute; the
   conditions moved to a sibling `restricted_knowledge_lifts` so a future per-NPC background system
   (§10.1) can lift a limit **server-side**, which is the only place a condition can be resolved.
   Same principle as boons and the spy crack: *the server resolves conditions, the model never does.*
2. **But that was not the real cause.** Removing the conditional barely moved the number. The model
   fails on limits it can **picture** — it has strong genre priors about what a temple contains and
   fills them in. `later-region politics` scores 10/10 precisely because there is nothing to fill in
   with. `LIMITS_HEADER` now forbids that route by name ("do NOT describe what such a place is
   probably or usually like… you are not being asked what temples are like in general").
3. **State the limit as a fact about the character, not only as a prohibition.** The lore file now
   carries `restricted_knowledge_never` — first-person statements like *"You have never once set
   foot inside the Temple. You have seen it only from the outside, and you have no idea what is in
   there."* These are emitted **alongside** the `You do NOT know:` line, not instead of it: the fact
   makes the character inhabit the ignorance, the prohibition keeps it enforceable, and each alone
   is markedly worse than both. It also produces *better* answers — they describe the exterior,
   which is legitimate and characterful, instead of stonewalling.

**All 47 rendered `restricted_knowledge` items now carry a `restricted_knowledge_never`
statement** — there are no prohibition-only limits left. Confirmed to generalize beyond the
Temple: *"what does Hell look like further in, past where we've been?"* scored **~1/8 without**
the statements and **~5/8 with** them. That probe is harder than the Temple's, because the
character *has* been in Hell and the limit is about the boundary of their own travel.

**Writing new ones:** first person, state what they have NOT experienced, and END by naming the
ignorance outright (*"…and you have no idea what is in there"*). Keep the item in
`restricted_knowledge` as well — the pair is what works. `build_lore_context` slices
`restricted_knowledge[:3]`, so a fourth entry would never render and its statement would be dead
weight; all profiles currently have ≤3.

⚠ **When testing anti-fabrication, probe BOTH kinds.** An abstract topic will score 10/10 and tell
you nothing; a concrete, nameable place is where the mechanism actually strains.

**`npc_rules` are grounding, not ignorance — they no longer render as limits.** They describe what
a creature of this kind plausibly knows *here*, and several of them **grant** knowledge ("Gnome
residents should have strong colony/resource knowledge"), so rendering them under
`LIMITS_HEADER` ("you genuinely do not know these things") was a flat mislabel. They now render
in the facts as `HOW YOUR KIND FITS IN HERE:`.

**11 rules were addressed to the lore author, not to a character** — *"Separate Bram's Castle from
the Haunted Castle"*, *"Do not make every resident a direct descendant of a survivor"*. They cost
prompt tokens and say nothing a character can act on. Moved to a `authoring_notes` key per profile:
kept in the file, never rendered.

**`knowledge_boundary` was left almost entirely alone, on purpose.** 53 of 75 are author-phrased
("Do not import Hindu theology as Barony world history"), but unlike `npc_rules` those are directly
actionable by the model — it *is* the author of the character's speech — and they measured working.
Only the 6 with conditions the runtime cannot resolve were made absolute (*"No human-level abstract
history ~~unless a special individual is elevated~~"*), with the original preserved in
`knowledge_boundary_lift`. Four conditionals about canon/sources (`merlin`, `shelob`, `bram_kindly`,
`succubus_consort`) read correctly as written and were left.

⚠ **Lore lookup was silently missing most of the file.** Two separate key bugs, both found by
auditing rather than by anything failing visibly:

- **Multi-word races never matched.** The game hands back display names with spaces
  (`"crystal golem"`, `"earth sprite"`, `"revenant skull"`, `"gnome thief"`) while the lore keys use
  underscores. `_lore_key()` now normalises (lowercase, spaces/hyphens → `_`, strip apostrophes).
- **44 of the 75 `individual_denizen_research` entries were unreachable.** They are keyed by
  *individual*, not race — `merlin`, `king_arthur`, `lilith`, `bram_kindly`, `gharbad`, `algernon`,
  `baron_herx` — but lookup only ever used the race. Every named NPC fell through to generic race
  lore, so all that researched canon was dead weight. `build_lore_context(..., npc_name=)` now
  prefers the individual's entry over their species'.

Verified: King Arthur asked *"who are you, and what is that sword?"* names **Dyrnwyn** 4/4, where an
unnamed human gives a generic "old family blade". And his boundary — *"Do not import Camelot,
Guinevere, Lancelot"* — refuses 6/6, a rule that **could never have fired before**.

⚠ **`npc_name` is passed only from `build_npc_prompt`, never for followers.** A follower's name is
chosen by the model, and a follower who names itself "Merlin" must not inherit Merlin's canon.

`npc_rules` is now sliced `[:3]` rather than `[:2]` — three rules (in `gnomish_mines`, `mines`,
`swamp`) were authored but never rendered. `restricted_knowledge[:3]` still covers every profile.

⚠ **Measure before rewriting a limit class.** Three probes — authoring-instruction leakage at the
Temple, a permission-under-prohibition Swamp goblin, and a class restriction on a Mine rat — all
scored **6/6 correct** *before* any of this. Neither class was behaviourally broken; the changes
above are correctness and prompt-cost tidy-ups, not bug fixes, and were kept narrow for that
reason. Temple probe after: **7/8**, no regression.

⚠ **Spec §26 (information provenance) was tried here and REMOVED — do not re-add it as a prompt
instruction.** *"When you pass on something you did not witness, say how you came by it"* reads
harmless but explicitly licenses the hedged-hearsay route (*"I don't know, but I've heard..."*)
that `LIMITS_HEADER` exists to kill. Fencing it with a "this never applies to your hard limits"
carve-out did not clear it — the phrase primes "I've heard" merely by being present. If provenance
is wanted, it must come from the structured `event_log` (which already carries a `provenance`
field), never from free text.

### Relationships — the vector (design spec §4.1)

`friendship` alone could not express *"likes you but does not trust you"*, which the spec calls the
whole point. Seven dimensions now, **none ever shown to the player as a number**:
`friendship, trust, respect, fear, resentment, dependence, curiosity` (0–100, curiosity starts at 15).
`adjust(st, **deltas)` is the only way any of them move, and it raises on an unknown dimension name
so a typo can't silently create a dimension that nothing reads.

**What moves them**
- **Events** — `EVENT_DIMENSIONS` gives each event type its own explicit deltas (friendship
  included), because importance ranks a *memory* while the deltas say what it **cost or built**.
  Seven types now, and the negative half is what makes the vector work at all:

  | Event | Detected by | Effect |
  |---|---|---|
  | `recruitment` | interact-recruit hook | curiosity, dependence |
  | `fought_alongside` | combat→calm edge | trust, respect, dependence |
  | `healed_by_player` | HP jumps ≥15% max near their leader | trust, friendship, −resentment |
  | `wounded` | HP crosses **down** through 35% max | fear, dependence |
  | `hurt_by_player` | `updateEntityOnHit` — their own leader struck them | **−friendship, −trust, +fear, +resentment** |
  | `left_behind` | >25 tiles adrift for 25s | −trust, fear, resentment |
  | `ally_died` | a watched follower vanishes; fired for the **survivors** | −trust, fear, resentment |
  | `resummoned` | a persistent identity binds to a new body (summons/bots) | dependence, curiosity — see below |

  Only friendly fire needed a new upstream hook. Everything else rides the follower scan
  `mymod_ambientTick` already runs each frame (`mymod_watch`). Two traps handled there:
  a **level change also empties the map**, so the watch is keyed per floor or every staircase
  reads as a massacre; and *wounded* fires on **crossing** the threshold, not on sitting below it.
- **Talk** — friendship + curiosity only, still metered by `CHAT_CAP_PER_FLOOR`. Trust and respect
  are deliberately *not* buyable by talking; they have to be earned by deeds.
- **How the player speaks** — `PLAYER_TONE` does a coarse keyword read (praise / threat / apology /
  personal question). Crude and it misses plenty; effects are small and capped at
  `TONE_CAP_PER_FLOOR = 3` so twenty "thank you"s can't buy respect.

**Rendering: behaviour, never feeling.** `DIMENSION_BEHAVIOR` gives each band a line saying what to
*do* ("Ask them a question about themselves, unprompted"), never what to feel. Capped at the 4
strongest — seven lines every turn flattens into noise.

⚠ **Tension conditions are RELATIVE, not absolute — this was got wrong first.** The dimensions
grow at wildly different rates: friendship is deliberately slow (100 = a whole playthrough) while
fear and resentment jump in a single event. Absolute thresholds like *"friendship ≥ 40 AND
trust ≤ 15"* were unreachable in an actual run — a full troubled playthrough fired **zero**
tensions. Now phrased as gaps (`friendship - trust >= 15`) and calibrated so a **clean run fires
none, a troubled run 1–2, an abusive run 3**. Restraint is the point (spec §36).

**`DIMENSION_TENSIONS` is the part that earns the vector.** A pair like high friendship + low trust
is exactly what the 8B smooths into plain friendliness, so each tension names the smoothing route
and forbids it — the same technique as the hard-limits and spy-crack blocks, and it is placed after
the plain lines because position is a lever.

Measured, same question to the same character:

| Condition | Result |
|---|---|
| friendship 60 + **trust 60** (control), *"anything you haven't told me?"* | **0/5** held anything back |
| friendship 60 + **trust 5** (tension) | **4/5** held back — *"better left unsaid for now"*, *"Could be nothing, but…"* |
| **respect 70 + friendship 5** | 5/5 purely tactical, no warmth |
| respect 70 + friendship 70 (control) | 3/4 opened *"Well, Ada"*, volunteered personal detail |
| **fear 70 + resentment 45**, *"wait here"* | 5/5 clipped — *"Alright... I'll wait here."* |
| trust 70 + fear 0 (control), same order | 4/4 warm, protective, pushed back |

Verified end-to-end from real event histories, same order (*"I need you to go in first"*):
clean run 5/5 cheerful — *"Aye, boss! I'll go first."*; troubled run 5/5 **obeyed resentfully** —
*"Fine, you want me to go first? I'm not getting killed again because of your recklessness."*
One reply cited *"the last time you left me behind. Floor 7"* unprompted, so the structured event
log is being recalled specifically rather than as general mood.

**Obedience now comes from the vector, not friendship.** `compliant = 0.4·friendship + 0.3·trust +
0.3·respect + 0.25·fear − 0.35·resentment`. Someone can obey out of respect without warmth, or out
of fear without loyalty, and resentment eats compliance built by any of the others. When fear
dominates friendship an extra line fires — obey, but *"a little too quick, a little too flat"* — and
that produced the sharpest behavioural separation of anything tested so far.

⚠ All existing friendship gates (naming ≥5, boons ≥10, spy crack ≥30, Herx ≥50) still key off the
`friendship` dimension and are unchanged.

### Relationships
`follower_state[uid] = {friendship, events, event_log, name, race, allegiance, motive, last_boon_floor}` in service RAM, within-run. Friendship hidden from the player. Chat capped per floor; **deeds are meant to drive the climb to 100**. Obedience scales with friendship (f≤4 refuse risky with action NONE; f≤9 grumble; f≥10 obey).

### Lore retrieval
`build_lore_context(race_l, floor, budget=16)` walks: entity profile → `canon_facts` (4) → `safe_inferences` (2) → race worldview + axes → location `canon` (2) + `high_value_local_knowledge` + `local_population`. Constraints: `knowledge_boundary`, `restricted_knowledge`, location `npc_rules` (2). `floor_to_region`: 1-4 mines, 5-8 swamp, 9-13 sand_labyrinth, 14-18 ruins, 19-24 underworld, 25+ hell.

**⚠ THE MOST IMPORTANT PROMPT FINDING:** constraints worded as "do not claim otherwise" failed **5/5** — the model hedged and answered anyway ("whispers say silver..."). Naming the *rhetorical route* took it to **0/5**. The working wording:

> HARD LIMITS ON WHAT YOU KNOW. You genuinely do not know these things: / If asked about any of them, say plainly and in character that you do not know, and STOP. / Do NOT guess, speculate, theorize, or pass on rumors about them. Hedged answers are FORBIDDEN: / 'some say...', 'whispers speak of...', 'perhaps it is...', 'I have heard...' followed by an answer / counts as claiming and is wrong. An honest 'I do not know' is always the correct reply.

**Generalize this:** at 8B, forbidding a *conclusion* without forbidding the *evasion pattern* gets routed around. Name the specific pattern. Verified to generalize — 9-10/10 across 5 races, 3 regions, and topics not in the limits block at all.

### The short forms: ambient babble and taunts

⚠ **Babble did not know where it stood.** The ambient payload never carried `map.name`, so the
prompt said *"YOU ARE: a shopkeeper on dungeon floor 25"* — to a shopkeeper standing in the town
of Hamlet. Exactly the bug `place_name()` was written to fix for conversation, still live here
because the fix was made service-side and this payload was never updated. Taunts had it too.
Both payloads now send the map.

Measured before → after, same lines:
*"Still a lot of junk." / "Another dull night."* → *"Another fine day for inventory." / "Running
low on candles."*; a demon in Hell went from *"Foolish mortals will never find this level"* (game
speak) to *"Fools still think they can escape"*; a human in the Mines now says *"Another day,
another coal seam."*

⚠ **The book block was the ENTIRE prompt-size swing**, and it is not a grounding gap. Only **four
races have book text at all** — human, skeleton, goblin, automaton — and for a human it was
**~1680 of ~1875 ambient tokens**, paid in full to produce four words of muttering. `race_lore`
and the denizen profiles already cover all 49 races, so the thin races were never less grounded;
they were just cheaper. `_persona(..., books=False, budget=N)` now drops books from the short
forms and keeps them in conversation, where a follower actually draws on them.

| | ambient | taunt |
|---|---|---|
| human | 1875 → **198** | 2404 → **406** |
| shopkeeper | 202 → 208 | 635 → **380** |
| demon | 189 → 191 | 520 → **334** |
| **spread across all 49 races** | 189–1875 → **175–215** | — |

Ambient stays **deliberately ungrounded** (no canon, no hard limits): a four-word mutter has no
room to make a claim worth constraining, and the measured lines never fabricated. What it lacked
was somewhere to be. Taunts keep their canon but on a budget of 6 rather than 16. 16 assertions
in `test_prompts.py`.

### Event memory
Structured records: `{type, floor, claim, importance, provenance}`. `IMPORTANCE_WEIGHT = {routine:0, notable:3, major:8, world_changing:20}` seeds friendship. Handler has an early fire-and-forget branch: any POST with an `"event"` field records and returns `{"ok":true}` with no generation.

- **`recruitment`** — once per follower, notable (+3)
- **`fought_alongside`** — repeatable, notable (+3). Edge-detection at the **top of `mymod_ambientTick`, before the inflight guard** (critical — behind the guard it never fires, since you're usually mid-conversation during fights). `inCombat = (monsterState == MONSTER_STATE_ATTACK(1) || == MONSTER_STATE_HUNT(3))`; fires on combat→calm while alive. Cooldown `45*50` ticks.
- **`new_run`** — clears `follower_state` and `HERX_STATE`

### Naming
Friendship ≥5 unlocks a nudge; follower names itself. `extract_name(raw, speech, says)` prefers a JSON `name` field, falls back to parsing speech — **the fallback is essential**, the 8B says the name but omits the field. Patterns use `(?i:...)` scoped flags and normalize typographic apostrophes (`\u2019`) — both were real bugs that silently disabled naming *and*, transitively, the Herx secret (which gates on the follower being named).
⚠ **The 8B just as often says NOTHING BUT the name, and that was dropped for the whole project.**
Both `NAME_PATTERNS` required an introducing phrase ("my name is", "I'm", "they call me"). Asked
*"what is your name"*, a terse creature answers `"Glim."` and stops — observed on slimes, rats and
skeletons alike. From one real co-op session: `Rast` ✗, `Glim` ✗, `Zhilak` ✗, `Kha'zix` ✗, while
`"My name is Kalthok"` ✓. The player caught it live — *"your name didn't change the first time but
it just changed to kalthok"* — which is exactly the diagnosis.

`BARE_NAME` now takes the whole reply as the name, **gated on `ASKED_NAME` matching what the
player actually said** — which is why `extract_name` needs `says`. Without that gate the branch
would take any short reply (`"Yes."`, `"Cheese!"`) as the character's name; `NAME_REJECTS` grew
from 5 entries to ~45 for the same reason.

⚠ **Invented names carry apostrophes and hyphens, and `[A-Z][a-zA-Z]+` truncated them.** `"I am
Zx'thal"` yielded `Zx`. The class is now `[A-Z][a-zA-Z'\-]+`. The TTS section already assumed
names like `Zx'thal` exist, so this was quietly costing both systems.

Order is unchanged and matters: JSON field → introducer patterns → bare reply. 17/17 on a table
built from real session strings, negatives included.

**Names are PRE-CACHED server-side, not invented by the model** (`follower_names.json`, 4153
names). Asked to make one up, the 8B draws from a handful — the same names kept reappearing
across separate playthroughs. So the service reserves a name when the follower is created and
the nudge names it explicitly (*"Your name is Skarn … use EXACTLY that name"*); the model only
phrases the reveal. Same division of labour as boons, the spy crack and identification, and for
the same reason: one literal choice left to the 8B comes back canned.

- **Lookup is tiered, not a union** — race pool → the race's comprehension group → `default`,
  mirroring `noise_for`. A union averaged the flavour away and produced *Perrick the goblin*;
  tiers spend the goblin names first. The group tier is what a race with **no entry of its own**
  draws from, which is the DLC case flagged below.
- **`name_history.json` is the part that fixes the actual complaint.** A name is written there
  only when it is *actually spoken in play* (`commit_name`), and reserved-but-never-revealed
  names are released, so a long session doesn't burn the pool. `reserve_name` skips anything in
  the last `NAME_HISTORY_MAX` (400) revealed names, so consecutive runs don't repeat. Gitignored
  — it is per-player state. Env: `BARONY_AI_NAMEHIST`, `BARONY_AI_NAMEHIST_MAX`.
- Constraints weaken in a fixed order when a pool runs dry: cross-run repetition is conceded
  **before** within-run repetition — two followers sharing a name in one party is worse.

⚠ **The total was never the binding constraint — the REACHABLE pool for a favoured species is.**
Because lookup is tiered, a player who mostly recruits goblins draws from goblin + humanoid +
default, not from all 4153. Simulated at the original sizes (4 named followers per run, half of
them goblins): a goblin got a **non-goblin name at run 21**, and a name **repeated at run 61** —
both far sooner than "1432 names" suggests. Measure the reachable pool, not the file.

| | before | after |
|---|---|---|
| goblin race tier | 40 | **106** |
| reachable pool for a goblin | 124 | **319** |
| goblin pool runs dry | run 21 | **run 56** |
| first repeated name | run 61 | **run 157** |
| worst gap between repeats (600-run stress) | — | **43 runs** |

`NAME_HISTORY_MAX` went 400 → **2000** for the same reason: a history shorter than the reachable
pool becomes the binding constraint itself.

⚠ **Once every name really is spent, hand back the one seen LONGEST ago.** The last-resort tier
now sorts by position in `_NAME_HISTORY`, which is oldest-first. This needed a matching fix in
`commit_name`: a name met *again* must move to the END of history, not keep its original index —
otherwise it is permanently "the oldest" and comes back every single run. Measured, that bug took
the gap between repeats down to **1 run** before the recency fix took it to 43.

⚠ **The validator was only testing the most permissive route.** `name_report` round-tripped each
name through the JSON `"name"` field alone, whose regex is `"name"\s*:\s*"([^"]{1,40})"` — and
`[^"]` accepts anything that is not a quote. Two names with Cyrillic letters in them passed while
being unmatchable by the speech patterns, which are the load-bearing ones (the 8B reliably *says*
the name and omits the field). It now round-trips all three routes: JSON field, introducer
pattern, and bare reply.

⚠ **The reserved name appearing verbatim in the reply is itself a reveal.** `extract_name`'s
patterns only catch phrasings they know; here we know the exact string to look for. That match is
**case-sensitive and gated on the nudge being live** (`friendship >= 5`) — several pool names are
ordinary words (`Ember`, `Scrap`, `Bill`), and a lowercase one in passing must not read as an
introduction.

⚠ **If the model coins its own name anyway, the model wins** — `resolve_revealed_name` takes it
and releases the reservation. A party HUD disagreeing with the speech bubble the player just read
is worse than a repeated name. `logreview` flags those as `self-chosen`; they are the cases where
the prompt lost.

**`python3 service.py --names`** prints pool coverage and round-trips every name through the real
`extract_name`. A name the extractor can't read back would be assigned, spoken, and silently never
stick — invisible for exactly one race. Names must match `[A-Z][a-zA-Z'\-]+` (**no digits**, so
`Cog-Seven` works and `Unit-7` cannot) and must miss `NAME_REJECTS` (`Nobody` was caught this way).
Verified end-to-end: 12 followers over 3 consecutive runs, 4 races, **12/12 used the reserved name,
zero repeats**; 28 unit assertions cover reservation, the reveal routes, release, persistence and
pool exhaustion.

**The HUD rename is free:** `GameUI.cpp:2401` already reads `followerStats->name` and displays it when non-empty and not `"nothing"`. So `strncpy(Stat->name, ...)` renames the party UI with no rendering change. (`getMonsterLocalizedName` in `actmonster.cpp:224` does *not* use `Stat->name` for ordinary races — dead end.)

### The Archmagisters: Orpheus and Erudyce (floor 35)

The endgame twins — `LICH_ICE` **Erudyce** and `LICH_FIRE` **Orpheus** (`stat_shared.cpp:918`,
`946`), 2000 HP each against Herx's 1250. Arch-wizard **siblings** and the Guild's former
leadership who, per the lore, *bicker*.

⚠ **That bickering is the design.** Their weakness is not anatomy like Herx's — it is each other.
Every truth is something one sibling would be humiliated to have said aloud, and each names
**which twin** it is about, so a truth about Orpheus does nothing to Erudyce. Learning *which*
is the point.

⚠ **A spy's lie here BUFFS instead of doing nothing.** Herx's false secrets report `debuff 0` and
the boss is untouched; at the endgame, walking in with a plan that is wrong should cost something.
`debuff` is **negative** for a buff, and the false "they are bound together" variant names
Orpheus *and* buffs Orpheus — act on it and you pour everything into the sibling who just got
harder.

| | Herx | the twins |
|---|---|---|
| races | skeleton, human | human, gnome, automaton, shopkeeper |
| friendship | 50 | **60** |
| fights | 4 | **6** |
| floors | up to 20 | **25+** |
| a lie does | nothing (`debuff 0`) | **buffs the boss** |

⚠ **`until` matters as much as `floor`.** Herx dies on floor 20, so a follower offering his
weakness on floor 30 is offering something worthless — and worse, it consumed the roll that
should have gone to the Archmagisters.

⚠ **The boss the player ASKED about comes first.** Measured: asking *"what do you know about the
Archmagisters?"* deep in a run offered the **Herx** secret, the model quite reasonably answered
the question instead, and the offer was dropped — so the arc could never fire at all.

⚠ **Every variant must NAME its sibling.** Left with a pronoun the model swapped them —
*"Orpheus won't act while **her** brother's alive"*, mechanically targeting Erudyce while telling
the player to leave Orpheus for last. A true secret that misdirects is worse than none. After
naming them explicitly: **2/2 correct**. There is a test that no variant can be added without a
name.

The engine field extends rather than changes: Herx keeps `"<debuff>:<uid>"` and the twins add
`"<debuff>:<uid>:twins:<who>"` (1 = Erudyce, 2 = Orpheus). `initLichIce`/`initLichFire` each apply
only their own `who`, and tier 2 still doubles either direction if the informant is alive.

The whole arc is **generalised, not duplicated**: `BOSS_SECRETS` and `SECRET_STATE` are keyed by
boss, `HERX_STATE` aliases `SECRET_STATE["herx"]` so both views stay in step, and the 24 Herx
assertions were the regression net through the refactor.

### Herx secret weakness
Gate: eligible race (skeleton/human) **and** named **and** friendship ≥50 **and** ≥4 `fought_alongside` events, then an escalating roll (+30% if the player asks about Herx directly, +25% if the follower is a spy). One reveal per run.

Four paired truth/debuff variants — the flavor always matches the mechanic. Reply carries `"secret": "<debuff>:<uid>"`. There is **no sentinel in the model's output** — `herx_detect(uid, raw, speech)` decides whether the secret was *actually told* by matching the variant's `keywords` against `speech + raw`, plus a literal `"secret"` substring check on `raw`. A pending offer that the model talks around is dropped (`HERX_STATE["pending"] = None` either way).

`initLich` applies it: **tier 1** = knowledge alone; **tier 2** = double, if the informant is alive at spawn (`uidToEntity(informant)`, `HP > 0`). Evaluated once at spawn, not continuously.

Barony's own naming calls Herx the **midpoint** (`MOVIE_MIDGAME_HERX`, `HerxMidpoint*`), which is why friendship 50 fits.

### The Herx secret: the ordering bug that suppressed the payoff

⚠ **The carve-out has to land AFTER the limits it carves out of.** The offer used to end with
*"this overrides your limits"* while sitting ~600 chars **before** the `LIMITS_HEADER` block that
says *"say you do not know and STOP"*. This project has twice measured that whichever of two
conflicting instructions lands later wins — which is exactly why the limits are emitted last — so
the override lost every time and a loyal follower talked around the payoff of the entire arc.

`_secret_override()` is now emitted separately, after `limits`, and names the routes the limits
push toward (*"saying you do not know it, saying you have only heard rumours of it, or answering
with general talk about Herx instead is WRONG"*).

| | offers converted |
|---|---|
| loyal, before | **40%** (2/5) |
| loyal, after | **81%** (9/11) |

⚠ **Why the false secret looked healthy while the true one was broken.** The false variants are
concrete nouns the model repeats anyway — *silver clasp*, *running water*, *Herxel* — while the
true ones are precisely the kind of specific claim a hard limit suppresses. So the spy branch sat
at ~80% throughout and hid the fact that the honest branch was failing.

⚠ **The spy crack does NOT need to stand down for the secret** — tested, and the intuition was
wrong. Suppressing it (as the identify path does) measured **62%** against **70%** for leaving it
on, i.e. noise, and if anything it helps: the crack primes *"let something slip"*, which is what
telling the secret is. The two are aligned, unlike the crack and an identify request. Reverted.

**`BARONY_AI_TESTHOOK=1` enables a `{"force": {...}}` payload** that writes directly into a
follower's state, so the friendship-50 / four-fights gate can be reached without playing to it.
Off unless the env var is set, and the service binds loopback only. This is the only practical
way to exercise the Herx arc; keep it.

### The fearful allegiance (8%)

⚠ **`self_interested` and `fearful` had one prompt line each and no mechanics** — 23% of recruits
with no behaviour of their own, against 7% spies with five. This is the first half of closing
that.

**The shape: a liability in a fight who is the reason you survive the floor.** Each behaviour is
a different face of the same nerves.

| | |
|---|---|
| **They break under fire** | `EFF_COWARDICE` at 50% HP *while fighting*. The game's own effect — `shouldRetreat()` honours it (`entity.cpp:25937`) so they run, and it docks STR and CON so they fight worse while panicking. 8s, then a 30s cooldown so they can rally. |
| **Frightened, not bitter** | `ALLEGIANCE_FEEL` scales fear ×1.5 and resentment ×0.5. **Increases only** — reassurance still calms them at full rate, or they could never be talked down. |
| **They will not go deeper** | Below floor 15 with trust <45 they refuse `FOLLOW` outright. |
| **⚠ But they notice things first** | Trust gate for the minotaur guard drops 40 → **20**, chance 70% → **90%**. |

That last row is the point of the whole allegiance. The coward is the one who saves you, because
they were the one who wasn't relaxed — which is what makes them worth keeping rather than
dismissing.

⚠ **The refusal names the required speech act, not the mood.** "You are frightened" produced a
follower who agreed cheerfully and *mentioned* being scared — the same failure the spy crack and
the hard limits both taught. It forbids agreeing cheerfully and *"if you insist"* by name, and
lands **after** `_obedience_section`, which is telling them to carry out orders.

Measured on floor 17, same order to each:

| | result |
|---|---|
| fearful, trust 20 | **4/4 refused**, action NONE |
| loyal control, trust 20 | **3/3 followed** |
| fearful, trust 20, reassured in the same message | **3/4 went**, reluctantly |
| fearful, trust 60 (earned) | **3/3 went** willingly |

That progression is the conversation system doing real mechanical work: fear is real, talking
them down works, and earned trust removes it entirely.

⚠ **Allegiance stays server-side; only `traits` crosses to the engine.** The engine has no
business knowing who the spy is and its log is visible, so the recruitment response carries
`"fearful"` and nothing else — sent because cowardice has to trigger on HP, which only the engine
sees.

### The mercenary (self_interested, 15%)

**Not a worse loyal follower — a different deal.** They will not give you things, because things
are worth money; but they know what things are worth, and they get you a better price.

⚠ **NO loot-grabbing** (`monsterAllyPickupItems`), tempting as it is: items are one of the main
ways the player progresses, and a follower hoovering up the floor takes that away rather than
colouring it. Their price is paid in favours and information, never in your gear.

| | |
|---|---|
| **They never gift the good item** | boons skew to **information at 85%** (60% for everyone else); the once-per-run good item is off entirely — they would sell it |
| **They know what they are holding** | identify accuracy **+20%**, capped at 95%. Outside their species' expertise this is the big one; inside it the cap bites |
| **They talk merchants down** | a mercenary escorting you doubles the haggle step, 1% → **2%**. Measured: −2/−4/−5 across three shops against −1/−2/−3 with a loyal companion |
| **Favours are bought, not earned** | see below — gold **replaces** the relationship gate rather than raising it |

**The engine sends only an `escort` uid** with the shop payload — never an allegiance. The
service already knows what that follower is; the engine has no business knowing, same rule as
the `traits` string.

⚠ **A removed instruction, and why not to re-add it.** The merchant used to be told to credit the
mercenary for the price. Measured **0/10** with a plain *"mention them"*, and only **2/10** after
naming the forbidden route the way the spy crack does — with 2/10 explicitly taking the credit
anyway. Sixty tokens in an already-crowded section for noise. The price movement *is* the
feature and lands 100% of the time; the player reads the number, not a line about it.



### The mercenary's fee — gold as a substitute for the relationship

⚠ **`MERC_FAVOUR_TAX` is gone, and its removal is the point.** It made him need you to like him
*more* than a loyal follower does (+20 friendship for Sokoban, +20 trust for the guard) — which
quietly made him **a worse loyal follower**, the one thing the allegiance is not supposed to be.
Inverted, the deal reads properly:

> A loyal follower does it because they like you. A mercenary does it because you paid.

So he will solve Sokoban at **friendship 0** if the money is right, and the loyal follower still
does it free at 20. Verified: a friendship-0 mercenary cleared the room for 450 gold, and a loyal
follower in the identical situation still acted immediately, free, with no quote.

**No new packet.** `'GOLD'` (`net.cpp:4788`) is an **absolute set** of a client's gold that vanilla
already sends after the host changes the value (`net.cpp:9426`), so a remote client resyncs for
free — unlike `MYFG`/`MYHG`, which had no vanilla equivalent. `mymod_chargeGold()` deducts, clamps
at zero, plays the coin sound and sends it.

**The coin sound is the feedback.** `242 + rand%4` — the game's own, what `DGLD` plays when you
drop gold (`net.cpp:8976`) and what a bounty pays out with. Paid **before** the job and before his
line, so the player hears the money leave and then hears him report the work.

| Service | Price | Why |
|---|---|---|
| identify | `25 + 5×floor` | the everyday repeatable |
| minotaur guard | `100 + 25×floor` | it saves your run; it should hurt |
| Sokoban | **25% of the gold in the room** (floor 100) | a *cut*, not a fee — self-balancing, and the engine counts it with `mymod_goldOnFloor()` |

Calibrated against the real economy: a floor pile is `10 + rand%100 + currentlevel`
(`maps.cpp:7775`), monsters carry ~15–50, and starting gold is **0–250 for most classes**
(Merchant 1000; `stat_shared.cpp:70` is the 0 default). ⚠ **So a paid mercenary is a midgame
unlock** — early on his value has to stay the free stuff (the identify bonus, the haggle step).

**Consent is explicit, because taking gold unasked is theft and theft is the spy's job.** Quote →
accept → charge on delivery. Nothing is billed for a job that did not happen.

⚠ **The two favours are engine-initiated, so acceptance has to be read somewhere else.**
`mymod_favourFetch` is a one-shot request/response with nobody to say yes to, so he quotes there
and is answered in ordinary conversation (`_merc_section`), which stamps `LAST_FAVOUR` and lets
`mymod_performFavour` run from the conversation path. That is why the favour logic is now split
out of `mymod_favourTick`.

⚠ **Identify is the exception: asking again IS the acceptance.** The player typed `/aiidentify`,
saw the price, typed it again. One turn each way, and no cross-turn item state — the quote turn
leaves the item unidentified, which it would have been anyway.

⚠ **He must never say the number, and the engine prints it.** Settled by the haggle measurement,
not re-litigated: asked to quote, the 8B invents a figure (*"25 gold instead of 30"*, beside a shop
window showing the real price). `mymod_printQuote` states it; `MERC_NO_NUMBERS` is in all four
blocks. Measured **0/6 named a figure**, and 5/6 clearly demanded payment.

⚠ **Charging a remote client races with their own shop.** `buyItemFromShop` runs client-side
(`interface/shopgui.cpp:1597` checks affordability against the client's own copy), so an absolute
`'GOLD'` set landing mid-purchase clobbers a transaction in flight. `mymod_canChargeNow()` holds
the fee while a shop is open.

**Two prompt findings, both the same lesson this project keeps relearning — name the required
speech act and forbid the specific route out:**

1. ⚠ **The conversation path had no idea what a finished job sounds like.** `build_guard_prompt`'s
   careful *"do NOT explain the mechanism, do NOT name a minotaur"* wording is not in the
   conversation prompt at all, and the acceptance happens there. Measured: the fee stamped 6/6
   while only ~3/6 reported the work — the rest drifted into travelogue (*"we're on the 8th level
   of the Swamp, it's a Goblin infestation"*) or invented a method (*"I set up a few traps"*).
   `MERC_JOB_DONE` carries the per-kind wording across; **4–5/6** after.
2. ⚠ **The refusal has to forbid the agreeable OPENER by name.** With only a general prohibition
   the engine refused correctly **3/3** — no charge, no job — but the *speech* agreed **2/3**
   (*"Alright then! What's the plan?"*), so the player reads a yes and then watches nothing happen.
   Forbidding `Alright / Sure / Aye / Of course / What's the plan` by name took it to **5/5**:
   *"You can't afford me, and that's the end of it."*

⚠ **`LAST_CHARGE` / `LAST_FAVOUR` / `LAST_QUOTE` are per-run latches and are cleared in
`reset_run`** — the `LAST_SABOTAGE` leak class, and this one takes the player's money.

#### He sells the map, and it took three attempts to make him say so

His best product, and the only service he **offers** rather than being asked for — so it needed
an offer path rather than the ask→quote→accept one. He pitches, you agree, he is paid, the
minimap fills in. `50 + 10×floor`.

⚠ **The sale is checked BEFORE the friendship gate in `boon_roll`.** Everything below that gate
is *generosity* — a follower doing something nice because they have come to like you — and is
rightly friendship-gated and friendship-scaled. Selling you a map is a transaction, and the whole
point of the allegiance is that a transaction does not require him to like you. Verified firing at
**friendship 0**. Curiosity ≥40 stays, because it is the **capability** gate: it is what makes him
the sort who wanders off and looks. It still consumes the floor's boon slot, so he cannot pitch
every time you speak — measured **170/200** floors produce the pitch within 15 exchanges, the
other 30 pre-empted by an ordinary boon stamping the slot first.

⚠ **The once-per-run latch is spent on the SALE, not the offer** — a declined pitch must not burn
the player's one map for the run.

**Three failures, in order, and the third is the one worth remembering:**

| Attempt | Result |
|---|---|
| offer block in the ordinary boon slot | **0/6** — ignored completely, small talk about goblins |
| moved after `limits`, before the action rules | **2/6** |
| moved to the LAST slot **and** given server-chosen content | **~6/8** |

1. ⚠ **It is a knowledge claim about a place, and `LIMITS_HEADER` forbids exactly that** — at
   length, including *"do NOT describe its traps, corridors, chambers, contents"* and *"an honest
   'I do not know' is always the correct reply."* Same ordering problem as the Herx secret, and it
   needs the same fix: the block names itself `AN EXCEPTION TO THE LIMITS ABOVE` and lands after
   them. `MERC_LATE` is that channel.
2. ⚠ **Position again: the true last slot is after the ACTION RULES**, where the spy crack sits
   (20/20). After `limits` but before them scored only 2/6.
3. ⚠ **But the real cause was that it asked the 8B to invent the substance.** Every block that
   measures well here hands over concrete content and leaves only the phrasing —
   `_secret_override` quotes the actual secret, the info boon quotes the actual fact, the spy
   crack and the sabotage tell are chosen server-side. `MAP_PITCH_ANGLES` and
   `MAP_DELIVERED_ANGLES` do the same. **Placement alone did not fix it; content did.**

⚠ **The delivery block failed the same way for the same reason.** Reusing `merc_paid_section` ran
three prohibitions (*"do NOT list directions, do NOT name rooms, do NOT describe the floor"*)
against one instruction — and the safest way to obey *"do not describe the floor"* is not to
mention the floor at all. **0/6**, with the mechanics landing perfectly every time. Rewritten
positive-led with one short prohibition: **~8/10**.

⚠ **A bad probe nearly cost a working prompt.** The first delivery regex scored the rewrite 1/6;
reading the actual replies, *"I've given you the lay of the land on this floor"*, *"You've got a
map of the floor"* and *"the lowdown on this floor"* were all deliveries it did not match. Read
the outputs before believing the number — the same lesson the sabotage-tell probe taught.

**A missed line is not a broken feature**: the fee, the `map:` boon and the minimap fill are 100%
regardless, and the engine prints *"(Skarn wants 140 gold for the layout of this floor. Say yes to
agree.)"* whether or not the pitch was spoken.

### How far a follower's eye reaches — the legendary tier is nobody's to appraise

⚠ **Expressed in the GAME'S OWN CURRENCY, not as a blocklist.** Barony gates appraisal on the
item's gold value against `PRO_APPRAISAL`, in a data-driven table (`data/appraisal_tables.json`,
read by `Appraisal_t::appraisalPossible`, `interface/identify_and_appraise.cpp:372`):

| skill (+PER×`per_stat_mult`, shipped as 1) | can identify up to |
|---|---|
| 0 / 10 / 20 / 30 | 30 / 100 / 200 / 320 gold |
| 35 | 425 |
| **40** | **1500** |
| **50** | anything |

So **1500 is the game saying "past here you need a near-master appraiser."** There are exactly
**23 items above that line**: all ten artifacts (**including the Sokoban gloves the mercenary can
now win for you**), `mask_artifact_visor`, the amulet of lifesaving (5000), the big gems
(diamond/ruby/sapphire/emerald/jacinth), rings of regeneration and levitation, the vampire
doublet, and the three strongest spellbooks (steal weapon, drain soul, dominate). Handing those
over for a few coins skips a progression the game deliberately gates behind a maxed skill.

**Nobody reaches the top tier.** A companion is not a master appraiser, and that tier is the
player's own to earn:

| | reach | why |
|---|---|---|
| ordinary follower | **300** | ≈ the skill-30 breakpoint; covers **69%** of items in the game |
| expert for that kind (`IDENT_EXPERTISE`) | **1500** | the skill-40 breakpoint — a gnome and a ring, a goblin and an axe |
| **mercenary** | **no limit** | ⚠ the one exception — he reads the legendary tier too, at **20% of its worth** |
| everyone else, ever | **ceiling 1500** | the 23 legendary items are past them |

Using the game's own numbers means **DLC and modded items are covered automatically**, with no
list to maintain. Distribution: 69% of items ≤300, 27% in 301–1500, **4.4% above**.

⚠ **The check runs BEFORE the spy branch.** A spy who cannot read the thing has nothing to lie
about either — and a confident false name for an artifact is exactly the hole this closes, from
the one follower most motivated to create it.

⚠ **`GEM_GLASS` is weighed at 1000**, mirroring `appraisalPossible` exactly. A worthless gem that
looks valuable is *supposed* to be hard to tell from a real one; skipping that would let a
follower spot the joke for free.

**"Beyond me" is a fourth outcome, not a refusal and not a guess** (`refuse | beyond | correct |
mistaken | lie`). Saying so is real information: it tells the player the thing is worth carrying
to someone who can read it. The block permits *"it looks valuable"* — that much is obvious — and
forbids the route the model reaches for by default, naming it anyway behind a hedge (*"it could
be…", "it looks like a…"*), the way `LIMITS_HEADER` names its routes. Measured on an artifact
sword: **0/8 named or hedged a name**, 7/8 pointed the player at a real appraiser.

⚠ **The mercenary is not paid for a job he cannot do.** He says it is beyond him for free —
quoting and charging for "it is beyond me" would be the worst version of this feature.

**He prices by what the thing is worth**, the Sokoban-cut precedent again:
`max(25 + 5×floor, 20% of value)`.

| item | fee |
|---|---|
| ordinary dagger | flat `25 + 5×floor` |
| 1200-gold spellbook | **240** |
| artifact (3000) | **600** |
| amulet of lifesaving (5000) | **1000** |

⚠ **At that price he takes his time** — `IDENT_LEGENDARY_ACCURACY = 0.85` floors his accuracy
above 1500. Without it a non-expert mercenary reads a 600-gold artifact at
`IDENT_LAY_ACCURACY + MERC_IDENT_BONUS` = **50%**, a coin flip on a fortune. The fee is meant to
be the cost, not a gamble on top of it. (He is never a spy — that is a different allegiance — so
there is no lying branch to worry about here.)

**This is the only route to a top-tier appraisal that does not go through a maxed
`PRO_APPRAISAL`**, which is what keeps it a trade-off rather than a hole: 600 gold at the point
in a run where artifacts appear is a real decision. `MERC_REACH_UNLIMITED` is the knob if it
ever needs putting back.

**The engine sends the value** (`mymod_appraisalValue`, mirroring `appraisalPossible`), because
only the engine knows it. For a remote client it rides in `MYID` **after the counted decoys**, so
it cannot be confused with one, and the fixed header offsets are untouched; `packtest.cpp` covers
it, including an artifact-tier 5000 value. Worst realistic packet is now 143 bytes of 512.

⚠ **The introduction had to stop saying "anything".** It promised a mercenary would *"put a name
to anything they are carrying"*, which the ceiling made false — the same truthfulness rule the
rest of that list follows.

⚠ **`identify_attempt` is probabilistic, so `== "correct"` is a flaky assertion.** Tests assert
the follower ATTEMPTED it (`correct | mistaken | lie`) rather than got it right; the first
version failed intermittently, a different line each run.

### "That looks worth something" — remarking on a valuable pickup

A follower reacts when you pick up something valuable. Value ≥ **1000** (aquamarine and up), one
remark per 20s, and it rides the conversation slot like a taunt.

⚠ **NO UPSTREAM HOOK.** `itemPickup()` is called from a dozen contexts — shop purchases, stack
splits, emptying bottles — and hooking it would put a new file in the upstream diff for a
cosmetic feature. Instead `mymod_valuablesTick()` rides the per-frame scan `mymod_watch` already
runs, watching each player's inventory for a uid that was not there last frame. Cheap, and it
catches every route into the inventory rather than the ones `itemPickup` happens to cover.

⚠ **Seeded silently on the first pass**, or a new character's starting kit reads as a pile of
treasure picked up the instant the run begins — a Merchant starts with 1000 gold of it. Cleared
and re-seeded on `new_run`.

⚠ **THEY NEVER NAME IT**, and this is the balance-critical line. Most followers cannot appraise a
legendary item at all, so naming one here would hand out free exactly what the appraisal tiers
gate — and would contradict the same follower calling it beyond them a moment later. The engine
sends the **look** (`getUnidentifiedName()` — "a gold ring", "a curved sword"), the category, and
a value band; the prompt forbids naming it, guessing its powers, or saying what it might be.
Measured across gems, artifacts, rings and amulets: **0/6 leaked a real item name**.

⚠ **An unidentified item is judged the way the APPRAISAL system judges it**, so `GEM_GLASS` is
weighed at 1000 — which means a follower gets excited about worthless glass, and the player only
finds out by having it appraised. That is Barony's own joke arriving for free. An *identified*
item uses its real value, so known glass gets no remark.

**The reaction is allegiance-flavoured** (`VALUABLE_ANGLE`), and ⚠ **a spy uses the loyal one** —
same rule as the introduction, since a spy who eyes your treasure differently is a tell the
player gets for nothing. Measured:

| | |
|---|---|
| loyal human, ruby | *"That's a pretty gemstone, we should keep an eye on it."* |
| mercenary, artifact sword | *"…that's a sword fit for a lord."* |
| **fearful** goblin, ring | *"Ugga ugga! Shiny! Worth big squabble!"* — it draws attention |
| loyal gnome, **glass** | *"worth at least three weeks' worth of good scraps!"* |

⚠ The mercenary is told **not** to tout for the appraisal in this line. He already quotes when
asked; touting here would make every valuable pickup a sales pitch.

Prompts are ~280 tokens — the short-form shape, no books, budget 6. It records no relationship
movement, consumes no chat cap and no boon slot: it is a remark, not an exchange.

### The introduction: what a follower is FOR

The follower system is deep and almost entirely invisible — a new player has no idea they can ask
for an appraisal, that a companion volunteers what it knows, or that a gnome can make a floor's
traps safe. **The mercenary states his terms because that is what a mercenary does**, and once he
is doing it the same beat works for everyone: the first thing a new follower says is what it is
willing to do for you. Fires once per follower, on the first exchange.

⚠ **EVERY LINE MUST BE TRUE OF THAT PARTICULAR FOLLOWER.** An introduction that promises something
the game then refuses reads as the system being *broken*, not as the character declining. So the
list is assembled server-side (`_service_list`) from the same gates the features actually use:

| | |
|---|---|
| a loyal follower | promises appraisal **only later** — `identify_attempt` refuses below `IDENTIFY_MIN_TRUST`/`FRIENDSHIP` |
| a mercenary | promises it **now**, because money replaces that gate, and adds haggling |
| gnome/automaton/kobold/goblin | offer traps; nobody else mentions them |
| a sentrybot | never promises to move — it offers to hold ground and shoot |
| a summon or bot | offers no knowledge of the depths; it has no history here |

⚠ **A SPY GETS THE LOYAL INTRODUCTION, BYTE FOR BYTE.** The whole disguise is that a spy looks like
an ordinary recruit, and an introduction keyed on the true allegiance would hand the player the one
thing the system exists to hide. There is a test asserting the two strings are identical. A
mercenary announcing he works for pay is *not* a leak — it is his character, and the player learns
it the moment he quotes a price anyway.

⚠ **A defect this surfaced: a PAID mercenary still refused to appraise.** `identify_attempt`'s
stranger gate ("you do not know them well enough to want to handle their belongings") ran ahead of
everything, so you could agree his fee, be charged, and be told he does not know you. Money settles
exactly that objection, the same way it replaces the relationship gate on his favours. Found by
asking what the introduction was allowed to promise.

**Two prompt findings, both already familiar:**

1. ⚠ **Position, again.** Emitted five blocks from the end — with `limits` and the action rules
   after it — it stated **2.5 of 4** services and ignored the "do not open with your life story"
   line outright. Moved to the **LAST slot** (the `MERC_LATE` channel, now the general late
   channel shared with the map pitch): **3.0+**, the slot the spy crack measures 20/20 from.
2. ⚠ **"Introduce yourself" reads to the 8B as "give a life story".** Every reply opened with
   invented biography — *"Name's Joe, shopkeeper, been runnin' this stall for years…"* — and one
   never reached the terms at all. `INTRO_NO_BACKSTORY` forbids the route by name and allows a
   line of it at the END, which is where it belongs.

⚠ **The introduction suppresses the other unprompted behaviours for that turn** (boon, map pitch).
Two mandatory behaviours in one reply and the later one takes the turn — spec §35/§36 again.

**The engine names the commands, the follower does not.** Same split as the price figures: asking
an 8B to recite `/aicommand` is non-diegetic and it would get it wrong, while the engine knows it
for certain. `mymod_printHints()` prints two lines **once per run**, after the follower's line so
it reads as a footnote rather than a banner:

> *(Talk to them with /aicommand &lt;what you want to say&gt;, or hold V to speak aloud.)*
> *(/aiidentify has a companion appraise an unidentified item. They do it for someone they trust — or for coin.)*

Measured sample, mercenary: *"What you get out of keeping me around is that I'll go where you point
me, hold ground when told, and fight when it comes to that; I'll tell you what I know about these
depths… and if you've got something strange on you, I can probably identify it."*

### Allegiance & spies
Weighted roll at recruitment: **loyal 70 / self_interested 15 / fearful 8 / spy 7**. Never shown to the player. Spies get a motive and behavioral tells: deflect on personal history, over-interest in the player's gear and plans, hesitation at Herx's name. `allegiance_section(st, says)` detects probing questions and sharpens the tell.

**Verified:** all 3 spies deflected on family; both loyal controls gave rich specific histories. One spy dodged a family question and immediately asked about the player's rations and map — a genuine, unscripted tell.

**The friendship-30 crack — FIXED, and the second big prompt finding.** A spy who has come to
like the player must visibly strain. Measured, `human` spy at f=32, asked "do you trust me?":

| Wording | Cracks |
|---|---|
| Atmospheric: *"it is eating at you, let a crack show: an unfinished sentence, an odd warning..."* | **0/8** |
| Concrete required speech act + the smooth route forbidden by name | 3/8 |
| ↑ plus the specific chosen server-side, and the block moved to LAST position | **20/20** |

Three separate lessons, and the first is the same one the hard-limits block taught:

1. **Describing a feeling does nothing; naming the required speech act works.** "Let a crack show"
   scored 0/8 — the model answered warmly and ignored it. Listing the exact forbidden replies
   (*"Of course I trust you", "I've got your back", "you've saved my skin"*) is what moved it.
2. **One literal example gets parroted verbatim.** At 3/8 the hits copied *"don't take the east
   stair on the next floor"* almost word for word — canned across a playthrough. So `spy_crack_section`
   picks the concrete detail server-side from `SPY_CRACK_WARNINGS` / `SPY_CRACK_CONFESSIONS` and the
   model only phrases it, the same division of labour boons already use. Rewordings came back varied
   (one invented "the old ventilation shaft").
3. **Position is a lever.** The block is appended LAST in `build_prompt`, after the obedience and
   action rules — at 8B whatever follows an instruction competes with it. It was previously buried
   inside `allegiance_section`, five blocks from the end.

**Watch the examples for incriminating nouns.** An early confession example read *"If you knew why I
was sent, you'd —"*; the model parroted the phrase and leaked the betrayal outright in 2/10. The model
copies the incriminating noun, not just the shape. Fixed by scrubbing "sent"/"orders"/"report" from
every example and requiring the sentence to stop *before it names anything* — breaking off and then
explaining is called out as failure. 0/8 leaked after.

Verified after the fix: warning branch 6/6, confession branch 8/8, `rat` (thinnest lore) 6/6.
Gating is unit-tested: silent for loyal/self_interested/fearful at any friendship, and for a spy at 29.

**Spies reveal FALSE weaknesses** from `HERX_FALSE_VARIANTS` (running water / silver / true name — all plausible undead lore, all wrong). Reported as `debuff 0`, so `initLich` applies nothing. Discovered through consequences, never announced — per the design doc's core rule.

### The map boon: a follower who walked the floor

A curious companion slips away, walks the whole level, and comes back having described it — the
minimap fills in completely.

⚠ **Gated on CURIOSITY**, which was otherwise the least-used dimension in the system: nothing
read it but a single behaviour line. It is also exactly the trait of someone who goes and looks.
Same move as giving trust a job with the minotaur guard. Curiosity ≥40, friendship ≥20, 25% when
eligible, and it **outranks the good item** in the boon order — a follower who has been off
exploring should not hand you bread instead.

⚠ **Stronger than the scroll it resembles**, so it is **once per run per player**. Scroll of
magic mapping reveals `16 + 8*beatitude` tiles from you (`item_usage_funcs.cpp:3906`); this
reveals the floor. `radius == 0` means every tile (`maps.cpp:11002`).

**No netcode needed.** `spell_magicMap` already sends an `MMAP` packet to a remote client itself
(`magic.cpp:70`), so a co-op client's minimap fills in with nothing added by us — the same
self-replication that made chat and speech bubbles free.

The prompt line explains where they went and forbids reciting directions: the engine has already
filled the map in, so a list of corridors would be noise on top of it.

> *"We've been over this floor, Ada. I told you about the layout and what to expect."*

### Spy sabotage: calling the minotaur

**The first thing a follower can do that actively spends the player's run against them.**
Two kinds, with **separate budgets**, because they are not the same size of event: the minotaur
can cost you the run, rigged traps cost you a health bar.

| kind | budget | what it does |
|---|---|---|
| `minotaur` | once per **run** | starts the countdown; see below |
| `traps` | once per **floor**, 3 per run | every boulder and arrow trap on the floor fires **twice** |
| `fog` | once per **floor**, 2 per run | wipes what you have explored of this floor's map |

**Clouding the map** is the mirror of the map boon, and the mildest of the three — a pure setback
rather than a danger, so it gets the smallest budget. A **cursed** scroll of magic mapping already
does exactly this: wipes `minimap[][]` and says *"Huh? What? Where am I?"*
(`item_usage_funcs.cpp:3914`, Language 869). The effect and the message are both the game's own,
and the message is ideal here — disorienting, and it does not say who did it.

⚠ **Unlike revealing, this needs a packet.** `spell_magicMap` sends `MMAP` to a remote client, but
there is no vanilla equivalent for un-mapping, and the cursed-scroll path bails out entirely for a
remote player (`if (multiplayer == SERVER && player > 0) return;`) because the client runs its own
copy. `MYFG` is the eighth mod packet.

**Rigged traps** re-fire ~2s after they go off — a second boulder out of the same hole once you
have stepped clear and stopped worrying about it. No new firing code: `skill[28]` is Barony's
power state (0 = not powerable, 1 = unpowered, 2 = powered, `mechanisms.cpp:876`), and powering a
trap directly is what `mechanisms.cpp:1293` already does. The mod clears the trap's fired flag
(`skill[0]` — a 0/1 flag for boulders, an even/odd counter for the arrow trap, whose `skill[3]`
refire cooldown is cleared too), powers it for one frame, and the trap's own act function spawns
exactly what it would have spawned. `skill[28]` is then **restored to what it was**, so the
circuit is left as found.

⚠ **Boulders and arrows only.** Magic and spear traps (`actTrap`/`actTrapPermanent`) already fire
on a repeating cycle, so "again" means nothing for them. Already-spent traps are not re-armed
either — the edge detection only fires on a trap going off *after* the rigging, so the design is
"the next trap you set off fires twice", not "a boulder rolls at nobody".

Rigging is **per floor and does not follow you down** — `mymod_rigged` is cleared on a level
change and on `new_run`.

Measured with realistic allegiance rolls (7% spy, 40% of those saboteurs):

| followers recruited | a minotaur | any rigged floor | floors rigged, avg |
|---|---|---|---|
| 1 | 2.5% | 2.8% | 0.08 |
| 3 | 7.0% | 7.5% | 0.21 |
| 5 | 12.0% | 12.9% | 0.35 |

⚠ **One tell pool for both kinds**, deliberately: the player must not be able to read which
sabotage happened from what the spy said.

Everything else is additive — gifts, information, trap disarms — and the false Herx secret only
misleads. This takes.

The engine does the work: `createMinotaurTimer()` is a plain call already used by level
generation (`maps.cpp:7200`) and the `/minotaur` cheat (`consolecommand.cpp:2544`), so starting a
countdown on a floor that was **not** a minotaur level is a supported operation, not a hack.
Arrival is `getMinotaurTimeToArrive()` — **150s**, or 210s below floor 5, on 10–14 and 25+. There
is also a 1-in-5 chance the **ZAP Brigade** (nine humans) spawns 30s before arrival
(`monster_minotaur.cpp:694`).

⚠ **Starting the timer mid-floor is silent in the engine** — the warning speech fires on level
ENTRY (`actplayer.cpp:8089`) — **so the mod fires it explicitly.** Silence was the wrong call: the
minotaur is fast, and 150 seconds of nothing followed by it rounding a corner is an unannounced
execution on a floor the player had no reason to expect one. Tension needs a clock the player can
see.

`mymod_minotaurWarningTick()` delivers vanilla's own two-stage warning — *"You hear a voice inside
your head…"* then *"My minotaur is hungry!"*, with the follow-up 8s later. It is **Herx** taunting
you, not the spy, so it is fair and legible without naming the culprit.

⚠ **Delayed 9 seconds, on purpose.** Herx gloating in the same beat as the spy's small talk hands
the player the culprit for free and throws the tell away. Nine seconds of separation stops the two
reading as cause and effect and costs **6%** of the countdown:

| floors | spy speaks | Herx warns | minotaur | warning the player gets |
|---|---|---|---|---|
| 5–9, 15–19 | 0s | 9s (+8s) | 150s | **141s** |
| 10–14 | 0s | 9s (+8s) | 210s | **201s** |

⚠ **Cancelled if the party leaves the floor inside the delay window.** Level generation resets
`minotaurlevel` and destroys the timer, so without the guard Herx would gloat about nothing on the
next floor down. `messagePlayerColor` self-replicates, so one per-player loop reaches the whole
party including remote clients.

**Gating** — every bound is about the difference between a betrayal and a mugging:

| | |
|---|---|
| allegiance | `spy` only; never a summon or bot (a conjuration has no outside to signal to) |
| floors | **5–19**. Not the opening mines, not a boss floor, not Hell, not the town |
| friendship | ≥15 — they have to be embedded first; a stranger sabotaging you is not betrayal |
| frequency | **once per run**, run-global (three spies still produce one sabotage) |
| who | only ~40% of spies are saboteurs at all — decided **once**, on first eligibility |

⚠ **The per-reply chance was nearly meaningless, and measuring said so.** An embedded spy gets
roughly thirty eligible replies across floors 5–19, so 12% per reply fires essentially always —
the measured per-run rate tracked the 7% spy rate *exactly*, i.e. every spy sabotaged. Turning the
chance down would only have moved *when* it happened, not *how often*. The real decision is now
made **once per spy** (`SABOTAGE_SPY_SHARE = 0.4`): most traitors are watchers, not saboteurs.
That is the honest knob; the per-reply chance just staggers the moment.

| followers recruited in a run | before | after |
|---|---|---|
| 1 | 7.1% | **2.6%** |
| 3 | 19.1% | **8.0%** |
| 5 | 29.7% | **12.6%** |

⚠ **The engine keeps its own latch too** (`mymod_sabotageUsed`, cleared on `new_run`). The
service's latch lives in RAM, so a host who restarts `service.py` mid-playthrough re-arms it —
true of all run state, but this is the only thing that can actively cost the player the run.
It also fails safe the other way: if a reply is lost between the roll and delivery, the sabotage
is spent without happening.

**The tell** is chosen server-side from `SABOTAGE_TELLS` — the spy-crack lesson again, since left
to invent one the 8B repeats the same line every run. Each has to be innocuous in the moment and
obvious in hindsight.

⚠ **The block spends most of its words on what NOT to do.** Everything else in the prompt tells
this character it is a traitor, so it will happily foreshadow — and a tell that telegraphs is a
warning, at which point the player simply leaves the floor. It forbids sounding nervous, ominous,
regretful or apologetic by name, and says outright *"you are not confessing and you are not
cracking — you are making small talk."*

⚠ **The spy crack stands down for a sabotage** — the opposite call from the Herx secret, and for
a reason that is worth keeping straight. Herx and the crack measured as *aligned*: both are "let
something slip". The crack and a tell pull opposite ways — the crack wants visible strain, the
tell only works if it sounds like nothing.

Measured with a **neutral** question ("what did you do before all this?"): **0/6 telegraphed**.
A first pass showed 2/4 and that was a bad probe — asking "anything worth knowing here?" invites
hazard talk, so the regex was catching the follower answering the question. Sample:
*"I'm doing all right, I guess… the air's not too bad here on this floor. We should probably get
moving soon, though."*

⚠ **`logrec`'s first positional parameter is named `kind`**, so `logrec("sabotage", kind=...)`
throws. Caught by the test, and it would have thrown the first time the feature ever fired in a
real run.

### Follower favours

The **positive counterpart to sabotage**. Before these, the best a loyal follower could do was
hand you a healing potion once a run, while a spy had four separate ways to cost you the run.
One async slot serves them all: the engine notices a situation, asks whether the nearest suitable
follower acts, and applies the answer. Independent once-per-run budgets, so spending one does not
cost you the other.

#### Solving Sokoban for you

The hidden boulder-and-pits puzzle is optional content, and a player who dislikes it should be
able to hand it to a companion. Gated on **friendship ≥20** rather than trust — this is a chore
done out of fondness, not a judgement call — at 75%.

⚠ **Nothing is spawned by us.** The artifact gloves already sit **invisible on the map**
(`actboulder.cpp:2408`); the game's own `boulderSokobanOnDestroy(false)` reveals them once no
boulders remain, and the surviving gold un-hides itself the next frame (`actgold.cpp:44`).

⚠ **Boulders are broken ONE AT A TIME**, ~0.25s apart, with the game's own crumble sound (67)
and rock particles. The first version removed them all in a single frame, which emptied the room
instantly and silently and read as a glitch rather than as a companion doing something.
Staggering also lets their line land first: they say they will deal with it, then you hear it
happen.

⚠ **Breaking, not pushing** — and the mod must NOT let the game's own boulder-destroyed path run
here. On Sokoban that path spawns a scorpion or insectoid **per boulder** (`actboulder.cpp:609`).
Removing the node ourselves and calling `boulderSokobanOnDestroy` once at the end skips it.
Making them *fall* instead would match the game's real solve (`actboulder.cpp:876`, which costs
no gold) but would drop them wherever they stand rather than into a pit, which looks worse than
breaking them — and breaking is what justifies the gold cost.

That gives the intended trade: you get the gloves and ~34 of the 40 gold bags, and you do **not**
get the perfect-solve credit, which needs ≥39. You skipped the puzzle; you do not also get the
prize for doing it flawlessly.

Measured: friendship 35 solved it **4/5**, friendship 5 refused **2/2**.

> *"Got 'em all shifted into the pits now, sorry about knockin' some extra gold loose."*
> *"Leave that mess for someone else, eh? I've got better things to do."*

#### A trusted follower heading off the minotaur

The **positive counterpart to sabotage**. Before this, the best a loyal follower could do was
hand you a healing potion once a run, while a spy had four separate ways to cost you the run.

⚠ **One mechanism covers both sources, because it watches for the TIMER rather than hooking the
sabotage.** A spy's sabotage and a naturally-generated minotaur floor create the same
`actMinotaurTimer` entity, so a trusted companion heads off a betrayal and a designed encounter
with the same code, in the same beat. Cancelling is the game's own move: the timer ends itself
with `list_RemoveNode` when it is done (`monster_minotaur.cpp:819`).

⚠ **Gated on TRUST, not friendship** — trust ≥40. Trust is otherwise read only by the disclosure
tiers and the obedience formula, and this is exactly what it means: not *"do they like you"* but
*"will they act on your behalf when it matters"*. Being adored at friendship 100 with no trust
does nothing, which is tested.

| | |
|---|---|
| who | any non-spy follower, nearest to their player, alive; never a bot (a turret notices nothing) or a summon |
| chance | **70%** — even the right companion can miss it, so the minotaur stays a threat |
| budget | **once per run**, and the engine asks at most once per floor |

**The engine owns the mechanism, the model owns only the reaction.** The prompt never names a
minotaur and forbids explaining *how* — asking an 8B to invent a method produces a set-piece.
They just sound like someone who has noticed something and dealt with it. Measured live:
a trusted companion caught it **3/4**, an unearned one **0/3**, and none of the seven replies
named a minotaur.

> trust 55: *"We've got another one quiet, at least for now."*
> trust 10: *"Storm in the walls. Not stopping nothing."*

When the guard cancels a **sabotage**, the spy's once-per-run minotaur is still spent — it tried
and was foiled — and the pending Herx warning is cleared too, so nothing gloats about a threat
that is gone.

### Killing a spy costs you nothing

⚠ **`ally_died` used to fire for the survivors whatever the deceased had been doing**, so
correctly identifying a traitor and dealing with them cost you −trust, +fear and +resentment
across the whole rest of the party. The player was punished for a correct read.

Two suppressions, both gated on `allegiance == "spy"`:

| | |
|---|---|
| `ally_died` | the survivors do not grieve, and nothing is written into their memory |
| `hurt_by_player` | striking the spy costs nothing with the spy either |

Everything else is untouched: a `loyal`, `self_interested`, `fearful`, `bound` or `machine` death
is mourned exactly as before, and hitting any non-spy still costs what it always did.

⚠ **Unconditional, deliberately — it does NOT require the spy to have been "exposed" first.**
There is no proof to be had: allegiance is never shown, and a spy who dies before cracking never
confirms anything. Requiring proof would punish a correct read for lacking evidence the game does
not provide.

**The engine has to name the deceased**, since allegiance lives service-side. `ally_died` carries
an `about` uid via `mymod_recordEventAbout()` — an internal overload rather than widening
`mymod_recordEvent`, whose signature is externed from `files.cpp` and `actmonster.cpp` and would
have dragged two upstream files along. A death with no `about` (an unknown body) is still mourned.

⚠ **`record_event` now returns whether it actually recorded**, and the handler only logs an
`event` when it did. Without that, `logreview` counted a suppressed `ally_died` as a real one and
misreported the run.

### Boons
`boon_roll(st, floor)`: friendship ≥10, one per follower per floor, `chance = min(0.35, (f-10)/200)`. Measured: none at f9, ~2% at f15, ~9% at f30, ~19% at f45, ~25% at f60.

Types, resolved in strict priority order (first match wins): **trap disarm** (gnome/automaton/kobold/goblin only, f≥30, 15%) → **one good item per run** (healing/extra healing potion or garnet, f≥40, 20%, latched globally by `BOON_STATE["good_used"]`) → **info** (60%) → **mundane item** (bread/cheese/glass gem/torch, the remainder). A successful stage-1 roll stamps `last_boon_floor` immediately, so it consumes the floor's slot whichever type comes out.

**The boon never round-trips through the model** — there is no `::BOON::` sentinel. `boon_roll` writes `"traps:"` or `"item:TYPE:N"` straight into `LAST_BOON[uid]` (via `_boon_section`), and the handler `pop`s it into the reply's `"boon"` field. The `UNPROMPTED:` prompt line is flavor only: items and trap disarms fire even if the 8B never mentions them. **`info` is the exception** — it writes no payload, so it exists *only* as whatever the model chooses to say, and silently evaporates if the instruction is ignored.

`mymod_disarmFloorTraps()` sets `actTrapSabotaged = 1` — **Barony's own sabotage flag**, checked by every trap type (arrow, boulder ×5, magic, spear). Items spawn via `newItem(...)` + `dropItemMonster(it, giver, ...)` at the follower's feet.

### Robustness
`parse_reply(raw)` — strict `json.loads` first, then a regex fallback for malformed 8B output. Tested against 9 patterns. Logs `(JSON malformed - recovered via fallback)`; observed firing in real play.

Display: status lines (thinking/listening/transcribing/executed) are `printlog` (terminal only). **Double-print fixed** — every line used to print twice (a `MESSAGE_MISC` *and* the `MESSAGE_CHAT` broadcast); now one chat line + one bubble. Each conversation slot carries its own `speaker_uid` (whose head the bubble goes over) and `prefix` (the `[taunt]`/`[overheard]` label, or the co-op `"<Player>'s <Follower>: "` attribution).

---

### Non-follower NPCs — townsfolk, merchants, named characters

Followers are one relationship; the world is full of people who are **not** following you.
Talking to them runs on separate state and a separate prompt.

**Engaging.** Clicking an NPC is the game's own affordance, so that is the verb: `handleMonsterChatter`
(the canned-line path, `actmonster.cpp:12210`) is intercepted, and the NPC becomes that player's
**conversation partner**. `/aicommand` and voice then address the partner instead of the follower,
until they die, are left >8 tiles behind, or another NPC is engaged. `mymod_partner[MAXPLAYERS]`,
so four players each hold their own conversation.

The decision tree already in `actMonster`, and where the mod attaches:

| Condition | Vanilla does | Mod does |
|---|---|---|
| `STAT_FLAG_NPC == 0` | `makeFollower()` | recruitment event (unchanged) |
| `STAT_FLAG_NPC != 0` | `handleMonsterChatter()` — canned `Language::get(2700+…)` | **AI dialogue**, falling back to canned |
| shopkeeper / trader | `startTradingServer()` | **AI greeting into the shop window** |

**`STAT_FLAG_NPC` is overloaded** — for shopkeepers it is the *store type* (stored as `store + 1`,
so never 0 for them); for everyone else it is the *dialogue type*. `mymod_npcDescribe()` reads
`monsterStoreType` for merchants and `monsterNameIsGeneric()` to spot the ~30 named NPCs that
`monster_data.json` already defines (King Arthur, Merlin, Lilith, Bram Kindly, Gharbad, Algernon…).

**Merchants speak in the shop window, not a floating bubble** — that is where the player is looking.
The shop GUI already owns a speech box: `updateShopWindow()` copies `shopspeech[player]` into
`shopGUI.chatStrFull` with a typewriter effect. Two hazards, both handled in `mymod_setShopLine` /
`mymod_holdShopLine`:
- an idle **chitchat timer overwrites `shopspeech` every ~600 ticks**, so the AI line is re-asserted
  every frame while live. Re-asserting the *same* string is free — `updateShopWindow` guards on
  `chatStrFull != buf`, so the typewriter does not restart.
- **`shopspeech` is used as a printf FORMAT STRING** (`shopgui.cpp:286`). Stray `%` in model output
  corrupts the line, so it goes through `messageSanitizePercentSign` first. Same class of bug as the
  `"%s"` guard on speech bubbles.

Clients get merchant lines via `MYSH`; the shop GUI is local to whoever has it open.

**State: light per-run memory, no friendship.** `npc_state[uid]` holds race/name/role/shop and the
last 4 exchanges. No friendship ladder, no obedience, no boons, no allegiance — that is all follower
machinery. **Unlike `follower_state`, exchanges store BOTH sides**, which is the fix for the known
contradiction bug (a rat that loved cheese and then did not eat cheese); verified — asked the same
question twice, got the same answer.

**The one rule that makes an NPC not a follower** is `NPC_STANDING`. Without it the 8B slides
straight into companion voice — offering to come along, awaiting orders, saying "master". It
forbids those routes by name, the same technique as the hard-limits and spy-crack blocks. Verified:
asked a merchant to come fight, got *"I'm a shopkeeper, not a soldier."*

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

### Haggling with merchants

A merchant who likes you shades the price your way; one you have been rude to shades it the
other. **±1% per negotiation, capped at ±5%**, and that smallness is the design, not timidity:
Barony's own trading skill already swings buy prices from **×3.00 down to ×1.00**
(`items.cpp:5990`) and charisma moves sell prices by up to +100%. Anything with real economic
weight here would just be a worse version of a system the game already has. What this buys is
the *feeling* of having negotiated, and a reason to be civil to a shopkeeper.

On a 200-gold sword: 480g at PRO_TRADING 0, and the negotiation moves it to 456 or 504. The
game's own trading skill moves the same sword from 480 to 200.

⚠ **Applied inside `Item::buyValue`/`sellValue`, not at the point of purchase.** Those are the
only two functions the shop DISPLAY and the transaction both pass through — hooking the purchase
alone is how you get a shop that quotes one number and charges another. Negative percent means
"better for the player" on both sides of the counter, so `mymod_priceModifier` **flips the sign
when selling**, or a good deal would mean opposite things buying and selling.

⚠ **At PRO_TRADING ≥ 75 the negotiation stops mattering** — `buyValue` ends
`return std::max(value, getGoldValue())`, and a maxed trader is already at that floor. Not worth
fixing; a master haggler having nothing left to haggle for is a reasonable place to land.

**The server resolves the outcome; the model only phrases it** — boons, the spy crack and
identification all work this way, and asked to judge its own haggle the 8B agrees to practically
anything. Regard moves on courtesy and rudeness (`MERCHANT_TONE`, rudeness costs twice what
courtesy earns), one attempt per merchant per floor, and the outcome follows regard: liked →
better, disliked → *worse*, neutral → a coin flip.

Three things the model had to be forbidden by name, each found by testing:

1. ⚠ **It quotes figures it cannot know.** Left alone it answered *"I can give ya the sword for
   25 gold instead of 30"* — an invented 17% cut, while the shop window beside it showed the real
   price. Every branch now forbids stating any number; the engine owns the arithmetic and the
   merchant owns only the attitude.
2. ⚠ **It repeats its own previous reply verbatim.** `_npc_memory_block` says *"stay consistent
   with this; do not contradict yourself"* and quotes what the merchant said last time — so
   repeating the sentence is the most obedient answer available. Adding a server-chosen angle
   (`HAGGLE_GRANT_ANGLES`, the `SPY_CRACK_WARNINGS` pattern) was **not enough on its own**; the
   fix needed the route named too: *"staying consistent means not contradicting the FACTS — it
   does not mean repeating the sentence."* Five identical replies became five distinct ones.
3. **A refusal gets talked round.** Each branch names the specific relenting route ("a smaller
   discount, a compromise, or 'just this once'").

⚠ **`shop` type 0 is "arms and armor" and 0 is falsy** — `int(data.get("shop", -1) or -1)` read
every weapon merchant, the commonest shop in the game, as *not a shop at all*. Pre-existing, and
it meant they were described generically in their own prompt. Found only because haggling never
fired for them.

`MYHG` is the seventh packet: clients compute shop prices themselves for display
(`monster_shopkeeper.cpp:1074` calls `buyValue` with `clientnum`), so without it a client would
see the pre-haggle price and be charged the post-haggle one. 45 assertions in `test_haggle.py`.

### Item identification (design spec §9)

`/aiidentify [n]` — ask your follower what your nth unidentified item is. **The engine stays
authoritative about what the item IS; the service decides only what the character CLAIMS**, which
is what makes the spec's three-way split real: *actual identity / what they know / whether they are
honest*. Only a claim that is **both correct and honest** sets `item->identified`, so a lie leaves
you carrying something that is not what you were told.

The claim is chosen server-side from **decoy names the engine supplies** (real item names from the
same category) — never invented by the model, same division of labour as boons and the spy crack.

Measured distribution, and observed in dialogue:

| Who | Outcome |
|---|---|
| stranger (trust <15 and friendship <20) | refuses, 100% |
| trusted **expert** for that category | 85% correct |
| trusted non-expert | ~30% correct, rest honest mistakes |
| trusted **spy** | ~52% deliberate lie |

⚠ **Two mandatory unprompted behaviours must not stack in one reply.** The spy crack is placed last
on purpose, so it *stole the turn* from identification — measured, the claim never got made and one
reply invented an entirely different object. Boon offers and the spy crack are now both suppressed
when an identify request is in flight. Spec §35/§36: scarcity, chaos without noise.

**Works in multiplayer.** A remote client owns its own inventory display and vanilla pointedly
refuses to touch a client's items server-side (`items.cpp:3867`), so the client resolves the item
from its OWN inventory and ships the description to the host (`MYID`), and the host sends the
verdict back (`MYIV`) for the client to apply. The client describes its own item, so a dishonest
client could only mislead itself.

The packet pack/parse (NUL-separated variable-length strings, the classic place for an off-by-one)
is unit-tested standalone in `src/mymod/packtest.cpp` — build it with `g++ -o packtest packtest.cpp`.
It is **not** in the build: `src/CMakeLists.txt` lists `mymod/mymod.cpp` explicitly rather than
globbing, so a second `main()` there is inert. Worst realistic packet is 141 bytes of 512.

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

⚠ **Prompt size is now ~3.5k tokens** and `logreview` warns above that. Context was never the limit
here — attention is — so this is worth watching as more sections are added.

### Follower-watch false positives (found by audit, not by play)

The detection added for fear/resentment has never run in a live game, and its failure modes are
**silent** — a follower quietly resenting you is very hard to attribute mid-playtest. Reading it
back found four real bugs:

1. **`left_behind` fired for OBEYING.** A follower told to hold position is exactly where it was
   ordered to be — and the mod's own DEFEND/WAIT action issues `ALLY_CMD_DEFEND`, so the player
   would be resented for being obeyed. Now gated on `monsterAllyState == ALLY_STATE_DEFAULT`.
2. **`healed_by_player` fired on LEVEL-UP.** Levelling raises MAXHP and restores HP, which read as
   a big heal, so the follower thanked the player for something they did not do. Guarded by
   comparing MAXHP against the previous frame.
3. **`w.maxHP` was written but never read** — it was assigned before the comparison that needed the
   old value, so the level-up guard could not have worked even if it had existed. `wounded` also now
   uses the *same* max on both sides, so a changed MAXHP cannot fake a threshold crossing.
4. **The death sweep could not tell a death from a DISMISSAL.** Dropping out of the scan only means
   "no longer anyone's follower"; a dismissed follower is still standing there. `ally_died` now
   fires only when `uidToEntity()` confirms the body is really gone from the world.

**`/aistatus`** dumps what the mod currently believes — map name as the service sees it, each
player's partner and busy flag, the follower watch with HP and adrift state, any Herx debuff. The
point is to make a playtest diagnosable instead of guesswork, since almost none of the C++ added
recently has been exercised in a real run.

**`/aicommand` with no text** turns away from an engaged NPC and hands the conversation back to
your own follower. Previously the only exits were walking 8 tiles off or clicking someone else,
neither of which is discoverable.

### Class companions — summons, charms and bots are not recruits

Conjurer summons, Mesmer charms and Machinist bots are all followers, but they arrive by
mechanisms that **create and destroy them as ordinary use of the class** — and every social
system here was built for a creature you recruited once and kept.

⚠ **Gate on ORIGIN, never on the player's class.** Any caster can learn `SPELL_SUMMON`, a charm
scroll works for anybody, and a sentrybot found on the floor can be thrown by a Barbarian.
Shaman earth-elemental summons carry a summon rank too, so origin covers them for free.
`mymod_originOf()` reads what the engine already marks:

| Origin | Marker | Where |
|---|---|---|
| `summon` | `monsterAllySummonRank != 0` (`skill[50]`, replicated) | `entity_shared.cpp:105` |
| `charmed` | `Stat->monsterIsCharmed == 1` (`MISC_FLAGS[12]`, host-side) | `stat_shared.cpp:33` |
| `bot` | `monsterIsTinkeringCreation()` — gyro/dummy/sentry/spellbot, by sprite | `actmonster.cpp:14377` |

**Conjurer persistence was already in the engine — we only had to match it.** A summon's
LVL/HP/STR..CHR and rank are packed into the *player's* `Stat`: `playerSummonLVLHP` /
`STRDEXCONINT` / `PERCHR` for slot 1 and the `playerSummon2*` set for slot 2
(`entity.cpp:4210`), restored on the next spawn (`monster_skeleton.cpp:82`). Slot is decided by
`special_npc` = `"skeleton knight"` / `"skeleton sentinel"`. So the relationship is keyed by
**`(origin, player, origin_key)`** and `get_follower_state` points the new uid at the *same*
state dict — every uid-keyed call site downstream then works unchanged. Bots key on
`(player, bot type)`: the item carries HP across pickup but `appearance` encodes HP rather than
an id, so per-type is the honest granularity. Charmed and recruited followers stay uid-keyed —
they are ordinary dungeon creatures with one body.

Survives a real death as well as a voluntary recast, deliberately: the engine keeps the stats
through any death, so the next knight is statistically the same creature and is now socially the
same one too.

**Four things this fixed, all found by reading, none of which had ever surfaced in play:**

1. ⚠ **Every resummon fired `ally_died` for the whole party.** Recasting SUMMON kills the old
   pair outright (`actmagic.cpp:14288` sets their HP to 0) and retrieving a sentrybot kills it to
   fold it back into the item (`monster_sentrybot.cpp:521`). Both really leave the world, so the
   death sweep could not tell them from a death — a Conjurer recasting their signature spell was
   inflicting −trust/+fear/+resentment on every other follower, silently, every time. The engine
   draws the same line the other way: it sets `skipObituary` for exactly these
   (`actmonster.cpp:3905`). The watch now records origin **while the follower is alive** (a corpse
   cannot be asked) and skips the mourning. A summon genuinely slain in combat is therefore not
   mourned either — the deliberate trade, and the rarer half by a wide margin.
2. **Every resummon/redeploy fired `recruitment` and rerolled allegiance** — all three paths call
   `forceFollower`, which is our hook — so a skeleton knight got a fresh 7% spy roll on every
   cast. Now the identity key finds the existing row instead.
3. ⚠ **Renaming a summon corrupted the player's summon progression.**
   `nameMatchesSpecialNPCName` compares `Stat->name` **directly** (`monster_shared.cpp:569`), and
   the naming feature `strncpy`s over it. A renamed knight then fails the check at
   `monster_skeleton.cpp:66`, falls through to `secondarySummon`, and reads *and then overwrites*
   the **sentinel's** stat slot — both summons collapse onto slot 2 and slot 1's progression is
   lost. `mymod_nameIsLoadBearing()` (any follower with a `special_npc` attribute) now keeps the
   engine's copy; the AI name still lives service-side and still shows in speech and bubbles.
4. **A skeleton knight was Herx-eligible.** `getMonsterLocalizedName` reports it as plain
   `skeleton`, which is in `HERX_ELIGIBLE_RACES`, so a creature conjured ninety seconds ago could
   hold the Baron's secret weakness.

**What no longer applies** to `summon`/`bot`: the spy roll (`roll_allegiance` returns `bound` /
`machine`, and every spy gate keys off `allegiance == "spy"`), boons (a summon owns nothing; a bot
has no pockets), and the Herx secret. `allegiance_section` and `spy_crack_section` return `""` for
unrecognised allegiances, so these are **silent by design** until the class-companion work gives
them their own voice. Charmed followers are untouched so far — they are real dungeon creatures with
a full history.

`origin` and `origin_key` ride in `mymod_payloadHead` and in the event payload; they are empty
strings for an ordinary recruit, so normal play is unchanged. 30 assertions in `test_origin.py`,
plus an end-to-end check that a recast knight keeps its name, friendship and event log across a
new uid while an ordinary recruit's path is untouched.

#### The four bots are not one thing — emplacements vs mobile helpers

| Bot | Moves? | Vanilla role |
|---|---|---|
| `sentrybot` | **no** — bolted where thrown, rotates only | fixed gun |
| `spellbot` | **no** — same | fixed caster |
| `gyrobot` | yes, flies | carries items, lights, detects; four dedicated `ALLY_CMD_GYRO_*` |
| `dummybot` | yes, walks | decoy — its whole purpose is to be shot at |

⚠ **`ALLY_CMD_FOLLOW` on a sentrybot does not make it follow.** There is no pathing branch for
these two anywhere: follow resets `monsterSentrybotLookDir` and defend sets a facing
(`actmonster.cpp:12660-12690`). They are emplacements that rotate.

⚠ **So a deployed turret resented the player for being a turret.** It sits in
`ALLY_STATE_DEFAULT`, cannot walk, and holding a corridor while you move on is *the entire point
of the class* — so it drifted past the 25-tile line and fired `left_behind` (−trust, +fear,
+resentment) every two minutes. The same failure as the DEFEND case the watch already guards,
except this one cannot even disobey. `mymod_isEmplacement()` now excludes them.

**`is_emplacement()` also forces the action to NONE and adds a prompt block**, because the action
drives a real `ALLY_CMD` and because the model will otherwise offer to come along. Written the way
`LIMITS_HEADER` and the spy crack are — naming the forbidden route, not just the fact (*"you will
NEVER follow this adventurer… offering to follow is WRONG — you physically cannot, and they can
see that you cannot"*), and placed **after** `_obedience_section`, since that block is about
carrying out orders and position is a lever. Measured 5/5 refusals with the action NONE, against a
gyrobot control that follows normally 2/2.

⚠ **Vanilla gives follower interact dialogue to `HUMAN` only** (`actmonster.cpp:13421`) — every
bot, every summon and every recruited monster is silent in the base game. There is no canned bot
voice to match or fall back to, so whatever these say is entirely ours.

#### The dummybot heckler

The dummybot's combat value **is being noticed** — monsters spot one from 96 units away
(`actmonster.cpp:6115`) where they would not see anything else. So while deployed it shouts
insults at the nearest hostile, roughly every 1.2s, and is certain it is a celebrated warrior.

⚠ **Lines come in MAGAZINES, not one generation per shout.** Rapid-fire is the whole joke and a
generation is 1–4s, so a per-line request would either stutter or eat the GPU real dialogue needs.
One call returns a batch of 12 (measured **1.7–2.1s**), the mod fires them locally at no cost, and
a refill goes out at 5 remaining — about 6s of buffer against a 2s refill, so it never runs dry.
Refills are also gated on `mymod_anyPlayerBusy()`, so conversation keeps GPU priority.

⚠ **Bubble only, no chat line.** The shared feed carries the actual conversation, and a heckler
firing every 1.2s pushes real dialogue off the screen inside a single fight — at which point it
stops being funny. `mymod_broadcastBubble` is the bubble half of `mymod_broadcastLine`, which now
calls it, so one place still knows the `"%s"` guard. Add a chat line by calling `broadcastLine`
instead, if it turns out to read better in play.

`mymod_heckleTick()` runs **before** the ambient guards, like the fight-survival scan: firing
costs nothing and must keep happening mid-conversation. It early-outs on the interval timer rather
than scanning every frame, and reservoir-picks among eligible bots so several take turns instead
of the first in map order always winning. The magazine is written for whoever the bot is yelling
at *now*; after a target change it spends a few stale lines rather than throwing them away, and
nobody can tell.

The prompt's load-bearing instruction is the one forbidding self-awareness — *"never mention being
a dummy, a decoy, or made of canvas. You do not know."* Without it the model writes wistful lines
about being a decoy, which is a different (worse) joke. Sample against a crystal golem: *"Bring it
on, Sparkle Face!"*, *"Pebble Brain, what are you waiting for?"*, *"Rocky Road will be your tomb!"*
— and against a demon, *"My sword arm is longer than yours!"*, from a thing with no arms.

`parse_heckle_lines` strips numbering, bullets and quotes (the 8B numbers lists however firmly it
is told not to), drops preambles and trailing notes, dedupes case-insensitively, and cuts at 90
chars on a word boundary so a bubble stays glanceable. 21 assertions in `test_heckle.py`.

Heckles honour comprehension like any other overheard line — a polymorphed player who cannot
understand a construct gets noises, sampled at refill (so a mid-magazine polymorph is stale until
the next one). **`dummybot` had exactly one noise**, which meant a whole magazine of identical
chimes and read as a broken feature; it now has six, drawn without immediate repeats.

**No TTS for heckles**, deliberately: `tts_emit` fires at generation time, and a magazine is
generated seconds before it is spoken, so the audio would run ahead of the bubbles. The spool's
`TTS_MAX_PENDING = 6` would prune most of a batch anyway.

#### `resummoned` — being unmade and called back

The event that makes the persistence *sayable*. Fires on rebind, for summons and bots alike.

⚠ **It COALESCES rather than appending.** This fires as ordinary use of the class — a conjurer
recasts SUMMON dozens of times in a run — and thirty separate records would drown the six-slot
memory block in the one thing the creature already knows about itself. One record whose claim
carries the running count says strictly more in a sixth of the space, and the count *is* the
memory: the first unmaking and the twentieth are not the same experience.

⚠ **It grants nothing and costs nothing** — no friendship, no resentment, no fear, though all
three are tempting. Anything it grants is farmable by recasting, and anything it costs accrues for
playing the class *correctly*. `dependence` is the one axis that is simply true (the creature
exists at this adventurer's pleasure and has now been shown it) and is also **absent from the
`compliant` formula**, so a saturated one is pure characterisation and cannot warp obedience.
Resentment stays something the player earns by actual mistreatment.

The rebind is the only place that knows it happened but has no floor to record against, so it
leaves `pending_resummon` for the first caller that does (`flush_resummon`, called from the
handler's up-front bind and from `record_event`). Wording is origin-aware — a bot is not "unmade".

Measured: after 3 recasts, asked *"how many times have you died for me?"* — **3/3 correct**, one
reply picking up the claim's own phrasing (*"You unmade me and called me back each time"*). After
10, it answered 10. 26 assertions in `test_resummon.py`, including that friendship and resentment
are still exactly zero after 31 recasts. `logreview` prints a **COMPANION CONTINUITY** section
(`summon:0:skeleton_knight: 4 bodies (3 recalls)`).

**Designed, not built — the rest of the class-companion work.** Charm as a *hidden compulsion*
state (charmed followers start resentful, tiers shut, and the charm "slips" the way a spy cracks —
reusing the best-measured prompt block in the project); and bots on the **NPC state model** rather
than the follower vector, since `npc_state` is already "light per-run memory, both sides of the
exchange, no ladder, no boons, no allegiance", which is exactly what a machine wants.

### Multiplayer (host-authoritative)

**The host is the only machine that touches Python, Ollama, or the model.** Clients install the
mod and join — no service, no config, no port-forward. Setup lands entirely on the host, by design.
The service still binds `127.0.0.1` only: clients talk to the *host's game*, never to the service.

Two findings made this cheap, and both are worth remembering before writing any new netcode here:

- **`messagePlayerColor()` and `createDialogueTooltip()` self-replicate.** Called on the host for
  a remote player, they emit the vanilla `MSGS` / `BUBL` packets themselves (`net.cpp:431`,
  `GameUI.cpp:41828`). So host→client dialogue delivery — chat lines *and* speech bubbles — needed
  no new packets at all. Vanilla's own idiom for a party-wide bubble is a plain
  `for (c < MAXPLAYERS)` loop that lets the call route itself (`entity.cpp:24463`); `mymod_broadcastLine`
  matches it.
- **`monsterAllyIndex` is `skill[42]` and is already replicated** (`serverUpdateEntitySkill(e, 42)`).
  It gives a follower's owning player on both sides. **But `forceFollower` sets it to `-1` immediately
  before the recruitment hook runs**, so `mymod_ownerOf()` falls back to `leader_uid → actPlayer→skill[2]`.
  That fallback is what covers the `/friendly` + force-recruit path — don't remove it.

So the only new netcode is **three packets** (`MYID`/`MYIV` for identify aside):

| Packet | Direction | Payload | Why it must exist |
|---|---|---|---|
| `MYAI` | client → host | `[4]`=pnum, `[5..]`=utterance | the client→host direction has no vanilla equivalent |
| `MYNM` | host → clients | `[4..7]`=uid, `[8..]`=name | vanilla fills a follower's `clientStats->name` **only at recruit time** (`LEAD`); a later rename needs its own packet or the party HUD never updates |
| `MYFR` | host → clients | `[4]`=everybodyfriendly | `/friendly` is host-local and vanilla never networks it, which silently made the whole test harness single-player-only — see below |
| `MYHG` | host → clients | `[4..7]`=shopkeeper uid, `[8]`=percent+100 | clients compute shop prices themselves, so a haggle the client does not know about shows one price and charges another |
| `MYFG` | host → client | `[4]`=pnum | wiping a minimap is client-local and has no vanilla packet, unlike revealing one |

⚠ **`/friendly` was never replicated, and it silently broke co-op recruiting for the entire
project.** Monsters behaved correctly for clients — pacified, non-hostile — because monster AI is
host-authoritative. But a **client could not recruit any of them**, while the host could recruit
freely. Found only by reading; nothing errored.

`everybodyfriendly` appears in 10 places tree-wide and none is a packet: `/friendly` sets it
(`consolecommand.cpp:1012`, and it refuses outright on a client), `checkFriend`/`checkEnemy`
early-out on it (`entity.cpp:18806`, `19434`), `menu.cpp` resets it. The client's copy is
permanently `false`.

The chain, which is worth reading once because every link looks innocent:

1. `monsterIsFriendlyForTooltip` (`player.cpp:3361`) wraps its `checkEnemy` shortcut in
   `if (multiplayer != CLIENT)` — and that shortcut is **the only branch that reads
   `everybodyfriendly`**. A client never reaches it.
2. The client falls through to the static `monsterally[monsterType][playerRace]` table, which
   knows nothing about the cheat, and reads every summoned monster as hostile.
3. No friendly classification → no interact tooltip (`player.cpp:3534`, `4617`).
4. `entityClicked` is called with `ENTITY_CLICK_USE_TOOLTIPS_ONLY` (`actplayer.cpp:9742`) and
   returns null → no `CKIR` sent (`actplayer.cpp:10361`).
5. Host-side `client_selected[n]`/`inrange[n]` never set → `monsterclicked` never becomes that
   player (`actmonster.cpp:4484`) → `makeFollower` is never called for them.

⚠ **Replicating the flag alone does NOT fix it** — step 1 means the client would receive the value
and still ignore it. `MYFR` is therefore *half* the fix; the other half is an
`everybodyfriendly || intro` early-out added to `monsterIsFriendlyForTooltip` before the
`multiplayer != CLIENT` guard, mirroring exactly what `checkFriend`/`checkEnemy` already do.

**`MYFR` is pushed by polling, not by hooking `/friendly`.** `mymod_syncFriendly()` runs from the
host branch of `mymod_pollAI` and compares the flag against the last value sent to each client,
sending only on a change. That way a client who joins *after* the toggle is synced too, and
`consolecommand.cpp` needs no edit — keeping the upstream diff at two files instead of three.
Disconnect clears the per-client sent flag so a reconnect re-syncs.

Workaround if ever building without the fix: the HUMAN row of `monsterally`
(`actmonster.cpp:101`) is 1 for HUMAN, SHOPKEEPER, AUTOMATON and the bot family, so a Human client
can recruit a **human or automaton** with `/friendly` off entirely.

**A client can join and play with NO MOD INSTALLED.** Three engine facts make this work, and
all three were verified by reading rather than assumed:

1. **The version handshake passes.** `lobbyPlayerJoinRequest` compares `VERSION`
   (`net.cpp:1531`), and the mod never touches it — `game.hpp:28` still says `"v5.0.2"`. A stock
   Steam client and a modded host agree.
2. **Unknown packets are ignored, not fatal.** `clientPacketHandlers.find()` misses print
   *"Got a mystery packet"* and carry on (`net.cpp:6832`). `MYNM`/`MYSH`/`MYFR`/`MYHG` arriving at
   a vanilla client are inert — no crash, no desync, no disconnect.
3. **It can already hear everything**, because `messagePlayerColor` and `createDialogueTooltip`
   self-replicate as vanilla `MSGS`/`BUBL` — the finding that made host→client dialogue free in
   the first place.

⚠ **The only thing it could not do was SPEAK**, since `MYAI` is client→host and vanilla cannot
send it. Fixed by reading ordinary chat: the host receives every client's chat line in
`serverPacketHandlers['MSGS']` **with the sender's player number** (`net.cpp:7685`), so
`mymod_clientChat()` treats a line starting with `@` as an utterance and routes it into the same
`mymod_requestFromPlayer` the packet path uses. The line is still relayed to the party — swallowing
it would hide half the conversation from everyone else.

**What an unmodded client gives up** — all of it graceful, none of it broken:

| Missing | Effect |
|---|---|
| `MYNM` | party HUD shows "goblin" rather than the name it chose. Cosmetic; the name is still used in speech |
| `MYSH` | merchant lines don't appear inside the shop window (they still arrive as chat) |
| `MYID`/`MYIV` | `/aiidentify` unavailable to them |
| `MYHG` | haggled prices don't apply. ⚠ NOT a display/charge mismatch: `buyItemFromShop` runs **client-side** (`shopgui.cpp:1246`) and the client prices and charges itself, then tells the host which item left the shelf. They simply pay vanilla prices |

Everything host-side is unaffected: their follower still has a full relationship, still accrues
events, still names itself, still keeps secrets.

**Per-player state.** `mymod_convo[MAXPLAYERS + 1]` — one conversation slot per player, plus
`MYMOD_WORLD_SLOT` for ambient/taunts. Everything that was a global singleton (inflight, ready,
reply, action, name, boon, follower_uid) is now per-slot, so four players never stomp each other.
`mymod.hpp`'s extern surface shrank to three globals; the rest is file-static.

**Gating.** `gameLogic()` runs on hosts *and* clients, so `mymod_pollAI()` was firing HTTP calls
to a nonexistent localhost service on every client. Now: ambient/taunts, delivery, and
`mymod_recordEvent` are all host-only. The `new_run` guard matters most — a client loading a level
would otherwise wipe the host's entire run state.

**Service is threaded.** `ThreadingTCPServer` + a single `STATE_LOCK` held around state mutation
but deliberately **not** across `ask_ollama`. Measured: a fire-and-forget event record returns in
0.00s while a 5.3s generation is in flight. Under the old single-threaded `TCPServer` it waited the
full 5.3s — with four players that compounds badly.

**Scoping decisions.** Shared feed *and* shared bubbles: everyone sees every follower's line,
prefixed `"<Player>'s <Follower>: "` (singleplayer keeps the bare line). Herx stays **run-global**
(one boss, one secret, first reveal wins — `herx_detect` now re-checks `revealed`); the
once-per-run good item is **per-player**, so joining a party doesn't quarter your follower's
generosity. `HERX_STATE["pending"]` is keyed by uid now — as a single slot, two concurrent offers
raced and one was silently dropped.

**Prompt.** Requests carry `player`, `player_name`, and `party`. At `party > 1` the prompt names the
follower's own adventurer and states the others are companions, not leaders. Verified: a goblin
answered Bram by name without being told to.

**Voice in co-op:** clients get typed text with zero setup. Push-to-talk still works on a client
*if* that player chooses to run `voice_bridge.py` (Python + faster-whisper) — transcribed text goes
out over the same `MYAI` path. Optional extra, never a requirement.

### whisper.cpp in-process: voice with no Python at all

`-DADORCISM_WHISPER=ON -DWHISPER_ROOT=<checkout>` builds whisper.cpp into the binary, so a co-op
client needs **nothing but the mod and a model file** — no Python, no pip, no CUDA. OFF by default:
it adds real compile time and the mod is fully usable without it (the clip falls through to
`voice_bridge.py`, and falls through at runtime too if no model is on disk).

**Measured on this machine, CPU only:**

| | base.en (142 MB) | tiny.en (75 MB) |
|---|---|---|
| model load | 0.05s (mmap) | 0.04s |
| per utterance | **~0.78s**, flat regardless of clip length | ~0.32s |
| *"wait here and guard this door"* | ✅ | ✗ "got this door" |
| *"ask the gnome about Baphomet and the kobold camp"* | ✅ "Aphomet" | ✗ "Nomemout, Afomet, Cobbled Camp" |

base.en is the default. 0.78s is imperceptible after releasing a key, and the accuracy gap on this
game's vocabulary is large. Both numbers are against **espeak-ng synthetic speech**, which is much
harder than a human voice — real speech should do better than this on both.

⚠ **The vocabulary prompt is doing real work.** Whisper has no prior for these proper nouns.
Measured on the same clip, `initial_prompt` turned *"Barrenburg is waiting in the mines below
Hamlet"* into *"Baron Herx is waiting in the Mines below Hamlet."* Keep the list to names the model
would otherwise mangle — it is prepended as decode context, so length costs time.

`use_gpu = false` deliberately: the GPU is holding the 8B at ~6 of 8 GB, and this must never
compete with generation. Transcription runs on a detached thread (0.78s is eleven frames) and the
finished text is collected by `mymod_pollPTT` on the main thread. `mymod_onTranscribed()` is now
the one place that decides what a finished utterance does, whichever transcriber produced it.

Model discovery: `ADORCISM_WHISPER_MODEL` → beside the binary → `models/` → cwd, preferring
base.en over tiny.en when both exist. Tested in `wavtest.cpp` (32 assertions now), including that
an **empty** env var is ignored rather than taken literally.

Binary grows 14.2 → 16.3 MB; the static libs are 3.7 MB total. The model is **not** committed and
**not** shipped in the zip — `dist/` setup offers to download it, which keeps the mod download
small and lets a slow machine choose tiny.en.

### Push-to-talk: capture moved into the mod

The microphone is recorded by the **mod** now (SDL, in C++), not by a Python helper. That
matters most for a co-op client: their machine needs the mod and a transcriber, not
`sounddevice`, `numpy` and PortAudio as well.

⚠ **Audio must never cross the wire, and this is not a preference.** `NET_PACKET_SIZE` is
**512 bytes** (`game.hpp:38`), so three seconds of speech is ~200 UDP packets. A private TCP
socket to the host is *worse*: with a **Steam lobby there is no port at all** — Steam relays
everything — so a raw socket would not reach the host in exactly the mode a non-technical friend
would use. Whoever speaks transcribes on their own machine; only text travels, over the `MYAI`
path that already exists.

⚠ **Barony never initialises SDL audio.** `init_flags` is `VIDEO | EVENTS | JOYSTICK |
GAMECONTROLLER | HAPTIC` (`game.cpp:7288`) — sound goes through FMOD/OpenAL. The mod brings the
subsystem up **lazily on first use** (`SDL_InitSubSystem`), so a player who never holds the key
pays nothing, and a machine with no microphone complains once rather than every frame.

Capture opens with `SDL_AUDIO_ALLOW_FREQUENCY_CHANGE` — a device that only does 44100 is common,
and resampling one short clip is cheaper than having no voice at all. Guards: 15s cap so a stuck
key cannot eat memory, 300ms floor so brushing the key is not an utterance, and **write-then-
rename** on the clip so the transcriber never reads a half-written file.

**`mymod_transcribeClip()` is one seam, deliberately.** Today it hands the WAV to
`voice_bridge.py`; embedding whisper.cpp means replacing that function body and nothing else —
capture, key handling and the delivery path above and below it all stay put.

`voice_bridge.py` is correspondingly simpler: it watches for the clip instead of recording, so
it no longer needs `sounddevice` or `numpy`. It also **auto-detects CUDA and falls back to CPU
int8**, which the old hardcoded `device="cuda"` did not — a co-op client is not required to own
a spare GPU.

**Tests.** `g++ -o wavtest wavtest.cpp $(sdl2-config --cflags --libs) && ./wavtest` — 24
assertions on WAV framing and the resampler, both invisible failures (a bad header transcribes
as silence, a bad resample as gibberish), with **SDL itself as the reference decoder**. The pure
functions live in `mymod_voice.hpp` so the test exercises the real ones. Verified end to end by
synthesising speech with espeak-ng, dropping it in as a clip, and reading back
*"Stay behind me and watch the left corridor."*; a silent clip correctly produced nothing.

### Text-to-speech (optional, OFF by default)

Voices for followers, townsfolk, merchants and monsters. **Off unless explicitly turned on**, and
structured so that "off" genuinely costs nothing — this dev machine runs without it.

**Two halves, and both must be on.** Same split as push-to-talk, for the same reason: the game must
never wait on audio.

| Half | What it does | If missing |
|---|---|---|
| `BARONY_AI_TTS=1` on the service | appends one small JSON file per spoken line to a spool dir | `tts_emit` returns on a boolean; the spool dir is never even created |
| `python3 tts_bridge.py` | polls the spool, synthesises, plays | lines spool and are pruned; game unaffected |

The service **never synthesises or plays anything** — it writes `{text, race, uid, kind, name,
player, ts}` and forgets. Write-then-`os.replace` so the bridge can't read a half-written file.
Everything in `tts_emit` is wrapped in a bare `except: pass`: audio must never break the game.

⚠ **A late line is worse than no line.** Two independent drops, because a synth queue that just
grows will replay a conversation you left three rooms ago:
- service side, `TTS_MAX_PENDING = 6` — a new emit prunes the **oldest** pending files
- bridge side, `MAX_AGE = 20s` — anything older is discarded unspoken

The bridge also `os.remove`s a line **before** speaking it, so a crash mid-playback can't put the
same line on an infinite loop.

#### Quality tier: piper + VCTK

`python3 tts_bridge.py --setup` builds `.venv-tts/`, installs `piper-tts`, and downloads **one**
model. Both are gitignored — the model is 77 MB.

**`en_GB-vctk-medium` is the whole trick: 109 distinct real human voices in a single file**,
selectable by `-s <speaker_id>`. Casting a dungeon full of NPCs therefore costs one 73 MB download
rather than dozens of models, and the accents suit Barony. `en_US-libritts_r-medium` has *904*
speakers if more is ever wanted; `--setup --full` adds three single-speaker extras.

**Measured: RTF 0.034 on CPU** — 121 seconds of audio synthesised in 4.1s, model load 0.9s paid
once at startup. Deliberately CPU, never `--cuda`: the GPU is already holding the 8B at ~6.1 of
8 GB, and this is the feature that must not compete with generation.

⚠ **Piper's speaker ids carry no usable voice metadata** — the VCTK `speaker-info.txt` URL is dead,
and the model config is just `p239 → 0`. So voice character is **measured, not looked up**:
`--profile` synthesises one sentence with all 109 speakers and estimates each one's median F0 by
autocorrelation, then prints a `BANDS` block to paste back in. The bands are that measurement:

| band | measured | cast as |
|---|---|---|
| `deep` (17) | 80–98 Hz | trolls, liches, demons, skeletons, dwarves |
| `low` (27) | 100–130 Hz | goblins, vampires, slimes |
| `mid` (7) | 143–168 Hz | humans, shopkeepers, constructs |
| `high` (40) | 172–204 Hz | humans, succubi, shadows |
| `shrill` (18) | 206–251 Hz | gnomes, kobolds, imps, rats |

`mid` is thin because the corpus clusters male 100–130 and female 172–204; `human` therefore draws
from `low`+`mid`+`high` so a town sounds like a town.

**Character still comes from the sox chain, but a gentler one.** Each race carries two effect
chains: `fx` for piper and `efx` for espeak. Over-processing an already-natural voice is exactly
what makes cheap TTS sound cheap, so the piper chains are consistently milder — troll `pitch -260`
against espeak's `-320`, rat `380` against `420`. The construct races are the one place a robotic
result is *correct*, and they get a bandpass on top.

**Voice choice is deterministic in uid** via `hashlib.md5` — **not** Python's `hash()`, which is
salted per process and would recast every creature on each bridge restart. Length-scale and
espeak speed/pitch get a small per-uid jitter so two goblins cast from the same speaker still differ.

**Named characters get a FIXED speaker id** (`NAMED`) — Merlin is always spk93, Herx spk76 under
heavy reverb. Note this is the `npc_name` path only, so a *follower* who names itself "Merlin" does
not inherit the voice — the same scoping rule the lore lookup uses.

#### Fallback tier: espeak-ng

Kept, and still tuned, so the bridge works on a machine with nothing installed. It sounds like a
1987 answering machine, which is why the bridge prints a one-line nudge toward `--setup` when it
lands there. Voice character comes from espeak *variants*
(`/usr/share/espeak-ng-data/voices/!v` — `croak`, `whisper`, `Demonic`, `robosoft*`, `Tweaky`,
`grandpa`) plus the `efx` chain. Without sox at all, fx are skipped and voices go flat but still work.

⚠ **`.venv-tts/bin/python` is a SYMLINK to the system interpreter.** The bridge re-execs itself into
the venv so the documented `python3 tts_bridge.py` just works, and the loop guard originally compared
`realpath(sys.executable)` — which *matches*, so the hand-off silently never happened and it ran on
robotic espeak with no indication why. Compare **`sys.prefix`** against the venv dir instead. The
re-exec also passes `-u`, or a redirected bridge log looks empty.

⚠ **All 43 casts are validated by rendering**, both tiers: every speaker id, length-scale and sox
chain is actually synthesised and pushed through sox. A typo in one chain would otherwise fail
silently for exactly one race — and only the race nobody tested.

⚠ **`_norm()` must match the service's `_lore_key()` normalisation.** The game hands back display
names with spaces (`crystal golem`, `revenant skull`); a mismatch is a *silent* fallthrough to the
default voice, which is how the lore-lookup bug hid for so long.

**Text is cleaned before synthesis**, since model output is written to be read: strip
`*`/markdown, normalise typographic quotes and em-dashes, drop bare `...`, truncate at 320 chars on
a sentence boundary. Truncation matters — the queue is serial, so one rambling reply otherwise
holds the audio for a minute.

**All three speech paths are voiced**: followers/taunts/ambient, non-follower NPCs and merchants,
and the untranslated `*Squeak!*` comprehension noises.

**Testing without launching the game:**
```bash
python3 tts_bridge.py --setup                         # one time, ~80 MB
python3 tts_bridge.py --list                          # the casting table
python3 tts_bridge.py --say "Stay behind me." --race troll
python3 tts_bridge.py --say "Well met." --name Merlin
python3 tts_bridge.py --demo                          # one line in every race's voice
python3 tts_bridge.py --profile                       # re-measure the VCTK bands
```

**Not done:** no in-game toggle — there is no `/aitts` console command, so switching it on means
restarting the service with the env var. **No C++ changed at all, so this needs no rebuild.** Piper
mispronounces invented names (a follower who calls itself "Zx'thal") and there is no lexicon pass.
In co-op only the machine running the bridge hears anything; a client could run its own bridge, but
the spool is written host-side, so that needs a relay first.

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

**Known gaps:**
- ~~The false secret has never been observed firing~~ **OBSERVED, and it works.** Forced via the
  test hook: a spy is offered a false variant, tells it, and reports `0:<uid>` — and
  `monster_lich.cpp:99` guards on `debuff > 0`, so nothing is applied. A loyal follower reports
  its real debuff. It had simply never been rolled (7% allegiance), not broken.
- ~~Chat history stores only the player's side~~ **FIXED.** `record_follower_interaction` now
  stores both halves and `_follower_memory_block` renders them under the NPC path's exact
  wording — *"stay consistent with this; do not contradict yourself"*, which is the version that
  measured working there. Verified: a human follower asked about their past four different ways
  held "lived in Hamlet / worked in the mines as a laborer" every time. Bounded at 6 kept / 3
  shown / 140 chars of reply so a long conversation cannot inflate the prompt, and old
  string-format entries from a run already in flight are skipped rather than crashed on.
  16 assertions in `test_memory.py`.
- ~~Grounding is only in the main `build_prompt`~~ / ~~prompt sizes vary wildly by race~~ **BOTH
  FIXED, and they were the same problem.** See *The short forms* below.

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
