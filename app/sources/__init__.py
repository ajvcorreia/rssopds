from ..models import Feed, SourceKind
from .base import FetchResult, RawItem, Source, SourceError
from .fediverse import FediverseSource
from .rss import RssSource

_SOURCES = {
    SourceKind.rss: RssSource(),
    SourceKind.fediverse: FediverseSource(),
}


def source_for(feed: Feed) -> Source:
    try:
        return _SOURCES[feed.kind]
    except KeyError:
        raise SourceError(f"no source adapter for kind={feed.kind}") from None


__all__ = ["FetchResult", "RawItem", "Source", "SourceError", "source_for"]
