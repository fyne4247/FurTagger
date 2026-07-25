"""Source-URL write policy for Hydrus routing.

Hash-tier MD5 hits produce byte-verified post URLs that may be handed to
Hydrus's URL downloader (notes/descriptions/timestamps). Perceptual and
external provenance URLs are only associated with the existing file.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Iterable, Set, Tuple


class UrlWritePolicy(Enum):
    """How verified source URLs should be written into Hydrus."""

    # Never queue the downloader — associate every URL with the file hash.
    ASSOCIATE_ONLY = "associate_only"
    # Queue FurTag's canonical hash-source post URLs for parser enrichment;
    # everything else is associated normally.
    ENRICH_HASH_POSTS = "enrich_hash_posts"


# Canonical post URLs emitted by FurTag's MD5 hash-tier lookups.
_ENRICHABLE_POST_URL = re.compile(
    r"^(?:"
    r"https://e621\.net/posts/\d+"
    r"|https://inkbunny\.net/s/\d+"
    r"|https://danbooru\.donmai\.us/posts/\d+"
    r"|https://gelbooru\.com/index\.php\?page=post&s=view&id=\d+"
    r")$"
)


def is_enrichable_post_url(url: str) -> bool:
    """True when *url* is a FurTag-generated hash-source post URL."""
    return bool(url and _ENRICHABLE_POST_URL.fullmatch(url))


def partition_urls(
        urls: Iterable[str],
        policy: UrlWritePolicy,
) -> Tuple[Set[str], Set[str]]:
    """Split *urls* into (enrich_candidates, associate_only).

    Enrich candidates still require Hydrus ``get_url_info`` (parseable post)
    before they are queued; non-candidates never enter the downloader.
    """
    all_urls = {u for u in urls if u}
    if policy is not UrlWritePolicy.ENRICH_HASH_POSTS:
        return set(), all_urls
    enrich = {u for u in all_urls if is_enrichable_post_url(u)}
    return enrich, all_urls - enrich
