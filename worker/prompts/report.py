from langchain_core.prompts import ChatPromptTemplate

REPORT_PROMPT = """\
You are a senior business strategy consultant. Turn the research below into a \
professional structured business report.

Format requirements:
- Organize the content under clear headings and sub-headings.
- Write the content under each heading using bullet points.
- Keep it concise, precise, and professional.

Research:
{result}"""

REPORT_TEMPLATE = ChatPromptTemplate.from_messages([
    ("human", REPORT_PROMPT),
])
