"""Explicitly confirmed player identities. Never merge by fuzzy similarity."""

PLAYER_ALIASES = {
    'wiltchamberlain': 'Wilt Chamberlain',
    'wiltchaimberlain': 'Wilt Chamberlain',
    'richardhamilton': 'Richard Hamilton',
    'riphamiliton': 'Richard Hamilton',
    'riphamilton': 'Richard Hamilton',
    'richardhamiliton': 'Richard Hamilton',
    'richardhamliton': 'Richard Hamilton',
    'laurimarkannen': 'Lauri Markkanen',
    'laurimarkennan': 'Lauri Markkanen',
    'dwaynewade': 'Dwyane Wade',
    'stephcurry': 'Stephen Curry',
    'braddaughtery': 'Brad Daugherty',
    'dikembemutumbo': 'Dikembe Mutombo',
    'danillogallinari': 'Danilo Gallinari',
    'michealjordan': 'Michael Jordan',
    'dirknowitzski': 'Dirk Nowitzki',
    'jjreddick': 'JJ Redick',
    'gibertarenas': 'Gilbert Arenas',
    'jarretallen': 'Jarrett Allen',
    'herseyhawkings': 'Hersey Hawkins',
    'johnmcglocklin': 'Jon McGlocklin',
    'danrounfield': 'Dan Roundfield',
    'traceymcgrady': 'Tracy McGrady',
    'kerrykitles': 'Kerry Kittles',
    'pacalsiakam': 'Pascal Siakam',
    'tomchamber': 'Tom Chambers',
    'louhoudson': 'Lou Hudson',
    'wiliewise': 'Willie Wise',
    'giannisantetoukounmpo': 'Giannis Antetokounmpo',
    'giannisantetekounmpo': 'Giannis Antetokounmpo',
    'giannisantetokoumnpo': 'Giannis Antetokounmpo',
    'oganounoby': 'OG Anunoby',
    'ogannuoby': 'OG Anunoby',
    'victorwembanyana': 'Victor Wembanyama',
    'victorwembenyama': 'Victor Wembanyama',
    'amarestoudamire': "Amar'e Stoudemire",
    'shaquilleoneil': "Shaquille O'Neal",
    'alvinadams': 'Alvan Adams',
    'brandoyroy': 'Brandon Roy',
    'jamaalmashbrun': 'Jamal Mashburn',
    'pejastockavic': 'Peja Stojaković',
    'herbertjones': 'Herb Jones',
    'nicbatum': 'Nicolas Batum',
    'ottoporter': 'Otto Porter Jr.',
    'robertwilliams': 'Robert Williams III',
}

# Canonical names should also display consistently when they appear before an alias.
import re
import unicodedata
for _name in list(PLAYER_ALIASES.values()):
    _folded = ''.join(c for c in unicodedata.normalize('NFKD', _name).casefold() if not unicodedata.combining(c))
    PLAYER_ALIASES.setdefault(re.sub(r'[^a-z0-9]', '', _folded), _name)
