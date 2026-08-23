"""The mercenary (self_interested): worse at generosity, better at commerce."""
import os, sys, tempfile
_tmp = tempfile.mkdtemp()
os.environ["BARONY_AI_LOGDIR"] = _tmp
os.environ["BARONY_AI_NAMEHIST"] = os.path.join(_tmp, "n.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import service as S

fails = []
def ck(label, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else "   " + str(extra)))
    if not cond: fails.append(label)

def merc(uid=1, f=60, trust=60):
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0)
    st.update(allegiance="self_interested", friendship=f, trust=trust)
    return st
def loyal(uid=2, f=60, trust=60):
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0)
    st.update(allegiance="loyal", friendship=f, trust=trust)
    return st

# --- 1. they do not give things away ----------------------------------------------------
S.reset_run()
kinds = {}
st = merc(); st["friendship"] = 60
for i in range(3000):
    st["last_boon_floor"] = None
    r = S.boon_roll(st, 3, 0)
    if r: kinds[r[0]] = kinds.get(r[0], 0) + 1
tot = sum(kinds.values())
ck("their boons are mostly information", kinds.get("info", 0) / tot > 0.8, kinds)
# ⚠ They would sell the good item, not gift it. This is the constraint that keeps them from
# simply being a worse loyal follower AND from touching item progression.
ck("they never hand over the good item", "traps" not in kinds and
   S.BOON_STATE["good_used"] == set(), kinds)
S.reset_run()
st = loyal(); st["friendship"] = 60
lk = {}
for i in range(3000):
    st["last_boon_floor"] = None
    r = S.boon_roll(st, 3, 0)
    if r: lk[r[0]] = lk.get(r[0], 0) + 1
ck("a loyal follower still gives goods", lk.get("item", 0) / sum(lk.values()) > 0.2, lk)

# --- 2. but they know what things are worth ---------------------------------------------
def acc(st, cat, n=4000):
    good = 0
    for _ in range(n):
        k, _c, _t = S.identify_attempt(st, "human", cat, "real", ["fake1", "fake2"])
        if k == "correct": good += 1
    return good / n
# "ring" is a category a human is NOT expert in -- which is where handling goods for a living
# actually shows. On "weapon" a human is already an expert and the bonus just hits the 0.95 cap.
m, l = acc(merc(3), "ring"), acc(loyal(4), "ring")
ck("outside their species' expertise, a mercenary is far better",
   m > l + 0.15, (round(m, 2), round(l, 2)))
me, le = acc(merc(5), "weapon"), acc(loyal(6), "weapon")
ck("inside it, they are still better but the cap bites",
   me > le and me <= 0.96, (round(me, 2), round(le, 2)))
ck("but never infallible", m < 0.98 and me < 0.98, (round(m, 2), round(me, 2)))

# --- 3. they talk merchants down ---------------------------------------------------------
def deal_after(escort, rounds=3):
    shop = {"regard": 3, "deal_pct": 0, "haggle_floor": None}
    for f in range(rounds):
        shop["haggle_floor"] = None
        S.haggle_attempt(shop, f, escort)
    return shop["deal_pct"]
d_merc = deal_after({"allegiance": "self_interested"})
d_loyal = deal_after({"allegiance": "loyal"})
d_none = deal_after(None)
ck("a mercenary gets a better price", d_merc < d_loyal, (d_merc, d_loyal))
ck("a loyal companion changes nothing", d_loyal == d_none, (d_loyal, d_none))
ck("and the cap still holds", d_merc >= -S.HAGGLE_CAP, d_merc)

# --- 4. favours are PAID FOR, not earned --------------------------------------------------
# ⚠ Gold replaces the relationship gate for a mercenary; it does not stack on top of it. The
# old MERC_FAVOUR_TAX made him need you to like him MORE than a loyal follower does, which
# made him a worse loyal follower rather than a different deal.
S.reset_run()
def outcome(st, gold=0, says="", gold_here=0, floor=6):
    S.GUARD_STATE["sokoban"] = False
    st.pop("quote", None); st.pop("agreed", None)
    return S.favour_roll("sokoban", st, gold=gold, gold_here=gold_here, floor=floor, says=says)

def solves(st, **kw):
    for _ in range(400):
        if outcome(st, **kw) in (True, "paid"): return True
    return False

ck("a mercenary quotes instead of just doing it",
   outcome(merc(10, f=80), gold=9999) == "quote")
ck("a loyal one at friendship 20 just does it",
   solves(loyal(11, f=S.SOKOBAN_MIN_FRIENDSHIP)))
# The headline: no relationship at all, but the money is right.
ck("a mercenary at friendship 0 works for money",
   outcome(merc(12, f=0), gold=9999, says="yes, do it") == "paid")
ck("and turns you down flat when you cannot pay",
   outcome(merc(13, f=80), gold=0, says="yes, do it") == "broke")
ck("a quote does not spend the once-per-run favour",
   outcome(merc(14, f=80), gold=9999) == "quote" and not S.GUARD_STATE["sokoban"])

# Sokoban is a CUT of the gold in the room, not a flat fee.
ck("sokoban is priced as a cut of what he recovers",
   S.merc_price("sokoban", 6, gold_here=2000) == 500)
ck("with a floor under a thin room",
   S.merc_price("sokoban", 6, gold_here=10) == S.MERC_RATES["sokoban"]["base"])
ck("the flat services scale with depth",
   S.merc_price("identify", 15) > S.merc_price("identify", 1))

# --- 5. accepting, declining and the number the model must never say ----------------------
S.reset_run()
st = merc(20, f=40)
S.merc_quote(st, "minotaur", 8)
ck("saying no clears the offer",
   "TURNED DOWN" in S._merc_section(20, st, 8, "no, too expensive", gold=9999)
   and "quote" not in st)
S.merc_quote(st, "minotaur", 8)
ck("'no deal' is a refusal, not a deal", S.agrees_to_quote("no deal") is False)
ck("saying nothing relevant leaves it on the table",
   S._merc_section(20, st, 8, "what is down here?", gold=9999) == "" and "quote" in st)
_sec = S._merc_section(20, st, 8, "fine, do it", gold=9999)
ck("agreeing hires him", "THEY HAVE AGREED YOUR PRICE" in _sec)
ck("and stamps the fee for the engine", S.LAST_CHARGE.get(20) == S.merc_price("minotaur", 8))
ck("and tells the engine to perform the job", S.LAST_FAVOUR.get(20) == "minotaur")
ck("every mercenary block forbids naming a figure",
   all("Naming any number is WRONG" in b for b in
       (S.merc_quote_section("identify"), S.merc_paid_section("identify"),
        S.merc_broke_section("identify"), S.merc_declined_section("identify"))))
S.merc_quote(st, "minotaur", 8)
ck("a quote does not survive leaving the floor",
   S._merc_section(20, st, 12, "yes", gold=9999) == "" and "quote" not in st)

# --- 6. the fee latches must not leak into the next run -----------------------------------
# ⚠ Same class as the LAST_SABOTAGE leak: stamped while the prompt is built, popped only after
# ask_ollama returns -- so a generation that raises strands it. This one takes the player's money.
S.LAST_CHARGE[999] = 250; S.LAST_FAVOUR[999] = "sokoban"; S.LAST_QUOTE[999] = "identify:50"
S.reset_run()
ck("a stranded fee does not survive new_run",
   not S.LAST_CHARGE and not S.LAST_FAVOUR and not S.LAST_QUOTE)

# --- 7. identify: asking again IS the acceptance -------------------------------------------
# ⚠ Deliberately different from the two favours. The player typed /aiidentify, saw the price,
# and typed it again -- that keeps the whole exchange to one turn each way and needs no
# cross-turn item state. The quote turn simply leaves the item unidentified, which it would be
# anyway, so nothing is consumed by asking.
S.reset_run()
st = merc(30, f=0)
ck("he has not settled an identify rate yet", not S.merc_settled(st, "identify"))
_p = S.merc_quote(st, "identify", 10)
ck("the identify rate scales with depth", _p == 25 + 5 * 10, _p)
ck("a quote is remembered so the re-ask can be read as consent",
   st["quote"]["kind"] == "identify")
S.merc_agree(st, "identify", _p)
ck("once agreed he simply bills", S.merc_settled(st, "identify") and "quote" not in st)

# --- 8. every service has a way to report the finished job ----------------------------------
# ⚠ Without these the conversation path had no idea what a finished job sounds like --
# build_guard_prompt's wording is not in the conversation prompt at all. Measured: 6/6 stamped
# the fee correctly while only ~3/6 reported the work, the rest drifting into travelogue.
ck("every priced service says how to report it",
   all(k in S.MERC_JOB_DONE for k in S.MERC_RATES))
ck("and every one has a name for the job", all(k in S.MERC_JOB_WORDS for k in S.MERC_RATES))
# ⚠ The refusal has to forbid the AGREEABLE opener by name. With only a general prohibition the
# engine refused correctly 3/3 but the speech agreed 2/3 -- so the player read a yes and then
# watched nothing happen. Naming the openers took it to 5/5.
ck("the refusal forbids sounding like agreement",
   "Alright" in S.merc_broke_section("minotaur"))

# --- 9. he SELLS the map rather than giving it ---------------------------------------------
# ⚠ The map boon could not fire for a mercenary at all before this: the self_interested branch
# in boon_roll returned before the map check, so his best product was unsellable.
S.reset_run()
def merc_map(uid, f=20, cur=60):
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, "human", 0)
    st.update(allegiance="self_interested", friendship=f, curiosity=cur, name="Skarn")
    return st

st = merc_map(40)
got = None
for _ in range(400):
    st.pop("last_boon_floor", None)
    r = S.boon_roll(st, 9, 0)
    if r and r[0] == "map_offer": got = r; break
ck("a mercenary can be offered the map sale at all", got is not None)

# ⚠ The sale is checked BEFORE the friendship gate, because it is a transaction rather than
# generosity -- everything else in boon_roll is a gift and is rightly friendship-gated.
st = merc_map(41, f=0)
sold = False
for _ in range(400):
    st.pop("last_boon_floor", None)
    r = S.boon_roll(st, 9, 0)
    if r and r[0] == "map_offer": sold = True; break
ck("and at friendship 0, because it is a sale and not a gift", sold)
ck("curiosity is still required -- it is the capability gate",
   all(S.boon_roll(merc_map(42, f=0, cur=10), 9, 0) != ("map_offer", "") for _ in range(200)))

# ⚠ A declined offer must NOT burn the player's one map for the run.
S.reset_run()
st = merc_map(43)
for _ in range(400):
    st.pop("last_boon_floor", None)
    S.MERC_LATE.pop(43, None)
    S._boon_section(43, st, "human", 9, 0)
    if 43 in S.MERC_LATE: break
ck("pitching does not spend the once-per-run map", 0 not in S.BOON_STATE["map_used"])
ck("but it does put a price on the table", st.get("quote", {}).get("kind") == "map")
ck("and the price is right", st["quote"]["price"] == S.merc_price("map", 9))
ck("the pitch is emitted LATE, not in the boon slot", 43 in S.MERC_LATE)
S.MERC_LATE.pop(43, None)
_sec = S._merc_section(43, st, 9, "alright, deal", gold=9999)
ck("agreeing spends it now", 0 in S.BOON_STATE["map_used"])
ck("and delivers down the ordinary boon channel", S.LAST_BOON.get(43) == "map:")
ck("and charges the fee", S.LAST_CHARGE.get(43) == S.merc_price("map", 9))
ck("the delivery is emitted late too", 43 in S.MERC_LATE)

# ⚠ Both map blocks are knowledge claims about a place, which LIMITS_HEADER forbids at length.
# They must carve themselves out of the limits explicitly, the way _secret_override does.
ck("the late blocks name themselves an exception to the limits",
   "EXCEPTION TO THE LIMITS" in S.MERC_LATE[43])
# ⚠ Both hand over concrete content. Asked to invent the offer itself the model ignored the
# block entirely -- 0/6 at friendship 0 and 0/6 at friendship 30, even placed last.
ck("the pitch angles are server-chosen", len(S.MAP_PITCH_ANGLES) >= 4)
ck("so are the delivery angles", len(S.MAP_DELIVERED_ANGLES) >= 4)
S.MERC_LATE.clear()

# Cannot pay -> refused, and the map is NOT spent.
S.reset_run()
st = merc_map(44)
S.merc_quote(st, "map", 9)
_b = S._merc_section(44, st, 9, "yes", gold=0)
ck("too poor for the map means no map", "CANNOT PAY" in _b
   and 44 not in S.LAST_BOON and 0 not in S.BOON_STATE["map_used"])

# --- 10. the introduction: what this follower is FOR ---------------------------------------
# The mercenary states his terms because that is what a mercenary does; once he is doing it the
# same beat introduces the whole follower system, for every allegiance.
S.reset_run()
def intro(alleg, race="human", origin="", uid=60):
    S.follower_state.pop(uid, None)
    st = S.get_follower_state(uid, race, 0)
    st.update(allegiance=alleg, origin=origin, friendship=0)
    return S._introduction_section(uid, st, race), st

_loyal, _ = intro("loyal")
_spy, _   = intro("spy")
# ⚠ THE ONE THAT MATTERS. A spy is disguised as an ordinary recruit; an introduction keyed on
# the true allegiance would hand the player the single thing the system exists to hide.
ck("a spy's introduction is byte-identical to a loyal one", _loyal == _spy)
ck("a mercenary's is not", intro("self_interested")[0] != _loyal)
ck("a fearful one's is not", intro("fearful")[0] != _loyal)

# ⚠ Every line must be TRUE of this follower. An introduction that promises something the game
# then refuses reads as the system being broken, not as the character declining.
ck("a loyal follower promises appraisal only LATER, matching the identify gate",
   "once you know each other better" in _loyal)
ck("a mercenary promises it now, because money replaces that gate",
   "put a name to anything" in intro("self_interested")[0])
ck("a paid mercenary then does NOT refuse on acquaintance",
   S.identify_attempt(dict(allegiance="self_interested", trust=0, friendship=0),
                      "human", "potion", "potion of healing", ["x"])[0] != "refuse")
ck("while a loyal stranger still does",
   S.identify_attempt(dict(allegiance="loyal", trust=0, friendship=0),
                      "human", "potion", "potion of healing", ["x"])[0] == "refuse")
ck("a trap-capable race offers traps", "mechanisms" in intro("loyal", "gnome")[0])
ck("and one that cannot does not", "mechanisms" not in _loyal)
# ⚠ An emplacement promising to follow is a lie the game contradicts on the next order.
_bot, _ = intro("machine", "sentrybot", origin="bot")
ck("a turret never promises to move", "go where they point" not in _bot)
ck("it promises to hold ground instead", "hold whatever ground" in _bot)

# Once only, and it asks the engine for the command hints.
_txt, _st = intro("loyal", uid=61)
ck("the introduction fires", bool(_txt))
ck("and asks the engine to print the commands", S.LAST_HINT.get(61) == "1")
ck("and never fires twice", S._introduction_section(61, _st, "human") == "")
S.LAST_HINT.clear()
ck("the hint latch is per-run", not S.LAST_HINT)

# ⚠ Placement: measured five blocks from the end it stated 2.5 of 4 services and ignored the
# "do not open with your life story" line outright; in the LAST slot, 3.0+.
_st2 = S.get_follower_state(62, "human", 0); _st2.update(allegiance="loyal", friendship=0)
S._follower_sections(62, "human", 3, "who are you?", 0)
ck("the introduction is emitted through the LATE channel", 62 in S.MERC_LATE)
ck("and it forbids the backstory opener by name",
   "Do NOT open with your name, your life story" in S.MERC_LATE[62])
S.MERC_LATE.clear()

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
