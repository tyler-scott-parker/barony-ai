"""Re-measure offers-converted for the boss-secret arc, and quantify how much of the old
number was keyword false positives.

Every trial drives the real path -- build_prompt -> ask_ollama -> parse_reply -> herx_detect --
with secret_roll forced True, so what is measured is conversion GIVEN an offer, which is what
"offers converted" has always meant here.

Each generation is scored twice on the same text:
  OLD  any keyword hit, or a literal "secret" anywhere in raw   (the rule before today)
  NEW  a strong keyword alone, or two hits, or a model-written "secret"
Disagreements (OLD=told, NEW=dropped) are the suspect conversions the 81% included.
"""
import os, sys, json, time, tempfile, collections

_t = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _t
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_t, "n.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

N = int(os.environ.get("BENCH_N", "25"))
OUT = os.environ.get("BENCH_OUT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bench_transcripts.jsonl")

# ⚠ Append every trial as it happens, the same lesson bench_confession.py records: at n=45 a
# run is long enough that a kill would otherwise throw away every generation it had paid for.
# The final os.replace-free rewrite of OUT below still happens; this is the crash copy.
TRIALS = os.environ.get("BENCH_TRIALS") or (OUT + ".partial")


# ⚠ RUN IDENTITY ON EVERY ROW -- see the note in bench_confession.py. The run is a random
# effect here too: three cells whose code provably could not change moved -7, +4 and +5 points
# between two n=45 runs. Without a run id a file cannot be split into the batches that are the
# only comparisons worth making.
RUN_ID = os.environ.get("BENCH_RUN") or time.strftime("%Y%m%d-%H%M%S")

def record(row):
    row.setdefault("run", RUN_ID)
    row.setdefault("t", time.time())
    with open(TRIALS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

# Force the offer every trial: we are measuring conversion, not the roll.
S.secret_roll = lambda *a, **k: True

ASKS = {"herx":  "what do you know about Baron Herx?",
        "twins": "what do you know about the Archmagisters?"}


def old_rule(v, raw, speech):
    txt = ((speech or "") + " " + (raw or "")).lower()
    return '"secret"' in (raw or "").lower() or any(k in txt for k in v["keywords"])


def new_rule(v, raw, speech):
    txt = ((speech or "") + " " + (raw or "")).lower()
    hits = [k for k in v["keywords"] if k in txt]
    strong = [k for k in hits if k not in S.SECRET_WEAK_KEYWORDS]
    rawl = (raw or "").lower()
    said = '"secret"' in rawl.replace('"secret":', "")
    via = ("strong" if strong else "corroborated" if len(hits) >= 2
           else "literal" if said else None)
    return (bool(strong) or len(hits) >= 2 or said), hits, via


def setup(uid, boss, alleg):
    cfg = S.BOSS_SECRETS[boss]
    S.reset_run()
    race = cfg["races"][0]
    st = S.get_follower_state(uid, race, 0)
    # ⚠ Without `introduced` the intro block fires and ends with "make it the WHOLE of your
    # reply", which lands AFTER the secret override and wins -- 0% conversion, every time.
    st.update(allegiance=alleg, name="Kalthok", friendship=cfg["friendship"] + 10,
              introduced=True)
    st["event_log"] = [{"type": "fought_alongside", "floor": 3, "claim": "x",
                        "importance": "notable", "provenance": "participated"}
                       for _ in range(cfg["fights"] + 2)]
    floor = max(cfg["floor"], 1)
    if floor > cfg.get("until", 99):
        floor = cfg["floor"]
    return st, race, floor


def run(boss, alleg, n):
    rows = []
    for i in range(n):
        uid = 900 + i
        st, race, floor = setup(uid, boss, alleg)
        prompt = S.build_prompt(race, floor, ASKS[boss], uid, 0, "Ada", 1, "")
        pend = S.SECRET_STATE[boss]["pending"].get(uid)
        if not pend:
            rows.append({"skipped": "no offer for %s" % boss})
            continue
        vi, is_false = pend
        v = S._herx_pool(is_false, boss)[vi]
        try:
            raw = S.ask_ollama(prompt)
        except Exception as e:
            rows.append({"skipped": "ollama: %s" % e})
            continue
        speech, _action = S.parse_reply(raw)

        o = old_rule(v, raw, speech)
        nw, hits, via = new_rule(v, raw, speech)
        row = {"boss": boss, "alleg": alleg, "is_false": is_false, "variant": vi,
               "debuff": v["debuff"], "old": o, "new": nw, "hits": hits, "via": via,
               "speech": (speech or "")[:400], "truth": v["truth"][:80]}
        rows.append(row)
        record(row)
        sys.stderr.write("."); sys.stderr.flush()
    return rows


def report(label, rows):
    live = [r for r in rows if "skipped" not in r]
    if not live:
        print("  %-22s no usable trials (%s)" % (label, rows[0].get("skipped") if rows else "none"))
        return live
    n = len(live)
    o = sum(1 for r in live if r["old"])
    w = sum(1 for r in live if r["new"])
    via = collections.Counter(r["via"] for r in live if r["new"])
    print("  %-22s n=%-3d  OLD %2d/%-3d (%3.0f%%)   NEW %2d/%-3d (%3.0f%%)   via: %s"
          % (label, n, o, n, 100.0 * o / n, w, n, 100.0 * w / n,
             dict(via) or "-"))
    return live


print("Boss-secret conversion, %d trials per condition, model=%s" % (N, S.MODEL if hasattr(S, "MODEL") else "?"))
print("OLD = the rule before today; NEW = strong-alone / two-hits / model-written 'secret'\n")

all_rows = []
for boss in ("herx", "twins"):
    for alleg, label in (("loyal", "true secret"), ("spy", "false secret")):
        rows = run(boss, alleg, N)
        sys.stderr.write("\n")
        all_rows += report("%s / %s" % (boss, label), rows)

print()
sus = [r for r in all_rows if r["old"] and not r["new"]]
print("SUSPECT CONVERSIONS (old counted, new drops): %d of %d generations"
      % (len(sus), len(all_rows)))
for r in sus[:12]:
    print("   %s %s  hits=%s  debuff=%s" % (r["boss"], r["alleg"], r["hits"], r["debuff"]))
    print("      %r" % r["speech"][:200])

miss = [r for r in all_rows if r["new"] and not r["old"]]
if miss:
    print("\nNEW counts but OLD did not (should be impossible): %d" % len(miss))

with open(OUT, "w", encoding="utf-8") as fh:
    for r in all_rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print("\ntranscripts -> %s" % OUT)
