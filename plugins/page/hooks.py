from engine.providers.page import PageProvider


def extract_links(params):
    return [item.to_dict() for item in PageProvider.extract_links(params["url"], params.get("secrets"))]
