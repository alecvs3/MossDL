from engine.providers.cyberdrop_hosts import GenericKVSProvider

def resolve(params): return [item.to_dict() for item in GenericKVSProvider.resolve(params['url'], params.get('secrets'))]
def refresh(params): return resolve({'url': params['item']['source_url'], 'secrets': params.get('secrets')})
