from engine.providers.cyberdrop_hosts import VikingfileProvider

def resolve(params): return [item.to_dict() for item in VikingfileProvider.resolve(params['url'], params.get('secrets'))]
def refresh(params): return resolve({'url': params['item']['source_url'], 'secrets': params.get('secrets')})
