"""
Probe domain definitions
========================
Single canonical source for every domain KBF knows about. Each entry carries
all fields any caller needs:

  - name           — human-readable display name (logs / progress)
  - tolerance      — numeric tolerance, interpreted via tolerance_mode
  - tolerance_mode — "absolute" or "relative"
  - value_range    — (lo, hi) accepted for parsed answers
  - cloze_template — sentence with one {name} slot, used by the verifier
  - gen_prompt     — model prompt for generate_probes (with {theme} slot)
  - themes         — list of generation themes (rotated round-robin)

To add a new domain:
  1. Insert a new entry below with the seven fields.
  2. That's it — kbf_common.py and generate_probes.py pick it up automatically.

The three thin lookup tables at the bottom (DOMAIN_CLOZE, DOMAIN_RANGES,
DOMAIN_TOL) are derived dicts kept for back-compat with the existing
kbf_common.cloze_query_batch and check_match call sites.
"""

DOMAINS = {
    "chemistry_bp": {
        "name": "Chemistry (boiling points)",
        "tolerance": 3.0, "tolerance_mode": "absolute",
        "value_range": (-300, 600),
        "cloze_template": "The boiling point of {name} at 1 atm is ___°C.",
        "gen_prompt": (
            "List 15 obscure chemical compounds with well-defined boiling points at 1 atm. "
            "Focus on: {theme}.\n"
            "Only include compounds a specialist chemist would know.\n"
            "Format each line EXACTLY as: compound_name | boiling_point_celsius\n"
            "Example: trimethylchlorosilane | 57\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "organometallics and organosilicons with unusual bonding",
            "heterocyclic nitrogen compounds and azoles",
            "halogenated compounds (fluoro-, chloro-, bromo-) with 5+ carbons",
            "sulfur and phosphorus compounds used in synthesis",
            "esters, ethers, and acid anhydrides of obscure acids",
            "industrial specialty chemicals and reagents",
            "organoselenium and organotellurium compounds",
            "strained ring systems and bridged compounds (norbornane derivatives, adamantanes)",
        ],
    },
    "chemistry_mp": {
        "name": "Chemistry (melting points)",
        "tolerance": 3.0, "tolerance_mode": "absolute",
        "value_range": (-300, 4000),
        "cloze_template": "The melting point of {name} is ___°C.",
        "gen_prompt": (
            "List 15 obscure chemical compounds or minerals with well-defined melting points. "
            "Focus on: {theme}.\n"
            "Only include compounds a specialist would know.\n"
            "Format each line EXACTLY as: compound_name | melting_point_celsius\n"
            "Example: sodium azide | 275\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "metal oxides and ceramics",
            "organic crystals and pharmaceutical intermediates",
            "inorganic salts and coordination compounds",
            "intermetallic compounds and alloys",
        ],
    },
    "physics": {
        "name": "Physics (material properties)",
        "tolerance": 0.02, "tolerance_mode": "relative",
        "value_range": (0, 1e15),
        "cloze_template": "The numerical value of {name} in SI units is ___.",
        "gen_prompt": (
            "List 15 obscure physical constants, material properties, or measured quantities. "
            "Focus on: {theme}.\n"
            "Only include values a specialist physicist would know.\n"
            "Format each line EXACTLY as: quantity_description | numerical_value_in_SI_units\n"
            "Example: speed of sound in steel | 5960\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "specific heat capacities and thermal conductivities",
            "refractive indices and optical properties",
            "nuclear and particle physics constants",
            "acoustic properties and sound velocities",
        ],
    },
    "astronomy": {
        "name": "Astronomy (periods & distances)",
        "tolerance": 0.05, "tolerance_mode": "relative",
        "value_range": (0, 1e15),
        "cloze_template": "The numerical value of {name} is ___.",
        "gen_prompt": (
            "List 15 obscure astronomical measurements. "
            "Focus on: {theme}.\n"
            "Only include values a professional astronomer would know. "
            "Avoid famous objects (Moon, Mars, Jupiter, Sirius, Polaris). "
            "Focus on obscure moons, minor planets, dim stars, or unusual objects.\n"
            "Format each line EXACTLY as: object_or_quantity | numerical_value\n"
            "Example: orbital period of Io in days | 1.769\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "orbital periods of irregular moons of Uranus and Neptune (in days)",
            "orbital periods of Kuiper belt objects and trans-Neptunian objects (in years)",
            "apparent magnitudes of named variable stars and binary systems",
            "distances to specific open clusters and globular clusters (in light-years)",
            "rotation periods of asteroids and dwarf planets (in hours)",
            "semi-major axes of minor planet orbits (in AU)",
        ],
    },
    "biology": {
        "name": "Biology (chromosome numbers)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (1, 2000),
        "cloze_template": "The diploid chromosome number (2n) of {name} is ___.",
        "gen_prompt": (
            "List 15 obscure organisms with well-known diploid chromosome numbers (2n). "
            "Focus on: {theme}.\n"
            "Only include organisms a specialist biologist would know. "
            "Avoid common organisms (human, dog, cat, mouse, fruit fly, wheat). "
            "Focus on taxonomically unusual or rarely discussed species.\n"
            "Format each line EXACTLY as: organism_latin_name | 2n_number\n"
            "Example: jack jumper ant (Myrmecia pilosula) | 2\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "ferns and lycopods with extreme polyploidy",
            "deep-sea fish and cartilaginous fish (rays, skates, chimeras)",
            "parasitic wasps, beetles, and social insects with unusual karyotypes",
            "lichens, slime molds, and extremophile microorganisms",
            "marsupials, monotremes, and xenarthrans",
            "amphibians with notably high or low chromosome counts",
        ],
    },
    "math": {
        "name": "Math (special values)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (-1e10, 1e10),
        "cloze_template": "The numerical value of {name} is ___.",
        "gen_prompt": (
            "List 15 obscure but well-defined INTEGER mathematical values. "
            "Focus on: {theme}.\n"
            "Each value must be a specific integer (not a formula). "
            "Avoid famous constants (pi, e, phi, Euler-Mascheroni). "
            "Focus on specific integer values that require genuine mathematical knowledge.\n"
            "Format each line EXACTLY as: description | integer_value\n"
            "Example: number of groups of order 128 | 2328\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "specific OEIS sequence values at non-trivial indices (partition numbers, Ramanujan tau, Dedekind numbers, etc.)",
            "number theory: specific values of arithmetic functions (Euler totient at large n, Mertens function values, prime-counting function at specific points)",
            "combinatorics: specific Catalan, Motzkin, Bell, or Stirling numbers at medium indices",
            "group theory: number of groups of given order, number of Latin squares, graph coloring numbers",
            "specific terms of well-known but hard-to-compute sequences (Fibonacci at n=50+, factorials at n=20+, primorial values)",
            "algebraic number theory: class numbers of specific number fields, discriminants of quadratic forms",
        ],
    },
    "programming": {
        "name": "Programming (release years)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (1950, 2030),
        "cloze_template": "{name} occurred in the year ___.",
        "gen_prompt": (
            "List 15 obscure but well-defined dates in programming/computing history. "
            "Focus on: {theme}.\n"
            "Only include facts a specialist would know. "
            "Avoid well-known languages (C, Python, Java, JavaScript, Go, Rust, Ruby, PHP, Perl, Haskell). "
            "Focus on niche/forgotten tools that only a computing historian would know.\n"
            "Format each line EXACTLY as: event_description | year\n"
            "Example: first release of Erlang | 1986\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "first releases of obscure/forgotten programming languages from the 1960s-1980s (SNOBOL, SETL, CLU, Mesa, Modula, BLISS, BCPL, Occam, REXX, etc.)",
            "first releases of niche build systems, package managers, and dev tools (CMake, Bazel, Nix, Guix, Meson, etc.)",
            "publication years of lesser-known but important CS papers and technical reports",
            "release years of obscure operating systems, embedded RTOSes, and research OSes (Plan 9, Inferno, Minix 3, QNX, VxWorks, seL4, etc.)",
            "founding years of lesser-known tech companies, research labs, and open-source foundations",
            "first releases of obscure database systems, message queues, and distributed systems (RethinkDB, CockroachDB, FoundationDB, ZeroMQ, etc.)",
        ],
    },
    "medical": {
        "name": "Medical (drug half-lives)",
        "tolerance": 0.1, "tolerance_mode": "relative",
        "value_range": (0.01, 5000),
        "cloze_template": "The elimination half-life of {name} is ___ hours.",
        "gen_prompt": (
            "List 15 pharmaceutical drugs with well-known elimination half-lives in hours. "
            "Focus on: {theme}.\n"
            "Only include drugs a clinical pharmacologist would know the half-life of. "
            "Avoid extremely common drugs (aspirin, acetaminophen, ibuprofen, metformin). "
            "Focus on drugs with precisely documented but less commonly known half-lives.\n"
            "Format each line EXACTLY as: drug_name | half_life_in_hours\n"
            "Example: amiodarone | 1080\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "antiepileptic and anticonvulsant drugs",
            "antipsychotics and mood stabilizers",
            "anticoagulants and antiplatelet agents",
            "immunosuppressants and biologics",
            "antifungal and antiparasitic agents",
            "cardiovascular drugs",
        ],
    },
    "pop_culture": {
        "name": "Pop culture (box office & ratings)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (1, 10000),
        "cloze_template": "The numerical value of {name} is ___.",
        "gen_prompt": (
            "List 15 obscure but well-documented numerical facts from pop culture. "
            "Focus on: {theme}.\n"
            "Only include facts that a dedicated fan or pop culture historian would know. "
            "Avoid mega-hits and universally known franchises (Marvel Avengers, Star Wars main films, Harry Potter). "
            "Focus on mid-tier or cult properties with precisely documented numbers.\n"
            "Format each line EXACTLY as: description | number\n"
            "Example: worldwide box office of Moon (2009) in millions USD | 10\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "worldwide box office gross (in millions USD) of cult, indie, or mid-budget films from 2000-2020",
            "total episode counts of completed but lesser-known TV series (anime, British TV, cable dramas, web series)",
            "number of issues/volumes of lesser-known manga, comic book runs, or graphic novel series",
            "peak Billboard/UK chart positions of one-hit wonders or niche artists from 1980s-2010s",
            "Metacritic or Rotten Tomatoes scores of cult classic or controversial films",
            "release years of obscure but influential video games, albums, or films from the 1990s-2010s",
        ],
    },
    "internet_culture": {
        "name": "Internet culture (dates & numbers)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (1, 2030),
        "cloze_template": "The numerical value of {name} is ___.",
        "gen_prompt": (
            "List 15 obscure but well-documented numerical facts from internet history and culture. "
            "Focus on: {theme}.\n"
            "Only include facts that a dedicated internet historian or niche community member would know. "
            "Avoid universally known facts (Facebook founded 2004, YouTube 2005). "
            "Focus on precise dates, numbers, or statistics from niche corners of the internet.\n"
            "Format each line EXACTLY as: description | number\n"
            "Example: year the Something Awful forums launched | 1999\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "founding or launch years of obscure forums, imageboards, wikis, and online communities",
            "years when famous internet memes, copypastas, or viral events originated",
            "founding years or key dates of niche internet services, protocols, and platforms (IRC networks, BBS systems, early social networks, Usenet groups)",
            "version numbers or release years of historically important but obscure software, browser plugins, or web standards",
            "years of notable internet incidents, outages, exploits, or community events",
            "founding years of lesser-known tech startups, open-source projects, or internet organizations",
        ],
    },
    "chinese_history": {
        "name": "Chinese history (specific dates)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (-3000, 2030),
        "cloze_template": "{name}发生在公元___年。",
        "gen_prompt": (
            "列出15个中国历史中的精确年份或数字事实。聚焦于: {theme}。\n"
            "只包含历史专家才会知道的精确事实，避免太基础的常识。\n"
            "Format each line EXACTLY as: event_description | year\n"
            "Example: 明朝永乐大典编纂完成 | 1408\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "中国古代科举制度的关键变革年份",
            "中国古代重要战役的具体年份",
            "中国历代重要典籍的编纂或刊刻年份",
            "中国近代史中不太知名但重要的条约、协议的签订年份",
            "中国古代天文历法的关键年份",
            "中国古代重要工程的竣工年份",
        ],
    },
    "chinese_geography": {
        "name": "Chinese geography (county statistics)",
        "tolerance": 0.05, "tolerance_mode": "relative",
        "value_range": (1, 1e9),
        "cloze_template": "{name}的数值是___。",
        "gen_prompt": (
            "列出15个中国地理相关的精确数字事实。聚焦于: {theme}。\n"
            "只包含地理专业人士才会知道的精确数值。\n"
            "Format each line EXACTLY as: description | number\n"
            "Example: 鄱阳湖最大面积（平方千米） | 4125\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "中国各省会城市的海拔高度（米），聚焦西部和东北城市",
            "中国主要河流的长度（千米），避免长江黄河",
            "中国主要湖泊的面积（平方千米）",
            "中国各地级市的面积（平方千米），聚焦中西部不太知名的城市",
            "中国主要山峰的高度（米）",
            "中国各省的耕地面积（万公顷）或森林覆盖率数据",
        ],
    },
    "chinese_internet": {
        "name": "Chinese internet (platform dates)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (1990, 2030),
        "cloze_template": "{name}发生在___年。",
        "gen_prompt": (
            "列出15个中国互联网历史中的精确年份。聚焦于: {theme}。\n"
            "只包含中国互联网深度用户才会知道的精确日期。\n"
            "Format each line EXACTLY as: description | year\n"
            "Example: 豆瓣网上线年份 | 2005\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "中国早期互联网平台的创立年份",
            "中国主要互联网产品的上线或发布年份",
            "中国互联网重大事件的年份（3Q大战、百团大战、千播大战、社区团购等）",
            "中国游戏行业重要节点的年份",
            "中国互联网公司重要并购或上市的年份",
            "中国互联网监管政策的关键年份",
        ],
    },
    "chinese_literature": {
        "name": "Chinese literature (publication years)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (-3000, 2030),
        "cloze_template": "{name}的数值是___。",
        "gen_prompt": (
            "列出15个中国文学相关的精确年份或数字事实。聚焦于: {theme}。\n"
            "只包含文学研究者才会知道的精确事实。\n"
            "Format each line EXACTLY as: description | year_or_number\n"
            "Example: 鲁迅《狂人日记》发表年份 | 1918\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "中国现代文学重要作品的首次发表或出版年份",
            "中国当代文学重要作品的出版年份",
            "中国古典诗词名篇的创作年份或诗人的生卒年",
            "中国古典小说的成书或刊刻年份",
            "中国文学期刊和文学运动的关键年份",
            "中国科幻和网络文学里程碑年份",
        ],
    },
    "crypto_params": {
        "name": "Cryptography (parameters)",
        "tolerance": 0, "tolerance_mode": "absolute",
        "value_range": (0, 1e10),
        "cloze_template": "The numerical value of {name} is ___.",
        "gen_prompt": (
            "List 15 obscure cryptographic parameters. Focus on: {theme}.\n"
            "Format each line EXACTLY as: description | value\n"
            "ONLY output the list, nothing else."
        ),
        "themes": [
            "number of rounds, block sizes, and key sizes in lesser-known ciphers",
            "specific parameter values in hash functions and MACs",
            "year of publication or standardization of cryptographic algorithms",
            "specific bit lengths, polynomial degrees, and group orders in post-quantum schemes",
        ],
    },
}


# Progressive difficulty rotation for adaptive frontier generation. Index into
# this list grows with the round number; later rounds push the model harder.
DIFFICULTY_TIERS = [
    ("obscure", ""),
    ("obscure", ""),
    ("very obscure, specialist-only",
     "Only include items that require specialist training to know. "),
    ("very obscure, specialist-only",
     "Only include items that require specialist training to know. "),
    ("extremely obscure, deep-expert-only",
     "Only include items that even specialists might need to look up. "
     "These should be at the very edge of documented knowledge. "),
    ("extremely obscure, deep-expert-only",
     "Only include items that even specialists might need to look up. "
     "These should be at the very edge of documented knowledge. "),
    ("extraordinarily obscure",
     "Only include items from highly specialized reference works, niche databases, "
     "or obscure technical reports. A world expert might debate these values. "),
    ("extraordinarily obscure",
     "Only include items from highly specialized reference works, niche databases, "
     "or obscure technical reports. A world expert might debate these values. "),
]


# ── Derived lookup tables ──
# Kept for back-compat with kbf_common's cloze_query_batch / check_match call
# sites. Computed from DOMAINS so they cannot drift out of sync.

DOMAIN_CLOZE = {k: v["cloze_template"] for k, v in DOMAINS.items()}
DOMAIN_RANGES = {k: v["value_range"] for k, v in DOMAINS.items()}
DOMAIN_TOL = {k: (v["tolerance"], v["tolerance_mode"]) for k, v in DOMAINS.items()}
