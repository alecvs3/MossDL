from engine.providers.media import MediaProvider

_provider = MediaProvider()


def metadata(params):
    return _provider.metadata(params)
