#!/usr/bin/env python3
"""Summarise a Barony AI session log.

    python3 logreview.py                  # newest session in logs/
    python3 logreview.py logs/session-*.jsonl

Reads the JSONL timeline the service writes (and that the C++ side pushes into) and answers
the questions that actually come up after a playthrough: what broke, what was slow, what did
each relationship do, and did anything fire far more often than it should have.
"""
import json, sys, glob, os, collections

BASE = os.path.dirname(os.path.abspath(__file__))

def load(path):
    recs = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                print(f"  ! unparseable line {i}")
    return recs

def pct(vals, p):
    if not vals: return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p / 100))]

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        paths = args
    else:
        paths = sorted(glob.glob(os.path.join(BASE, "logs", "session-*.jsonl")))[-1:]
    if not paths:
        print("no session logs found in logs/"); return 1
    for path in paths:
        recs = load(path)
        print(f"\n{'='*72}\n{os.path.basename(path)}   {len(recs)} records")
        if not recs:
            continue
        dur = recs[-1].get("t", 0)
        print(f"duration {dur/60:.1f} min\n{'='*72}")

        kinds = collections.Counter(r["kind"] for r in recs)
        print("\nRECORDS BY KIND")
        for k, v in kinds.most_common():
            print(f"   {k:<12} {v}")

        # --- problems first: that is what the log is for ---
        errs = [r for r in recs if r["kind"] == "error"]
        if errs:
            print(f"\n!! ERRORS ({len(errs)})")
            for r in errs[:10]:
                print(f"   t={r['t']:>8.1f}  {r.get('err','')}")
        recovered = [r for r in recs if r.get("recovered")]
        if recovered:
            print(f"\n!! MALFORMED MODEL JSON, recovered by fallback: {len(recovered)}")
        notes = [r for r in recs if r["kind"] in ("mod", "cpp") and "NOTE" in r.get("msg", "")]
        if notes:
            print(f"\n>> PLAYER NOTES ({len(notes)}) -- start here")
            for r in notes:
                print(f"   t={r['t']:>8.1f}  {r.get('msg','')}")

        # --- latency ---
        gens = [r["gen_ms"] for r in recs if r.get("gen_ms")]
        if gens:
            print(f"\nGENERATION LATENCY  n={len(gens)}  "
                  f"median {pct(gens,50)/1000:.1f}s  p90 {pct(gens,90)/1000:.1f}s  max {max(gens)/1000:.1f}s")
            slow = [r for r in recs if r.get("gen_ms", 0) > 15000]
            if slow:
                print(f"   !! {len(slow)} generation(s) over 15s")
        pchars = [r["prompt_chars"] for r in recs if r.get("prompt_chars")]
        if pchars:
            # ~4 chars/token. The 8B has 16k ctx, but attention -- not context -- has always
            # been the limiting factor here, so growth is worth watching.
            med_tok, max_tok = pct(pchars, 50) // 4, max(pchars) // 4
            warn = "   <-- large; instructions start competing" if max_tok > 3500 else ""
            print(f"PROMPT SIZE         median ~{med_tok} tok  max ~{max_tok} tok{warn}")

        # --- who was secretly what, and the Herx arc ---
        alleg = [r for r in recs if r["kind"] == "allegiance"]
        if alleg:
            print("\nHIDDEN ALLEGIANCES (never shown to the player in game)")
            for r in alleg:
                spy = "  <-- SPY" if r.get("allegiance") == "spy" else ""
                mot = f"  motive: {r['motive']}" if r.get("motive") else ""
                print(f"   uid {r.get('uid')} ({r.get('race','?')}) {r.get('allegiance')}{spy}{mot}")
        ng = [r for r in recs if r["kind"] == "no_grief"]
        if ng:
            deaths = [r for r in ng if not r.get("struck")]
            hits = [r for r in ng if r.get("struck")]
            print("\nSPIES DEALT WITH (no social cost, because they had it coming)")
            if deaths:
                print(f"   {len(deaths)} companion(s) watched a spy die and did not hold it "
                      "against the player")
            if hits:
                print(f"   {len(hits)} strike(s) on a spy cost nothing")

        reb = [r for r in recs if r["kind"] == "rebind"]
        if reb:
            print("\nCOMPANION CONTINUITY (summons and bots that came back)")
            by_id = {}
            for r in reb:
                by_id[r.get("identity")] = r
            for ident, r in by_id.items():
                who = (" as '%s'" % r["name"]) if r.get("name") else ""
                # The count is the interesting number: it says how much of the run this
                # creature spent dead, and it is what the resummoned claim tells the model.
                n = r.get("bodies", 1) - 1
                print(f"   {ident}{who}: {r.get('bodies', 1)} bodies "
                      f"({n} recall{'' if n == 1 else 's'})")

        named = [r for r in recs if r["kind"] == "named"]
        if named:
            print("\nNAMES REVEALED")
            for r in named:
                # A follower that ignored its reserved name is the one interesting case:
                # it means the prompt lost, and that name is now the model's own low-entropy
                # pick -- the thing follower_names.json exists to avoid.
                own = "  <-- self-chosen, ignored '%s'" % r.get("assigned") if r.get("self_chosen") else ""
                print(f"   uid {r.get('uid')} ({r.get('race','?')}) is '{r.get('name')}'{own}")

        herx = [r for r in recs if r["kind"] == "herx"]
        if herx:
            print("\nHERX ARC")
            for r in herx:
                bits = [f"stage={r.get('stage')}", f"uid={r.get('uid')}"]
                if r.get("is_false"): bits.append("FALSE SECRET (spy)")
                if r.get("debuff") is not None: bits.append(f"debuff={r['debuff']}")
                print(f"   t={r['t']:>8.1f}  " + "  ".join(bits))
                if r.get("truth"): print(f"                 \"{r['truth']}\"")
            if not any(r.get("stage") == "revealed" for r in herx):
                print("   (offered but never actually told -- the branch that is otherwise invisible)")

        # --- events, with a noise check ---
        evs = collections.Counter(r["event"] for r in recs if r["kind"] == "event")
        if evs:
            print("\nEVENTS FIRED")
            for k, v in evs.most_common():
                flag = ""
                if k in ("left_behind", "hurt_by_player", "ally_died") and v > 6:
                    flag = "   <-- suspicious volume, check for false positives"
                if k == "healed_by_player" and v > 10:
                    flag = "   <-- suspicious volume (level-ups misread as heals?)"
                print(f"   {k:<18} {v}{flag}")

        # --- per-follower relationship trajectory ---
        by_uid = collections.OrderedDict()
        for r in recs:
            if r.get("dims") and r.get("uid"):
                by_uid.setdefault(r["uid"], []).append(r)
        if by_uid:
            # first -> last ALONE hides a round trip: trust built up by fighting together and
            # then destroyed by friendly fire ends where it started and looks like "no change",
            # when it is the most interesting thing that happened. Peaks are shown too.
            print("\nRELATIONSHIPS (first -> last, with peak/trough where it round-tripped)")
            for uid, rs in by_uid.items():
                a, b = rs[0]["dims"], rs[-1]["dims"]
                race = rs[-1].get("race", "?")
                name = next((x.get("name") for x in reversed(rs) if x.get("name")), "")
                parts = []
                for k in b:
                    series = [r["dims"].get(k, 0) for r in rs]
                    lo, hi, v0, v1 = min(series), max(series), a.get(k, 0), b.get(k, 0)
                    if v0 == v1 and lo == hi:
                        continue                      # genuinely never moved
                    extra = ""
                    if hi > max(v0, v1): extra += f" peak {hi}"
                    if lo < min(v0, v1): extra += f" low {lo}"
                    parts.append(f"{k[:4]} {v0}->{v1}{extra}")
                shown = "  ".join(parts)
                print(f"   uid {uid} ({race}{', '+name if name else ''}) x{len(rs)}: {shown or 'no change'}")

        # --- ⚠ THE BIG ONE FOR THE FIRST PLAYTEST: does follower identity survive a staircase?
        # follower_state is keyed by uid, and a level change RE-CREATES every follower as a new
        # entity with a new uid (game.cpp:2438 -> summonMonster, entity_shared.cpp:490). If that
        # is right, the same creature appears here twice under different uids -- and everything
        # uid-keyed (friendship, allegiance, the event log, the name) silently starts over.
        newf = [r for r in recs if r.get("kind") == "newfollower"]
        if newf:
            by_name = {}
            for r in newf:
                nm = (r.get("engine_name") or "").strip()
                if nm:
                    by_name.setdefault((nm, r.get("race", "?")), []).append(r)
            repeats = {k: v for k, v in by_name.items() if len({x.get("uid") for x in v}) > 1}
            print(f"\nFOLLOWER IDENTITY  {len(newf)} state row(s) created this run")
            if repeats:
                print("   !! UID CHURN CONFIRMED -- the same named creature got a NEW state row:")
                for (nm, race), rs in repeats.items():
                    uids = ", ".join(str(x.get("uid")) for x in rs)
                    floors = ", ".join(str(x.get("floor")) for x in rs)
                    alls = ", ".join(str(x.get("allegiance")) for x in rs)
                    print(f"      {nm} ({race}): uids {uids}  on floors {floors}")
                    print(f"          allegiance re-rolled each time: {alls}")
                print("   -> friendship, events, allegiance and the assigned name all restart")
                print("      at every staircase. The Herx secret (friendship 50 + 4 fights) is")
                print("      unreachable in real play while this holds.")
            else:
                # ⚠ Names are the PROOF, but they need the engine to be sending follower_name
                # AND the follower to have named itself. Without either, fall back to the shape
                # of the data: the same race getting fresh rows on a later floor is churn in all
                # but name -- the first real session showed skeleton/skeleton/rat on floor 1 and
                # skeleton/skeleton/rat again on floor 2 with brand new uids.
                by_race = {}
                for r in newf:
                    by_race.setdefault(r.get("race", "?"), []).append(r)
                sus = {k: v for k, v in by_race.items()
                       if len({x.get("floor") for x in v if x.get("floor")}) > 1}
                if sus:
                    print("   !! UID CHURN SUSPECTED -- the same race got fresh rows on a later")
                    print("      floor (no names to confirm with, but this is the shape of it):")
                    for race, rs in sus.items():
                        for x in rs:
                            print(f"      {race:<9} uid {str(x.get('uid')):<6} floor "
                                  f"{str(x.get('floor')):<4} rolled {x.get('allegiance')}")
                    alls = {x.get("allegiance") for rs in sus.values() for x in rs}
                    if len(alls) > 1:
                        print("      -> allegiance differs between rows: it was RE-ROLLED, which")
                        print("         only happens when a brand new state row is created.")
                    print("   -> friendship, events and the assigned name restart each staircase.")
                elif len(newf) > 1:
                    print("   no repeated names and no repeated races across floors --")
                    print("   either identity held, or nothing was named yet.")
                if not any(r.get("engine_name") for r in newf):
                    print("   NOTE: no engine_name on any row -- the game binary predates the")
                    print("   follower_name field, so the name-based proof could not run.")

        # --- ⚠ CHATTER VOLUME: the thing most likely to need tuning on a first run ---
        rem = [r for r in recs if r.get("kind") in ("remark", "valuable")]
        if rem:
            dur_min = max((recs[-1]["t"] - recs[0]["t"]) / 60.0, 0.01)
            TIERS = {0: "reactive", 1: "event", 2: "idle", -1: "?"}
            per_kind, per_tier = {}, {}
            for r in rem:
                k = r.get("what") or r.get("kind")
                per_kind[k] = per_kind.get(k, 0) + 1
                per_tier[TIERS.get(r.get("tier", -1), "?")] = \
                    per_tier.get(TIERS.get(r.get("tier", -1), "?"), 0) + 1
            print(f"\nCHATTER  {len(rem)} remarks in {dur_min:.1f} min "
                  f"= {len(rem)/dur_min:.1f}/min")
            print("   by tier: " + "  ".join(f"{k} {v}" for k, v in sorted(per_tier.items())))
            top = sorted(per_kind.items(), key=lambda kv: -kv[1])[:12]
            print("   by kind: " + "  ".join(f"{k} {v}" for k, v in top))
            gaps = [rem[i]["t"] - rem[i-1]["t"] for i in range(1, len(rem))]
            if gaps:
                tight = sorted(gaps)[:3]
                print(f"   closest together: {', '.join(f'{g:.1f}s' for g in tight)}")
                if len(rem) / dur_min > 3:
                    print("   !! over 3/min -- consider raising MYMOD_TIER_GAP for idle/event")

        # --- the mercenary's economy ---
        merc = [r for r in recs if r.get("kind") == "merc"]
        if merc:
            print(f"\nMERCENARY ({len(merc)})")
            for r in merc[:14]:
                print(f"   t={r['t']:>8.1f}  {r.get('what','?'):<9} {r.get('stage','?'):<8} "
                      f"price={r.get('price')} gold={r.get('gold')}")

        # --- the rat ---
        rat = [r for r in recs if r.get("kind") == "ratfear"]
        if rat:
            print(f"\nTHE RAT ({len(rat)})")
            for r in rat:
                tag = "LEGEND" if r.get("legend") else "first"
                nm = r.get("ratname") or "(unnamed)"
                print(f"   t={r['t']:>8.1f}  {tag}  {r.get('boss')}  rat={nm}")
                print(f"      \"{r.get('reply','')}\"")

        # --- special outcomes ---
        for label, key in (("BOONS", "boon"), ("HERX SECRET", "secret"), ("IDENTIFY", "identify")):
            hits = [r for r in recs if r.get(key)]
            if hits:
                print(f"\n{label} ({len(hits)})")
                for r in hits[:12]:
                    print(f"   t={r['t']:>8.1f} uid={r.get('uid')} {key}={r.get(key)}")

        boons = [r for r in recs if r["kind"] == "boon"]
        if boons:
            print(f"\nBOONS ROLLED ({len(boons)})")
            for r in boons:
                what = r.get("item") or (r.get("fact", "")[:60])
                print(f"   t={r['t']:>8.1f} uid={r.get('uid')} {r.get('boon_kind','?'):<6} {what}")

        amb = [r for r in recs if r["kind"] in ("ambient", "taunt")]
        if amb:
            c = collections.Counter(r["kind"] for r in amb)
            print(f"\nWORLD CHANNEL   " + "  ".join(f"{k}={v}" for k, v in c.items()))

        tones = collections.Counter(r.get("tone") for r in recs if r["kind"] == "tone")
        if tones:
            print("\nHOW THE PLAYER SPOKE  " + "  ".join(f"{k}={v}" for k, v in tones.most_common()))

        npcs = [r for r in recs if r["kind"] == "npc"]
        if npcs:
            who = collections.Counter((r.get("npc_name") or r.get("race","?")) for r in npcs)
            print(f"\nNPC CONVERSATIONS ({len(npcs)})")
            for k, v in who.most_common(10):
                print(f"   {k:<20} {v}")

        cpp = [r for r in recs if r["kind"] == "cpp" and "NOTE" not in r.get("msg","")]
        if cpp:
            print(f"\nENGINE-SIDE EVENTS ({len(cpp)})")
            for r in cpp[-15:]:
                print(f"   t={r['t']:>8.1f}  {r.get('msg','')}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
