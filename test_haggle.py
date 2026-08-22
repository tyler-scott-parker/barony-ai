"""Merchant haggling: detection, regard, resolution, and the caps that keep it harmless."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
sys.path.insert(0, "/home/tyler/barony-ai")
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

def merchant(uid=1):
    S.npc_state.pop(uid, None)
    return S.get_npc_state(uid, "human", "", "shopkeeper", 0, 5)

# --- 1. detection ---------------------------------------------------------------------
for s in ("any chance of a discount?", "that's too expensive", "can you do better price?",
          "I can't afford that", "how about a deal for the lot", "come down a little?"):
    ck("detects: %r" % s, S.wants_to_haggle(s))
for s in ("what do you sell?", "tell me about Hamlet", "who runs this place?",
          "have you seen the Baron?", ""):
    ck("ignores: %r" % s, not S.wants_to_haggle(s))

# --- 2. regard moves on how you talk --------------------------------------------------
m = merchant()
ck("starts neutral", m["regard"] == 0 and m["deal_pct"] == 0)
S.merchant_tone(m, "thank you, that's kind of you")
ck("courtesy warms them", m["regard"] > 0, m["regard"])
S.merchant_tone(m, "this is robbery, you crook")
ck("rudeness costs more than courtesy earns", m["regard"] < 1, m["regard"])
for _ in range(20):
    S.merchant_tone(m, "you thief, this is a rip off")
ck("regard cannot run away downward", m["regard"] == -S.HAGGLE_REGARD_CAP, m["regard"])
for _ in range(40):
    S.merchant_tone(m, "thank you kindly")
ck("nor upward", m["regard"] == S.HAGGLE_REGARD_CAP, m["regard"])

# --- 3. resolution follows regard -----------------------------------------------------
m = merchant(2); m["regard"] = 3
out, deal = S.haggle_attempt(m, 5)
ck("a merchant who likes you gives ground", out == "better" and deal == -S.HAGGLE_STEP, (out, deal))
m = merchant(3); m["regard"] = -3
out, deal = S.haggle_attempt(m, 5)
ck("one who doesn't marks you up", out == "worse" and deal == S.HAGGLE_STEP, (out, deal))
m = merchant(4)
outs = set()
for f in range(400):
    m["haggle_floor"] = None
    m["deal_pct"] = 0
    outs.add(S.haggle_attempt(m, f)[0])
ck("neutral goes either way", outs == {"better", "refused"}, outs)

# --- 4. one attempt per floor ---------------------------------------------------------
m = merchant(5); m["regard"] = 5
S.haggle_attempt(m, 7)
first = m["deal_pct"]
for _ in range(10):
    out, deal = S.haggle_attempt(m, 7)
ck("asking again on the same floor changes nothing", deal == first, (deal, first))
ck("and says so", out == "already", out)
out, deal = S.haggle_attempt(m, 8)
ck("a new floor is a new conversation", out == "better" and deal == first - S.HAGGLE_STEP)

# --- 5. the cap is what keeps it harmless ---------------------------------------------
m = merchant(6); m["regard"] = 6
for f in range(60):
    m["haggle_floor"] = None
    S.haggle_attempt(m, f)
ck("a discount can never exceed the cap", m["deal_pct"] == -S.HAGGLE_CAP, m["deal_pct"])
m = merchant(7); m["regard"] = -6
for f in range(60):
    m["haggle_floor"] = None
    S.haggle_attempt(m, f)
ck("nor can a markup", m["deal_pct"] == S.HAGGLE_CAP, m["deal_pct"])
ck("and the cap is small next to the game's own 3x trading swing", S.HAGGLE_CAP <= 10)

# --- 6. the model is never allowed to invent a figure or repeat itself ----------------
for out in ("better", "worse", "refused", "already"):
    sec = S.haggle_section(out, -1)
    ck("%s: forbids quoting a number" % out, "Do NOT state any number" in sec)
for out in ("better", "worse", "refused"):
    sec = S.haggle_section(out, -1)
    ck("%s: forbids reusing the previous wording" % out, "previous wording" in sec)
    ck("%s: carries a server-chosen angle" % out, "HOW YOU PLAY IT:" in sec, sec[:80])
angles = {S.haggle_section("better", -1).split("HOW YOU PLAY IT: ")[1][:40] for _ in range(80)}
ck("the angle actually varies", len(angles) >= 4, len(angles))

# --- 7. every outcome tells the model what already happened ---------------------------
for out in ("better", "worse", "refused", "already"):
    sec = S.haggle_section(out, -1)
    ck("%s: names the contradicting route" % out, "Do NOT" in sec or "do NOT" in sec, sec[:60])
ck("success forbids refusing", "Do NOT refuse" in S.haggle_section("better", -1))
ck("refusal forbids relenting", "compromise" in S.haggle_section("refused", 0))
ck("an unknown outcome renders nothing", S.haggle_section("", 0) == "")

# --- 8. state survives, and a new run clears it ---------------------------------------
ck("the modifier persists on the merchant", S.npc_state[6]["deal_pct"] == -S.HAGGLE_CAP)
S.reset_run()
ck("a new run forgets every merchant", not S.npc_state)

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
