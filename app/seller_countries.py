"""Home countries for roster sellers/breakers (Brian 2026-10-11).

Brian: every seller on every platform gets an explicit region — no NULLs.
YouTube is handled separately (snippet.country auto-fetch + KNOWN_COUNTRIES
in youtube_roster.py). This module covers Twitch, eBay, and Fanatics, which
have no API-provided country.

Only confident, evidence-backed entries live here. The health check flags any
breaker without a country so gaps get researched, not silently defaulted.
"""

# Twitch login (lowercased) -> 2-letter country
# Researched 2026-10-11 (web: Twitch bio -> website -> socials).
# "?" in comment = no positive non-US evidence; best-judgment US.
TWITCH_COUNTRIES: dict[str, str] = {
    "laytonsportscards": "US",    # 130 E Altamonte Dr, Altamonte Springs, FL
    "da_thelivelounge": "US",     # Dave & Adam's, Williamsville, NY
    "da_2bros1break": "US",       # Dave & Adam's channel
    "da_nerdout": "US",           # Dave & Adam's channel
    "steelcitybreakroom": "US",   # Steel City Collectibles, PA
    "cardboardhobby": "US",        # Erie, PA
    "vintagebreaks": "US",        # Millburn/Somerset, NJ
    "strykerbreaks": "US",        # Lapeer, MI
    "jaspysbreaks": "US",          # Hermosa Beach, CA
    "soccerbreakersfc": "US",      # "SHIPPING TO THE USA IS FREE"
    "dark_ecko_gaming": "US",      # USPS/UPS shipping, USD
    "thecardconomist": "US",       # "$9 USA flat rate"
    "cardsmiths": "US",            # Sacramento, CA
    "joshodeezy": "US",            # US shipments, EDT
    "thecoffeebreakers": "US",     # Los Angeles, CA
    "bamfbreakers": "US",          # roster: US
    "dearcindyphan": "US",         # roster: PandaTCG, US
    "honeyholebreaks": "US",       # roster: SoCal
    "thornswarehouse": "US",       # "Open to U.S. residents only"
    "joshscards": "US",            # Independence, MO
    "685media": "US",              # Boynton Beach, FL
    "ripnshipgaming": "US",        # Long Island, NY
    "lonestarpackbreaks": "US",    # Carrollton, TX
    "wildflowa": "US",             # CA LLC
    "likebutterlive": "US",        # ThePokePair LLC
    "pokeboyztrading": "US",       # ? no location found; English-language
    "kjs_live_hub": "US",          # ? no website/location found
    "rappcollect": "US",           # ? PT schedule; no stated location
    "tffbreaks": "GB",             # TFF BREAKS LIMITED, Liverpool, UK
    "roaringsensei": "GB",         # Newcastle upon Tyne, UK
    "dollysbreaks": "CA",          # Toronto area, ON (dollys.ca)
    "topshelfbreaks": "CA",        # Edmonton, AB (topshelfbreaks.ca)
    "mtgnorthrips": "CA",          # Goderich, ON (MTGNorth.ca)
    "pokemonbaribari": "JP",       # Tokyo, Japan
    "tcgpeppi": "NL",              # Dutch; TCGPeppi.nl
    "drewsbreaks": "GR",           # Thessaloniki, Greece
    "magictradersnl": "NL",        # Netherlands ("nl" in name)
    "packflipps": "CA",      # PackFlipps, Ontario, Canada (TCG sweep 2026-10-11)
    "packripstcg": "GR",     # PackRipsTCG, Greece (TCG sweep 2026-10-11)
    "retro_now": "US",       # Retro Now (TCG sweep 2026-10-11)
    "tcgtwan": "NL",         # TCGTwan, Netherlands (TCG sweep 2026-10-11)
    "tcgaddan": "SE",        # tcgaddan, Sweden (TCG sweep 2026-10-11)
    "gbkort": "NO",          # GBKort, Norway (TCG sweep 2026-10-11)
    "paymoneywubby": "US",   # PaymoneyWubby, San Diego (TCG sweep 2026-10-11)
    "ruxin34": "US",         # ruxin34, Ohio area (TCG sweep 2026-10-11)
    "rngmart": "AU",         # RNGmart, Australia (TCG sweep 2026-10-11)
    "dreamitcollectibles": "US",  # DreamItCollectibles, Southern California (Brian approved 2026-10-11, weekly discovery)
    "cardcollectivenz": "NZ",  # Card Collective, New Zealand (Brian approved 2026-10-12, weekly discovery)
    "thegamecapital": "US",  # The Game Capital, De Pere WI (Brian approved 2026-10-12, weekly discovery)
    "deriumandwifey": "US",  # Derium & Wifey, Cuyahoga Falls OH (Brian approved 2026-10-12, weekly discovery)
    "pushdich_tcg": "DE",  # pushdich_tcg, Germany (Brian approved 2026-10-12, weekly discovery PM)
    "mysticalspark": "CA",  # mysticalspark, Canada (Brian approved 2026-10-13, weekly discovery AM)
    "thejokershouse_live": "ZA",  # The Joker's House Live, South Africa (Brian approved 2026-10-13, weekly discovery AM)
    "ripwithc50g": "US",  # ripwithc50g, US (Brian approved 2026-10-13, weekly discovery AM)
    "ipoketcg": "US",  # iPokeTCG, US rip & ship (Brian approved 2026-10-13, weekly discovery overnight)
    "collectibleno": "NO",  # Collectible.no, Norway (Brian approved 2026-10-13, weekly discovery overnight)
    "migueltcgbreaks": "PT",  # MiguelTcgBreaks, Portugal (Brian approved 2026-10-13, weekly discovery overnight)
    "dannybartok": "US",  # dannybartok, US rip & ship (Brian approved 2026-10-13, weekly discovery PM)
    "trainer_trav": "US",  # trainer_trav, US shop (streams from Japan; Brian approved 2026-10-14, weekly discovery)
    "hhaxi": "US",  # hhaxi, US-only shop (Brian approved 2026-10-14, weekly discovery)
    "kaartkamer": "NL",  # KaartKamer, Netherlands-only webshop (Brian approved 2026-10-14, weekly discovery AM)
    "team_arti": "NL",  # Team Arti, Dutch rip & ship shops (Brian approved 2026-10-14, weekly discovery PM)
}
# eBay seller username (lowercased) -> 2-letter country
# Researched 2026-10-11 (eBay seller pages, listing locations, seller sites).
EBAY_SELLER_COUNTRIES: dict[str, str] = {
    "playoffpushsc_15": "US",  # picclick: "Location: Du Quoin, IL, US"
    "skolnsteelbreaks": "US",  # 4 listings "Located in United States" vs 1 stale "Located in Canada"
    "tombstonecollectibles": "US",  # eBay Live "Location: United States"; shop 2770 SW 27th Ave, Miami, F
    "941sportscards": "US",  # eBay Live "Location: United States"
    "1of1cardshop": "US",  # topratedseller "based in the United States"; shop 13221 Prospect Rd,
    "gamedaylegends": "US",  # eBay Live "Location: United States"
    "jackpotbreakers": "US",  # eBay Live "Location: United States"; jackpotbreakers.com
    "bodegacards": "US",  # eBay Live "Location: United States"; shop Hoboken/Jersey City, NJ
    "junkwaxvintage": "US",  # eBay Live "Location: United States"
    "cardlinesbreaks": "US",  # eBay Live "Location: United States"
    "bakersfield_hobby": "US",  # seed vetting note: Bakersfield CA
    "chomps_live_breaks": "US",  # picclick: "Location: Alma, AR, US"
    "chefssportscards": "US",  # YouTube "Based in United States"; chefssportscards.com
    "breaksntakes2": "US",  # eBay Live "Location: United States"
    "hobby_legends": "US",  # topratedseller "based in the United States"; shop 1277 W Central Ave
    "blezsportscards": "US",  # eBay Live "Location: United States"
    "smitty_ripz": "US",  # eBay Live "Location: United States"; event times in MST
    "bigtimecards_collectibles": "US",  # picclick: "Location: Saint Joseph, MO, US"
    "topptiercards": "US",  # listings "Located in United States"
    "dogfatherbreaks": "US",  # listings "Located in United States"
    "joshodeezy": "US",  # picclick: "Location: Bardstown, KY, US"
    "123tcg": "US",  # listings "from United States"
    "viprips": "US",  # listings "Located in United States"
    "thepullpenlive": "US",  # listings "from United States"
    "stlcards6": "US",  # listings "Located in United States" (STL = St. Louis)
    "bigwrangs": "US",  # listings "from United States"
    "thetcgbrothers": "US",  # listings "Located in United States"
    "adam-12!": "US",  # listings "from United States" (Greg's Cards Comics and Cars)
    "statelinebreaks1": "US",  # listings "Located in United States"
    "blackgoldsportscards": "US",  # listings "Located in United States"; eBay Live San Jose/SF
    "cajunbreaker2": "US",  # listings: "Youngsville, Louisiana, United States"
    "passtimebreaks": "US",  # listings "from United States"
    "pullwax": "US",  # item location "Miami, FL, United States"; retail store in Miami, FL
    "paradisecardbreaks": "US",  # listings "from United States"
    "lasordascardhouse": "US",  # listings "from United States"
    "gametimecardz": "US",  # listings "from United States"
    "thecoffeebreakers": "US",  # eBay Live "Location: United States"; Los Angeles location
    "ryans_cardhouse": "US",  # eBay Live "Location: United States"
    "onlycardsbreaks": "US",  # eBay Live "Location: United States"
    "unova_tcg": "US",  # eBay Live "Location: United States"
    "steelcitycollectibles1": "US",  # Steel City Collectibles, 1931 Lincoln Way, White Oak, PA 15131
    "cardboardhobby": "US",  # eBay Live "Location: United States"; Erie, PA
    "strykerbreaks": "US",  # PO Box 292, Lapeer, MI 48446 (strykerbreaks.com)
    "jaspyscasebreaks": "US",  # eBay Live "Location: United States"
    "topshelfbreakscards": "US",  # "Location: Elgin, Illinois, US"; streams Central time
    "filthbombbreaks": "US",  # 120 Glen Street, Glen Cove, NY 11542 (filthbombbreaks.com)
    "tombstone_rips": "US",  # 9770 W. Flamingo Road, Las Vegas, NV 89147
    "kos-cardbreaks": "US",  # eBay Live "Location: United States"
    "bmc.collectibles": "US",  # "Member since Nov-01-19 in United States"; listings "Located in Unit
    "pokestocked": "US",  # 221 1st Ave SW, Suite 610, Rochester MN 55902
    "internationalsportscards": "US",  # listings consistently "Located in United States" (name is branding o
    "thecardconomist": "US",  # "$9 USA flat rate" domestic shipping; Zelle/Venmo/CashApp
    "packhypecollectivellc": "US",  # listings "Located in United States"
    "pfootballpete4dhx": "US",  # eBay Live "Location: United States"
    "bombersportscards": "US",  # eBay Live "Location: United States"; Northeast Ohio
    "rj_duke_sports": "US",  # "McKinney, TX, US"
    "225breakers": "US",  # "Baton Rouge, LA, US"
    "breaksntakes": "US",  # eBay Live "Location: United States" (distinct from breaksntakes2)
    "bestcardbreaks": "US",  # listings "Located in United States"
    "grandsalamisportscards": "US",  # "Seattle, WA, US"
    "breakinglegends2318": "US",  # "Bethel, OK, US"
    "lmsboxbreaks": "US",  # eBay Live "Location: United States"
    "topnotchsportsclub": "US",  # seed vetting note: Mentor OH
    "bigapplebreakszn": "US",  # seed vetting note: NY
    "528breaks": "US",  # seed vetting note: MT
    "kingpinbreaks": "US",  # seed vetting note: Knoxville TN
    "breakscenter": "US",  # eBay Live "Location: United States"
    "mamabreaks": "US",  # eBay "Location: United States"; YouTube channel Country = US
    "gradientcollects": "US",  # eBay Live "Location: United States"
    "pearlslabs": "US",  # listings "Located in United States"
    "cardandallgaming": "US",  # seed vetting note: Raleigh NC
    "houseofcards-nola": "US",  # seed vetting note: NOLA
    "ksehd": "US",  # seed vetting note: CA
    "mojobreak": "US",  # Santa Clara, CA (mojobreak.com); eBay Live "Location: United States"
    "freshwaxbreaks": "US",  # listings "Located in United States"
    "mint_memorabilia": "US",  # eBay Live "Location: United States"; Lake Zurich, IL
    "hobbyshopbreaks": "US",  # listings "Located in United States"
    "apebreaks": "US",  # listings "Located in United States"; apebreaks.com
    "card-shop-live": "US",  # eBay Live "Location: United States"; Henderson, NV
    "cardvaultbreaks": "US",  # eBay Live "Location: United States"; CardVault US stores
    "48breaks": "US",  # listings "Located in United States"
    "sportscollectorandmore": "US",  # eBay Live "Location: United States"
    "honeyholecollect": "US",  # eBay Live "Location: United States"; San Diego County/Temecula, CA
    "otbreaks": "US",  # seed vetting note: San Diego CA
    "thecardboardjungle": "US",  # TopRatedSeller "based in the United States"
    "break_n_bad": "US",  # listings "Located in United States"
    "breakzonelive": "US",  # eBay Live "Location: United States"
    "sdsportscards23": "US",  # seed vetting note: San Diego card shop
    "overtimerips": "US",  # listings "Located in United States"
    "fastbreaktradingcards23": "US",  # listings "Located in United States" (eBay username fastbreaktradingc
    "gothamsportscards": "US",  # "Basking Ridge, NJ, US"
    "daddy_dubs": "US",  # listings "from United States"
    "chamaxcards": "CA",  # POSITIVE: topratedseller.com "based in Canada"; multiple listings "L
    "tffbreaks": "GB",  # POSITIVE: eBay UK Live; tffbreaks.com priced in GBP
    "uk_blue_eyes_tcg": "GB",  # POSITIVE: eBay UK Live "Location: United Kingdom"; Croydon, GB; Comp
    "morrisontradingpost": "CA",  # POSITIVE: eBay Live "Location: Canada"; runs events on ebay.ca
    "cardshub_it": "IT",  # POSITIVE: listings show "Reggio Emilia, IT" (confirms _it = Italy)
    "renegadecollectibles": "CA",  # POSITIVE: listings consistently "Located in Canada"
    "popeyscardbreaks": "AU",  # known: Australian seller, AFL breaks (not re-researched)
    "weedles_warehouse": "US",       # TCG sweep 2026-10-11
    "pokeswpz": "US",                # TCG sweep 2026-10-11
    "fabricatorsforge": "US",        # TCG sweep 2026-10-11
    "professordantcg": "GB",        # London, UK (TCG sweep 2026-10-11)
    "lakecardstore": "GB",          # UK (TCG sweep 2026-10-11)
    "cheeky-jigglypuff-cards": "GB",  # UK (TCG sweep 2026-10-11)
    "rarecandy.collectibles": "GB",  # UK (TCG sweep 2026-10-11)
    "alt-i-rip": "GB",              # UK (TCG sweep 2026-10-11)
    "swapitttt": "AU",               # Australia (TCG sweep 2026-10-11)
    "ozzie.collectables": "AU",      # Melbourne (TCG sweep 2026-10-11)
    "thewanderingcollectorau": "AU",  # Australia (TCG sweep 2026-10-11)
    "funkohub": "AU",                # Australia (TCG sweep 2026-10-11)
    "ccjamesinc": "CA",              # Montreal (TCG sweep 2026-10-11)
    "slabsharkslive": "CA",          # Canada (TCG sweep 2026-10-11)
    "card.colosseum": "US",         # Card Colosseum, eBay seller page shows Location: United States (99.5% pos, ~2.6K feedback) (Brian approved 2026-10-11, weekly discovery)
    "3rddowndisplays": "GB",        # 3rd Down Trading Card Shop, Keighley West Yorkshire UK (Brian approved 2026-10-11, weekly discovery) — moved from Fanatics map 2026-10-12 (eBay-only seller)
    "718breaks": "US",              # 718Breaks, Brooklyn NY (Brian approved 2026-10-12, weekly discovery) — moved from Fanatics map 2026-10-12 (eBay-only seller)
    "jetbreakers": "US",            # Jet Breakers, eBay Live NBA/NFL mixers (Brian approved 2026-10-12, weekly discovery) — moved from Fanatics map 2026-10-12 (eBay-only seller)
    "elites-breaks": "AU",          # Elites Breaks, Australia (Brian approved 2026-10-12, weekly discovery) — moved from Fanatics map 2026-10-12 (eBay-only seller)
    "northwestbreaks": "GB",        # Northwest Breaks, UK (Brian approved 2026-10-12, weekly discovery) — moved from Fanatics map 2026-10-12 (eBay-only seller)
    "maniasports": "GB",            # maniasports, UK (Brian approved 2026-10-12, weekly discovery PM)
    "midasbreaks": "GB",            # midasbreaks, UK (Brian approved 2026-10-12, weekly discovery PM)
    "gridcardsuk": "GB",            # gridcardsuk, UK (Brian approved 2026-10-12, weekly discovery PM)
    "westlondonjoe91": "GB",        # westlondonjoe91, UK (Brian approved 2026-10-12, weekly discovery PM)
    "tidy.collectables": "AU",      # Tidy Collectables, Australia (Brian approved 2026-10-13, weekly discovery AM)
    "jsr-collectables": "GB",       # JSR Collectables, UK (Brian approved 2026-10-13, weekly discovery AM)
    "gamedaylr": "US",              # GameDayLR, Little Rock AR (Brian approved 2026-10-13, weekly discovery AM)
    "replaysportscards": "US",      # Replay Sports Cards, US (Brian approved 2026-10-13, weekly discovery overnight)
    "yunaversetcg": "US",           # YunaverseTCG, US (Brian approved 2026-10-13, weekly discovery overnight)
    "savage.cards": "CA",           # Savage Cards, Piedmont QC (Brian approved 2026-10-13, weekly discovery overnight)
    "collect540": "CA",             # Collect540, Montreal (Brian approved 2026-10-13, weekly discovery overnight)
    "sportycardsit": "IT",          # Sporty Cards IT, Italy (Brian approved 2026-10-13, weekly discovery PM)
    "fgdef_uk_sportscards": "GB",   # FGDEF UK Sportscards, UK (Brian approved 2026-10-13, weekly discovery PM)
    "the-hobby-au": "AU",           # The Hobby AU, Australia (Brian approved 2026-10-13, weekly discovery PM)
    "camscards12": "US",            # Cam's Cards, Oklahoma City (Brian approved 2026-10-14, weekly discovery)
    "pristineworldwide": "CA",      # Pristine Worldwide, Kelowna BC (Brian approved 2026-10-14, weekly discovery)
    "hot_ice_breaks_collectables": "US",  # Hot Ice Breaks, New York (Brian approved 2026-10-14, weekly discovery)
    "breakmachine-it": "IT",        # Break Machine IT, Napoli (Brian approved 2026-10-14, weekly discovery AM)
    "eastside.collectables": "AU",  # Eastside Collectables, NSW (Brian approved 2026-10-14, weekly discovery AM)
    "sccardsroma": "IT",            # SC Cards Roma, Roma (Brian approved 2026-10-14, weekly discovery AM)
    "3gsports": "US",               # 3G Sports, Birmingham AL (Brian approved 2026-10-14, weekly discovery PM)
    "toyboxtradingco": "CA",        # Toybox Trading Co, Canada (Brian approved 2026-10-14, weekly discovery PM)
    "liaanaqc": "CA",               # LiaanaQC, Quebec (Brian approved 2026-10-14, weekly discovery PM)
}
# Fanatics Live shop name -> 2-letter country
# Researched 2026-10-11 (shop sites, addresses, web search).
FANATICS_SHOP_COUNTRIES: dict[str, str] = {
    "1of1 cardshop": "US",  # evidence: 1of1cardshop.com; ran "Topps Rip Party in Dallas, Texas"
    "225 breakers": "US",  # evidence: eBay seller 225breakers "Located in Baton Rouge, Louisiana
    "black gold sports cards": "US",  # evidence: 2327 Silvernail Road, Pewaukee, Wisconsin (blackgoldcards.
    "blez sports cards": "US",  # evidence: theBlez LLC; Las Vegas, Nevada
    "breaks n takes": "US",  # evidence: eBay seller breaksntakes, Gardena, CA "Located in United S
    "filthbomb breaks": "US",  # evidence: 116 Glen St, Glen Cove, NY
    "firehand cards": "US",  # evidence: Firehand Cards LLC, Newhall/Encino, California
    "game day legends": "US",  # evidence: 15121 Graham St #108, Huntington Beach, CA
    "layton sports cards": "US",  # evidence: store in Apopka/Altamonte Springs, Florida
    "lilpullman": "US",  # evidence: LILPULLMAN LLC, Aventura, Florida; on-site job posting in 
    "mojobreak": "US",  # evidence: 2060 Duane Ave, Santa Clara, California
    "paradise card breaks": "US",  # evidence: 6551 S Las Vegas Blvd, Las Vegas, NV (same-named shop also
    "pullwax": "US",  # evidence: 1950 NE 163rd St, North Miami Beach, Florida
    "skolnsteel breaks": "US",  # evidence: opened own card shop in Pinellas, Florida (YouTube Ep. 36)
    "state line breaks": "US",  # evidence: 34500 US-45, Third Lake, Illinois (statelinesportscards.co
    "swish breaks": "US",  # evidence: headquartered New York City; job postings "New York, NY, U
    "tombstone rips": "US",  # evidence: HQ 9770 W Flamingo Rd, Las Vegas NV; retail store Miami FL
    "viprips": "US",  # evidence: VIPRIPS LLC, Deerfield Beach / Boca Raton, Florida (per BB
    "wethehobby": "US",  # evidence: 23 Arlington St, Rochester, New York
    "dave & adam's": "US",  # evidence: known — dacardworld (Tonawanda NY area chain)
    "jaspy's": "US",  # evidence: known — jaspysbreaks, also on Twitch roster
    "thepullpen": "US",  # evidence: known
    "east west sports cards": "US",  # evidence: 2349 Vanderbilt Beach Rd, Naples, Florida (eastwestcards.c
    "suncoast sports": "US",  # evidence: studios in Tampa, Florida and Phoenix, Arizona
    "the monster card shop": "US",  # evidence: 18540 E San Tan Blvd #112, Queen Creek, Arizona (themonste
    "break'n bad": "US",  # evidence: eBay seller break_n_bad "Located in United States"; alread
    "collecting dust": "US",  # evidence: 6045 S Fort Apache, Las Vegas, Nevada
    "waxxed": "US",  # evidence: Waxxed Sports Cards, 3700 Vision Dr Ste 108, Fort Worth, T
    "freshwax": "US",  # evidence: Freshwax Inc, 8792 Fruitridge Road, Sacramento, California
    "rizo sports": "US",  # evidence: retail locations Santa Monica, Riverside, Murrieta, Califo
    "mitch's box breaks": "US",  # evidence: Mitch's Box Breaks LLC, 11235 N Cedarburg Rd, Mequon, Wisc
    "cardvault breaks": "US",  # evidence: CardVault by Tom Brady chain (Brady + US co-owners; stores
    "going twice": "US",  # evidence: GOING TWICE INC, 10321 Gravelly Lake Dr SW, Lakewood, Wash
    "dimetime sportscards": "US",  # evidence: eBay seller dimetime_sportscards, Gilbert, Arizona
    "vita breaks": "US",  # evidence: BreakRadar tags 🇺🇸 / "Fanatics Live Local shop"; show titl
    "cardboard47": "US",  # evidence: on-site/hybrid job posting in Aventura, Florida; "united u
    "headline breaks": "US",  # ? evidence: sports-cards co. behind "Headline Breaks" + "The Locker"
    "slabstox": "US",  # evidence: YouTube channel profile lists "United States" (slabstox.co
    "cult of wax": "US",  # evidence: Fanatics Live shop (IG @cult_of_wax); Topps release-day br
    "the wax culture": "US",  # evidence: PO Box 198, Victor, NY 14564; store at 2 West Main (thewax
    "lms box breaks": "US",  # ? evidence: socials IG @lmsboxbreaks / X @BoxLms; multi-platform US 
    "dave & adams uk": "GB",  # evidence: known — United Kingdom storefront
    "badger breaks": "US",           # TCG sweep 2026-10-11
    "bozeman breaks": "US",          # Bozeman, MT (TCG sweep 2026-10-11)
    "crossingtcg": "US",             # TCG sweep 2026-10-11
    "the card bar": "US",            # TCG sweep 2026-10-11
    "mama breaks": "US",             # TCG sweep 2026-10-11
    "gotham card breaks": "US",      # TCG sweep 2026-10-11
    "suhpreme cards": "US",          # TCG sweep 2026-10-11
    "queenz of cardz": "US",         # TCG sweep 2026-10-11
    "allsportsbreaks": "US",         # TCG sweep 2026-10-11
    "hit seekers sports cards": "US",  # TCG sweep 2026-10-11
    "brew town trading co": "US",    # TCG sweep 2026-10-11
    "coach chris cards": "US",       # TCG sweep 2026-10-11
    "wheelhouse breaks": "US",       # TCG sweep 2026-10-11
}
_MAPS = {
    "twitch": TWITCH_COUNTRIES,
    "ebay": EBAY_SELLER_COUNTRIES,
    "fanatics": FANATICS_SHOP_COUNTRIES,
}


def seller_country(source: str, seller_key: str | None) -> str | None:
    """Country for a seller, or None if not yet researched.

    source: 'twitch' | 'ebay' | 'fanatics'. seller_key is the login /
    username / shop name (case-insensitive).
    """
    if not seller_key:
        return None
    return _MAPS.get(source, {}).get(seller_key.strip().lower())
