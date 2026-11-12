class JSONMergeError(Exception):
	def __init__(self, message, value=None, strategy_name=None):
		self.message = message
		self.value = value
		self.strategy_name = strategy_name

	def __str__(self):
		r = self.message

		try:
			ref = self.value.ref
		except:
			ref = None

		if ref is not None:
			r = "%s: %s" % (r, ref)

		if self.strategy_name is not None:
			r = "'%s' merge strategy: %s" % (self.strategy_name, r)

		return r

class BaseInstanceError(JSONMergeError): pass
class HeadInstanceError(JSONMergeError): pass
class SchemaError(JSONMergeError): pass

class AmbiguousIdError(BaseInstanceError):
    """Raised when an id in head matches more than one entry in base.

    The offending head entry is available in the 'value' attribute, while
    the 'base_refs' attribute lists the JSON references of the conflicting
    entries already present in the base.
    """
    def __init__(self, message, value=None, strategy_name=None, base_refs=None):
        JSONMergeError.__init__(self, message, value, strategy_name)
        self.base_refs = base_refs or []
