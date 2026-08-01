from langchain_core.prompts import ChatPromptTemplate

MAX_QUESTIONS = 5

PLAN_QUESTIONNAIRE_PROMPT = """\
You are a business consultant interviewing an entrepreneur who just shared their business idea.

Business idea: {idea}

First, extract every concrete detail already stated in the idea (target location, customer segment, product, scale, funding, etc.) into the "facts" object. Take as much as you can from the prompt.

Then, generate up to {max_questions} deep, specific questions to fill the most important remaining gaps for writing a strong business report: market and competitors, target customer, pricing and revenue model, differentiation, capital and costs, operations, regulations.

Rules:
- Between 1 and {max_questions} questions, no more. Fewer is better if the idea is already detailed.
- Questions must be concrete and build on the idea, not generic yes/no questions.
- Do NOT ask about anything already covered by the extracted facts.

Return ONLY valid JSON with this exact shape, nothing else:
{{"facts": {{"business_about": "<full idea text>", "business_location": "<if stated else empty string>", "business_vision": "<if stated else empty string>", "target_customers": "<if stated else empty string>"}},
 "questions": [{{"key": "q1", "question": "..."}}, {{"key": "q2", "question": "..."}}]}}

Make sure the JSON is valid and complete."""

PLAN_QUESTIONNAIRE_TEMPLATE = ChatPromptTemplate.from_messages(
    [("human", PLAN_QUESTIONNAIRE_PROMPT)]
)

PARSE_ANSWERS_PROMPT = """\
You are processing an entrepreneur's free-form answers to a questionnaire about their business.

Questions:
{questions}

The entrepreneur replied with the following text (may be numbered, separated by newlines, or one big paragraph):

{answers}

Return ONLY valid JSON: an array of strings aligned 1:1 with the questions above (same order and count). Use an empty string for any question that was not answered.

Example: ["answer 1", "answer 2"]"""

PARSE_ANSWERS_TEMPLATE = ChatPromptTemplate.from_messages(
    [("human", PARSE_ANSWERS_PROMPT)]
)
