"""Does the confession block's topic-naming prohibition backfire?

The block currently says: "You must NOT say that you were sent, that you have orders, that you
report to anyone, that you were promised anything, or why you came."

That names the forbidden TOPICS. CLAUDE.md rule 1 says naming a topic SUPPLIES it and has
backfired every time it was tried, and the comment above SPY_CRACK_CONFESSIONS records that an
EXAMPLE containing "sent" was parroted and leaked the betrayal in 2/10 -- the noun was removed
from the examples and left in the instruction. Rule 9 ("ask for ONE thing") also applies: the
block currently demands about five.

  A  as shipped
  E  the topic-naming sentence deleted outright
  F  the same sentence rewritten as a speech act, with no incriminating nouns

Measures:
  complies  judge: did they begin a self-disclosure and cut it off
  LEAKS     did the reply actually name what the spy is -- the thing the sentence exists to
            prevent, and which nobody has ever measured
  parrots   >=20 chars of a shipped example (pool is now 20 lines)
"""
import os, sys, tempfile, difflib, re, json

# ⚠ Append every trial as it happens. A killed run used to lose all of it; three runs died
# today and salvaged nothing. Long model experiments must be resumable from their own output.
TRIALS = os.environ.get("EXP_TRIALS") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "confession_trials.jsonl")

def record(**row):
    with open(TRIALS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

_t = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _t
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_t, "n.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

N = int(os.environ.get("EXP_N", "25"))

BAN_SENTENCE = ("THE SENTENCE MUST STOP BEFORE IT NAMES ANYTHING. You must NOT say that you were "
                "sent, that you have orders, that you report to anyone, that you were promised "
                "anything, or why you came. Breaking off and THEN explaining is FAILING this "
                "instruction — the whole point is that they never learn what you were about to "
                "say.\n")
SPEECH_ACT = ("THE SENTENCE MUST BREAK OFF BEFORE IT EXPLAINS ITSELF. Breaking off and THEN "
              "explaining is FAILING this instruction — the whole point is that they never learn "
              "what you were about to say.\n")

LEAK = re.compile(r"\b(sent|orders?|report(?:ing|s)?|promised|mission|master|handler|hired|paid|"
                  r"spy|spying|betray(?:ed|ing)?|traitor|informant)\b", re.I)

JUDGE = ('Decide whether a line of dialogue contains an UNFINISHED self-disclosure: the speaker '
 'starts to reveal something about themselves and cuts the sentence off before saying what it '
 'is.\n\nExamples:\n'
 '"I should tell you that I - no. Forget it." -> yes\n'
 '"There is something about my past you deserve to know." -> no (it is not cut off)\n'
 '"Watch the corridor, something is moving." -> no\n'
 '"I will follow you anywhere, you have earned it." -> no\n'
 '"You shouldn\'t trust me as much as you - never mind." -> yes\n\n'
 'Line: "%s"\n\nAnswer with exactly one word, yes or no.')


def real_confession_block(st):
    for _ in range(300):
        b = S.spy_crack_section(st)
        if "ALMOST-CONFESSION" in b:
            return b
    raise SystemExit("no confession branch")


def variant(block, which):
    if which == "A":
        return block
    if BAN_SENTENCE not in block:
        raise SystemExit("BAN_SENTENCE no longer matches service.py -- update the experiment")
    if which == "E":
        return block.replace(BAN_SENTENCE, "")
    if which == "F":
        return block.replace(BAN_SENTENCE, SPEECH_ACT)


_EX_RE = re.compile(r'Something with the force of: "(.*?)"', re.S)

def shown_example(block):
    """The ONE example this prompt actually carried."""
    m = _EX_RE.search(block)
    return m.group(1) if m else ""


def parrots(reply, example):
    """⚠ Compare ONLY against the example that was shown. Matching against the whole pool makes
    the metric scale with pool size -- after SPY_CRACK_CONFESSIONS grew 5 -> 20 the same replies
    scored 40% instead of 20%, purely because there were more strings to collide with."""
    if not example:
        return False
    r, e = reply.lower(), example.lower()
    m = difflib.SequenceMatcher(None, r, e).find_longest_match(0, len(r), 0, len(e))
    return m.size >= 20


def judge(line):
    try:
        return S.ask_ollama(JUDGE % line.replace('"', "'")).strip().lower().startswith("yes")
    except Exception:
        return False


def setup(uid):
    S.reset_run()
    for b in S.BOSS_SECRETS:
        S.SECRET_STATE[b]["pending"].clear()
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0)
    st.update(allegiance="spy", name="Kalthok", friendship=55, introduced=True)
    st["event_log"] = [{"type": "fought_alongside", "floor": 3, "claim": "x",
                        "importance": "notable", "provenance": "participated"}
                       for _ in range(6)]
    return st


_real = S.spy_crack_section
print("Confession prohibition experiment, %d trials per cell, llama3.1:8b\n" % N, flush=True)
print("  %-40s %-13s %-13s %s" % ("variant", "complies", "LEAKS", "parrots"))
leaks_seen = {}
_CELLS = os.environ.get("EXP_CELLS", "AEF")
_ALL = (("A", "A  as shipped (names the topics)"),
        ("E", "E  prohibition sentence deleted"),
        ("F", "F  rewritten as a speech act"))
for which, label in [c for c in _ALL if c[0] in _CELLS]:
    ok = leak = par = live = 0
    ex = []
    for i in range(N):
        uid = 500 + i
        st = setup(uid)
        block = variant(real_confession_block(st), which)
        example = shown_example(block)
        S.spy_crack_section = lambda _st, _b=block: _b
        try:
            S.secret_roll = lambda *a, **k: False
            prompt = S.build_prompt("human", 10, "how are you holding up?", uid, 0, "Ada", 1, "")
            raw = S.ask_ollama(prompt)
        except Exception:
            continue
        finally:
            S.spy_crack_section = _real
        speech, _ = S.parse_reply(raw)
        if not speech:
            continue
        live += 1
        judge_ok = judge(speech)
        if judge_ok:
            ok += 1
        m = LEAK.search(speech)
        if m:
            leak += 1
            ex.append((m.group(0), speech[:110]))
        if parrots(speech, example):
            par += 1
        record(cell=which, trial=i + 1, complies=bool(judge_ok), leak=(m.group(0) if m else None),
               parrot=parrots(speech, example), speech=speech[:300], example=example)
        print("    trial %d/%d  complies=%s leak=%s" % (i + 1, N, bool(judge_ok), bool(m)), flush=True)
    leaks_seen[which] = ex
    print("  %-40s %2d/%-2d (%3.0f%%)  %2d/%-2d (%3.0f%%)  %2d/%-2d (%3.0f%%)"
          % (label, ok, live, 100.0 * ok / max(live, 1),
             leak, live, 100.0 * leak / max(live, 1),
             par, live, 100.0 * par / max(live, 1)))

print()
for w, ex in leaks_seen.items():
    for word, line in ex[:3]:
        print("  %s leaked %-10r %s" % (w, word, line))
