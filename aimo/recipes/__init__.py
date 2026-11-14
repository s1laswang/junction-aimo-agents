"""Menu -> recipe -> order. Owner: port.

Ported from Momin's junction26-valio (`aimo/recipes.py` and `data_extra/recipes.json`, branch dev),
with credit. His dish data (11 dishes, illustrative per-cover quantities, en/fi aliases) and his
allergen swap rules are reused verbatim (`recipes.json`). What changed for our contract:
  - quantities become our `Need` objects (one per ingredient, unit L / KG / PCS) and are planned by
    OUR catalog (`aimo.catalog.plan_need`: search, live offers, good/better/best, split);
  - swap triggers read our customers' policy exclusions (trust YAML `allergen_block.excluded`), so a
    milk-allergy school gets the dairy-free option, not "lactose-free" milk (which still contains milk);
  - all offers for the whole recipe come from ONE batched price + availability call (polite to the mock API).

Public API:
    list_recipes() -> list[dict]
    get_recipe(recipe_id) -> dict | None
    recipe_needs(recipe_id|text, portions, delivery_by, customer_id, ...) -> RecipeNeeds
    plan_recipe(recipe_id|text, portions, delivery_by, customer_id, ...) -> dict (JSON-ready)
"""
from aimo.recipes.planner import (DATA_NOTE, RecipeError, get_recipe, list_recipes, match_dishes, plan_recipe,
                                  recipe_needs)

__all__ = ["DATA_NOTE", "RecipeError", "get_recipe", "list_recipes", "match_dishes", "plan_recipe", "recipe_needs"]
