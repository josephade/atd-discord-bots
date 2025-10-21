def get_player_data(name: str):
    name = name.lower()
    players = {
        "lebron james": {
            "name": "LeBron James",
            "image": "https://cdn.nba.com/headshots/nba/latest/1040x760/2544.png",
            "prime_years": "2011–2018 (Miami Heat / Cleveland Cavaliers)",
            "bref": "https://www.basketball-reference.com/players/j/jamesle01.html",
            "nbarapm": "https://nbarapm.com/",
            "shotmap": "https://nbavisuals.com/shotmap?player=LeBron%20James&season=2011-18",

            # Default prime stats (for no year specified)
            "prime_stats": {
                "ppg": 27.4,
                "rpg": 8.2,
                "apg": 7.4,
                "fg": 52.1,
                "three_pt": 36.0,
                "three_pa": 4.7,
            },

            # Year-specific stats (examples)
            "season_stats": {
                "2012-2013": {
                    "ppg": 26.8,
                    "rpg": 8.0,
                    "apg": 7.3,
                    "fg": 56.5,
                    "three_pt": 40.6,
                    "three_pa": 3.3,
                },
                "2016-2017": {
                    "ppg": 26.4,
                    "rpg": 8.6,
                    "apg": 8.7,
                    "fg": 54.8,
                    "three_pt": 36.3,
                    "three_pa": 4.6,
                },
            },

            # Peak impact metrics for reference
            "impact_metrics": {
                "prime": {
                    "raptor": {"off": 9.3, "def": 3.2, "total": 12.6, "rank": 2},
                    "lebron": {"off": 7.3, "def": 1.5, "total": 8.8, "rank": 2},
                    "darko": {"off": 7.4, "def": 1.6, "total": 9.1, "rank": 3},
                },
                "2012-2013": {
                    "raptor": {"off": 8.6, "def": 2.9, "total": 11.5, "rank": 3},
                    "lebron": {"off": 7.0, "def": 1.4, "total": 8.4, "rank": 2},
                    "darko": {"off": 7.1, "def": 1.5, "total": 8.6, "rank": 3},
                },
            },

            "offense": (
                "**Playmaking:** One of the greatest ever at manipulating defenses. "
                "His vision from every spot on the floor made him a system unto himself.\n"
                "**Scoring Versatility:** Dominant downhill, improved midrange and post game, "
                "and excellent as a secondary shooter off ball.\n"
                "**Transition:** Unmatched pace control and power in open court.\n"
                "**Shooting:** While streaky early, became a reliable 3PT shooter during his Miami/Cavs years."
            ),
            "defense": (
                "**Versatility:** Could guard 1–5 effectively at his peak, switching across lineups.\n"
                "**Help Defense:** Elite anticipation — chasedown blocks defined a generation.\n"
                "**On-Ball:** Locked into key assignments in crunch time.\n"
                "**Defensive IQ:** Brilliant communicator and orchestrator, quarterbacked defenses."
            ),
        },
    }

    return players.get(name)
