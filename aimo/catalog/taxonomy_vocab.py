"""Controlled product-type vocabulary (~150 types) for the 17,546-product catalog.

Each type: (family, short description used in the Gemini prompt). Families group types for
"related type" suggestions; the planner hard-filters on the exact type set mapped from the need.
"""
from __future__ import annotations

TYPES: dict[str, tuple[str, str]] = {
    # ---- dairy
    "milk_drink": ("dairy", "plain cow's milk or milk drink (any fat %, incl. lactose-free, UHT, ESL), unflavoured"),
    "flavoured_milk": ("dairy", "chocolate/cocoa/strawberry/vanilla milk drinks"),
    "milk_coffee_drink": ("dairy", "ready-to-drink coffee with milk, latte, iced coffee drinks"),
    "sour_milk": ("dairy", "sour milk, buttermilk, piimä, filmjölk, AB/Gefilus sour milk"),
    "kefir_drinkable_yoghurt": ("dairy", "kefir, drinkable yoghurt, yoghurt drinks, smoothie-type dairy drinks"),
    "yoghurt": ("dairy", "spoonable yoghurt (plain or flavoured), viili"),
    "quark": ("dairy", "quark, rahka, skyr"),
    "cream": ("dairy", "cooking cream, whipping cream, double cream (dairy)"),
    "coffee_cream": ("dairy", "coffee cream / coffee milk / creamer (dairy), incl. portion creamers"),
    "sour_cream": ("dairy", "sour cream, creme fraiche, smetana, ranskankerma, turkish yoghurt-style cooking cream"),
    "butter": ("dairy", "butter, butter-oil blends, clarified butter, ghee"),
    "margarine_spread": ("fats", "margarine, vegetable fat spreads, baking margarine"),
    "cheese_hard": ("dairy", "hard and semi-hard cheese (Edam, Emmental, cheddar, parmesan, Oltermanni), blocks/slices/grated"),
    "cheese_soft": ("dairy", "soft, fresh and specialty cheese: brie, feta, mozzarella, halloumi, blue, goat, bread cheese"),
    "cream_cheese": ("dairy", "cream cheese, cottage cheese, processed/spreadable cheese, cheese sauces as spread"),
    "milk_powder_condensed": ("dairy", "milk powder, condensed or evaporated milk"),
    "dairy_dessert": ("dairy", "puddings, dairy desserts, mousse, rice pudding"),
    "ice_cream": ("frozen_dessert", "ice cream, soft ice mix, sorbet, ice lollies"),
    # ---- plant-based alternatives
    "oat_drink": ("plant_based", "oat drink / oat milk alternative (incl. barista, flavoured)"),
    "plant_drink_other": ("plant_based", "soy, almond, rice, coconut, pea drinks as milk alternatives"),
    "plant_yoghurt": ("plant_based", "plant-based yoghurt/quark alternatives (oat, soy, coconut gurt)"),
    "plant_cream": ("plant_based", "plant-based cooking/whipping cream alternatives, oat cream"),
    "plant_cheese": ("plant_based", "plant-based cheese alternatives"),
    "plant_protein": ("plant_based", "tofu, tempeh, seitan, pulled oats, soy/pea protein mince, plant burgers/nuggets"),
    # ---- eggs
    "eggs": ("eggs", "fresh shell eggs"),
    "egg_products": ("eggs", "liquid egg, egg white/yolk, boiled peeled eggs, egg powder"),
    # ---- bakery
    "bread_loaf": ("bakery", "bread loaves and sliced bread (rye, wheat, mixed, gluten-free loaves), fresh or frozen"),
    "bread_rolls": ("bakery", "bread rolls, buns for burgers/hot dogs, baguettes, small breads, rieska, karelian pies excluded"),
    "flatbread_tortilla": ("bakery", "tortillas, wraps, pita, naan, flatbreads, lavash"),
    "crispbread": ("bakery", "crispbread, näkkileipä, crackers, rice cakes, knäckebröd"),
    "toast_rusk": ("bakery", "toast bread for toasting, rusks, croutons excluded"),
    "sweet_pastry": ("bakery", "sweet buns, cinnamon rolls, doughnuts, croissants, danish pastries, muffins"),
    "cake_dessert_bakery": ("bakery", "cakes, tarts, cheesecakes, brownies, baked desserts"),
    "biscuits_cookies": ("bakery", "biscuits, cookies, wafers"),
    "savoury_pastry": ("bakery", "savoury pies and pastries, karelian pies, meat pies, quiche, samosas, sausage rolls"),
    "pizza": ("bakery", "pizza, pizza bases"),
    "dough": ("bakery", "raw or frozen dough, puff pastry, pie crusts, bake-off raw products"),
    "flour": ("baking", "flour and meal (wheat, rye, oat, gluten-free flour), semolina, starch"),
    "bread_mix": ("baking", "bread/cake/baking mixes (flour mixes for bread, pancake mix, muffin mix)"),
    "baking_ingredients": ("baking", "yeast, baking powder, gelatine, baking chocolate, sprinkles, marzipan, vanilla sugar"),
    "breadcrumbs_croutons": ("baking", "breadcrumbs, panko, croutons, bread cubes"),
    # ---- cereals & dry goods
    "breakfast_cereal": ("dry_goods", "muesli, granola, cornflakes, breakfast cereals"),
    "oat_flakes_porridge": ("dry_goods", "oat flakes, porridge oats and other cereal flakes, instant porridge"),
    "rice": ("dry_goods", "rice of any kind (dry or precooked)"),
    "pasta": ("dry_goods", "pasta (dry or fresh, incl. gluten-free pasta), lasagne sheets, gnocchi"),
    "noodles": ("dry_goods", "noodles (egg, rice, glass, udon)"),
    "grains_other": ("dry_goods", "bulgur, couscous, quinoa, barley, buckwheat, millet, polenta"),
    "legumes": ("dry_goods", "beans, lentils, chickpeas, peas (dry or canned/cooked)"),
    "nuts_seeds": ("dry_goods", "nuts, seeds, nut butters, peanuts, coconut flakes"),
    "dried_fruit": ("dry_goods", "raisins, dried fruit and berries"),
    # ---- meat
    "beef": ("meat", "fresh or frozen beef cuts, steaks, roasts"),
    "pork": ("meat", "fresh or frozen pork cuts"),
    "poultry": ("meat", "chicken, turkey, duck: raw cuts, fillets, whole birds"),
    "lamb_game": ("meat", "lamb, mutton, reindeer, elk, venison, other game"),
    "minced_meat": ("meat", "minced/ground meat of any animal"),
    "sausage": ("meat", "sausages, frankfurters, hot dogs, grill sausages, chorizo for cooking"),
    "cold_cuts": ("meat", "sliced cold cuts: ham, salami, roast beef slices, turkey slices, meat spreads, pâté"),
    "bacon": ("meat", "bacon, bacon cubes, pancetta"),
    "meat_prepared": ("meat", "cooked/prepared meat products: meatballs, kebab meat, pulled pork, cooked chicken, schnitzels"),
    # ---- fish & seafood
    "fish": ("fish", "fresh or frozen fish, fillets, portions (raw)"),
    "fish_processed": ("fish", "smoked, salted, pickled or canned fish, fish fingers, fish cakes, herring in sauce, roe"),
    "seafood": ("fish", "shrimp, prawns, crab, lobster, mussels, squid, octopus, other shellfish and molluscs"),
    # ---- produce
    "vegetables_fresh": ("produce", "fresh vegetables (incl. cut/peeled fresh), root vegetables except potatoes"),
    "potatoes": ("produce", "fresh potatoes"),
    "salad_leaves": ("produce", "fresh lettuce, salad leaves, spinach leaves, sprouts, cress"),
    "herbs_fresh": ("produce", "fresh herbs and edible flowers"),
    "mushrooms": ("produce", "fresh or frozen mushrooms"),
    "fruit_fresh": ("produce", "fresh fruit"),
    "berries_fresh": ("produce", "fresh berries"),
    "vegetables_frozen": ("frozen", "frozen vegetables, vegetable mixes, frozen potato products excluded"),
    "potato_products": ("frozen", "fries, potato wedges, mashed potato, potato gratin, precooked potato, potato flakes"),
    "fruit_berries_frozen": ("frozen", "frozen fruit and berries, purées"),
    "vegetables_preserved": ("preserved", "canned/jarred vegetables, tomatoes (canned, passata, purée), olives, pickles, sauerkraut"),
    "fruit_preserved": ("preserved", "canned fruit, fruit in syrup, compote, fruit soup (kiisseli)"),
    # ---- prepared foods
    "ready_meal": ("prepared", "ready meals, casseroles, lasagne, frozen meals, meal components"),
    "soup": ("prepared", "soups (ready, frozen, powder), soup bases"),
    "salad_prepared": ("prepared", "prepared salads (potato salad, pasta salad, coleslaw), salad toppings"),
    "sandwich_snack_meal": ("prepared", "sandwiches, wraps filled, sushi, filled baguettes, snack meals"),
    "baby_food": ("prepared", "baby food, infant formula, baby porridge"),
    # ---- condiments & cooking
    "sauce": ("condiments", "cooking sauces, pasta sauces, gravy, curry sauce, hollandaise, bechamel, wok sauces, soy sauce"),
    "dressing_mayonnaise": ("condiments", "mayonnaise, aioli, salad dressings, remoulade"),
    "ketchup_mustard": ("condiments", "ketchup, mustard, hot sauce, chili sauce, barbecue sauce, relish"),
    "spices": ("condiments", "dried herbs, spices, spice mixes, pepper, seasoning"),
    "salt": ("condiments", "salt"),
    "sugar_sweetener": ("condiments", "sugar, syrup for baking, sweeteners"),
    "oil_cooking_fat": ("fats", "cooking oils, frying fat, olive oil, rapeseed oil, coconut fat"),
    "vinegar": ("condiments", "vinegar"),
    "stock_bouillon": ("condiments", "stock, bouillon cubes, fond, broth concentrates"),
    "jam_marmalade": ("condiments", "jam, marmalade, fruit spreads, berry purées for spreading"),
    "honey_syrup": ("condiments", "honey, maple syrup, dessert syrups and toppings"),
    "sweet_spread": ("condiments", "chocolate spreads, caramel spreads"),
    "pickles_chutney": ("condiments", "chutneys, relish, pickled condiments, capers"),
    # ---- snacks & sweets
    "chocolate": ("snacks", "chocolate bars, tablets, pralines, chocolate confectionery"),
    "candy": ("snacks", "candy, sweets, liquorice, chewing gum, pastilles"),
    "savoury_snacks": ("snacks", "crisps, chips, popcorn, pretzels, nachos, snack nuts mixes"),
    "snack_bar": ("snacks", "cereal bars, protein bars, granola bars"),
    # ---- beverages
    "juice_100": ("beverages", "100 % fruit or vegetable juice (not from concentrate or from concentrate), no added sugar"),
    "juice_drink": ("beverages", "juice drinks, nectars, fruit drinks with added water/sugar"),
    "juice_concentrate": ("beverages", "juice/berry concentrates and squash to dilute (mehu-tiiviste), cordials"),
    "smoothie": ("beverages", "fruit smoothies (non-dairy or mainly fruit)"),
    "soft_drink": ("beverages", "carbonated soft drinks, colas, lemonades, tonic, mixers"),
    "energy_sports_drink": ("beverages", "energy drinks, sports drinks"),
    "water": ("beverages", "still or sparkling water, flavoured water, mineral water"),
    "coffee": ("beverages", "coffee beans, ground coffee, instant coffee, coffee capsules/pads"),
    "tea": ("beverages", "tea, herbal tea, iced tea concentrate"),
    "cocoa_drink": ("beverages", "cocoa powder for drinks, hot chocolate powder"),
    "beer": ("beverages", "beer, including low/no alcohol beer"),
    "cider_long_drink": ("beverages", "cider, long drinks, hard seltzers (incl. 0 %)"),
    "wine": ("beverages", "wine, sparkling wine, alcohol-free wine"),
    "spirits": ("beverages", "spirits and liqueurs"),
    # ---- other food
    "protein_supplement": ("other_food", "protein powders, clinical nutrition drinks, sports nutrition"),
    "food_other": ("other_food", "food that fits no other type"),
    # ---- non-food
    "pet_food": ("nonfood", "pet food and treats"),
    "kitchen_equipment": ("nonfood", "pans, pots, knives, utensils, GN containers, appliances, thermometers"),
    "tableware": ("nonfood", "plates, glasses, cups, cutlery, jugs, serving ware (durable)"),
    "disposables": ("nonfood", "disposable cups, plates, cutlery, takeaway boxes, lids, straws, food containers"),
    "packaging_film_bags": ("nonfood", "cling film, foil, baking paper, bags, vacuum bags, wrapping"),
    "paper_napkins": ("nonfood", "napkins, paper towels, toilet paper, table covers, tissue, dispensers for them"),
    "cleaning": ("nonfood", "detergents, dishwasher products, cleaners, rinse aid, disinfectants, machine cleaners"),
    "personal_hygiene": ("nonfood", "hand soap, hand cream, shampoo, toothpaste, gloves for hygiene, first aid"),
    "office_supplies": ("nonfood", "office paper, pens, tape, labels, receipt rolls"),
    "nonfood_other": ("nonfood", "any other non-food item: candles, decorations, batteries, gas, newspapers"),
}

FOOD_FAMILIES = {f for f, _ in TYPES.values()} - {"nonfood"}
EU14_KEYS = ["gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk", "nuts", "celery", "mustard",
             "sesame", "sulphites", "lupin", "molluscs"]


def family(t: str | None) -> str | None:
    return TYPES.get(t or "", (None, ""))[0]


def related_types(t: str) -> list[str]:
    f = family(t)
    return [k for k, (fam, _) in TYPES.items() if fam == f and k != t]


def vocab_prompt() -> str:
    return "\n".join(f"- {k}: {d}" for k, (_f, d) in TYPES.items())
