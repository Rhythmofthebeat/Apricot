"""Location rules shared by every form that asks where someone or something is.

Structured the way job sites and applicant-tracking systems do it: country
first, then a state/province picker for countries that have a fixed list, then
a free-text city, then a postal code checked against that country's format.
The aim is that a country can't be typed into the city box and a postal code
can't be junk, without turning every field into a dropdown.
"""
import re

from .models import COUNTRIES

US_STATES = [
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"), ("CA", "California"),
    ("CO", "Colorado"), ("CT", "Connecticut"), ("DE", "Delaware"), ("DC", "District of Columbia"),
    ("FL", "Florida"), ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
    ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"), ("NV", "Nevada"),
    ("NH", "New Hampshire"), ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"), ("OK", "Oklahoma"), ("OR", "Oregon"),
    ("PA", "Pennsylvania"), ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
    ("WA", "Washington"), ("WV", "West Virginia"), ("WI", "Wisconsin"), ("WY", "Wyoming"),
    ("PR", "Puerto Rico"),
]
CANADA_PROVINCES = [
    ("AB", "Alberta"), ("BC", "British Columbia"), ("MB", "Manitoba"), ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"), ("NS", "Nova Scotia"), ("NT", "Northwest Territories"),
    ("NU", "Nunavut"), ("ON", "Ontario"), ("PE", "Prince Edward Island"), ("QC", "Quebec"),
    ("SK", "Saskatchewan"), ("YT", "Yukon"),
]
AUSTRALIA_STATES = [
    ("ACT", "Australian Capital Territory"), ("NSW", "New South Wales"), ("NT", "Northern Territory"),
    ("QLD", "Queensland"), ("SA", "South Australia"), ("TAS", "Tasmania"), ("VIC", "Victoria"),
    ("WA", "Western Australia"),
]

# Countries with a fixed list get a dropdown; everywhere else the region is
# free text, because inventing an incomplete list would be worse than none.
REGIONS = {
    "United States": US_STATES,
    "Canada": CANADA_PROVINCES,
    "Australia": AUSTRALIA_STATES,
}
REGION_LABELS = {"United States": "State", "Canada": "Province", "Australia": "State / Territory"}

# Postal code formats (anchored when applied). Countries not listed accept any
# short alphanumeric code; POSTAL_OPTIONAL countries don't use postal codes.
POSTAL_PATTERNS = {
    "United States": r"\d{5}(-\d{4})?",
    "Canada": r"[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d",
    "United Kingdom": r"[A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2}",
    "Ireland": r"[A-Za-z]\d{2}\s?[A-Za-z\d]{4}",
    "Australia": r"\d{4}",
    "New Zealand": r"\d{4}",
    "Germany": r"\d{5}",
    "France": r"\d{5}",
    "Spain": r"\d{5}",
    "Italy": r"\d{5}",
    "Netherlands": r"\d{4}\s?[A-Za-z]{2}",
    "Portugal": r"\d{4}-\d{3}",
    "Poland": r"\d{2}-\d{3}",
    "Sweden": r"\d{3}\s?\d{2}",
    "Switzerland": r"\d{4}",
    "India": r"\d{6}",
    "Singapore": r"\d{6}",
    "Philippines": r"\d{4}",
    "Pakistan": r"\d{5}",
    "Mexico": r"\d{5}",
    "Brazil": r"\d{5}-?\d{3}",
    "Argentina": r"([A-Za-z]\d{4}[A-Za-z]{3}|\d{4})",
}
POSTAL_EXAMPLES = {
    "United States": "94105", "Canada": "M5V 2T6", "United Kingdom": "SW1A 1AA", "Ireland": "D02 X285",
    "Australia": "2000", "Germany": "10115", "France": "75001", "India": "560001", "Brazil": "01310-100",
}
POSTAL_LOOSE = r"[A-Za-z0-9][A-Za-z0-9 \-]{1,9}"
POSTAL_OPTIONAL = {"United Arab Emirates"}

# Things people type into a city box that aren't a city.
_COUNTRY_ALIASES = {
    "us", "usa", "u.s.", "u.s.a.", "u s", "united states of america", "america", "uk", "u.k.", "gb",
    "great britain", "britain", "england", "uae", "u.a.e.",
}
# Compared with leading/trailing dots stripped, so "U.S.A." and "USA." both match.
COUNTRY_TERMS = {t.strip(".") for t in ({c.lower() for c in COUNTRIES if c.lower() != "other"} | _COUNTRY_ALIASES)}
GENERIC_TERMS = {"worldwide", "global", "anywhere", "remote", "n/a", "na", "none", "other", "unknown", "test"}


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def city_error(city):
    """Why this can't be a city (or None if it plausibly is)."""
    city = _clean(city)
    if not city:
        return "Enter your city."
    if len(city) < 2 or len(city) > 100:
        return "Enter a valid city name."
    if re.search(r"[@<>{}\\/]|https?:|www\.", city, re.I) or re.search(r"\d", city):
        return "A city name can't contain numbers or web addresses."
    low = city.lower().strip(".")
    if low in COUNTRY_TERMS:
        return f'"{city}" is a country, not a city - pick the country above and enter your city, e.g. Austin.'
    if low in GENERIC_TERMS:
        return f'"{city}" isn\'t a city - enter the town or city you\'re in, e.g. Austin.'
    return None


def place_error(place, field="location"):
    """For one-line "City, State/Country" fields (job location, profile
    "based in", company HQ). The first part must be a real city; a bare
    country is rejected, since that's the mistake this exists to catch."""
    place = _clean(place)
    if not place:
        return None
    if place.lower().strip(".") in COUNTRY_TERMS:
        return f'"{place}" is a country - add a city too, e.g. "Austin, TX" or "London, UK".'
    err = city_error(place.split(",")[0])
    if err and err != "Enter your city.":
        return err.replace("your city", "a city")
    return None


def postal_error(country, postal):
    postal = _clean(postal)
    if not postal:
        return None if country in POSTAL_OPTIONAL else "Enter your postal code."
    pattern = POSTAL_PATTERNS.get(country, POSTAL_LOOSE)
    if not re.fullmatch(pattern, postal):
        label = "ZIP code" if country == "United States" else "postal code"
        example = POSTAL_EXAMPLES.get(country)
        return f"That doesn't look like a valid {country} {label}" + (f" (e.g. {example})." if example else ".")
    return None


def region_error(country, region):
    region = _clean(region)
    if not region:
        return f"Select your {REGION_LABELS.get(country, 'state / province / region').lower()}."
    options = REGIONS.get(country)
    if options and region not in {code for code, _ in options} | {name for _, name in options}:
        return f"Select a valid {REGION_LABELS[country].lower()} from the list."
    return None


def work_location_error(auth):
    """First problem with the residence block of a work-authorization dict,
    or None. Mirrors the checks the apply form runs in the browser."""
    if not isinstance(auth, dict):
        return "Work authorization details are missing."
    country = _clean(auth.get("country"))
    if country not in COUNTRIES or country == "Other":
        return "Select your country of residence."
    return (
        region_error(country, auth.get("state"))
        or city_error(auth.get("city"))
        or postal_error(country, auth.get("postal_code"))
    )


def rules_for_js():
    """Everything the apply form needs to run the same checks client-side."""
    return {
        "regions": {c: [{"code": code, "name": name} for code, name in opts] for c, opts in REGIONS.items()},
        "region_labels": REGION_LABELS,
        "postal_patterns": POSTAL_PATTERNS,
        "postal_loose": POSTAL_LOOSE,
        "postal_examples": POSTAL_EXAMPLES,
        "postal_optional": sorted(POSTAL_OPTIONAL),
        "country_terms": sorted(COUNTRY_TERMS),
        "generic_terms": sorted(GENERIC_TERMS),
    }
