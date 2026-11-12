# vim:ts=4 sw=4 expandtab softtabstop=4


class WalkContext(object):
    """Read-only view of the position of a walk in the documents.

    The same class is used by both the document merge walk
    (WalkInstance) and the downstream schema derivation walk
    (WalkSchema). Every strategy and descender receives the owning walk,
    whose ``context`` attribute exposes the current column path
    (``schema_ref``), the source of the base and head instances
    (``base_ref``, ``head_ref``) and the resource the current schema was
    loaded from (``resource_uri``).

    A nested derivation started from a strategy (for example from a
    strategy handling members of an array) gets its own context, so the
    outer walk is unaffected once the nested walk returns or raises.
    """

    __slots__ = ('schema_ref', 'base_ref', 'head_ref', 'resource_uri')

    def __init__(self, schema_ref='#', base_ref=None, head_ref=None,
                 resource_uri=''):
        self.schema_ref = schema_ref
        self.base_ref = base_ref
        self.head_ref = head_ref
        self.resource_uri = resource_uri

    def copy(self):
        return WalkContext(
                schema_ref=self.schema_ref,
                base_ref=self.base_ref,
                head_ref=self.head_ref,
                resource_uri=self.resource_uri)

    def __repr__(self):
        return 'WalkContext(schema_ref=%r, base_ref=%r, head_ref=%r, ' \
               'resource_uri=%r)' % (
                       self.schema_ref, self.base_ref, self.head_ref,
                       self.resource_uri)
