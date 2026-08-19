# Barony AI-NPC Mod — Project Context

**Purpose of this doc:** paste or upload this at the start of a new chat to restore full project context without replaying the conversation history.

---

## What this project is

An original AI-NPC mod for **Barony** (open-source C++ roguelike, v5.0.2), built from source on **Nobara Linux** (Fedora-based, KDE/Wayland). Barony was chosen over a Skyrim/CHIM setup because it uses almost no VRAM (freeing the GPU for a local LLM) and its follower command-wheel is a pre-built action API.

**Architecture:** thin C++ engine hooks in Barony ↔ a local Python HTTP service that owns lore/prompts/state and calls a local 8B Ollama model. Everything local, no cloud.

**Hardware:** i7-9700K, RTX 2070 Super (8GB VRAM), 32GB RAM.

**Release intent:** publish to Nexus Mods. Co-op friends are non-technical and on Windows (dev env is Linux), so the design converges on: thin mod + configurable backend endpoint + companion setup guide, host-authoritative ("one capable host runs the stack, thin clients connect"). User brings Ollama; the Python service ships with the mod.

---

## Key paths

| What | Where |
|---|---|
| Barony source | `~/Barony/` |
| Build dir | `~/Barony/build/` |
| Main modded file (all `mymod_*` code, ~7300 lines) | `~/Barony/src/interface/consolecommand.cpp` |
| Cross-file hooks (recruitment) | `~/Barony/src/actmonster.cpp` |
| Party HUD name render | `~/Barony/src/ui/GameUI.cpp` (~line 2401) |
| `Stat::name` declaration | `~/Barony/src/stat.hpp:321` — `char name[128]` |
| `MONSTER_STATE_*` enum | `~/Barony/src/monster.hpp:~975` |
| Dev binary | `~/.local/share/Steam/steamapps/common/Barony/barony-modded` |
| Python service dir | `~/barony-ai/` |

**Service dir contents:** `service.py` (main, port 5001), `voice_bridge.py` (signal-driven STT), `barony_lore.json` (old paraphrased lore, still loaded as `LORE`), `barony_lore_full.json` (449KB / 45-section researched lore, loaded as `FULL`), `race_lore.json`, `race_books.json`, `comprehension.json`.

**IPC files (Linux-specific — flagged for Windows portability pass):** `/tmp/mymod_ptt.signal`, `/tmp/mymod_voice_text.txt`, `/tmp/mymod_ai.json`, `/tmp/mymod_amb.json`, `/tmp/mymod_payload.json`, `/tmp/mymod_event.json`, `/tmp/mymod_server.cfg`.

---

## Build & run recipe

**Deps:**
```bash
sudo dnf install git cmake gcc-c++ make SDL2-devel SDL2_image-devel SDL2_net-devel \
  SDL2_ttf-devel physfs-devel rapidjson-devel
```

**Configure (once):**
```bash
cmake .. -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DOPENAL_ENABLED=OFF -DFMOD_ENABLED=OFF \
  -DSTEAMWORKS_ENABLED=OFF -DEOS_ENABLED=OFF
```

**Iteration loop:**
```bash
# edit ~/Barony/src/...
cd ~/Barony/build && make -j$(nproc) 2>&1 | tail -10
cp ~/Barony/build/barony ~/.local/share/Steam/steamapps/common/Barony/barony-modded
cd ~/.local/share/Steam/steamapps/common/Barony && ./barony-modded 2>&1 | grep -E 'MYMOD|AI'
```

**Run the service + voice (separate terminals):**
```bash
python3 ~/barony-ai/service.py
python3 ~/barony-ai/voice_bridge.py
```

**Voice deps:** `pip install --user faster-whisper sounddevice numpy` + `sudo dnf install portaudio portaudio-devel`

**Test harness in-game:** `/enablecheats` → `/summonall` → `/friendly`, then interact-recruit a follower.

---

## Completed features (all verified in-game)

### Foundation
- **Async architecture:** detached `std::thread` + shared globals (`mymod_ai_mutex` / `mymod_ai_reply` / `mymod_ai_ready` / `mymod_ai_inflight`) + `mymod_pollAI()` called every frame. No game freeze. This is the template every slow call reuses.
- **Two-way command loop:** `/aicommand` finds the player's follower (`Stat->leader_uid == player uid`), service returns `{speech, action}` where action ∈ FOLLOW/DEFEND/WAIT/ATTACK/NONE. ATTACK is **diegetic only** — Barony's own combat AI handles fighting (programmatic target-attack needs cursor-aim, deliberately out of scope).
- **Lore:** 34 canonical in-game book `.txt` files injected per-race, cached.
- **Ambient babble + combat taunts** via `mymod_ambientTick()` each frame.
- **Polymorph-as-comprehension:** you only understand a creature's real dialogue if your current form shares its comprehension group; otherwise noises. Changes live on polymorph.
- **Voice input (push-to-talk):** hold V → `mymod_pollPTT()` writes signal → `voice_bridge.py` records mic + transcribes with faster-whisper (small.en, cuda/float16, `initial_prompt` vocab hint, `beam_size=5`) → writes text file → game reads it and calls the shared `mymod_sendToFollower(says)` path.
- **Speech bubbles:** `players[clientnum]->worldUI.worldTooltipDialogue.createDialogueTooltip(uid, DIALOGUE_NPC, "%s", reply)` — **the `"%s"` guard is required** (variadic/printf; AI text can contain stray `%`). Bubbles are distance-based (vanilla behavior, despawn out of range — not a bug).
- **Configurable backend:** global `mymod_ai_server` + `/aiserver <url>` console command, persisted to `/tmp/mymod_server.cfg`, loaded at startup. This is the BYO-model linchpin.

### Social layer
- **Relationship persistence:** `follower_state[uid] = {friendship, events, event_log, name, race}` in service RAM (within-run). `friendship_descriptor` thresholds at 1/4/9. Friendship is **hidden from the player**, watched via `[SERVICE-DBG]` log lines.
- **Friendship pacing:** chat is per-floor-capped (`CHAT_CAP_PER_FLOOR=2`, +1 per 3 exchanges, ceiling 100). **Design goal: friendship 100 is a major playthrough milestone** — chat alone contributes ~60 max across a ~30-floor run, so the bulk of the climb must come from deeds/events.
- **Earned obedience:** `obedience_section` scales with friendship — f≤4 owe nothing, refuse risky (action NONE); f≤9 carry out reasonable, may grumble; f≥10 obey even risky. Hard rule: *if you REFUSE, action MUST be NONE.*

### Lore integration (static slice)
`barony_lore_full.json` is a **knowledge architecture**, not just lore: 115-node relationship graph + edge audit ledger, event-memory model with provenance types, context-resolution source scoring + assertion thresholds, per-entity `knowledge_boundary` guards, location `high_value` vs `restricted` knowledge, 75 denizen profiles, 26 class profiles. It specifies its **own retrieval budget** ("3-8 facts", priority order, fallback chain).

Built a deterministic retrieval layer (Path A — static lookup, no embeddings):
- `floor_to_region(floor)`: 1-4 mines, 5-8 swamp, 9-13 sand_labyrinth, 14-18 ruins, 19-24 underworld, 25+ hell
- `build_lore_context(race, floor, budget=8)` walks entity profile → race worldview + axes → knowledge_boundary → location high_value + restricted, returning `(facts, constraints)`
- `build_prompt` injects **CANONICAL GROUNDING** + **HARD LIMITS ON WHAT YOU KNOW** blocks

**Verified:** a floor-1 skeleton asked for "Baron Herx's secret weakness" replied *"I do not know his secret weakness."* / NONE — it declined to hallucinate a boss secret it has no acquisition path to. That's the whole thesis of the audited file working live.

Only ~6 of 45 sections are wired. Grounding lives only in the main `/aicommand` `build_prompt` (there are 3 CHARACTER GUIDANCE sites; ambient/taunt don't get grounding — fine, those lines are short).

### Event memory
Structured first-class records (design B): `st["event_log"] = [{type, floor, claim, importance, provenance}]`, `IMPORTANCE_WEIGHT = {routine:0, notable:3, major:8, world_changing:20}` seeds friendship, `events_for_prompt()` ranks by importance then recency into a **WHAT YOU REMEMBER** prompt block.

The handler has an **early fire-and-forget branch**: any POST with an `"event"` field records it and returns `{"ok":true}` with no dialogue generation.

C++ side: `mymod_recordEvent(etype, uid, raceEnum, floor)` — defined in `consolecommand.cpp` (global scope), writes `/tmp/mymod_event.json` + detached curl. `actmonster.cpp` calls it via `extern`, passing race as a **raw int enum**.

> **Lesson:** pass primitives across the cross-file boundary and resolve names where the function links. `getMonsterLocalizedName` links in `consolecommand.cpp` but not `actmonster.cpp` — passing the enum dodges the linker error.

**Event types implemented:**

1. **`recruitment`** (once per follower, `notable` +3) — hooked at *both* recruitment paths: `forceFollower` (~line 11186, guarded `if leader.behavior == &actPlayer`) and the interact-recruit path (~line 2170). **The interact path is the one `/friendly` and normal recruitment actually use** — `forceFollower` alone did not fire. Service-side dedup makes double-firing safe.

2. **`fought_alongside`** (repeatable, `notable` +3) — the first repeatable deed. Edge-detection on follower combat state in a scan at the **top of `mymod_ambientTick`, before the `mymod_ai_inflight` guard** (critical: when the scan sat after the guard it never fired, because you're usually mid-conversation during fights). Logic: for each living follower with `leader_uid == player`, `inCombat = (monsterState == MONSTER_STATE_ATTACK(1) || monsterState == MONSTER_STATE_HUNT(3))`; on the combat→calm transition while alive, fire the event. Per-follower cooldown `MYMOD_FIGHT_COOLDOWN = 45 * 50` ticks. Confirmed followers use HUNT(3) in combat.

**Verified:** skeleton went recruited(3) → survived a fight → friendship 7 → *"A truth, perhaps. I recall fighting by your side."*

### Follower naming (marquee feature)
Followers reveal a name at **friendship ≥5**, asked or volunteered naturally, and that name **replaces the generic race label in the party HUD** ("Skeleton" → "Kalthok"), keeping HP/level.

**Service:** `follower_state[uid]["name"]` (default `""`). `build_prompt` has a `name_section` — if named, "YOUR NAME IS X, speak as them"; elif friendship ≥5, nudge to share a name fitting its kind and put **only** the name in a JSON `name` field. `extract_name(raw, speech)` prefers the JSON field but **falls back to parsing the speech** (patterns: "they call me / call me / my name is / i am / i'm / name's / i go by / known as X"; `_clean` rejects titles and non-names, length 2-40). The fallback is necessary — the 8B reliably *says* the name but often omits the field. First name wins and sticks. The name is returned in **every** reply so C++ can re-assert it.

**C++ chain:** global `mymod_ai_name` → the `/aicommand` worker's inline python also prints `"::NAME::" + d.get("name","")` → the worker splits `::NAME::` off the action tail (raw output is `reply\n::ACTION::ACTION\n::NAME::NAME`) and stores it under the mutex → the poll does `strncpy(follower->getStats()->name, mymod_ai_name, 127)` + null-terminate when it differs.

**The HUD rename is free:** `GameUI.cpp:2401` already reads `followerStats->name` and displays it when non-empty and not `"nothing"` (guard at line 2408), else the race name. So setting `Stat->name` renames the party UI with no rendering change. (`getMonsterLocalizedName` in `actmonster.cpp:224` does *not* use `Stat->name` for ordinary races — that path was a dead end.)

**Verified:** skeleton 163 warmed to friendship 8, said *"My name is... Kalthok."*, and the party corner showed **Kalthok**. Emergent detail: at friendship 4 (below threshold) the same skeleton said *"Name... forgotten. I am but a vessel for the darkness"* — the gate expressed itself in character, unscripted.

### JSON parse hardening
`parse_reply(raw)` tries strict `json.loads` first (correct case unchanged), then a regex fallback recovering speech + action from mildly malformed 8B output (missing comma, unquoted action, preamble junk, reversed fields), logging `[SERVICE] (JSON malformed - recovered via fallback)`. Tested against 9 malformation patterns including the real bug `{"speech": "As you will," "action": "FOLLOW"}`. The action whitelist still normalizes anything bad to NONE.

### Display cleanup
- Status-clutter lines (thinking / listening / transcribing / `-> executed` / attack-ack) converted from `messagePlayer(MESSAGE_MISC)` to `printlog` — terminal only, off the game screen. AI dialogue, the "you said" echo, genuine "no follower nearby" feedback, and `/aiserver` output stay visible.
- Taunts and ambient babble now also bubble over the **speaker's** head: global `mymod_ambient_speaker_uid` set in the taunt worker (tauntTarget uid) and babble worker (calmPick uid); the poll uses `bubbleUID = (mymod_ai_follower_uid != 0) ? follower : ambient_speaker`, cleared after.
- **Double-print fixed:** every AI line was printing twice — a `messagePlayer(MESSAGE_MISC)` *and* the `MESSAGE_CHAT` broadcast loop. Both MESSAGE_MISC lines dropped; the ambient branch sets `mymod_chat_prefix = mymod_ambient_label + " "` (follower branch clears it) and the broadcast prints `"%s%s"` prefix+reply. Each utterance now appears once in the blue scrollable feed plus a bubble. To restore an `"[AI] "` tag on follower lines, set `mymod_chat_prefix = "[AI] "` in the follower branch.
- Follower ambient babble already worked (the babble picker sets `relation = isFollower ? "follower" : "hostile"`).

---

## Editing discipline (hard-won — please follow)

`service.py` and the large `consolecommand.cpp` drift from repeated string-match patches. Several silent match-failures cost many rounds. Rules that work:

1. **Verify by the most specific unique token** — a function *call* with its name (e.g. `mymod_recordEvent("fought_alongside"`), never a bare word that could also appear in a comment. A generic substring check once passed against an unrelated comment while the actual code was never inserted, masking ~8 rounds of failure.
2. **Use an atomic write-then-reread-and-print-boolean in one Python command.** Separate `grep` checks against a scrolling terminal gave false negatives repeatedly.
3. **Assert anchor uniqueness before writing** (`assert s.count(anchor) == 1`). There are four copies of `Entity* pl = players[clientnum]->entity;` in `consolecommand.cpp` — pl-anchored inserts are ambiguous.
4. **Watch tab-vs-space indentation** — the file mixes them; reconstructed multi-line anchors usually fail.
5. **Run one command block at a time** — concatenated pastes tangle results.
6. Prefer full-function replacement over scattered patches; verify each site of a multi-site edit individually.
7. Never confirm success from anything but actually pasted output.

Other recurring build lessons:
- New C++ feature includes, globals, and functions go at **global scope at the top of the file**.
- Namespace-vs-global linker traps: a call inside `ConsoleCommands` binds to `ConsoleCommands::foo` unless there's a global forward declaration at file top plus a `::`-qualified call.
- JSON payloads containing spoken apostrophes must be written to a temp file and curl'd with `--data @file` (shell single-quotes break otherwise).

---

## Decisions on record

- **TTS: no**, for now. Text-first protects the sandbox's quality and flexibility (any reply length/style, no mispronounced generated names, no extra VRAM atop 8B + Whisper, fits the text-native shared-feed and BYO/publishing design). Parked as a possible later optional toggle — if voice ever comes, it adapts to the text, not vice versa.
- **Obedience is earned, not gated in C++** — prompt-driven, so it's probabilistic and characterful. A hard C++ friendship-gate is a possible later layer.
- **ATTACK stays diegetic** — target-based attack needs cursor-aim, parked.
- **Event log is structured records (design B)**, not flat strings — so new event types slot in with just a type + importance.

---

## Next steps (rough priority)

1. **More event types** — `gift` is the obvious next: an *active* lever the player controls, so it should be higher weight (`major` +8 vs combat's +3). Then possibly `healed`, `betrayed`. All reuse the existing pipe: C++ detection hook → fire-and-forget POST → `record_event`.
2. **Tune** the `fought_alongside` +3 weight and 45s cooldown across a longer run.
3. **Deeper lore slices** — event-memory provenance/propagation, the relationship knowledge graph (NPCs knowing each other), rumor propagation, world-state. Each is its own slice needing runtime state.
4. **Ambient babble could also volunteer names** — needs the babble path to send the follower uid (check whether it does).
5. **Stage-2 networked co-op** — host broadcasts dialogue packets. A whole chapter; needs host-authoritative decisions: who runs the AI, whose mic, where state lives.
6. **Windows/portability pass for release** — `/tmp/*` and hardcoded `~/barony-ai/` paths → Barony's cross-platform data dir. Write the Nexus setup guide.
7. **Per-race voice tags** in `race_lore.json` (being done in spare moments).

---

## Working style notes

Prove each piece in isolation before wiring it: curl-test the service before touching the game; keep known-good fallbacks; read the game's own source rather than guessing at APIs. Build service-side first (testable via curl), then the C++ hook. No unsolicited break suggestions or project timeline estimates.
