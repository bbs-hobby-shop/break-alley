"""Team name -> sport mapping (Brian 2026-10-11).

Hundreds of break listings are titled "<TEAM> <product> break" with no
league keyword (e.g. "Kansas City Royals 2026 TOPPS UPDATE 4X HOBBY BOX
BREAK"). This table lets the sport detector resolve those. Matched
case-insensitively as whole phrases; aliases cover common shortenings.
"""

# phrase (lowercase) -> sport key (matches normalizer SPORTS)
TEAM_SPORTS: dict[str, str] = {}

def _add(sport: str, *names: str) -> None:
    for n in names:
        TEAM_SPORTS[n.lower()] = sport

# --- MLB (30) ---
_add("baseball",
     "arizona diamondbacks", "diamondbacks", "atlanta braves", "braves",
     "baltimore orioles", "orioles", "boston red sox", "red sox",
     "chicago cubs", "cubs", "chicago white sox", "white sox",
     "cincinnati reds", "reds", "cleveland guardians", "guardians",
     "colorado rockies", "rockies", "detroit tigers", "tigers",
     "houston astros", "astros", "kansas city royals", "royals",
     "los angeles angels", "angels", "los angeles dodgers", "dodgers",
     "miami marlins", "marlins", "milwaukee brewers", "brewers",
     "minnesota twins", "twins", "new york mets", "mets",
     "new york yankees", "yankees", "oakland athletics", "athletics",
     "philadelphia phillies", "phillies", "pittsburgh pirates", "pirates",
     "san diego padres", "padres", "san francisco giants", "giants",
     "seattle mariners", "mariners", "st. louis cardinals",
     "st louis cardinals", "cardinals", "tampa bay rays", "rays",
     "texas rangers", "rangers", "toronto blue jays", "blue jays",
     "washington nationals", "nationals")

# --- NFL (32) ---
_add("football",
     "arizona cardinals", "atlanta falcons", "falcons",
     "baltimore ravens", "ravens", "buffalo bills", "bills",
     "carolina panthers", "panthers", "chicago bears", "bears",
     "cincinnati bengals", "bengals", "cleveland browns", "browns",
     "dallas cowboys", "cowboys", "denver broncos", "broncos",
     "detroit lions", "lions", "green bay packers", "packers",
     "houston texans", "texans", "indianapolis colts", "colts",
     "jacksonville jaguars", "jaguars", "kansas city chiefs", "chiefs",
     "las vegas raiders", "raiders", "los angeles chargers", "chargers",
     "los angeles rams", "rams", "miami dolphins", "dolphins",
     "minnesota vikings", "vikings", "new england patriots", "patriots",
     "new orleans saints", "saints", "new york giants", "giants",
     "new york jets", "jets", "philadelphia eagles", "eagles",
     "pittsburgh steelers", "steelers", "san francisco 49ers", "49ers",
     "seattle seahawks", "seahawks", "tampa bay buccaneers", "buccaneers",
     "tennessee titans", "titans", "washington commanders", "commanders")

# --- NBA (30) ---
_add("basketball",
     "atlanta hawks", "hawks", "boston celtics", "celtics",
     "brooklyn nets", "nets", "charlotte hornets", "hornets",
     "chicago bulls", "bulls", "cleveland cavaliers", "cavaliers", "cavs",
     "dallas mavericks", "mavericks", "mavs", "denver nuggets", "nuggets",
     "detroit pistons", "pistons", "golden state warriors", "warriors",
     "houston rockets", "rockets", "indiana pacers", "pacers",
     "los angeles clippers", "clippers", "los angeles lakers", "lakers",
     "memphis grizzlies", "grizzlies", "miami heat", "heat",
     "milwaukee bucks", "bucks", "minnesota timberwolves", "timberwolves",
     "wolves", "new orleans pelicans", "pelicans",
     "new york knicks", "knicks", "oklahoma city thunder", "thunder",
     "orlando magic", "magic", "philadelphia 76ers", "76ers", "sixers",
     "phoenix suns", "suns", "portland trail blazers", "trail blazers",
     "blazers", "sacramento kings", "kings", "san antonio spurs", "spurs",
     "toronto raptors", "raptors", "utah jazz", "jazz",
     "washington wizards", "wizards")

# --- NHL (32) ---
_add("hockey",
     "anaheim ducks", "ducks", "arizona coyotes", "coyotes",
     "boston bruins", "bruins", "buffalo sabres", "sabres",
     "calgary flames", "flames", "carolina hurricanes", "hurricanes",
     "chicago blackhawks", "blackhawks", "colorado avalanche", "avalanche",
     "columbus blue jackets", "blue jackets", "dallas stars", "stars",
     "detroit red wings", "red wings", "edmonton oilers", "oilers",
     "florida panthers", "panthers", "los angeles kings", "kings",
     "minnesota wild", "wild", "montreal canadiens", "canadiens", "habs",
     "nashville predators", "predators", "new jersey devils", "devils",
     "new york islanders", "islanders", "new york rangers", "rangers",
     "ottawa senators", "senators", "philadelphia flyers", "flyers",
     "pittsburgh penguins", "penguins", "san jose sharks", "sharks",
     "seattle kraken", "kraken", "st. louis blues", "st louis blues",
     "blues", "tampa bay lightning", "lightning", "toronto maple leafs",
     "maple leafs", "leafs", "vancouver canucks", "canucks",
     "vegas golden knights", "golden knights", "washington capitals",
     "capitals", "winnipeg jets", "jets", "utah hockey club",
     "utah mammoth", "mammoth")

# --- EPL / Premier League (20) ---
_add("soccer",
     "arsenal", "aston villa", "bournemouth", "brentford",
     "brighton", "burnley", "chelsea", "crystal palace", "everton",
     "fulham", "leeds united", "leeds", "liverpool",
     "manchester city", "man city", "manchester united", "man united",
     "newcastle united", "newcastle", "nottingham forest",
     "sunderland", "tottenham", "spurs", "west ham", "wolves",
     "wolverhampton")

# --- MLS (30) ---
_add("soccer",
     "atlanta united", "austin fc", "charlotte fc",
     "chicago fire", "fc cincinnati", "colorado rapids",
     "columbus crew", "d.c. united", "dc united", "fc dallas",
     "houston dynamo", "sporting kansas city", "la galaxy", "galaxy",
     "lafc", "inter miami", "miami", "minnesota united",
     "cf montreal", "nashville sc", "new england revolution",
     "revolution", "new york city fc", "nycfc",
     "new york red bulls", "red bulls", "orlando city",
     "philadelphia union", "union", "portland timbers", "timbers",
     "real salt lake", "san diego fc", "san jose earthquakes",
     "earthquakes", "seattle sounders", "sounders",
     "st. louis city", "st louis city", "toronto fc",
     "vancouver whitecaps", "whitecaps")


def detect_team_sport(title: str) -> str | None:
    """Sport from a team-name mention, or None. Longest match wins so
    'st. louis cardinals' beats 'cardinals' (MLB) vs 'arizona cardinals'
    (NFL) — full names are checked before short aliases."""
    t = (title or "").lower()
    best: str | None = None
    best_len = 0
    for phrase, sport in TEAM_SPORTS.items():
        if len(phrase) > best_len and phrase in t:
            # whole-phrase guard: avoid matching inside longer words
            idx = t.find(phrase)
            before_ok = idx == 0 or not t[idx - 1].isalnum()
            end = idx + len(phrase)
            after_ok = end >= len(t) or not t[end].isalnum()
            if before_ok and after_ok:
                best, best_len = sport, len(phrase)
    return best
