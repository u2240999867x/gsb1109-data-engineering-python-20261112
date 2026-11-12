# vim:ts=4 sw=4 expandtab softtabstop=4
import sys

if sys.version_info[0] >= 3:
    text_type = str
else:
    text_type = unicode

class _Undefined(object):
    """Sentinel marking a whole record that the caller declares absent.

    Passing this value as ``base`` to :meth:`Merger.merge` means "there was
    no such record in the past". It is distinct from an explicit JSON
    ``null``.
    """
    def __repr__(self):
        return '<undefined>'

#: Use this as ``base`` to declare that no record ever existed. It is
#: distinct from an explicit JSON ``null``.
UNDEFINED = _Undefined()

class JSONValue(object):
    def __init__(self, val=None, ref='#', undef=False, meta=None):
        assert not isinstance(val, JSONValue)
        self.val = val
        self.ref = ref
        self.undef = undef
        # Provenance for an absent value. ``None`` means either a present
        # JSON value or an undefined value created the legacy way (no known
        # origin). Dictionaries describe where the absence came from.
        self.meta = meta

    @classmethod
    def present_null(cls, ref='#', meta=None):
        """Build a JSONValue holding an explicit JSON ``null``.

        Such a value is present (``is_undef()`` is False), unlike a value
        that is missing from the document.
        """
        return cls(val=None, ref=ref, undef=False, meta=meta)

    def is_undef(self):
        return self.undef

    def is_null(self):
        """True when this value is an explicit JSON ``null``."""
        return (not self.undef) and self.val is None

    def _ref_escape(self, key):
        return key.replace('~', '~0').replace('/', '~1')

    def _subval(self, key, **kwargs):
        return JSONValue(ref=self.ref+'/'+self._ref_escape(text_type(key)), **kwargs)

    def source(self):
        """Return provenance describing where an absent value came from.

        Returns ``None`` for present values and for undefined values without
        recorded provenance. Otherwise returns a dictionary that at least
        contains an ``origin`` string and a ``ref`` JSON pointer.
        """
        if self.meta is None:
            return None
        return dict(self.meta)

    def __setitem__(self, key, item):
        if item.is_undef():
            if isinstance(self.val, list):
                raise ValueError("Can't assign an undefined value to a list")

            # setting a dict element to an undefined value deletes that element
            if key in self.val:
                del self.val[key]
        else:
            self.val[key] = item.val

    def __getitem__(self, key):
        return self._subval(key, val=self.val[key])

    def append(self, item):
        assert isinstance(self.val, list)

        if not item.is_undef():
            self.val.append(item.val)

    def get(self, key, *args):
        if key in self.val:
            # An explicit JSON null is a present value: it must not be
            # confused with a key that is entirely missing.
            return self._subval(key, val=self.val[key])

        if args:
            return self._subval(key, val=args[0])

        return self._subval(key, undef=True,
                            meta={'origin': 'missing', 'key': key,
                                  'ref': self.ref + '/' + self._ref_escape(text_type(key))})

    def __repr__(self):
        if self.is_undef():
            return 'JSONValue(undef=True)'
        else:
            return 'JSONValue(%r,%r)' % (self.val, self.ref)

    def items(self):
        for k, v in self.val.items():
            yield (k, self._subval(k, val=v))

    def __iter__(self):
        assert isinstance(self.val, list)

        for i, v in enumerate(self.val):
            yield self._subval(i, val=v)

    def sort(self, *args, **kwargs):
        assert isinstance(self.val, list)

        i = list(self)
        i.sort(*args, **kwargs)
        self.val = [ j.val for j in i ]
