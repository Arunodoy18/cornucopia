#!/usr/bin/env python3
import argparse
import logging
import sys
import atheris
import os
from unittest.mock import patch

# Add the root directory to sys.path to import scripts
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts")))

try:
    import scripts.convert_capec_map_to_asvs_map as target
except ImportError:
    import convert_capec_map_to_asvs_map as target  # type: ignore[no-redef]

# Initialize convert_vars (usually done at the bottom of the script under __main__)
if not hasattr(target, "convert_vars"):
    target.convert_vars = target.ConvertVars()


# ---------------------------------------------------------------------------
# Fuzzed data-structure builders
# ---------------------------------------------------------------------------


def _build_fuzzed_yaml_data(fdp: atheris.FuzzedDataProvider) -> object:
    """Return a fuzzed top-level data structure covering happy and error paths.

    Covers:
    - Completely wrong types (None, int, str, list)  → invalid YAML structure
    - Missing ``suits`` key                          → missing expected key
    - ``suits`` with a non-list value               → unexpected value type
    - Empty suits list                              → empty mapping
    - Nested corruption via helper builders
    """
    choice = fdp.ConsumeIntInRange(0, 6)
    if choice == 0:
        return None
    if choice == 1:
        return fdp.ConsumeUnicodeNoSurrogates(64)
    if choice == 2:
        return []
    if choice == 3:
        return fdp.ConsumeInt(4)

    data: dict = {}

    if fdp.ConsumeBool():
        data["meta"] = {
            "component": fdp.ConsumeUnicodeNoSurrogates(32) if fdp.ConsumeBool() else "mappings",
            "version": fdp.ConsumeUnicodeNoSurrogates(16) if fdp.ConsumeBool() else "3.0",
        }

    if not fdp.ConsumeBool():
        # Omit the "suits" key entirely to exercise the missing-key warning path
        data[fdp.ConsumeUnicodeNoSurrogates(16)] = fdp.ConsumeUnicodeNoSurrogates(32)
        return data

    suits = [_build_fuzzed_suit(fdp) for _ in range(fdp.ConsumeIntInRange(0, 4))]

    # Occasionally replace the list with a scalar to test the non-iterable guard
    data["suits"] = suits if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(32)

    return data


def _build_fuzzed_suit(fdp: atheris.FuzzedDataProvider) -> object:
    """Return a fuzzed suit, which may be a non-dict, lack ``cards``, or have corrupted cards."""
    if fdp.ConsumeBool():
        # Non-dict suit: exercises the isinstance / early-return guard
        return fdp.ConsumeUnicodeNoSurrogates(32)

    suit: dict = {}

    if fdp.ConsumeBool():
        # No "cards" key: exercises the missing-key early-return guard
        suit[fdp.ConsumeUnicodeNoSurrogates(16)] = fdp.ConsumeUnicodeNoSurrogates(32)
        return suit

    cards = [_build_fuzzed_card(fdp) for _ in range(fdp.ConsumeIntInRange(0, 5))]
    # Occasionally use a non-list value for cards
    suit["cards"] = cards if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(16)
    return suit


def _build_fuzzed_card(fdp: atheris.FuzzedDataProvider) -> object:
    """Return a fuzzed card which may lack ``capec_map`` or carry bad types."""
    if fdp.ConsumeBool():
        # Non-dict card
        return fdp.ConsumeUnicodeNoSurrogates(32)

    card: dict = {}

    if not fdp.ConsumeBool():
        # No "capec_map" key: exercises the missing-key early-return guard
        card[fdp.ConsumeUnicodeNoSurrogates(16)] = fdp.ConsumeUnicodeNoSurrogates(32)
        return card

    card["capec_map"] = _build_fuzzed_capec_map(fdp)
    return card


def _build_fuzzed_capec_map(fdp: atheris.FuzzedDataProvider) -> object:
    """Return a fuzzed capec_map value which may be a non-dict or have unusual keys/values."""
    if fdp.ConsumeBool():
        # Non-dict: exercises the ``isinstance(capec_map, dict)`` guard
        return fdp.ConsumeUnicodeNoSurrogates(32) if fdp.ConsumeBool() else []

    result: dict = {}
    for _ in range(fdp.ConsumeIntInRange(0, 5)):
        # CAPEC codes are normally ints; also fuzz with strings and edge-case values
        key = fdp.ConsumeIntInRange(1, 10000) if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(16)
        result[key] = _build_fuzzed_asvs_reqs(fdp)

    return result


def _build_fuzzed_asvs_reqs(fdp: atheris.FuzzedDataProvider) -> object:
    """Return a fuzzed ASVS requirements mapping.

    Covers:
    - Non-dict value                 → unexpected value type for the whole entry
    - Missing ``owasp_asvs``         → missing expected key (uses .get default)
    - Empty ``owasp_asvs`` list      → empty mapping
    - Non-list ``owasp_asvs``        → unexpected value type for the iterable
    - ``None`` for ``owasp_asvs``    → corrupted content
    """
    if fdp.ConsumeBool():
        # Non-dict value: exercises the .get() fallback to []
        return fdp.ConsumeUnicodeNoSurrogates(32)

    reqs: dict = {}

    if fdp.ConsumeBool():
        choice = fdp.ConsumeIntInRange(0, 3)
        if choice == 0:
            reqs["owasp_asvs"] = []
        elif choice == 1:
            reqs["owasp_asvs"] = [
                fdp.ConsumeUnicodeNoSurrogates(16) for _ in range(fdp.ConsumeIntInRange(1, 5))
            ]
        elif choice == 2:
            # Non-list value for owasp_asvs to test the iteration guard
            reqs["owasp_asvs"] = fdp.ConsumeUnicodeNoSurrogates(32)
        else:
            reqs["owasp_asvs"] = None  # Corrupted / missing value

    if fdp.ConsumeBool():
        # Extra unexpected key to ensure robustness against schema drift
        reqs[fdp.ConsumeUnicodeNoSurrogates(16)] = fdp.ConsumeUnicodeNoSurrogates(32)

    return reqs


def _build_fuzzed_output_map(fdp: atheris.FuzzedDataProvider) -> dict:
    """Build a fuzzed mapping for ``convert_to_output_format``.

    Mixes int and string keys, and uses both sets and scalars as values to
    stress-test the output formatter.
    """
    result: dict = {}
    for _ in range(fdp.ConsumeIntInRange(0, 10)):
        key = fdp.ConsumeIntInRange(1, 10000) if fdp.ConsumeBool() else fdp.ConsumeUnicodeNoSurrogates(16)
        if fdp.ConsumeBool():
            result[key] = {fdp.ConsumeUnicodeNoSurrogates(16) for _ in range(fdp.ConsumeIntInRange(0, 5))}
        else:
            # Non-set value: exercises type-error paths in the formatter
            result[key] = fdp.ConsumeUnicodeNoSurrogates(16) if fdp.ConsumeBool() else None
    return result


# ---------------------------------------------------------------------------
# Main fuzz target
# ---------------------------------------------------------------------------


def test_main(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    logging.getLogger().setLevel(logging.CRITICAL)

    # ------------------------------------------------------------------
    # 1. Fuzz extract_capec_mappings – pure extraction, no I/O
    # ------------------------------------------------------------------
    fuzzed_data = _build_fuzzed_yaml_data(fdp)
    try:
        if isinstance(fuzzed_data, dict):
            target.extract_capec_mappings(fuzzed_data)
    except (TypeError, ValueError, AttributeError):
        pass

    # ------------------------------------------------------------------
    # 2. Fuzz extract_asvs_to_capec_mappings – same structure, inverse map
    # ------------------------------------------------------------------
    try:
        if isinstance(fuzzed_data, dict):
            target.extract_asvs_to_capec_mappings(fuzzed_data)
    except (TypeError, ValueError, AttributeError):
        pass

    # ------------------------------------------------------------------
    # 3. Fuzz convert_to_output_format – output serialisation layer
    # ------------------------------------------------------------------
    try:
        fuzzed_map = _build_fuzzed_output_map(fdp)
        parameter = fdp.ConsumeUnicodeNoSurrogates(32) if fdp.ConsumeBool() else "owasp_asvs"
        meta: dict | None = {"component": fdp.ConsumeUnicodeNoSurrogates(16)} if fdp.ConsumeBool() else None
        target.convert_to_output_format(fuzzed_map, parameter=parameter, meta=meta)
    except (TypeError, ValueError, AttributeError):
        pass

    # ------------------------------------------------------------------
    # 4. Fuzz main() with mocked I/O – full pipeline including arg parsing
    # ------------------------------------------------------------------
    version = fdp.ConsumeUnicodeNoSurrogates(32)
    edition = fdp.ConsumeUnicodeNoSurrogates(32)
    input_path = fdp.ConsumeUnicodeNoSurrogates(128)
    output_path = fdp.ConsumeUnicodeNoSurrogates(128)
    debug = fdp.ConsumeBool()

    argp = {
        "input_path": input_path if fdp.ConsumeBool() else None,
        "version": version,
        "edition": edition,
        "output_path": output_path if fdp.ConsumeBool() else str(target.ConvertVars.DEFAULT_OUTPUT_PATH),
        "debug": debug,
    }

    args = ["convert_capec_map_to_asvs_map.py", "-v", version, "-e", edition]
    if argp["input_path"]:
        args.extend(["-i", argp["input_path"]])
    if argp["output_path"]:
        args.extend(["-o", argp["output_path"]])
    if debug:
        args.append("-d")

    # Re-build a potentially different data payload for the main() pipeline
    main_yaml_data = _build_fuzzed_yaml_data(fdp) if fdp.ConsumeBool() else {}
    save_result = fdp.ConsumeBool()

    try:
        with (
            patch.object(target, "load_yaml_file", return_value=main_yaml_data),
            patch.object(target, "save_yaml_file", return_value=save_result),
            patch.object(target, "parse_arguments", return_value=argparse.Namespace(**argp)),
            patch("sys.argv", args),
        ):
            target.main()
    except SystemExit:
        # Script calls sys.exit(1) on failure paths – expected during fuzzing
        pass
    except (TypeError, ValueError, KeyError, AttributeError):
        # Expected exceptions from malformed fuzzed data flowing through
        # extraction / output-format logic in the main pipeline
        pass
    except Exception as e:
        # Re-raise genuinely unexpected exceptions so Atheris can surface them
        raise e


def main() -> None:
    atheris.instrument_all()
    atheris.Setup(sys.argv, test_main, enable_python_coverage=True)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
