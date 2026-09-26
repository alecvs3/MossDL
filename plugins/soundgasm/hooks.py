"""Generated provider adapter from a Cyberdrop-DL parity specification.

This file uses the transfer-manager provider implementation. The reference
crawler is represented by parity.json and is never imported at runtime.
"""

from engine.providers.cyberdrop_hosts import SoundGasmProvider

_PROVIDER = SoundGasmProvider

def _items(operation, params):
    method = getattr(_PROVIDER, operation)
    if operation == 'refresh':
        item = params.get('item') or {}
        url = item.get('source_url') or params.get('url')
    else:
        url = params.get('url')
    result = method(url, params.get('secrets'))
    return [item.to_dict() for item in result]

def resolve(params):
    return _items('resolve', params)

def refresh(params):
    item = params.get('item') or {}
    return resolve({'url': item.get('source_url') or params.get('url'),
                    'secrets': params.get('secrets')})
