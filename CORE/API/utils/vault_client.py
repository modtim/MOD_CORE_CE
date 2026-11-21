"""Compatibility alias: utils.vault_client IS utils.openbao_client (GAP-MODCE89 slice C).

Kept so every existing importer and monkeypatch target keeps working. Remove
only after all importers are repointed (later slices).
"""

import sys

from utils import openbao_client as _impl

sys.modules[__name__] = _impl
