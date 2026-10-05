"""Starter product alias table for normalization.

Maps canonical product names -> lowercase alias strings. An alias matches when
it appears as a substring of the lowercased listing title. Keep aliases
specific enough to avoid false positives (include the year + brand where
possible). Curated by hand; grows over time.
"""
# canonical_name -> {"sport": ..., "aliases": [...]}
PRODUCTS = {
    "2024 Panini Prizm Football Hobby": {
        "sport": "football",
        "aliases": [
            "2024 prizm football hobby", "2024 panini prizm football",
            "prizm fb hobby", "2024 prizm fb",
        ],
    },
    "2024 Panini Prizm Football No Huddle": {
        "sport": "football",
        "aliases": ["2024 prizm football no huddle", "prizm no huddle football"],
    },
    "2024 Donruss Football Hobby": {
        "sport": "football",
        "aliases": ["2024 donruss football hobby", "2024 donruss fb hobby"],
    },
    "2024 Panini Mosaic Football Hobby": {
        "sport": "football",
        "aliases": ["2024 mosaic football hobby", "2024 panini mosaic fb"],
    },
    "2024 Panini Select Football Hobby": {
        "sport": "football",
        "aliases": ["2024 select football hobby", "2024 panini select fb"],
    },
    "2024 Panini Contenders Football Hobby": {
        "sport": "football",
        "aliases": ["2024 contenders football hobby", "2024 panini contenders fb"],
    },
    "2023 Panini Prizm Football Hobby": {
        "sport": "football",
        "aliases": ["2023 prizm football hobby", "2023 panini prizm fb"],
    },
    "2024 Panini Prizm Basketball Hobby": {
        "sport": "basketball",
        "aliases": ["2024 prizm basketball hobby", "2024 panini prizm bk"],
    },
    "2024 Panini Mosaic Basketball Hobby": {
        "sport": "basketball",
        "aliases": ["2024 mosaic basketball hobby", "2024 panini mosaic bk"],
    },
    "2023-24 Panini Prizm Basketball Hobby": {
        "sport": "basketball",
        "aliases": ["2023-24 prizm basketball", "23-24 prizm bk hobby"],
    },
    "2024 Panini Select Basketball Hobby": {
        "sport": "basketball",
        "aliases": ["2024 select basketball hobby", "2024 panini select bk"],
    },
    "2024 Bowman Chrome Baseball Hobby": {
        "sport": "baseball",
        "aliases": ["2024 bowman chrome hobby", "2024 bowman chrome baseball"],
    },
    "2024 Bowman Baseball Hobby": {
        "sport": "baseball",
        "aliases": ["2024 bowman hobby", "2024 bowman baseball"],
    },
    "2024 Topps Chrome Baseball Hobby": {
        "sport": "baseball",
        "aliases": ["2024 topps chrome hobby", "2024 topps chrome baseball"],
    },
    "2024 Topps Series 2 Baseball Hobby": {
        "sport": "baseball",
        "aliases": ["2024 topps series 2 hobby", "2024 topps s2 hobby"],
    },
    "2024 Panini Prizm Baseball Hobby": {
        "sport": "baseball",
        "aliases": ["2024 prizm baseball hobby", "2024 panini prizm bb"],
    },
    "2024 Panini Donruss Soccer Hobby": {
        "sport": "soccer",
        "aliases": ["2024 donruss soccer hobby", "2024 panini donruss soccer"],
    },
    "2024 Panini Prizm Premier League Soccer Hobby": {
        "sport": "soccer",
        "aliases": ["2024 prizm premier league", "2024 prizm epl hobby"],
    },
    "2023-24 Upper Deck Series 2 Hockey Hobby": {
        "sport": "hockey",
        "aliases": ["2023-24 upper deck series 2", "23-24 ud s2 hockey"],
    },
    "2024 Upper Deck Hockey Hobby": {
        "sport": "hockey",
        "aliases": ["2024 upper deck hockey hobby", "2024 ud hockey"],
    },
}
