from engine.providers.generic import GenericProvider

def resolve(params):
    return [item.to_dict() for item in GenericProvider.resolve(params["url"], params.get("secrets"))]

def refresh(params):
    return resolve({"url": params["item"]["source_url"], "secrets": params.get("secrets")})
