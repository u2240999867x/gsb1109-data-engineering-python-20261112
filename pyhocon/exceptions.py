class ConfigException(Exception):

    def __init__(self, message, ex=None):
        super(ConfigException, self).__init__(message)
        self._exception = ex


class ConfigMissingException(ConfigException, KeyError):
    pass


class ConfigSubstitutionException(ConfigException):
    pass


class ConfigWrongTypeException(ConfigException):
    pass


class ConfigCircularIncludeException(ConfigException):
    """Raised when an include chain references a resource already being loaded."""

    def __init__(self, chain):
        self.chain = list(chain)
        message = 'Circular include detected: {chain}'.format(
            chain=' -> '.join(self.chain))
        super(ConfigCircularIncludeException, self).__init__(message)


class ConfigForbiddenIncludeException(ConfigException):
    """Raised when an include points at a source forbidden by the resource manifest."""

    def __init__(self, target, referrer, reason=None):
        self.target = target
        self.referrer = referrer
        self.reason = reason
        message = 'Forbidden include {target!r} referenced from {referrer!r}'.format(
            target=target, referrer=referrer)
        if reason:
            message += ': {reason}'.format(reason=reason)
        super(ConfigForbiddenIncludeException, self).__init__(message)
