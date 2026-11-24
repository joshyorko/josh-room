"""Bounded check of the actual product pin guard, not a replacement model."""
import hashlib
import importlib.util
import inspect
import itertools
import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "src"))
from josh_room import composition_safety
from josh_room.catalog import Catalog


def check(guard):
    spec = importlib.util.spec_from_file_location("pin_fixtures", REPOSITORY / "tests/test_composition_safety.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    body, _, _ = fixtures.fixture()
    catalog = Catalog.from_body(body["dimension_catalog"])
    digests = {room: hashlib.sha256(room.encode()).hexdigest() for room in ("child", "parent")}
    cases = accepted = 0
    for actual_room, actual_pin, digest, reserved_room, reserved_pin in itertools.product(
        ("child", "parent"), ("jat-child", "jat-parent"), (*digests.values(), "f" * 64),
        ("child", "parent"), ("jat-child", "jat-parent")
    ):
        pin = {"room_id": actual_room, "logical_jat_id": actual_pin, "descriptor_sha256": digest}
        expected = (actual_room == reserved_room and actual_pin == reserved_pin
                    and reserved_pin == "jat-" + reserved_room and digest == digests[reserved_room])
        got = True
        try:
            guard(pin, catalog, reserved_room, reserved_pin)
        except ValueError:
            got = False
        cases += 1
        accepted += got
        if got != expected:
            return {"pass": False, "cases": cases, "counterexample": {"pin": pin, "reserved_room": reserved_room, "reserved_pin": reserved_pin, "accepted": got}}
    assert accepted == 2, "Positive exact child and parent pin reachability required"
    return {"pass": True, "cases": cases, "positive_exact_pins": accepted}


def main(destination):
    source = inspect.getsource(composition_safety._pin)
    positive = check(composition_safety._pin)
    mutations = {}
    for name, old, new in [
        ("reserved-identity-check-removed", 'if pin["room_id"] != room_id or pin["logical_jat_id"] != logical_id:', 'if False:'),
        ("descriptor-digest-check-removed", ' or record["ciphertext_sha256"] != pin["descriptor_sha256"]', '')
    ]:
        assert source.count(old) == 1, "Actual product seam changed"
        namespace = dict(vars(composition_safety))
        exec(compile(source.replace(old, new), "<product-pin-mutation>", "exec"), namespace)
        mutations[name] = check(namespace["_pin"])
    assert positive["pass"] and all(not result["pass"] for result in mutations.values())
    receipt = {"scope": "Actual composition_safety._pin over 48 finite room/pin/digest/reserved-slot combinations",
               "positive": positive, "mutations": mutations,
               "product_source_sha256": hashlib.sha256(Path(composition_safety.__file__).read_bytes()).hexdigest(),
               "limits": "No cryptography, provider authentication, capture atomicity or filesystem restore proof. Live restoration is separate evidence."}
    Path(destination).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"pass": True, "cases": positive["cases"], "mutants_killed": len(mutations)}))


if __name__ == "__main__":
    main(sys.argv[1])
