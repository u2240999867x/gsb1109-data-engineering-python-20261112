"""Custom postcode provider used by postcode_probe.py.

New postcode format: ``CN-######`` (prefix + 6 digits), which cannot collide
with the en_US formats (``#####`` / ``#####-####``) or en_GB formats, so a
simple regex decides whether a row hit the new format.

The class must be named ``Provider`` because ``Factory.create(..., includes=[...])``
resolves providers via ``import_module(path).Provider``.
"""

from faker.providers import BaseProvider


class Provider(BaseProvider):
    def postcode(self) -> str:
        return "CN-" + self.numerify("######")
