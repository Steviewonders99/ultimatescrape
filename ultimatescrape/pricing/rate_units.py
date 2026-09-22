"""rate_unit codebook for projects_db rate tables.

POPULATED FROM DISCOVERY, NOT GUESSED. Provenance: accounts_db.um_dict
(NOT projects_db/crowd_db/xxjob_db — the dictionary table lives in a
different platform DB than the rate tables it decodes), query run
2026-09-22 via the onetake proxy (read-only):

    SELECT * FROM um_dict WHERE code ~* 'rate|unit' LIMIT 50;

Live rows under parent_code='rate_unit' (status=1, is_deleted=0) — the
canonical codebook, `value` is the code stored in each source table's
`rate_unit` column, `name` is the platform's human label:

    id=41  value='1'  name='Per Hour'          code=rate_unit_0001
    id=42  value='2'  name='Per Word'          code=rate_unit_0002
    id=43  value='3'  name='Per Page'          code=rate_unit_0003
    id=44  value='4'  name='Per Character'     code=rate_unit_0004
    id=45  value='5'  name='Per Count'         code=rate_unit_0005
    id=46  value='6'  name='Per Piece'         code=rate_unit_0006
    id=47  value='7'  name='Per Task'          code=rate_unit_0007
    id=48  value='8'  name='Per Hit'           code=rate_unit_0008
    id=49  value='9'  name='Per Day'           code=rate_unit_0009
    id=50  value='10' name='Per Month'         code=rate_unit_0010
    id=51  value='11' name='Per Topic'         code=rate_unit_0011
    id=52  value='12' name='Per Minute'        code=rate_unit_0012
    id=53  value='13' name='Per Approved Hit'  code=rate_unit_0013
    id=4720 value='14' name='Per Audio Hour'   code=rate_unit_0014
    id=5262 value='15' name='Per Session'      code=rate_unit_0015
    id=5298 value='16' name='Per Segment'      code=rate_unit_0016

(There are also parent_code='rate_unit_ai' / 'rate_unit_ls' / 'rate_unit_df'
sub-codebooks — per-vertical historical variants of the same numbering.
Sampled and confirmed the value->meaning mapping is IDENTICAL wherever the
codes overlap, e.g. rate_unit_df_0002 value='1' is also 'Per Hour',
rate_unit_ls_0001 value='2' is also 'Per Word' — so one flat value-keyed
table is safe and correct across all four rate source tables.)

Sanity-checked against the measured shape (2026-09-22, projects_db, live
proxy, `GROUP BY rate_unit` on each source table):

    project_resource_crowd_service_rate: rate_unit='1' n=4    avg(rate)=15.0
                                          rate_unit='2' n=5955 avg(rate)=0.0189
    project_lang_pair:                   rate_unit='1' n=6036 avg(rate)=48.918
                                          rate_unit='2' n=16027 avg(rate)=0.2729

matches the brief's stated hourly-shaped ('1' ~15/~49) and per-word-shaped
('2' ~0.02/~0.27) exactly. code '0' appears in configuration_client_rate
(n=952, avg~1.0) but has NO row anywhere under parent_code='rate_unit' —
unmapped, decodes to None, excluded from hourly benchmarks per spec.

configuration_client_rate rate_unit='2' also contains a cluster of $250
flat "language pair" fees (one project, one timestamp, 9 target locales)
mixed in with legitimate ~$0.02-0.03/word rows under the SAME code — a
platform-side data-entry inconsistency, not a decode error. Left as-is;
the codebook decodes the code as documented, it does not second-guess the
platform's own tagging.
"""

from __future__ import annotations

from dataclasses import dataclass

RATE_UNIT_DECODE: dict[str, str] = {
    "1": "hour",
    "2": "word",
    "3": "page",
    "4": "character",
    "5": "count",
    "6": "piece",
    "7": "task",
    "8": "hit",
    "9": "day",
    "10": "month",
    "11": "topic",
    "12": "minute",
    "13": "approved_hit",
    "14": "audio_hour",
    "15": "session",
    "16": "segment",
}


def decode(code: object) -> str | None:
    return RATE_UNIT_DECODE.get(str(code)) if code is not None else None


@dataclass(frozen=True)
class RateSource:
    table: str
    pk_col: str
    project_col: str | None
    locale_col: str | None
    side: str  # 'buy' | 'sell'


#: Column names verified against information_schema + live samples,
#: 2026-09-22, projects_db via the onetake proxy.
#:
#: project_resource_crowd_service_rate: id, crowd_service_id, rate_unit,
#:   rate, currency, special_condition, created_time, created_by. No
#:   project_id and no locale/language column at all (only a
#:   crowd_service_id FK) -> project_col=None, locale_col=None.
#:
#: project_lang_pair: has project_id AND source_language/target_language
#:   (both well-formed xx_YY, e.g. 'ru_RU', 'en_US') -> target_language
#:   used as locale_col per the brief's lang-pair hint.
#:
#: project_resource_info: NO project_id column (job_request_id,
#:   resource_id, recruiter_id, job_id instead -> would require a join,
#:   out of scope for a single-table proxy pull) -> project_col=None.
#:   DOES carry a direct `locale` column, already well-formed xx_YY
#:   (e.g. 'de_DE', 'en_US', 'zh_HK').
#:
#: configuration_client_rate: has project_id AND target_language
#:   (well-formed xx_YY, e.g. 'de_DE', 'fr_FR') -> locale_col.
#:
#: All four tables have real, populated `rate` and `rate_unit` columns
#: (5,960 / 24,464 / 374,558 / 31,608 rows respectively as of 2026-09-22)
#: -- none dropped.
RATE_SOURCES: list[RateSource] = [
    RateSource("project_resource_crowd_service_rate", "id", None, None, "buy"),
    RateSource("project_lang_pair", "id", "project_id", "target_language", "buy"),
    RateSource("project_resource_info", "id", None, "locale", "buy"),
    RateSource("configuration_client_rate", "id", "project_id", "target_language", "sell"),
]
