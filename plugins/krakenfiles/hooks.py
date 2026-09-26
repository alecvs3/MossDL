from engine.providers.hosted import KrakenfilesProvider

def resolve(params): return [item.to_dict() for item in KrakenfilesProvider.resolve(params["url"], params.get("secrets"))]
def refresh(params): return resolve({"url": params["item"]["source_url"], "secrets": params.get("secrets")})
