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
