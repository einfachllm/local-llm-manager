"""The example recipes are documentation, so three things have to agree: the files in the folder, the
table in examples/recipes/README.md and the table in docs/recipes.md. They are hand-maintained copies of
each other and drifted apart once already - and an example that does not import cleanly is a bug report
waiting for a user who pasted it.
"""

import collections
import pathlib
import re

import pytest

from local_llm_manager.recipefile import parse_recipe_file, recipe_from_file
from local_llm_manager.script_check import check_script

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples" / "recipes"
FOLDER_README = EXAMPLES / "README.md"
DOCS_PAGE = ROOT / "docs" / "recipes.md"

SUFFIXES = (".yaml", ".yml", ".json", ".sh")
# The same Qwen3.8-27B-NVFP4 on four engines: one port, one served name, so **Switch to** keeps the endpoint.
FAMILY = ("qwen38-mtp.yaml", "qwen38-dflash.yaml", "qwen38-sglang.yaml", "qwen38-tensorfold.yaml")


def files() -> list[pathlib.Path]:
    return sorted(p for p in EXAMPLES.iterdir() if p.suffix in SUFFIXES)


def listed_in(md: pathlib.Path) -> list[str]:
    """The example file names a table row links to."""
    return re.findall(r"\[`([\w.\-]+\.(?:yaml|yml|json|sh))`\]", md.read_text())


def recipes() -> dict[str, dict]:
    """Every compact example file, imported with the weights entry it names already registered."""
    out = {}
    for f in files():
        if f.suffix == ".sh":
            continue
        data = parse_recipe_file(f.read_text())
        named = [str(data.get("weights") or "")]
        known = [{"id": i + 1, "name": n, "source": "", "locations": {}} for i, n in enumerate(named) if n]
        recipe, warnings = recipe_from_file(data, known)
        recipe["_warnings"] = warnings
        out[f.name] = recipe
    return out


def tokens(text: str) -> list[str]:
    """The words of a server command, comment lines and the quotes around JSON arguments left out."""
    lines = [line for line in (text or "").splitlines() if not line.strip().startswith("#")]
    return [t.strip("'\"") for t in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", " ".join(lines))]


def flags(text: str) -> list[str]:
    """The long CLI flags of a server command, once per mention."""
    return [t.split("=")[0] for t in tokens(text) if t.startswith("--")]


def served_name(recipe: dict) -> str | None:
    """The model name clients send: --served-model-name (vLLM/SGLang), --alias (llama.cpp) or
    --name (TensorFold, in args or command) - whichever the engine uses."""
    given = tokens(recipe.get("args")) + tokens(recipe.get("command"))
    for flag in ("--served-model-name", "--alias", "--name"):
        if flag in given:
            return given[given.index(flag) + 1]
    return None


def lines_of(recipe: dict, field: str) -> list[str]:
    return [line.strip() for line in (recipe.get(field) or "").splitlines() if line.strip()]


def test_every_example_is_listed_once_in_both_tables():
    on_disk = sorted(f.name for f in files())
    assert sorted(listed_in(FOLDER_README)) == on_disk, "examples/recipes/README.md table is out of date"
    assert sorted(listed_in(DOCS_PAGE)) == on_disk, "docs/recipes.md table is out of date"


def test_no_file_is_advertised_twice():
    for md in (FOLDER_README, DOCS_PAGE):
        repeated = [n for n, count in collections.Counter(listed_in(md)).items() if count > 1]
        assert not repeated, f"{md} lists {repeated} more than once"


def test_examples_import_without_warnings():
    assert {n: r["_warnings"] for n, r in recipes().items() if r["_warnings"]} == {}


def test_examples_have_unique_recipe_names():
    names = collections.Counter(r["name"] for r in recipes().values())
    assert [n for n, count in names.items() if count > 1] == []


def test_an_example_does_not_set_the_same_flag_or_mount_twice():
    for name, recipe in recipes().items():
        repeated = [f for f, count in collections.Counter(flags(recipe.get("args"))).items() if count > 1]
        assert repeated == [], f"{name}: server flags given twice: {repeated}"
        for field, key in (("volumes", lambda l: l.split(":")[1] if ":" in l else l),
                           ("env", lambda l: l.split("=")[0])):
            again = [k for k, count in collections.Counter(key(l) for l in lines_of(recipe, field)).items()
                     if count > 1]
            assert again == [], f"{name}: {field} sets {again} twice"


def test_the_family_shares_one_port_and_one_served_name():
    all_recipes = recipes()
    missing = [n for n in FAMILY if n not in all_recipes]
    assert missing == [], f"the Qwen3.8 engine family lost {missing}"
    ports = {all_recipes[n]["host_port"] for n in FAMILY}
    assert len(ports) == 1, f"the family no longer shares one port: {ports}"
    # All four engines, native TensorFold included: its --name is the served model id, so a client
    # asking for "qwen3.8" keeps working after a switch to it.
    served = {served_name(all_recipes[n]) for n in FAMILY}
    assert len(served) == 1 and None not in served, f"the engines disagree on the served name: {served}"


def test_no_other_example_sits_on_the_family_port():
    all_recipes = recipes()
    family_port = all_recipes[FAMILY[0]]["host_port"]
    others = {n: r["host_port"] for n, r in all_recipes.items() if n not in FAMILY and r["kind"] != "agent"}
    clash = [n for n, port in others.items() if port == family_port]
    assert clash == [], f"{clash} would block **Switch to** on port {family_port}"


def test_a_model_named_by_weights_is_mounted_from_it():
    """`weights:` with a model outside the {weights} mount starts a container without its model."""
    for name, recipe in recipes().items():
        if not recipe.get("weights_id"):
            continue
        model = str(recipe.get("model") or "")
        if not model.startswith("/"):  # native: {weights} is the model argument itself
            assert "{weights}" in model, f"{name}: native model {model!r} does not come from {{weights}}"
            continue
        targets = [line.split(":")[1] for line in lines_of(recipe, "volumes")
                   if line.startswith("{weights}")]
        assert any(model == t.rstrip("/") or model.startswith(t.rstrip("/") + "/") for t in targets), \
            f"{name}: model {model!r} is not inside a {{weights}} mount ({targets})"


def test_starter_scripts_in_the_folder_keep_every_important_line():
    """`check_script` reports lines the restricted sandbox would skip - a dropped `docker run` means an
    empty recipe. An absolute path used as the model argument is only informational (`skipped` false)."""
    for f in files():
        if f.suffix != ".sh":
            continue
        dropped = [x for x in check_script(f.read_text())["findings"] if x["skipped"]]
        assert dropped == [], f"{f.name} has lines the import would skip: {dropped}"


def test_yaml_examples_are_saveable_through_the_api():
    """The imported drafts must pass the API's own validation, not just the file parser."""
    from local_llm_manager.main import RecipeIn

    for name, recipe in recipes().items():
        RecipeIn(**{k: v for k, v in recipe.items() if k != "_warnings"})


def test_starter_scripts_really_import_into_valid_recipes():
    """Beyond the static check: run every published .sh (both example folders) through the restricted
    import and validate the draft against the API model. Only benign warnings are allowed: the manager
    drops -d itself, and the Hermes gateway has no model argument. The undefined $ARGS in the Hermes
    vLLM starter must expand to nothing, not leak into the recipe."""
    import asyncio

    from local_llm_manager.importer import import_script
    from local_llm_manager.main import RecipeIn

    folders = (EXAMPLES, ROOT / "examples" / "hermes-agent")
    scripts = sorted(p for folder in folders for p in folder.glob("*.sh"))
    assert scripts, "no starter scripts found"
    for path in scripts:
        result = asyncio.run(import_script(path.read_text()))
        recipe = result["recipe"]
        RecipeIn(**recipe)  # the draft must be saveable through the API
        assert recipe["host_port"] > 0, path.name
        assert "$ARGS" not in (recipe.get("args") or ""), f"{path.name}: unexpanded variable in args"
        unexpected = [w for w in result["warnings"]
                      if not w.startswith("Removed ")
                      and not (path.name == "docker-recipe.example.sh"
                               and w.startswith("Could not tell which argument is the model"))]
        assert unexpected == [], f"{path.name}: {unexpected}"
        assert not any("stopped before starting" in w for w in result["warnings"]), \
            f"{path.name}: preflight checks blocked the import"


@pytest.mark.parametrize("name", ["qwen38-dflash.yaml", "qwen38-sglang.yaml", "qwen38-tensorfold.yaml"])
def test_a_drafter_travels_with_its_model_instead_of_a_machines_home(name):
    """Model and drafter come from one registered entry, so no example may name a user's home directory.
    The TensorFold binary has no placeholder of its own: `/home/YOU/...` is the one path a user edits."""
    recipe = recipes()[name]
    text = " ".join(str(recipe.get(field) or "") for field in ("model", "args", "volumes", "command"))
    assert "/home/" not in text.replace("/home/YOU/", ""), f"{name} hardcodes one machine's home directory"
    assert "~/models" not in text, f"{name} names a model folder instead of the registered {{weights}}"
    if recipe["runtime"] == "docker":  # model and drafter ride in one mount
        assert any(l.startswith("{weights}") for l in lines_of(recipe, "volumes")), f"{name} mounts nothing from {{weights}}"
    else:  # a native process gets the paths as arguments
        assert "{weights}" in str(recipe.get("model")), f"{name}: the model does not come from {{weights}}"
        assert "{weights}" in str(recipe.get("args")), f"{name}: the drafter is not under {{weights}}"
    assert [l for l in lines_of(recipe, "args") if "~" in l] == [], \
        f"{name} relies on ~ inside args, which the process runtime does not expand"
