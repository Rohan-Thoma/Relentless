"""Strict, composable Location & Work-Mode Policy Engine (OR / AND / XOR / NOT).

Fixes all legacy filtering flaws:
- Never treats 'hybrid' or 'apac' as 'remote'
- Never rewrites US state Indiana ('IN') into 'India'
- Detects geo-restricted remote strings ('San Francisco, CA or Remote, US', 'Remote - EMEA')
- Checks JD text for hard foreign work-authorization / clearance / 5-day-onsite blockers
- Evaluates arbitrary composable boolean rules (OR / AND / XOR / NOT)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from src.config import LocationPolicy, LocationRule

INDIAN_CITIES: set[str] = {
    "bangalore",
    "bengaluru",
    "hyderabad",
    "secunderabad",
    "chennai",
    "delhi",
    "new delhi",
    "ncr",
    "gurgaon",
    "gurugram",
    "noida",
    "greater noida",
    "faridabad",
    "mumbai",
    "navi mumbai",
    "thane",
    "pune",
    "kolkata",
    "ahmedabad",
    "jaipur",
    "kochi",
    "cochin",
    "thiruvananthapuram",
    "trivandrum",
    "coimbatore",
    "indore",
    "chandigarh",
    "mohali",
    "mysuru",
    "mysore",
    "nagpur",
    "vadodara",
    "visakhapatnam",
    "vizag",
    "bhubaneswar",
    "surat",
    "lucknow",
}

INDIAN_STATES: set[str] = {
    "karnataka",
    "telangana",
    "maharashtra",
    "tamil nadu",
    "uttar pradesh",
    "haryana",
    "west bengal",
    "gujarat",
    "kerala",
    "rajasthan",
    "andhra pradesh",
    "madhya pradesh",
    "punjab",
    "odisha",
}

US_STATE_NAMES: set[str] = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
}

US_STATE_ABBREVS: set[str] = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

US_MAJOR_CITIES: set[str] = {
    "san francisco", "new york", "nyc", "seattle", "austin", "boston",
    "chicago", "los angeles", "denver", "atlanta", "miami", "dallas",
    "houston", "palo alto", "mountain view", "menlo park", "sunnyvale",
    "san jose", "san diego", "portland", "minneapolis", "detroit",
    "philadelphia", "pittsburgh", "raleigh", "charlotte", "phoenix",
    "salt lake city", "indianapolis", "bloomington", "fort wayne",
    "south bend", "evansville", "washington, d.c.", "washington dc",
    "washington, dc", "arlington", "reston", "mclean", "bethesda",
    "bellevue", "redmond", "cambridge", "cupertino", "santa clara",
}

CITY_ALIASES: dict[str, str] = {
    "bengaluru": "bangalore",
    "gurugram": "gurgaon",
    "new delhi": "delhi",
    "ncr": "delhi",
    "secunderabad": "hyderabad",
    "navi mumbai": "mumbai",
    "cochin": "kochi",
    "trivandrum": "thiruvananthapuram",
    "mysuru": "mysore",
    "vizag": "visakhapatnam",
}

GLOBAL_REMOTE_TOKENS: tuple[str, ...] = (
    "worldwide",
    "anywhere",
    "global",
    "international",
    "any location",
    "work from anywhere",
    "wfa",
)

COUNTRY_PATTERNS: dict[str, list[str]] = {
    "india": [r"\bindia\b", r"\bbharat\b"],
    "us": [
        r"\bunited states\b",
        r"\bu\.s\.a\.?\b",
        r"\bu\.s\.?\b",
        r"\busa\b",
        r"\bus\b",
        r"\bnorth america\b",
        r"\bamericas\b",
    ],
    "canada": [r"\bcanada\b", r"\btoronto\b", r"\bvancouver\b", r"\bmontreal\b", r"\bottawa\b"],
    "uk": [r"\bunited kingdom\b", r"\bu\.k\.?\b", r"\buk\b", r"\blondon\b", r"\bmanchester\b", r"\bedinburgh\b", r"\bengland\b"],
    "eu": [
        r"\beuropean union\b", r"\beu\b", r"\beurope\b", r"\bgermany\b", r"\bberlin\b",
        r"\bmunich\b", r"\bfrance\b", r"\bparis\b", r"\bnetherlands\b", r"\bamsterdam\b",
        r"\bireland\b", r"\bdublin\b", r"\bspain\b", r"\bmadrid\b", r"\bbarcelona\b",
        r"\bpoland\b", r"\bwarsaw\b", r"\bsweden\b", r"\bstockholm\b", r"\bswitzerland\b",
        r"\bzurich\b", r"\bitaly\b", r"\bportugal\b", r"\blisbon\b",
    ],
    "emea": [r"\bemea\b"],
    "europe": [r"\beurope\b"],
    "australia": [r"\baustralia\b", r"\bsydney\b", r"\bmelbourne\b", r"\banz\b"],
    "latam": [r"\blatam\b", r"\blatin america\b", r"\bbrazil\b", r"\bmexico\b", r"\bargentina\b", r"\bcolombia\b"],
    "apac": [r"\bapac\b", r"\basia[- ]pacific\b"],
    "singapore": [r"\bsingapore\b"],
    "japan": [r"\bjapan\b", r"\btokyo\b"],
}

JD_FOREIGN_AUTH_BLOCKERS: list[tuple[str, str]] = [
    (r"\bmust be authorized to work in the (?:u\.?s\.?|united states)\b", "requires US work authorization"),
    (r"\b(?:u\.?s\.?|united states) citizen(?:ship)? (?:is )?required\b", "requires US citizenship"),
    (r"\brequires? (?:an )?active (?:security|top secret|ts/sci|public trust) clearance\b", "requires US security clearance"),
    (r"\bmust reside in the (?:u\.?s\.?|united states)\b", "requires US residency"),
    (r"\bmust be (?:physically )?located in the (?:u\.?s\.?|united states)\b", "requires US location"),
    (r"\b(?:u\.?s\.?|united states) public sector\b", "US public sector role"),
    (r"\bright to work in the (?:u\.?k\.?|united kingdom)\b", "requires UK right to work"),
    (r"\b(?:eu|european union) work permit (?:is )?required\b", "requires EU work permit"),
    (r"\bno (?:visa )?sponsorship.*(?:u\.?s\.?|united states)\b", "no US sponsorship"),
]

JD_STRICT_ONSITE_PATTERNS: list[str] = [
    r"\b5 days (?:a|per) week in[- ]office\b",
    r"\b100%\s*on[- ]?site\b",
    r"\bfully on[- ]?site\b",
    r"\bin[- ]office 5 days\b",
]


@dataclass
class JobLocationFacts:
    raw_location: str
    work_mode: str = "unknown"  # remote | hybrid | onsite | unknown
    countries: set[str] = field(default_factory=set)
    cities: set[str] = field(default_factory=set)
    remote_regions: set[str] = field(default_factory=set)
    has_foreign_auth_blocker: bool = False
    blocker_reason: str | None = None


def normalize_city(name: str) -> str:
    low = (name or "").strip().lower()
    return CITY_ALIASES.get(low, low)


def normalize_location_text(loc: str) -> str:
    """Normalizes Indian city/state aliases WITHOUT falsely converting US Indiana ('IN') into 'India'."""
    text = (loc or "").lower().strip()
    if not text:
        return ""

    # Replace unambiguous city/state aliases
    aliases = {
        r"\bbengaluru\b": "bangalore",
        r"\bgurugram\b": "gurgaon",
        r"\bnew delhi\b": "delhi",
        r"\bncr\b": "delhi",
        r"\bsecunderabad\b": "hyderabad",
        r"\bnavi mumbai\b": "mumbai",
        r"\bkarnataka\b": "bangalore india",
        r"\btelangana\b": "hyderabad india",
        r"\bmaharashtra\b": "mumbai pune india",
        r"\btamil nadu\b": "chennai india",
    }
    for pat, repl in aliases.items():
        text = re.sub(pat, repl, text)

    # Only expand ', in' or trailing ' in' to 'india' when accompanied by an Indian city/state
    # (NEVER convert 'Indianapolis, IN' or 'Bloomington, IN' into India!)
    has_indian_anchor = any(
        re.search(r"\b" + re.escape(tok) + r"\b", text)
        for tok in (INDIAN_CITIES | INDIAN_STATES)
    )
    has_us_anchor = (
        any(re.search(r"\b" + re.escape(c) + r"\b", text) for c in US_MAJOR_CITIES)
        or "indiana" in text
        or bool(re.search(r"\b(usa|u\.s\.a\.?|united states)\b", text))
    )
    if has_indian_anchor and not has_us_anchor:
        text = re.sub(r",\s*in\b", ", india", text)
        text = re.sub(r"\bin\s*$", "india", text)

    return text


def extract_job_location_facts(
    title: str = "",
    location: str = "",
    jd_text: str = "",
    is_remote_hint: bool | None = None,
    candidate_country: str = "india",
    check_jd_geo_blockers: bool = True,
) -> JobLocationFacts:
    """Extracts structured work_mode, countries, cities, remote_regions, and JD geo blockers."""
    raw_loc = (location or "").strip()
    loc_low = raw_loc.lower()
    title_low = (title or "").lower()
    combined_meta = f"{title_low} | {loc_low}"
    norm_text = normalize_location_text(raw_loc)

    facts = JobLocationFacts(raw_location=raw_loc)

    # 1. Detect cities (Indian + US major)
    for city in INDIAN_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", norm_text):
            facts.cities.add(normalize_city(city))
            facts.countries.add("india")

    for state in INDIAN_STATES:
        if re.search(r"\b" + re.escape(state) + r"\b", loc_low):
            facts.countries.add("india")

    # 2. Detect US states & US cities carefully (including 'Indianapolis, IN' -> US!)
    for us_city in US_MAJOR_CITIES:
        if re.search(r"\b" + re.escape(us_city) + r"\b", loc_low):
            facts.countries.add("us")

    for us_state in US_STATE_NAMES:
        if re.search(r"\b" + re.escape(us_state) + r"\b", loc_low):
            facts.countries.add("us")

    # Match ', XX' 2-letter US state codes (e.g., ', CA', ', NY', ', TX', ', IN', ', D.C.')
    if re.search(r",\s*d\.?c\.?\b", loc_low):
        facts.countries.add("us")

    for match in re.finditer(r",\s*([a-z]{2})\b", loc_low):
        code = match.group(1)
        if code == "in":
            # Disambiguate ', IN': Indian city -> India, otherwise US Indiana!
            if "india" in facts.countries:
                continue
            facts.countries.add("us")
        elif code in US_STATE_ABBREVS:
            facts.countries.add("us")

    # 3. Detect explicit countries & macro regions
    for country, pats in COUNTRY_PATTERNS.items():
        if any(re.search(p, norm_text) or re.search(p, loc_low) for p in pats):
            facts.countries.add(country)

    # Also check title for explicit geo locks like 'U.S. Public Sector' or '(US)'
    if re.search(r"\b(u\.s\.|usa|united states|federal focus)\b", title_low):
        facts.countries.add("us")

    # 4. Determine strict work_mode (NEVER treat 'hybrid' or 'apac' as 'remote'!)
    has_hybrid = bool(re.search(r"\bhybrid\b", combined_meta))
    has_explicit_remote_token = bool(
        re.search(
            r"\b(remote|work from home|wfh|work from anywhere|wfa|telecommute|distributed|worldwide|anywhere|global)\b",
            combined_meta,
        )
    )
    has_full_remote = bool(
        re.search(r"\b(fully remote|100%\s*remote|remote[- ]first|remote only)\b", combined_meta)
    )
    has_or_remote = bool(re.search(r"\bor\s+remote\b", loc_low))
    has_onsite_token = bool(re.search(r"\b(on[- ]?site|in[- ]office|in person)\b", combined_meta))

    if has_hybrid and not has_full_remote and not has_or_remote:
        facts.work_mode = "hybrid"
    elif is_remote_hint is True or has_full_remote or has_or_remote or has_explicit_remote_token:
        facts.work_mode = "remote"
    elif has_onsite_token or facts.cities or facts.countries or raw_loc:
        facts.work_mode = "onsite"
    else:
        facts.work_mode = "unknown"

    # 5. Extract remote regions if work_mode == 'remote'
    if facts.work_mode == "remote":
        for gtok in GLOBAL_REMOTE_TOKENS:
            if re.search(r"\b" + re.escape(gtok) + r"\b", loc_low):
                facts.remote_regions.add(gtok)
        if re.search(r"\bapac\b|\basia[- ]pacific\b", loc_low):
            facts.remote_regions.add("apac")
        for c in facts.countries:
            facts.remote_regions.add(c)
        if not facts.remote_regions:
            facts.remote_regions.add("unspecified")

    # 6. Check JD text for hard foreign authorization blockers or strict 5-day onsite contradiction
    if check_jd_geo_blockers and jd_text:
        jd_low = jd_text.lower()
        cand_c = (candidate_country or "india").strip().lower()
        # Only flag foreign auth blocker if candidate_country isn't explicitly offered as eligible
        if cand_c not in facts.countries:
            for pat, reason in JD_FOREIGN_AUTH_BLOCKERS:
                if re.search(pat, jd_low):
                    facts.has_foreign_auth_blocker = True
                    facts.blocker_reason = reason
                    break

        # If metadata only had unspecified remote (e.g. from JobSpy query) but JD explicitly demands 5 days in-office
        if facts.work_mode == "remote" and facts.remote_regions == {"unspecified"}:
            if any(re.search(p, jd_low) for p in JD_STRICT_ONSITE_PATTERNS):
                facts.work_mode = "onsite"
                facts.remote_regions.clear()

    return facts


def _evaluate_rule(rule: LocationRule, facts: JobLocationFacts, reject_countries: set[str], candidate_country: str) -> bool:
    """Evaluates a single LocationRule (including nested all_of/any_of/xor_of/not_rule)."""
    # Nested boolean combinators
    if rule.not_rule is not None:
        if _evaluate_rule(rule.not_rule, facts, reject_countries, candidate_country):
            return False
    if rule.all_of:
        if not all(_evaluate_rule(sub, facts, reject_countries, candidate_country) for sub in rule.all_of):
            return False
    if rule.any_of:
        if not any(_evaluate_rule(sub, facts, reject_countries, candidate_country) for sub in rule.any_of):
            return False
    if rule.xor_of:
        matches = sum(1 for sub in rule.xor_of if _evaluate_rule(sub, facts, reject_countries, candidate_country))
        if matches != 1:
            return False

    # If rule ONLY had nested combinators and no leaf constraints, it passed above
    has_leaf = bool(
        rule.work_modes
        or rule.not_work_modes
        or rule.cities
        or rule.not_cities
        or rule.countries
        or rule.not_countries
        or rule.allowed_regions
    )
    if not has_leaf:
        return True

    # 1. Work mode constraints
    if rule.work_modes:
        allowed_modes = {m.strip().lower() for m in rule.work_modes}
        if facts.work_mode not in allowed_modes:
            return False

    if rule.not_work_modes:
        blocked_modes = {m.strip().lower() for m in rule.not_work_modes}
        if facts.work_mode in blocked_modes:
            return False

    # 2. Exclusions on cities / countries
    if rule.not_cities:
        blocked_cities = {normalize_city(c) for c in rule.not_cities}
        if facts.cities and facts.cities.issubset(blocked_cities):
            return False

    if rule.not_countries:
        blocked_countries = {c.strip().lower() for c in rule.not_countries}
        if facts.countries and facts.countries.issubset(blocked_countries):
            return False

    # 3. Remote region matching when this is a remote rule
    if facts.work_mode == "remote" and rule.allowed_regions:
        allowed_reg = {r.strip().lower() for r in rule.allowed_regions}
        # If job is geo-restricted to a rejected country (e.g., Remote, US) and NOT eligible region/candidate country:
        explicit_global = bool(facts.remote_regions & {"anywhere", "worldwide", "global", "international"})
        has_allowed_region = bool(facts.remote_regions & allowed_reg)
        is_unspecified = facts.remote_regions == {"unspecified"}

        if is_unspecified:
            if not rule.allow_unspecified_remote:
                return False
        elif not has_allowed_region and not explicit_global:
            return False

    # 4. City / country constraints (for hybrid/onsite or geo-targeted rules)
    if rule.cities:
        req_cities = {normalize_city(c) for c in rule.cities}
        if not (facts.cities & req_cities):
            return False

    if rule.countries:
        req_countries = {c.strip().lower() for c in rule.countries}
        if not (facts.countries & req_countries):
            return False

    return True


def evaluate_location_policy(
    policy: LocationPolicy,
    facts: JobLocationFacts,
) -> tuple[bool, str]:
    """Evaluates JobLocationFacts against a LocationPolicy, returning (accepted, reason)."""
    cand_country = (policy.candidate_country or "india").strip().lower()
    reject_set = {c.strip().lower() for c in (policy.reject_countries or [])}
    # Normalize common aliases in reject_countries
    if "usa" in reject_set or "united states" in reject_set:
        reject_set.add("us")
    if "united kingdom" in reject_set:
        reject_set.add("uk")

    # 1. Hard JD work-authorization blocker
    if policy.check_jd_geo_blockers and facts.has_foreign_auth_blocker:
        return False, f"blocked by JD geo/auth requirement ({facts.blocker_reason})"

    # 2. Global rejected-country check (unless candidate_country or global/worldwide is also present)
    explicit_global = bool(facts.remote_regions & {"anywhere", "worldwide", "global", "international"})
    if reject_set and facts.countries:
        has_rejected = bool(facts.countries & reject_set)
        has_eligible = (cand_country in facts.countries) or explicit_global
        if has_rejected and not has_eligible:
            bad = ", ".join(sorted(facts.countries & reject_set))
            return False, f"geo-restricted to rejected country/region ({bad})"

    # 3. Evaluate rules with policy.operator (OR / AND / XOR)
    if not policy.rules:
        return True, "no rules configured"

    rule_results = [
        (r.name or f"rule_{idx}", _evaluate_rule(r, facts, reject_set, cand_country))
        for idx, r in enumerate(policy.rules)
    ]
    matched_names = [name for name, ok in rule_results if ok]

    op = (policy.operator or "OR").upper()
    if op == "OR":
        if matched_names:
            return True, f"matched rule(s): {', '.join(matched_names)}"
        return False, f"no location rule matched (work_mode={facts.work_mode}, countries={sorted(facts.countries)}, cities={sorted(facts.cities)})"
    if op == "AND":
        if len(matched_names) == len(rule_results):
            return True, f"matched all rules ({', '.join(matched_names)})"
        failed = [name for name, ok in rule_results if not ok]
        return False, f"failed AND rules: {', '.join(failed)}"
    if op == "XOR":
        if len(matched_names) == 1:
            return True, f"matched exclusive XOR rule: {matched_names[0]}"
        return False, f"XOR expected exactly 1 rule match, got {len(matched_names)} ({matched_names})"

    return False, f"unsupported operator: {op}"


def evaluate_job_location(
    title: str,
    location: str,
    jd_text: str = "",
    is_remote_hint: bool | None = None,
    policy: LocationPolicy | None = None,
) -> tuple[bool, str, JobLocationFacts]:
    """Convenience entrypoint returning (accepted, reason, extracted_facts)."""
    pol = policy or LocationPolicy()
    facts = extract_job_location_facts(
        title=title,
        location=location,
        jd_text=jd_text,
        is_remote_hint=is_remote_hint,
        candidate_country=pol.candidate_country,
        check_jd_geo_blockers=pol.check_jd_geo_blockers,
    )
    ok, reason = evaluate_location_policy(pol, facts)
    return ok, reason, facts


def derive_jobboard_queries_from_policy(policy: LocationPolicy) -> list[dict[str, Any]]:
    """Derives clean JobSpy query specs [{'location': ..., 'is_remote': bool}] from LocationPolicy."""
    specs: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()

    def _add(loc: str, is_rem: bool) -> None:
        key = (loc.strip().lower(), is_rem)
        if loc.strip() and key not in seen:
            seen.add(key)
            specs.append({"location": loc.strip(), "is_remote": is_rem})

    for rule in policy.rules:
        modes = {m.strip().lower() for m in rule.work_modes}
        if "remote" in modes:
            # Query Remote explicitly with is_remote=True
            _add("Remote", True)
            if policy.candidate_country:
                _add(policy.candidate_country.title(), True)
        if modes & {"hybrid", "onsite"}:
            if rule.cities:
                country_suffix = f", {policy.candidate_country.title()}" if policy.candidate_country else ""
                for city in rule.cities:
                    _add(f"{city.title()}{country_suffix}", False)
            elif rule.countries:
                for c in rule.countries:
                    _add(c.title(), False)
            elif policy.candidate_country:
                _add(policy.candidate_country.title(), False)

    if not specs:
        _add("Remote", True)
    return specs
