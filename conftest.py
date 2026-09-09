"""Put the repository root on sys.path for tests.

`scripts/` is not an installed package — it holds operational scripts, not
library code, and packaging it would imply it is importable by the apps. But
`tests/test_seed_extent.py` does need to import the seed's constants to assert
on them, and asserting against a copy of those constants would test nothing.

pytest inserts this file's directory into sys.path under the default import
mode, which is the whole job of this file.
"""
