from engine.providers.cyberdrop_hosts import CheveretoGenericProvider

def resolve(params): return [item.to_dict() for item in CheveretoGenericProvider.resolve(params['url'], params.get('secrets'))]
def enumerate(params): return [item.to_dict() for item in CheveretoGenericProvider.enumerate(params['url'], params.get('secrets'))]
def refresh(params): return resolve({'url': params['item']['source_url'], 'secrets': params.get('secrets')})
