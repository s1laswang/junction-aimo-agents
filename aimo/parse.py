import json
import os
import re
from datetime import date, timedelta

from aimo.models import Constraint, Need, NeedItem

NUM = r"\d{1,3}(?:[  ,.]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?"

UNITS = {
    "L": r"litres?|liters?|litraa|litran|litroja|litra|ltr|l",
    "kg": r"kilograms?|kilogrammaa|kilos?|kiloa|kilon|kg",
    "g": r"grams?|grammaa|gramma|g",
    "pcs": r"pieces?|pcs|pc|kpl|kappaletta|kappale|units?|pallets?|lavaa|lava|pal",
    "packs": r"packages?|packs?|pkt|pakettia|paketti|paketit|boxes|box|cases?|laatikkoa|laatikko",
    "ml": r"millilitres?|milliliters?|ml",
    "dl": r"decilitres?|deciliters?|dl",
    "cl": r"centilitres?|centiliters?|cl",
    "t": r"tonnes?|tons?|tonnia|tonni",
}
UNIT_RE = "|".join(f"(?P<u_{k}>{v})" for k, v in UNITS.items())
QTY = re.compile(rf"(?P<n>{NUM})\s*(?:(?:{UNIT_RE})(?![a-zäöå]))?", re.I)
TO_BASE = {"ml": ("L", 0.001), "dl": ("L", 0.1), "cl": ("L", 0.01), "t": ("kg", 1000.0)}

WEEKDAYS = [
    r"monday|maanantai", r"tuesday|tiistai", r"wednesday|keskiviikko",
    r"thursday|torstai", r"friday|perjantai", r"saturday|lauantai", r"sunday|sunnuntai",
]
MONTHS = [
    "january|tammikuu", "february|helmikuu", "march|maaliskuu", "april|huhtikuu",
    "may|toukokuu", "june|kesäkuu", "july|heinäkuu", "august|elokuu",
    "september|syyskuu", "october|lokakuu", "november|marraskuu", "december|joulukuu",
]
NEXT_WEEK = r"next\s+week(?:'s)?|ensi\s+viikon|ensi\s+viikolla|ensi\s+viikolle|ensi\s+viikko"

ALLERGENS = {
    "nuts": r"nuts?|peanuts?|pähkinä\w*|maapähkinä\w*",
    "egg": r"eggs?|muna\w*|kananmuna\w*",
    "milk": r"dairy|milk|maito\w*|maido\w*",
    "soy": r"soy\w*|soija\w*",
    "fish": r"fish|kala\w*",
    "celery": r"celery|selleri\w*",
    "mustard": r"mustard|sinappi\w*",
    "sesame": r"sesame|seesami\w*",
    "shellfish": r"shellfish|äyriäis\w*",
    "wheat": r"wheat|vehnä\w*",
}

FI_ALLERGEN_STEMS = {
    "nuts": "pähkinä", "egg": "muna", "milk": "maido", "soy": "soija",
    "fish": "kala", "celery": "selleri", "mustard": "sinappi", "sesame": "seesami",
    "shellfish": "äyriäis", "wheat": "vehnä",
}

BRANDS = {
    "valio": "Valio", "fazer": "Fazer", "atria": "Atria", "arla": "Arla", "hk": "HK",
    "saarioinen": "Saarioinen", "snellman": "Snellman", "paulig": "Paulig",
    "vaasan": "Vaasan", "apetit": "Apetit", "kariniemen": "Kariniemen",
    "elovena": "Elovena", "findus": "Findus", "mccain": "McCain", "felix": "Felix",
    "knorr": "Knorr", "meira": "Meira", "oddlygood": "Oddlygood", "santa maria": "Santa Maria",
    "feelia": "Feelia", "fredman": "Fredman", "tork": "Tork", "duni": "Duni",
}

FI_NAME = {
    "milk": "maito", "cream": "kerma", "butter": "voi", "potatoes": "peruna", "cheese": "juusto",
    "yoghurt": "jogurtti", "egg": "kananmuna", "flour": "jauho", "sugar": "sokeri", "salt": "suola",
    "rice": "riisi", "bread": "leipä", "beef": "naudanliha", "pork": "porsaanliha", "chicken": "kana",
    "salmon": "lohi", "fish": "kala", "meat": "liha", "mince": "jauheliha", "tomato": "tomaatti",
    "carrot": "porkkana", "onion": "sipuli", "apple": "omena", "juice": "mehu", "water": "vesi",
    "coffee": "kahvi", "oil": "öljy", "chocolate": "suklaa", "oat drink": "kaurajuoma",
    "sour cream": "kermaviili", "whipping cream": "vispikerma", "ice cream": "jäätelö",
    "quark": "rahka", "ham": "kinkku", "peanut butter": "maapähkinävoi", "cottage cheese": "raejuusto",
    "soy sauce": "soijakastike", "cucumber": "kurkku", "broccoli": "parsakaali", "banana": "banaani",
    "cauliflower": "kukkakaali", "zucchini": "kesäkurpitsa", "spinach": "pinaatti", "cabbage": "kaali",
    "beetroot": "punajuuri", "garlic": "valkosipuli", "mushroom": "sieni", "pumpkin": "kurpitsa",
    "radish": "retiisi", "parsnip": "palsternakka", "kale": "lehtikaali", "swede": "lanttu",
    "sprouts": "ruusukaali", "celery": "selleri", "fennel": "fenkoli", "eggplant": "munakoiso",
    "asparagus": "parsa", "artichoke": "artisokka", "corn": "maissi", "pea": "herne", "lentil": "linssi",
    "chickpea": "kikherne", "bean": "papu", "pepper": "pippuri", "paprika": "paprika", "chilli": "chili",
    "ginger": "inkivääri", "turmeric": "kurkuma", "cinnamon": "kaneli", "cardamom": "kardemumma",
    "dill": "tilli", "parsley": "persilja", "thyme": "timjami", "rosemary": "rosmariini",
    "oregano": "oregano", "chives": "ruohosipuli", "coriander": "korianteri", "basil": "basilika",
    "pear": "päärynä", "peach": "persikka", "plum": "luumu", "cherry": "kirsikka", "mango": "mango",
    "pineapple": "ananas", "melon": "meloni", "watermelon": "vesimeloni", "mandarin": "mandariini",
    "grapefruit": "greippi", "grape": "viinirypäle", "lemon": "sitruuna", "orange": "appelsiini",
    "avocado": "avokado", "rhubarb": "raparperi", "strawberry": "mansikka", "raspberry": "vadelma",
    "blueberry": "mustikka", "lingonberry": "puolukka", "cranberry": "karpalo",
    "blackcurrant": "mustaherukka", "cloudberry": "lakka", "sea buckthorn": "tyrni",
    "olive": "oliivi", "coconut": "kookos", "almond": "manteli", "hazelnut": "hasselpähkinä",
    "peanut": "maapähkinä", "pistachio": "pistaasi", "cashew": "cashew", "pecan": "pekaanipähkinä",
    "sesame": "seesami", "honey": "hunaja", "jam": "hillo", "marmalade": "marmeladi", "syrup": "siirappi",
    "cocoa": "kaakao", "caramel": "karamelli", "liquorice": "lakritsi", "marzipan": "marsipaani",
    "marshmallow": "vaahtokarkki", "gingerbread": "piparkakku", "cookie": "keksi", "waffle": "vohveli",
    "doughnut": "munkki", "muffin": "muffinssi", "croissant": "croissant", "bagel": "bagel",
    "baguette": "patonki", "ciabatta": "ciabatta", "crispbread": "näkkileipä", "pancake": "pannukakku",
    "porridge": "puuro", "muesli": "mysli", "granola": "granola", "barley": "ohra", "buckwheat": "tattari",
    "semolina": "mannasuurimo", "bulgur": "bulgur", "couscous": "couscous", "quinoa": "kvinoa",
    "yeast": "hiiva", "starch": "tärkkelys", "pasta": "pasta", "spaghetti": "spagetti",
    "macaroni": "makaroni", "penne": "penne", "noodle": "nuudeli", "lasagne": "lasagne", "pizza": "pizza",
    "tortilla": "tortilla", "salad": "salaatti", "lettuce": "salaatti", "soup": "keitto",
    "sauce": "kastike", "ketchup": "ketsuppi", "mayonnaise": "majoneesi", "mustard": "sinappi",
    "dressing": "salaattikastike", "vinegar": "etikka", "salsa": "salsa", "pesto": "pesto",
    "hummus": "hummus", "tofu": "tofu", "margarine": "margariini", "buttermilk": "piimä", "kefir": "kefir",
    "feta": "feta", "halloumi": "halloumi", "gouda": "gouda", "edam": "edam", "emmental": "emmental",
    "brie": "brie", "parmesan": "parmesan", "ricotta": "ricotta", "mozzarella": "mozzarella",
    "mascarpone": "mascarpone", "tuna": "tonnikala", "shrimp": "katkarapu", "cod": "turska",
    "pike-perch": "kuha", "whitefish": "siika", "perch": "ahven", "vendace": "muikku", "herring": "silli",
    "trout": "kirjolohi", "sausage": "makkara", "frankfurter": "nakki", "bacon": "pekoni",
    "salami": "salami", "chorizo": "chorizo", "pepperoni": "pepperoni", "prosciutto": "prosciutto",
    "turkey": "kalkkuna", "lamb": "karitsa", "veal": "vasikka", "reindeer": "poro", "liver": "maksa",
    "meatball": "lihapyörykkä", "nugget": "nugget", "fish finger": "kalapuikko", "burger": "hampurilainen",
    "beer": "olut", "wine": "viini", "lemonade": "limonaadi", "tea": "tee", "chips": "sipsi",
    "tenderloin": "sisäfilee", "entrecote": "entrecote",
}

NOUNS = [
    ("oat drink", r"oat\s+(?:milk|drink)|kaura(?:juoma|maito)\w*"),
    ("sour cream", r"sour\s+cream|kermaviili\w*"),
    ("whipping cream", r"whipping\s+cream|vispikerma\w*|kuohukerma\w*"),
    ("cream cheese", r"cream\s+cheese|tuorejuusto\w*"),
    ("ice cream", r"ice\s*cream|jäätelö\w*"),
    ("milk", r"milk|\bmai[dt]o\w*"),
    ("cream", r"cream|\bkerma\w*"),
    ("butter", r"butter|\bvoi(?:ta|n|lla|hin|ksi)?\b"),
    ("potatoes", r"potato\w*|\bperun\w*"),
    ("cheese", r"cheese|\bjuust\w*"),
    ("yoghurt", r"yogh?urt\w*|\bjogurt\w*|\bjugurt\w*"),
    ("egg", r"\beggs?\b|\bmun(?:a|ia|ien|at)\b|\bkananmun\w*"),
    ("quark", r"quark|\brahka\w*"),
    ("flour", r"flour|\bjauho(?:a|ja|n|t|jen)?\b"),
    ("sugar", r"sugar|\bsokeri(?:a|n|t|ksi)?\b"),
    ("salt", r"\bsalt\b|\bsuola(?:a|n)?\b"),
    ("rice", r"\brice\b|\briisi\w*"),
    ("pasta", r"pasta\w*"),
    ("bread", r"bread|\bleip(?:ä|ää|än|iä)\b"),
    ("mince", r"\bmince\w*|ground\s+(?:beef|meat)|\bjauheliha\w*"),
    ("beef", r"beef|\bnauda\w*|\bnauta\w*"),
    ("pork", r"\bpork\b|\bporsaan\w*|\bpossun\w*"),
    ("chicken", r"chicken|\bkan(?:a|aa|an\w*|oja)\b"),
    ("salmon", r"salmon|\blohi\w*|\blohta\b"),
    ("fish", r"\bfish\b|\bkala(?:a|n|t)?\b"),
    ("ham", r"\bham\b|\bkinkku\w*"),
    ("sausage", r"sausage\w*|\bmakkara\w*"),
    ("meat", r"\bmeat\b|\bliha(?:a|n)?\b"),
    ("tomato", r"tomato\w*|\btomaatt\w*"),
    ("carrot", r"carrot\w*|\bporkkan\w*"),
    ("onion", r"onion\w*|\bsipul\w*"),
    ("apple", r"apple\w*|\bomen\w*"),
    ("orange", r"orange\w*|\bappelsiin\w*"),
    ("banana", r"banana\w*|\bbanaan\w*"),
    ("lemon", r"lemon\w*|\bsitruun\w*"),
    ("cucumber", r"cucumber\w*|\bkurkku\w*"),
    ("cabbage", r"cabbage\w*|\bkaali\w*"),
    ("salad", r"salad\w*|\bsalaatt\w*"),
    ("juice", r"juice\w*|\bmehu\w*"),
    ("water", r"\bwater\b|\bvesi\b|\bvettä\b"),
    ("coffee", r"coffee|\bkahvi\w*"),
    ("tea", r"\btea\b|\btee(?:tä|n)?\b"),
    ("oil", r"\boil\b|\böljy\w*"),
    ("chocolate", r"chocolate|\bsuklaa\w*"),
    ("napkin", r"napkin\w*|\blautasliina\w*"),
    ("soup", r"\bsoup\w*|\bkeitto\w*"),
    ("sauce", r"\bsauce\w*|\bkastike\w*"),
    ("strawberry", r"strawberr\w*|\bmansikk\w*"),
    ("blueberry", r"blueberr\w*|\bmustikk\w*"),
    ("raspberry", r"raspberr\w*|\bvadelma\w*"),
    ("mushroom", r"mushroom\w*|\bsien(?:i|iä|et|ten)\b"),
    ("pepper", r"pepper|\bpippuri\w*"),
]
_SPECIAL_FI = {
    "lamb": r"\bkarits\w*|\blampaan\w*|\blammas\w*", "veal": r"\bvasik\w*", "reindeer": r"\bpor(?:o|oa|on|oja|onlih\w*)\b",
    "cod": r"\btursk\w*", "egg": r"\bmun(?:a|ia|ien|at)\b", "pike-perch": r"\bkuha\w*", "fish finger": r"\bkalapuik\w*",
    "burger": r"\bhampurilais\w*|\bburger\w*", "pepper": r"\bpippur\w*", "chilli": r"\bchili\w*",
}


def _fi_rx(w):
    if len(w) <= 4:
        return rf"\b{w}(?:a|ä|n|ta|tä|lla|llä|ksi|ssa|ssä|hin|en|ja|jä|t)?\b"
    stem = w[:-1] if w[-1] in "aeiouyäö" else w
    stem = re.sub(r"(k|p|t)\1", r"\1{1,2}", stem)
    return rf"\b{stem}\w*"


def _en_rx(w):
    w = w.replace("-", r"[\s-]?").replace(" ", r"\s+")
    return rf"\b{w[:-1]}(?:y|ies)\b" if w.endswith("y") else rf"\b{w}(?:s|es)?\b"


_have = {n for n, _ in NOUNS}
_multi, _single = [], []
for _n, _fi in FI_NAME.items():
    if _n in _have:
        continue
    _p = _en_rx(_n) + "|" + _SPECIAL_FI.get(_n, _fi_rx(_fi))
    (_multi if (" " in _n) else _single).append((_n, _p))
NOUNS = _multi + NOUNS + _single
NOUNS = [(n, re.compile(p, re.I)) for n, p in NOUNS]

STOP = set(
    "i we need needs want would like please of the a an some by for to deliver delivered delivery "
    "on until before and with find me us our get order buy have it that is are be at in from "
    "tarvitsen tarvitsemme haluan haluaisin tilaan tilata ja sekä ennen mennessä toimitus toimita "
    "meille minulle noin tai approx approximately about around circa ca asap että kiitos option options requirements meets".split()
)


def _num(s):
    s = s.replace(" ", " ").strip()
    if re.fullmatch(r"\d{1,3}(?:[ ,.]\d{3})+", s):
        return float(re.sub(r"[ ,.]", "", s))
    m = re.fullmatch(r"(\d{1,3}(?:[ ,.]\d{3})+)[.,](\d{1,2})", s)
    if m:
        return float(re.sub(r"[ ,.]", "", m.group(1)) + "." + m.group(2))
    return float(s.replace(",", "."))


def _fix_year(d, today):
    return d if d >= today else d.replace(year=d.year + 1)


def _find_date(text, today):
    t = text
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), t[: m.start()] + " " + t[m.end():]
        except ValueError:
            pass
    mon = "|".join(MONTHS).replace("|", "|")
    for idx, names in enumerate(MONTHS):
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\.?\s*(?:of\s+)?(?:{names})\w*(?:\s+(\d{{4}}))?", t, re.I)
        if m:
            try:
                y = int(m[2]) if m[2] else today.year
                d = date(y, idx + 1, int(m[1]))
                return (d if m[2] else _fix_year(d, today)), t[: m.start()] + " " + t[m.end():]
            except ValueError:
                pass
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})?", t)
    if not m:
        m = re.search(r"(?:\bby|\buntil|\bon|\bennen|\bmennessä)\s+(\d{1,2})\.(\d{1,2})(?![\d.]|\s*[a-zäö])", t, re.I)
    if m:
        try:
            y = int(m[3]) if m.lastindex and m.lastindex >= 3 and m[3] else today.year
            d = date(y, int(m[2]), int(m[1]))
            return (d if y != today.year or (m.lastindex and m.lastindex >= 3 and m[3]) else _fix_year(d, today)), t[: m.start()] + " " + t[m.end():]
        except ValueError:
            pass
    m = re.search(r"\basap\b|\bas\s+soon\s+as\s+possible\b|\bmahdollisimman\s+pian\b|\bheti\b|\burgent\b", t, re.I)
    if m:
        return today + timedelta(1), t[: m.start()] + " " + t[m.end():]
    m = re.search(r"\b(?:by\s+)?(?:the\s+)?end\s+of\s+(?:the\s+)?week\b|\bviikon\s+loppuun(?:\s+mennessä)?", t, re.I)
    if m:
        return today + timedelta((4 - today.weekday()) % 7 or 7), t[: m.start()] + " " + t[m.end():]
    m = re.search(r"\bday after tomorrow\b|\bylihuomen\w*", t, re.I)
    if m:
        return today + timedelta(2), t[: m.start()] + " " + t[m.end():]
    m = re.search(r"\btomorrow\b|\bhuomen\w*|\bhuomis\w*", t, re.I)
    if m:
        return today + timedelta(1), t[: m.start()] + " " + t[m.end():]
    m = re.search(r"\btoday\b|\btänään\b", t, re.I)
    if m:
        return today, t[: m.start()] + " " + t[m.end():]
    m = re.search(r"\bin\s+(\d+)\s+days?\b|\b(\d+)\s+päivän\s+päästä|\b(\d+)\s+päivässä", t, re.I)
    if m:
        n = int(next(g for g in m.groups() if g))
        return today + timedelta(n), t[: m.start()] + " " + t[m.end():]
    nw = re.search(NEXT_WEEK, t, re.I)
    for i, names in enumerate(WEEKDAYS):
        m = re.search(rf"\b(?:{names})\w*", t, re.I)
        if m:
            if nw:
                start = today - timedelta(today.weekday()) + timedelta(7)
                d = start + timedelta(i)
                end = max(m.end(), nw.end())
                lo = min(m.start(), nw.start())
                return d, t[:lo] + " " + t[end:]
            ahead = (i - today.weekday()) % 7 or 7
            return today + timedelta(ahead), t[: m.start()] + " " + t[m.end():]
    if nw:
        return today + timedelta(7), t[: nw.start()] + " " + t[nw.end():]
    return None, t


def _find_price(text):
    pat = re.compile(
        r"(?:under|below|less\s+than|max(?:imum)?|at\s+most|up\s+to|alle|enintään|korkeintaan|<)\s*"
        rf"(?:€\s*)?(?P<p>\d+(?:[.,]\d+)?)\s*(?:eur(?:os?|oa)?|€|e\b)?"
        r"(?:\s*(?:/|per|a)\s*(?:kg|l|litre|liter|kilo|litra|kpl|pcs|unit|piece)\b)?",
        re.I,
    )
    m = pat.search(text)
    if m and re.search(r"eur|€|e\b|euro", m.group(0), re.I):
        return float(m["p"].replace(",", ".")), text[: m.start()] + " " + text[m.end():]
    return None, text


def _constraints(seg):
    c = Constraint()
    p, seg = _find_price(seg)
    c.max_unit_price = p
    pats = [
        ("lactose_free", r"lactos?e?[\s-]*fr?e{1,2}\b|\blactose[\s-]*intoleran\w*|\blaktoosi[\s-]*intoleran\w*|\bno\s+lactose|\blaktoos\w*?t(?:on|om)\w*"),
        ("gluten_free", r"glut[eo]n[\s-]*fr?e{1,2}\b|\bno\s+gluten|\bgluteen\w*?t(?:on|om)\w*"),
    ]
    for key, pat in pats:
        if re.search(pat, seg, re.I):
            setattr(c, key, True)
            seg = re.sub(pat, " ", seg, flags=re.I)
    for name, stems in ALLERGENS.items():
        en = rf"(?:\bno|\bwithout|\bfree\s+(?:of|from)|\bilman)\s+(?:{stems})|\b(?:{stems})[\s-]*free\b|\b(?:{stems})[\s-]*allerg\w*|\ballergic\s+to\s+(?:{stems})"
        fi_stem = FI_ALLERGEN_STEMS[name]
        fi = rf"\b{fi_stem}\w*?t(?:on|om|ön|öm)\w*|\bmaidot\w*" if name == "milk" else rf"\b{fi_stem}\w*?t(?:on|om|ön|öm)\w*"
        if name == "nuts":
            fi = r"\bpähkinät(?:ön|töm)\w*|\bmaapähkinät(?:ön|töm)\w*"
        if name == "milk":
            fi += r"|\bmai[dt]o\w*?allerg\w*|\bmaidoton\w*"
        elif name in FI_ALLERGEN_STEMS:
            fi += rf"|\b{FI_ALLERGEN_STEMS[name]}\w*?allerg\w*"
        for pat in (en, fi):
            if re.search(pat, seg, re.I):
                if name not in c.exclude_allergens:
                    c.exclude_allergens.append(name)
                seg = re.sub(pat, " ", seg, flags=re.I)
    if re.search(r"\borganic\b|\bluomu\w*|\bbio\b|\bekologinen\w*", seg, re.I):
        c.labels.append("organic")
        seg = re.sub(r"\borganic\b|\bluomu\w*|\bbio\b|\bekologinen\w*", " ", seg, flags=re.I)
    org = r"\bfinnish\b|\bfrom\s+finland\b|\bmade\s+in\s+finland\b|\bkotimai\w*|\bsuomalai\w*|\bsuomesta\b|\bdomestic\b"
    if re.search(org, seg, re.I):
        c.origin = "fi"
        seg = re.sub(org, " ", seg, flags=re.I)
    low = seg.lower()
    for key, name in BRANDS.items():
        m = re.search(rf"\b{key}\w*", low)
        if m:
            c.brands.append(name)
            seg = seg[: m.start()] + " " + seg[m.end():]
            low = seg.lower()
    return c, seg


def _noun(seg):
    for name, rx in NOUNS:
        if rx.search(seg):
            return name
    words = [w for w in re.findall(r"[a-zäöå]+", seg.lower()) if w not in STOP and len(w) > 1]
    return " ".join(words[:2]) or None


def _nouns(seg):
    out = []
    for piece in re.split(r"\band\b|\bja\b|\bsekä\b|[,&]", seg, flags=re.I):
        for name, rx in NOUNS:
            if rx.search(piece):
                if name not in out:
                    out.append(name)
                break
    return out


def _unit(m):
    for k in UNITS:
        if m.group(f"u_{k}"):
            return k
    return None


def _merge(c, g, allergens_only=False):
    for a in g.exclude_allergens:
        if a not in c.exclude_allergens:
            c.exclude_allergens.append(a)
    if allergens_only:
        return
    for l in g.labels:
        if l not in c.labels:
            c.labels.append(l)
    for b in g.brands:
        if b not in c.brands:
            c.brands.append(b)
    c.origin = c.origin or g.origin
    c.lactose_free = c.lactose_free or g.lactose_free
    c.gluten_free = c.gluten_free or g.gluten_free
    if c.max_unit_price is None:
        c.max_unit_price = g.max_unit_price


def _rule_parse(text, customer, today):
    d, rest = _find_date(text, today)
    deliver = d or today + timedelta(2)
    price, rest = _find_price(rest)
    rest = re.sub(r"\b(?:a|an|one|yksi)\s+(?=(?:pallets?|tonnes?|tons?|lava)\b)", "1 ", rest, flags=re.I)
    full_c, _ = _constraints(rest)
    tail_c = None
    while True:
        commas = list(re.finditer(r",\s+", rest))
        if not commas:
            break
        last = commas[-1]
        tail = rest[last.end():]
        if QTY.search(tail):
            break
        tc, cl = _constraints(tail)
        if _noun(cl) is not None:
            break
        if tail_c is None:
            tail_c = tc
        else:
            _merge(tail_c, tc)
        rest = rest[: last.start()]
    rest_all = rest
    matches = []
    for m in QTY.finditer(rest):
        if m.start() > 0 and rest[m.start() - 1].isalpha():
            continue
        matches.append(m)
    segs = []
    prev_end = 0
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        segs.append((m, rest[m.end():end], rest[prev_end:m.start()]))
        prev_end = end
    suffix = bool(segs) and bool(_nouns(_constraints(segs[0][2])[1]))
    if suffix:
        segs = [(m, a_, rest[(matches[i - 1].end() if i else 0):m.start()]) for i, (m, a_, _) in enumerate(segs)]
    items = []
    single = len(segs) <= 1

    def build(c0):
        c = c0.model_copy(deep=True)
        c.max_unit_price = c.max_unit_price if c.max_unit_price is not None else price
        _merge(c, full_c, allergens_only=True)
        if tail_c:
            _merge(c, tail_c)
        return c

    for m, after, before in segs:
        n = _num(m["n"])
        u = _unit(m)
        if u is None:
            u = "pcs"
        elif u in TO_BASE:
            u, f = TO_BASE[u]
            n = round(n * f, 6)
        own = before if suffix else after
        c0, cleaned_own = _constraints(rest_all if single else own)
        _, cleaned_after = _constraints(after)
        _, cleaned_before = _constraints(before)
        names = _nouns(cleaned_before if suffix else cleaned_after) or _nouns(cleaned_after if suffix else cleaned_before)
        if not names:
            q = _noun(cleaned_own) or _noun(_constraints(rest_all)[1])
            names = [q] if q else []
        for q in names:
            items.append(NeedItem(query=q, quantity=n, unit=u, constraints=build(c0)))
    if not items:
        c0, cleaned = _constraints(rest_all)
        names = _nouns(cleaned) or [_noun(cleaned) or text.strip()[:60] or "product"]
        items = [NeedItem(query=q, quantity=1.0, unit="pcs", constraints=build(c0)) for q in names]
    return Need(customer=customer, deliver_by=deliver, items=items, raw_text=text)


def _llm_parse(text, customer, today):
    try:
        from agents import llm

        if not llm.available():
            return None
        raw = llm.chat(
            "Extract a purchasing need. Reply with only JSON: {\"deliver_by\": \"YYYY-MM-DD\", \"items\": "
            "[{\"query\": short English noun, \"quantity\": number, \"unit\": \"L|kg|g|pcs|packs\", "
            "\"constraints\": {\"lactose_free\": bool|null, \"gluten_free\": bool|null, "
            "\"exclude_allergens\": [str], \"labels\": [str], \"origin\": str|null, "
            "\"max_unit_price\": number|null, \"brands\": [str]}}]}. "
            "Allergen exclusions apply to every item. Today is " + today.isoformat() + ".",
            text,
        )
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
        data = json.loads(raw)
        data["customer"] = customer
        data["raw_text"] = text
        need = Need.model_validate(data)
        if not need.items or any(i.quantity <= 0 or not i.query.strip() or i.unit not in ("L", "kg", "g", "pcs", "packs") for i in need.items):
            return None
        allergens = {a for i in need.items for a in i.constraints.exclude_allergens}
        for i in need.items:
            i.constraints.exclude_allergens = sorted(allergens)
        return need
    except Exception:
        return None


def deliver_date(text, today):
    d, _ = _find_date(text, today)
    return d or today + timedelta(2)


def text_constraints(text):
    return _constraints(text)[0]


def _menu_parse(text, customer, today):
    from aimo import recipes

    if not recipes._dishes_in(text):
        return None
    c = text_constraints(text)
    c.max_unit_price = None
    need = recipes.menu_to_need(text, customer, deliver_by=deliver_date(text, today), today=today, constraints=c)
    return need if need.items else None


def parse_need(text: str, customer: str, today: date | None = None) -> Need:
    today = today or date.today()
    return _menu_parse(text, customer, today) or _llm_parse(text, customer, today) or _rule_parse(text, customer, today)


FI_ALLERGEN = {
    "nuts": "pähkinä", "egg": "muna", "milk": "maito", "soy": "soija", "fish": "kala",
    "celery": "selleri", "mustard": "sinappi", "sesame": "seesami", "shellfish": "äyriäiset", "wheat": "vehnä",
}
FI_LABEL = {"organic": "luomu"}
DAYS = {"en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "fi": ["ma", "ti", "ke", "to", "pe", "la", "su"]}
MON_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def describe_need(need: Need, lang: str = "en") -> str:
    fi = lang == "fi"
    parts = []
    for it in need.items:
        c = it.constraints
        q = f"{it.quantity:g} {it.unit} " + (FI_NAME.get(it.query, it.query) if fi else it.query)
        tags = []
        if c.lactose_free:
            tags.append("laktoositon" if fi else "lactose-free")
        if c.gluten_free:
            tags.append("gluteeniton" if fi else "gluten-free")
        for a in c.exclude_allergens:
            tags.append(f"ilman: {FI_ALLERGEN.get(a, a)}" if fi else f"no {a}")
        for l in c.labels:
            tags.append(FI_LABEL.get(l, l) if fi else l)
        if c.origin:
            tags.append("kotimainen" if fi and c.origin == "fi" else ("Finnish" if c.origin == "fi" else c.origin))
        if c.brands:
            tags.append(", ".join(c.brands))
        if c.max_unit_price is not None:
            tags.append(f"max {c.max_unit_price:g} EUR")
        parts.append(", ".join([q] + tags))
    d = need.deliver_by
    wd = DAYS["fi" if fi else "en"][d.weekday()]
    when = f"toimitus {wd} {d.day}.{d.month}.{d.year}" if fi else f"deliver by {wd} {d.day} {MON_EN[d.month - 1]} {d.year}"
    return "; ".join(parts) + ", " + when if len(parts) == 1 else "; ".join(parts) + "; " + when
