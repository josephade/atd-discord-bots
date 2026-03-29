# player_data.py
# Player tiers and ATD-strategy flags.
# Positions are loaded from the local player_positions.py (ATD Draft Bot copy).
# Edit that file to restrict players to specific positions for drafting.

from player_positions import PLAYER_POSITIONS

_LOCAL_POSITIONS = {  # removed — using Team Sheet Bot's player_positions.py instead
    "Magic Johnson":        "PG",
    "Stephen Curry":        "PG/SG",
    "Chris Paul":           "PG",
    "Steve Nash":           "PG",
    "Oscar Robertson":      "PG",
    "John Stockton":        "PG",
    "Isiah Thomas":         "PG",
    "Gary Payton":          "PG",
    "Jason Kidd":           "PG",
    "Kyrie Irving":         "PG",
    "Russell Westbrook":    "PG",
    "Damian Lillard":       "PG",
    "Allen Iverson":        "PG/SG",
    "Chauncey Billups":     "PG",
    "Walt Frazier":         "PG/SG",
    "Bob Cousy":            "PG",
    "Pete Maravich":        "PG/SG",
    "Tiny Archibald":       "PG",
    "Dennis Johnson":       "PG/SG",
    "Penny Hardaway":       "PG/SG",
    "Rajon Rondo":          "PG",
    "Deron Williams":       "PG",
    "Trae Young":           "PG",
    "Luka Doncic":          "PG/SF",
    "Ja Morant":            "PG",
    "Derrick Rose":         "PG",
    "Baron Davis":          "PG",
    "Gilbert Arenas":       "PG",
    "Tim Hardaway Sr.":     "PG",
    "Mark Price":           "PG",
    "Kevin Johnson":        "PG",
    "Lenny Wilkens":        "PG",
    "Dave Bing":            "PG/SG",
    "Gus Williams":         "PG",
    "De'Aaron Fox":         "PG",
    "LaMelo Ball":          "PG",
    "John Wall":            "PG",
    "Tyrese Haliburton":    "PG",
    "Jalen Brunson":        "PG",
    # ── Shooting Guards ─────────────────────────────────────────────────
    "Michael Jordan":       "SG",
    "Kobe Bryant":          "SG",
    "Dwyane Wade":          "SG",
    "Ray Allen":            "SG",
    "Reggie Miller":        "SG",
    "Clyde Drexler":        "SG/SF",
    "Jerry West":           "SG",
    "Hal Greer":            "SG",
    "Sam Jones":            "SG",
    "Mitch Richmond":       "SG",
    "Joe Dumars":           "SG",
    "Sidney Moncrief":      "SG",
    "George Gervin":        "SG",
    "Klay Thompson":        "SG",
    "James Harden":         "SG",
    "Paul George":          "SG/SF",
    "Tracy McGrady":        "SG/SF",
    "Vince Carter":         "SG/SF",
    "Anfernee Hardaway":    "SG/SF",
    "Gail Goodrich":        "SG/PG",
    "Lou Hudson":           "SG",
    "Chet Walker":          "SG/SF",
    "Spencer Haywood":      "SF/PF",
    "Bob Love":             "SF",
    "Jimmy Butler":         "SG/SF",
    "Donovan Mitchell":     "SG",
    "Bradley Beal":         "SG",
    "Devin Booker":         "SG",
    "Jrue Holiday":         "SG",
    # ── Small Forwards ──────────────────────────────────────────────────
    "LeBron James":         "SF/PF",
    "Larry Bird":           "SF",
    "Kevin Durant":         "SF/PF",
    "Scottie Pippen":       "SF",
    "Julius Erving":        "SF",
    "Elgin Baylor":         "SF",
    "Rick Barry":           "SF",
    "John Havlicek":        "SF/SG",
    "Dominique Wilkins":    "SF",
    "Paul Pierce":          "SF",
    "Carmelo Anthony":      "SF",
    "Kawhi Leonard":        "SF",
    "Alex English":         "SF",
    "Bernard King":         "SF",
    "Bob Pettit":           "SF/PF",
    "Dave DeBusschere":     "SF/PF",
    "Billy Cunningham":     "SF/PF",
    "Gus Johnson":          "SF/PF",
    "Elvin Hayes":          "PF/C",
    "Dennis Rodman":        "PF/SF",
    "James Worthy":         "SF",
    "Rudy Gay":             "SF",
    "Paul George":          "SF/SG",
    "Grant Hill":           "SF",
    "Pippen":               "SF",
    "Andre Iguodala":       "SF",
    "Khris Middleton":      "SF",
    "Andrew Wiggins":       "SF",
    # ── Power Forwards ──────────────────────────────────────────────────
    "Tim Duncan":           "PF/C",
    "Karl Malone":          "PF",
    "Charles Barkley":      "PF",
    "Dirk Nowitzki":        "PF",
    "Kevin Garnett":        "PF/C",
    "Giannis Antetokounmpo":"PF/C",
    "Bob McAdoo":           "PF/C",
    "Dan Issel":            "PF/C",
    "Dave Cowens":          "C/PF",
    "Willis Reed":          "C/PF",
    "Dolph Schayes":        "PF",
    "Jerry Lucas":          "PF/C",
    "Horace Grant":         "PF",
    "Buck Williams":        "PF",
    "Amar'e Stoudemire":    "PF",
    "Chris Webber":         "PF",
    "Pau Gasol":            "PF/C",
    "Anthony Davis":        "PF/C",
    "Zion Williamson":      "PF",
    "Pascal Siakam":        "PF/SF",
    "Draymond Green":       "PF",
    "Blake Griffin":        "PF",
    "Chris Bosh":           "PF/C",
    "LaMarcus Aldridge":    "PF",
    "Rasheed Wallace":      "PF",
    "Antawn Jamison":       "PF/SF",
    "Tom Heinsohn":         "PF/SF",
    "Bob Boozer":           "PF",
    # ── Centers ─────────────────────────────────────────────────────────
    "Kareem Abdul-Jabbar":  "C",
    "Wilt Chamberlain":     "C",
    "Bill Russell":         "C",
    "Shaquille O'Neal":     "C",
    "Hakeem Olajuwon":      "C",
    "Moses Malone":         "C",
    "Patrick Ewing":        "C",
    "David Robinson":       "C",
    "Artis Gilmore":        "C",
    "Nate Thurmond":        "C",
    "Wes Unseld":           "C",
    "Bill Walton":          "C",
    "Bob Lanier":           "C",
    "Alonzo Mourning":      "C",
    "Dikembe Mutombo":      "C",
    "Robert Parish":        "C",
    "Dwight Howard":        "C",
    "Joel Embiid":          "C",
    "Nikola Jokic":         "C",
    "Bill Cartwright":      "C",
    "Walt Bellamy":         "C",
    "Wayne Embry":          "C",
    "Mel Daniels":          "C",
    "George Mikan":         "C",
    "Len Bias":             "SF/PF",
    "Ben Wallace":          "C/PF",
    "Marc Gasol":           "C",
    "Rudy Gobert":          "C",
    "Joakim Noah":          "C",
    "Clint Capela":         "C",
    "Brook Lopez":          "C",
    "Rik Smits":            "C",
}   # _LOCAL_POSITIONS (unused — PLAYER_POSITIONS comes from Team Sheet Bot)

# ── Tier Ratings ────────────────────────────────────────────────────────────
# Lower number = better. Unlisted players default to tier 11.
# Every AI team should have at least 1 player from each tier 1-10.
PLAYER_TIERS: dict[str, int] = {
    # ── Tier 1 ───────────────────────────────────────────────────────────────
    "Michael Jordan":           1,
    "LeBron James":             1,
    "Shaquille O'Neal":         1,
    "Stephen Curry":            1,
    "Kevin Garnett":            1,
    "Larry Bird":               1,
    "Magic Johnson":            1,
    "Kareem Abdul-Jabbar":      1,
    "Hakeem Olajuwon":          1,
    "Kevin Durant":             1,
    "Kobe Bryant":              1,
    "Jerry West":               1,
    "Tim Duncan":               1,
    "Nikola Jokic":             1,
    "Kawhi Leonard":            1,
    "Shai Gilgeous-Alexander":  1,
    "Dwyane Wade":              1,
    "David Robinson":           1,
    "Steve Nash":               1,
    "Chris Paul":               1,
    "Tracy McGrady":            1,
    "Oscar Robertson":          1,
    "Bill Walton":              1,
    "Giannis Antetokounmpo":    1,
    "Wilt Chamberlain":         1,
    "Anthony Davis":            1,
    "Joel Embiid":              1,
    "Julius Erving":            1,
    "Bill Russell":             1,
    "James Harden":             1,
    # ── Tier 2 ───────────────────────────────────────────────────────────────
    "Karl Malone":              2,
    "Dirk Nowitzki":            2,
    "Grant Hill":               2,
    "Jayson Tatum":             2,
    "Luka Doncic":              2,
    "Scottie Pippen":           2,
    "Reggie Miller":            2,
    "Draymond Green":           2,
    "Penny Hardaway":           2,
    "Charles Barkley":          2,
    "Paul George":              2,
    "Manu Ginobili":            2,
    "Walt Frazier":             2,
    "Clyde Drexler":            2,
    "Bob McAdoo":               2,
    "Jimmy Butler":             2,
    "Victor Wembanyama":        2,
    "Ray Allen":                2,
    "Paul Pierce":              2,
    "Dwight Howard":            2,
    "Damian Lillard":           2,
    "Mark Price":               2,
    "John Havlicek":            2,
    "Rick Barry":               2,
    "Chris Mullin":             2,
    "Sidney Moncrief":          2,
    "Vince Carter":             2,
    "Moses Malone":             2,
    "Willis Reed":              2,
    "Chauncey Billups":         2,
    # ── Tier 3 ───────────────────────────────────────────────────────────────
    "Patrick Ewing":            3,
    "Jason Kidd":               3,
    "Anthony Edwards":          3,
    "Shawn Kemp":               3,
    "Russell Westbrook":        3,
    "Klay Thompson":            3,
    "Terry Porter":             3,
    "Marques Johnson":          3,
    "Rasheed Wallace":          3,
    "Brandon Roy":              3,
    "Devin Booker":             3,
    "Rashard Lewis":            3,
    "Eddie Jones":              3,
    "Tyrese Haliburton":        3,
    "Deron Williams":           3,
    "Kyle Lowry":               3,
    "Larry Nance Sr.":          3,
    "John Stockton":            3,
    "Evan Mobley":              3,
    "Gary Payton":              3,
    "Peja Stojakovic":          3,
    "George Gervin":            3,
    "Shawn Marion":             3,
    "Andrei Kirilenko":         3,
    "Bobby Jones":              3,
    "Marc Gasol":               3,
    "Kevin Johnson":            3,
    "Joe Dumars":               3,
    "Alonzo Mourning":          3,
    "Dave Cowens":              3,
    # ── Tier 4 ───────────────────────────────────────────────────────────────
    "Pau Gasol":                4,
    "Victor Oladipo":           4,
    "James Worthy":             4,
    "Isiah Thomas":             4,
    "Jalen Williams":           4,
    "Jaylen Brown":             4,
    "Kyrie Irving":             4,
    "Mitch Richmond":           4,
    "Andre Iguodala":           4,
    "Khris Middleton":          4,
    "Mike Conley":              4,
    "Bob Lanier":               4,
    "Dan Majerle":              4,
    "Al Horford":               4,
    "Chris Bosh":               4,
    "Pascal Siakam":            4,
    "Jrue Holiday":             4,
    "Alex English":             4,
    "Bam Adebayo":              4,
    "Chet Holmgren":            4,
    "Rudy Gobert":              4,
    "Derrick Rose":             4,
    "Jaren Jackson Jr.":        4,
    "Derek Harper":             4,
    "Bradley Beal":             4,
    "Paul Millsap":             4,
    "Michael Cooper":           4,
    "Donovan Mitchell":         4,
    "Allen Iverson":            4,
    "Karl-Anthony Towns":       4,
    # ── Tier 5 ───────────────────────────────────────────────────────────────
    "Jalen Brunson":            5,
    "Mikal Bridges":            5,
    "Elgin Baylor":             5,
    "Shane Battier":            5,
    "Doug Christie":            5,
    "Jeff Hornacek":            5,
    "Derrick White":            5,
    "Zion Williamson":          5,
    "Danny Granger":            5,
    "Horace Grant":             5,
    "Bob Dandridge":            5,
    "Danny Green":              5,
    "Jack Sikma":               5,
    "Paul Pressey":             5,
    "Gordon Hayward":           5,
    "Lauri Markkanen":          5,
    "Lamar Odom":               5,
    "Chris Webber":             5,
    "Hersey Hawkins":           5,
    "Sam Jones":                5,
    "Lou Hudson":               5,
    "Desmond Bane":             5,
    "Kevin Love":               5,
    "David Thompson":           5,
    "Artis Gilmore":            5,
    "Michael Finley":           5,
    "Blake Griffin":            5,
    "Dennis Rodman":            5,
    "Baron Davis":              5,
    "Gilbert Arenas":           5,
    # ── Tier 6 ───────────────────────────────────────────────────────────────
    "Kevin McHale":             6,
    "Chet Walker":              6,
    "Joe Johnson":              6,
    "Kiki VanDeWeghe":          6,
    "OG Anunoby":               6,
    "Tim Hardaway Sr.":         6,
    "Dominique Wilkins":        6,
    "Cade Cunningham":          6,
    "Ron Artest":               6,
    "Darius Garland":           6,
    "Richard Hamilton":         6,
    "Connie Hawkins":           6,
    "Walter Davis":             6,
    "Ja Morant":                6,
    "Hedo Turkoglu":            6,
    "Joakim Noah":              6,
    "Tayshaun Prince":          6,
    "Reggie Lewis":             6,
    "Bernard King":             6,
    "Latrell Sprewell":         6,
    "Ron Harper":               6,
    "Steve Smith":              6,
    "Robert Horry":             6,
    "David West":               6,
    "Kyle Korver":              6,
    "Dennis Johnson":           6,
    "Gus Johnson":              6,
    "Luol Deng":                6,
    "Nicolas Batum":            6,
    "Goran Dragic":             6,
    # ── Tier 7 ───────────────────────────────────────────────────────────────
    "Alvan Adams":              7,
    "Jamal Murray":             7,
    "Terrell Brandon":          7,
    "Michael Redd":             7,
    "Wesley Matthews":          7,
    "Billy Cunningham":         7,
    "Sam Perkins":              7,
    "Tyson Chandler":           7,
    "Elton Brand":              7,
    "Gus Williams":             7,
    "Paul Westphal":            7,
    "Antonio McDyess":          7,
    "George Hill":              7,
    "Carmelo Anthony":          7,
    "Amar'e Stoudemire":        7,
    "Ben Wallace":              7,
    "Ivica Zubac":              7,
    "Boris Diaw":               7,
    "John Wall":                7,
    "Dikembe Mutombo":          7,
    "Robert Covington":         7,
    "Dick Van Arsdale":         7,
    "Hal Greer":                7,
    "Bob Pettit":               7,
    "Glen Rice":                7,
    "Trae Young":               7,
    "Bobby Phills":             7,
    "Tony Parker":              7,
    "Jamaal Wilkes":            7,
    "Nate Thurmond":            7,
    # ── Tier 8 ───────────────────────────────────────────────────────────────
    "Tyrese Maxey":             8,
    "Josh Howard":              8,
    "Scott Wedman":             8,
    "Josh Smith":               8,
    "Andrew Bogut":             8,
    "Lonzo Ball":               8,
    "Andrew Wiggins":           8,
    "Robert Parish":            8,
    "Kemba Walker":             8,
    "Sam Cassell":              8,
    "Franz Wagner":             8,
    "Clifford Robinson":        8,
    "Sleepy Floyd":             8,
    "Arvydas Sabonis":          8,
    "Jermaine O'Neal":          8,
    "Kirk Hinrich":             8,
    "Kristaps Porzingis":       8,
    "Brad Daugherty":           8,
    "Gerald Wallace":           8,
    "Jarrett Allen":            8,
    "Aaron Gordon":             8,
    "Fat Lever":                8,
    "Maurice Cheeks":           8,
    "Brent Barry":              8,
    "Sam Lacey":                8,
    "Dale Ellis":               8,
    "Detlef Schrempf":          8,
    "Isaiah Thomas":            8,
    "Jason Terry":              8,
    "Jamal Mashburn":           8,
    # ── Tier 9 ───────────────────────────────────────────────────────────────
    "Tiny Archibald":           9,
    "Byron Scott":              9,
    "Fred VanVleet":            9,
    "De'Aaron Fox":             9,
    "Dave DeBusschere":         9,
    "Danny Ainge":              9,
    "Rolando Blackman":         9,
    "Brook Lopez":              9,
    "Vlade Divac":              9,
    "Zach LaVine":              9,
    "LaMelo Ball":              9,
    "Alex Caruso":              9,
    "Otto Porter Jr.":          9,
    "Brian Taylor":             9,
    "Toni Kukoc":               9,
    "Yao Ming":                 9,
    "Bob Love":                 9,
    "Isaiah Hartenstein":       9,
    "Trevor Ariza":             9,
    "Elvin Hayes":              9,
    "Amen Thompson":            9,
    "Brandon Ingram":           9,
    "Steve Francis":            9,
    "Roger Brown":              9,
    "Wes Unseld":               9,
    "Marcus Smart":             9,
    "Wesley Person":            9,
    "Brad Miller":              9,
    "Michael Porter Jr.":       9,
    "Nene Hilario":             9,
    # ── Tier 10 ──────────────────────────────────────────────────────────────
    "Caron Butler":             10,
    "Jon McGlocklin":           10,
    "Phil Smith":               10,
    "Fred Brown":               10,
    "Derrick Coleman":          10,
    "Anthony Mason":            10,
    "Dan Roundfield":           10,
    "Gary Harris":              10,
    "Robert Williams":          10,
    "Richard Jefferson":        10,
    "Malcolm Brogdon":          10,
    "PJ Brown":                 10,
    "Mookie Blaylock":          10,
    "James Posey":              10,
    "Bill Laimbeer":            10,
    "Phil Chenier":             10,
    "Mike Miller":              10,
    "Mike Bibby":               10,
    "Marcus Camby":             10,
    "Allan Houston":            10,
    "Ralph Sampson":            10,
    "Rod Strickland":           10,
    "Kerry Kittles":            10,
    "Julius Randle":            10,
    "Jerome Kersey":            10,
    "Jason Richardson":         10,
    "Jaden McDaniels":          10,
    "Bo Outlaw":                10,
    "Scottie Barnes":           10,
    "Drazen Petrovic":          10,
}

# ── ATD Strategy Flags ──────────────────────────────────────────────────────
BALL_DOMINANT: set[str] = {
    "Michael Jordan", "Kobe Bryant", "LeBron James", "Magic Johnson",
    "Oscar Robertson", "Allen Iverson", "Russell Westbrook", "James Harden",
    "Carmelo Anthony", "Isiah Thomas", "Pete Maravich",
    "Damian Lillard", "Trae Young", "Luka Doncic",
    "Dwyane Wade", "Tiny Archibald", "Ja Morant", "John Wall", "Derrick Rose",
    "Giannis Antetokounmpo", "Tracy McGrady", "Penny Hardaway",
    "Gilbert Arenas", "Baron Davis",
    # Isolation-first wings who need the ball to generate offense
    "Dominique Wilkins", "Elgin Baylor", "Tracy McGrady",
    # High-usage guards who dominate the ball — can't easily share with another ball-dominant PG
    "Darius Garland", "De'Aaron Fox", "Kyrie Irving", "LaMelo Ball",
    "Tyrese Maxey", "Kemba Walker", "Stephen Curry",
    "Shai Gilgeous-Alexander", "Cade Cunningham", "Zach LaVine",
}

# ── Shot creators / go-to scorers ────────────────────────────────────────────
# Players who can reliably create their own shot and score at a high clip —
# the "go-to guy" concept. Every team needs at least one.
# Broader than BALL_DOMINANT: includes prolific wing/big scorers who aren't
# necessarily full-time ball-handlers (Gervin, Kawhi, Dirk, Carmelo, etc.).
SHOT_CREATORS: set[str] = {
    # Elite ball-dominant creators (Tier 1-2)
    "Michael Jordan", "Kobe Bryant", "LeBron James", "Magic Johnson",
    "Oscar Robertson", "Allen Iverson", "Russell Westbrook", "James Harden",
    "Carmelo Anthony", "Isiah Thomas", "Damian Lillard", "Trae Young",
    "Luka Doncic", "Dwyane Wade", "Ja Morant", "John Wall", "Derrick Rose",
    "Giannis Antetokounmpo", "Tracy McGrady", "Penny Hardaway",
    "Gilbert Arenas", "Tiny Archibald",
    "Kevin Durant", "Kawhi Leonard", "Stephen Curry", "Dirk Nowitzki",
    "Charles Barkley", "Karl Malone", "Hakeem Olajuwon", "Shaquille O'Neal",
    "Kareem Abdul-Jabbar", "Wilt Chamberlain",
    "Julius Erving", "Elgin Baylor", "Rick Barry", "George Gervin",
    "Dominique Wilkins", "Clyde Drexler", "Paul Pierce", "Vince Carter", "Jerry West",
    "Shai Gilgeous-Alexander", "Anthony Davis", "Joel Embiid", "Nikola Jokic",
    "Jayson Tatum", "Anthony Edwards", "Devin Booker", "Donovan Mitchell",
    "Bradley Beal", "Mitch Richmond", "Kyrie Irving",
    "Bob McAdoo", "Alex English", "Bernard King", "David Thompson",
    "Moses Malone", "David Robinson", "Patrick Ewing",
    "LaMarcus Aldridge", "Kevin Johnson", "Brandon Roy", "Jalen Williams",
    "Deron Williams", "Shawn Kemp", "Kemba Walker", "Jamal Murray", "Jaylen Brown", "Manu Ginobili",
    "Paul George", "Jimmy Butler", "Cade Cunningham",
    "Pascal Siakam", "Brandon Ingram", "Zion Williamson", "James Worthy",
    "Rashard Lewis", "Joe Johnson", "Jamal Mashburn", "Reggie Lewis", "Reggie Miller", "Ray Allen",
    # Tier 3+ self-creators — break down defense off the dribble / post
    "Marques Johnson", "Victor Oladipo", "Jalen Brunson",
    "Danny Granger", "Chris Webber", "Desmond Bane", "Michael Finley",
    "Blake Griffin", "Chet Walker", "Kiki VanDeWeghe", "Connie Hawkins",
    "Walter Davis", "Steve Smith", "Michael Redd", "Billy Cunningham",
    "Glen Rice", "Tony Parker", "Dale Ellis", "Detlef Schrempf",
    "Isaiah Thomas", "Jason Terry", "Zach LaVine", "LaMelo Ball",
    "Hedo Turkoglu",
}

# ── Three-point / spot-up shooters ──────────────────────────────────────────
# AI gives a bonus to these when the team lacks spacing.
SHOOTERS: set[str] = {
    "Stephen Curry", "Ray Allen", "Reggie Miller", "Klay Thompson",
    "Jerry West", "Steve Nash", "Mark Price", "Joe Dumars", "Sam Jones",
    "Tim Hardaway Sr.", "Gilbert Arenas", "Chris Paul", "James Harden",
    "Kyrie Irving", "John Stockton", "Damian Lillard", "Deron Williams",
    "Chauncey Billups", "Mitch Richmond", "Devin Booker", "Bradley Beal",
    "Donovan Mitchell", "Trae Young", "Tyrese Haliburton", "Jalen Brunson",
    "Mike Conley", "Kyle Lowry", "Fred VanVleet", "Kemba Walker",
    "Jamal Murray", "Goran Dragic", "Darius Garland", "Isaiah Thomas",
    "Sleepy Floyd", "Sam Cassell", "Terry Porter", "Scott Wedman",
    "Byron Scott", "Jeff Hornacek", "Hersey Hawkins", "Rolando Blackman",
    "Dell Curry", "Steve Smith", "Michael Finley", "Richard Hamilton",
    "Joe Johnson", "Michael Redd", "Hedo Turkoglu", "Eddie Jones",
    "Derek Harper", "Doug Christie", "Gary Harris", "Brent Barry",
    "Kirk Hinrich", "Dick Van Arsdale", "Bobby Phills", "Drazen Petrovic",
    "Dale Ellis", "Glen Rice", "LaMelo Ball", "Lonzo Ball",
    "Malcolm Brogdon", "Derrick White", "Desmond Bane", "Jamal Mashburn",
    "Kiki VanDeWeghe", "Wesley Matthews", "Wesley Person", "George Hill",
    "Victor Oladipo", "Brandon Roy", "Josh Howard", "Luol Deng",
    "Larry Bird", "Kevin Durant", "Paul George", "Kawhi Leonard",
    "Vince Carter", "Manu Ginobili", "Paul Pierce", "John Havlicek",
    "Carmelo Anthony", "Tracy McGrady", "Penny Hardaway",
    "Chris Mullin", "Rashard Lewis", "Dan Majerle", "Peja Stojakovic",
    "Jayson Tatum", "Jaylen Brown", "Andrew Wiggins", "Anthony Edwards",
    "Zach LaVine", "Toni Kukoc", "Detlef Schrempf", "Robert Horry",
    "Shane Battier", "Michael Cooper", "Danny Green", "Mikal Bridges",
    "OG Anunoby", "Danny Granger", "Robert Covington", "Gordon Hayward",
    "Brandon Ingram", "Michael Porter Jr.", "Herb Jones", "Joe Ingles",
    "Nicolas Batum", "Rasheed Wallace", "Khris Middleton", "Aaron Gordon",
    "Jalen Williams", "Sam Perkins",
    "Dirk Nowitzki", "Luka Doncic", "Joel Embiid", "Bob McAdoo",
    "Karl-Anthony Towns", "Kevin Love", "Lauri Markkanen", "Al Horford",
    "Paul Millsap", "Evan Mobley", "Pascal Siakam", "Jaren Jackson Jr.",
    "Chet Holmgren", "Brook Lopez", "J.J. Redick", "Mike Miller", "Kyle Korver",
    "Tyrese Haliburton", "LaMelo Ball", "Jalen Williams",
}

HIGH_PORTABILITY: set[str] = {
    "Ray Allen", "Reggie Miller", "Klay Thompson", "Scottie Pippen", "Tim Duncan", "Kevin Garnett",
    "Dirk Nowitzki", "Paul Pierce", "Shaquille O'Neal",
    "Kawhi Leonard", "Draymond Green", "Jimmy Butler", "Jrue Holiday",
    "Andre Iguodala", "John Stockton", "Chauncey Billups", "Joe Dumars", "Jason Kidd",
    "Nikola Jokic", "Pascal Siakam"
}

# ── Non-scoring / defensive specialists ──────────────────────────────────────
# Players whose value is primarily defense/rebounding/passing — not scoring.
# Drafting two of these (regardless of position) leaves the bench without a
# scoring punch. The AI penalises a second non-scorer on the same team.
NON_SCORING_BIGS: set[str] = {
    # Defensive bigs
    "Draymond Green", "Ben Wallace", "Dennis Rodman", "Dikembe Mutombo",
    "Rudy Gobert", "Joakim Noah", "Horace Grant", "Bill Walton", "Evan Mobley",
    "Marc Gasol", "Al Horford", "Robert Parish", "Dave Cowens", "Jaren Jackson Jr.",
    "Chet Holmgren", "Bobby Jones", "Paul Millsap", "Bam Adebayo", "Artis Gilmore",
    "Andrew Bogut", "Sam Lacey", "Arvydas Sabonis", "Bo Outlaw", "Brook Lopez",
    "Robert Williams", "Tyson Chandler", "Andrew Bogut", "Jarrett Allen",
    # Non-scoring wings/forwards
    "Shawn Marion",
    # Defensive wing / guard specialists (non-scorers)
    "Ron Artest", "Alex Caruso", "Marcus Smart", "Andre Iguodala",
    "Michael Cooper", "Shane Battier", "Danny Green", "Herb Jones",
    "Mikal Bridges", "OG Anunoby", "Jaden McDaniels",
    "Derek Harper", "Jerome Kersey", "Tayshaun Prince", "Bruce Bowen",
    "Luol Deng", "Tony Allen", "Patrick Beverley", "Alec Burks",
    "Rodney McCray", "Paul Pressey", "Quinn Buckner", "Maurice Cheeks",
}

# ── Soft bigs ─────────────────────────────────────────────────────────────────
# Offensive-first PF/C who lack the defensive anchor ability.
# Two soft bigs together = no rim protection, no defensive identity in the frontcourt.
# A soft big next to an immobile center is especially damaging.
SOFT_BIGS: set[str] = {
    "Karl-Anthony Towns", "Zion Williamson", "Chris Bosh",
    "Pau Gasol", "LaMarcus Aldridge", "Amar'e Stoudemire",
    "Bob McAdoo", "Dan Issel", "Walt Bellamy", "Bob Boozer",
    "Antawn Jamison", "Chris Webber", "Larry Kenon", "Shaquille O'Neal", "Nikola Jokic",
    "Moses Malone", "Bob Lanier", "Kevin Love", "Lauri Markkanen", "David West", "Blake Griffin",
    "Brad Daugherty", "Bob Pettit", "Vlade Divac", "Yao Ming", "Brad Miller", 
}

# ── Immobile centers ──────────────────────────────────────────────────────────
# Traditional bigs who dominate in the paint but can't guard the perimeter or
# cover for a soft PF. Pairing with a soft big = no defensive identity at all.
IMMOBILE_CENTERS: set[str] = {
    "Karl-Anthony Towns", "Zion Williamson", "Chris Bosh",
    "Pau Gasol", "LaMarcus Aldridge", "Amar'e Stoudemire",
    "Bob McAdoo", "Dan Issel", "Walt Bellamy", "Bob Boozer",
    "Antawn Jamison", "Chris Webber", "Larry Kenon", "Shaquille O'Neal", "Nikola Jokic",
    "Moses Malone", "Bob Lanier", "Kevin Love", "Lauri Markkanen", "David West", "Blake Griffin",
    "Brad Daugherty", "Bob Pettit", "Vlade Divac", "Yao Ming", "Brad Miller", 
    "Shaquille O'Neal", "Dwight Howard","Artis Gilmore",
    "Bob Lanier"
}

# ── Versatile defenders ───────────────────────────────────────────────────────
# Mobile, multi-positional defenders who can cover for immobile centers and
# soft bigs — the Larry Nance / Kirilenko / Marion type.
# The AI actively seeks these out when the frontcourt needs defensive help.
VERSATILE_DEFENDERS: set[str] = {
    "Kevin Garnett", "Scottie Pippen", "Kawhi Leonard", "Draymond Green",
    "Shawn Marion", "Andrei Kirilenko", "Bobby Jones", "Larry Nance Sr.",
    "Evan Mobley", "Anthony Davis", "Pascal Siakam", 
    "Paul George", "Jimmy Butler", "Dennis Rodman", "Dave DeBusschere",
    "Horace Grant", "Andre Iguodala", "Luol Deng",
    "Ben Wallace", "Victor Wembanyama", "Tim Duncan",
}

# ── Good-to-elite perimeter defenders ────────────────────────────────────────
# Guards and wings who can credibly guard the perimeter.
# Used to detect weak defensive backcourts: if 2+ of the starting PG/SG/SF
# are NOT on this list, the team needs defensive compensation via the frontcourt
# and bench.
PERIMETER_DEFENDERS: set[str] = {
    # Elite guard defenders
    "Michael Jordan", "Jerry West", "Kobe Bryant", "Dwyane Wade",
    "Shai Gilgeous-Alexander", "Chris Paul", "Oscar Robertson",
    "Walt Frazier", "Manu Ginobili", "Chauncey Billups",
    "Sidney Moncrief", "Terry Porter", "Kyle Lowry", "Jason Kidd",
    "Russell Westbrook", "Joe Dumars", "Gary Payton", "Victor Oladipo",
    "John Stockton", "Mike Conley", "Jrue Holiday", "Michael Cooper",
    "Derek Harper", "Paul Pressey", "Derrick White", "Baron Davis",
    "Gus Williams", "Dennis Johnson", "Ron Harper", "Cade Cunningham",
    "George Hill", "Lonzo Ball", "John Wall", "Maurice Cheeks",
    "Kirk Hinrich", "Marcus Smart", "Alex Caruso", "Mookie Blaylock",
    "Nate McMillan", "Brian Taylor", "Phil Smith", "Amen Thompson",
    # Wing and forward perimeter defenders
    "Kawhi Leonard", "Paul George", "Jimmy Butler", "Paul Pierce",
    "John Havlicek", "Vince Carter", "Klay Thompson", "Eddie Jones",
    "Jaylen Brown", "Andre Iguodala", "Dan Majerle", "Jalen Williams",
    "Mikal Bridges", "Doug Christie", "Danny Green", "Reggie Lewis",
    "Nicolas Batum", "Latrell Sprewell", "Wesley Matthews", "Bobby Phills",
    "Andrew Wiggins", "Josh Howard", "Scott Wedman", "Gary Harris",
    "Herb Jones", "Jerry Sloan", "Caron Butler", "Dorian Finney-Smith",
    "Trey Murphy",
}

# ── Elite rim protectors ──────────────────────────────────────────────────────
# Shot-blockers and defensive anchors who protect the paint.
# Every team needs at least one — if none are on the roster by round 5,
# the AI will actively seek one out.
ELITE_RIM_PROTECTORS: set[str] = {
    "Bill Russell", "Hakeem Olajuwon", "Kevin Garnett", "Tim Duncan",
    "Dikembe Mutombo", "Rudy Gobert", "Anthony Davis", "Victor Wembanyama",
    "Ben Wallace", "David Robinson", "Alonzo Mourning", "Joel Embiid",
    "Patrick Ewing", "Nate Thurmond", "Wes Unseld", "Artis Gilmore",
    "Dwight Howard", "Joakim Noah", "Evan Mobley", "Giannis Antetokounmpo",
    "Kareem Abdul-Jabbar",
    "Wilt Chamberlain", "Bill Walton", "Shawn Kemp", "Willis Reed",
    "Jaren Jackson Jr.", "Chet Holmgren", "Jack Sikma", "Tyson Chandler",
    "Jarrett Allen", "Andrew Bogut", "Jermaine O'Neal",
    "Robert Williams", "Serge Ibaka", 
}


# ── Do-Not-Draft list ─────────────────────────────────────────────────────────
# Players who are rock-bottom priority — the AI should essentially never pick them.
# A very large ADP penalty is applied in ai_drafter.py for any player in this set.
DO_NOT_DRAFT: set[str] = {
    "Duncan Robinson", "Anthony Parker", "Tony Allen", "Gary Payton II",
    "Bruce Bowen", "Danilo Gallinari", "Myles Turner", "John Collins",
    "John Salley", "Phil Chenier", "Pete Maravich", "Rodney McCray",
    "Walt Bellamy", "Paul Silas", "Trey Murphy",
    "Andrew Bynum", "Larry Johnson", "Kevin Martin", "Joe Harris",
    "Micheal Ray Richardson", "Ben Simmons", "James Silas", 
    "Darryl Dawkins", "Greg Ballard", "Kendall Gill", "Jim Paxson",
    "Bob Cousy", "Derrick Coleman", "Andre Roberson", "Mickey Johnson",
    "Quinn Buckner", "Tom Chambers", "Nick Anderson", "Lonnie Shelton",
    "Mo Williams", "Clint Capela", "Jusuf Nurkic", "Cedric Maxwell",
    "Tom Boerwinkle", "Richie Guerin", "Buddy Hield",
     "Danny Manning", "Norm Van Lier", "Archie Clark",
    "Jalen Rose", "Jameer Nelson", "Mark Aguirre", "Jonathan Isaac",
    "DeMarre Carroll", "Arron Afflalo", "Mark Eaton", "John Starks",
    "Joe Caldwell", "Billy Knight", "DeAndre Jordan", "Marvin Williams",
    "Chuck Person", "Derek Anderson", "Zydrunas Ilgauskas", "Glenn Robinson",
    "Nikola Vucevic", "DeMarcus Cousins", "Charlie Ward", "Hot Rod Williams",
    "Donyell Marshall", "Ricky Pierce", "Maurice Lucas", "Adrian Dantley",
    "Antawn Jamison", "Stephon Marbury", "Carlos Boozer", "Dana Barros",
    "World B. Free", "George McGinnis", "Dolph Schayes", "Sean Elliott",
    "DeMar DeRozan", "Ryan Anderson", "Avery Bradley", "Zach Randolph",
    "Andre Miller", "Larry Hughes", "Bill Sharman", "Earl Monroe",
    "Ty Lawson", "Dan Issel", "Xavier McDaniel", "Ricky Rubio",
    "Mike Dunleavy", "Raef LaFrentz", "Spencer Haywood", "Channing Frye",
    "Zelmo Beaty", "Dave Bing", "Shareef Abdur-Rahim", "Ben Gordon",
    "Calvin Murphy", "Lenny Wilkens", "Maurice Stokes", "Micheal Williams",
    "Jose Calderon", "Jack Twyman", "Bill Bridges", "Dell Curry",
    "Wil Jones", "Bobby Simmons", "Mel Daniels", "Terry Mills",
    "Bill Cartwright", "Eddie Johnson", "Charles Oakley", "KC Jones",
    "Tom Sanders", "Calvin Natt", "Ray Williams", "Mehmet Okur",
    "Rik Smits", "Brian Winters", "JoJo White", "Mychal Thompson",
    "Rudy Tomjanovich", "Jay Vincent", "Vin Baker", "Andrew Toney",
    "Lucius Allen", "Paul Arizin", "Wally Szczerbiak", "Doug Collins",
    "Tom Gola", "Thabo Sefolosha",
}


# ── Elite playmakers / floor generals ────────────────────────────────────────
# Players who elevate the offense and make good offensive players around them better.
# Some are pass-first (Nash, Stockton, Kidd), others are ball-dominant (LeBron, Magic).
# The common thread: elite court vision, decision-making, and ability to organise an offense.
ELITE_PLAYMAKERS: set[str] = {
    "Magic Johnson", "Chris Paul", "Steve Nash", "John Stockton",
    "Jason Kidd", "Oscar Robertson", "Isiah Thomas", "LeBron James",
    "Nikola Jokic",
}

# ── PnR creators ─────────────────────────────────────────────────────────────
# Guards and wings who use pick-and-roll as a primary weapon.
# These players need a C/PF who can screen and finish at the rim or pop for a
# jumper. Without a scoring big, this part of their offense is neutralised.
PNR_CREATORS: set[str] = {
    # Pass-first playmakers — ran deadly PnR systems
    "Magic Johnson", "Chris Paul", "Steve Nash", "John Stockton",
    "Jason Kidd", "Oscar Robertson", "Isiah Thomas",
    "James Harden", "Luka Doncic", "Damian Lillard",
    "Shai Gilgeous-Alexander", "Russell Westbrook",
    "Allen Iverson", "Dwyane Wade", "Trae Young", "Ja Morant",
    "Penny Hardaway", "Gilbert Arenas", "Baron Davis",
    "Deron Williams", "Gary Payton", "Derrick Rose",
    "John Wall", "Kemba Walker", "LeBron James", "Giannis Antetokounmpo","Chauncey Billups",
    "Mark Price", "Tyrese Haliburton", "Jalen Brunson", "Mike Conley", "Kyle Lowry", "Fred VanVleet",
     "Jamal Murray", "Goran Dragic", "Darius Garland", "Isaiah Thomas", "Sleepy Floyd", "Sam Cassell", "Terry Porter",
}









# ── Draft pool display categories ────────────────────────────────────────────
# Used by !draftpool Guard/Wing/Forward/Big to filter the available player list.
POOL_CATEGORIES: dict[str, list[str]] = {
    "guard": [
        "Michael Jordan", "Stephen Curry", "Magic Johnson", "Jerry West",
        "Kobe Bryant", "Dwyane Wade", "Shai Gilgeous-Alexander", "Steve Nash",
        "Chris Paul", "Oscar Robertson", "James Harden", "Luka Doncic",
        "Penny Hardaway", "Walt Frazier", "Manu Ginobili", "Mark Price",
        "Damian Lillard", "Chauncey Billups", "Sidney Moncrief", "Terry Porter",
        "Kyle Lowry", "Brandon Roy", "Devin Booker", "Deron Williams",
        "Jason Kidd", "Russell Westbrook", "Joe Dumars", "Gary Payton",
        "Kevin Johnson", "Isiah Thomas", "Tyrese Haliburton", "Victor Oladipo",
        "Kyrie Irving", "John Stockton", "Mike Conley", "Mitch Richmond",
        "Jrue Holiday", "Donovan Mitchell", "Michael Cooper", "Allen Iverson",
        "Derrick Rose", "Derek Harper", "Jalen Brunson", "Bradley Beal",
        "Jeff Hornacek", "Hersey Hawkins", "Paul Pressey", "Derrick White",
        "Tim Hardaway Sr.", "Gilbert Arenas", "Ja Morant", "Darius Garland",
        "Baron Davis", "Trae Young", "Tony Parker", "Terrell Brandon",
        "Gus Williams", "Dennis Johnson", "Hal Greer", "Ron Harper",
        "Jamal Murray", "Cade Cunningham", "Goran Dragic", "Sleepy Floyd",
        "Jason Terry", "George Hill", "De'Aaron Fox", "Lonzo Ball",
        "John Wall", "Maurice Cheeks", "Paul Westphal", "Tyrese Maxey",
        "Kemba Walker", "Fred VanVleet", "Kirk Hinrich", "Fat Lever",
        "Don Buse", "Sam Cassell", "Marcus Smart", "Alex Caruso",
        "Zach LaVine", "Mookie Blaylock", "Nate McMillan", "Byron Scott",
        "Danny Ainge", "Isaiah Thomas", "Tiny Archibald", "Malcolm Brogdon",
        "Brian Taylor", "LaMelo Ball", "JJ Redick", "Kenny Anderson",
        "Steve Francis", "Jon McGlocklin", "Phil Smith", "Fred Brown",
        "Amen Thompson", "Mike Bibby", "Drazen Petrovic", "Norman Powell",
        "Austin Reaves", "Dyson Daniels", "Lu Dort", "Randy Smith",
        "Darrell Armstrong", "Johnny Moore", "Rod Strickland", "Doc Rivers",
        "Gail Goodrich", "Louie Dampier", "Dejounte Murray", "Otis Birdsong",
        "Rajon Rondo", "Kentavious Caldwell-Pope", "Alvin Robertson",
        "Jalen Suggs", "CJ McCollum", "Gary Payton II", "Phil Chenier",
        "Pete Maravich", "Micheal Ray Richardson", "James Silas", "Bob Cousy",
        "Quinn Buckner", "Mo Williams", "Richie Guerin", "Norm Van Lier",
        "John Starks", "Charlie Ward", "Ricky Pierce", "Stephon Marbury",
        "Jameer Nelson", "Dana Barros", "World B. Free", "Avery Bradley",
        "Andre Miller", "Larry Hughes", "Earl Monroe", "Ty Lawson",
        "Ricky Rubio", "Dave Bing", "Calvin Murphy", "Lenny Wilkens",
        "Micheal Williams", "Jose Calderon", "Dell Curry", "Archie Clark",
        "KC Jones", "Ray Williams",
    ],
    "wing": [
        "Michael Jordan", "Kobe Bryant", "Kawhi Leonard", "Tracy McGrady",
        "Grant Hill", "Reggie Miller", "Paul George", "Penny Hardaway",
        "Manu Ginobili", "Clyde Drexler", "Ray Allen", "Jimmy Butler",
        "Paul Pierce", "John Havlicek", "Chris Mullin", "Sidney Moncrief",
        "Vince Carter", "Rick Barry", "Klay Thompson", "Eddie Jones",
        "Anthony Edwards", "Brandon Roy", "Devin Booker", "Marques Johnson",
        "George Gervin", "Victor Oladipo", "Mitch Richmond", "Jaylen Brown",
        "Andre Iguodala", "Khris Middleton", "Dan Majerle", "Jalen Williams",
        "Alex English", "Bradley Beal", "Mikal Bridges", "Hersey Hawkins",
        "Doug Christie", "Danny Green", "Paul Pressey", "Sam Jones",
        "Lou Hudson", "Elgin Baylor", "Desmond Bane", "Richard Hamilton",
        "Michael Finley", "Joe Johnson", "Michael Redd", "Walter Davis",
        "Reggie Lewis", "David Thompson", "Nicolas Batum", "Latrell Sprewell",
        "Kyle Korver", "Steve Smith", "Glen Rice", "Wesley Matthews",
        "Bobby Phills", "Andrew Wiggins", "Josh Howard", "Brent Barry",
        "Dick Van Arsdale", "Zach LaVine", "Scott Wedman", "Raja Bell",
        "Rolando Blackman", "Gary Harris", "LaMelo Ball", "Herb Jones",
        "Jon McGlocklin", "Phil Smith", "Kerry Kittles", "Roger Brown",
        "Mike Miller", "Wesley Person", "Amen Thompson", "Jerry Sloan",
        "Caron Butler", "Bryon Russell", "Allan Houston", "Dorian Finney-Smith",
        "Norman Powell", "Lu Dort", "Deni Avdija", "JR Smith",
        "Kentavious Caldwell-Pope", "Duncan Robinson", "Anthony Parker",
        "Bruce Bowen", "Phil Chenier", "Trey Murphy", "Kevin Martin",
        "Joe Harris", "Kendall Gill", "Jim Paxson", "Andre Roberson",
        "Nick Anderson", "Buddy Hield", "Jason Richardson", "Arron Afflalo",
        "Joe Caldwell", "Billy Knight", "Mark Aguirre", "Derek Anderson",
        "Sean Elliott", "DeMar DeRozan", "Bill Sharman", "Ben Gordon",
        "Jack Twyman", "Bobby Simmons", "Jalen Rose", "Eddie Johnson",
        "Paul Arizin", "Aaron McKie", "Cliff Hagan", "Jim McMillian",
        "Jerry Stackhouse", "Mike Riordan", "Sarunas Marciulionis",
        "Jeff Malone", "Greg Ballard", "Kelly Tripucka", "Josh Richardson",
    ],
    "forward": [
        "LeBron James", "Larry Bird", "Magic Johnson", "Kevin Durant",
        "Kawhi Leonard", "Tracy McGrady", "Giannis Antetokounmpo",
        "Julius Erving", "Karl Malone", "Grant Hill", "Scottie Pippen",
        "Jayson Tatum", "Charles Barkley", "Draymond Green", "Paul George",
        "Clyde Drexler", "Jimmy Butler", "Paul Pierce", "Rashard Lewis",
        "Marques Johnson", "Peja Stojakovic", "Bobby Jones", "Andrei Kirilenko",
        "Shawn Marion", "Shane Battier", "James Worthy", "Pascal Siakam",
        "Alex English", "Paul Millsap", "Lauri Markkanen", "Zion Williamson",
        "Gordon Hayward", "Elgin Baylor", "Luol Deng", "Ron Artest",
        "Robert Covington", "OG Anunoby", "Dennis Rodman", "Danny Granger",
        "Lamar Odom", "Tayshaun Prince", "Bernard King", "Dominique Wilkins",
        "Bob Dandridge", "Kiki VanDeWeghe", "Robert Horry", "Chet Walker",
        "Kevin McHale", "Hedo Turkoglu", "Nicolas Batum", "Connie Hawkins",
        "Carmelo Anthony", "Billy Cunningham", "Gus Johnson", "Josh Smith",
        "Boris Diaw", "Sam Perkins", "Andrew Wiggins", "Josh Howard",
        "Dave DeBusschere", "Jamaal Wilkes", "Gerald Wallace", "Clifford Robinson",
        "Dale Ellis", "Aaron Gordon", "Otto Porter Jr.", "Franz Wagner",
        "Toni Kukoc", "James Posey", "Trevor Ariza", "Jamal Mashburn",
        "Detlef Schrempf", "Brandon Ingram", "Michael Porter Jr.", "Herb Jones",
        "Joe Ingles", "Jaden McDaniels", "Bob Love", "Jerome Kersey",
        "Richard Jefferson", "Scottie Barnes", "Willie Wise", "Thabo Sefolosha",
        "Jae Crowder", "Dorian Finney-Smith", "Julius Randle", "Jalen Johnson",
        "Deni Avdija", "Terry Cummings", "PJ Tucker", "Jerami Grant",
        "Derrick McKey", "Danilo Gallinari", "John Collins", "Rodney McCray",
        "Paul Silas", "Anthony Mason", "Larry Johnson", "Ben Simmons",
        "Greg Ballard", "Mickey Johnson", "Lonnie Shelton", "Cedric Maxwell",
        "Larry Kenon", "Jonathan Isaac", "DeMarre Carroll", "Billy Knight",
        "Mark Aguirre", "Marvin Williams", "Chuck Person", "Glenn Robinson",
        "Donyell Marshall", "Adrian Dantley", "Antawn Jamison",
        "George McGinnis", "DeMar DeRozan", "Ryan Anderson", "Xavier McDaniel",
        "Mike Dunleavy", "Shareef Abdur-Rahim", "Bill Bridges", "Wil Jones",
        "Jalen Rose", "Tom Sanders", "Calvin Natt", "Tommy Heinsohn",
        "Thurl Bailey", "Kenny Sears", "Rudy Tomjanovich", "Marvin Barnes",
    ],
    "big": [
        "Kevin Garnett", "Shaquille O'Neal", "Hakeem Olajuwon",
        "Kareem Abdul-Jabbar", "Tim Duncan", "David Robinson", "Nikola Jokic",
        "Wilt Chamberlain", "Bill Walton", "Giannis Antetokounmpo",
        "Anthony Davis", "Bill Russell", "Joel Embiid", "Karl Malone",
        "Dirk Nowitzki", "Draymond Green", "Bob McAdoo", "Dwight Howard",
        "Moses Malone", "Patrick Ewing", "Shawn Kemp", "Willis Reed",
        "Rasheed Wallace", "Larry Nance Sr.", "Victor Wembanyama",
        "Evan Mobley", "Marc Gasol", "Pau Gasol", "Dave Cowens",
        "Alonzo Mourning", "Chris Bosh", "Al Horford", "Jaren Jackson Jr.",
        "Bob Lanier", "Chet Holmgren", "Paul Millsap", "Karl-Anthony Towns",
        "Rudy Gobert", "Bam Adebayo", "Chris Webber", "Kevin Love",
        "Horace Grant", "Artis Gilmore", "Kevin McHale", "David West",
        "Jack Sikma", "Ben Wallace", "Joakim Noah", "Nate Thurmond",
        "Blake Griffin", "Dikembe Mutombo", "Tyson Chandler", "Alvan Adams",
        "Ivica Zubac", "Robert Parish", "Amar'e Stoudemire", "Sam Perkins",
        "Jarrett Allen", "Kristaps Porzingis", "Brad Daugherty", "Bob Pettit",
        "Elton Brand", "Antonio McDyess", "Clifford Robinson", "Andrew Bogut",
        "Jermaine O'Neal", "Sam Lacey", "Arvydas Sabonis", "Bo Outlaw",
        "Brook Lopez", "Robert Williams", "Vlade Divac", "Brad Miller",
        "Yao Ming", "PJ Brown", "Elvin Hayes", "Wes Unseld", "Buck Williams",
        "Dan Roundfield", "Isaiah Hartenstein", "Theo Ratliff", "Deandre Ayton",
        "Serge Ibaka", "Domantas Sabonis", "Clifford Ray", "Ralph Sampson",
        "Nicolas Claxton", "Marcus Camby", "LaMarcus Aldridge", "Kenyon Martin",
        "Jerry Lucas", "Bill Laimbeer", "Myles Turner", "John Collins",
        "John Salley", "Walt Bellamy", "Andrew Bynum", "Nene Hilario",
        "Darryl Dawkins", "Mickey Johnson", "Derrick Coleman", "Tom Chambers",
        "Lonnie Shelton", "Clint Capela", "Jusuf Nurkic", "Tom Boerwinkle",
        "Danny Manning", "Mark Eaton", "DeAndre Jordan", "Zydrunas Ilgauskas",
        "Nikola Vucevic", "DeMarcus Cousins", "Hot Rod Williams",
        "Maurice Lucas", "Carlos Boozer", "Dolph Schayes", "Zach Randolph",
        "Dan Issel", "Raef LaFrentz", "Spencer Haywood", "Channing Frye",
        "Zelmo Beaty", "Maurice Stokes", "Bill Bridges", "Mel Daniels",
        "Terry Mills", "Bill Cartwright", "Charles Oakley", "Calvin Natt",
        "Mehmet Okur", "Shareef Abdur-Rahim", "Neil Johnston", "Elmore Smith",
        "Tree Rollins", "Vin Baker", "Mychal Thompson", "Troy Murphy",
        "Billy Paultz", "Mitchell Robinson", "Jeff Ruland", "Rick Mahorn",
        "Rik Smits", "Manute Bol", "Kendrick Perkins", "Ed Macauley",
    ],
}

# Aliases so users can type partial names
POOL_CATEGORY_ALIASES: dict[str, str] = {
    "guard":   "guard",  "guards":   "guard", "g": "guard",
    "wing":    "wing",   "wings":    "wing",  "w": "wing",
    "forward": "forward","forwards": "forward","f": "forward","fwd": "forward",
    "big":     "big",    "bigs":     "big",   "b": "big",    "big man": "big",
}


def get_pool_category(key: str) -> list[str] | None:
    """Return the ordered player list for a display category, or None if unrecognised."""
    return POOL_CATEGORIES.get(POOL_CATEGORY_ALIASES.get(key.lower()))


def get_tier(player: str) -> int:
    """Return tier 1-11 for a player (1 = best). Unlisted players are tier 11."""
    for name, tier in PLAYER_TIERS.items():
        if name.lower() == player.lower():
            return tier
    return 11


def is_shooter(player: str) -> bool:
    return any(p.lower() == player.lower() for p in SHOOTERS)


def get_positions(player: str) -> list[str]:
    """Return list of valid positions for a player (e.g. ['SF', 'PF'])."""
    valid = {'PG', 'SG', 'SF', 'PF', 'C'}
    for name, pos_str in PLAYER_POSITIONS.items():
        if name.lower() == player.lower():
            return [p.strip().upper() for p in pos_str.split('/') if p.strip().upper() in valid]
    return []


def is_ball_dominant(player: str) -> bool:
    return any(p.lower() == player.lower() for p in BALL_DOMINANT)


def is_shot_creator(player: str) -> bool:
    return any(p.lower() == player.lower() for p in SHOT_CREATORS)


def is_high_portability(player: str) -> bool:
    return any(p.lower() == player.lower() for p in HIGH_PORTABILITY)


def is_non_scoring_big(player: str) -> bool:
    return any(p.lower() == player.lower() for p in NON_SCORING_BIGS)


def is_soft_big(player: str) -> bool:
    return any(p.lower() == player.lower() for p in SOFT_BIGS)


def is_immobile_center(player: str) -> bool:
    return any(p.lower() == player.lower() for p in IMMOBILE_CENTERS)


def is_versatile_defender(player: str) -> bool:
    return any(p.lower() == player.lower() for p in VERSATILE_DEFENDERS)


def is_perimeter_defender(player: str) -> bool:
    return any(p.lower() == player.lower() for p in PERIMETER_DEFENDERS)


def is_elite_rim_protector(player: str) -> bool:
    return any(p.lower() == player.lower() for p in ELITE_RIM_PROTECTORS)


def is_elite_playmaker(player: str) -> bool:
    return any(p.lower() == player.lower() for p in ELITE_PLAYMAKERS)


def is_pnr_creator(player: str) -> bool:
    return any(p.lower() == player.lower() for p in PNR_CREATORS)


def is_do_not_draft(player: str) -> bool:
    return any(p.lower() == player.lower() for p in DO_NOT_DRAFT)
