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

# --- 4. favours cost more ----------------------------------------------------------------
S.reset_run()
def solves(st):
    for _ in range(400):
        S.GUARD_STATE["sokoban"] = False
        if S.favour_roll("sokoban", st): return True
    return False
ck("a mercenary will not do a favour cheaply",
   not solves(merc(10, f=S.SOKOBAN_MIN_FRIENDSHIP)))
ck("a loyal one will", solves(loyal(11, f=S.SOKOBAN_MIN_FRIENDSHIP)))
ck("but the mercenary will once you are worth it",
   solves(merc(12, f=S.SOKOBAN_MIN_FRIENDSHIP + S.MERC_FAVOUR_TAX)))

print("\n%d failure(s)" % len(fails), fails)
sys.exit(1 if fails else 0)
