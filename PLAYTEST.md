# First playtest — what to watch for

The mod has never run in a real game. Everything below is verified service-side by curl and
by reading Barony's source; **none of it has been seen in play.** This is the list of things
that are most likely to be wrong, roughly in order of how much they matter.

---

## Before you start

```bash
python3 ~/Projects/barony-ai/service.py                 # terminal 1 — leave it visible, it prints a lot
cd ~/.local/share/Steam/steamapps/common/Barony && ./barony-modded
```

The binary is already built and copied. The service must be running **before** you load a save.

**In game, the two commands that make this diagnosable:**

| | |
|---|---|
| `/ailog <what went wrong>` | drops a note into the timeline. **Use it constantly.** `logreview` prints these first, and one line of "the bubble never appeared here" is worth more than an hour of guessing afterwards |
| `/aistatus` | dumps what the mod currently believes: partner, busy flags, follower watch, Herx debuff |

**Or just say it.** Hold **V** and start with **note / log / bug / todo / mark** and the rest goes
into the timeline instead of to your follower:

> *"note the bubble never appeared here"* → `NOTE (p0): (spoken) the bubble never appeared here`

⚠ It has to be the first word, so ordinary speech is safe — *"nothing here is worth taking"*,
*"logging off soon"* and *"bugbears are ahead"* all still reach your follower (13 assertions in
`src/mymod/notetest.cpp`). **Every** transcript is logged either way, so if whisper mishears you
the review shows what it actually heard next to the reply it produced — which is how you tell a
transcription failure from a model failure.

Afterwards: `python3 ~/Projects/barony-ai/logreview.py`

---

## 1. ⚠ The big one: does a follower survive a staircase?

**The suspicion.** `follower_state` is keyed by entity uid. On a level change Barony *re-creates*
every follower as a brand-new entity (`game.cpp:2438` → `summonMonster`) with a fresh uid
(`entity_shared.cpp:490`). If that's right, then at **every staircase** a follower arrives as a
stranger: friendship 0, a **fresh allegiance roll**, an empty event log, and a new reserved name.

I found this by reading, not by running it. It is the single most important thing to confirm.

**How to test it, in about three minutes:**

1. Recruit any follower on floor 1 (`/enablecheats`, `/summonall`, `/friendly`, then interact).
2. Talk to it until it **names itself** — that needs friendship 5, so roughly 15 exchanges.
   Watch for the name appearing in the party HUD.
3. Note the name. Take **one staircase down**.
4. Talk to it again and ask something it already answered.

**What you should see if identity holds:** it remembers, keeps the name, keeps its manner.

**What you'll see if I'm right:** the HUD still shows the old name (that half lives on the
engine's `Stat` and does survive), but the follower treats you as a stranger — and may
introduce itself all over again, since the introduction fires on first contact.

`logreview` now detects this directly. Look for:

```
FOLLOWER IDENTITY  2 state row(s) created this run
   !! UID CHURN CONFIRMED -- the same named creature got a NEW state row:
      Gristletail (rat): uids 41, 118  on floors 3, 4
          allegiance re-rolled each time: loyal, spy
```

⚠ **An unnamed follower cannot be tracked this way** — the detector matches on the engine-side
name, which is the only identity that crosses a floor. So the naming step above is required.

**If it's confirmed, stop worrying about the rest of the deep systems for this run.** Friendship
gates (boons at 10, the map boon at 20, the Herx secret at 50) will all be unreachable, and that
explains anything that "never fires".

---

## 2. Chatter volume — the thing most likely to need tuning

31 kinds of remark went in today and the pacing numbers are guesses. Followers now comment on
biome, chests, food, valuables, kit, kills, hunger, drunkenness, swimming, levitation, boulders,
walls, fountains, bridges, wealth, nakedness, arrow traps, mimics, bosses, minotaurs, deaths,
new areas and their own opinion of your skills.

**My honest guess: it will feel too chatty before it feels too quiet.**

`logreview` prints:

```
CHATTER  47 remarks in 22.0 min = 2.1/min
   by tier: reactive 6  event 19  idle 22
   by kind: biome 4  chest 7  food 5  kill 6 ...
   closest together: 2.0s, 5.1s, 6.3s
```

**Rule of thumb: over ~3/min is too much.** The knob is `MYMOD_TIER_GAP` in `mymod.cpp` — three
numbers now, not one:

```cpp
static const Uint32 MYMOD_TIER_GAP[3] = { 50 * 2, 50 * 15, 50 * 60 };
//                                        reactive   event      idle
```

Raise **idle** first (it's pure flavour), then **event**. ⚠ Don't raise **reactive** — that's
boulders, mimics, arrow traps, lava, death: things where a late line is nonsense.

Also worth noting *which kind* annoys you. It's cheap to demote one kind to `IDLE` or drop its
percentage without touching the rest.

---

## 3. Multiplayer — never met a second machine

Your friend needs **nothing**: no Python, no service, no port-forward. Just the modded binary,
and it works even completely unmodded (they lose name updates in the HUD, shop-window lines, and
`/aiidentify`; they can still talk by prefixing chat with `@`).

Watch for, in rough order of likelihood:

- **Does a client's speech reach the host at all?** Client types `/aicommand ...`, or `@hello`
  in chat. If nothing happens, that's the `MYAI` path.
- **Do bubbles render for a player nowhere near the speaker?** `createDialogueTooltip` is keyed
  by uid; if the entity isn't loaded on that client the bubble may just not draw.
- **Recruitment attribution for a client's follower** — verified by reading, never by play.
  Does the client's follower actually belong to *them*?
- **Four people talking at once.** Ollama serialises on one GPU, so replies queue. See whether
  that feels broken or just slow.
- ⚠ **`/friendly` needs the host to set it** — it's replicated by `MYFR`, which was a bug fixed
  only by reading. Confirm a *client* can recruit after the host runs it.

---

## 4. Things that have never fired in a real game

| feature | how to reach it | what "working" looks like |
|---|---|---|
| **The introduction** | recruit anyone, speak once | they say what they're for, then two hint lines print |
| **Boons** | friendship 10+ | an item at their feet, or a piece of info |
| **The mercenary** | 15% of recruits | quotes a price, engine prints the figure, coin sound on paying |
| **Identify** | `/aiidentify` | refuses if you're strangers; "beyond me" on artifacts |
| **A spy** | 7% of recruits | deflects on personal history, over-interest in your gear |
| **Sabotage** | a spy, floors 5–19 | a minotaur, rigged traps, or your map wiped |
| **The rat** | rat follower next to Herx | 50%: he panics and dies |

⚠ **Most of these are gated on friendship**, so if §1 is confirmed you may see none of them.
That is worth knowing rather than chasing.

---

## 5. Specific things I'm unsure about

- **The bridge hunger effect.** You said the transition levels dampen hunger. `MFLAG_DISABLEHUNGER`
  genuinely gates hunger in the engine, but I could not confirm offline that those maps set it —
  my `.lmp` header parse came back misaligned. The follower only mentions it **if the flag is
  actually set**, so if you never hear that line on a bridge, the maps don't set it.
- **Arrow-trap and boulder attribution.** Both are inferred from "your HP dropped within a few
  ticks of one being near you", because hooking the damage path would mean touching `entity.cpp`.
  A coincidental HP drop could misattribute. Watch for a follower reacting to a boulder that
  didn't hit you.
- **The coin sound.** Paying a mercenary should play the game's own coin sound *before* he
  reports the work. If the ordering feels wrong, that's a one-line fix.
- **Speech bubble length.** Some replies run 300+ characters. Check they don't overflow.
- **Herx's portal.** Killing him via the rat does **not** spawn an exit in the expanded campaign —
  `actWinningPortal` gates that on `SV_FLAG_CLASSIC`. That's vanilla behaviour, not a bug.

---

## 6. What to write down

Anything you `/ailog` gets pulled to the top of the review, so bias toward over-using it. Beyond
that, the four notes worth making by hand:

1. **Did a follower remember you across a staircase?** (§1 — the answer shapes everything else.)
2. **How often did they speak, and did it get annoying?** (a number and a feeling both help)
3. **Any line that was flatly wrong about the game** — naming an item they shouldn't know,
   claiming to have warned you, talking about the wrong floor, or calling a monster-race player
   a human.
4. **Anything that fired twice**, or two followers speaking over each other.
