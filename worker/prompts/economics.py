from langchain_core.prompts import ChatPromptTemplate

ECONOMICS_PLAN_PROMPT = """\
You are a data-request planner for KapexAI, a business consultant assistant. You turn a user's \
request about economic or market data into a single API call plan.

Business context gathered so far:
{context}

Conversation so far:
{transcript}

Latest user request:
{user_input}

Choose EXACTLY ONE of the following operations and fill in its arguments from the request:

1. "world_bank_indicator" — a time series for one World Bank indicator and country/region.
   args: {{"country_code": "<ISO 3166-1 alpha-2 or region code, e.g. IN, US, WLD>", \
"indicator_code": "<World Bank indicator id, e.g. NY.GDP.MKTP.CD, SP.POP.TOTL, FP.CPI.TOTL.ZG>", \
"start_year": <int, default 2015>, "end_year": <int or null, default current year>}}

2. "world_bank_country_profile" — metadata about a country (region, income level, capital).
   args: {{"country_code": "<ISO 3166-1 alpha-2 code>"}}

3. "bls_time_series" — U.S. labor/inflation/wage series from BLS by series ID.
   args: {{"series_ids": ["<BLS series id>", ...] (1 to 20), \
"start_year": <int or null>, "end_year": <int or null>}}

4. "exchange_rate_series" — currency exchange rates from the Frankfurter API.
   args: {{"base_currency": "<3-letter code>", "quote_currencies": ["<3-letter code>", ...], \
"start_date": "<YYYY-MM-DD or null>", "end_date": "<YYYY-MM-DD or null>", "group": "<week|month|null>"}}

Guidance:
- Prefer "world_bank_indicator" for GDP, inflation, population, unemployment, trade, or similar \
macro indicators. "world_bank_country_profile" is for country facts like income level or region.
- Use "bls_time_series" only for explicitly U.S. labor/inflation/wage statistics (CPI-U, unemployment, \
median wages). If the request mentions inflation but no country, World Bank inflation is safer.
- Use "exchange_rate_series" for currency conversion or exchange-rate questions.
- If the request does not clearly map to any operation, still pick the closest one.

Return ONLY valid JSON with this exact shape, nothing else:
{{"operation": "<one of the four names>", "args": {{...}}}}
Keep the JSON valid and complete."""

ECONOMICS_PLAN_TEMPLATE = ChatPromptTemplate.from_messages(
    [("human", ECONOMICS_PLAN_PROMPT)]
)

ECONOMICS_SUMMARY_PROMPT = """\
You are a business consultant summarising live economic data for an entrepreneur or small business \
owner. Write a CONCISE, well-organized summary of around 200-300 words.

The user asked:
{request}

Business context:
{context}

Conversation so far:
{transcript}

Raw data returned by the API:
{data}

Guidance:
- Ground every claim in the raw data — do NOT invent numbers that are not present.
- Translate the figures into plain language and explain briefly what they mean for a small business \
(e.g. what inflation or an exchange rate move implies for costs, pricing, or a market).
- If a value is missing or null, say so instead of guessing.
- Use short paragraphs and a few bullet points. Do not repeat the user's question back.
- End with a short "Next steps" section that asks the user 1-2 specific follow-up questions \
(e.g. about a region, currency pair, or indicator they'd like to compare) to keep the \
conversation moving."""

ECONOMICS_SUMMARY_TEMPLATE = ChatPromptTemplate.from_messages(
    [("human", ECONOMICS_SUMMARY_PROMPT)]
)
