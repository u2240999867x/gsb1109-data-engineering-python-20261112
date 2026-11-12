"""
A minimal application to be frozen by PyInstaller.
"""

import json

import jsonpath_ng
import jsonpath_ng.ext

data = {"a": [1, -1, 2]}

results = {
    "base": [m.value for m in jsonpath_ng.parse("$.a[*]").find(data)],
    "ext": [m.value for m in jsonpath_ng.ext.parse("$.a[?@ > 0]").find(data)],
}
print(json.dumps(results))
