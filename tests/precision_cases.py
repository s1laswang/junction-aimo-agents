BAD_DAIRY = r"chocolate|suklaa|yogh?urt|jogurt|oat|kaura|soy|soija|powder|jauhe|ice cream|jäätelö"

PREPARED = r"soup|keitto|sauce|kastike|salad|salaatti|pizza|chips|crisp|pie|piirakka|cake|kakku|juice|mehu"

SET1 = [
    ("I need 1,000 litres of lactose-free milk by Monday", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("Tarvitsen 1000 litraa laktoositonta maitoa maanantaiksi", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("200 kg of Finnish potatoes by Friday", [(r"potato|peruna", r"salad|salaatti|chips|fries|ranskal|mash|muusi|dumpling")]),
    ("50 l whipping cream tomorrow, organic", [(r"cream|kerma", r"sour|kermaviili|ice|jäätelö|cheese|juusto|sauce|kastike|soup|keitto")]),
    ("20 kg Valio butter", [(r"butter|voi\b", r"cookie|keksi|bun|pulla|sauce|kastike|croissant|peanut|maapähkinä")]),
    ("100 kg gluten-free pasta", [(r"pasta", r"sauce|kastike|salad|salaatti|lasagne|kana|chicken|liha|meat")]),
    ("500 eggs next week", [(r"\begg|muna", r"plant|salad|salaatti|noodle|nuudeli|mayo|majoneesi")]),
    ("30 kg lactose-free cheese", [(r"cheese|juusto|luostari", r"cream cheese|tuorejuusto|sauce|kastike|pizza|burger|mac &")]),
    ("200 l laktoositonta maitoa huomenna, pähkinätön", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("Tarvitsen 50 kg gluteenitonta jauhoa", [(r"flour|jauho", r"mix|seos|pizza|bread|leipä|pulla")]),
    ("40 kg kanaa, gluteeniton", [(r"chicken|kana", r"soup|keitto|sauce|kastike|egg|muna|sandwich|leipä")]),
    ("100 l oat drink no soy", [(r"oat|kaura", r"soy|soija|ice|jäätelö|yogh?urt|jogurt|porridge|puuro")]),
    ("20 kg rahkaa", [(r"quark|rahka", r"cake|kakku|pie|piirakka")]),
    ("300 l milk for school lunch under 1.5 EUR", [(r"\bmilk\b|maito", BAD_DAIRY)]),
    ("100 kg carrots", [(r"carrot|porkkana", r"salad|salaatti|cake|kakku|juice|mehu|soup|keitto")]),
    ("80 kg rice", [(r"\brice\b|riisi", r"pudding|puuro|cake|kakku|noodle|milk|maito|crisp|waffle")]),
    ("Tilaan 60 kg perunaa ja 40 l maitoa maanantaiksi", [(r"potato|peruna", r"salad|salaatti|chips|fries|ranskal"), (r"\bmilk\b|maito", BAD_DAIRY)]),
    ("30 kg lohta huomenna", [(r"salmon|lohi", r"sauce|kastike|salad|salaatti|soup|keitto|pasta")]),
    ("20 kg organic apples", [(r"apple|omena", r"juice|mehu|pie|piirakka|sauce|sose|jam|hillo|cider|siideri|bar|cinnamon|rawbite")]),
    ("100 kg tomatoes by Wednesday", [(r"tomato|tomaatti", r"sauce|kastike|soup|keitto|ketchup|paste|purée|sose|pizza")]),
]

HOLDOUT = [
    ("150 kg onions for Tuesday", [(r"onion|sipuli", PREPARED + r"|ring|rengas|fried|paistettu|pickle|marin")]),
    ("Tarvitsen 25 kg voita", [(r"butter|voi\b|voi ", r"cookie|keksi|bun|pulla|peanut|maapähkinä|sauce|kastike|croissant")]),
    ("60 kg minced beef", [(r"beef|nauta|jauheliha|mince", r"patty|pihvi|pizza|pasta|soup|keitto|sauce|kastike|liver|maksa")]),
    ("300 eggs for Friday", [(r"\begg|muna", r"plant|salad|salaatti|noodle|nuudeli|mayo|majoneesi|chocolate|suklaa|pasta|plant")]),
    ("100 l lactose-free cream", [(r"cream|kerma", r"sour|kermaviili|ice|jäätelö|cheese|juusto|sauce|kastike|soup|keitto|coconut|kookos")]),
    ("40 kg cucumbers", [(r"cucumber|kurkku", r"pickle|suolakurkku|hapan|relish|salad|salaatti|sauce|kastike")]),
    ("Tarvitsen 80 kg kaurahiutaleita", [(r"oat|kaura", r"bar|patukka|cookie|keksi|biscuit|drink|juoma|chocolate|suklaa")]),
    ("50 kg lactose free yoghurt", [(r"yogh?urt|jogurt", r"ice|jäätelö|oat|soy|kaura|soija|chocolate|suklaa|cake|kakku")]),
    ("70 kg broccoli", [(r"broccoli|parsakaali", PREPARED)]),
    ("120 kg bananas", [(r"banana|banaani", r"chips|bar|patukka|bread|leipä|cake|kakku|milk|maito|yogh?urt|jogurt|smoothie|" + PREPARED)]),
]

HOLDOUT2 = [
    ("Sairaalakeittiö tarvitsee 80 l laktoositonta maitoa, pähkinätön", [(r"\bmilk\b|maito", r"chocolate|suklaa|yogh?urt|jogurt|oat|kaura|soy|soija|powder|jauhe|ice cream|jäätelö")]),
    ("School kitchen: 120 kg carrots and 90 kg potatoes by Thursday", [(r"carrot|porkkana", r"salad|salaatti|cake|kakku|juice|mehu|soup|keitto|mash|muusi"), (r"potato|peruna", r"salad|salaatti|chips|fries|ranskal|mash|muusi")]),
    ("Koulukeittiölle 60 kg riisiä", [(r"\brice\b|riisi", r"pudding|puuro|cake|kakku|noodle|milk|maito|crisp|waffle|vinegar|etikka")]),
    ("40 kg gluten-free oats for the hospital, no nuts", [(r"oat|kaura", r"bar|patukka|cookie|keksi|biscuit|drink|juoma|chocolate|suklaa")]),
    ("Tarvitsen 30 kg kananmunia perjantaiksi", [(r"\begg|muna", r"plant|salad|salaatti|noodle|nuudeli|mayo|majoneesi|chocolate|suklaa|pasta")]),
    ("25 l lactose free milk and 10 kg lactose free butter", [(r"\bmilk\b|maito", r"chocolate|suklaa|yogh?urt|jogurt|oat|kaura|soy|soija|powder|jauhe|ice cream|jäätelö"), (r"butter|voi\b|voi ", r"cookie|keksi|bun|pulla|peanut|maapähkinä|sauce|kastike|croissant|veitsi")]),
    ("Tilaan 50 kg lohta ja 20 kg tilliä", [(r"salmon|lohi", r"sauce|kastike|salad|salaatti|soup|keitto|pasta|veitsi|knife"), (r"dill|tilli", r"pickle|kurkku|sauce|kastike|salad|salaatti|mustard|sinappi")]),
    ("100 kg tomatoes, hospital, allergen free (no gluten)", [(r"tomato|tomaatti", r"sauce|kastike|soup|keitto|ketchup|paste|purée|sose|pizza")]),
    ("Koulu tarvitsee 200 l maitoa ja 50 kg juustoa huomenna", [(r"\bmilk\b|maito", r"chocolate|suklaa|yogh?urt|jogurt|oat|kaura|soy|soija|powder|jauhe|ice cream|jäätelö"), (r"cheese|juusto", r"cream cheese|tuorejuusto|sauce|kastike|pizza|burger|mac &|sämpylä|bun")]),
    ("45 kg pork for Monday", [(r"pork|possu|sika", r"sauce|kastike|soup|keitto|pizza|pie|piirakka|burger|dog|sausage|makkara")]),
]


MILK = BAD_DAIRY

HOLDOUT3 = [
    ("Hotel breakfast: 90 kg oat flakes and 60 l orange juice for Monday", [(r"oat|kaura", r"bar|patukka|cookie|keksi|biscuit|drink|juoma|chocolate|suklaa"), (r"juice|mehu", r"bar|jelly|hyytel|candy|karkki|alcohol|cider|siideri|ice|jäätelö")]),
    ("Tarvitsen 35 kg kirjolohifileetä torstaiksi", [(r"trout|kirjolohi|salmon|lohi", r"sauce|kastike|salad|salaatti|soup|keitto|veitsi|knife")]),
    ("Hospital ward: 150 l lactose-free milk and 40 kg gluten-free bread, no nuts", [(r"\bmilk\b|maito", MILK), (r"bread|leipä", r"crumb|korppu|crisp|knäcke|cake|kakku|mix|seos")]),
    ("Koulun keittiöön 120 kg porkkanaa ja 80 kg kaalia", [(r"carrot|porkkana", r"salad|salaatti|cake|kakku|juice|mehu|soup|keitto|mash|muusi"), (r"cabbage|kaali", r"parsa|kukka|broc|cauli|rosen|ruusu|salad|salaatti|soup|keitto|pie|piirakka|roll|rulla")]),
    ("50 kg cheddar for the hotel buffet", [(r"cheddar", r"sauce|kastike|dip|jalape|burger|pizza|mix|seos|snack|chips")]),
    ("100 kg pears by Thursday", [(r"pear|päärynä", r"juice|mehu|jam|hillo|cake|kakku|cider|siideri|bar|patukka|sose|puree|purée")]),
    ("Tilaan 40 l ruokakermaa ja 20 kg voita huomenna", [(r"cream|kerma", r"sour|kermaviili|ice|jäätelö|cheese|juusto|coconut|kookos|soup|keitto|sauce|kastike"), (r"butter|voi\b|voi ", r"cookie|keksi|bun|pulla|peanut|maapähkinä|sauce|kastike|croissant|veitsi|knife")]),
    ("60 kg frozen peas for school lunch", [(r"pea|herne", r"soup|keitto|salad|salaatti|mash|puree|purée|pate|pasta|snack|wasabi|sugar|sokeri")]),
    ("20 kg cottage cheese for the hospital", [(r"cottage|raejuusto", r"sauce|kastike|dip|salad|salaatti|pasta|pizza|pie|piirakka")]),
    ("Lasagne and salad bar for 200 guests, lactose-free, for Friday", "menu", {
        "lactose-free milk": (r"\bmilk\b|maito", MILK),
        "ground beef": (r"beef|nauta|jauheliha|mince", r"patty|pihvi|pizza|pasta|soup|keitto|sauce|kastike|liver|maksa"),
        "iceberg lettuce": (r"lettuce|salaatti|jäävuori|iceberg", r"dressing|kastike|sauce|chips|pizza"),
    }),
    ("Karjalanpaisti 150 hengelle perjantaiksi", "menu", {
        "pork shoulder": (r"pork|possu|sika|porsa", r"sauce|kastike|soup|keitto|pizza|burger|sausage|makkara|pie|piirakka"),
        "beef cubes": (r"beef|nauta", r"patty|pihvi|burger|soup|keitto|sauce|kastike|pizza|liver|maksa"),
        "onion": (r"onion|sipuli", r"ring|rengas|fried|paistettu|crispy|soup|keitto|pickle|marin|salad|salaatti"),
    }),
    ("Pancakes and oat porridge for 80 guests, school, gluten-free", "menu", {
        "gluten-free flour mix": (r"flour|jauho", r"pizza|bread|leipä|pulla|pasta|cake|kakku"),
        "gluten-free oats": (r"oat|kaura", r"bar|patukka|cookie|keksi|biscuit|drink|juoma|chocolate|suklaa"),
    }),
    ("Kahvilaan 25 kg kahvia", [(r"coffee|kahvi", r"cup|muki|filter|suodatin|capsule|kapseli|drink|juoma|cake|kakku|syrup|siirappi|liqueur|energy|cream|kerma")]),
    ("300 kg of flour for the bakery", [(r"flour|jauho", r"mix|seos|pizza|bread|leipä|pulla|cake|kakku")]),
    ("80 l soy drink, school, no nuts", [(r"soy|soija", r"yogh?urt|jogurt|ice|jäätelö|dessert|jälkiruoka|chocolate|suklaa|coffee|kahvi")]),
]
