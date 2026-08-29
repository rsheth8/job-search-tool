#!/usr/bin/env python3
"""Build data/company_catalog.json — sector employer lists + known ATS boards.

References (names) come from public lists: CMS Medicare hospitals, US universities,
and US-listed companies by industry. Live probe tokens come from ats_boards.json.

Does not dump name-only employers into the rotating directory — those have no
public Greenhouse/Lever/Ashby board we can poll.

Usage:
    .venv/bin/python -m scripts.build_company_catalog
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUTPUT = ROOT / "data" / "company_catalog.json"
BOARDS_PATH = ROOT / "data" / "ats_boards.json"

NASDAQ_URL = (
    "https://raw.githubusercontent.com/Ate329/top-us-stock-tickers/main/tickers/all.csv"
)
UNIS_URL = (
    "https://raw.githubusercontent.com/Hipo/university-domains-list/master/"
    "world_universities_and_domains.json"
)
AIRPORTS_URL = (
    "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
)
CMS_HOSPITALS = "https://data.cms.gov/provider-data/api/1/datastore/query/xubh-q36u/0"
CMS_NURSING_HOMES = "https://data.cms.gov/provider-data/api/1/datastore/query/4pq5-n9py/0"
CMS_HOME_HEALTH = "https://data.cms.gov/provider-data/api/1/datastore/query/6jpm-sxkc/0"
HEALTHCARE_CAP = 2500

SECTOR_CAP = 1500

NASDAQ_INDUSTRY = {
    "Technology": "software",
    "Health Care": "science",
    "Finance": "finance",
    "Consumer Discretionary": "retail",
    "Consumer Staples": "retail",
    "Industrials": "manufacturing",
    "Energy": "energy",
    "Utilities": "energy",
    "Real Estate": "real_estate",
    "Telecommunications": "telecom",
    "Basic Materials": "manufacturing",
    "Miscellaneous": "logistics",
}

SKIP_NAME = re.compile(
    r"\b(ETF|ETN|ETP|Fund|Trust|Index|Warrant|Preferred|Notes|"
    r"Acquisition Corp|SPAC|Right|Units?|Bond|Depositary)\b",
    re.I,
)
SHARE_CLASS = re.compile(
    r"\s+(Common Stock|Capital Stock|Ordinary Shares|Class [A-Z].*|"
    r"American Depositary.*|Warrant.*)$",
    re.I,
)

# Known ATS boards that aren't "software" (tokens as stored in ats_boards.json).
BOARD_SECTORS: dict[tuple[str, str], list[str]] = {
    ("greenhouse", "stripe"): ["software", "finance"],
    ("greenhouse", "robinhood"): ["software", "finance"],
    ("greenhouse", "sofi"): ["software", "finance"],
    ("greenhouse", "affirm"): ["software", "finance"],
    ("greenhouse", "chime"): ["software", "finance"],
    ("greenhouse", "brex"): ["software", "finance"],
    ("greenhouse", "mercury"): ["software", "finance"],
    ("greenhouse", "glossier"): ["retail", "marketing"],
    ("ashby", "plaid"): ["software", "finance"],
    ("ashby", "ramp"): ["software", "finance"],
    ("ashby", "column"): ["software", "finance"],
    ("ashby", "unit"): ["software", "finance"],
    ("smartrecruiters", "AbbVie"): ["healthcare", "science"],
    ("smartrecruiters", "Intuitive"): ["healthcare", "science"],
    ("smartrecruiters", "NBCUniversal3"): ["media"],
    ("smartrecruiters", "BoschGroup"): ["manufacturing"],
    ("smartrecruiters", "Experian"): ["finance"],
    ("smartrecruiters", "Sandisk"): ["manufacturing"],
    ("smartrecruiters", "Thales"): ["manufacturing"],
    ("smartrecruiters", "WesternDigital"): ["manufacturing"],
    ("greenhouse", "andurilindustries"): ["aerospace", "software"],
    ("greenhouse", "rocketlab"): ["aerospace"],
    ("greenhouse", "waymo"): ["automotive", "software"],
    ("greenhouse", "nuro"): ["automotive", "software"],
    ("lever", "zoox"): ["automotive", "software"],
    ("greenhouse", "roblox"): ["gaming", "software"],
    ("greenhouse", "intercom"): ["support", "software"],
    ("greenhouse", "amplitude"): ["product", "software"],
    ("greenhouse", "mixpanel"): ["software", "product"],
    ("ashby", "notion"): ["product", "software"],
    ("ashby", "linear"): ["product", "software"],
    ("greenhouse", "twilio"): ["telecom", "software"],
    ("workable", "thorlabs"): ["science", "manufacturing"],
    ("workable", "grayce"): ["consulting"],
}

EXTRA_NAMES: dict[str, list[str]] = {
    "consulting": [
        "McKinsey & Company", "Boston Consulting Group", "Bain & Company",
        "Deloitte", "PwC", "EY", "KPMG", "Accenture", "Capgemini", "IBM Consulting",
        "Oliver Wyman", "Kearney", "Roland Berger", "LEK Consulting",
        "Booz Allen Hamilton", "Accenture Federal Services", "Guidehouse",
        "FTI Consulting", "Alvarez & Marsal", "West Monroe", "Slalom",
        "Thoughtworks", "Publicis Sapient", "Cognizant", "Infosys", "Wipro",
        "Tata Consultancy Services", "Cognizant Technology Solutions",
    ],
    "legal": [
        "Kirkland & Ellis", "Latham & Watkins", "DLA Piper", "Baker McKenzie",
        "Skadden", "Jones Day", "Sidley Austin", "White & Case", "Ropes & Gray",
        "Gibson Dunn", "Morgan Lewis", "Hogan Lovells", "Norton Rose Fulbright",
        "Dentons", "Greenberg Traurig", "Cooley", "Wilson Sonsini",
        "Fenwick & West", "Gunderson Dettmer", "Paul Weiss", "Simpson Thacher",
        "Sullivan & Cromwell", "Davis Polk", "Cravath", "Wachtell Lipton",
        "Quinn Emanuel", "WilmerHale", "Covington & Burling", "Perkins Coie",
        "Orrick", "Goodwin Procter", "Morrison & Foerster", "Reed Smith",
    ],
    "hospitality": [
        "Marriott International", "Hilton", "Hyatt", "IHG Hotels & Resorts",
        "Wyndham Hotels", "Choice Hotels", "Accor", "Airbnb", "Booking Holdings",
        "Expedia Group", "Starbucks", "McDonald's", "Chipotle", "Yum Brands",
        "Darden Restaurants", "Starbucks Coffee", "Compass Group", "Aramark",
        "Sodexo", "Delaware North", "MGM Resorts", "Caesars Entertainment",
        "Las Vegas Sands", "Wynn Resorts", "Royal Caribbean", "Carnival Cruise Line",
        "Delta Air Lines", "United Airlines", "American Airlines", "Southwest Airlines",
        "Four Seasons Hotels", "Ritz-Carlton", "Waldorf Astoria",
    ],
    "media": [
        "The New York Times", "The Washington Post", "The Wall Street Journal",
        "CNN", "NPR", "NBC News", "ABC News", "CBS News", "Fox News",
        "Bloomberg", "Reuters", "Associated Press", "The Atlantic",
        "The New Yorker", "Vox Media", "BuzzFeed", "Condé Nast",
        "Hearst", "Gannett", "News Corp", "Paramount", "Warner Bros. Discovery",
        "Disney", "Netflix", "Spotify", "The Athletic", "Politico",
    ],
    "marketing": [
        "WPP", "Omnicom", "Publicis Groupe", "Interpublic Group", "Dentsu",
        "Havas", "Ogilvy", "BBDO", "McCann", "TBWA", "Leo Burnett", "Saatchi & Saatchi",
        "VML", "R/GA", "AKQA", "Huge", "Droga5", "Wieden+Kennedy", "72andSunny",
        "Edelman", "Weber Shandwick", "FleishmanHillard", "Ketchum",
        "Golin", "BCW", "Hill+Knowlton", "Burson", "Cramer-Krasselt",
        "Horizon Media", "GroupM", "Mindshare", "MediaCom", "EssenceMediacom",
    ],
    "government": [
        "U.S. Department of Veterans Affairs", "Department of Defense",
        "Department of Health and Human Services", "Department of Homeland Security",
        "Department of Justice", "Department of State", "Department of Treasury",
        "Department of Agriculture", "Department of Energy", "Department of Education",
        "Department of Labor", "Department of Transportation", "Department of Interior",
        "Department of Commerce", "NASA", "National Institutes of Health",
        "Centers for Disease Control and Prevention", "FDA", "EPA", "FBI",
        "CIA", "NSA", "Social Security Administration", "IRS", "USPS",
        "National Park Service", "Forest Service", "Census Bureau",
        "Federal Reserve", "USAID", "Peace Corps", "National Science Foundation",
        "Library of Congress", "Smithsonian Institution", "GAO", "OMB",
    ],
    "nonprofit": [
        "Red Cross", "United Way", "American Cancer Society", "St. Jude Children's Research Hospital",
        "Doctors Without Borders", "UNICEF", "World Wildlife Fund", "The Nature Conservancy",
        "Feeding America", "Habitat for Humanity", "Salvation Army", "Goodwill",
        "Teach For America", "Khan Academy", "Kiva", "Charity: Water", "Gates Foundation",
        "Ford Foundation", "MacArthur Foundation", "Open Society Foundations",
        "Planned Parenthood", "Planned Parenthood Federation", "AARP",
        "Boys & Girls Clubs of America", "YMCA", "American Heart Association",
    ],
    "hr": [
        "Robert Half", "Adecco", "Randstad", "ManpowerGroup", "Kelly Services",
        "Insperity", "TriNet", "ADP", "Paychex", "Workday", "UKG", "Ceridian",
        "Greenhouse Software", "Lever", "Ashby", "iCIMS", "Jobvite",
        "Indeed", "LinkedIn", "Glassdoor", "ZipRecruiter", "Handshake",
    ],
    "logistics": [
        "UPS", "FedEx", "USPS", "DHL", "Amazon Logistics", "XPO Logistics",
        "J.B. Hunt", "Schneider National", "Werner Enterprises", "Old Dominion",
        "C.H. Robinson", "Expeditors", "Ryder", "Penske Logistics",
        "Maersk", "CMA CGM", "MSC", "Union Pacific", "BNSF Railway", "CSX",
        "Norfolk Southern", "Walmart Logistics", "Target Supply Chain",
    ],
    "insurance": [
        "UnitedHealth Group", "Elevance Health", "Cigna", "Humana", "Centene",
        "Kaiser Permanente", "Blue Cross Blue Shield", "Aetna", "MetLife",
        "Prudential", "New York Life", "Northwestern Mutual", "State Farm",
        "Allstate", "Progressive", "GEICO", "Liberty Mutual", "Travelers",
        "AIG", "Chubb", "The Hartford", "Nationwide", "USAA", "Lemonade",
    ],
    "design": [
        "IDEO", "Frog Design", "Pentagram", "Figma", "Adobe", "Canva",
        "Nike", "Apple", "Airbnb", "Shopify", "Mailchimp", "Webflow",
        "InVision", "Abstract", "Framer", "Miro", "Lucid", "Notion",
    ],
    "product": [
        "Figma", "Amplitude", "Mixpanel", "Pendo", "Productboard", "Aha!",
        "Atlassian", "Monday.com", "Asana", "Notion", "Linear", "Height",
        "LaunchDarkly", "Statsig", "Optimizely", "LaunchDarkly", "Reforge",
        "Pinterest", "Airbnb", "Stripe", "Shopify", "Slack",
    ],
    "support": [
        "Zendesk", "Intercom", "Freshworks", "Gladly", "Gorgias", "Kustomer",
        "Help Scout", "Salesforce", "HubSpot", "ServiceNow",
        "Teleperformance", "Concentrix", "TaskUs", "Foundever", "Alorica",
        "TTEC", "Sitel Group", "Liveops", "Working Solutions", "Sutherland",
        "Genpact", "Wipro", "Telus International",
    ],
    "construction": [
        "Bechtel", "Fluor", "Kiewit", "Turner Construction", "Skanska",
        "PCL Construction", "Clark Construction", "Whiting-Turner", "DPR Construction",
        "Hensel Phelps", "Gilbane", "Mortenson", "McCarthy Building",
        "Brasfield & Gorrie", "Suffolk Construction", "Holder Construction",
        "Walsh Group", "Granite Construction", "Tutor Perini", "Quanta Services",
        "EMCOR", "Comfort Systems USA", "MasTec", "MYR Group", "Primoris",
        "Caterpillar", "John Deere", "United Rentals", "Fastenal", "Ferguson",
        "W.W. Grainger", "AECOM", "Jacobs", "KBR", "Chicago Bridge & Iron",
        "Lennar", "D.R. Horton", "PulteGroup", "NVR", "Toll Brothers",
    ],
    "aerospace": [
        "Lockheed Martin", "RTX", "Boeing", "Northrop Grumman", "General Dynamics",
        "L3Harris", "Huntington Ingalls", "Textron", "Honeywell Aerospace",
        "SpaceX", "Blue Origin", "Relativity Space", "Rocket Lab", "Virgin Galactic",
        "Sierra Space", "Planet Labs", "Maxar", "Ball Aerospace", "Aerojet Rocketdyne",
        "Spirit AeroSystems", "Howmet Aerospace", "TransDigm", "HEICO", "Moog",
        "Garmin", "Iridium", "Anduril", "Palantir", "Firefly Aerospace",
        "AST SpaceMobile", "Intuitive Machines", "Axiom Space",
    ],
    "aviation": [
        "Delta Air Lines", "United Airlines", "American Airlines", "Southwest Airlines",
        "JetBlue", "Alaska Airlines", "Spirit Airlines", "Frontier Airlines",
        "Hawaiian Airlines", "Allegiant Air", "Sun Country Airlines", "Breeze Airways",
        "SkyWest Airlines", "Republic Airways", "Envoy Air", "Endeavor Air",
        "PSA Airlines", "Horizon Air", "Mesa Airlines", "GoJet", "CommuteAir",
        "FedEx Express", "UPS Airlines", "Atlas Air", "Kalitta Air", "Polar Air Cargo",
        "Federal Aviation Administration", "TSA", "National Transportation Safety Board",
        "Port Authority of New York and New Jersey", "Los Angeles World Airports",
        "Chicago Department of Aviation", "Massport", "DFW Airport",
        "Hartsfield-Jackson Atlanta International Airport",
        "AAR Corp", "StandardAero", "Lufthansa Technik", "Delta TechOps",
        "GE Aerospace", "Pratt & Whitney", "Rolls-Royce", "Collins Aerospace",
        "Embry-Riddle Aeronautical University", "ATP Flight School", "CAE",
        "FlightSafety International", "NetJets", "Flexjet", "Wheels Up",
        "Boeing", "Airbus", "Embraer", "Bombardier",
    ],
    "automotive": [
        "General Motors", "Ford", "Stellantis", "Toyota", "Honda", "Nissan",
        "Hyundai", "Tesla", "Rivian", "Lucid Motors", "Cummins", "PACCAR",
        "BorgWarner", "Aptiv", "Magna International", "Lear", "Adient",
        "Autoliv", "Gentex", "Mobileye", "AutoNation", "Lithia Motors",
        "Penske Automotive", "CarMax", "Carvana", "NIO", "VinFast",
        "Harley-Davidson", "Polaris", "Oshkosh", "Navistar",
    ],
    "telecom": [
        "Verizon", "AT&T", "T-Mobile", "Comcast", "Charter Communications",
        "Cox Communications", "Altice USA", "Lumen", "Frontier Communications",
        "DISH Network", "EchoStar", "Iridium", "Crown Castle", "American Tower",
        "SBA Communications", "Ericsson", "Nokia", "Motorola Solutions",
        "Cisco", "Juniper Networks", "Arista Networks", "Ciena",
    ],
    "architecture": [
        "Gensler", "Perkins&Will", "HOK", "Skidmore Owings & Merrill",
        "Foster + Partners", "Stantec", "HDR", "WSP", "Thornton Tomasetti",
        "Arup", "HKS", "NBBJ", "Populous", "Perkins Eastman", "ZGF Architects",
        "Kohn Pedersen Fox", "CallisonRTKL", "CannonDesign", "SmithGroup",
        "HNTB", "Kimley-Horn", "Tetra Tech", "Burns & McDonnell",
    ],
    "gaming": [
        "Electronic Arts", "Activision Blizzard", "Take-Two Interactive",
        "Ubisoft", "Epic Games", "Valve", "Riot Games", "Bungie", "Roblox",
        "Unity Technologies", "Niantic", "Scopely", "Zynga", "King",
        "Nintendo of America", "Sony Interactive Entertainment",
        "Xbox Game Studios", "Bethesda", "CD Projekt", "Square Enix",
        "Bandai Namco", "Sega", "Insomniac Games", "Naughty Dog", "Rockstar Games",
        "Respawn Entertainment", "Gearbox Software", "Discord",
    ],
    "sports": [
        "NFL", "NBA", "MLB", "NHL", "MLS", "NCAA", "ESPN", "IMG", "Endeavor",
        "DraftKings", "FanDuel", "Fanatics", "Nike", "Adidas", "Under Armour",
        "Dick's Sporting Goods", "Madison Square Garden", "Live Nation",
        "Formula 1", "UFC", "WWE", "NASCAR", "PGA Tour", "USTA",
    ],
    "fitness": [
        "Equinox", "Planet Fitness", "LA Fitness", "24 Hour Fitness",
        "Orangetheory Fitness", "SoulCycle", "Peloton", "Life Time",
        "Anytime Fitness", "F45", "Barry's", "CorePower Yoga", "Crunch Fitness",
        "Gold's Gym", "YMCA", "Club Pilates", "Pure Barre", "StretchLab",
    ],
    "veterinary": [
        "Banfield Pet Hospital", "VCA Animal Hospitals", "BluePearl",
        "Compassion-First Pet Hospitals", "National Veterinary Associates",
        "Petco", "PetSmart", "Chewy", "IDEXX", "Zoetis", "Elanco",
        "Veterinary Emergency Group", "Bond Vet", "Small Door Veterinary",
        "Heartland Veterinary Partners", "Pathway Vet Alliance",
    ],
}


def _get(url: str, **kwargs) -> httpx.Response:
    headers = {"User-Agent": "job-search-tool/catalog-builder"}
    resp = httpx.get(url, timeout=60.0, follow_redirects=True, headers=headers, **kwargs)
    resp.raise_for_status()
    return resp


def _clean_listed_name(name: str) -> str:
    t = SHARE_CLASS.sub("", name or "").strip()
    t = re.sub(r"\s+", " ", t).strip(" ,")
    return t


def fetch_nasdaq() -> dict[str, list[str]]:
    print("NASDAQ listed companies ...", flush=True)
    text = _get(NASDAQ_URL).text
    by: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        industry = (row.get("industry") or "").strip()
        sector = NASDAQ_INDUSTRY.get(industry)
        if not sector:
            continue
        name = _clean_listed_name(row.get("name") or "")
        if not name or SKIP_NAME.search(name):
            continue
        key = name.lower()
        bucket = seen.setdefault(sector, set())
        if key in bucket:
            continue
        bucket.add(key)
        by.setdefault(sector, []).append(name)
    for sector, names in by.items():
        by[sector] = names[:SECTOR_CAP]
        print(f"  {sector}: {len(by[sector])}", flush=True)
    return by


def fetch_cms_names(
    url: str, name_keys: tuple[str, ...], label: str, cap: int
) -> list[str]:
    print(f"{label} ...", flush=True)
    names: list[str] = []
    seen: set[str] = set()
    offset = 0
    page = 500
    while len(names) < cap:
        resp = _get(url, params={"limit": page, "offset": offset, "count": "false"})
        data = resp.json()
        rows = data.get("results") or []
        if not rows:
            break
        for row in rows:
            for key in name_keys:
                raw = (row.get(key) or "").strip()
                if not raw:
                    continue
                title = raw.title() if raw.isupper() else raw
                nk = title.lower()
                if nk in seen or len(title) < 3:
                    continue
                seen.add(nk)
                names.append(title)
                if len(names) >= cap:
                    break
            if len(names) >= cap:
                break
        offset += page
        if len(rows) < page:
            break
    print(f"  {label}: {len(names)}", flush=True)
    return names


def fetch_hospitals() -> list[str]:
    return fetch_cms_names(
        CMS_HOSPITALS, ("facility_name",), "CMS hospitals", SECTOR_CAP
    )


def fetch_universities() -> list[str]:
    print("US universities ...", flush=True)
    data = _get(UNIS_URL).json()
    names: list[str] = []
    seen: set[str] = set()
    for row in data:
        if (row.get("alpha_two_code") or "").upper() != "US":
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= SECTOR_CAP:
            break
    print(f"  education: {len(names)}", flush=True)
    return names


def fetch_airports() -> list[str]:
    """US large/medium airports from the public OurAirports dataset."""
    print("US airports ...", flush=True)
    text = _get(AIRPORTS_URL).text
    names: list[str] = []
    seen: set[str] = set()
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        if (row.get("iso_country") or "").upper() != "US":
            continue
        kind = (row.get("type") or "").strip().lower()
        if kind not in ("large_airport", "medium_airport"):
            continue
        name = (row.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        names.append(name)
        if len(names) >= SECTOR_CAP:
            break
    print(f"  aviation airports: {len(names)}", flush=True)
    return names


def _display_name(source: str, token: str) -> str:
    if source == "smartrecruiters":
        return re.sub(r"(\d+)$", "", token).replace("-", " ")
    return token.replace("-", " ").title()


def load_boards() -> list[dict]:
    boards: list[dict] = []
    if not BOARDS_PATH.exists():
        return boards
    data = json.loads(BOARDS_PATH.read_text(encoding="utf-8"))
    for source in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters"):
        for token in data.get(source) or []:
            tok = str(token).strip()
            if not tok:
                continue
            key = (source, tok)
            lookup_key = (source, tok if source == "smartrecruiters" else tok.lower())
            sectors = BOARD_SECTORS.get(lookup_key, ["software"])
            boards.append({
                "name": _display_name(source, tok),
                "source": source,
                "token": tok,
                "sectors": sectors,
            })
    return boards


def _merge_names(
    dst: dict[str, list[str]], sector: str, incoming: list[str], *, cap: int = SECTOR_CAP
) -> None:
    seen = {n.lower() for n in dst.get(sector, [])}
    out = list(dst.get(sector, []))
    for name in incoming:
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= cap:
            break
    dst[sector] = out


def main() -> int:
    names: dict[str, list[str]] = {}
    try:
        listed = fetch_nasdaq()
        for sector, lst in listed.items():
            _merge_names(names, sector, lst)
        _merge_names(names, "healthcare", fetch_hospitals(), cap=HEALTHCARE_CAP)
        remain = HEALTHCARE_CAP - len(names.get("healthcare") or [])
        if remain > 0:
            _merge_names(
                names,
                "healthcare",
                fetch_cms_names(
                    CMS_NURSING_HOMES,
                    ("chain_name", "provider_name"),
                    "CMS nursing homes",
                    remain,
                ),
                cap=HEALTHCARE_CAP,
            )
        remain = HEALTHCARE_CAP - len(names.get("healthcare") or [])
        if remain > 0:
            _merge_names(
                names,
                "healthcare",
                fetch_cms_names(
                    CMS_HOME_HEALTH,
                    ("provider_name", "facility_name"),
                    "CMS home health",
                    remain,
                ),
                cap=HEALTHCARE_CAP,
            )
        _merge_names(names, "education", fetch_universities())
        _merge_names(names, "aviation", fetch_airports())
    except httpx.HTTPError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 1

    for sector, extra in EXTRA_NAMES.items():
        _merge_names(names, sector, extra)

    boards = load_boards()
    for row in boards:
        for sector in row.get("sectors") or ["software"]:
            _merge_names(names, sector, [row["name"]])

    payload = {
        "version": 1,
        "boards": boards,
        "names": {k: v for k, v in sorted(names.items()) if v},
    }
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT}")
    print(f"  live ATS boards: {len(boards)}")
    for sector, lst in sorted(payload["names"].items()):
        print(f"  {sector}: {len(lst)} names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
