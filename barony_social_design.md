# Barony AI-NPC Social System — Design Specification

**Status:** Design / research baseline  
**Version:** 0.1  
**Date:** 2026-08-12  
**Relationship to lore:** Companion system to `barony_lore.json`  
**Implementation scope:** Design only; no engine/API implementation specified here

---

## 1. Purpose

This document defines the **social gameplay layer** for the Barony AI-NPC project.

The lore database answers:

> **What is true about Barony, and what could this character plausibly know?**

This document answers:

> **What can a character do with that knowledge, how can relationships change behavior, and how can social interaction change gameplay and endings?**

The design is intended to make AI NPCs consequential without turning Barony into a conventional dialogue-heavy RPG. The target experience remains recognizably Barony: chaotic, dangerous, strange, darkly funny, mechanically grounded, and capable of surprising the player.

The system should reward players for paying attention to people rather than optimizing around a visible affection meter.

---

## 2. Design Goals

### 2.1 Primary goals

1. **Make followers worth protecting.**
   Followers should provide reasons to remain alive and remain in the party beyond disposable combat assistance.

2. **Make relationships discoverable rather than explicit.**
   Likes, dislikes, loyalties, fears, and hidden agendas should emerge through conversation and behavior rather than an exposed checklist.

3. **Make knowledge a gameplay resource.**
   Shopkeepers, followers, guild members, survivors, monsters, and other specialists should possess different kinds of useful information.

4. **Make social behavior mechanically consequential.**
   Trust can produce tangible benefits. Betrayal can create tangible dangers.

5. **Preserve chaos.**
   The system should permit spies, theft, misinformation, infighting, unexpected loyalties, and reversals without becoming arbitrary.

6. **Preserve canon.**
   Generated motivations may fill open space, but they must not overwrite established Barony history.

7. **Support alternate victory.**
   The eventual system should allow some runs to resolve major conflicts without conventional boss combat, through leverage, negotiation, bribery, faction alignment, betrayal, or other socially earned routes.

8. **Make the world remember.**
   NPC reactions should depend on what the player actually did, not merely on the current conversation.

---

## 3. Scope Boundary

### 3.1 Lore layer

`barony_lore.json` contains:

- canonical history
- books and source notes
- race/culture profiles
- class/role context
- locations
- factions
- named NPC identities
- relationship/knowledge graph
- source provenance
- world-state facts
- event memories and knowledge propagation rules

### 3.2 Social layer

`barony_social_design.md` defines:

- relationship mechanics
- follower behavior
- hidden preferences
- information economy
- social rewards
- social risks
- espionage/betrayal
- party dynamics
- social boss leverage
- alternate endings
- design constraints and balancing principles

### 3.3 Engine layer

The engine implementation is deliberately outside this document.

The engine is ultimately responsible for enforcing authoritative state such as:

- inventory and gold
- actual item identification
- follower membership
- deaths
- boss health/defense state
- spawned events
- portal creation
- world-state transitions
- save persistence

The AI/social layer should **request or recommend** these consequences; it must never be the authoritative source of truth.

---

# 4. Core Social Model

## 4.1 Relationships are multidimensional

A single 0–100 friendship score is useful internally, but it should **not** be the complete relationship model and should not be exposed directly to the player.

The proposed internal relationship vector is:

- **Friendship** — general affection and personal warmth.
- **Trust** — belief that the player is honest and will not betray the NPC.
- **Respect** — admiration for competence, courage, status, discipline, or character.
- **Fear** — expectation that the player could harm or abandon the NPC.
- **Resentment** — accumulated grievance.
- **Dependence** — how much the NPC relies on the player for safety, food, status, freedom, or purpose.
- **Curiosity** — desire to understand the player.

The player should generally **not** be able to see these values numerically.

The player's perception should instead be communicated through:

- dialogue
- tone
- unsolicited comments
- voluntary gifts
- refusal to cooperate
- requests for help
- protective behavior
- gossip
- jealousy
- withdrawal
- betrayal
- changes in willingness to disclose information

### Design principle

> **The player should learn the relationship by living through it.**

A follower can like the player while distrusting them, respect them while resenting them, or fear them while still depending on them.

This is intentional.

---

# 5. Hidden Preferences

## 5.1 Preferences should not be obvious

Followers should have preferences that are partly latent.

Examples:

- values generosity
- hates waste
- values loyalty to family
- dislikes needless violence
- admires courage
- fears magic
- distrusts rich people
- loves rare food
- dislikes another race
- values competence more than kindness
- secretly enjoys being praised
- dislikes being questioned about the past
- wants independence
- wants recognition
- resents being treated as expendable

A preference should usually become apparent through repeated interactions rather than through an explicit trait screen.

## 5.2 Preferences should sometimes conflict

Characters should not be reducible to one moral axis.

Examples:

- A coward who deeply values loyalty.
- A greedy follower who gives food to children.
- A proud warrior who hates unnecessary killing.
- A compassionate healer who resents being taken for granted.
- A spy who genuinely likes the player.
- A follower who loves the player but refuses to forgive one particular betrayal.

## 5.3 Preference discovery

The player can discover preferences through:

1. repeated conversation
2. observed reactions to events
3. comments about other NPCs
4. voluntary confessions
5. gifts
6. arguments
7. requests
8. memories recalled from prior events
9. interactions between followers

The AI should not directly reveal hidden preference labels unless a character deliberately explains themselves.

---

# 6. Follower Lifecycle

A follower should evolve through a recognizable social lifecycle.

### Stage 0 — Stranger

The NPC has little reason to care about the player.

They may cooperate because of circumstance, recruitment mechanics, fear, or mutual interest.

### Stage 1 — Acquaintance

The follower recognizes the player and remembers basic interactions.

Possible behavior:

- comments on player actions
- begins revealing minor preferences
- remembers favors and insults
- develops an opinion of other followers

### Stage 2 — Companion

The follower begins showing voluntary initiative.

Possible behavior:

- warns the player about hazards
- comments on routes
- shares minor information
- suggests actions
- notices unusual items
- occasionally provides small supplies

### Stage 3 — Trusted Companion

The follower becomes meaningfully invested.

Possible behavior:

- finds or gives useful items
- offers personal information
- reveals faction knowledge
- advocates for the player in social situations
- protects the player beyond simple combat obedience

### Stage 4 — Deep Bond

The NPC's personal history can materially affect the run.

Possible behavior:

- reveals dangerous secrets
- provides boss-related information
- identifies unusual objects
- makes sacrifices
- intervenes in conflicts
- creates new opportunities
- challenges the player about major decisions

### Stage 5 — Irreversible Relationship

The relationship becomes one of the major remembered arcs of the run.

At this level, the NPC may:

- become a lifelong ally
- become a bitter enemy
- sacrifice themselves
- betray the party
- demand a major choice
- affect an ending

Not every follower should naturally reach this stage.

---

# 7. Followers as Gameplay Actors

Followers should occasionally do things **without being prompted**.

The goal is to make them feel like companions rather than commandable weapons.

Potential unsolicited behaviors:

- point out a useful item
- notice a trap or hazard
- suggest a safer route
- find food
- find gold
- identify an object
- remember a previous encounter
- warn about a named NPC
- criticize another follower
- ask for something
- offer something
- investigate an unusual room
- leave the party
- attempt a private conversation
- lie about what happened

The frequency should be low enough that initiative remains special.

---

# 8. Social Rewards

## 8.1 Small rewards

At developing/trusted relationships, followers may occasionally provide:

- gold
- food
- mundane equipment
- potions
- useful consumables
- information

These gifts should come from plausible circumstances rather than appearing from nowhere.

Examples:

> “Found this in a dead adventurer's pack. Thought you'd want it.”

> “I kept the last ration for you.”

> “I sold something I didn't need. Here.”

## 8.2 Information rewards

Information should generally be more valuable than random loot.

Possible revelations:

- local danger
- route information
- NPC identity
- faction allegiance
- hidden relationship
- monster weakness
- unusual item properties
- boss behavior
- historical context
- alternative ways through an area

## 8.3 Specialist rewards

Certain followers can provide specialized assistance based on class/race/background:

- merchant → appraisal/trade context
- wizard → magical interpretation
- healer → medical/curse context
- rogue → trap/ambush knowledge
- hunter → tracking/monster information
- mechanist → construct/item interpretation
- cleric → undead/holy knowledge
- brewer → potion/brewing interpretation

The lore layer determines whether the NPC plausibly knows something; this system determines **when they choose to tell the player**.

---

# 9. Item Identification as a Social Reward

Some followers may be able to identify objects for the player.

This should be relationship- and expertise-dependent.

A knowledgeable follower might say:

> “Give me that. I've seen one before.”

A less trustworthy follower might identify it incorrectly or refuse.

A spy could deliberately provide a false identification.

The engine remains authoritative about the actual item state.

The AI only controls the **character's claim** about what the item is.

This creates a distinction between:

- actual item identity
- character knowledge
- character honesty

That distinction is essential for believable deception.

---

# 10. Secrets and Boss Knowledge

## 10.1 Personal history can unlock extraordinary information

Some followers should have unusual histories.

A small minority may have had direct relationships with major Barony figures.

Examples:

- former servant
- former guard
- merchant contact
- prisoner
- resistance member
- Guild apprentice
- former associate
- victim
- collaborator
- relative of a victim
- witness

These backgrounds should be rare.

## 10.2 Boss weaknesses

A sufficiently trusted follower with a plausible relationship may reveal a **boss weakness**.

The social system should represent the revelation as a grounded fact:

```text
knowledge:
  target: Baron Herx
  type: weakness
  source: firsthand experience
  confidence: high
  disclosed_by: follower
```

The engine may later translate this into an actual gameplay modifier.

Possible effects include:

- armor reduction
- resistance reduction
- temporary vulnerability
- reduced regeneration
- disabled summon behavior
- altered boss phase
- altered dialogue
- alternative interaction opportunity

The specific numerical effect is an engine-design question, not a lore-system rule.

## 10.3 Existing mechanics should be reused

Where possible, social revelations should piggyback on existing Barony mechanics instead of inventing parallel systems.

For example, Herx already has a relationship to Mystic Orbs and the game's post-boss progression uses magical sphere state. A future social weakness system should preferably feed into an existing style of boss modifier/state rather than creating an unrelated “AI bonus” subsystem.

---

# 11. Betrayal and Espionage

## 11.1 Betrayal is a character property, not a random event

Followers may have hidden allegiance structures such as:

- loyal
- self-interested
- fearful
- coerced
- opportunistic
- double agent
- secret spy
- fanatic
- resentful
- unstable
- genuinely conflicted

These are not player-visible labels.

## 11.2 Spies need motives

A spy should have a reason for betraying the player.

Potential motives:

- threatened family
- ideological loyalty
- loyalty to Herx
- loyalty to a faction
- desire for money
- fear of death
- desire for revenge
- desire to manipulate the player
- belief that the player's quest is doomed
- desire to join the winning side

The motive should affect when and how betrayal occurs.

## 11.3 Betrayal should not always be malicious

A follower may betray the player while still caring about them.

Examples:

> “I told them where you were because they had my sister.”

> “I wanted you to win. I simply didn't think I could survive the way you were doing it.”

> “I never wanted you dead. I wanted them to stop chasing us.”

This creates moral conflict rather than a simple villain flag.

---

# 12. Sabotage

Sabotage should be rare, legible in hindsight, and mechanically consequential.

Potential forms:

### Resource theft

- steal gold
- steal food
- steal consumables
- hide useful items

### Intelligence leakage

- reveal player location
- reveal player composition
- reveal equipment strategy
- reveal intended destination

### Social sabotage

- lie about another follower
- poison trust between followers
- give false directions
- create unnecessary arguments

### Combat sabotage

- refuse a command
- abandon the party at a critical time
- intentionally fail to help
- strengthen an enemy through a special interaction

### Boss sabotage

A spy may create a real boss advantage by leaking information or activating a special state.

The important design rule is:

> **The player should discover what happened through consequences and evidence, not through a system notification saying “Your follower betrayed you.”**

---

# 13. The Minotaur as a Model for Chaotic Social Consequences

Barony already has a highly recognizable Minotaur timing system: on levels where a Minotaur attack is scheduled, a countdown occurs and the Minotaur subsequently enters and pursues the nearest player. ([barony.wiki.gg](https://barony.wiki.gg/wiki/Minotaur))

That makes it a strong candidate for social sabotage.

A sufficiently credible spy could reveal the party's location or otherwise trigger a world-state consequence that causes the player to suspect:

> “Someone told the Minotaur where we were.”

The exact engine trigger remains unspecified here.

The social system only requires:

```text
spy_action:
  type: leak_location
  target: hostile_force
  consequence: increased_danger
```

This preserves the core principle that **AI decides social intent; game state determines mechanical reality**.

---

# 14. Party Dynamics

The player should not be the only relationship center.

Followers should have relationships with one another.

Examples:

- friendship
- rivalry
- fear
- professional respect
- jealousy
- shared history
- cultural hostility
- teacher/student relationship
- family
- former enemies
- mutual distrust

This permits scenes such as:

> “Ask Pip. He knows this place better than I do.”

or:

> “Don't listen to her. She lies when she's frightened.”

or:

> “Why are you still keeping him around?”

The player becomes the mediator of a small social system.

---

# 15. Direct Conversation With Specific Followers

The system needs a way for the player to address an individual follower explicitly.

Desired conceptual interactions:

- “Rukka, what do you think?”
- “Pip, do you trust Rukka?”
- “Mara, have you seen this before?”
- “Rukka, tell Pip what you found.”
- “Everybody, listen.”

The AI should identify the intended conversational target and preserve the distinction between:

- player → one follower
- player → party
- follower → player
- follower → follower

This is important for both party roleplay and the eventual possibility of multi-NPC conversations.

---

# 16. Shopkeepers as Information Brokers

Shopkeepers should be among the most socially valuable NPCs in the system.

Barony already gives shopkeepers several useful characteristics for this role:

- they are distributed across the dungeon
- they interact with many travelers
- they understand value and trading
- shop types specialize their inventory
- higher Trading/Charisma unlocks private selections
- they have distinct reactions to hostile or experienced customers
- they can remember hostility through the Wanted state

These existing systems make the shopkeeper an ideal foundation for an **information economy** rather than requiring an entirely new NPC category. ([barony.wiki.gg](https://barony.wiki.gg/wiki/Shopkeeper), [barony.wiki.gg](https://barony.wiki.gg/wiki/Trading))

## 16.1 Shopkeeper information categories

### Local rumors

- nearby monsters
- dangerous rooms
- unusual travelers
- local deaths
- resource shortages

### Trade intelligence

- valuable item types
- buyers and sellers
- scarcity
- expected prices
- unusual demand

### Social intelligence

- who has been through recently
- who is allied with whom
- who is wanted
- who is trustworthy
- who owes money

### Historical knowledge

- local settlement history
- old stories
- famous adventurers
- Herx-era events

### Specialist information

Based on shop type and individual history:

- weapons
- armor
- food
- potions
- tools
- magic
- jewelry

### Rare secrets

- hidden routes
- faction plans
- boss information
- unusual NPC identities
- valuable item locations

Rare information should require a relationship, payment, favor, threat, or unusual circumstance.

---

# 17. Information Has Value

Information should not be treated as free flavor text.

The proposed internal information-value model is:

```text
information value = rarity
                  × usefulness
                  × danger
                  × personal risk
```

This is qualitative rather than necessarily a literal numerical formula.

A shopkeeper may freely share:

> “There's a spider infestation down the hall.”

but demand payment for:

> “A powerful artifact was recently brought through here.”

and refuse to reveal:

> “Who is secretly working for Herx?”

unless the player has earned sufficient trust or leverage.

---

# 18. Shopkeeper Personality and Information Behavior

Shopkeepers should not all behave identically.

Potential archetypes:

- honest professional
- gossip
- miser
- extortionist
- charitable merchant
- former adventurer
- coward
- collector
- information broker
- opportunist
- faction loyalist
- paranoid survivor

A shopkeeper can also deliberately mislead the player.

However, false information should require a reason.

Possible motives:

- protect another customer
- protect a faction
- protect themselves
- dislike the player
- manipulate the player into buying something
- conceal their involvement in an event
- favor another NPC

The engine should remain authoritative about real game state.

The AI only controls the shopkeeper's **claim and motive**.

---

# 19. Trading Skill vs. Social Relationship

The existing Trading system should remain meaningful rather than being replaced by the AI relationship system.

Trading skill already changes prices and shopkeeper dialogue, while Charisma contributes to Trading and helps unlock private selections. ([barony.wiki.gg](https://barony.wiki.gg/wiki/Trading))

The AI social layer should therefore add a second axis:

```text
mechanical bargaining skill
        +
relationship / reputation
        =
character-specific negotiation behavior
```

A highly skilled but disliked player may still get a grudgingly fair deal.

A trusted customer with mediocre Trading may receive:

> “Don't worry about it. Take it at my cost.”

A manipulative shopkeeper may recognize that the player has excellent Trading and deliberately hide information instead of changing the item price.

---

# 20. Social Chaos

The system should intentionally allow unexpected outcomes, but **not arbitrary outcomes**.

The principle is:

> **Chaotic because characters have conflicting motives; not chaotic because the AI rolls random nonsense.**

Good chaos:

- a spy genuinely cares about the player but betrays them under coercion
- two followers both want the same objective for different reasons
- a shopkeeper lies because they fear another NPC
- an NPC deserts because their dependence falls below a threshold
- a follower reveals a secret at exactly the wrong time
- a trusted character changes allegiance after a major world-state event

Bad chaos:

- random unexplained betrayal
- random boss buff with no cause
- arbitrary friendship changes
- NPC suddenly knowing secret information
- NPC changing personality because the LLM generated a different response

Persistent state must dominate.

---

# 21. Alternative Boss Outcomes

The eventual long-term goal is to make some final-boss encounters solvable without conventional combat.

This should not mean every boss becomes diplomatically defeatable.

It should mean that **socially earned alternatives exist**.

Potential outcomes:

### Conventional victory

Kill the boss normally.

### Negotiated passage

Convince the boss to allow the player to leave or proceed.

### Bribery

Provide something valuable enough to change the boss's incentives.

### Bargain

The player accepts a cost in exchange for passage or power.

### Faction alignment

Join the boss or become aligned with its interests.

### Manipulation

Turn one powerful faction against another.

### Secret route

Use information acquired from followers/shops to bypass the fight.

### Conditional surrender

Convince the boss that fighting is no longer advantageous.

### Alternate portal

A socially earned interaction causes a valid exit/ending state to become available.

The player should not necessarily know that these routes exist at the beginning of a run.

---

# 22. Social Victory Should Be Discovered

The player should not see:

> “Unlock Diplomacy Ending: 4/6 relationships.”

Instead, clues should accumulate naturally.

For example:

1. A follower remembers Herx.
2. A shopkeeper knows someone who worked for him.
3. A Guild NPC explains something about the Caves.
4. Another follower warns that the first one is withholding information.
5. A boss reveals an unexpected concern.
6. The player realizes there may be a non-combat path.

The route emerges through social exploration.

This matches Barony's broader storytelling tradition of fragmentary information rather than a conventional quest log.

---

# 23. Ending State Model

The ending system should eventually evaluate the accumulated social history of the run.

Potential dimensions:

- major NPC relationships
- follower survival
- follower loyalty
- betrayals committed
- secrets discovered
- secrets kept
- faction reputation
- shopkeeper reputation
- major bargains
- boss relationships
- player race/form history
- world-state changes
- major promises kept/broken
- known weaknesses
- alternate alliances

The actual ending should emerge from the resulting state rather than from one conversation.

---

# 24. Canonical Character Knowledge vs. Generated Social History

Generated social history must never silently rewrite Barony canon.

For example:

Allowed:

> A generated follower used to work in a shop in Minetown.

Not automatically allowed:

> That follower personally knew Baron Herx.

unless the generated biography explicitly creates that rare relationship and it does not contradict established lore.

For named canon characters, the rules should be stricter:

- preserve canonical relationships
- preserve canonical motivations
- preserve canonical identity
- expand only where the source material leaves room

The social system may add:

- feelings toward the player
- memories of player actions
- newly created relationships that occur during the run
- consequences of world-state changes

---

# 25. Memory Principles

## 25.1 Event memory is more important than dialogue transcript memory

NPCs should remember meaningful events rather than storing every sentence spoken.

Examples of high-value memories:

- player rescued them
- player threatened them
- player stole from them
- player killed someone they cared about
- player kept a promise
- player broke a promise
- player transformed in front of them
- follower witnessed a boss encounter
- player gave a meaningful gift

Routine transactions should compress.

> “Player bought bread three times.”

can become:

> “Regular customer who often buys food.”

---

# 26. Information Provenance

Every important piece of social knowledge should ideally preserve:

- source
- confidence
- acquisition method
- timestamp/recency
- whether the NPC witnessed it
- whether the NPC believes it
- whether the NPC is willing to disclose it

Example:

```text
fact: Orpheus has plans for the Crystal Caves
source: Guild colleague
acquisition: overheard
confidence: medium
belief: strong
disclosure: reluctant
```

This allows the AI to produce natural uncertainty:

> “I've heard the Archmagisters are doing something down there. I wouldn't swear to it.”

rather than false omniscience.

---

# 27. Relationship Graph + Social System

The relationship graph in `barony_lore.json` provides canonical or generated social connections.

The social system adds:

- relationship strength
- emotional state
- incentives
- secrets
- behavior triggers
- consequences

The distinction is:

```text
Lore graph:
    Who is connected to whom?

Social state:
    How does this relationship currently feel?

Event memory:
    What happened between them?

Social behavior:
    What does the NPC do because of that relationship?
```

---

# 28. Anti-Hallucination Rules

The social AI must obey these principles:

1. Engine state beats dialogue.
2. Canon beats generated biography.
3. Personal memory beats generic assumptions.
4. A witness has stronger knowledge than a rumor.
5. A source with uncertainty should remain uncertain.
6. NPCs may lie, but lies require motives.
7. NPCs may be wrong, but mistakes should arise from limited knowledge, memory, rumor, or deception.
8. A relationship does not automatically grant knowledge.
9. A high friendship score does not force honesty.
10. The AI should say “I don't know” when the character has no plausible basis for an answer.

---

# 29. Social Consequence Design Rules

### Rule 1 — Consequences should be reversible when possible

Minor offenses should often create opportunities for reconciliation.

### Rule 2 — Major betrayals should matter

A follower who intentionally causes a major disaster should not instantly return to normal after one friendly conversation.

### Rule 3 — Give the player evidence

Players should generally be able to reason backward from consequences and discover what happened.

### Rule 4 — Rare powerful relationships should remain rare

Not every follower should have a secret boss connection.

### Rule 5 — Don't optimize every interaction

Social systems should sometimes create messy, ambiguous outcomes.

### Rule 6 — Protect the chaos

The goal is not to make every NPC perfectly predictable. It is to make their unpredictability understandable in retrospect.

---

# 30. Proposed Social Archetypes

These are behavioral archetypes, not rigid NPC classes.

### Loyalist

Strong attachment, high trust, low betrayal probability.

### Opportunist

Will cooperate while the relationship is profitable.

### Coward

Loyal when safe; unreliable under extreme pressure.

### Protector

Strong dependence and loyalty toward vulnerable party members.

### Zealot

Strong ideological commitment that can override personal affection.

### Spy

Maintains a hidden competing loyalty.

### Double Agent

May genuinely support both sides and switch based on circumstances.

### Conflicted

Relationship state changes dramatically when certain values are challenged.

### Secret Keeper

Knows more than they are willing to disclose.

### Gossip

High information acquisition and poor information discipline.

### Manipulator

Builds relationships primarily to gain leverage.

### Reluctant Companion

Doesn't particularly like the player but has reason to remain.

---

# 31. Proposed Shopkeeper Archetypes

### Professional

Information is a commodity and business comes first.

### Gossip

Will trade information socially rather than financially.

### Survivor

Knows danger and values stability.

### Former Adventurer

Rich in firsthand dungeon knowledge.

### Information Broker

Explicitly treats secrets as valuable.

### Opportunist

Sells information to whoever pays more.

### Protector

Withholds information if revealing it would endanger someone.

### Conspirator

Knows more about a faction than the player expects.

---

# 32. Relationship-Based Information Disclosure

Information disclosure should depend on more than friendship.

Possible inputs:

```text
friendship
trust
respect
fear
relationship_to_target
information_value
personal_risk
player_reputation
current_world_state
follower_motivation
```

Examples:

### Friendly but afraid

> shares low-risk information
> withholds dangerous secrets

### Distrustful but respectful

> gives accurate professional advice
> avoids personal disclosures

### High friendship but ideological conflict

> remains warm but refuses certain requests

### High trust + high dependence

> shares deeply personal information

### High respect + low friendship

> may provide useful tactical advice without becoming emotionally close

---

# 33. Follower Death and Replacement

Follower death should matter socially.

Surviving followers may:

- grieve
- become angry
- blame the player
- blame another follower
- become fearful
- become more loyal
- become less willing to join
- seek revenge

A follower's death should be a major event-memory candidate.

However, the system should not automatically assign identical grief to everyone.

One follower may say:

> “He was my friend.”

Another:

> “He was useful. We needed him.”

A third:

> “I told you he wouldn't survive this.”

All three reactions can be coherent.

---

# 34. Promises and Commitments

Promises are a useful bridge between dialogue and long-term memory.

The system should allow NPCs to make commitments such as:

- “I'll bring you the artifact.”
- “I won't tell anyone.”
- “I'll protect her.”
- “We'll go to the Caves.”
- “I won't steal from you again.”

A later event can verify whether the commitment was actually kept.

Keeping promises should increase relevant relationship dimensions.

Breaking them should create persistent resentment or reduced trust.

The player should not be able to erase a broken promise simply by talking about it.

---

# 35. Social Scarcity

Powerful social effects should be scarce.

Examples:

- boss weakness revelation: rare
- major betrayal: rare
- ending-changing relationship: very rare
- unique lore secret: uncommon
- minor gift: occasional
- gossip: common

This prevents the system from becoming noisy.

---

# 36. Chaos Without Noise

The system should feel chaotic because:

- people have conflicting incentives
- relationships evolve
- information is imperfect
- characters make decisions
- secrets exist
- the player cannot predict every motivation

It should **not** feel chaotic because:

- every NPC talks constantly
- every event triggers a social reaction
- followers give gifts every floor
- spies betray the player every hour
- every shopkeeper knows ten secret plots

Restraint is part of immersion.

---

# 37. Test Scenarios

The following scenarios should eventually be used as design tests.

### Scenario A — Loyal follower

Player protects follower repeatedly.

Expected:

- trust increases
- affection increases
- follower becomes more forthcoming
- occasional initiative becomes possible

### Scenario B — Useful but disliked follower

Player treats follower harshly but relies on them.

Expected:

- respect may rise
- affection falls
- follower remains useful
- eventual resentment may emerge

### Scenario C — Secret spy

Follower likes the player but is coerced by an enemy.

Expected:

- genuine affection remains
- information leakage becomes possible
- betrayal is explainable afterward

### Scenario D — Shopkeeper relationship

Player becomes a repeat customer, pays fairly, and protects shopkeeper.

Expected:

- shopkeeper becomes warmer
- information disclosure improves
- rare information becomes available before price discounts necessarily do

### Scenario E — Boss knowledge

A follower with a canonically plausible relationship to Herx becomes deeply trusted.

Expected:

- follower reveals previously withheld information
- player gains an opportunity for a meaningful boss-state advantage

### Scenario F — False information

A dishonest follower intentionally lies about an unidentified item.

Expected:

- engine item state remains correct
- follower memory records the lie
- later discovery can damage trust

### Scenario G — Follower conflict

Two followers have incompatible values.

Expected:

- arguments emerge naturally
- player can mediate, ignore, support one side, or lose one/both

### Scenario H — Social victory

Player accumulates multiple relationships, secrets, and faction connections.

Expected:

- an alternative boss outcome becomes possible without being explicitly presented as a quest

---

# 38. Future Expansion

Potential future systems, deliberately not required for v1:

- faction reputation
- family networks
- inheritance
- NPC marriage/partnership
- persistent cross-run relationships
- shopkeeper businesses changing based on player actions
- NPCs recruiting other NPCs
- follower-specific quests
- faction wars
- rival adventuring parties
- dynamic gossip networks
- long-term NPC aging
- unique social endings

These should remain out of scope until the basic follower/shopkeeper system proves fun.

---

# 39. Implementation Philosophy

This document intentionally stops before C++ implementation.

The desired separation is:

```text
Lore DB
   ↓
What is true / what is plausible knowledge?
   ↓
Social model
   ↓
What does this character want, fear, trust, resent, hide, or reveal?
   ↓
AI decision
   ↓
What does the NPC say / attempt?
   ↓
Engine validation
   ↓
What actually happened?
   ↓
Event memory
   ↓
Lore/social state updated
```

The engine should remain the final authority.

The AI should never be allowed to turn a spoken claim into objective game state without a validated transaction.

---

# 40. Open Design Questions

These should be decided through playtesting rather than prematurely locked.

1. Should the internal friendship score use 0–100 exactly, or should it be normalized to another range?
2. How many hidden relationship dimensions should the first implementation actually use?
3. How frequently should follower initiative occur?
4. What percentage of generated followers should contain a secret agenda?
5. How rare should spies be?
6. Can a spy be permanently redeemed?
7. How much should a shopkeeper charge for valuable information?
8. Should information purchases consume gold, favors, goods, reputation, or several currencies?
9. How many followers can plausibly have meaningful boss history?
10. Should every final boss have at least one possible non-combat route?
11. Should alternative endings be generated from social state or hand-authored around a set of validated conditions?
12. How much should followers interact with each other without player prompting?
13. Should the player ever receive explicit confirmation that a follower is lying?
14. How much misinformation is fun before it becomes frustrating?
15. How much should follower personality persist across runs versus being generated fresh each run?

---

# 41. Current Design Position

The project should currently treat the social system as a **layer of consequence**, not as a replacement for Barony's core gameplay.

The best version of this system should make the player think:

> “I kept Rukka alive because she's useful.”

and eventually:

> “I kept Rukka alive because she's Rukka.”

That transition is the central success criterion.

The same principle applies to shopkeepers:

> “I need this shop for supplies.”

should gradually become:

> “I need to know what this shopkeeper knows.”

And eventually:

> “I can't afford to lose this person because nobody else knows what they know.”

The intended end state is a Barony run where **people themselves become part of the player's strategic toolkit, social risk, and narrative path through the dungeon**.

---

# Appendix A — Relationship-to-Gameplay Matrix

| Relationship state | Typical NPC behavior | Possible gameplay effect |
|---|---|---|
| Stranger | Neutral/transactional | Little or no special effect |
| Acquaintance | Minor conversation | Local tips, small favors |
| Developing | More personal disclosure | Warnings, useful minor items |
| Trusted | Voluntary initiative | Gifts, identification, route advice |
| Deep bond | Major disclosure | Secrets, rare knowledge, special assistance |
| Strained | Irritation/withdrawal | Reduced help, arguments |
| Resentful | Passive resistance | Refusal, departure, misinformation |
| Hostile | Active opposition | Sabotage or betrayal |
| Secret alliance | Hidden cooperation | Major unexpected opportunities or threats |

---

# Appendix B — Design Vocabulary

**Canon:** directly established by Barony's source material or current game data.

**Inference:** a reasonable interpretation supported by canon but not explicitly established.

**Generated biography:** procedural content created for a particular NPC.

**Event:** an actual occurrence in the game run.

**Memory:** an NPC-specific representation of an event.

**Rumor:** information learned indirectly and potentially degraded or distorted.

**Knowledge:** a fact the NPC currently believes or knows, with provenance.

**Relationship state:** the emotional/social condition of one NPC toward another.

**Social action:** an NPC action motivated by relationships or information.

**World state:** authoritative game state outside the AI's control.

**Social consequence:** a gameplay-relevant effect caused by NPC behavior or relationship state.

---

# Appendix C — Existing Barony Systems This Design Intentionally Builds Upon

- Leadership and follower commands
- Trading and Charisma
- Shopkeeper hostility/Wanted state
- Shop private selections
- Item identification/appraisal
- Minotaur timed appearances
- Mystic Orb and boss progression systems
- Existing boss progression and endings
- Existing NPC/follower relationships
- Existing save/game-state persistence

The social layer should extend these systems rather than replace them wherever possible.
