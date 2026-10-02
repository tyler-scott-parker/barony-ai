"""The HTTP handler, driven with payloads shaped EXACTLY as the C++ builds them.

Every other suite calls the prompt builders directly, which is how a bug that lived entirely in
the handler survived 17 green suites: the skill remark's payload carried "tier" TWICE (the remark
tier as an int, then the skill tier as a string), json.loads kept the string, and the int() in the
log line raised after every generation. These payload strings are copied from mymod.cpp's
snprintf formats -- raw strings, so duplicate keys reach the service exactly as the game sends
them. Ollama is stubbed; nothing here needs a GPU or a running service.
"""
import os, sys, json, glob, random, tempfile, threading, socketserver, urllib.request, urllib.error
_tmp = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
os.environ["BARONY_AI_TESTHOOK"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

# --- the model stub: whatever the test says it answers next ----------------------------------
MODEL = {"speech": "Aye.", "action": "NONE", "raise": False}
PROMPTS = []
def fake_ollama(prompt):
    PROMPTS.append(prompt)
    if MODEL["raise"]:
        raise RuntimeError("ollama fell over")
    if "Respond ONLY with JSON" in prompt:
        return json.dumps({"speech": MODEL["speech"], "action": MODEL["action"]})
    return MODEL["speech"]
S.ask_ollama = fake_ollama

def last_prompt():
    return PROMPTS[-1] if PROMPTS else ""

class _Srv(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
srv = _Srv(("127.0.0.1", 0), S.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def post(raw):
    req = urllib.request.Request("http://127.0.0.1:%d/" % srv.server_address[1],
                                 data=raw.encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def head(uid, says="", race="gnome", floor=7, gold=100, persist=0, origin="", origin_key="",
         mino=0):
    """mymod_payloadHead, field for field (says last, as the engine now appends it)."""
    return ('"race":"%s","floor":%d,"map":"Swamp","uid":%d,"player":0,"player_name":"Ada",'
            '"origin":"%s","origin_key":"%s","gold":%d,"player_kind":"","follower_name":"",'
            '"persist":%d,"mino_timer":%d,"says":"%s"'
            % (race, floor, uid, origin, origin_key, gold, persist, mino, says))

def recruit(uid, race="gnome", floor=7, persist=0, origin="", origin_key=""):
    """mymod_recordEventAbout's recruitment payload."""
    return post('{"event":"recruitment","race":"%s","floor":%d,"uid":%d,"player":0,'
                '"origin":"%s","origin_key":"%s","about":0,"persist":%d}'
                % (race, floor, uid, origin, origin_key, persist))

def force(uid, **kw):
    return post("{" + head(uid) + ',"force":%s' % json.dumps(kw) + "}")

def log_records(kind):
    path = sorted(glob.glob(os.path.join(_tmp, "session-*.jsonl")))[-1]
    with open(path, encoding="utf-8") as fh:
        return [r for r in map(json.loads, fh) if r.get("kind") == kind]

class rolls_succeed:
    """Every random.random() roll inside the block lands -- boons, secrets, guards."""
    def __enter__(self):
        self._r = S.random.random
        S.random.random = lambda: 0.0
    def __exit__(self, *a):
        S.random.random = self._r

IDENT = (',"party":1,"identify":{"category":"ring","real":"ring of conflict","unid":"gold ring",'
         '"decoys":["ring of warning","ring of strength"],"value":%d}')
ASK = "what is this? can you tell me what I'm carrying?"

# --- 1. the skill remark ----------------------------------------------------------------------
S.reset_run(); MODEL.update(speech="You swing that axe like you mean it.", action="NONE")
recruit(995)
st, js = post("{" + head(995) + ',"remark":"skillbest","tier":2,"skill":"Axes","skilltier":"an expert"}')
ck("a skill remark is answered (200)", st == 200, (st, js))
ck("...with the follower's line, not an error", js.get("reply", "").startswith("You swing"), js)
ck("...and the skill tier reached the prompt", "They are an expert with Axes." in last_prompt())
# A binary built before the rename still sends "tier" twice; json.loads keeps the string.
st, js = post("{" + head(995) + ',"remark":"skillworst","tier":2,"skill":"Stealth","tier":"a novice"}')
ck("the OLD duplicate-key payload no longer 500s", st == 200, (st, js))
ck("...and still reads the skill tier", "They are a novice at Stealth." in last_prompt())

# --- 2. the remark tier in the log ------------------------------------------------------------
post("{" + head(995) + ',"remark":"mimic","tier":0}')
post("{" + head(995) + ',"remark":"drunk","tier":1}')
tiers = {r["what"]: r.get("tier") for r in log_records("remark") if r["what"] in ("mimic", "drunk")}
ck("a REACTIVE remark logs tier 0, not -1", tiers.get("mimic") == 0, tiers)
ck("an EVENT remark logs tier 1", tiers.get("drunk") == 1, tiers)
ck("_as_int: 0 is a value", S._as_int(0) == 0 and S._as_int("0") == 0)
ck("_as_int: a word is the default, not a crash", S._as_int("an expert") == -1)

# --- 3. an exception is never spoken ----------------------------------------------------------
MODEL["raise"] = True
st, js = post("{" + head(995, says="hello") + ',"party":1}')
MODEL["raise"] = False
ck("a service exception is a 500", st == 500, st)
ck("...whose reply is EMPTY (the engine reads reply as speech)", js.get("reply") == "", js)
ck("...and whose error is in its own field", "ollama fell over" in js.get("error", ""), js)

# --- 4. no order the player did not give ------------------------------------------------------
S.reset_run(); recruit(41, race="skeleton"); force(41, introduced=True)
for says, model, want in (("What is your name?", "WAIT", "NONE"),
                          ("Do you know this floor?", "WAIT", "NONE"),
                          ("Wait, how can I even understand you?", "WAIT", "NONE"),
                          ("I'm going to close the game out here.", "DEFEND", "NONE"),
                          ("Follow me, my friend.", "FOLLOW", "FOLLOW"),
                          ("stay here and guard the door", "DEFEND", "DEFEND"),
                          ("follow me", "WAIT", "NONE"),        # a refusal is NONE, never WAIT
                          ("kill that goblin", "ATTACK", "ATTACK")):
    MODEL.update(speech="Hm.", action=model)
    st, js = post("{" + head(41, says=says, race="skeleton") + ',"party":1}')
    ck("%-40r model %-6s -> %s" % (says, model, want), js.get("action") == want, js.get("action"))
rec = [r for r in log_records("reply") if r.get("says") == "What is your name?"]
ck("a suppressed action is logged as model_action", rec and rec[-1].get("model_action") == "WAIT",
   rec[-1:] if rec else None)
MODEL.update(speech="Aye.", action="NONE")

# --- 5. the mercenary's appraisal -------------------------------------------------------------
S.reset_run(); recruit(501, race="human")
force(501, allegiance="self_interested", introduced=True)
st, js = post("{" + head(501, says=ASK, race="human", gold=500) + IDENT % 200 + "}")
ck("the first /aiidentify quotes", js.get("quote", "").startswith("identify:") and not js.get("charge"),
   js)
st, js = post("{" + head(501, says="ok, lets go", race="human", gold=500) + ',"party":1}')
ck("an 'ok' in ordinary chat does NOT settle it", not js.get("charge"), js)
ck("...and nothing was pretended to be appraised", "THEY HAVE AGREED YOUR PRICE" not in last_prompt())
st, js = post("{" + head(501, says=ASK, race="human", gold=500) + IDENT % 900 + "}")
ck("asking about a DIFFERENT item is a new quote, not a charge", js.get("quote") and not js.get("charge"),
   js)
st, js = post("{" + head(501, says=ASK, race="human", gold=500) + IDENT % 900 + "}")
ck("asking again about the same item hires him -- charged ONCE", js.get("charge") == str(
   S.merc_price("identify", 7, 900)), js)
ck("...and the item is appraised on that turn", "THEY ARE ASKING YOU TO IDENTIFY" in last_prompt())

# --- 6. a turn that already belongs to something else rolls no boon ----------------------------
S.reset_run(); recruit(601)
force(601, allegiance="loyal", friendship=80, trust=60, introduced=True)
with rolls_succeed():
    st, js = post("{" + head(601, says=ASK) + IDENT % 200 + "}")
ck("no boon is delivered on an /aiidentify turn", not js.get("boon"), js.get("boon"))
ck("...and the floor's boon slot was not spent", S.follower_state[601].get("last_boon_floor") != 7)
with rolls_succeed():
    st, js = post("{" + head(601, says="anything to report?") + ',"party":1}')
ck("an ordinary turn still gets its boon", bool(js.get("boon")), js)
S.reset_run(); recruit(602)
force(602, allegiance="loyal", friendship=40)  # fought a lot, never spoken to
with rolls_succeed():
    st, js = post("{" + head(602, says="hello there") + ',"party":1}')
ck("the introduction turn delivers no boon", not js.get("boon"), js.get("boon"))
ck("...and spends no once-per-run latch", not S.BOON_STATE["good_used"] and not S.BOON_STATE["map_used"])
S.reset_run(); recruit(701, race="human")
force(701, allegiance="self_interested", introduced=True, curiosity=50, agreed={"identify": 25})
with rolls_succeed():
    st, js = post("{" + head(701, says=ASK, race="human", gold=999) + IDENT % 200 + "}")
ck("a merc's map pitch does not take an /aiidentify turn",
   "you will tell them what you found" not in last_prompt() and not js.get("quote"), js.get("quote"))

# --- 7. identity on the first request of a floor ----------------------------------------------
for first in ("remark", "favour", "conversation"):
    S.reset_run()
    recruit(100, race="skeleton", floor=4, persist=3, origin="summon", origin_key="skeleton knight")
    knight = S.follower_state[100]
    knight.update(friendship=30, name="Rattlebones")
    if first == "remark":
        post("{" + head(250, race="skeleton", floor=5, persist=3, origin="summon",
                        origin_key="skeleton knight") + ',"remark":"biome","tier":2}')
    elif first == "favour":       # mymod_favourFetch, as it now sends identity too
        post('{"favour":"minotaur","uid":250,"race":"skeleton","floor":5,"player":0,"map":"",'
             '"gold":0,"gold_here":0,"origin":"summon","origin_key":"skeleton knight","persist":3}')
    else:
        post("{" + head(250, says="hello", race="skeleton", floor=5, persist=3, origin="summon",
                        origin_key="skeleton knight") + ',"party":1}')
    ck("a summon whose first request on a new floor is a %s keeps its row" % first,
       S.follower_state.get(250) is knight, S.follower_state.get(250, {}).get("allegiance"))

S.reset_run(); recruit(100, race="human", floor=6, persist=4)
row = S.follower_state[100]; row.update(allegiance="loyal", trust=60, friendship=40, name="Odo")
with rolls_succeed():
    st, js = post('{"favour":"minotaur","uid":300,"race":"human","floor":7,"player":0,"map":"",'
                  '"gold":100,"gold_here":0,"origin":"","origin_key":"","persist":4}')
ck("an ordinary follower asked to guard on arrival is still itself", S.follower_state.get(300) is row)
ck("...so a trusted one actually heads the minotaur off", js.get("act") == "1", js)

# --- 8. boss secrets: timing and the informant -------------------------------------------------
S.reset_run()
st8 = S.get_follower_state(441, "human", 0, persist=7)
st8.update(friendship=70, introduced=True, name="Odo")
st8["event_log"] = [{"type": "fought_alongside", "floor": 1, "claim": "", "importance": "notable"}
                    for _ in range(8)]
ck("Herx's secret is offerable on floor 19", bool(S.secret_eligible("herx", st8, "human", 19)))
ck("...but not on 20, where initLich has already run", not S.secret_eligible("herx", st8, "human", 20))
ck("the twins' is offerable on 34", bool(S.secret_eligible("twins", st8, "human", 34)))
ck("...but not on 35, where they have already spawned", not S.secret_eligible("twins", st8, "human", 35))
S.SECRET_STATE["herx"]["pending"][441] = (0, False)
S.herx_detect(441, "", "The miners' picks broke his left side and the bone never knit.", 0)
ck("a reveal records the informant's persist token", S.SECRET_STATE["herx"]["persist"] == 7,
   S.SECRET_STATE["herx"])
S._req_set(persist=7, floor=12)
moved = S.get_follower_state(9100, "human", 0, persist=7)       # eight staircases later
ck("...which is still what the engine is handed after the uid changes",
   S.secret_informant_field() == "7" and moved is st8, S.secret_informant_field())
ck("the informant is still reminded what it told, under its new uid",
   "YOU HAVE ALREADY TOLD THIS ADVENTURER" in S._secret_section(9100, st8, "human", "hi", 12))
st, js = post("{" + head(9100, says="hello", race="human", floor=12, persist=7) + ',"party":1}')
ck("the reply carries the informant field beside the secret", js.get("informant") == "7"
   and js.get("secret", "").startswith("1:"), js)

# --- 9. a minotaur deal struck after the minotaur arrived --------------------------------------
for mino, want_charge in ((1, True), (0, False)):
    S.reset_run(); recruit(801, race="human")
    force(801, allegiance="self_interested", introduced=True)
    S.follower_state[801]["quote"] = {"kind": "minotaur", "price": 275, "floor": 7}
    st, js = post("{" + head(801, says="yes, do it", race="human", gold=999, mino=mino) + ',"party":1}')
    if want_charge:
        ck("'yes' while the countdown runs hires him", js.get("charge") == "275"
           and js.get("favour") == "minotaur", js)
    else:
        ck("'yes' after the countdown is gone charges nothing", not js.get("charge")
           and not js.get("favour"), js)
        ck("...and retires the quote", "quote" not in S.follower_state[801])

# --- 10. small things ------------------------------------------------------------------------
m = {"regard": 0}
S.merchant_tone(m, "you're a lifesaver")
ck("a compliment is not rudeness", m["regard"] >= 0, m)
S.merchant_tone(m, "you're a thief")
ck("an insult still is", m["regard"] < 0, m)
ck("friendship 20 is not 'genuinely trust and value'", "trust" not in S.friendship_descriptor(20))
ck("friendship 60 keeps the wording the benches measured",
   S.friendship_descriptor(60) == "You have come to genuinely trust and value this adventurer.")

srv.shutdown()
print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
