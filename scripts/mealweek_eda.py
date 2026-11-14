"""Rebuild the MealWeek data audit, downloading the pinned USDA release on first use."""

import csv
import hashlib
import json
from collections import Counter, defaultdict
from io import TextIOWrapper
from pathlib import Path, PurePosixPath
from statistics import median, quantiles
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZipFile

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_URL = (
    "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_survey_food_csv_2024-10-31.zip"
)
ARCHIVE = ROOT / "data/usda/fndds-2021-2023-csv.zip"
SHA256 = "5ccc25ec2777a8982fbb61378a42f415316173eb11e48c9a8ba4cb19f5a4f29c"
EDITS = ROOT / "datasets/edits"
OUTPUT = ROOT / "reports/eda"
NUTRIENTS = {
    "208": ("kcal", "KCAL"),
    "203": ("protein", "G"),
    "204": ("fat", "G"),
    "205": ("carbs", "G"),
}
CATEGORIES = {
    "3804": "Супы на бульоне",
    "3806": "Крем-супы",
    "4804": "Кукурузная и другие каши",
    "1822": "Греческий йогурт",
    "3202": "Блюда из риса",
    "2502": "Яйца и омлеты",
}


def rows(archive, name):
    (member,) = (m for m in archive.namelist() if PurePosixPath(m).name == name + ".csv")
    with archive.open(member) as stream, TextIOWrapper(stream, encoding="utf-8-sig") as reader:
        yield from csv.DictReader(reader)


def summarize(values):
    q1, _, q3 = quantiles(values, n=4, method="inclusive")
    return {
        "min": min(values),
        "q1": round(q1, 4),
        "median": median(values),
        "q3": round(q3, 4),
        "max": max(values),
    }


def audit_source(archive):
    foods = list(rows(archive, "food"))
    names = {r["fdc_id"]: r["description"] for r in foods}
    survey = list(rows(archive, "survey_fndds_food"))
    portions = list(rows(archive, "food_portion"))
    ingredients = list(rows(archive, "input_food"))
    valid_categories = {r["wweia_food_category"] for r in rows(archive, "wweia_food_category")}
    dictionary = {r["nutrient_nbr"]: r for r in rows(archive, "nutrient")}
    for key, (_, unit) in NUTRIENTS.items():
        if dictionary[key]["unit_name"] != unit:
            raise ValueError(f"Unexpected unit for nutrient {key}")
    nutrients = defaultdict(dict)
    keys = set()
    n_rows = orphans = repeated = empty = negative = 0
    for row in rows(archive, "food_nutrient"):
        food, code = row["fdc_id"], row["nutrient_id"]
        n_rows += 1
        orphans += food not in names
        repeated += (food, code) in keys
        keys.add((food, code))
        if code in NUTRIENTS:
            if not row["amount"].strip():
                empty += 1
                continue
            value = float(row["amount"])
            negative += value < 0
            nutrients[food][NUTRIENTS[code][0]] = value
    complete = {food for food in names if len(nutrients.get(food, {})) == len(NUTRIENTS)}
    group_values = {code: [] for code in CATEGORIES}
    for row in survey:
        code, food = row["wweia_category_number"], row["fdc_id"]
        if code in group_values and food in complete:
            group_values[code].append(nutrients[food]["kcal"])
    quantities = Counter(
        r["fdc_id"]
        for r in portions
        if float(r["gram_weight"]) > 0
        and r["portion_description"].strip()
        and r["portion_description"].strip().lower() != "quantity not specified"
    )
    ingredient_counts = Counter(r["fdc_id"] for r in ingredients)
    quality = {
        "archive_sha256": SHA256,
        "foods": len(foods),
        "duplicate_food_ids": len(foods) - len(names),
        "duplicate_names_exact": len(foods) - len(set(names.values())),
        "survey_foods": len(survey),
        "food_without_survey_row": len(names.keys() - {r["fdc_id"] for r in survey}),
        "duplicate_survey_ids": len(survey) - len({r["fdc_id"] for r in survey}),
        "unknown_category_rows": sum(
            r["wweia_category_number"] not in valid_categories for r in survey
        ),
        "represented_categories": len({r["wweia_category_number"] for r in survey}),
        "nutrient_rows": n_rows,
        "duplicate_food_nutrient_pairs": repeated,
        "orphan_rows": {
            "nutrient": orphans,
            **{
                name: sum(r["fdc_id"] not in names for r in dataset)
                for name, dataset in (
                    ("portions", portions),
                    ("ingredients", ingredients),
                    ("survey", survey),
                )
            },
        },
        "empty_macro_amount_rows": empty,
        "negative_macro_rows": negative,
        "complete_macros": len(complete),
        "incomplete_foods": {k: names[k] for k in sorted(names.keys() - complete)},
        "zero_values_per_nutrient": {
            field: sum(nutrients[k][field] == 0 for k in complete)
            for field, _ in NUTRIENTS.values()
        },
        "per_100g": {
            field: summarize([nutrients[k][field] for k in complete])
            for field, _ in NUTRIENTS.values()
        },
        "portions": len(portions),
        "nonpositive_portion_weight": sum(float(r["gram_weight"]) <= 0 for r in portions),
        "unspecified_portion_rows": sum(
            r["portion_description"].strip().lower() == "quantity not specified" for r in portions
        ),
        "foods_with_positive_named_portion": len(names.keys() & quantities.keys()),
        "input_rows": len(ingredients),
        "foods_with_input_rows": len(names.keys() & ingredient_counts),
        "multi_input_foods": sum(count >= 2 for count in ingredient_counts.values()),
        "empty_input_food_fk": sum(not r["fdc_of_input_food"] for r in ingredients),
        "empty_sr_code": sum(not r["sr_code"] for r in ingredients),
        "empty_input_unit": sum(not r["unit"] for r in ingredients),
        "nonpositive_input_weight": sum(float(r["gram_weight"]) <= 0 for r in ingredients),
        "category_kcal_per_100g": {
            code: {"n": len(v), **summarize(v)} for code, v in group_values.items()
        },
    }
    return quality, nutrients, group_values


def choose_variant(form, recipes, *, rank_preferences=False):
    if not form.get("recipe") or not form.get("taste"):
        return "clarify"
    recipe = recipes[form["recipe"]]
    choices = []
    for variant in recipe["variants"]:
        final = recipe["base_g"] | variant["set_g"]
        if variant["taste"] != form["taste"] or set(form.get("exclude", [])) & final.keys():
            continue
        if form.get("strength") and form["strength"] != variant.get("strength"):
            continue
        change = sum(abs(g - recipe["base_g"].get(food, 0)) for food, g in variant["set_g"].items())
        preference_cost = 0
        if rank_preferences:
            increased = {f for f, g in variant["set_g"].items() if g > recipe["base_g"].get(f, 0)}
            preference_cost = len(set(form.get("prefer", [])) - increased)
            if form.get("prefer_existing"):
                preference_cost += len(final.keys() - recipe["base_g"].keys())
        choices.append((preference_cost, change, variant["id"]))
    return min(choices)[-1] if choices else "unavailable"


def per_serving(grams, food_ids, nutrients):
    return {
        field: round(
            sum(g * nutrients[str(food_ids[food])][field] / 100 for food, g in grams.items()), 4
        )
        for field, _ in NUTRIENTS.values()
    }


def audit_days(cases, catalog, nutrients):
    fixture = json.loads((EDITS / "days.json").read_text(encoding="utf-8"))
    request = next(c for c in cases if c["id"] == fixture["request_id"])
    recipe = catalog["recipes"][request["form"]["recipe"]]
    choice = choose_variant(request["form"], catalog["recipes"], rank_preferences=True)
    variant = next(v for v in recipe["variants"] if v["id"] == choice)
    portions = {
        "before": per_serving(recipe["base_g"], catalog["foods"], nutrients),
        "after": per_serving(recipe["base_g"] | variant["set_g"], catalog["foods"], nutrients),
    }
    result = []
    for context in fixture["contexts"]:
        row = {"id": context["id"]}
        for stage, portion in portions.items():
            day = {k: round(context["rest_of_day"][k] + v, 4) for k, v in portion.items()}
            violations = [
                k
                for k, v in day.items()
                if abs(v - fixture["target"][k])
                > fixture["target"][k] * fixture["relative_tolerance"][k]
            ]
            row[stage] = {"total": day, "violations": violations}
        result.append(row)
    return {"request_id": request["id"], "choice": choice, "per_serving": portions, "days": result}


def audit_edits(nutrients):
    catalog = json.loads((EDITS / "catalog.json").read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in (EDITS / "cases.jsonl").read_text("utf-8").splitlines()]
    complete_foods = sum(
        len(nutrients.get(str(fid), {})) == len(NUTRIENTS) for fid in catalog["foods"].values()
    )
    if complete_foods != len(catalog["foods"]):
        raise ValueError("An edit fixture refers to a food without complete nutrition")
    results = []
    for case in cases:
        recipe = catalog["recipes"].get(case["form"].get("recipe"), {})
        allowed = {"clarify", "unavailable"} | {v["id"] for v in recipe.get("variants", [])}
        if not case["gold"] or set(case["gold"]) - allowed:
            raise ValueError(f"Invalid reference outcome for {case['id']}")
        actual = choose_variant(case["form"], catalog["recipes"])
        ranked = choose_variant(case["form"], catalog["recipes"], rank_preferences=True)
        result = {k: case[k] for k in ("id", "family", "gold")}
        result.update(
            actual=actual,
            passed=actual in case["gold"],
            ranked=ranked,
            ranked_passed=ranked in case["gold"],
        )
        results.append(result)
    return {
        "foods": len(catalog["foods"]),
        "recipes": len(catalog["recipes"]),
        "variants": sum(len(r["variants"]) for r in catalog["recipes"].values()),
        "cases": len(cases),
        "families": len({c["family"] for c in cases}),
        "label_audit": {
            "duplicate_ids": len(cases) - len({c["id"] for c in cases}),
            "duplicate_texts": len(cases) - len({c["text"].strip().casefold() for c in cases}),
            "multiple_acceptable_answers": sum(len(c["gold"]) > 1 for c in cases),
            "with_hard_exclusions": sum(bool(c["form"].get("exclude")) for c in cases),
            "with_soft_ingredient_preferences": sum(
                "prefer" in c["form"] or "prefer_existing" in c["form"] for c in cases
            ),
            "foods_with_complete_macros": complete_foods,
        },
        "edit_in_day": audit_days(cases, catalog, nutrients),
        "results": results,
    }


def draw_figures(group_values, edits):
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(8, 4.2), layout="constrained")
    codes = sorted(group_values, key=lambda c: median(group_values[c]))
    ax.boxplot(
        [group_values[c] for c in codes],
        orientation="horizontal",
        tick_labels=[f"{CATEGORIES[c]} (n={len(group_values[c])})" for c in codes],
        patch_artist=True,
        boxprops={"facecolor": "#9ed8cf"},
        medianprops={"color": "#174c45", "linewidth": 2},
        flierprops={"marker": ".", "alpha": 0.55},
    )
    ax.set_xlabel("ккал на 100 г")
    ax.set_title("Калорийность внутри групп FNDDS", loc="left")
    ax.grid(axis="x", alpha=0.2)
    fig.savefig(OUTPUT / "energy.png", dpi=150)
    plt.close(fig)
    labels, basic, ranked, total = [], [], [], []
    for key, label in (
        ("apply", "Подобрать вариант"),
        ("unavailable", "Сообщить об отсутствии"),
        ("clarify", "Уточнить запрос"),
    ):
        subset = [
            r
            for r in edits["results"]
            if (r["gold"][0] if r["gold"][0] in ("unavailable", "clarify") else "apply") == key
        ]
        labels.append(label)
        basic.append(sum(r["passed"] for r in subset))
        ranked.append(sum(r["ranked_passed"] for r in subset))
        total.append(len(subset))
    fig, ax = plt.subplots(figsize=(8, 3.4), layout="constrained")
    for series, offset, color, label in (
        (basic, -0.19, "#617992", "Минимум изменений"),
        (ranked, 0.19, "#31877a", "С учётом предпочтений"),
    ):
        positions = [i + offset for i in range(len(labels))]
        ax.barh(positions, series, height=0.33, color=color, label=label)
        for y, count, n in zip(positions, series, total, strict=True):
            ax.text(count + 0.15, y, f"{count}/{n}", va="center")
    ax.set_yticks(range(len(labels)), labels)
    ax.set_xlim(0, max(total) + 2)
    ax.set_xlabel("Число совпадений с разметкой")
    ax.set_title("Правки рецептов по полям формы", loc="left")
    ax.invert_yaxis()
    ax.legend(loc="lower right", frameon=False)
    fig.savefig(OUTPUT / "edits.png", dpi=150)
    plt.close(fig)


def get_usda_archive():
    if ARCHIVE.is_file() and hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == SHA256:
        return ARCHIVE

    print("Downloading USDA FNDDS 2021–2023…")
    with urlopen(ARCHIVE_URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError(f"SHA-256 mismatch for USDA archive at {ARCHIVE_URL}")
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=ARCHIVE.parent) as temp_dir:
        downloaded = Path(temp_dir) / ARCHIVE.name
        downloaded.write_bytes(data)
        downloaded.replace(ARCHIVE)
    return ARCHIVE


def main():
    with ZipFile(get_usda_archive()) as archive:
        source, nutrients, group_values = audit_source(archive)
    edits = audit_edits(nutrients)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    draw_figures(group_values, edits)
    payload = json.dumps({"source": source, "edits": edits}, ensure_ascii=False, indent=2)
    (OUTPUT / "results.json").write_text(payload + "\n", encoding="utf-8", newline="\n")
    print(f"{source['foods']} foods, {edits['cases']} edit cases. Results in reports/eda.")


if __name__ == "__main__":
    main()
