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
- Service: `~/barony-ai/service.py` (port 5001), plus `barony_lore_full.json` (449KB, 45 sections), `barony_lore.json` (**only** source of `world.setting` — its wording differs from the full file's, so they are not interchangeable), `race_lore.json`, `race_books.json`, `comprehension.json`, `voice_bridge.py`
- Data files are resolved relative to `service.py`, so the repo can live anywhere
- Dev binary: `~/.local/share/Steam/steamapps/common/Barony/barony-modded`
- IPC via `/tmp/mymod_*.json` (Linux-specific; needs a portability pass before release)

## Upstream hooks (verify these after any upstream merge)

| File | Hook | Notes |
|---|---|---|
| `game.cpp` | `mymod_pollAI()`, `mymod_ambientTick()` | per-frame, in the game loop |
| `actmonster.cpp` | `mymod_recordEvent("recruitment", ...)` ×2 | `forceFollower` (~11186, guard `leader.behavior == &actPlayer`) and interact-recruit (~2170). **The interact path is the one `/friendly` and normal recruitment actually use.** |
| `files.cpp` | `new_run` event in `physfsLoadMapFile` at `levelToLoad <= 1`; plus a `__attribute__((weak))` `mymod_recordEvent` stub | The weak stub exists because the **editor** target compiles `files.cpp` without the mod and would otherwise fail to link |
| `monster_lich.cpp` | Herx debuff block after `my->setHardcoreStats(*myStats)` | inside the `!MONSTER_INIT` guard, so it can't double-apply |
| `consolecommand.cpp` | `/aicommand`, `/aiserver`, `/aitest` + `#include "../mymod/mymod.hpp"` | commands legitimately belong here |
| `net.cpp` | `{'MYAI', ...}` in `serverPacketHandlers`; `{'MYNM', ...}` and `{'MYSH', ...}` in `clientPacketHandlers` + `#include "mymod/mymod.hpp"` | the three mod packets. The tables are file-`static`, so registration cannot happen from `mymod.cpp` |
| `actmonster.cpp` | `mymod_npcEngage()` at the top of `handleMonsterChatter` (~12216) | clicking a talking NPC. **Falls through to the vanilla canned line when it returns false**, so an NPC is never mute if the service is down |
| `shops.cpp` | `mymod_npcEngage()` at the end of `startTradingServer` | merchant greets on shop open. At the END of the function on purpose — the local-player and remote-client branches both flow through it |

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
python3 ~/barony-ai/voice_bridge.py
```

Service config is env-overridable, so a released build needs no file edits:
`BARONY_AI_OLLAMA`, `BARONY_AI_MODEL`, `BARONY_AI_PORT`, `BARONY_AI_BOOKS` (the books dir still
defaults to the hardcoded Steam path — that default is the remaining portability blocker).

In-game test harness: `/enablecheats` → `/summonall` → `/friendly`, then interact-recruit.

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
Async via detached `std::thread` + shared globals + `mymod_pollAI()` per frame (no freeze). Two-way command loop: `/aicommand` finds the player's follower via `Stat->leader_uid`, service returns `{speech, action}` ∈ FOLLOW/DEFEND/WAIT/ATTACK/NONE. **ATTACK is diegetic only** — Barony's combat AI handles fighting. Push-to-talk voice (hold V → faster-whisper small.en, cuda/float16). Speech bubbles via `createDialogueTooltip(uid, DIALOGUE_NPC, "%s", reply)` — **the `"%s"` guard is required**. Polymorph-as-comprehension. 34 canonical books injected per race. `/aiserver <url>` for BYO-model.

### Relationships
`follower_state[uid] = {friendship, events, event_log, name, race, allegiance, motive, last_boon_floor}` in service RAM, within-run. Friendship hidden from the player. Chat capped per floor; **deeds are meant to drive the climb to 100**. Obedience scales with friendship (f≤4 refuse risky with action NONE; f≤9 grumble; f≥10 obey).

### Lore retrieval
`build_lore_context(race_l, floor, budget=16)` walks: entity profile → `canon_facts` (4) → `safe_inferences` (2) → race worldview + axes → location `canon` (2) + `high_value_local_knowledge` + `local_population`. Constraints: `knowledge_boundary`, `restricted_knowledge`, location `npc_rules` (2). `floor_to_region`: 1-4 mines, 5-8 swamp, 9-13 sand_labyrinth, 14-18 ruins, 19-24 underworld, 25+ hell.

**⚠ THE MOST IMPORTANT PROMPT FINDING:** constraints worded as "do not claim otherwise" failed **5/5** — the model hedged and answered anyway ("whispers say silver..."). Naming the *rhetorical route* took it to **0/5**. The working wording:

> HARD LIMITS ON WHAT YOU KNOW. You genuinely do not know these things: / If asked about any of them, say plainly and in character that you do not know, and STOP. / Do NOT guess, speculate, theorize, or pass on rumors about them. Hedged answers are FORBIDDEN: / 'some say...', 'whispers speak of...', 'perhaps it is...', 'I have heard...' followed by an answer / counts as claiming and is wrong. An honest 'I do not know' is always the correct reply.

**Generalize this:** at 8B, forbidding a *conclusion* without forbidding the *evasion pattern* gets routed around. Name the specific pattern. Verified to generalize — 9-10/10 across 5 races, 3 regions, and topics not in the limits block at all.

### Event memory
Structured records: `{type, floor, claim, importance, provenance}`. `IMPORTANCE_WEIGHT = {routine:0, notable:3, major:8, world_changing:20}` seeds friendship. Handler has an early fire-and-forget branch: any POST with an `"event"` field records and returns `{"ok":true}` with no generation.

- **`recruitment`** — once per follower, notable (+3)
- **`fought_alongside`** — repeatable, notable (+3). Edge-detection at the **top of `mymod_ambientTick`, before the inflight guard** (critical — behind the guard it never fires, since you're usually mid-conversation during fights). `inCombat = (monsterState == MONSTER_STATE_ATTACK(1) || == MONSTER_STATE_HUNT(3))`; fires on combat→calm while alive. Cooldown `45*50` ticks.
- **`new_run`** — clears `follower_state` and `HERX_STATE`

### Naming
Friendship ≥5 unlocks a nudge; follower names itself. `extract_name(raw, speech)` prefers a JSON `name` field, falls back to parsing speech — **the fallback is essential**, the 8B says the name but omits the field. Patterns use `(?i:...)` scoped flags and normalize typographic apostrophes (`\u2019`) — both were real bugs that silently disabled naming *and*, transitively, the Herx secret (which gates on the follower being named).

**The HUD rename is free:** `GameUI.cpp:2401` already reads `followerStats->name` and displays it when non-empty and not `"nothing"`. So `strncpy(Stat->name, ...)` renames the party UI with no rendering change. (`getMonsterLocalizedName` in `actmonster.cpp:224` does *not* use `Stat->name` for ordinary races — dead end.)

### Herx secret weakness
Gate: eligible race (skeleton/human) **and** named **and** friendship ≥50 **and** ≥4 `fought_alongside` events, then an escalating roll (+30% if the player asks about Herx directly, +25% if the follower is a spy). One reveal per run.

Four paired truth/debuff variants — the flavor always matches the mechanic. Reply carries `"secret": "<debuff>:<uid>"`. There is **no sentinel in the model's output** — `herx_detect(uid, raw, speech)` decides whether the secret was *actually told* by matching the variant's `keywords` against `speech + raw`, plus a literal `"secret"` substring check on `raw`. A pending offer that the model talks around is dropped (`HERX_STATE["pending"] = None` either way).

`initLich` applies it: **tier 1** = knowledge alone; **tier 2** = double, if the informant is alive at spawn (`uidToEntity(informant)`, `HP > 0`). Evaluated once at spawn, not continuously.

Barony's own naming calls Herx the **midpoint** (`MOVIE_MIDGAME_HERX`, `HerxMidpoint*`), which is why friendship 50 fits.

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

**The town is level 25 and gates behind Baphomet, not the Baron.** The lore file's own hamlet canon
agrees: *"reached after the Baron/Baphomet sequence."*

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

So the only new netcode is **two packets**:

| Packet | Direction | Payload | Why it must exist |
|---|---|---|---|
| `MYAI` | client → host | `[4]`=pnum, `[5..]`=utterance | the client→host direction has no vanilla equivalent |
| `MYNM` | host → clients | `[4..7]`=uid, `[8..]`=name | vanilla fills a follower's `clientStats->name` **only at recruit time** (`LEAD`); a later rename needs its own packet or the party HUD never updates |

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

## Decisions on record

- **TTS: no**, for now. Text-first protects reply quality and flexibility (any length/style, no mispronounced generated names, no extra VRAM atop 8B + Whisper, fits the text-native shared feed and the BYO/publishing design). Parked as a possible later optional toggle — if voice ever comes, it adapts to the text, not vice versa.
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
- The **false secret has never been observed firing** — all test rolls landed non-spy. It's the one branch where a bug would be invisible.
- **Chat history stores only the player's side**, so followers contradict themselves across turns (observed: a rat claimed to love cheese, then to not eat cheese). Storing both sides would fix it.
- Grounding is only in the main `build_prompt` and `build_taunt_prompt`; ambient babble has none.
- Prompt sizes vary wildly by race (652 rat → 3100 human) because book-lore injection is uneven. Thin races are where fabrication would reappear first.

**Designed, not built — the peaceful Herx route.** `actWinningPortal` already contains the answer: the portal **exists on the boss floor, invisible**, and reveals itself when its per-tick scan finds no `LICH` or `DEVIL` alive. So a spared-Herx ending needs only (a) a flag excluding a pacified Herx from that scan and (b) clearing his hostility. No new entity, no replaced code path. The chain that *earns* it — merchants, testimony, leverage — is the real project and is undesigned. Design the failure mode first: a half-fired peaceful path could strand the player with a non-hostile boss and no exit.

**DLC:** *Deserters & Disciples* shipped Jan 29 2026 with *Instruments of Destruction Part 1* (which removed Magic/Casting/Swimming and revamped magic into Sorcery/Thaumaturgy/Mysticism). **Part 2 is the upcoming one.** New races will need entries in `race_lore.json`, `comprehension.json`, and the lore file's denizen profiles or they fall through to generic defaults. Untested: whether a `STEAMWORKS_ENABLED=OFF` build can access DLC content at all.

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
