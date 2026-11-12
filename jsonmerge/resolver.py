# vim:ts=4 sw=4 expandtab softtabstop=4
import contextlib

from jsonschema.validators import RefResolver, urldefrag, urljoin

try:
    from urllib.parse import unquote
except ImportError:
    # Python 2
    from urllib import unquote

from jsonschema.exceptions import RefResolutionError

try:
    from referencing.exceptions import Unresolvable
except ImportError:
    # referencing is not installed (jsonschema<4.18). Only the legacy
    # RefResolver-based resolution path is available.
    RESOLUTION_ERRORS = (RefResolutionError,)
    _ResolutionError = RefResolutionError
else:
    # Tuple of exceptions that can be raised when a reference cannot be
    # resolved. Code that needs to handle such errors should catch this
    # tuple instead of referencing a specific class.
    RESOLUTION_ERRORS = (RefResolutionError, Unresolvable)
    _ResolutionError = Unresolvable


class LocalRefResolver(RefResolver):
    # We want to have a class that is the same as jsonschema's RefResolver
    # except:
    #
    #  * No caching for resolves. We are changing the schema as we walk through
    #    it, so with caching you can get outdated resolves.
    #
    #  * Provide a _is_remote_ref() method to check if a $ref points to an
    #    external reference.

    def __init__(self, *args, **kwargs):
        kwargs["remote_cache"] = self.resolve_from_url
        super(LocalRefResolver, self).__init__(*args, **kwargs)

    def is_remote_ref(self, ref):
        url = urljoin(self.resolution_scope, ref)
        url, fragment = urldefrag(url)
        return url != self.base_uri


def _resolve_pointer(document, fragment):
    # Resolve a JSON pointer (RFC 6901) fragment within a document.
    fragment = unquote(fragment).lstrip("/")

    if not fragment:
        return document

    for part in fragment.split("/"):
        part = part.replace("~1", "/").replace("~0", "~")

        if isinstance(document, list):
            try:
                part = int(part)
            except ValueError:
                raise _ResolutionError(fragment)

        try:
            document = document[part]
        except (KeyError, IndexError, TypeError):
            raise _ResolutionError(fragment)

    return document


class DocumentResolver(object):
    """Resolve local references within a single document.

    Only fragment-only references containing JSON pointers (e.g. "#/a/b")
    are supported. The document is not copied or cached, so resolutions
    always see the current state of the document.
    """

    def __init__(self, document):
        self.document = document

    @contextlib.contextmanager
    def resolving(self, ref):
        url, fragment = urldefrag(ref)
        if url:
            raise _ResolutionError(ref)
        yield _resolve_pointer(self.document, fragment)

    @staticmethod
    def resolve_fragment(document, fragment):
        return _resolve_pointer(document, fragment)


class RegistryResolver(object):
    """Resolve references using a referencing.Registry.

    This is a replacement for the deprecated jsonschema.RefResolver that
    is used when the Merger is created with a "registry" argument. It
    provides the subset of the RefResolver interface that jsonmerge
    needs.

    base_uri is the URI of the root schema (the value of its "id" or
    "$id" keyword, or the empty string if it has none). Relative
    references are resolved against it and the URI is also used as the
    root schema's identity in the registry.
    """

    def __init__(self, registry, base_uri=""):
        self._registry = registry
        self.base_uri = base_uri
        self._scopes = [base_uri]
        self._resolvers = [registry.resolver(base_uri=base_uri)]

    @property
    def registry(self):
        return self._registry

    @property
    def current_resolver(self):
        # referencing.Resolver for the innermost resolution scope. Can be
        # passed to jsonschema.Validator.evolve() as the "_resolver"
        # argument so that references in subschemas are resolved against
        # the correct base URI.
        return self._resolvers[-1]

    @registry.setter
    def registry(self, registry):
        # Registry objects are immutable, so Merger.cache_schema()
        # replaces the registry with an updated one.
        self._registry = registry
        del self._scopes[1:]
        del self._resolvers[1:]
        self._resolvers[0] = registry.resolver(base_uri=self.base_uri)

    @contextlib.contextmanager
    def resolving(self, ref):
        resolution = self._resolvers[-1].lookup(ref)

        self._resolvers.append(resolution.resolver)
        self._scopes.append(urldefrag(urljoin(self._scopes[-1], ref))[0])
        try:
            yield resolution.contents
        finally:
            self._resolvers.pop()
            self._scopes.pop()

    def is_remote_ref(self, ref):
        url, fragment = urldefrag(urljoin(self._scopes[-1], ref))
        return url != self.base_uri

    @staticmethod
    def resolve_fragment(document, fragment):
        return _resolve_pointer(document, fragment)


def specification_for_validator(validatorclass):
    """Return the referencing specification for a jsonschema validator class.

    The specification determines how resource identities ("id" vs. "$id")
    and subresources are interpreted for schemas handled by the given
    validator class.
    """
    import referencing
    import referencing.jsonschema

    dialect_id = None

    id_of = getattr(validatorclass, 'ID_OF', None)
    meta_schema = getattr(validatorclass, 'META_SCHEMA', None)
    if id_of is not None and meta_schema is not None:
        dialect_id = id_of(meta_schema)

    return referencing.jsonschema.specification_with(
            dialect_id or "urn:unknown-dialect",
            default=referencing.Specification.OPAQUE)
