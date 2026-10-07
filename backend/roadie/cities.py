"""The 12 candidate cities and their approximate coordinates (Roadie's own constants, not Qloo data).

Keys match ``interpretation.within`` in the saved where_popular files. The coordinates are
rough city centers, only good enough for a west-to-east ordering by longitude.
"""

CITIES = [
    "New York, NY",
    "Los Angeles, CA",
    "Chicago, IL",
    "Boston, MA",
    "Philadelphia, PA",
    "Washington, DC",
    "Atlanta, GA",
    "Austin, TX",
    "Dallas, TX",
    "Denver, CO",
    "Seattle, WA",
    "San Francisco, CA",
]

# (latitude, longitude)
CITY_COORDS = {
    "New York, NY": (40.71, -74.01),
    "Los Angeles, CA": (34.05, -118.24),
    "Chicago, IL": (41.88, -87.63),
    "Boston, MA": (42.36, -71.06),
    "Philadelphia, PA": (39.95, -75.17),
    "Washington, DC": (38.91, -77.04),
    "Atlanta, GA": (33.75, -84.39),
    "Austin, TX": (30.27, -97.74),
    "Dallas, TX": (32.78, -96.80),
    "Denver, CO": (39.74, -104.99),
    "Seattle, WA": (47.61, -122.33),
    "San Francisco, CA": (37.77, -122.42),
}
